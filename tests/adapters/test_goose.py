"""goose adapter: config.yaml provider entries, env aliases, YAML merge."""

from __future__ import annotations

from pathlib import Path

from moat_install.adapters._base import ProviderSelection
from moat_install.adapters.goose import GooseAdapter
from moat_install.providers import ProviderRegistry
from tests.conftest import KEY_AZURE, KEY_GATEWAY, assert_no_secret_outside_env_file


def test_fresh_home(home_base: Path, selections, registry) -> None:
    report = GooseAdapter(home_base, selections).run()
    assert report.ok and report.status == "created"
    text = (home_base / ".config/goose/config.yaml").read_text("utf-8")
    assert "active_provider: openai" in text
    assert "model: example-org/model-a" in text
    assert "configured: true" in text
    assert f"OPENAI_BASE_URL: {registry.get('llm-gateway').base_url}" in text
    assert report.parse_check == "ok"
    assert_no_secret_outside_env_file(home_base, KEY_GATEWAY, KEY_AZURE)


def test_merge_preserves_unrelated_settings(home_base: Path, selections) -> None:
    target = home_base / ".config/goose/config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    fixture = (Path(__file__).parent.parent / "fixtures/goose_config_existing.yaml").read_text(
        "utf-8"
    )
    target.write_text(fixture, encoding="utf-8")

    report = GooseAdapter(home_base, selections).run()
    assert report.ok
    text = target.read_text("utf-8")
    assert "# goose config — my settings" in text  # comment preserved
    assert 'GOOSE_MODE: "approve"' in text  # unrelated setting preserved
    assert "anthropic" in text  # unrelated provider entry preserved
    assert "developer" in text  # extensions preserved
    assert "active_provider: openai" in text
    assert "model: example-org/model-a" in text
    assert report.backups


def test_no_api_key_in_config(home_base: Path, selections) -> None:
    """goose never reads keys from config.yaml — none may be written there."""
    GooseAdapter(home_base, selections).run()
    text = (home_base / ".config/goose/config.yaml").read_text("utf-8")
    assert KEY_GATEWAY not in text
    assert KEY_AZURE not in text


def test_env_aliases_gateway(registry: ProviderRegistry) -> None:
    aliases = GooseAdapter.env_aliases(registry.get("llm-gateway"))
    assert aliases == {"OPENAI_API_KEY": "$LLM_GATEWAY_KEY"}


def test_env_aliases_azure_adds_custom_header(registry: ProviderRegistry) -> None:
    aliases = GooseAdapter.env_aliases(registry.get("azure"))
    assert aliases["OPENAI_CUSTOM_HEADERS"] == "Ocp-Apim-Subscription-Key=$AZURE_APIM_KEY"
    # a reference, never the literal secret
    assert KEY_AZURE not in aliases["OPENAI_CUSTOM_HEADERS"]


def test_azure_default_config(home_base: Path, registry: ProviderRegistry) -> None:
    report = GooseAdapter(home_base, [ProviderSelection(registry.get("azure"), KEY_AZURE)]).run()
    assert report.ok
    text = (home_base / ".config/goose/config.yaml").read_text("utf-8")
    assert "model: gpt-5-2025-08-07" in text
    assert f"OPENAI_BASE_URL: {registry.get('azure').base_url}" in text
    assert any("OPENAI_CUSTOM_HEADERS" in n for n in report.manual_notes)


def test_idempotent_second_run(home_base: Path, selections) -> None:
    GooseAdapter(home_base, selections).run()
    snapshot = (home_base / ".config/goose/config.yaml").read_bytes()
    second = GooseAdapter(home_base, selections).run()
    assert second.status == "no-change"
    assert not second.backups
    assert (home_base / ".config/goose/config.yaml").read_bytes() == snapshot


def test_dry_run(home_base: Path, selections) -> None:
    GooseAdapter(home_base, selections, dry_run=True).run()
    assert not (home_base / ".config/goose/config.yaml").exists()
