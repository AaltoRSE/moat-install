"""Endpoint validation and model discovery: ``GET {base_url}/models``.

Uses only the stdlib (urllib). Runs once per provider (never per harness) on
the host; containers inherit host networking, so a passing check implies
in-container reachability. Network reachability requirements are
deployment-specific and live with the provider specification.

Two consumers share the fetch: :func:`check_provider` validates an endpoint
for providers with an explicit catalog, and :func:`discover_models` builds a
catalog at run time for providers whose specification omits ``models`` (a
successful discovery also validates the endpoint).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .home import mask_key
from .logging_setup import get_logger
from .providers import AUTH_BEARER, AUTH_HEADER, NETWORK_HINT, ModelInfo, ProviderProfile

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
    model_list, error = _fetch_model_list(profile, key, timeout, urlopen_fn)
    if model_list is None:
        return False, error
    logger.debug("%s: /models validation ok (%d models)", profile.name, len(model_list))
    return (
        True,
        f"{profile.name}: /models OK ({len(model_list)} models)",
    )


def discover_models(
    profile: ProviderProfile,
    key: str,
    timeout: float = DEFAULT_TIMEOUT_S,
    urlopen_fn: Optional[Any] = None,
) -> Tuple[bool, str, Tuple[ModelInfo, ...], str]:
    """Query ``GET {base_url}/models`` and build a catalog for ``profile``.

    Used for providers whose specification omits ``models``: the catalog is
    discovered from the endpoint at run time instead of being declared in the
    specification file. Returns ``(ok, detail, models, default_model)``;
    ``models``/``default_model`` are empty on failure.

    The default model is the specification's ``default_model`` when one was
    given (it must then be offered by the endpoint), otherwise the first
    discovered model. A successful discovery also proves the endpoint works
    with the key, so callers can skip the separate :func:`check_provider`
    validation for that provider (one ``/models`` call per provider, never
    two). ``detail`` never contains the key.
    """
    model_list, error = _fetch_model_list(profile, key, timeout, urlopen_fn)
    if model_list is None:
        return False, error, (), ""

    models: List[ModelInfo] = []
    seen_ids = set()
    unusable = 0
    for entry in model_list:
        model = _model_from_entry(entry)
        if model is None:
            unusable += 1
            continue
        if model.id in seen_ids:
            continue  # deduplicate (some gateways repeat entries)
        seen_ids.add(model.id)
        models.append(model)
    if unusable:
        logger.debug(
            "%s: skipped %d unusable /models entr%s (no 'id' field)",
            profile.name,
            unusable,
            "y" if unusable == 1 else "ies",
        )
    if not models:
        return (
            False,
            f"{profile.name}: no usable model entries in the /models response "
            "(entries need an 'id' field)",
            (),
            "",
        )

    if profile.default_model:
        if profile.default_model not in seen_ids:
            available = ", ".join(m.id for m in models[:10])
            more = "" if len(models) <= 10 else f" … and {len(models) - 10} more"
            return (
                False,
                f"{profile.name}: default_model {profile.default_model!r} is not offered "
                f"by {profile.base_url.rstrip('/')}/models (available: {available}{more})",
                (),
                "",
            )
        default_model = profile.default_model
    else:
        default_model = models[0].id

    detail = (
        f"{profile.name}: {len(models)} model(s) discovered via /models (default: {default_model})"
    )
    logger.debug("%s: discovered models: %s", profile.name, ", ".join(m.id for m in models))
    return True, detail, tuple(models), default_model


def _fetch_model_list(
    profile: ProviderProfile,
    key: str,
    timeout: float,
    urlopen_fn: Optional[Any],
) -> Tuple[Optional[list], str]:
    """``GET {base_url}/models`` → ``(model_list, "")`` or ``(None, detail)``.

    Shared by endpoint validation (:func:`check_provider`) and model discovery
    (:func:`discover_models`). Connection-level failures produce a masked,
    human-readable ``detail``.
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
        return None, (
            f"{profile.name}: /models returned HTTP {exc.code} {detail}".rstrip() + f" ({hint})"
        )
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        logger.debug("GET %s failed: %s", url, reason)
        return None, (f"{profile.name}: cannot reach {url} ({reason}) ({NETWORK_HINT})")
    except TimeoutError:
        logger.debug("GET %s timed out after %ss", url, timeout)
        return None, f"{profile.name}: request to {url} timed out ({NETWORK_HINT})"

    if status != 200:
        return None, f"{profile.name}: /models returned HTTP {status}"
    # Must parse as JSON whose model list is either a top-level array, a
    # "data" array, or a "models" list — enough to prove a genuine
    # OpenAI-compatible catalog.
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, f"{profile.name}: /models returned non-JSON content"
    if isinstance(payload, list):
        model_list: Optional[list] = payload
    elif isinstance(payload, dict):
        model_list = payload.get("data") or payload.get("models")
    else:
        return None, f"{profile.name}: /models response is not a JSON object or array"
    if not isinstance(model_list, list):
        return None, f"{profile.name}: /models response has no model list"
    return model_list, ""


def _model_from_entry(entry: Any) -> Optional[ModelInfo]:
    """Build a :class:`ModelInfo` from one /models entry, or ``None``.

    An entry is usable when it carries a model id (``id``, with ``model`` or
    ``name`` accepted as fallbacks). Limit metadata is taken when the gateway
    provides it (several field spellings are accepted) and defaults to
    ``0`` — meaning *unknown* — otherwise; adapters omit unknown metadata
    rather than writing zeroes (harness defaults apply instead).
    """
    if not isinstance(entry, dict):
        return None
    model_id = entry.get("id") or entry.get("model") or entry.get("name")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    display_name = entry.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = model_id
    return ModelInfo(
        id=model_id,
        display_name=display_name,
        context_window=_first_positive_int(
            entry, ("context_window", "context_length", "max_model_len", "max_context_length")
        ),
        max_output_tokens=_first_positive_int(
            entry, ("max_output_tokens", "max_completion_tokens", "max_output")
        ),
    )


def _first_positive_int(entry: Any, keys: Tuple[str, ...]) -> int:
    """First positive integer among ``entry[key]`` for ``keys``; 0 if none."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return 0


def _safe_body(body: str) -> str:
    """Truncate an error body and scrub anything that looks like the key."""
    text = body.strip().replace("\n", " ")
    if len(text) > 200:
        text = text[:200] + "…"
    # Defensive masking: keys may leak into proxy error echoes.
    return mask_key(text) if _looks_secretish(text) else text


def _looks_secretish(text: str) -> bool:
    return "api" in text.lower() and "key" in text.lower() and "invalid" not in text.lower()
