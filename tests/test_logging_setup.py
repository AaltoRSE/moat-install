"""The logging standard (``moat_install.logging_setup``).

Covers: logger namespacing, handler idempotency, level switching, the
``[debug]`` output format, and the guarantee that debug traces of file
creation and HTTP connections never contain secret key material.
"""

from __future__ import annotations

import email.message
import io
import json
import logging
import urllib.error
from pathlib import Path
from typing import Any, Iterator

import pytest

from moat_install.adapters.pi import PiAdapter
from moat_install.cli import run
from moat_install.httpcheck import check_provider
from moat_install.logging_setup import LOGGER_NAME, get_logger, setup_logging
from tests.conftest import ENV_GATEWAY, KEY_GATEWAY

# ----------------------------------------------------------------------
# logging_setup unit tests
# ----------------------------------------------------------------------


def test_get_logger_namespaces_under_the_package() -> None:
    assert get_logger() is logging.getLogger("moat_install")
    assert get_logger("moat_install.home") is logging.getLogger("moat_install.home")
    assert get_logger("home") is logging.getLogger("moat_install.home")
    # Everything hangs below the package logger, so setup_logging governs it.
    assert get_logger("anything").name.startswith(f"{LOGGER_NAME}.")


def test_setup_logging_is_idempotent() -> None:
    setup_logging(debug=True, stream=io.StringIO())
    setup_logging(debug=True, stream=io.StringIO())
    assert len(get_logger().handlers) == 1


def test_debug_level_emits_prefixed_records() -> None:
    stream = io.StringIO()
    setup_logging(debug=True, stream=stream)
    log = get_logger("test_debug_level")
    log.debug("decision %d", 42)
    log.warning("careful")
    out = stream.getvalue()
    assert "[debug] decision 42" in out
    assert "[warning] careful" in out


def test_warning_level_drops_debug_records() -> None:
    stream = io.StringIO()
    setup_logging(debug=False, stream=stream)
    log = get_logger("test_warning_level")
    log.debug("invisible")
    log.warning("visible")
    out = stream.getvalue()
    assert "invisible" not in out
    assert "visible" in out


# ----------------------------------------------------------------------
# CLI integration
# ----------------------------------------------------------------------


@pytest.fixture
def spec_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point $MOAT_INSTALL_PROVIDERS at a minimal spec file."""
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(_SPEC), encoding="utf-8")
    monkeypatch.setenv("MOAT_INSTALL_PROVIDERS", str(path))
    yield


_SPEC: dict = {
    "providers": [
        {
            "name": "llm-gateway",
            "base_url": "https://llm-gateway.example/api/v1",
            "api_key_env": ENV_GATEWAY,
            "auth": "bearer",
            "default_model": "m1",
            "models": [
                {"id": "m1", "display_name": "M1", "context_window": 1024, "max_output_tokens": 16}
            ],
        }
    ]
}


@pytest.fixture
def mock_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence the /models validation (no network)."""
    monkeypatch.setattr("moat_install.cli.check_provider", lambda *a, **k: (True, "mocked"))


def test_cli_debug_flag_produces_debug_trace(
    spec_env, keys_env, home_base, mock_validate, capsys
) -> None:
    rc = run(["--home-base", str(home_base), "--harness", "pi", "--no-verify", "--debug"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "[debug] running adapter: pi" in err
    assert "[debug] created:" in err  # file creation is traced
    assert "[debug] env file:" in err  # env-file write is traced
    assert "[debug] rc file" in err  # rc-file write is traced


def test_cli_without_debug_flag_emits_no_debug_trace(
    spec_env, keys_env, home_base, mock_validate, capsys
) -> None:
    rc = run(["--home-base", str(home_base), "--harness", "pi", "--no-verify"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "[debug]" not in err


# ----------------------------------------------------------------------
# Debug traces of writes and connections must never leak secrets
# ----------------------------------------------------------------------


@pytest.fixture
def capture_pkg_logs(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Let package log records reach caplog for the duration of a test."""
    pkg = logging.getLogger(LOGGER_NAME)
    pkg.propagate = True
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    try:
        yield caplog
    finally:
        pkg.propagate = False


def test_adapter_write_trace_has_no_secret(
    home_base, selections, capture_pkg_logs: pytest.LogCaptureFixture
) -> None:
    report = PiAdapter(home_base, selections).run()
    assert report.ok
    messages = [record.getMessage() for record in capture_pkg_logs.records]
    assert any("created" in m and "models.json" in m for m in messages)
    assert KEY_GATEWAY not in capture_pkg_logs.text


def test_httpcheck_trace_has_no_secret(
    registry, capture_pkg_logs: pytest.LogCaptureFixture
) -> None:
    profile = registry.get("llm-gateway")

    class FakeResponse:
        status = 200

        def read(self) -> bytes:
            return json.dumps({"data": [{"id": "m1"}]}).encode()

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

    ok, _detail = check_provider(profile, KEY_GATEWAY, urlopen_fn=lambda *a, **k: FakeResponse())
    assert ok
    messages = [record.getMessage() for record in capture_pkg_logs.records]
    assert any(f"GET {profile.base_url}/models" in m for m in messages)
    # The request line logs the auth *style*, never the key or headers.
    assert any("auth=bearer" in m for m in messages)
    assert "Bearer" not in capture_pkg_logs.text
    assert KEY_GATEWAY not in capture_pkg_logs.text


def test_httpcheck_http_error_is_traced(
    registry, capture_pkg_logs: pytest.LogCaptureFixture
) -> None:
    profile = registry.get("llm-gateway")

    def fake_urlopen(request: Any, timeout: Any = None) -> Any:
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "not found",
            email.message.Message(),
            io.BytesIO(b""),
        )

    ok, _detail = check_provider(profile, KEY_GATEWAY, urlopen_fn=fake_urlopen)
    assert not ok
    messages = [record.getMessage() for record in capture_pkg_logs.records]
    assert any("HTTP 404" in m for m in messages)
    assert KEY_GATEWAY not in capture_pkg_logs.text
