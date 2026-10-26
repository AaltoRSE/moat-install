"""claude adapter: settings.json env block, env-ref vs literal key modes."""

from __future__ import annotations

import json
from pathlib import Path

from moat_install.adapters._base import ProviderSelection
from moat_install.adapters.claude import ClaudeAdapter
from moat_install.providers import ProviderRegistry
from tests.conftest import (
    KEY_AZURE,
    KEY_GATEWAY,
    assert_no_secret_outside_env_file,
)


def test_gateway_uses_env_ref(home_base: Path, selections, registry) -> None:
    report = ClaudeAdapter(home_base, selections).run()
    assert report.ok
    doc = json.loads((home_base / ".claude/settings.json").read_text("utf-8"))
    env = doc["env"]
    assert env["ANTHROPIC_BASE_URL"] == registry.get("llm-gateway").base_url
    assert "ANTHROPIC_AUTH_TOKEN" not in env  # delivered via env, never in file
    assert report.key_mode == "env-ref"
    assert_no_secret_outside_env_file(home_base, KEY_GATEWAY, KEY_AZURE)
    assert any("ANTHROPIC_AUTH_TOKEN" in n for n in report.manual_notes)


def test_azure_writes_literal_custom_header(home_base: Path, registry: ProviderRegistry) -> None:
    report = ClaudeAdapter(home_base, [ProviderSelection(registry.get("azure"), KEY_AZURE)]).run()
    assert report.ok
    doc = json.loads((home_base / ".claude/settings.json").read_text("utf-8"))
    env = doc["env"]
    assert env["ANTHROPIC_BASE_URL"] == registry.get("azure").base_url
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == (f"Ocp-Apim-Subscription-Key: {KEY_AZURE}")
    assert report.key_mode == "literal"
    assert any("plaintext" in w for w in report.warnings)
    # literal-key file is deliberately exempt from the no-secret rule, but must
    # be 0600.
    mode = (home_base / ".claude/settings.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_merge_preserves_settings_and_removes_stale_token(
    home_base: Path, registry: ProviderRegistry
) -> None:
    target = home_base / ".claude/settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    fixture = (Path(__file__).parent.parent / "fixtures/claude_settings_existing.json").read_text(
        "utf-8"
    )
    target.write_text(fixture, encoding="utf-8")

    report = ClaudeAdapter(home_base, selections_two_providers(registry)).run()
    assert report.ok and report.status == "updated"
    doc = json.loads(target.read_text("utf-8"))
    assert doc["theme"] == "dark"  # preserved
    assert doc["env"]["OTHER_VAR"] == "keep-me"  # preserved
    assert doc["env"]["ANTHROPIC_BASE_URL"] == registry.get("llm-gateway").base_url  # updated
    assert "ANTHROPIC_AUTH_TOKEN" not in doc["env"]  # stale token removed
    assert any("stale" in w for w in report.warnings)
    assert report.backups


def test_second_provider_produces_manual_note(home_base: Path, registry: ProviderRegistry) -> None:
    report = ClaudeAdapter(home_base, selections_two_providers(registry)).run()
    assert any("azure" in n for n in report.manual_notes)


def selections_two_providers(registry: ProviderRegistry):
    return [
        ProviderSelection(registry.get("llm-gateway"), KEY_GATEWAY),
        ProviderSelection(registry.get("azure"), KEY_AZURE),
    ]


def test_idempotent_second_run(home_base: Path, selections) -> None:
    ClaudeAdapter(home_base, selections).run()
    snapshot = (home_base / ".claude/settings.json").read_bytes()
    second = ClaudeAdapter(home_base, selections).run()
    assert second.status == "no-change"
    assert not second.backups
    assert (home_base / ".claude/settings.json").read_bytes() == snapshot
