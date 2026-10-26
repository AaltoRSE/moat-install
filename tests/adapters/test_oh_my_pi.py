"""oh-my-pi adapter: mirrors pi's format under ~/.omp/agent."""

from __future__ import annotations

import json
from pathlib import Path

from moat_install.adapters.oh_my_pi import OhMyPiAdapter
from moat_install.providers import ProviderRegistry
from tests.conftest import KEY_GATEWAY, assert_no_secret_outside_env_file


def test_config_path_and_shape(home_base: Path, selections, registry: ProviderRegistry) -> None:
    report = OhMyPiAdapter(home_base, selections).run()
    assert report.ok
    path = home_base / ".omp/agent/models.json"
    assert path.exists()
    doc = json.loads(path.read_text("utf-8"))
    gw = doc["providers"]["llm-gateway"]
    assert gw["baseUrl"] == registry.get("llm-gateway").base_url
    assert gw["apiKey"] == "$LLM_GATEWAY_KEY"
    assert gw["api"] == "openai-completions"
    az = doc["providers"]["azure"]
    assert az["headers"]["Ocp-Apim-Subscription-Key"] == "$AZURE_APIM_KEY"
    assert_no_secret_outside_env_file(home_base, KEY_GATEWAY)


def test_idempotent(home_base: Path, selections) -> None:
    OhMyPiAdapter(home_base, selections).run()
    snapshot = (home_base / ".omp/agent/models.json").read_bytes()
    second = OhMyPiAdapter(home_base, selections).run()
    assert second.status == "no-change"
    assert (home_base / ".omp/agent/models.json").read_bytes() == snapshot
