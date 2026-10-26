"""Shared fixtures: provider specification, temp home base, HTTP mocking, key masking."""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Iterator, Tuple

import pytest

import moat_install.cli as cli_module
import moat_install.httpcheck as httpcheck_module
from moat_install.adapters._base import ProviderSelection
from moat_install.providers import ProviderRegistry, load_provider_specification

# Randomly generated per test session — no test may depend on a particular
# key value, and no (fake) key material is ever committed. The ``gw-``/``az-``
# prefixes only make written files easier to eyeball when a test fails.
KEY_GATEWAY = "gw-" + secrets.token_hex(24)
KEY_AZURE = "az-" + secrets.token_hex(24)

#: Environment-variable *names* used by the test specification. These are
#: identifiers (referenced by the spec, the ``keys_env`` fixture and written
#: configs alike), not secrets — but they are kept in constants so no quoted
#: ``api_key_env: "…"`` assignment appears in test code (detect-secrets
#: flags any non-empty quoted value assigned to a ``*key*``-named field).
ENV_GATEWAY = "LLM_GATEWAY_KEY"
ENV_AZURE = "AZURE_APIM_KEY"
FIXTURES = Path(__file__).parent / "fixtures"

#: The provider specification used by tests — mirrors the shape of the shipped
#: examples but with placeholder endpoints (the repository contains no real
#: URL locations).
TEST_SPEC = {
    "providers": [
        {
            "name": "llm-gateway",
            "display_name": "Example LLM Gateway",
            "base_url": "https://llm-gateway.example/api/v1",
            "api_key_env": ENV_GATEWAY,
            "auth": "bearer",
            "default_model": "example-org/model-a",
            "models": [
                {
                    "id": "example-org/model-a",
                    "display_name": "Model A",
                    "context_window": 1048576,
                    "max_output_tokens": 65536,
                },
                {
                    "id": "example-org/model-b",
                    "display_name": "Model B",
                    "context_window": 131072,
                    "max_output_tokens": 8192,
                },
                {
                    "id": "example-org/model-c",
                    "display_name": "Model C",
                    "context_window": 262144,
                    "max_output_tokens": 32768,
                },
            ],
            "pi_api": "openai-completions",
            "opencode_npm": "@ai-sdk/openai-compatible",
            "codex_disable_response_storage": False,
        },
        {
            "name": "azure",
            "display_name": "Example Azure OpenAI",
            "base_url": "https://azure-openai.example/v1/openai",
            "api_key_env": ENV_AZURE,
            "auth": "header",
            "auth_header": "Ocp-Apim-Subscription-Key",
            "default_model": "gpt-5-2025-08-07",
            "models": [
                {
                    "id": "gpt-5-2025-08-07",
                    "display_name": "GPT-5 (Azure)",
                    "context_window": 131072,
                    "max_output_tokens": 8192,
                }
            ],
            "pi_api": "openai-responses",
            "opencode_npm": "@ai-sdk/openai",
            "codex_disable_response_storage": True,
        },
    ]
}


def write_spec(path: Path, spec: dict = TEST_SPEC) -> Path:
    """Write a provider specification JSON file (used by tests and fixtures)."""
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


@pytest.fixture(scope="session")
def registry(tmp_path_factory: pytest.TempPathFactory) -> ProviderRegistry:
    """Session-wide registry parsed from the test specification."""
    return load_provider_specification(
        write_spec(tmp_path_factory.mktemp("spec") / "providers.json")
    )


@pytest.fixture(autouse=True)
def _isolate_home_base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic resolution: no test inherits a developer's home-base override."""
    monkeypatch.delenv("MOAT_INSTALL_HOME_BASE", raising=False)


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Reset package-logging state after every test.

    ``cli.run()`` installs a stderr handler on the ``moat_install`` logger; a
    handler bound to a finished capture stream must not leak into later tests.
    Production never needs this: ``setup_logging`` replaces its handler on
    every run.
    """
    yield
    pkg = logging.getLogger("moat_install")
    for handler in list(pkg.handlers):
        pkg.removeHandler(handler)
    pkg.setLevel(logging.NOTSET)
    pkg.propagate = True


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    """A provider specification file in the test's tmp dir (for CLI runs)."""
    return write_spec(tmp_path / "providers.json")


@pytest.fixture
def home_base(tmp_path: Path) -> Path:
    """A fresh home-base directory for a test run."""
    base = tmp_path / "moat-home"
    base.mkdir()
    return base


@pytest.fixture
def keys_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide both provider keys via environment variables."""
    monkeypatch.setenv("LLM_GATEWAY_KEY", KEY_GATEWAY)
    monkeypatch.setenv("AZURE_APIM_KEY", KEY_AZURE)


@pytest.fixture
def mock_validate(monkeypatch: pytest.MonkeyPatch) -> Iterator[Tuple[list, list]]:
    """Mock the /models validation so no network is touched.

    Records (profile-name, key) calls; tests may pre-load outcomes. Returns
    the (calls, outcomes) lists for inspection/mutation.
    """
    calls: list = []
    outcomes: list = []

    def fake_check(profile, key, timeout=httpcheck_module.DEFAULT_TIMEOUT_S, urlopen_fn=None):
        calls.append((profile.name, key))
        if outcomes:
            return outcomes.pop(0)
        return True, f"{profile.name}: /models OK (mocked)"

    monkeypatch.setattr(cli_module, "check_provider", fake_check)
    yield calls, outcomes


@pytest.fixture
def selections(registry: ProviderRegistry) -> list:
    """Default ordered selections (llm-gateway first, azure second)."""
    return [
        ProviderSelection(registry.get("llm-gateway"), KEY_GATEWAY),
        ProviderSelection(registry.get("azure"), KEY_AZURE),
    ]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def assert_no_secret_outside_env_file(home: Path, *keys: str, exempt: tuple = ()) -> None:
    """No key value may appear in any file except the managed env file.

    ``exempt`` lists (path, key) pairs that are allowed to contain the key
    (e.g. cline's providers.json azure header in literal-key mode).
    """
    env_file = home / ".config/moat-install/env.sh"
    for key in keys:
        for path in home.rglob("*"):
            if not path.is_file() or path == env_file:
                continue
            if any(path == home / rel and allowed == key for rel, allowed in exempt):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            assert key not in text, f"secret leaked into {path}"
