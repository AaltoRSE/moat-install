"""cline adapter: providers.json schema, multiple instances, literal azure header."""

from __future__ import annotations

import json
import re
from pathlib import Path

from moat_install.adapters._base import ProviderSelection
from moat_install.adapters.cline import ClineAdapter
from moat_install.providers import ProviderRegistry
from tests.conftest import (
    KEY_AZURE,
    KEY_GATEWAY,
    assert_no_secret_outside_env_file,
)


def _doc(home_base: Path) -> dict:
    return json.loads((home_base / ".cline/data/settings/providers.json").read_text("utf-8"))


def test_fresh_home_schema(home_base: Path, selections, registry: ProviderRegistry) -> None:
    report = ClineAdapter(home_base, selections).run()
    assert report.ok and report.status == "created"
    doc = _doc(home_base)
    assert doc["version"] == 1
    assert doc["lastUsedProvider"] == "moat-llm-gateway"

    gw = doc["providers"]["moat-llm-gateway"]
    settings = gw["settings"]
    assert settings["provider"] == "openai-compatible"
    assert settings["baseUrl"] == registry.get("llm-gateway").base_url
    assert settings["model"] == registry.get("llm-gateway").default_model
    assert settings["protocol"] == "openai-chat"
    assert "apiKey" not in settings  # env-ref: resolved from OPENAI_API_KEY
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", gw["updatedAt"])
    assert gw["tokenSource"] == "manual"

    az = doc["providers"]["moat-azure"]
    assert az["settings"]["headers"]["Ocp-Apim-Subscription-Key"] == KEY_AZURE
    assert az["settings"]["baseUrl"].endswith("/deployments/gpt-5-2025-08-07")
    assert report.parse_check == "ok"
    # gateway instance is env-ref; only the azure header file holds the literal
    gw_text = json.dumps(doc["providers"]["moat-llm-gateway"])
    assert KEY_GATEWAY not in gw_text


def test_mixed_key_mode_and_chmod(home_base: Path, selections) -> None:
    report = ClineAdapter(home_base, selections).run()
    assert report.key_mode == "mixed"
    assert any("plaintext" in w for w in report.warnings)
    mode = (home_base / ".cline/data/settings/providers.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_gateway_only_is_env_ref(home_base: Path, registry: ProviderRegistry) -> None:
    report = ClineAdapter(
        home_base, [ProviderSelection(registry.get("llm-gateway"), KEY_GATEWAY)]
    ).run()
    assert report.key_mode == "env-ref"
    assert_no_secret_outside_env_file(home_base, KEY_GATEWAY)


def test_merge_preserves_user_instances(home_base: Path, selections) -> None:
    target = home_base / ".cline/data/settings/providers.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    fixture = (Path(__file__).parent.parent / "fixtures/cline_providers_existing.json").read_text(
        "utf-8"
    )
    target.write_text(fixture, encoding="utf-8")

    report = ClineAdapter(home_base, selections).run()
    assert report.ok
    doc = _doc(home_base)
    # user's own instance untouched
    user = doc["providers"]["openai-compatible"]
    assert user["settings"]["baseUrl"] == "https://api.example.com/v1"
    assert user["settings"]["model"] == "my-model"
    # moat instances added, default switched to ours
    assert "moat-llm-gateway" in doc["providers"]
    assert doc["lastUsedProvider"] == "moat-llm-gateway"
    assert report.backups


def test_env_aliases(registry: ProviderRegistry) -> None:
    aliases = ClineAdapter.env_aliases(registry.get("llm-gateway"))
    assert aliases == {"OPENAI_API_KEY": "$LLM_GATEWAY_KEY"}


def test_idempotent_second_run(home_base: Path, selections) -> None:
    ClineAdapter(home_base, selections).run()
    snapshot = (home_base / ".cline/data/settings/providers.json").read_bytes()
    second = ClineAdapter(home_base, selections).run()
    assert second.status == "no-change"
    assert not second.backups
    assert (home_base / ".cline/data/settings/providers.json").read_bytes() == snapshot


def test_dry_run(home_base: Path, selections) -> None:
    ClineAdapter(home_base, selections, dry_run=True).run()
    assert not (home_base / ".cline/data/settings/providers.json").exists()
