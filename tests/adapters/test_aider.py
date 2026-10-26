"""aider adapter: .aider.conf.yml merge, openai/ prefix, env aliases."""

from __future__ import annotations

from pathlib import Path

from moat_install.adapters.aider import AiderAdapter
from moat_install.providers import ProviderRegistry
from tests.conftest import KEY_AZURE, KEY_GATEWAY, assert_no_secret_outside_env_file


def test_fresh_home(home_base: Path, selections, registry) -> None:
    report = AiderAdapter(home_base, selections).run()
    assert report.ok and report.status == "created"
    text = (home_base / ".aider.conf.yml").read_text("utf-8")
    assert "model: openai/example-org/model-a" in text
    gateway = registry.get("llm-gateway")
    assert f"openai-api-base: {gateway.base_url}" in text
    assert report.parse_check == "ok"
    assert_no_secret_outside_env_file(home_base, KEY_GATEWAY, KEY_AZURE)


def test_merge_preserves_comments_and_settings(home_base: Path, selections, registry) -> None:
    target = home_base / ".aider.conf.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    fixture = (Path(__file__).parent.parent / "fixtures/aider_conf_existing.yml").read_text("utf-8")
    target.write_text(fixture, encoding="utf-8")

    report = AiderAdapter(home_base, selections).run()
    assert report.ok
    text = target.read_text("utf-8")
    assert "# My aider preferences" in text  # comment preserved
    assert "# Editing behaviour" in text
    assert "auto-commits: false" in text  # unrelated setting preserved
    assert "dark-mode: true" in text
    assert "model: openai/example-org/model-a" in text  # updated in place
    assert f"openai-api-base: {registry.get('llm-gateway').base_url}" in text
    assert "gpt-4o" not in text  # old value replaced
    assert report.backups


def test_env_aliases_reference_managed_var(registry: ProviderRegistry) -> None:
    aliases = AiderAdapter.env_aliases(registry.get("llm-gateway"))
    assert aliases == {"OPENAI_API_KEY": "$LLM_GATEWAY_KEY"}


def test_azure_default_warns_about_header(home_base: Path, registry: ProviderRegistry) -> None:
    from moat_install.adapters._base import ProviderSelection

    report = AiderAdapter(home_base, [ProviderSelection(registry.get("azure"), KEY_AZURE)]).run()
    assert report.ok
    text = (home_base / ".aider.conf.yml").read_text("utf-8")
    assert "model: openai/gpt-5-2025-08-07" in text
    assert any("Ocp-Apim-Subscription-Key" in w for w in report.warnings)


def test_second_provider_gets_manual_note(home_base: Path, selections) -> None:
    report = AiderAdapter(home_base, selections).run()
    assert any("azure" in n for n in report.manual_notes)


def test_idempotent_second_run(home_base: Path, selections) -> None:
    AiderAdapter(home_base, selections).run()
    snapshot = (home_base / ".aider.conf.yml").read_bytes()
    second = AiderAdapter(home_base, selections).run()
    assert second.status == "no-change"
    assert not second.backups
    assert (home_base / ".aider.conf.yml").read_bytes() == snapshot


def test_dry_run(home_base: Path, selections) -> None:
    AiderAdapter(home_base, selections, dry_run=True).run()
    assert not (home_base / ".aider.conf.yml").exists()
