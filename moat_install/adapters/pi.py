"""pi adapter — configures pi via its agent-directory ``models.json``.

RESEARCH findings (verified offline against the installed pi distribution and
its bundled docs):

* pi's user-level configuration lives in the *agent directory*, which defaults
  to ``~/.pi/agent`` (the ``PI_CODING_AGENT_DIR`` environment variable can move
  it; we do not set it). Documentation:
  ``pi-coding-agent/docs/configuration.md`` ("The agent directory ... defaults
  to ``~/.pi/agent``").
* Compatible endpoints are declared in ``<agent-dir>/models.json`` with a
  top-level ``providers`` map (``models.md`` § "Configure a compatible
  endpoint"). Each provider entry supports ``baseUrl``, ``api`` (one of
  ``openai-completions``, ``openai-responses``, ...), ``apiKey`` (literal,
  ``$NAME``/``${NAME}`` environment interpolation, or ``!command``), optional
  ``headers``, and a ``models`` list.
* Therefore: ``<home-base>/.pi/agent/models.json``.
* Header-auth profiles carry the credential in ``headers`` (``$NAME``
  interpolation) and write ``apiKey`` as an empty string. Verified against the
  installed pi distribution (bundled models-config code): an entry with only
  ``baseUrl`` + ``headers`` + ``models`` passes provider validation, and the
  auth check accepts a *present* ``apiKey`` with no ``$ENV`` references — an
  empty string resolves to ``""`` and never throws — while an absent ``apiKey``
  would leave the provider unconfigured ("No API key for …").

Container-runtime key delivery: pi resolves ``$VAR``-style ``apiKey`` values
from its own process environment, so the key must be exported before launch —
satisfied by the guarded rc block in the home base (sourced when the harness
is launched via a shell) and/or by host environment passthrough. pi supports
env-var indirection, so key_mode is always "env-ref" and no key value is ever
written to ``models.json``.

Merge behaviour: only ``providers.<profile-name>`` entries listed in the
selection are added/updated in place; all other top-level keys (settings-like
``modelOverrides``, unrelated providers) are preserved.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..merge import ensure_table
from ..providers import AUTH_BEARER, AUTH_HEADER
from . import _base
from ._base import AdapterReport, ProviderSelection


class PiFamilyAdapter(_base.Adapter):
    """Shared implementation for pi-compatible harnesses (pi, oh-my-pi)."""

    #: Agent-directory path relative to the home base (e.g. ".pi/agent").
    agent_dir: str = ".pi/agent"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not self.name:
            raise ValueError(f"{type(self).__name__} must set a harness name")

    def run(self) -> AdapterReport:
        report = AdapterReport(harness=self.name, key_mode="env-ref")
        try:
            path = self.home_base / self.agent_dir / "models.json"
            self.dbg(f"config path: {path}")
            doc: Dict[str, Any] = self._load_json(path) if path.exists() else {}
            providers = ensure_table(doc, "providers")

            for sel in self.selections:
                self._merge_provider(providers, sel, report)

            content = self._dump_json(doc)
            self._write_text(
                path,
                content,
                report,
                parse_check=self._parse_check,
            )
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
        # Own fields are set deterministically; unknown sibling keys inside the
        # provider entry (e.g. "compat") are preserved.
        entry["baseUrl"] = profile.base_url
        entry["api"] = profile.pi_api
        if profile.auth == AUTH_BEARER:
            entry["apiKey"] = "$" + profile.api_key_env
        elif profile.auth == AUTH_HEADER:
            # The credential travels in `headers` ($ENV interpolation); apiKey
            # must merely be present for pi's auth check — an empty value is
            # valid (verified against the installed pi distribution), so no
            # placeholder text is written here.
            entry["apiKey"] = ""
            raw_headers = entry.get("headers")
            headers: Dict[str, Any] = dict(raw_headers) if isinstance(raw_headers, dict) else {}
            headers[profile.auth_header] = "$" + profile.api_key_env
            entry["headers"] = headers
        providers[profile.name] = entry

        # Merge model list by id: update matching entries in place, append new
        # ones in catalog order, preserve unknown sibling fields per model.
        existing_models: List[Dict[str, Any]] = [
            m for m in entry.get("models", []) if isinstance(m, dict)
        ]
        by_id = {m.get("id"): m for m in existing_models}
        merged: List[Dict[str, Any]] = []
        for model in profile.models:
            target = by_id.get(model.id, {})
            target["id"] = model.id
            target.setdefault("name", model.display_name)
            target.setdefault("input", ["text"])
            target["contextWindow"] = model.context_window
            target["maxTokens"] = model.max_output_tokens
            merged.append(target)
        entry["models"] = merged

        report.provider_status[profile.name] = "updated" if existed else "created"

    @staticmethod
    def _parse_check(raw_path: str) -> None:
        import json

        with open(raw_path, encoding="utf-8") as fh:
            json.load(fh)


class PiAdapter(PiFamilyAdapter):
    """pi harness (see module docstring for research findings)."""

    name = "pi"
    label = "pi"
    agent_dir = ".pi/agent"
