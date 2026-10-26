"""Provider specification loading: schema validation and error reporting."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from moat_install.providers import (
    AUTH_BEARER,
    AUTH_HEADER,
    SPEC_ENV_VAR,
    ProviderProfile,
    SpecificationError,
    load_provider_specification,
)
from tests.conftest import ENV_GATEWAY, TEST_SPEC, write_spec


def test_loads_both_profiles_in_file_order(tmp_path: Path, registry) -> None:
    registry = load_provider_specification(write_spec(tmp_path / "p.json"))
    assert registry.names() == ("llm-gateway", "azure")
    gw = registry.get("llm-gateway")
    assert gw.auth == AUTH_BEARER
    assert gw.api_key_env == ENV_GATEWAY
    az = registry.get("azure")
    assert az.auth == AUTH_HEADER
    assert az.auth_header == "Ocp-Apim-Subscription-Key"


def test_spec_env_var_name() -> None:
    assert SPEC_ENV_VAR == "MOAT_INSTALL_PROVIDERS"


def test_default_model_is_in_catalog(tmp_path: Path, registry) -> None:
    registry = load_provider_specification(write_spec(tmp_path / "p.json"))
    for profile in registry.profiles.values():
        assert profile.default_model in {m.id for m in profile.models}


def test_model_lookup(tmp_path: Path, registry) -> None:
    registry = load_provider_specification(write_spec(tmp_path / "p.json"))
    gw = registry.get("llm-gateway")
    model = gw.model("example-org/model-a")
    assert model.context_window > 0
    with pytest.raises(KeyError):
        gw.model("does-not-exist")


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SpecificationError, match="cannot read"):
        load_provider_specification(tmp_path / "nope.json")


def test_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SpecificationError, match="invalid JSON"):
        load_provider_specification(path)


def test_top_level_must_be_object(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SpecificationError, match="top level"):
        load_provider_specification(path)


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    spec["providerz"] = []
    with pytest.raises(SpecificationError, match="unknown top-level key"):
        load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_missing_providers_key(tmp_path: Path) -> None:
    with pytest.raises(SpecificationError, match="missing required key 'providers'"):
        load_provider_specification(write_spec(tmp_path / "p.json", {}))


def test_empty_providers_rejected(tmp_path: Path) -> None:
    with pytest.raises(SpecificationError, match="non-empty"):
        load_provider_specification(write_spec(tmp_path / "p.json", {"providers": []}))


def test_unknown_provider_key_rejected(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    # The value is irrelevant — the unknown *key* must be rejected.
    spec["providers"][0]["api_Key"] = ""
    with pytest.raises(SpecificationError, match="unknown key"):
        load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_unknown_model_key_rejected(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    spec["providers"][0]["models"][0]["contextWindow"] = 5
    with pytest.raises(SpecificationError, match="unknown key"):
        load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_missing_required_provider_key(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    del spec["providers"][0]["base_url"]
    with pytest.raises(SpecificationError, match="missing required key"):
        load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_missing_required_model_key(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    del spec["providers"][0]["models"][0]["context_window"]
    with pytest.raises(SpecificationError, match="missing required key"):
        load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_default_model_must_be_in_catalog(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    spec["providers"][0]["default_model"] = "nope"
    with pytest.raises(SpecificationError, match="default_model"):
        load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_header_auth_requires_auth_header(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    del spec["providers"][1]["auth_header"]
    with pytest.raises(SpecificationError, match="auth_header"):
        load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_unknown_auth_style_rejected(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    spec["providers"][0]["auth"] = "cookie"
    with pytest.raises(SpecificationError, match="'auth'"):
        load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_invalid_provider_name_rejected(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    spec["providers"][0]["name"] = "Has Space"
    with pytest.raises(SpecificationError, match="'name'"):
        load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_duplicate_names_rejected(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    spec["providers"][1]["name"] = spec["providers"][0]["name"]
    with pytest.raises(SpecificationError, match="duplicate name"):
        load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_empty_models_rejected(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    spec["providers"][0]["models"] = []
    with pytest.raises(SpecificationError, match="non-empty"):
        load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_bad_model_context_window_rejected(tmp_path: Path) -> None:
    for bad in (0, -5, "big", 1.5, True):
        spec = copy.deepcopy(TEST_SPEC)
        spec["providers"][0]["models"][0]["context_window"] = bad
        with pytest.raises(SpecificationError, match="context_window"):
            load_provider_specification(write_spec(tmp_path / "p.json", spec))


def test_error_message_identifies_file_and_provider(tmp_path: Path) -> None:
    spec = copy.deepcopy(TEST_SPEC)
    spec["providers"][1]["default_model"] = "nope"
    path = write_spec(tmp_path / "p.json", spec)
    with pytest.raises(SpecificationError) as excinfo:
        load_provider_specification(path)
    assert str(path) in str(excinfo.value)
    assert "azure" in str(excinfo.value)


def test_optional_fields_have_defaults(tmp_path: Path, registry) -> None:
    """A minimal spec parses with derived/optional fields defaulted."""
    minimal = {
        "providers": [
            {
                "name": "minimal",
                "base_url": "https://x.example/api/v1",
                "api_key_env": ENV_GATEWAY,
                "auth": "bearer",
                "default_model": "m1",
                "models": [
                    {
                        "id": "m1",
                        "display_name": "M1",
                        "context_window": 1000,
                        "max_output_tokens": 100,
                    }
                ],
            }
        ]
    }
    registry = load_provider_specification(write_spec(tmp_path / "p.json", minimal))
    profile = registry.get("minimal")
    assert profile.display_name == "minimal"
    assert profile.pi_api == "openai-completions"
    assert profile.opencode_npm == "@ai-sdk/openai-compatible"
    assert profile.codex_disable_response_storage is False


def test_provider_profile_direct_validation() -> None:
    with pytest.raises(ValueError):
        ProviderProfile(
            name="x",
            base_url="https://x",
            api_key_env="X",
            auth=AUTH_BEARER,
            models=(),
            default_model="missing",
        )
