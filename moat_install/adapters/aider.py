"""aider adapter — ``<home-base>/.aider.conf.yml`` plus managed env aliases.

RESEARCH findings (from https://aider.chat/docs/config/aider_conf.html, the
linked API-key/dotenv docs, and https://aider.chat/docs/llms/openai-compat.html):

* ``.aider.conf.yml`` is looked up in the home directory (then git root, then
  cwd) — so ``<home-base>/.aider.conf.yml``.
* OpenAI-compatible endpoints: model name prefixed with ``openai/`` plus the
  endpoint via ``openai-api-base`` (config) or ``OPENAI_API_BASE`` (env).
* Keys: only OpenAI/Anthropic keys belong in the YAML; keys for all APIs can
  be stored in a ``.env`` file or the environment. The openai-compatible flow
  reads ``OPENAI_API_KEY`` from the environment — so key_mode is "env-ref":
  we never write the key into the YAML; it is delivered via the managed env
  file, which exports an alias ``OPENAI_API_KEY="$<provider key env>"``
  (declared through :meth:`Adapter.env_aliases`, resolved at launch from the
  guarded rc block and/or host env passthrough).

Merge behaviour (YAML, round-trip preserving via ruamel.yaml — justified per
AGENTS.md §10: aider's config is YAML and user comments/formatting must
survive): only the top-level ``model`` and ``openai-api-base`` keys are
updated in place; every other key and comment is preserved.
Single-provider constraint: aider has one active ``openai-api-base``, so only
the first selected provider (the default) is configured; further selections
are reported as manual notes. For header-auth providers (e.g. an Azure APIM
gateway) a warning is emitted: the custom ``auth_header`` header, which
aider's env-var config cannot express (would need a literal header in
.aider.model.settings.yml).
"""

from __future__ import annotations

from typing import Dict

from ..providers import ProviderProfile
from . import _base
from ._base import AdapterReport

AIDER_MANAGED_KEYS = ("model", "openai-api-base")


class AiderAdapter(_base.Adapter):
    """aider harness (see module docstring for research findings)."""

    name = "aider"
    label = "aider"

    @classmethod
    def env_aliases(cls, default_profile: ProviderProfile) -> Dict[str, str]:
        """aider's OpenAI-compatible flow reads OPENAI_API_KEY at runtime."""
        return {"OPENAI_API_KEY": "$" + default_profile.api_key_env}

    def run(self) -> AdapterReport:
        report = AdapterReport(harness=self.name, key_mode="env-ref")
        try:
            from . import _yaml

            path = self.home_base / ".aider.conf.yml"
            self.dbg(f"config path: {path}")
            existed = path.exists()
            doc = _yaml.load_round_trip(path) if existed else _yaml.new_document()
            if doc is None:
                doc = _yaml.new_document()

            default = self.default_selection.profile
            doc["model"] = f"openai/{default.default_model}"
            doc["openai-api-base"] = default.base_url

            content = _yaml.dump_round_trip(doc)
            self._write_text(path, content, report, parse_check=_yaml.safe_parse)

            report.provider_status[default.name] = "updated" if existed else "created"
            for sel in self.selections[1:]:
                report.manual_notes.append(
                    f"aider supports one active endpoint; provider '{sel.profile.name}' "
                    "is not configured for aider (configured for other harnesses; "
                    "switch by editing model/openai-api-base in .aider.conf.yml)."
                )
            if default.auth == "header":
                report.warnings.append(
                    f"aider cannot deliver the {default.auth_header} header via "
                    f"environment variables; provider '{default.name}' will likely "
                    "reject requests. Configure .aider.model.settings.yml manually "
                    "or use a bearer-auth provider for aider."
                )
        except Exception as exc:
            report.error = str(exc)
            report.status = "failed"
        self._finalize_status(report)
        return report
