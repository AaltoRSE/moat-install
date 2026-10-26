"""Cline adapter — ``<home-base>/.cline/data/settings/providers.json``.

RESEARCH findings (from https://docs.cline.bot/getting-started/config and
/provider-config/openai-compatible, cross-checked against the installed Cline
CLI core, node_modules/@cline/core/dist/index.js and @cline/llms):

* Cline configuration lives in ``~/.cline/`` (global; shared across the IDE
  extension, CLI, and SDK). Provider settings are stored in
  ``~/.cline/data/settings/providers.json`` — "API keys and provider
  configuration". ``CLINE_DIR``/``CLINE_DATA_DIR`` can relocate these; we do
  not set them. (The old VS Code ``state.vscdb`` globalState flow is legacy —
  current Cline core migrates globalState *into* this JSON file.)
* Document schema (zod, from the core dist)::

      {
        "version": 1,
        "lastUsedProvider": "<instance key>",        # optional
        "providers": {
          "<instance key>": {
            "settings": {
              "provider": "openai-compatible",       # ^[a-z0-9][a-z0-9-]*$
              "baseUrl": "https://...",              # must be a valid URL
              "model": "<model id>",
              "protocol": "openai-chat",
              "client": "openai-compatible",
              "apiKey": "...",                       # optional, literal
              "headers": {"Name": "value"}           # optional, literal
            },
            "updatedAt": "<ISO datetime>",           # required
            "tokenSource": "manual"
          }
        }
      }

* The built-in ``openai-compatible`` provider declares
  ``apiKeyEnv: ["OPENAI_API_KEY"]`` and ``apiKeyOptional: true`` — when no
  literal ``apiKey`` is stored it is resolved from the ``OPENAI_API_KEY``
  environment variable at runtime. Hence env-ref mode for the gateway: the
  managed env file exports ``OPENAI_API_KEY="$<provider key env>"`` via
  :meth:`Adapter.env_aliases`.
* Unlike most harnesses, Cline supports multiple provider *instances*; both
  selected providers are written as separate instances.
* Header-auth providers (e.g. Azure APIM): the endpoint authenticates via the
  custom ``auth_header`` header;
  Cline settings carry literal ``headers`` (no env interpolation), and the
  OpenAI-compatible Azure flow expects a base URL ending in
  ``/deployments/<deployment>``. So the header-auth instance stores the literal
  key in ``headers`` — plaintext-secret warning, file chmod 0600 (matching the
  core's own writer, which chmods 0600).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from ..providers import AUTH_BEARER, AUTH_HEADER, ProviderProfile
from . import _base
from ._base import AdapterReport


def _instance_key(profile: ProviderProfile) -> str:
    """Stable, moat-owned instance key (never clobbers user instances)."""
    return f"moat-{profile.name}"


class ClineAdapter(_base.Adapter):
    """Cline harness (see module docstring for research findings)."""

    name = "cline"
    label = "cline"

    @classmethod
    def env_aliases(cls, default_profile: ProviderProfile) -> Dict[str, str]:
        """The openai-compatible provider resolves OPENAI_API_KEY from env."""
        return {"OPENAI_API_KEY": "$" + default_profile.api_key_env}

    def run(self) -> AdapterReport:
        report = AdapterReport(harness=self.name, key_mode="env-ref")
        try:
            path = self.home_base / ".cline" / "data" / "settings" / "providers.json"
            self.dbg(f"config path: {path}")
            doc: Dict[str, Any] = self._load_json(path) if path.exists() else {}

            if doc.get("version") != 1:
                doc["version"] = 1
            providers = doc.get("providers")
            if not isinstance(providers, dict):
                providers = {}
                doc["providers"] = providers

            now = (
                datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
            literal_key = False
            for sel in self.selections:
                profile = sel.profile
                key = _instance_key(profile)
                existing = providers.get(key)
                existed = isinstance(existing, dict)
                settings = self._build_settings(sel)
                if profile.auth == AUTH_HEADER:
                    literal_key = True
                if existed and existing.get("settings") == settings:
                    # Settings unchanged: preserve updatedAt/tokenSource so the
                    # file stays byte-identical on re-runs (idempotency).
                    self.dbg(f"instance '{key}' settings unchanged; preserving updatedAt")
                    report.provider_status[profile.name] = "no-change"
                    continue
                providers[key] = {
                    "settings": settings,
                    "updatedAt": now,
                    "tokenSource": "manual",
                }
                report.provider_status[profile.name] = "updated" if existed else "created"

            default_key = _instance_key(self.default_selection.profile)
            doc["lastUsedProvider"] = default_key

            if literal_key:
                report.key_mode = (
                    "mixed"
                    if any(s.profile.auth == AUTH_BEARER for s in self.selections)
                    else "literal"
                )
                header_details = ", ".join(
                    f"{s.profile.auth_header} header for '{s.profile.name}'"
                    for s in self.selections
                    if s.profile.auth == AUTH_HEADER
                )
                report.warnings.append(
                    "providers.json contains a plaintext API key "
                    f"({header_details}); the file is "
                    "mode 0600 and the key is masked in all logs"
                )

            content = self._dump_json(doc)
            self._write_text(path, content, report, parse_check=self._parse_check)
            if not self.dry_run and path.exists():
                self.dbg(f"chmod 0600 (file contains provider secrets): {path}")
                path.chmod(0o600)  # provider secrets file (core uses 0600 too)
        except Exception as exc:
            report.error = str(exc)
            report.status = "failed"
        self._finalize_status(report)
        return report

    # ------------------------------------------------------------------

    @staticmethod
    def _build_settings(sel: Any) -> Dict[str, Any]:
        profile = sel.profile
        settings: Dict[str, Any] = {
            "provider": "openai-compatible",
            "protocol": "openai-chat",
            "client": "openai-compatible",
            "model": profile.default_model,
        }
        if profile.auth == AUTH_BEARER:
            # key resolved from OPENAI_API_KEY at runtime (apiKeyEnv)
            settings["baseUrl"] = profile.base_url
        elif profile.auth == AUTH_HEADER:
            # Header-auth (Azure-APIM-style): deployments-style base URL +
            # literal key in headers (no env interpolation in cline settings)
            settings["baseUrl"] = f"{profile.base_url}/deployments/{profile.default_model}"
            settings["headers"] = {profile.auth_header: sel.key}
        return settings

    @staticmethod
    def _parse_check(raw_path: str) -> None:
        import json

        with open(raw_path, encoding="utf-8") as fh:
            json.load(fh)
