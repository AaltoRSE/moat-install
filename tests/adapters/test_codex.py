"""codex adapter: TOML round-trip, catalog derivation, conservative defaults."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import tomlkit

from moat_install.adapters._base import ProviderSelection
from moat_install.adapters.codex import CodexAdapter
from moat_install.providers import ModelInfo, ProviderRegistry
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


def test_catalog_requires_truncation_policy_without_context_window(
    home_base: Path, registry: ProviderRegistry
) -> None:
    """codex-cli (verified against 0.167.0) requires truncation_policy on every
    catalog entry: a model with unknown context window (discovered metadata 0)
    must still get one, with the 500k fallback limit."""
    profile = replace(
        registry.get("llm-gateway"),
        models=(
            ModelInfo(
                id="example-org/model-a",
                display_name="No Metadata",
                context_window=0,
                max_output_tokens=0,
            ),
        ),
    )
    report = CodexAdapter(home_base, [ProviderSelection(profile, KEY_GATEWAY)]).run()
    assert report.ok
    catalog = json.loads((home_base / ".codex/moat-models.json").read_text("utf-8"))
    (model,) = catalog["models"]
    # context_window stays omitted when unknown (never a zero)
    assert "context_window" not in model
    assert model["truncation_policy"] == {"mode": "tokens", "limit": 500_000}


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


def test_sandbox_mode_container_vs_host(home_base: Path, selections) -> None:
    """Container mode (default) grants full access *within* the container
    (moat provides containment); host mode (--init-home-dir) never relaxes
    the sandbox and strips a previously written grant."""
    # Container mode (default): sandbox_mode set on create.
    CodexAdapter(home_base, selections).run()
    doc = tomlkit.loads((home_base / ".codex/config.toml").read_text("utf-8"))
    assert doc["sandbox_mode"] == "danger-full-access"

    # User-set value is preserved, never overwritten.
    config_path = home_base / ".codex/config.toml"
    config_path.write_text('sandbox_mode = "read-only"\n', encoding="utf-8")
    report = CodexAdapter(home_base, selections).run()
    assert report.ok
    doc = tomlkit.loads(config_path.read_text("utf-8"))
    assert doc["sandbox_mode"] == "read-only"
    assert not report.warnings

    # Host mode: never set.
    fresh = home_base.parent / "host-home"
    fresh.mkdir()
    report = CodexAdapter(fresh, selections, container_mode=False).run()
    assert report.ok and not report.warnings
    doc = tomlkit.loads((fresh / ".codex/config.toml").read_text("utf-8"))
    assert "sandbox_mode" not in doc

    # Host mode: a previously written danger-full-access grant is removed.
    (fresh / ".codex/config.toml").write_text(
        'sandbox_mode = "danger-full-access"\n', encoding="utf-8"
    )
    report = CodexAdapter(fresh, selections, container_mode=False).run()
    assert report.ok
    assert report.warnings  # stripped with a warning, not silently
    doc = tomlkit.loads((fresh / ".codex/config.toml").read_text("utf-8"))
    assert "sandbox_mode" not in doc


def test_daemon_auto_start_container_vs_host(home_base: Path, selections) -> None:
    """Container mode disables codex's daemon auto-start ([features]
    daemon_auto_start = false, verified against codex-cli 0.167.0) unless the
    user set it; host mode leaves the setting untouched entirely."""
    # Container mode (default): set on create.
    CodexAdapter(home_base, selections).run()
    doc = tomlkit.loads((home_base / ".codex/config.toml").read_text("utf-8"))
    assert doc["features"]["daemon_auto_start"] is False

    # User-set value is preserved, never overwritten.
    config_path = home_base / ".codex/config.toml"
    config_path.write_text("[features]\ndaemon_auto_start = true\n", encoding="utf-8")
    report = CodexAdapter(home_base, selections).run()
    assert report.ok
    doc = tomlkit.loads(config_path.read_text("utf-8"))
    assert doc["features"]["daemon_auto_start"] is True
    assert not report.warnings

    # Host mode: never set (a restriction, not a grant — nothing to strip).
    fresh = home_base.parent / "host-home-daemon"
    fresh.mkdir()
    report = CodexAdapter(fresh, selections, container_mode=False).run()
    assert report.ok and not report.warnings
    doc = tomlkit.loads((fresh / ".codex/config.toml").read_text("utf-8"))
    assert "features" not in doc


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
