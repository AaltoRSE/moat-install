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
    # per-provider catalogs: the process-global default catalog holds only the
    # default provider's models; other providers get their own file
    assert config_path.parent.joinpath("moat-models.json").exists()
    assert config_path.parent.joinpath("moat-models-azure.json").exists()

    # Profile-V2 overlays: codex -p <provider> switches provider + catalog
    gw_overlay = tomlkit.loads(
        config_path.parent.joinpath("llm-gateway.config.toml").read_text("utf-8")
    )
    assert gw_overlay["model"] == gateway.default_model
    assert gw_overlay["model_provider"] == "llm-gateway"
    assert gw_overlay["model_catalog_json"] == "moat-models.json"
    assert gw_overlay["disable_response_storage"] is False
    az_overlay = tomlkit.loads(config_path.parent.joinpath("azure.config.toml").read_text("utf-8"))
    assert az_overlay["model_provider"] == "azure"
    assert az_overlay["model_catalog_json"] == "moat-models-azure.json"
    assert az_overlay["disable_response_storage"] is True

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


def test_catalogs_are_scoped_per_provider(home_base: Path, selections, registry) -> None:
    """The codex catalog is process-global and provider-agnostic (verified
    against codex-cli 0.172.0): each catalog file must hold a single
    provider's models, or the picker offers models the active provider does
    not serve and requests are sent to the wrong provider."""
    CodexAdapter(home_base, selections).run()
    codex_dir = home_base / ".codex"
    default_catalog = json.loads((codex_dir / "moat-models.json").read_text("utf-8"))
    azure_catalog = json.loads((codex_dir / "moat-models-azure.json").read_text("utf-8"))
    assert {m["slug"] for m in default_catalog["models"]} == {
        m.id for m in registry.get("llm-gateway").models
    }
    assert {m["slug"] for m in azure_catalog["models"]} == {
        m.id for m in registry.get("azure").models
    }
    # entries carry parsed ModelInfo fields only: provider/name/model are
    # silently ignored by codex and must not be written
    entry = default_catalog["models"][0]
    assert "provider" not in entry
    assert "name" not in entry
    assert "model" not in entry
    assert entry["shell_type"] == "unified_exec"
    assert entry["visibility"] == "list"
    assert entry["supported_in_api"] is True
    assert entry["base_instructions"]
    # required by the codex ModelInfo schema (no serde default)
    assert entry["support_verbosity"] is False
    assert entry["experimental_supported_tools"] == []
    model_a = next(m for m in default_catalog["models"] if m["slug"] == "example-org/model-a")
    assert model_a["context_window"] == 1_048_576
    assert model_a["truncation_policy"] == {"mode": "tokens", "limit": 500_000}
    for dropped in (
        "supports_tools",
        "supports_parallel_tool_calls",
        "supports_reasoning_summaries",
    ):
        assert dropped not in model_a


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
    # single-provider run: the default provider still gets its overlay
    assert (home_base / ".codex/llm-gateway.config.toml").exists()


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
    overlay = tomlkit.loads((home_base / ".codex/azure.config.toml").read_text("utf-8"))
    assert overlay["model_provider"] == "azure"
    assert overlay["disable_response_storage"] is True


def test_codex_profile_name_override(home_base: Path, registry: ProviderRegistry) -> None:
    """codex_profile in the specification names the -p overlay; it defaults
    to the provider name."""
    profile = replace(registry.get("azure"), codex_profile="apim")
    report = CodexAdapter(home_base, [ProviderSelection(profile, KEY_AZURE)]).run()
    assert report.ok
    overlay = home_base / ".codex/apim.config.toml"
    assert overlay.exists()
    assert not (home_base / ".codex/azure.config.toml").exists()
    doc = tomlkit.loads(overlay.read_text("utf-8"))
    assert doc["model_provider"] == "azure"
    # sole provider => it is the default => default catalog file
    assert doc["model_catalog_json"] == "moat-models.json"


def test_legacy_profile_conflict_warns(home_base: Path, selections) -> None:
    """codex -p <name> refuses to start while config.toml has a legacy
    profile selector of the same name (verified against the 0.172.0 loader)."""
    config_path = home_base / ".codex/config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('[profiles.azure]\nmodel = "legacy"\n', encoding="utf-8")
    report = CodexAdapter(home_base, selections).run()
    assert report.ok
    assert any("[profiles.azure]" in w for w in report.warnings)


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
    codex_dir = home_base / ".codex"
    managed = [
        "config.toml",
        "moat-models.json",
        "moat-models-azure.json",
        "llm-gateway.config.toml",
        "azure.config.toml",
    ]
    snapshots = {name: (codex_dir / name).read_bytes() for name in managed}
    second = CodexAdapter(home_base, selections).run()
    assert second.status == "no-change"
    assert not second.backups
    for name, data in snapshots.items():
        assert (codex_dir / name).read_bytes() == data, name


def test_dry_run(home_base: Path, selections) -> None:
    report = CodexAdapter(home_base, selections, dry_run=True).run()
    assert report.ok
    for name in (
        "config.toml",
        "moat-models.json",
        "moat-models-azure.json",
        "llm-gateway.config.toml",
        "azure.config.toml",
    ):
        assert not (home_base / ".codex" / name).exists()
