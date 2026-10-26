"""Home-base resolution, the managed env file, and rc-file source lines.

The home base is the directory that the container runner (moat) later
bind-mounts as the user's home directory inside harness containers. All
harness global configs therefore live at their usual home-relative paths under
the base, and shell startup files written here are the runtime ``$HOME`` rc
files. Host-side absolute paths of the base must never be baked into harness
configs (§7 path rule) — only ``$HOME``-relative references are safe.
"""

from __future__ import annotations

import os
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .logging_setup import get_logger
from .providers import ProviderProfile

#: All debugging output goes through Python's ``logging`` (see
#: ``logging_setup.py``); this module traces home-base resolution and every
#: env-file / rc-file / backup write at DEBUG level.
logger = get_logger(__name__)

ENV_DIR = ".config/moat-install"
ENV_FILE_NAME = "env.sh"
RC_GUARD_BEGIN = "# >>> moat-install >>>"
RC_GUARD_END = "# <<< moat-install <<<"
RC_SOURCE_LINE = '. "$HOME/.config/moat-install/env.sh"'
RC_FILES: Tuple[str, ...] = (".bashrc", ".zshrc")

#: Environment variable consulted for the default home base (after the
#: ``--home-base`` / ``--init-home-dir`` flags). Its value is used directly
#: as the home base directory.
ENV_BASE_VAR = "MOAT_INSTALL_HOME_BASE"

#: Header line written at the top of the managed env file.
_ENV_FILE_HEADER = "# Managed by moat-install — API keys for institutional LLM providers."


def mask_key(key: Optional[str]) -> str:
    """Mask a secret for logs/debug/summaries: fully, as ``***``.

    No part of the value — not even the last characters — is ever shown.
    The ``key`` argument is accepted so call sites can pass values directly
    without preprocessing.
    """
    del key
    return "***"


def env_file_path(home_base: Path) -> Path:
    """Path of the managed env file under the home base."""
    return home_base / ENV_DIR / ENV_FILE_NAME


def resolve_env_aliases(
    aliases: Dict[str, str],
) -> Dict[str, str]:
    """Validate alias templates (must not contain double quotes)."""
    for var, template in aliases.items():
        if '"' in template or "\n" in template:
            raise ValueError(f"invalid env alias template for {var!r}")
    return dict(aliases)


def resolve_home_base(
    home_base_flag: Optional[str],
    init_home_dir: bool,
    prompt: Optional[Callable[[], str]] = None,
) -> Path:
    """Resolve the home base directory.

    Precedence: ``--home-base`` → ``--init-home-dir`` (real ``$HOME``) →
    ``$MOAT_INSTALL_HOME_BASE`` → interactive prompt for the path.

    Raises ``RuntimeError`` if nothing can be resolved (non-interactive stdin
    and ``$MOAT_INSTALL_HOME_BASE`` unset).
    """
    if home_base_flag:
        base = Path(home_base_flag).expanduser()
        logger.debug("home base: %s (from --home-base)", base)
        return base
    if init_home_dir:
        base = Path.home()
        logger.debug("home base: %s (from --init-home-dir)", base)
        return base
    env_base = os.environ.get(ENV_BASE_VAR, "").strip()
    if env_base:
        base = Path(env_base).expanduser()
        logger.debug("home base: %s (from $%s)", base, ENV_BASE_VAR)
        return base
    if prompt is not None:
        raw = prompt()
        if raw.strip():
            base = Path(raw.strip()).expanduser()
            logger.debug("home base: %s (from interactive prompt)", base)
            return base
    raise RuntimeError(
        f"could not resolve a home base: pass --home-base PATH, use --init-home-dir, "
        f"or set ${ENV_BASE_VAR}"
    )


@dataclass
class EnvFileReport:
    """Outcome of planning/writing the managed env file."""

    path: Path
    status: str = "no-change"  # created | updated | no-change | would-create | would-update
    backup: Optional[Path] = None
    entries: List[Tuple[str, str]] = field(default_factory=list)  # (VAR, masked value)


def _parse_env_file(text: str) -> Tuple[Dict[str, str], List[str]]:
    """Parse ``export VAR=value`` lines.

    Returns (values-by-var, list-of-lines-excluding-our-known-vars). Unknown
    lines (comments, other exports) are preserved verbatim and in order.
    """
    values: Dict[str, str] = {}
    other_lines: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            body = stripped[len("export ") :].strip()
            if "=" in body and not body.startswith("("):
                var, _, value = body.partition("=")
                var = var.strip()
                value = value.strip()
                try:
                    unquoted = shlex.split(value)
                    if len(unquoted) == 1:
                        value = unquoted[0]
                except ValueError:
                    pass
                values[var] = value
                other_lines.append(line)
                continue
        other_lines.append(line)
    return values, other_lines


def plan_env_entries(
    home_base: Path,
    selections: List[Tuple[ProviderProfile, str]],
    aliases: Optional[Dict[str, str]] = None,
) -> EnvFileReport:
    """Compute the env-file entries (masked) without writing anything."""
    path = env_file_path(home_base)
    entries = [(profile.api_key_env, mask_key(key)) for profile, key in selections]
    for var in sorted(aliases or {}):
        entries.append((var, "<derived>"))
    logger.debug("planned %d env-file entries for %s", len(entries), path)
    return EnvFileReport(path=path, entries=entries)


def write_env_file(
    home_base: Path,
    selections: List[Tuple[ProviderProfile, str]],
    aliases: Optional[Dict[str, str]] = None,
    dry_run: bool = False,
) -> EnvFileReport:
    """Create or idempotently update the managed env file.

    Only moat-managed variables are touched: existing ``export VAR=...`` lines
    for moat-managed variables are rewritten in place and new variables are
    appended; every other line (comments, unrelated exports) is preserved
    verbatim. ``aliases`` maps additional variable names to template strings
    that may reference managed variables (e.g. ``OPENAI_API_KEY`` →
    ``"$GATEWAY_API_KEY"``); they are emitted as double-quoted exports so
    they expand at source time and the key *value* is still persisted exactly
    once. The file holds plaintext API keys, so it is created with mode 0600.
    In dry-run mode nothing is written.
    """
    path = env_file_path(home_base)
    alias_map = resolve_env_aliases(aliases or {})
    new_values = {profile.api_key_env: key for profile, key in selections}

    if path.exists():
        existing_text = path.read_text(encoding="utf-8")
        _old_values, existing_lines = _parse_env_file(existing_text)
        # Drop our own header so re-runs never accumulate it.
        existing_lines = [
            line for line in existing_lines if line.strip() != _ENV_FILE_HEADER.strip()
        ]
    else:
        existing_text, _old_values, existing_lines = "", {}, []

    # Rebuild: keep every line except moat-managed exports, which are
    # rewritten in place (same position) or appended for new variables.
    managed: Dict[str, str] = dict(new_values)
    managed.update({var: f'"{template}"' for var, template in alias_map.items()})
    lines: List[str] = []
    emitted = set()
    for line in existing_lines:
        stripped = line.strip()
        if stripped.startswith("export "):
            body = stripped[len("export ") :].strip()
            var = body.split("=", 1)[0].strip() if "=" in body else ""
            if var in managed:
                if var not in emitted:
                    lines.append(f"export {var}={managed[var]}")
                    emitted.add(var)
                continue
        lines.append(line)
    for var, value in managed.items():
        if var not in emitted:
            lines.append(f"export {var}={value}")

    header = _ENV_FILE_HEADER
    content = "\n".join([header, *lines]) + "\n"

    status = "updated"
    if not path.exists():
        status = "created"
    elif content == existing_text:
        status = "no-change"

    report = EnvFileReport(
        path=path,
        status=status,
        entries=[(var, mask_key(key)) for var, key in new_values.items()]
        + [(var, "<derived>") for var in sorted(alias_map)],
    )
    if dry_run:
        report.status = (
            "would-create"
            if status == "created"
            else "would-update"
            if status == "updated"
            else "no-change"
        )
        logger.debug("env file (dry run): %s -> %s", path, report.status)
        return report

    if status == "no-change":
        logger.debug("env file unchanged: %s", path)
        return report

    if not path.parent.exists():
        logger.debug("env file: creating directory %s", path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        report.backup = backup_file(path)
    logger.debug(
        "env file: writing %s (status=%s, %d key entries, %d aliases)",
        path,
        status,
        len(new_values),
        len(alias_map),
    )
    path.write_text(content, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600: the file contains secrets
    logger.debug("env file: %s, mode set to 0600: %s", status, path)
    return report


def backup_file(path: Path) -> Path:
    """Create a timestamped ``<name>.bak-YYYYMMDD-HHMMSS`` next to ``path``."""
    import shutil
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    logger.debug("backup: %s -> %s", path, backup)
    return backup


def rc_block() -> str:
    """The idempotently-guarded rc block that sources the managed env file."""
    return f"{RC_GUARD_BEGIN}\n{RC_SOURCE_LINE}\n{RC_GUARD_END}"


@dataclass
class RcReport:
    """Outcome of planning/updating one rc file."""

    path: Path
    status: str = "no-change"  # created | appended | no-change | would-create | would-append


def rc_paths(home_base: Path) -> List[Path]:
    """Startup files inside the home base (runtime ``$HOME`` rc files)."""
    return [home_base / name for name in RC_FILES]


def update_rc_file(path: Path, dry_run: bool = False) -> RcReport:
    """Append the guarded source block to one rc file, idempotently.

    Missing files are created. Existing guarded blocks are left untouched.
    In dry-run mode nothing is written.
    """
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if RC_GUARD_BEGIN in text:
            logger.debug("rc file %s: source block already present (no-change)", path)
            return RcReport(path=path, status="no-change")
        status = "appended"
    else:
        text = ""
        status = "created"

    if dry_run:
        planned = "would-create" if status == "created" else "would-append"
        logger.debug("rc file (dry run): %s -> %s", path, planned)
        return RcReport(path=path, status=planned)

    if not path.parent.exists():
        logger.debug("rc file: creating directory %s", path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    block = rc_block()
    if text and not text.endswith("\n"):
        text += "\n"
    text += f"\n{block}\n"
    path.write_text(text, encoding="utf-8")
    logger.debug("rc file %s: %s (guarded source block)", path, status)
    return RcReport(path=path, status=status)
