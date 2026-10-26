"""Endpoint validation: ``GET {base_url}/models`` with the provider key.

Uses only the stdlib (urllib). Runs once per provider (never per harness) on
the host; containers inherit host networking, so a passing check implies
in-container reachability. Network reachability requirements are
deployment-specific and live with the provider specification.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .home import mask_key
from .logging_setup import get_logger
from .providers import AUTH_BEARER, AUTH_HEADER, NETWORK_HINT, ProviderProfile

#: Connection-level debugging goes through ``logging`` (see ``logging_setup.py``):
#: every request logs method/URL/timeout/auth-style and the outcome. Headers are
#: never logged — they carry the API key.
logger = get_logger(__name__)

DEFAULT_TIMEOUT_S = 20.0


def auth_headers(profile: ProviderProfile, key: str) -> Dict[str, str]:
    """Headers that present ``key`` for ``profile`` (bearer or custom header)."""
    if profile.auth == AUTH_BEARER:
        return {"Authorization": f"Bearer {key}"}
    if profile.auth == AUTH_HEADER:
        return {profile.auth_header: key}
    raise ValueError(f"unknown auth style {profile.auth!r}")


def check_provider(
    profile: ProviderProfile,
    key: str,
    timeout: float = DEFAULT_TIMEOUT_S,
    urlopen_fn: Optional[Any] = None,
) -> Tuple[bool, str]:
    """Validate ``GET {base_url}/models`` once for a provider.

    Returns ``(ok, detail)``. ``detail`` is a human-readable message that never
    contains the key (masking is applied defensively to any echoed payload).

    The model list may arrive either as the OpenAI-style envelope
    ``{"data": [...]}`` (or ``{"models": [...]}``) or as a bare top-level
    JSON array — some gateways use the latter.
    """
    url = f"{profile.base_url.rstrip('/')}/models"
    request = urllib.request.Request(url, headers={**auth_headers(profile, key)})
    do_open = urlopen_fn if urlopen_fn is not None else urllib.request.urlopen
    logger.debug("GET %s (timeout=%ss, auth=%s)", url, timeout, profile.auth)
    try:
        with do_open(request, timeout=timeout) as response:  # type: ignore[operator]
            body = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
        logger.debug("GET %s -> HTTP %s (%d bytes)", url, status, len(body))
    except urllib.error.HTTPError as exc:
        logger.debug("GET %s -> HTTP %s", url, exc.code)
        detail = _safe_body(exc.read().decode("utf-8", errors="replace")) if exc.fp else ""
        hint = NETWORK_HINT
        if exc.code == 404:
            hint += (
                "; a 404 can also mean the gateway hides routes from invalid "
                "keys (check the key) or that this gateway has no /models route "
                "(--no-verify skips this check)"
            )
        return False, (
            f"{profile.name}: /models returned HTTP {exc.code} {detail}".rstrip() + f" ({hint})"
        )
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        logger.debug("GET %s failed: %s", url, reason)
        return False, (f"{profile.name}: cannot reach {url} ({reason}) ({NETWORK_HINT})")
    except TimeoutError:
        logger.debug("GET %s timed out after %ss", url, timeout)
        return False, f"{profile.name}: request to {url} timed out ({NETWORK_HINT})"

    if status != 200:
        return False, f"{profile.name}: /models returned HTTP {status}"
    # Must parse as JSON whose model list is either a top-level array, a
    # "data" array, or a "models" list — enough to prove a genuine
    # OpenAI-compatible catalog.
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False, f"{profile.name}: /models returned non-JSON content"
    if isinstance(payload, list):
        model_list = payload
    elif isinstance(payload, dict):
        model_list = payload.get("data") or payload.get("models")
    else:
        return False, f"{profile.name}: /models response is not a JSON object or array"
    if not isinstance(model_list, list):
        return False, f"{profile.name}: /models response has no model list"
    logger.debug("%s: /models validation ok (%d models)", profile.name, len(model_list))
    return (
        True,
        f"{profile.name}: /models OK ({len(model_list)} models)",
    )


def _safe_body(body: str) -> str:
    """Truncate an error body and scrub anything that looks like the key."""
    text = body.strip().replace("\n", " ")
    if len(text) > 200:
        text = text[:200] + "…"
    # Defensive masking: keys may leak into proxy error echoes.
    return mask_key(text) if _looks_secretish(text) else text


def _looks_secretish(text: str) -> bool:
    return "api" in text.lower() and "key" in text.lower() and "invalid" not in text.lower()
