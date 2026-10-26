"""Codex adapter — ``<home-base>/.codex/config.toml`` plus a derived model catalog.

Config mechanism (verified against the installed codex-cli 0.163.0 and the
legacy reference configs):

* ``~/.codex/config.toml`` declares providers under ``[model_providers.<id>]``
  with ``name``, ``base_url``, ``wire_api`` (``"responses"`` for both current
  profiles) and key delivery via ``env_key`` (bearer) or ``env_http_headers``
  (custom header). Top-level ``model`` + ``model_provider`` select the default.
* ``model_catalog_json = "<file>"`` points at a JSON model catalog that must
  live next to ``config.toml``. We derive it at write time from the selected
  providers' model lists (never shipped as a static file) and reference it by
  bare filename — the path rule forbids serializing host-side absolute paths.
* Conservative defaults on creation: ``approval_policy = "on-request"``
  (read-only-ish on shared HPC). Existing values of
  unrelated settings (and of approval_policy itself) are preserved.
* When the default provider's specification sets
  ``codex_disable_response_storage``, the top-level ``disable_response_storage``
  is set to ``true`` if absent (some gateways reject stored responses).

Container-runtime key delivery: Codex resolves ``env_key``/``env_http_headers``
variable *names* from its process environment at request time — env-ref mode;
the values arrive via the guarded rc block in the home base (sourced when
launched via a shell) and/or host env passthrough.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from ..providers import AUTH_BEARER, AUTH_HEADER
from . import _base
from ._base import AdapterReport, ProviderSelection

CATALOG_FILE_NAME = "moat-models.json"


class CodexAdapter(_base.Adapter):
    """Codex harness (see module docstring)."""

    name = "codex"
    label = "codex"

    def run(self) -> AdapterReport:
        report = AdapterReport(harness=self.name, key_mode="env-ref")
        try:
            config_dir = self.home_base / ".codex"
            config_path = config_dir / "config.toml"
            catalog_path = config_dir / CATALOG_FILE_NAME
            self.dbg(f"config path: {config_path}")
            self.dbg(f"catalog path (derived): {catalog_path}")

            doc = self._load_toml(config_path) if config_path.exists() else None
            created = doc is None
            if doc is None:
                import tomlkit

                doc = tomlkit.document()

            default_sel = self.default_selection

            # Conservative defaults, only ever set if absent.
            if "approval_policy" not in doc:
                doc["approval_policy"] = "on-request"
            if (
                default_sel.profile.codex_disable_response_storage
                and "disable_response_storage" not in doc
            ):
                doc["disable_response_storage"] = True

            # Default model + provider: first selection in order.
            doc["model"] = default_sel.profile.default_model
            doc["model_provider"] = default_sel.profile.name
            doc["model_catalog_json"] = CATALOG_FILE_NAME

            model_providers = self._ensure_table(doc, "model_providers")
            for sel in self.selections:
                self._merge_provider(model_providers, sel, report)

            config_content = self._dump_toml(doc)
            self._write_text(
                config_path,
                config_content,
                report,
                parse_check=self._parse_toml_check,
            )
            if not report.error:
                self._write_catalog(catalog_path, report)

            if created:
                report.manual_notes.append(
                    "codex config.toml was created with approval_policy = "
                    '"on-request" (conservative default for shared HPC).'
                )
        except Exception as exc:
            report.error = str(exc)
            report.status = "failed"
        self._finalize_status(report)
        return report

    # ------------------------------------------------------------------

    def _merge_provider(
        self,
        model_providers: Any,
        sel: ProviderSelection,
        report: AdapterReport,
    ) -> None:
        profile = sel.profile
        existed = profile.name in model_providers
        entry = model_providers.get(profile.name)
        if not isinstance(entry, dict):
            import tomlkit

            entry = tomlkit.table()
        entry["name"] = profile.display_name
        entry["base_url"] = profile.base_url
        entry["wire_api"] = "responses"
        if profile.auth == AUTH_BEARER:
            entry["env_key"] = profile.api_key_env
            entry.pop("env_http_headers", None)
        elif profile.auth == AUTH_HEADER:
            import tomlkit

            headers = tomlkit.inline_table()
            headers[profile.auth_header] = profile.api_key_env
            entry["env_http_headers"] = headers
            entry.pop("env_key", None)
        model_providers[profile.name] = entry
        report.provider_status[profile.name] = "updated" if existed else "created"

    def _write_catalog(self, catalog_path: Path, report: AdapterReport) -> None:
        """Derive the model catalog JSON from the selected providers."""
        models: List[Dict[str, Any]] = []
        for sel in self.selections:
            profile = sel.profile
            for model in profile.models:
                models.append(
                    {
                        "slug": model.id,
                        "display_name": model.display_name,
                        "name": model.display_name,
                        "model": model.id,
                        "provider": profile.name,
                        "context_window": model.context_window,
                        "truncation_policy": {
                            "mode": "tokens",
                            "limit": min(model.context_window, 500_000),
                        },
                        "shell_type": "shell_command",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 0,
                        "base_instructions": "You are a helpful coding assistant.",
                        "supports_tools": True,
                        "supports_parallel_tool_calls": False,
                        "experimental_supported_tools": [],
                        "supports_reasoning_summaries": True,
                        "support_verbosity": False,
                        "supported_reasoning_levels": [],
                    }
                )
        content = self._dump_json({"models": models})
        self._write_text(catalog_path, content, report, parse_check=self._parse_json_check)

    @staticmethod
    def _ensure_table(doc: Any, key: str) -> Any:
        import tomlkit

        value = doc.get(key)
        if value is None or not isinstance(value, dict):
            value = tomlkit.table()
            doc[key] = value
        return value

    @staticmethod
    def _parse_toml_check(raw_path: str) -> None:
        import tomlkit

        with open(raw_path, encoding="utf-8") as fh:
            tomlkit.load(fh)

    @staticmethod
    def _parse_json_check(raw_path: str) -> None:
        import json

        with open(raw_path, encoding="utf-8") as fh:
            json.load(fh)
