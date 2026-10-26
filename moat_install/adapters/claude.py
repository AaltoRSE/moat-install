"""Claude Code adapter — ``<home-base>/.claude/settings.json``.

Config mechanism (verified against the installed Claude Code 2.1.123 binary,
whose embedded strings contain the variable names below; plus the standard
Claude Code settings.json schema):

* ``~/.claude/settings.json`` supports an ``"env"`` block whose entries are
  applied to the harness process at startup (they take precedence over the
  inherited shell environment — stale values must be removed when switching).
* Relevant variables (present in the binary's strings):
  ``ANTHROPIC_BASE_URL``, ``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_CUSTOM_HEADERS``,
  ``ANTHROPIC_MODEL``.

Key handling — per-provider reality:

* ``llm-gateway`` (bearer): ``ANTHROPIC_AUTH_TOKEN`` is read from the process
  environment, so we deliberately do NOT write it into settings.json. The
  token value lives only in the managed env file (``env.sh``), delivered via
  the guarded rc block and/or host env passthrough. env-ref mode. A stale
  ``ANTHROPIC_AUTH_TOKEN`` in the settings env block is removed (with a
  warning) because the settings block would otherwise override the delivered
  environment value.
* Header-auth providers (e.g. an Azure APIM gateway): the endpoint
  authenticates via the custom ``auth_header`` header.
  ``ANTHROPIC_CUSTOM_HEADERS`` is a
  literal string without environment interpolation, so the key must be
  written into settings.json in plaintext. Literal-key mode: the report and
  summary flag the file as containing a plaintext secret.

Single-provider constraint: Claude Code has exactly one active endpoint, so
only the first selected provider (the default) is written into settings.json;
a second selected provider is still fully configured for the other harnesses
and its key is present in the managed env file, but switching Claude Code to
it requires editing settings.json (reported as a manual note).
"""

from __future__ import annotations

from typing import Any, Dict

from ..providers import AUTH_BEARER, AUTH_HEADER
from . import _base
from ._base import AdapterReport


class ClaudeAdapter(_base.Adapter):
    """Claude Code harness (see module docstring)."""

    name = "claude"
    label = "claude"

    def run(self) -> AdapterReport:
        report = AdapterReport(harness=self.name)
        try:
            path = self.home_base / ".claude" / "settings.json"
            self.dbg(f"config path: {path}")
            doc: Dict[str, Any] = self._load_json(path) if path.exists() else {}
            env = doc.get("env")
            if not isinstance(env, dict):
                env = {}
                doc["env"] = env

            default = self.default_selection
            profile = default.profile

            # Only the default provider can be active in Claude Code.
            env["ANTHROPIC_BASE_URL"] = profile.base_url
            report.provider_status[profile.name] = (
                "updated" if existed(env, "ANTHROPIC_BASE_URL") else "created"
            )

            if profile.auth == AUTH_BEARER:
                # Token is delivered via the managed env file, not the config.
                if "ANTHROPIC_AUTH_TOKEN" in env:
                    del env["ANTHROPIC_AUTH_TOKEN"]
                    report.warnings.append(
                        "removed stale ANTHROPIC_AUTH_TOKEN from settings.json env "
                        "block (it would override the token delivered via the "
                        "managed env file)"
                    )
                report.key_mode = "env-ref"
                report.manual_notes.append(
                    "claude reads ANTHROPIC_AUTH_TOKEN from the environment; it is "
                    f"exported by the managed env file ({profile.api_key_env}). Make "
                    "sure the rc block in the home base is sourced (shell launch) or "
                    "export it on the host (env passthrough)."
                )
            elif profile.auth == AUTH_HEADER:
                # ANTHROPIC_CUSTOM_HEADERS is literal-only → plaintext secret.
                env["ANTHROPIC_CUSTOM_HEADERS"] = f"{profile.auth_header}: {default.key}"
                report.key_mode = "literal"
                report.warnings.append(
                    "settings.json contains a plaintext API key "
                    f"(ANTHROPIC_CUSTOM_HEADERS for {profile.name}); the file is "
                    "mode 0600 and the key is masked in all logs"
                )
                if "ANTHROPIC_AUTH_TOKEN" in env:
                    del env["ANTHROPIC_AUTH_TOKEN"]
                    report.warnings.append(
                        "removed stale ANTHROPIC_AUTH_TOKEN from settings.json env block"
                    )

            if len(self.selections) > 1:
                other = ", ".join(s.profile.name for s in self.selections[1:])
                report.manual_notes.append(
                    f"claude supports one active endpoint; only '{profile.name}' is "
                    f"configured. To switch to {other}, edit "
                    "<base>/.claude/settings.json env block."
                )

            content = self._dump_json(doc)
            self._write_text(path, content, report, parse_check=self._parse_check)
            if report.key_mode == "literal" and not self.dry_run and path.exists():
                self.dbg(f"chmod 0600 (file contains a plaintext secret): {path}")
                path.chmod(0o600)  # plaintext secret inside
        except Exception as exc:
            report.error = str(exc)
            report.status = "failed"
        self._finalize_status(report)
        return report

    @staticmethod
    def _parse_check(raw_path: str) -> None:
        import json

        with open(raw_path, encoding="utf-8") as fh:
            json.load(fh)


def existed(env: Dict[str, Any], key: str) -> bool:
    """True if ``key`` was already present in ``env`` before this run's merge."""
    return key in env
