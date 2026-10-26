"""opencode adapter: `provider` (singular), {env:} interpolation, merge."""

from __future__ import annotations

import json
from pathlib import Path

from moat_install.adapters.opencode import OpencodeAdapter
from tests.conftest import (
    KEY_AZURE,
    KEY_GATEWAY,
    assert_no_secret_outside_env_file,
)


def test_fresh_home(home_base: Path, selections, registry) -> None:
    report = OpencodeAdapter(home_base, selections).run()
    assert report.ok and report.status == "created"
    doc = json.loads((home_base / ".config/opencode/opencode.json").read_text("utf-8"))
    assert doc["$schema"] == "https://opencode.ai/config.json"

    gateway = registry.get("llm-gateway")
    gw = doc["provider"]["llm-gateway"]
    assert gw["npm"] == "@ai-sdk/openai-compatible"
    assert gw["options"]["baseURL"] == gateway.base_url
    assert gw["options"]["apiKey"] == "{env:LLM_GATEWAY_KEY}"
    assert "headers" not in gw["options"]
    assert doc["model"] == "llm-gateway/example-org/model-a"
    model_c = gw["models"]["example-org/model-c"]
    assert model_c["limit"] == {"context": 262_144, "output": 32_768}

    az = doc["provider"]["azure"]
    assert az["npm"] == "@ai-sdk/openai"
    assert az["options"]["apiKey"] == ""  # no Bearer header; credential in headers
    assert az["options"]["headers"]["Ocp-Apim-Subscription-Key"] == "{env:AZURE_APIM_KEY}"
    assert_no_secret_outside_env_file(home_base, KEY_GATEWAY, KEY_AZURE)


def test_merge_preserves_unrelated_content(home_base: Path, selections) -> None:
    target = home_base / ".config/opencode/opencode.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    fixture = (Path(__file__).parent.parent / "fixtures/opencode_existing.json").read_text("utf-8")
    target.write_text(fixture, encoding="utf-8")

    report = OpencodeAdapter(home_base, selections).run()
    assert report.ok and report.status == "updated"
    doc = json.loads(target.read_text("utf-8"))
    assert doc["permission"] == {"*": "ask"}
    assert doc["provider"]["ollama"]["name"] == "Ollama"
    assert doc["provider"]["llm-gateway"]["options"]["apiKey"] == "{env:LLM_GATEWAY_KEY}"
    assert report.backups


def test_idempotent_second_run(home_base: Path, selections) -> None:
    OpencodeAdapter(home_base, selections).run()
    snapshot = (home_base / ".config/opencode/opencode.json").read_bytes()
    second = OpencodeAdapter(home_base, selections).run()
    assert second.status == "no-change"
    assert not second.backups
    assert (home_base / ".config/opencode/opencode.json").read_bytes() == snapshot


def test_dry_run(home_base: Path, selections) -> None:
    OpencodeAdapter(home_base, selections, dry_run=True).run()
    assert not (home_base / ".config/opencode/opencode.json").exists()
