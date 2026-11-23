"""httpcheck: auth header construction, masking, urllib mocking (no network)."""

from __future__ import annotations

import io
import json
import secrets
import urllib.error
from email.message import Message

import pytest

from moat_install.httpcheck import auth_headers, check_provider, discover_models
from moat_install.providers import AUTH_BEARER, ProviderProfile

SECRET = secrets.token_hex(24)


class FakeResponse:
    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body.encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_bearer_headers(registry) -> None:
    headers = auth_headers(registry.get("llm-gateway"), SECRET)
    assert headers == {"Authorization": f"Bearer {SECRET}"}


def test_custom_header_for_azure(registry) -> None:
    headers = auth_headers(registry.get("azure"), SECRET)
    assert headers == {"Ocp-Apim-Subscription-Key": SECRET}


def test_success_with_data_list(monkeypatch: pytest.MonkeyPatch, registry) -> None:
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(json.dumps({"data": [{"id": "m1"}]}))

    ok, detail = check_provider(registry.get("llm-gateway"), SECRET, urlopen_fn=fake_urlopen)
    assert ok
    assert "/models" in seen["url"]
    assert seen["auth"] == f"Bearer {SECRET}"
    assert SECRET not in detail


def test_http_error_is_reported_and_masked(monkeypatch: pytest.MonkeyPatch, registry) -> None:
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=io.BytesIO(b'{"detail":"Invalid API Key"}'),
        )

    ok, detail = check_provider(registry.get("llm-gateway"), SECRET, urlopen_fn=fake_urlopen)
    assert not ok
    assert "401" in detail
    assert "reachable from a specific network or via VPN" in detail
    assert SECRET not in detail


def test_url_error_mentions_hint(monkeypatch: pytest.MonkeyPatch, registry) -> None:
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    ok, detail = check_provider(registry.get("llm-gateway"), SECRET, urlopen_fn=fake_urlopen)
    assert not ok
    assert "connection refused" in detail
    assert "VPN" in detail


def test_non_json_body_fails(monkeypatch: pytest.MonkeyPatch, registry) -> None:
    ok, detail = check_provider(
        registry.get("llm-gateway"),
        SECRET,
        urlopen_fn=lambda req, timeout=None: FakeResponse("<html/>"),
    )
    assert not ok
    assert "non-JSON" in detail


def test_json_without_model_list_fails(monkeypatch: pytest.MonkeyPatch, registry) -> None:
    ok, _ = check_provider(
        registry.get("llm-gateway"),
        SECRET,
        urlopen_fn=lambda req, timeout=None: FakeResponse('{"nope": 1}'),
    )
    assert not ok


def test_models_key_also_accepted(monkeypatch: pytest.MonkeyPatch, registry) -> None:
    ok, _ = check_provider(
        registry.get("llm-gateway"),
        SECRET,
        urlopen_fn=lambda req, timeout=None: FakeResponse('{"models": [{"id": "x"}]}'),
    )
    assert ok


def test_bare_top_level_array_accepted(monkeypatch: pytest.MonkeyPatch, registry) -> None:
    """Some gateways return a bare JSON array instead of {"data": [...]}."""
    ok, detail = check_provider(
        registry.get("llm-gateway"),
        SECRET,
        urlopen_fn=lambda req, timeout=None: FakeResponse('[{"id": "m1"}, {"id": "m2"}]'),
    )
    assert ok
    assert "2 models" in detail


def test_non_list_json_scalar_fails(monkeypatch: pytest.MonkeyPatch, registry) -> None:
    ok, detail = check_provider(
        registry.get("llm-gateway"),
        SECRET,
        urlopen_fn=lambda req, timeout=None: FakeResponse('"just a string"'),
    )
    assert not ok
    assert "not a JSON object or array" in detail


# ----------------------------------------------------------------------
# Model discovery (providers whose specification omits 'models')
# ----------------------------------------------------------------------


def _discovery_profile() -> ProviderProfile:
    """A discovery-mode profile: no catalog, no default model."""
    return ProviderProfile(
        name="gw",
        base_url="https://gw.example/api/v1",
        api_key_env="GW_KEY",
        auth=AUTH_BEARER,
    )


def test_discover_models_parses_data_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {
            "data": [
                {"id": "m1", "display_name": "Model One"},
                {"id": "m2"},
            ]
        }
    )
    ok, detail, models, default = discover_models(
        _discovery_profile(), SECRET, urlopen_fn=lambda req, timeout=None: FakeResponse(body)
    )
    assert ok
    assert "2 model(s) discovered" in detail
    assert SECRET not in detail
    assert [m.id for m in models] == ["m1", "m2"]
    assert models[0].display_name == "Model One"
    assert models[1].display_name == "m2"  # falls back to the id
    assert default == "m1"  # first discovered model is the default


def test_discover_models_parses_limit_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Context/output limits are picked up under their alternative spellings;
    absent metadata becomes 0 (unknown)."""
    body = json.dumps(
        {
            "data": [
                {"id": "m1", "context_length": 4096, "max_output_tokens": 1024},
                {"id": "m2"},
            ]
        }
    )
    ok, _detail, models, _default = discover_models(
        _discovery_profile(), SECRET, urlopen_fn=lambda req, timeout=None: FakeResponse(body)
    )
    assert ok
    assert models[0].context_window == 4096
    assert models[0].max_output_tokens == 1024
    assert models[1].context_window == 0  # unknown
    assert models[1].max_output_tokens == 0  # unknown


def test_discover_models_deduplicates_and_skips_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(
        {
            "data": [
                {"id": "m1"},
                {"id": "m1"},  # duplicate
                {"no_id_here": True},  # unusable
                "not-a-dict",  # unusable
            ]
        }
    )
    ok, _detail, models, _default = discover_models(
        _discovery_profile(), SECRET, urlopen_fn=lambda req, timeout=None: FakeResponse(body)
    )
    assert ok
    assert [m.id for m in models] == ["m1"]


def test_discover_models_without_usable_entries_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    ok, detail, models, default = discover_models(
        _discovery_profile(),
        SECRET,
        urlopen_fn=lambda req, timeout=None: FakeResponse('{"data": [42]}'),
    )
    assert not ok
    assert "no usable model entries" in detail
    assert models == ()
    assert default == ""


def test_discover_models_specified_default_must_be_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderProfile(
        name="gw",
        base_url="https://gw.example/api/v1",
        api_key_env="GW_KEY",
        auth=AUTH_BEARER,
        default_model="m2",
    )
    body = json.dumps({"data": [{"id": "m1"}, {"id": "m3"}]})
    ok, detail, models, default = discover_models(
        profile, SECRET, urlopen_fn=lambda req, timeout=None: FakeResponse(body)
    )
    assert not ok
    assert "default_model 'm2' is not offered" in detail
    assert "m1" in detail and "m3" in detail  # available models listed
    assert models == ()
    assert default == ""


def test_discover_models_specified_default_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = ProviderProfile(
        name="gw",
        base_url="https://gw.example/api/v1",
        api_key_env="GW_KEY",
        auth=AUTH_BEARER,
        default_model="m2",
    )
    body = json.dumps({"data": [{"id": "m1"}, {"id": "m2"}]})
    ok, _detail, _models, default = discover_models(
        profile, SECRET, urlopen_fn=lambda req, timeout=None: FakeResponse(body)
    )
    assert ok
    assert default == "m2"


def test_discover_models_http_error_is_reported_and_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=io.BytesIO(b'{"detail":"Invalid API Key"}'),
        )

    ok, detail, models, _default = discover_models(
        _discovery_profile(), SECRET, urlopen_fn=fake_urlopen
    )
    assert not ok
    assert "401" in detail
    assert SECRET not in detail
    assert models == ()


def test_discover_models_bare_array(monkeypatch: pytest.MonkeyPatch) -> None:
    ok, _detail, models, default = discover_models(
        _discovery_profile(),
        SECRET,
        urlopen_fn=lambda req, timeout=None: FakeResponse('[{"id": "m1"}]'),
    )
    assert ok
    assert [m.id for m in models] == ["m1"]
    assert default == "m1"
