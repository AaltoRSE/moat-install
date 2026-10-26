"""Home-base resolution precedence, env file, and rc-file idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from moat_install.home import (
    ENV_FILE_NAME,
    RC_GUARD_BEGIN,
    mask_key,
    plan_env_entries,
    resolve_home_base,
    update_rc_file,
    write_env_file,
)
from moat_install.providers import ProviderRegistry


def test_mask_key() -> None:
    assert mask_key("supersecret") == "***"
    assert mask_key("") == "***"
    assert mask_key(None) == "***"
    assert mask_key("ab") == "***"  # no last-characters leak, ever


def test_resolution_home_base_flag_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOAT_INSTALL_HOME_BASE", str(tmp_path))
    got = resolve_home_base(str(tmp_path / "explicit"), False)
    assert got == tmp_path / "explicit"


def test_resolution_init_home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MOAT_INSTALL_HOME_BASE", raising=False)
    got = resolve_home_base(None, True)
    assert got == tmp_path


def test_resolution_env_var_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "moat-home"
    monkeypatch.setenv("MOAT_INSTALL_HOME_BASE", str(base))
    got = resolve_home_base(None, False)
    assert got == base


def test_resolution_prompt_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOAT_INSTALL_HOME_BASE", raising=False)
    got = resolve_home_base(None, False, prompt=lambda: "/some/prompted/path")
    assert got == Path("/some/prompted/path")


def test_resolution_failure_without_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOAT_INSTALL_HOME_BASE", raising=False)
    with pytest.raises(RuntimeError):
        resolve_home_base(None, False, prompt=None)


def _selections(registry: ProviderRegistry):
    return [(registry.get("llm-gateway"), "key-gw-1234")]


def test_env_file_created_and_mode_0600(tmp_path: Path, registry: ProviderRegistry) -> None:
    report = write_env_file(tmp_path, _selections(registry))
    assert report.status == "created"
    text = report.path.read_text(encoding="utf-8")
    assert "export LLM_GATEWAY_KEY=key-gw-1234" in text
    mode = report.path.stat().st_mode & 0o777
    assert mode == 0o600


def test_env_aliases_are_references(tmp_path: Path, registry: ProviderRegistry) -> None:
    """Aliases expand at source time; the key value is persisted only once."""
    report = write_env_file(
        tmp_path,
        _selections(registry),
        aliases={"OPENAI_API_KEY": "$LLM_GATEWAY_KEY"},
    )
    text = report.path.read_text(encoding="utf-8")
    assert 'export OPENAI_API_KEY="$LLM_GATEWAY_KEY"' in text
    assert text.count("key-gw-1234") == 1  # value only in its own export
    # idempotent
    again = write_env_file(
        tmp_path,
        _selections(registry),
        aliases={"OPENAI_API_KEY": "$LLM_GATEWAY_KEY"},
    )
    assert again.status == "no-change"


def test_env_alias_rewritten_in_place(tmp_path: Path, registry: ProviderRegistry) -> None:
    write_env_file(tmp_path, _selections(registry), aliases={"OPENAI_API_KEY": "$LLM_GATEWAY_KEY"})
    report = write_env_file(
        tmp_path,
        _selections(registry),
        aliases={
            "OPENAI_API_KEY": "$LLM_GATEWAY_KEY",
            "OPENAI_CUSTOM_HEADERS": "Ocp-Apim-Subscription-Key=$AZURE_APIM_KEY",
        },
    )
    text = report.path.read_text(encoding="utf-8")
    assert 'export OPENAI_CUSTOM_HEADERS="Ocp-Apim-Subscription-Key=$AZURE_APIM_KEY"' in text


def test_env_alias_rejects_bad_template(tmp_path: Path, registry: ProviderRegistry) -> None:
    import pytest

    with pytest.raises(ValueError):
        write_env_file(tmp_path, _selections(registry), aliases={"BAD": 'has"quote'})


def test_env_file_idempotent(tmp_path: Path, registry: ProviderRegistry) -> None:
    first = write_env_file(tmp_path, _selections(registry))
    snapshot = first.path.read_bytes()
    second = write_env_file(tmp_path, _selections(registry))
    assert second.status == "no-change"
    assert second.backup is None
    assert second.path.read_bytes() == snapshot


def test_env_file_header_not_duplicated(tmp_path: Path, registry: ProviderRegistry) -> None:
    for _ in range(3):
        write_env_file(tmp_path, _selections(registry))
    text = (tmp_path / ".config/moat-install" / ENV_FILE_NAME).read_text("utf-8")
    assert text.count("# Managed by moat-install") == 1


def test_env_file_preserves_unknown_lines(tmp_path: Path, registry: ProviderRegistry) -> None:
    report = write_env_file(tmp_path, _selections(registry))
    report.path.write_text(
        "# my comment\nexport UNRELATED=yes\nexport LLM_GATEWAY_KEY=old\n",
        encoding="utf-8",
    )
    report2 = write_env_file(tmp_path, _selections(registry))
    assert report2.status == "updated"
    text = report2.path.read_text("utf-8")
    assert "# my comment" in text
    assert "export UNRELATED=yes" in text
    assert "export LLM_GATEWAY_KEY=key-gw-1234" in text
    assert "old" not in text


def test_env_file_update_creates_backup(tmp_path: Path, registry: ProviderRegistry) -> None:
    first = write_env_file(tmp_path, _selections(registry))
    second = write_env_file(tmp_path, [(registry.get("llm-gateway"), "rotated-key-9999")])
    assert second.status == "updated"
    assert second.backup is not None and second.backup.exists()
    backup_text = second.backup.read_text("utf-8")
    assert "key-gw-1234" in backup_text  # backup holds the pre-update content
    assert "rotated-key-9999" not in backup_text
    assert first.path.read_text("utf-8").count("rotated-key-9999") == 1


def test_env_file_dry_run_writes_nothing(tmp_path: Path, registry: ProviderRegistry) -> None:
    report = write_env_file(tmp_path, _selections(registry), dry_run=True)
    assert report.status == "would-create"
    assert not report.path.exists()


def test_env_entries_masked_in_plan(tmp_path: Path, registry: ProviderRegistry) -> None:
    plan = plan_env_entries(tmp_path, [(registry.get("llm-gateway"), "key-gw-1234")])
    assert plan.entries == [("LLM_GATEWAY_KEY", "***")]


def test_rc_block_appended_once(tmp_path: Path) -> None:
    rc = tmp_path / ".bashrc"
    first = update_rc_file(rc)
    assert first.status == "created"
    second = update_rc_file(rc)
    assert second.status == "no-change"
    text = rc.read_text("utf-8")
    assert text.count(RC_GUARD_BEGIN) == 1
    assert '. "$HOME/.config/moat-install/env.sh"' in text


def test_rc_block_preserves_existing_content(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text("export EDITOR=vi\n", encoding="utf-8")
    update_rc_file(rc)
    text = rc.read_text("utf-8")
    assert "export EDITOR=vi" in text
    assert RC_GUARD_BEGIN in text


def test_rc_dry_run(tmp_path: Path) -> None:
    rc = tmp_path / ".bashrc"
    report = update_rc_file(rc, dry_run=True)
    assert report.status == "would-create"
    assert not rc.exists()
