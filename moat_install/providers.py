"""Provider profiles: loaded from a JSON provider specification file.

Provider facts (base URLs, auth style, model catalogs, default models) are
**not** hard-coded in the code base. They are read from a JSON specification
file located via, in order of precedence:

1. ``--provider-specification PATH`` (CLI flag)
2. the ``MOAT_INSTALL_PROVIDERS`` environment variable

Specification file format (JSON)::

    {
      "providers": [
        {
          "name": "llm-gateway",
          "display_name": "Example LLM Gateway",
          "base_url": "https://llm-gateway.example/api/v1",
          "api_key_env": "LLM_GATEWAY_KEY",
          "auth": "bearer",
          "default_model": "model-a",
          "models": [
            {"id": "model-a", "display_name": "Model A",
             "context_window": 1048576, "max_output_tokens": 65536}
          ],
          "pi_api": "openai-completions",
          "opencode_npm": "@ai-sdk/openai-compatible",
          "codex_disable_response_storage": false
        }
      ]
    }

The order of the ``providers`` list is the default selection order: the first
entry becomes each harness's default provider (see the default-model rule in
AGENTS.md §5). See ``examples/`` for ready-made specification files.

Validation is strict: unknown keys (top level, provider, model) are rejected,
required fields must be present, and cross-field invariants (default_model in
models, auth_header for header auth, unique names) are enforced. All errors
raise :class:`SpecificationError` with a message identifying the offending
file and key.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .logging_setup import get_logger

#: Specification loading is traced at DEBUG level (see ``logging_setup.py``).
logger = get_logger(__name__)

# Auth styles
AUTH_BEARER = "bearer"  # key sent as ``Authorization: Bearer <key>``
AUTH_HEADER = "header"  # key sent via a custom header (see ``auth_header``)

#: Environment variable holding the path to the provider specification file.
SPEC_ENV_VAR = "MOAT_INSTALL_PROVIDERS"

#: Provider names become TOML/JSON/YAML keys in harness configs — keep them
#: conservative.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_TOP_LEVEL_KEYS = frozenset({"providers"})
_PROVIDER_KEYS = frozenset(
    {
        "name",
        "display_name",
        "base_url",
        "api_key_env",
        "auth",
        "auth_header",
        "models",
        "default_model",
        "pi_api",
        "opencode_npm",
        "codex_disable_response_storage",
    }
)
_MODEL_KEYS = frozenset({"id", "display_name", "context_window", "max_output_tokens"})


class SpecificationError(ValueError):
    """Raised when a provider specification file is missing or invalid."""


@dataclass(frozen=True)
class ModelInfo:
    """One model in a provider's catalog, with the metadata harnesses need."""

    id: str
    display_name: str
    context_window: int
    max_output_tokens: int


@dataclass(frozen=True)
class ProviderProfile:
    """Declarative description of one institutional provider.

    Attributes mirror what the adapters need:

    * ``name`` — registry key used as the provider id in every harness config.
    * ``base_url`` — API root; ``GET {base_url}/models`` must validate.
    * ``api_key_env`` — environment variable holding the API key.
    * ``auth`` — ``AUTH_BEARER`` or ``AUTH_HEADER``.
    * ``auth_header`` — custom header name when ``auth == AUTH_HEADER``.
    * ``models`` — ordered model catalog; ``default_model`` must be a member.
    * ``pi_api`` — pi/oh-my-pi ``api`` field for this endpoint.
    * ``opencode_npm`` — opencode ``provider.<id>.npm`` package for this API.
    * ``codex_disable_response_storage`` — top-level Codex setting implied when
      this provider is the harness default (for gateways that reject stored
      responses).
    """

    name: str
    base_url: str
    api_key_env: str
    auth: str
    models: Tuple[ModelInfo, ...]
    default_model: str
    auth_header: str = ""
    pi_api: str = "openai-completions"
    opencode_npm: str = "@ai-sdk/openai-compatible"
    codex_disable_response_storage: bool = False
    display_name: str = ""

    def __post_init__(self) -> None:
        model_ids = {m.id for m in self.models}
        if self.default_model not in model_ids:
            raise ValueError(
                f"provider {self.name!r}: default_model {self.default_model!r} "
                f"is not in models {sorted(model_ids)}"
            )
        if self.auth == AUTH_HEADER and not self.auth_header:
            raise ValueError(f"provider {self.name!r}: auth=header requires auth_header")
        if not self.display_name:
            # display_name is derived, stored via object.__setattr__ (frozen dataclass)
            object.__setattr__(self, "display_name", self.name)

    def model(self, model_id: str) -> ModelInfo:
        """Return catalog metadata for ``model_id`` (raises KeyError if absent)."""
        for m in self.models:
            if m.id == model_id:
                return m
        raise KeyError(f"provider {self.name!r} has no model {model_id!r}")


@dataclass(frozen=True)
class ProviderRegistry:
    """The parsed provider specification: profiles plus default selection order."""

    profiles: Dict[str, ProviderProfile]
    order: Tuple[str, ...]  # file order; first entry is the global default

    def get(self, name: str) -> ProviderProfile:
        """Return the profile for ``name`` (raises KeyError if absent)."""
        return self.profiles[name]

    def names(self) -> Tuple[str, ...]:
        """All provider names in file order."""
        return self.order


def load_provider_specification(path: Path) -> ProviderRegistry:
    """Load and validate a provider specification JSON file.

    Raises :class:`SpecificationError` with a precise message on any problem
    (missing file, bad JSON, schema violation, cross-field invariant failure).
    """
    path = Path(path)
    logger.debug("loading provider specification: %s", path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecificationError(f"cannot read provider specification {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecificationError(f"{path}: invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise SpecificationError(f"{path}: top level must be a JSON object")
    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise SpecificationError(
            f"{path}: unknown top-level key(s) {', '.join(unknown)}; "
            f"valid keys: {', '.join(sorted(_TOP_LEVEL_KEYS))}"
        )
    raw_providers = data.get("providers")
    if raw_providers is None:
        raise SpecificationError(f"{path}: missing required key 'providers'")
    if not isinstance(raw_providers, list) or not raw_providers:
        raise SpecificationError(
            f"{path}: 'providers' must be a non-empty JSON array of provider objects"
        )

    profiles: Dict[str, ProviderProfile] = {}
    order: List[str] = []
    for index, raw in enumerate(raw_providers):
        profile = _parse_provider(path, index, raw)
        if profile.name in profiles:
            raise SpecificationError(
                f"{path}: provider #{index + 1}: duplicate name {profile.name!r}"
            )
        profiles[profile.name] = profile
        order.append(profile.name)
    logger.debug("loaded %d provider(s) from %s: %s", len(order), path, ", ".join(order))
    return ProviderRegistry(profiles=profiles, order=tuple(order))


def _parse_provider(path: Path, index: int, raw: Any) -> ProviderProfile:
    """Parse and validate one provider object from the specification."""
    where = f"{path}: provider #{index + 1}"
    if not isinstance(raw, dict):
        raise SpecificationError(f"{where}: must be a JSON object")
    unknown = sorted(set(raw) - _PROVIDER_KEYS)
    if unknown:
        raise SpecificationError(
            f"{where}: unknown key(s) {', '.join(unknown)}; "
            f"valid keys: {', '.join(sorted(_PROVIDER_KEYS))}"
        )

    missing = [
        k
        for k in ("name", "base_url", "api_key_env", "auth", "models", "default_model")
        if k not in raw
    ]
    if missing:
        raise SpecificationError(f"{where}: missing required key(s) {', '.join(missing)}")

    name = raw["name"]
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise SpecificationError(f"{where}: 'name' must match {_NAME_RE.pattern!r} (got {name!r})")
    where = f"{path}: provider {name!r}"

    for key in ("base_url", "api_key_env", "auth"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise SpecificationError(f"{where}: {key!r} must be a non-empty string")

    auth = raw["auth"]
    if auth not in (AUTH_BEARER, AUTH_HEADER):
        raise SpecificationError(
            f"{where}: 'auth' must be {AUTH_BEARER!r} or {AUTH_HEADER!r} (got {auth!r})"
        )
    auth_header = raw.get("auth_header", "")
    if auth == AUTH_HEADER and (not isinstance(auth_header, str) or not auth_header.strip()):
        raise SpecificationError(f"{where}: 'auth_header' is required when auth is {AUTH_HEADER!r}")

    models_raw = raw["models"]
    if not isinstance(models_raw, list) or not models_raw:
        raise SpecificationError(f"{where}: 'models' must be a non-empty JSON array")
    models = tuple(_parse_model(path, name, i, m) for i, m in enumerate(models_raw))

    default_model = raw["default_model"]
    if not isinstance(default_model, str) or not default_model:
        raise SpecificationError(f"{where}: 'default_model' must be a non-empty string")
    model_ids = {m.id for m in models}
    if default_model not in model_ids:
        raise SpecificationError(
            f"{where}: 'default_model' {default_model!r} is not in models {sorted(model_ids)}"
        )

    for key in ("pi_api", "opencode_npm"):
        if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
            raise SpecificationError(f"{where}: {key!r} must be a non-empty string")
    drs = raw.get("codex_disable_response_storage", False)
    if not isinstance(drs, bool):
        raise SpecificationError(f"{where}: 'codex_disable_response_storage' must be a boolean")

    try:
        return ProviderProfile(
            name=name,
            base_url=raw["base_url"],
            api_key_env=raw["api_key_env"],
            auth=auth,
            auth_header=auth_header,
            models=models,
            default_model=default_model,
            pi_api=raw.get("pi_api", "openai-completions"),
            opencode_npm=raw.get("opencode_npm", "@ai-sdk/openai-compatible"),
            codex_disable_response_storage=drs,
            display_name=raw.get("display_name", "") or name,
        )
    except ValueError as exc:
        raise SpecificationError(f"{where}: {exc}") from exc


def _parse_model(path: Path, provider_name: str, index: int, raw: Any) -> ModelInfo:
    """Parse and validate one model object from the specification."""
    where = f"{path}: provider {provider_name!r}, model #{index + 1}"
    if not isinstance(raw, dict):
        raise SpecificationError(f"{where}: must be a JSON object")
    unknown = sorted(set(raw) - _MODEL_KEYS)
    if unknown:
        raise SpecificationError(
            f"{where}: unknown key(s) {', '.join(unknown)}; "
            f"valid keys: {', '.join(sorted(_MODEL_KEYS))}"
        )
    missing = [
        k for k in ("id", "display_name", "context_window", "max_output_tokens") if k not in raw
    ]
    if missing:
        raise SpecificationError(f"{where}: missing required key(s) {', '.join(missing)}")
    for key in ("id", "display_name"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise SpecificationError(f"{where}: {key!r} must be a non-empty string")
    for key in ("context_window", "max_output_tokens"):
        value = raw[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise SpecificationError(f"{where}: {key!r} must be a positive integer (got {value!r})")
    return ModelInfo(
        id=raw["id"],
        display_name=raw["display_name"],
        context_window=raw["context_window"],
        max_output_tokens=raw["max_output_tokens"],
    )


#: Hint shown when endpoint validation fails (reachability requirements are
#: deployment-specific — some gateways live on private networks or behind VPN).
NETWORK_HINT = (
    "Note: the provider endpoint may only be reachable from a specific network or via VPN."
)
