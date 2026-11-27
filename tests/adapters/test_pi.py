"""pi adapter: fresh-home creation, merge into existing, idempotency, dry-run."""

from __future__ import annotations

import json
from pathlib import Path

from moat_install.adapters._base import ProviderSelection
from moat_install.adapters.pi import PiAdapter
from moat_install.providers import ProviderRegistry
from tests.conftest import KEY_AZURE, KEY_GATEWAY, assert_no_secret_outside_env_file


def _run(home: Path, selections, dry_run=False):
    return PiAdapter(home, selections, dry_run=dry_run).run()


def test_fresh_home_creates_models_json(
    home_base: Path, selections, registry: ProviderRegistry
) -> None:
    report = _run(home_base, selections)
    assert report.ok
    assert report.status == "created"
    path = home_base / ".pi/agent/models.json"
    assert path.exists()
    doc = json.loads(path.read_text("utf-8"))
    gateway = registry.get("llm-gateway")
    gw = doc["providers"]["llm-gateway"]
    assert gw["baseUrl"] == gateway.base_url
    assert gw["api"] == "openai-completions"
    assert gw["apiKey"] == "$LLM_GATEWAY_KEY"  # env-ref, never the literal key
    ids = [m["id"] for m in gw["models"]]
    assert ids == [
        "example-org/model-a",
        "example-org/model-b",
        "example-org/model-c",
    ]
    model_a = gw["models"][0]
    assert model_a["contextWindow"] == 1_048_576
    assert model_a["maxTokens"] == 65_536
    assert report.parse_check == "ok"
    assert_no_secret_outside_env_file(home_base, KEY_GATEWAY, KEY_AZURE)


def test_azure_provider_uses_custom_header(home_base: Path, selections) -> None:
    _run(home_base, selections)
    doc = json.loads((home_base / ".pi/agent/models.json").read_text("utf-8"))
    az = doc["providers"]["azure"]
    assert az["api"] == "openai-responses"
    assert az["apiKey"] == ""  # present-but-empty; the credential is in headers
    assert az["headers"]["Ocp-Apim-Subscription-Key"] == "$AZURE_APIM_KEY"
    assert_no_secret_outside_env_file(home_base, KEY_AZURE)


def test_fresh_home_writes_default_selection_settings(home_base: Path, selections) -> None:
    report = _run(home_base, selections)
    assert report.ok
    path = home_base / ".pi/agent/settings.json"
    assert path.exists()
    doc = json.loads(path.read_text("utf-8"))
    # first configured provider + its default_model, both keys together
    # (pi pins the startup model only when both are set)
    assert doc["defaultProvider"] == "llm-gateway"
    assert doc["defaultModel"] == "example-org/model-a"
    assert report.parse_check == "ok"
    assert_no_secret_outside_env_file(home_base, KEY_GATEWAY, KEY_AZURE)


def test_settings_merge_overrides_defaults_preserves_rest(home_base: Path, selections) -> None:
    target = home_base / ".pi/agent/settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        (Path(__file__).parent.parent / "fixtures/pi_settings_existing.json").read_text("utf-8"),
        encoding="utf-8",
    )
    report = _run(home_base, selections)
    assert report.ok
    # settings.json merged (updated, backed up); models.json created fresh
    assert str(target) in report.files_updated
    assert str(home_base / ".pi/agent/models.json") in report.files_created
    doc = json.loads(target.read_text("utf-8"))
    # moat-managed keys overridden in place, unrelated keys preserved
    assert doc["defaultProvider"] == "llm-gateway"
    assert doc["defaultModel"] == "example-org/model-a"
    assert doc["defaultThinkingLevel"] == "high"
    assert doc["hideThinkingBlock"] is True
    assert doc["$schema"] == "https://pi.dev/schemas/settings.schema.json"
    assert report.backups, "pre-existing file must be backed up before first change"


def test_merge_preserves_unrelated_providers_and_keys(home_base: Path, selections) -> None:
    target = home_base / ".pi/agent/models.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        (Path(__file__).parent.parent / "fixtures/pi_models_existing.json").read_text("utf-8"),
        encoding="utf-8",
    )
    report = _run(home_base, selections)
    assert report.ok
    # models.json merged (updated, backed up); settings.json created fresh
    assert str(target) in report.files_updated
    assert str(home_base / ".pi/agent/settings.json") in report.files_created
    doc = json.loads(target.read_text("utf-8"))
    # unrelated provider and top-level keys preserved
    assert doc["providers"]["ollama"]["apiKey"] == ""
    assert doc["modelOverrides"]["claude-sonnet-4"]["contextWindow"] == 123
    # moat entries updated in place, exactly one each
    assert doc["providers"]["llm-gateway"]["apiKey"] == "$LLM_GATEWAY_KEY"
    assert report.backups, "pre-existing file must be backed up before first change"


def test_idempotent_second_run(home_base: Path, selections) -> None:
    _run(home_base, selections)
    models_snapshot = (home_base / ".pi/agent/models.json").read_bytes()
    settings_snapshot = (home_base / ".pi/agent/settings.json").read_bytes()
    second = _run(home_base, selections)
    assert second.status == "no-change"
    assert not second.backups
    assert (home_base / ".pi/agent/models.json").read_bytes() == models_snapshot
    assert (home_base / ".pi/agent/settings.json").read_bytes() == settings_snapshot
    assert all(v == "no-change" for v in second.provider_status.values())


def test_dry_run_writes_nothing(home_base: Path, selections) -> None:
    report = _run(home_base, selections, dry_run=True)
    assert report.ok
    assert not (home_base / ".pi/agent/models.json").exists()
    assert not (home_base / ".pi/agent/settings.json").exists()


def test_default_is_first_selection(home_base: Path, registry: ProviderRegistry) -> None:
    """Single-provider run: only that provider is written."""
    single = [ProviderSelection(registry.get("llm-gateway"), KEY_GATEWAY)]
    _run(home_base, single)
    doc = json.loads((home_base / ".pi/agent/models.json").read_text("utf-8"))
    assert set(doc["providers"]) == {"llm-gateway"}
    settings = json.loads((home_base / ".pi/agent/settings.json").read_text("utf-8"))
    assert settings["defaultProvider"] == "llm-gateway"
    assert settings["defaultModel"] == "example-org/model-a"
