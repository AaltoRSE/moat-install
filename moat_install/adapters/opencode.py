"""OpenCode adapter — ``<home-base>/.config/opencode/opencode.json``.

Config mechanism (verified against the locally installed opencode and its real
config file, plus https://opencode.ai/config.json schema):

* Providers are declared under the top-level ``"provider"`` key (singular —
  the outdated plural ``"providers"`` spelling is not part of the schema).
* Each provider entry: ``npm`` package implementing the API flavour
  (``@ai-sdk/openai-compatible`` for OpenAI-style endpoints,
  ``@ai-sdk/openai`` for Azure-style endpoints), ``name``, ``options``
  (``baseURL``, ``apiKey``, optional ``headers``), and a ``models`` map with
  per-model ``name``/``limit`` metadata.
* API-key references use ``{env:VAR}`` interpolation, and the provider may
  also list required variables in ``options``/``env``. We use ``{env:VAR}``
  (env-ref mode) so no key value is persisted here.
* Header-auth profiles write ``options.apiKey`` as an empty string and put the
  credential into ``options.headers`` (``{env:VAR}``). Verified against the
  installed ``@ai-sdk/openai-compatible`` provider source: the SDK emits
  ``Authorization: Bearer <apiKey>`` only when ``apiKey`` is truthy and merges
  ``options.headers`` afterwards, so an empty ``apiKey`` yields exactly the
  credential header and no bogus ``Bearer`` header.
* Top-level ``"model"`` (``<provider>/<model>``) selects the default model and
  is treated as moat-managed (set from the first selected provider).

Container-runtime key delivery: opencode interpolates ``{env:VAR}`` at
startup from its process environment — provided by the guarded rc block in
the home base and/or host env passthrough.
"""

from __future__ import annotations

from typing import Any, Dict

from ..providers import AUTH_BEARER, AUTH_HEADER
from . import _base
from ._base import AdapterReport, ProviderSelection


class OpencodeAdapter(_base.Adapter):
    """OpenCode harness (see module docstring)."""

    name = "opencode"
    label = "opencode"

    def run(self) -> AdapterReport:
        report = AdapterReport(harness=self.name, key_mode="env-ref")
        try:
            path = self.home_base / ".config" / "opencode" / "opencode.json"
            self.dbg(f"config path: {path}")
            doc: Dict[str, Any] = self._load_json(path) if path.exists() else {}
            # $schema is opencode's own key — set it only when absent so we
            # never stomp a user-pinned schema version.
            doc.setdefault("$schema", "https://opencode.ai/config.json")

            providers = doc.get("provider")
            if not isinstance(providers, dict):
                providers = {}
                doc["provider"] = providers

            for sel in self.selections:
                self._merge_provider(providers, sel, report)

            default_sel = self.default_selection
            doc["model"] = f"{default_sel.profile.name}/{default_sel.profile.default_model}"

            content = self._dump_json(doc)
            self._write_text(path, content, report, parse_check=self._parse_check)
        except Exception as exc:
            report.error = str(exc)
            report.status = "failed"
        self._finalize_status(report)
        return report

    # ------------------------------------------------------------------

    def _merge_provider(
        self,
        providers: Dict[str, Any],
        sel: ProviderSelection,
        report: AdapterReport,
    ) -> None:
        profile = sel.profile
        existed = isinstance(providers.get(profile.name), dict)
        entry = providers.get(profile.name)
        if not isinstance(entry, dict):
            entry = {}

        entry["npm"] = profile.opencode_npm
        entry["name"] = profile.display_name
        options = entry.get("options")
        if not isinstance(options, dict):
            options = {}
        options["baseURL"] = profile.base_url
        if profile.auth == AUTH_BEARER:
            options["apiKey"] = "{env:" + profile.api_key_env + "}"
            options.pop("headers", None)
        elif profile.auth == AUTH_HEADER:
            # The credential travels in `headers` ({env:VAR}); the SDK emits a
            # Bearer header only for a truthy apiKey, so an empty value avoids
            # a bogus `Authorization: Bearer …` header (verified against the
            # installed @ai-sdk/openai-compatible source).
            options["apiKey"] = ""
            headers = options.get("headers")
            if not isinstance(headers, dict):
                headers = {}
            headers[profile.auth_header] = "{env:" + profile.api_key_env + "}"
            options["headers"] = headers
        entry["options"] = options

        models = entry.get("models")
        if not isinstance(models, dict):
            models = {}
        for model in profile.models:
            existing = models.get(model.id)
            if not isinstance(existing, dict):
                existing = {}
            existing.setdefault("name", model.display_name)
            existing["limit"] = {
                "context": model.context_window,
                "output": model.max_output_tokens,
            }
            models[model.id] = existing
        entry["models"] = models

        providers[profile.name] = entry
        report.provider_status[profile.name] = "updated" if existed else "created"

    @staticmethod
    def _parse_check(raw_path: str) -> None:
        import json

        with open(raw_path, encoding="utf-8") as fh:
            json.load(fh)
