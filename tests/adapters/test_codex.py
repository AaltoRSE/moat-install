"""codex adapter: TOML round-trip, catalog derivation, conservative defaults."""

from __future__ import annotations

import json
from pathlib import Path

import tomlkit

from moat_install.adapters._base import ProviderSelection
from moat_install.adapters.codex import CodexAdapter
from moat_install.providers import ProviderRegistry
from tests.conftest import (
    KEY_AZURE,
    KEY_GATEWAY,
    assert_no_secret_outside_env_file,
)


def test_fresh_home(home_base: Path, selections, registry) -> None:
    report = CodexAdapter(home_base, selections).run()
    assert report.ok and report.status == "created"
    config_path = home_base / ".codex/config.toml"
    doc = tomlkit.loads(config_path.read_text("utf-8"))
    gateway = registry.get("llm-gateway")

    # default = first selection
    assert doc["model"] == gateway.default_model
    assert doc["model_provider"] == "llm-gateway"
    # conservative default on create
    assert doc["approval_policy"] == "on-request"
    # catalog referenced by bare filename (path rule: no absolute host paths)
    assert doc["model_catalog_json"] == "moat-models.json"
    assert config_path.parent.joinpath("moat-models.json").exists()

    gw = doc["model_providers"]["llm-gateway"]
    assert gw["base_url"] == gateway.base_url
    assert gw["wire_api"] == "responses"
    assert gw["env_key"] == "LLM_GATEWAY_KEY"
    assert "env_http_headers" not in gw

    az = doc["model_providers"]["azure"]
    assert az["env_http_headers"]["Ocp-Apim-Subscription-Key"] == "AZURE_APIM_KEY"
    assert "env_key" not in az
    assert report.parse_check == "ok"
    assert_no_secret_outside_env_file(home_base, KEY_GATEWAY, KEY_AZURE)


def test_catalog_is_derived_from_profiles(home_base: Path, selections, registry) -> None:
    CodexAdapter(home_base, selections).run()
    catalog = json.loads((home_base / ".codex/moat-models.json").read_text("utf-8"))
    by_slug = {m["slug"]: m for m in catalog["models"]}
    assert set(by_slug) == {m.id for m in registry.get("llm-gateway").models} | {
        m.id for m in registry.get("azure").models
    }
    model_a = by_slug["example-org/model-a"]
    assert model_a["provider"] == "llm-gateway"
    assert model_a["context_window"] == 1_048_576
    assert model_a["truncation_policy"] == {"mode": "tokens", "limit": 500_000}
    assert model_a["supports_tools"] is True
    gpt5 = by_slug["gpt-5-2025-08-07"]
    assert gpt5["provider"] == "azure"


def test_merge_preserves_comments_and_unrelated_settings(
    home_base: Path, selections, registry
) -> None:
    config_path = home_base / ".codex/config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = (Path(__file__).parent.parent / "fixtures/codex_config_existing.toml").read_text(
        "utf-8"
    )
    config_path.write_text(original, encoding="utf-8")

    report = CodexAdapter(home_base, selections).run()
    assert report.ok
    # config.toml updated in place (the derived catalog file is new => overall
    # status is "created", but the pre-existing file must appear in updated)
    assert str(config_path) in report.files_updated
    text = config_path.read_text("utf-8")
    # comments survive (tomlkit round-trip)
    assert "# User's hand-edited codex config" in text
    assert "# Read-only by default for safety" in text

    doc = tomlkit.loads(text)
    # user's approval_policy preserved, never overwritten
    assert doc["approval_policy"] == "untrusted"
    # unrelated tables preserved
    assert doc["projects"]["/some/project"]["trust_level"] == "trusted"
    # unrelated provider untouched
    assert doc["model_providers"]["ollama"]["name"] == "Local Ollama"
    # default switched to moat's first selection
    assert doc["model_provider"] == "llm-gateway"
    assert doc["model"] == registry.get("llm-gateway").default_model
    assert report.backups


def test_azure_default_sets_disable_response_storage(
    home_base: Path, registry: ProviderRegistry
) -> None:
    az = [ProviderSelection(registry.get("azure"), KEY_AZURE)]
    report = CodexAdapter(home_base, az).run()
    assert report.ok
    doc = tomlkit.loads((home_base / ".codex/config.toml").read_text("utf-8"))
    assert doc["model_provider"] == "azure"
    assert doc["model"] == registry.get("azure").default_model
    assert doc["disable_response_storage"] is True


def test_idempotent_second_run(home_base: Path, selections) -> None:
    CodexAdapter(home_base, selections).run()
    snap_cfg = (home_base / ".codex/config.toml").read_bytes()
    snap_cat = (home_base / ".codex/moat-models.json").read_bytes()
    second = CodexAdapter(home_base, selections).run()
    assert second.status == "no-change"
    assert not second.backups
    assert (home_base / ".codex/config.toml").read_bytes() == snap_cfg
    assert (home_base / ".codex/moat-models.json").read_bytes() == snap_cat


def test_dry_run(home_base: Path, selections) -> None:
    report = CodexAdapter(home_base, selections, dry_run=True).run()
    assert report.ok
    assert not (home_base / ".codex/config.toml").exists()
    assert not (home_base / ".codex/moat-models.json").exists()
