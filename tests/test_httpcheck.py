"""httpcheck: auth header construction, masking, urllib mocking (no network)."""

from __future__ import annotations

import io
import json
import secrets
import urllib.error
from email.message import Message

import pytest

from moat_install.httpcheck import auth_headers, check_provider

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
