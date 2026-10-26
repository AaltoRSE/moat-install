"""CLI parsing, orchestration, and exit codes.

The ``moat-install`` entry point (``[project.scripts]`` in pyproject.toml and
``python -m moat-install`` via ``__main__.py``). Contains zero
harness-specific branches: everything is driven by the provider registry
(``providers.py``) and the adapter registry (``adapters/__init__.py``).
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .adapters import ADAPTERS, ProviderSelection, adapter_names, build_adapters
from .home import (
    EnvFileReport,
    RcReport,
    plan_env_entries,
    rc_paths,
    resolve_home_base,
    update_rc_file,
    write_env_file,
)
from .httpcheck import check_provider
from .logging_setup import get_logger, setup_logging
from .providers import (
    SPEC_ENV_VAR,
    ProviderProfile,
    SpecificationError,
    load_provider_specification,
)
from .reporting import (
    env_status_label,
    rc_status_label,
    render_brief_summary,
    render_summary,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_MISSING_DEP = 2
EXIT_VALIDATION = 3
EXIT_CONFIG_WRITE = 4

#: All debugging output goes through Python's ``logging`` (see
#: ``logging_setup.py``); ``--debug`` switches this logger to DEBUG level.
logger = get_logger(__name__)


# ----------------------------------------------------------------------
# Startup banner
# ----------------------------------------------------------------------


#: Printed once at the start of every run: ASCII art plus a short
#: description of what the tool does and how the key prompts behave
#: (an empty answer skips that provider). Pure presentation.
_BANNER = r"""
                                  |>>>
                                  |
                              _  _|_  _
                             |;|_|;|_|;|
                             \\.    .  /
                              \\:  .  /
                               ||:   |
                               ||:.  |
                               ||:  .|
                               ||: , |
                               ||:   |
                               ||: . |
                               ||: , |
                  __           ||:   |
                 |__|          ||: . |
    ~ ~~ ~ ~~ ~ ~~ ~ ~~ ~ ~~   ||: , |  ~ ~~ ~ ~~ ~ ~~ ~ ~~
                  m o a t - i n s t a l l

   This tool configures agentic coding harnesses (pi, claude,
   codex, aider, goose, opencode, cline, oh-my-pi) so that they
   work with LLM providers.

   It has been designed to work with moat, which containerizes
   the agents. If you want to configure agents in your own home
   directory, run this tool with "--init-home-dir".

   API keys are read from the environment when already set;
   otherwise each provider's key is requested once. Leave a key
   prompt empty to skip that provider. You can re-run the tool at
   any time to re-configure the harnesses.
"""


def _print_banner() -> None:
    """Print the startup banner (ASCII art + tool description)."""
    print(_BANNER)


# ----------------------------------------------------------------------
# Startup import guard (safety net for source checkouts)
# ----------------------------------------------------------------------


def _check_dependencies() -> Optional[str]:
    missing = []
    for module, pip_name in (("tomlkit", "tomlkit"), ("ruamel.yaml", "ruamel.yaml")):
        try:
            __import__(module)
        except ImportError:
            missing.append(pip_name)
    if missing:
        names = " ".join(missing)
        return (
            f"moat-install requires the {names} package(s) but they are not "
            "importable.\n"
            "Remedy: if installed via pipx run  pipx reinstall moat-install\n"
            f"        otherwise run       pip install {names}"
        )
    return None


# ----------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moat-install",
        description=(
            "Configure agentic coding harnesses to use the providers defined in "
            "the provider specification file, writing each harness's global "
            "user configuration under a home-base directory."
        ),
    )
    parser.add_argument(
        "--provider-specification",
        default=None,
        metavar="PATH",
        help=(
            "path to the JSON provider specification file (wins over the "
            f"{SPEC_ENV_VAR} environment variable)"
        ),
    )
    parser.add_argument(
        "--provider",
        default=None,
        metavar="NAME[,NAME…]",
        help=(
            "comma-separated subset of the providers defined in the provider "
            "specification (default: all, in specification order; the first "
            "provider listed becomes each harness's default)"
        ),
    )
    parser.add_argument(
        "--harness",
        default=None,
        metavar="NAME[,NAME…]",
        help=(
            "comma-separated subset of "
            f"{','.join(adapter_names())} (default: all implemented harnesses)"
        ),
    )
    parser.add_argument(
        "--init-home-dir",
        action="store_true",
        help="use the real $HOME as home base (configure the host home directly)",
    )
    parser.add_argument(
        "--home-base",
        default=None,
        metavar="PATH",
        help=(
            "explicit home base directory (wins over $MOAT_INSTALL_HOME_BASE and --init-home-dir)"
        ),
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the /models endpoint validation",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="perform everything except writes; print the full plan",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "print the full summary table (per-provider statuses, files, key "
            "mode, backups, parse check), warnings, and manual-action notes"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="verbose trace of every decision (all secrets masked)",
    )
    return parser


def _parse_csv(
    raw: Optional[str],
    valid: Sequence[str],
    option: str,
) -> List[str]:
    """Parse a comma-separated list against a registry; raise UsageError."""
    if raw is None:
        return list(valid)
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        raise UsageError(f"{option}: no names given")
    invalid = [name for name in names if name not in valid]
    if invalid:
        raise UsageError(
            f"{option}: unknown name(s) {', '.join(invalid)}; valid names: {', '.join(valid)}"
        )
    if len(set(names)) != len(names):
        raise UsageError(f"{option}: duplicate names given")
    return names


class UsageError(Exception):
    """Raised for argument/usage errors (exit code 1)."""


# ----------------------------------------------------------------------
# Key collection
# ----------------------------------------------------------------------


def collect_keys(
    profiles: Sequence[ProviderProfile],
    stdin_is_tty: bool,
    prompt_fn=None,
) -> Tuple[List[ProviderSelection], List[ProviderProfile]]:
    """Gather each selected provider's key: env var first, then hidden prompt.

    Keys are never accepted as CLI arguments. In non-interactive mode (stdin
    not a TTY) a missing env var is a hard error for that provider. An empty
    answer at the interactive prompt **skips** that provider: it is returned
    in ``skipped`` and never configured, so the caller can tell the user to
    re-run the tool once the key has been retrieved.
    """
    prompt_fn = prompt_fn if prompt_fn is not None else getpass.getpass
    selections: List[ProviderSelection] = []
    skipped: List[ProviderProfile] = []
    for profile in profiles:
        key = os.environ.get(profile.api_key_env, "").strip()
        if key:
            logger.debug("provider '%s': key taken from $%s", profile.name, profile.api_key_env)
            selections.append(ProviderSelection(profile=profile, key=key))
            continue
        if not stdin_is_tty:
            raise UsageError(
                f"missing API key for provider '{profile.name}': set "
                f"{profile.api_key_env} or run interactively to be prompted"
            )
        logger.debug("prompting for provider '%s' key (hidden input)", profile.name)
        key = prompt_fn(
            f"API key for {profile.name} ({profile.api_key_env}) [leave empty to skip]: "
        ).strip()
        if not key:
            logger.debug("provider '%s' skipped: empty answer at key prompt", profile.name)
            skipped.append(profile)
            continue
        logger.debug("provider '%s': key received from interactive prompt", profile.name)
        selections.append(ProviderSelection(profile=profile, key=key))
    return selections, skipped


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------


def collect_env_aliases(harness_names: List[str], default_profile: ProviderProfile) -> dict:
    """Aggregate env aliases declared by the selected adapters.

    Aliases are derived env-file exports (e.g. ``OPENAI_API_KEY`` →
    ``$GATEWAY_API_KEY``) that harnesses resolve from the process
    environment; the key value itself is still persisted exactly once.
    """
    aliases: dict = {}
    for name in harness_names:
        for var, template in ADAPTERS[name].env_aliases(default_profile).items():
            aliases.setdefault(var, template)
    return aliases


def resolve_specification_path(specification_flag: Optional[str]) -> Optional[Path]:
    """Resolve the provider specification file path.

    Precedence: ``--provider-specification`` → ``$MOAT_INSTALL_PROVIDERS``.
    Returns ``None`` when neither is set (the caller turns that into a usage
    error — providers are no longer hard-coded in the code base).
    """
    if specification_flag:
        path = Path(specification_flag).expanduser()
        logger.debug("provider specification: %s (from --provider-specification)", path)
        return path
    env_value = os.environ.get(SPEC_ENV_VAR, "").strip()
    if env_value:
        path = Path(env_value).expanduser()
        logger.debug("provider specification: %s (from $%s)", path, SPEC_ENV_VAR)
        return path
    return None


def run(argv: Optional[Sequence[str]] = None) -> int:
    """Run moat-install; returns the process exit code."""
    _print_banner()

    dep_problem = _check_dependencies()
    if dep_problem:
        print(dep_problem, file=sys.stderr)
        return EXIT_MISSING_DEP

    parser = build_parser()
    args = parser.parse_args(argv)
    # Logging standard: configure the package logger once, right after the
    # flags are known (--debug ⇒ DEBUG level, otherwise WARNING).
    setup_logging(args.debug)

    # Load the provider specification first: everything downstream (provider
    # name validation, key collection, validation, adapters) derives from it.
    spec_path = resolve_specification_path(args.provider_specification)
    if spec_path is None:
        print(
            "error: no provider specification given; pass --provider-specification "
            f"PATH or set ${SPEC_ENV_VAR} (see the examples/ folder for the format)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        registry = load_provider_specification(spec_path)
    except SpecificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        provider_names = _parse_csv(args.provider, registry.names(), "--provider")
        harness_names = _parse_csv(args.harness, adapter_names(), "--harness")
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    logger.debug(
        "selection: providers=[%s] harnesses=[%s] dry-run=%s",
        ", ".join(provider_names),
        ", ".join(harness_names),
        args.dry_run,
    )

    # Resolve the home base before asking for keys and announce it: the user
    # should know where the configuration will be written before entering any
    # secrets. (Never prompts in non-interactive runs; a prompt callable is
    # only attached when stdin is a TTY.)
    prompt: Optional[Callable[[], str]] = None
    if sys.stdin.isatty():

        def _prompt() -> str:
            return input("Home base directory path: ")

        prompt = _prompt

    try:
        home_base = resolve_home_base(args.home_base, args.init_home_dir, prompt=prompt)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"Home base: {home_base}")

    try:
        selections, skipped_profiles = collect_keys(
            [registry.get(name) for name in provider_names],
            stdin_is_tty=sys.stdin.isatty(),
        )
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # An empty answer at the key prompt skips that provider entirely: no
    # adapter entry, no validation, no env-file entry. The user is told they
    # can re-run the tool once the key has been retrieved.
    for profile in skipped_profiles:
        logger.debug("provider '%s' skipped: no API key entered", profile.name)
        print(
            f"note: no API key entered for provider '{profile.name}' "
            f"({profile.api_key_env}); endpoint not configured — re-run "
            "moat-install after retrieving the key to configure it"
        )
    if not selections:
        print(
            "No API keys were entered, so no endpoints were configured. "
            "Re-run moat-install after retrieving the key(s) to configure them."
        )
        return EXIT_OK

    if not args.dry_run:
        home_base.mkdir(parents=True, exist_ok=True)

    # Print the resolution plan. The home base is already announced before
    # the key prompts (see above); the env-file and rc-file plan sections are
    # verbose-level detail: the default output already reports the env/rc
    # outcomes after the run.
    sel_pairs = [(s.profile, s.key) for s in selections]
    aliases = collect_env_aliases(harness_names, selections[0].profile)
    if args.verbose:
        env_plan: EnvFileReport = plan_env_entries(home_base, sel_pairs, aliases)
        print("Env file entries (values fully masked):")
        for var, masked in env_plan.entries:
            print(f"  export {var}={masked}")
        print("RC files:")
        for rc in rc_paths(home_base):
            action = "create" if not args.dry_run else "would create"
            if rc.exists():
                guarded = ""
                with contextlib.suppress(OSError):
                    guarded = rc.read_text(encoding="utf-8")
                if "# >>> moat-install >>>" in guarded:
                    action = "source block already present"
                else:
                    action = (
                        "append source block" if not args.dry_run else "would append source block"
                    )
            print(f"  {rc}: {action}")

    # Validate each selected provider once (host-side; containers inherit
    # host networking).
    if not args.no_verify:
        all_ok = True
        for sel in selections:
            logger.debug(
                "validating provider '%s' via GET %s/models",
                sel.profile.name,
                sel.profile.base_url,
            )
            ok, detail = check_provider(sel.profile, sel.key)
            print(f"validate: {detail}")
            if not ok:
                all_ok = False
        if not all_ok:
            print(
                "error: provider validation failed; refusing to write configs "
                "(use --no-verify to proceed anyway)",
                file=sys.stderr,
            )
            return EXIT_VALIDATION
    else:
        print("skipping endpoint validation (--no-verify)")

    # Configure harnesses via the registry (zero harness-specific branches).
    adapters = build_adapters(harness_names, home_base, selections, dry_run=args.dry_run)
    reports = []
    for adapter in adapters:
        logger.debug("running adapter: %s", adapter.name)
        reports.append(adapter.run())

    # Managed env file + rc integration (real writes only when not dry-run).
    env_report = write_env_file(home_base, sel_pairs, aliases=aliases, dry_run=args.dry_run)
    rc_reports: List[RcReport] = []
    for rc in rc_paths(home_base):
        rc_reports.append(update_rc_file(rc, dry_run=args.dry_run))

    # Summaries cover the providers actually configured: a key-skipped
    # provider has no statuses and must not shift the default marker.
    configured_names = [sel.profile.name for sel in selections]
    if args.verbose:
        print(render_summary(reports, configured_names, args.dry_run))
    else:
        print(render_brief_summary(reports, configured_names, args.dry_run))
        # Suppress warnings/manual notes by default; say how much was hidden.
        n_warnings = sum(len(r.warnings) for r in reports)
        n_notes = sum(len(r.manual_notes) for r in reports)
        counts = []
        if n_warnings:
            counts.append(f"{n_warnings} warning(s)")
        if n_notes:
            counts.append(f"{n_notes} manual-action note(s)")
        if counts:
            print(f"{' and '.join(counts)} hidden; rerun with --verbose to show them")
    if not args.dry_run:
        var_names = ", ".join(var for var, _masked in env_report.entries)
        print(
            f"env file: {env_status_label(env_report.status)}: {env_report.path} (sets {var_names})"
        )
        print("rc files in the home base:")
        for rc in rc_reports:
            print(f"  {rc.path}: {rc_status_label(rc.status)}")

    if any(not r.ok for r in reports):
        return EXIT_CONFIG_WRITE
    return EXIT_OK


def main() -> None:
    """Console-script entry point."""
    sys.exit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
