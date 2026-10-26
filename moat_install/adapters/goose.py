"""goose adapter — ``<home-base>/.config/goose/config.yaml``.

RESEARCH findings (from https://goose-docs.ai/docs/guides/config-files and
/docs/guides/environment-variables, cross-checked against the goose source at
github.com/aaif-goose/goose, crates/goose-providers/src/openai.rs and
crates/goose/src/providers/openai_def.rs):

* Primary config: ``~/.config/goose/config.yaml`` (macOS/Linux).
* Provider settings live under top-level ``active_provider`` and
  ``providers`` keys::

      active_provider: anthropic
      providers:
        anthropic:
          enabled: true
          model: claude-sonnet-4-5-20250929
          configured: true

* **Provider API keys never go in config.yaml** (goose ignores them there).
  Keys come from the keyring, ``secrets.yaml``, or the provider's environment
  variable, which takes precedence. The ``openai`` provider reads
  ``OPENAI_API_KEY`` (env), ``OPENAI_BASE_URL`` (env or config) and
  ``OPENAI_CUSTOM_HEADERS`` (env; comma-separated ``Name=value`` pairs).
* ``parse_openai_base_url`` normalizes any URL ending in ``/v1`` and appends
  ``v1/chat/completions`` — so the full gateway base URL (which ends in
  ``/api/v1``) is the correct value for ``OPENAI_BASE_URL``.
* Env vars override config-file settings, so the base URL is written into
  config.yaml (lower priority — the user can still override per-process).

Merge behaviour: only ``active_provider`` and ``providers.openai`` are
updated in place; all other settings (GOOSE_MODE, extensions, ...) are
preserved via ruamel.yaml round-trip (comment-preserving — justified per
AGENTS.md §10).

Key delivery: env-ref mode. The managed env file exports
``OPENAI_API_KEY="$<provider key env>"`` (declared via
:meth:`Adapter.env_aliases`); for header-auth providers (e.g. Azure APIM)
an additional ``OPENAI_CUSTOM_HEADERS="<auth_header>=$<env>"`` alias is
exported (a reference, not a duplicate of the secret value).
"""

from __future__ import annotations

from typing import Dict

from ..providers import AUTH_HEADER, ProviderProfile
from . import _base
from ._base import AdapterReport

GOOSE_PROVIDER_ID = "openai"


class GooseAdapter(_base.Adapter):
    """goose harness (see module docstring for research findings)."""

    name = "goose"
    label = "goose"

    @classmethod
    def env_aliases(cls, default_profile: ProviderProfile) -> Dict[str, str]:
        """goose's openai provider resolves the key and custom headers from env."""
        aliases: Dict[str, str] = {"OPENAI_API_KEY": "$" + default_profile.api_key_env}
        if default_profile.auth == AUTH_HEADER:
            aliases["OPENAI_CUSTOM_HEADERS"] = (
                f"{default_profile.auth_header}=${default_profile.api_key_env}"
            )
        return aliases

    def run(self) -> AdapterReport:
        report = AdapterReport(harness=self.name, key_mode="env-ref")
        try:
            from . import _yaml

            path = self.home_base / ".config" / "goose" / "config.yaml"
            self.dbg(f"config path: {path}")
            existed = path.exists()
            doc = _yaml.load_round_trip(path) if existed else _yaml.new_document()
            if doc is None:
                doc = _yaml.new_document()

            default = self.default_selection.profile
            doc["active_provider"] = GOOSE_PROVIDER_ID
            providers = doc.get("providers")
            if not isinstance(providers, dict) or providers is None:
                providers = _yaml.new_document()
                doc["providers"] = providers
            entry = providers.get(GOOSE_PROVIDER_ID)
            if not isinstance(entry, dict) or entry is None:
                entry = _yaml.new_document()
            entry["enabled"] = True
            entry["model"] = default.default_model
            entry["configured"] = True
            providers[GOOSE_PROVIDER_ID] = entry

            # The openai provider reads OPENAI_BASE_URL from env *or config*;
            # config keeps it user-overridable (env vars win at runtime).
            doc["OPENAI_BASE_URL"] = default.base_url

            content = _yaml.dump_round_trip(doc)
            self._write_text(path, content, report, parse_check=_yaml.safe_parse)

            report.provider_status[default.name] = "updated" if existed else "created"
            for sel in self.selections[1:]:
                report.manual_notes.append(
                    f"goose has one active provider; '{sel.profile.name}' is not "
                    "configured for goose (switch via active_provider/providers in "
                    "config.yaml or GOOSE_PROVIDER/GOOSE_MODEL env vars)."
                )
            if default.auth == AUTH_HEADER:
                report.manual_notes.append(
                    f"goose will authenticate to provider '{default.name}' via "
                    "OPENAI_CUSTOM_HEADERS (exported by the managed env file as "
                    f"a reference to ${default.api_key_env})."
                )
        except Exception as exc:
            report.error = str(exc)
            report.status = "failed"
        self._finalize_status(report)
        return report
