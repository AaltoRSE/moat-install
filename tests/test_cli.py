"""CLI: flags, exit codes, key handling, dry-run fs snapshot, e2e run."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import moat_install.cli as cli
from moat_install.cli import run
from tests.conftest import KEY_AZURE, KEY_GATEWAY, write_spec


@pytest.fixture(autouse=True)
def spec_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point $MOAT_INSTALL_PROVIDERS at a spec file for every test.

    Tests that exercise --provider-specification precedence monkeypatch this
    env var away (delenv) and pass the flag instead.
    """
    path = write_spec(tmp_path / "providers.json")
    monkeypatch.setenv("MOAT_INSTALL_PROVIDERS", str(path))
    return path


@pytest.fixture
def fs_snapshot(tmp_path: Path, home_base: Path):
    """Snapshot of all files under home_base for dry-run assertions."""
    files_before: dict = {}

    def take() -> dict:
        return {
            str(p.relative_to(home_base)): p.read_bytes()
            for p in sorted(home_base.rglob("*"))
            if p.is_file()
        }

    files_before.update(take())
    return files_before, take


# ----------------------------------------------------------------------
# Flag parsing / validation errors
# ----------------------------------------------------------------------


def test_unknown_provider_exits_1(keys_env) -> None:
    assert run(["--provider", "bogus", "--home-base", "/tmp/x", "--no-verify"]) == 1


def test_unknown_harness_exits_1(keys_env, home_base) -> None:
    assert run(["--harness", "bogus", "--home-base", str(home_base), "--no-verify"]) == 1


def test_missing_env_non_tty_exits_1(monkeypatch: pytest.MonkeyPatch, home_base) -> None:
    monkeypatch.delenv("LLM_GATEWAY_KEY", raising=False)
    monkeypatch.delenv("AZURE_APIM_KEY", raising=False)
    assert run(["--home-base", str(home_base), "--no-verify"]) == 1
    assert not (home_base / ".pi").exists()


def test_prompt_fallback(monkeypatch: pytest.MonkeyPatch, home_base) -> None:
    monkeypatch.delenv("LLM_GATEWAY_KEY", raising=False)
    monkeypatch.setenv("AZURE_APIM_KEY", KEY_AZURE)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": KEY_GATEWAY + "\n")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    rc = run(["--home-base", str(home_base), "--no-verify", "--provider", "llm-gateway"])
    assert rc == 0
    env_file = home_base / ".config/moat-install/env.sh"
    assert f"export LLM_GATEWAY_KEY={KEY_GATEWAY}" in env_file.read_text("utf-8")


def test_prompt_mentions_empty_skip(monkeypatch: pytest.MonkeyPatch, home_base) -> None:
    """The key prompt itself tells the user that an empty answer skips."""
    monkeypatch.delenv("LLM_GATEWAY_KEY", raising=False)
    monkeypatch.setenv("AZURE_APIM_KEY", KEY_AZURE)
    seen: dict = {}

    def fake_prompt(prompt: str = "") -> str:
        seen["prompt"] = prompt
        return KEY_GATEWAY

    monkeypatch.setattr(cli.getpass, "getpass", fake_prompt)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    rc = run(["--home-base", str(home_base), "--no-verify", "--provider", "llm-gateway"])
    assert rc == 0
    assert "leave empty to skip" in seen["prompt"]


def test_startup_banner_printed(keys_env, home_base, mock_validate, capsys) -> None:
    """Every run starts with the ASCII-art banner and tool description."""
    rc = run(["--home-base", str(home_base), "--harness", "pi"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "m o a t - i n s t a l l" in out
    assert "Leave a key" in out  # empty-key hint in the startup description
    assert "skip that provider" in out


def test_empty_prompt_skips_all_providers(
    monkeypatch: pytest.MonkeyPatch, home_base, capsys
) -> None:
    """An empty answer at every key prompt configures nothing; the user is
    told they can re-run the tool after retrieving the keys."""
    monkeypatch.delenv("LLM_GATEWAY_KEY", raising=False)
    monkeypatch.delenv("AZURE_APIM_KEY", raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "\n")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    rc = run(["--home-base", str(home_base), "--no-verify", "--harness", "pi"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no API key entered" in out
    assert "re-run" in out
    assert not (home_base / ".pi").exists()
    assert not (home_base / ".config/moat-install/env.sh").exists()


def test_empty_prompt_skips_only_that_provider(
    monkeypatch: pytest.MonkeyPatch, home_base, capsys
) -> None:
    """An empty prompt answer for one provider leaves it unconfigured while
    the remaining providers proceed; the summary lists only configured ones."""
    monkeypatch.setenv("LLM_GATEWAY_KEY", KEY_GATEWAY)
    monkeypatch.delenv("AZURE_APIM_KEY", raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "\n")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    rc = run(["--home-base", str(home_base), "--no-verify", "--harness", "pi,claude"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no API key entered for provider 'azure'" in out
    assert "re-run" in out
    assert "Providers: llm-gateway (default)" in out  # only configured providers
    doc = (home_base / ".pi/agent/models.json").read_text("utf-8")
    assert "llm-gateway" in doc
    assert "azure" not in doc
    env_text = (home_base / ".config/moat-install/env.sh").read_text("utf-8")
    assert f"export LLM_GATEWAY_KEY={KEY_GATEWAY}" in env_text
    assert "AZURE_APIM_KEY" not in env_text


def test_skipped_default_provider_promotes_next(
    monkeypatch: pytest.MonkeyPatch, home_base, capsys
) -> None:
    """When the first-listed provider is skipped, the next configured one
    becomes each harness's default provider (incl. derived env aliases)."""
    monkeypatch.delenv("LLM_GATEWAY_KEY", raising=False)
    monkeypatch.setenv("AZURE_APIM_KEY", KEY_AZURE)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "\n")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    rc = run(["--home-base", str(home_base), "--no-verify", "--harness", "pi,goose"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no API key entered for provider 'llm-gateway'" in out
    assert "Providers: azure (default)" in out
    doc = (home_base / ".pi/agent/models.json").read_text("utf-8")
    assert "azure" in doc
    assert "llm-gateway" not in doc
    env_text = (home_base / ".config/moat-install/env.sh").read_text("utf-8")
    assert 'export OPENAI_API_KEY="$AZURE_APIM_KEY"' in env_text
    assert "LLM_GATEWAY_KEY" not in env_text


def test_home_base_announced_before_key_prompt(
    monkeypatch: pytest.MonkeyPatch, home_base, capsys
) -> None:
    """The resolved home base is printed before the first API-key prompt."""
    monkeypatch.delenv("LLM_GATEWAY_KEY", raising=False)
    monkeypatch.setenv("AZURE_APIM_KEY", KEY_AZURE)
    captured_at_prompt: dict = {}

    def fake_prompt(prompt: str = "") -> str:
        # Snapshot stdout as it stands when the key prompt is about to show.
        captured_at_prompt["out"] = capsys.readouterr().out
        return KEY_GATEWAY

    monkeypatch.setattr(cli.getpass, "getpass", fake_prompt)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    rc = run(["--home-base", str(home_base), "--no-verify", "--provider", "llm-gateway"])
    assert rc == 0
    assert f"Home base: {home_base}" in captured_at_prompt["out"]


def test_home_base_printed_exactly_once(keys_env, home_base, mock_validate, capsys) -> None:
    """The home-base line appears once per run, not duplicated in the plan
    block or the summary."""
    rc = run(["--home-base", str(home_base), "--harness", "pi", "--dry-run"])
    assert rc == 0
    assert capsys.readouterr().out.count("Home base:") == 1

    rc = run(["--home-base", str(home_base), "--harness", "pi"])
    assert rc == 0
    assert capsys.readouterr().out.count("Home base:") == 1


def test_duplicate_names_error(keys_env) -> None:
    assert (
        run(
            [
                "--provider",
                "llm-gateway,llm-gateway",
                "--home-base",
                "/tmp/x",
                "--no-verify",
            ]
        )
        == 1
    )


# ----------------------------------------------------------------------
# Validation exit codes
# ----------------------------------------------------------------------


def test_validation_failure_exits_3(keys_env, home_base, mock_validate) -> None:
    calls, outcomes = mock_validate
    outcomes.append((False, "llm-gateway: /models returned HTTP 500"))
    rc = run(["--home-base", str(home_base), "--provider", "llm-gateway"])
    assert rc == 3
    assert len(calls) == 1  # once per provider, never per harness
    assert not (home_base / ".pi").exists()  # no configs written


def test_no_verify_proceeds_with_warning(keys_env, home_base, mock_validate, capsys) -> None:
    _calls, outcomes = mock_validate
    outcomes.append((False, "llm-gateway: /models returned HTTP 500"))
    rc = run(["--home-base", str(home_base), "--provider", "llm-gateway", "--no-verify"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipping endpoint validation" in out
    assert (home_base / ".pi/agent/models.json").exists()


def test_validation_called_once_per_provider(keys_env, home_base, mock_validate) -> None:
    rc = run(["--home-base", str(home_base), "--harness", "pi,opencode"])
    assert rc == 0
    calls, _ = mock_validate
    assert sorted(name for name, _ in calls) == ["azure", "llm-gateway"]


# ----------------------------------------------------------------------
# Dry run: zero disk writes
# ----------------------------------------------------------------------


def test_dry_run_writes_nothing(keys_env, home_base, mock_validate, fs_snapshot) -> None:
    before, take = fs_snapshot
    rc = run(["--home-base", str(home_base), "--dry-run"])
    assert rc == 0
    assert take() == before


def test_dry_run_prints_plan(keys_env, home_base, mock_validate, capsys) -> None:
    """With --verbose, the dry-run plan enumerates the masked env entries and
    the rc-file plan."""
    rc = run(["--home-base", str(home_base), "--dry-run", "--verbose"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"Home base: {home_base}" in out
    assert "export LLM_GATEWAY_KEY=***\n" in out  # fully masked
    assert "export AZURE_APIM_KEY=***\n" in out  # fully masked
    assert KEY_GATEWAY not in out
    assert KEY_AZURE not in out
    assert ".bashrc" in out
    assert "dry run" in out


def test_env_rc_plan_sections_are_verbose_only(keys_env, home_base, mock_validate, capsys) -> None:
    """The env-file and rc-file plan sections print only with --verbose."""
    rc = run(["--home-base", str(home_base), "--dry-run", "--harness", "pi"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Env file entries" not in out
    assert "RC files" not in out
    assert "export LLM_GATEWAY_KEY" not in out
    assert f"Home base: {home_base}" in out  # the dry-run home-base line stays

    rc = run(["--home-base", str(home_base), "--harness", "pi"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Env file entries" not in out
    assert "RC files" not in out
    assert "export LLM_GATEWAY_KEY" not in out


# ----------------------------------------------------------------------
# End-to-end: full run, then idempotent re-run
# ----------------------------------------------------------------------


ALL_HARNESSES = ["--harness", "codex,claude,opencode,pi,oh-my-pi,aider,goose,cline"]


def test_full_run_creates_everything(keys_env, home_base, mock_validate, capsys) -> None:
    rc = run(["--home-base", str(home_base), *ALL_HARNESSES])
    assert rc == 0
    out = capsys.readouterr().out

    # every harness file exists
    expected = [
        home_base / ".codex/config.toml",
        home_base / ".codex/moat-models.json",
        home_base / ".claude/settings.json",
        home_base / ".config/opencode/opencode.json",
        home_base / ".pi/agent/models.json",
        home_base / ".omp/agent/models.json",
        home_base / ".aider.conf.yml",
        home_base / ".config/goose/config.yaml",
        home_base / ".cline/data/settings/providers.json",
        home_base / ".config/moat-install/env.sh",
        home_base / ".bashrc",
        home_base / ".zshrc",
    ]
    for path in expected:
        assert path.exists(), f"missing {path}"

    # env file has exactly both keys plus derived aliases
    env_text = (home_base / ".config/moat-install/env.sh").read_text("utf-8")
    assert f"export LLM_GATEWAY_KEY={KEY_GATEWAY}" in env_text
    assert f"export AZURE_APIM_KEY={KEY_AZURE}" in env_text
    assert 'export OPENAI_API_KEY="$LLM_GATEWAY_KEY"' in env_text
    assert env_text.count(KEY_GATEWAY) == 1  # value persisted exactly once

    # rc files source the env file exactly once, guarded
    for rc_name in (".bashrc", ".zshrc"):
        text = (home_base / rc_name).read_text("utf-8")
        assert text.count("# >>> moat-install >>>") == 1
        assert '. "$HOME/.config/moat-install/env.sh"' in text

    # summary mentions every harness; outside the startup banner (which
    # deliberately mentions the moat container runner) there is no
    # containerization talk
    for harness in ("codex", "claude", "opencode", "pi", "oh-my-pi", "aider", "goose", "cline"):
        assert harness in out
    assert "container" not in out.replace(cli._BANNER, "").lower()

    # secrets never appear outside the managed env file and cline's azure header
    from tests.conftest import assert_no_secret_outside_env_file

    assert_no_secret_outside_env_file(
        home_base,
        KEY_GATEWAY,
        KEY_AZURE,
        exempt=(
            (".cline/data/settings/providers.json", KEY_AZURE),
            (".claude/settings.json", KEY_AZURE),
        ),
    )


def test_rerun_is_in_place_no_new_backups(keys_env, home_base, mock_validate, capsys) -> None:
    run(["--home-base", str(home_base), *ALL_HARNESSES])
    first_backup_count = len(list(home_base.rglob("*.bak-*")))
    rc = run(["--home-base", str(home_base), *ALL_HARNESSES])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no-change" in out
    assert len(list(home_base.rglob("*.bak-*"))) == first_backup_count == 0


# ----------------------------------------------------------------------
# Output verbosity: brief by default, full details with --verbose
# ----------------------------------------------------------------------


def test_default_output_is_brief(keys_env, home_base, mock_validate, capsys) -> None:
    """Default output names what was configured; details are --verbose-only."""
    rc = run(["--home-base", str(home_base), *ALL_HARNESSES])
    assert rc == 0
    out = capsys.readouterr().out
    # names every harness and both providers
    for harness in ("codex", "claude", "opencode", "pi", "oh-my-pi", "aider", "goose", "cline"):
        assert harness in out
    assert "llm-gateway (default)" in out
    assert "azure" in out
    # no full summary table, no warnings, no manual actions
    assert "key mode" not in out
    assert "Warnings:" not in out
    assert "Manual actions:" not in out
    # suppressed counts are hinted
    assert "hidden" in out
    assert "--verbose" in out
    # env/rc lines: name the vars set and the rc files created in the home base
    assert "rc files in the home base:" in out
    assert f"{home_base}/.bashrc: created" in out
    assert f"{home_base}/.zshrc: created" in out
    env_line = next(line for line in out.splitlines() if line.startswith("env file:"))
    assert "created" in env_line
    assert "sets LLM_GATEWAY_KEY, AZURE_APIM_KEY, OPENAI_API_KEY" in env_line


def test_verbose_output_shows_full_details(keys_env, home_base, mock_validate, capsys) -> None:
    """--verbose prints the full table plus warnings and manual actions."""
    rc = run(["--home-base", str(home_base), *ALL_HARNESSES, "--verbose"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "key mode" in out  # full table header
    assert "Warnings:" in out
    assert "Manual actions:" in out
    assert "hidden" not in out


def test_rc_modified_label_when_preexisting(keys_env, home_base, mock_validate, capsys) -> None:
    """A pre-existing unguarded .bashrc is reported as modified, not created."""
    (home_base / ".bashrc").write_text("# my rc\n", encoding="utf-8")
    rc = run(["--home-base", str(home_base), "--harness", "pi"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"{home_base}/.bashrc: modified (source block appended)" in out
    assert f"{home_base}/.zshrc: created" in out


def test_dry_run_brief_output_has_would_prefix(keys_env, home_base, mock_validate, capsys) -> None:
    rc = run(["--home-base", str(home_base), "--harness", "pi", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Harnesses that would be configured:" in out
    assert "WOULD no-change" in out  # dry-run writes nothing, so no-change
    assert "Home base:" in out  # plan block still printed in dry-run


def test_partial_run_then_extend(keys_env, home_base, mock_validate) -> None:
    run(["--home-base", str(home_base), "--harness", "pi"])
    rc = run(["--home-base", str(home_base), "--harness", "codex"])
    assert rc == 0
    assert (home_base / ".pi/agent/models.json").exists()
    assert (home_base / ".codex/config.toml").exists()


def test_debug_masks_secrets(keys_env, home_base, mock_validate, capsys) -> None:
    rc = run(["--home-base", str(home_base), "--no-verify", "--debug"])
    err = capsys.readouterr().err
    assert rc == 0
    assert KEY_GATEWAY not in err
    assert KEY_AZURE not in err


def test_env_var_default_resolution(
    keys_env,
    mock_validate,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    base = tmp_path / "moat-home"
    monkeypatch.setenv("MOAT_INSTALL_HOME_BASE", str(base))
    rc = run(["--harness", "pi", "--no-verify"])
    assert rc == 0
    assert (base / ".pi/agent/models.json").exists()


def test_json_config_reparse_after_write(keys_env, home_base, mock_validate) -> None:
    """Written files must parse (post-write check guarantees validity)."""
    run(["--home-base", str(home_base), *ALL_HARNESSES])
    for rel in (
        ".pi/agent/models.json",
        ".omp/agent/models.json",
        ".config/opencode/opencode.json",
        ".claude/settings.json",
        ".codex/moat-models.json",
    ):
        json.loads((home_base / rel).read_text("utf-8"))


def test_unwritable_config_exits_4(keys_env, home_base, mock_validate, capsys) -> None:
    """A config file that cannot be parsed must produce exit code 4, not a crash."""
    target = home_base / ".pi/agent/models.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not valid json", encoding="utf-8")
    rc = run(["--home-base", str(home_base), "--harness", "pi"])
    assert rc == 4
    out = capsys.readouterr().out
    assert "FAILED" in out


def test_missing_dependency_exits_2(
    keys_env, home_base, mock_validate, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "tomlkit", None)
    rc = run(["--home-base", str(home_base), "--harness", "pi", "--no-verify"])
    assert rc == 2


# ----------------------------------------------------------------------
# Provider specification resolution
# ----------------------------------------------------------------------


def test_missing_specification_exits_1(
    keys_env, home_base, mock_validate, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MOAT_INSTALL_PROVIDERS", raising=False)
    rc = run(["--home-base", str(home_base), "--no-verify"])
    assert rc == 1
    assert not (home_base / ".pi").exists()


def test_spec_flag_wins_over_env(
    keys_env, home_base, mock_validate, spec_env: Path, tmp_path: Path, capsys
) -> None:
    other = write_spec(tmp_path / "other.json")
    rc = run(
        [
            "--provider-specification",
            str(other),
            "--home-base",
            str(home_base),
            "--no-verify",
            "--harness",
            "pi",
        ]
    )
    assert rc == 0
    assert (home_base / ".pi/agent/models.json").exists()


def test_spec_env_var_used_without_flag(keys_env, home_base, mock_validate, spec_env) -> None:
    rc = run(["--home-base", str(home_base), "--no-verify", "--harness", "pi"])
    assert rc == 0


def test_spec_file_missing_exits_1(
    keys_env, home_base, mock_validate, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("MOAT_INSTALL_PROVIDERS", "/nonexistent/spec.json")
    rc = run(["--home-base", str(home_base), "--no-verify"])
    assert rc == 1
    assert "cannot read" in capsys.readouterr().err


def test_spec_file_invalid_json_exits_1(
    keys_env, home_base, mock_validate, tmp_path: Path, capsys
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    rc = run(["--provider-specification", str(bad), "--home-base", str(home_base), "--no-verify"])
    assert rc == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_provider_selection_validated_against_spec(keys_env, home_base, mock_validate) -> None:
    rc = run(["--home-base", str(home_base), "--provider", "not-in-spec", "--no-verify"])
    assert rc == 1


def test_spec_order_defines_default(
    keys_env, home_base, mock_validate, tmp_path: Path, capsys
) -> None:
    """A spec with azure first makes azure the default provider."""
    import copy

    from tests.conftest import TEST_SPEC

    spec = copy.deepcopy(TEST_SPEC)
    spec["providers"] = [spec["providers"][1], spec["providers"][0]]
    path = write_spec(tmp_path / "azure-first.json", spec)
    rc = run(
        [
            "--provider-specification",
            str(path),
            "--home-base",
            str(home_base),
            "--no-verify",
            "--harness",
            "pi",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "azure" in out
    doc = (home_base / ".pi/agent/models.json").read_text("utf-8")
    assert doc.index('"azure"') < doc.index('"llm-gateway"')
