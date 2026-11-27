"""Codex adapter — ``<home-base>/.codex/config.toml`` plus per-provider catalogs.

Config mechanism (verified against the installed codex-cli 0.172.0 **and** its
source, commit ``afe555b`` in the codex git checkout):

* ``~/.codex/config.toml`` declares providers under ``[model_providers.<id>]``
  with ``name``, ``base_url``, ``wire_api`` (``"responses"`` for both current
  profiles) and key delivery via ``env_key`` (bearer) or ``env_http_headers``
  (custom header). Top-level ``model`` + ``model_provider`` select the default.
* ``model_catalog_json = "<file>"`` points at a JSON model catalog that must
  live next to ``config.toml``; we derive it at write time from the selected
  providers' model lists (never shipped as a static file) and reference it by
  bare filename — the path rule forbids serializing host-side absolute paths.

**The catalog is process-global and provider-agnostic.** ``ModelInfo``
(``codex-rs/protocol/src/openai_models.rs``) has no ``provider`` field, and
``ThreadModelCatalog`` (``codex-rs/core/src/thread_manager/model_catalog.rs``)
binds exactly one catalog to the active provider
(``matches_catalog_provider``): "When set, this replaces the bundled catalog
for the current process" (``codex-rs/core/src/config/mod.rs``). Routing comes
solely from ``model_provider``, so a shared catalog holding models from
several providers would let the user pick a model that is then sent to the
*wrong* provider (verified: ``codex exec`` with a second provider's slug ran
``provider: llm-gateway``). Therefore each catalog file holds **one
provider's** models only:

* ``moat-models.json`` — the default (first-selected) provider, referenced
  from the top level; this is what plain ``codex`` uses.
* ``moat-models-<name>.json`` — every other selected provider.

**Provider switching uses codex Profile-V2 overlays.** ``codex -p <name>``
loads ``${CODEX_HOME}/<name>.config.toml`` as an extra user-config layer
(``codex-rs/config/src/loader/mod.rs``; ``ConfigProfile`` in
``codex-rs/config/src/profile_toml.rs`` carries ``model``, ``model_provider``
and ``model_catalog_json`` — the profile switches model, provider and catalog
together; verified with ``codex exec -p``). We write one overlay per selected
provider, named ``<codex_profile>.config.toml`` (``codex_profile`` defaults to
the provider name unless the specification overrides it). The overlay sets
only the moat-owned keys and is merged non-destructively. A legacy
``profile = "<name>"`` / ``[profiles.<name>]`` table in ``config.toml`` makes
``codex -p <name>`` refuse to start — we detect that and warn.

Catalog entry schema — the parsed ``ModelInfo`` fields only (unknown keys
are silently ignored by codex and are not written). Required (no serde
default): ``slug``, ``display_name``, ``shell_type`` (canonical
``"unified_exec"``; ``"shell_command"`` is only a serde alias),
``visibility``, ``supported_in_api``, ``priority``,
``supported_reasoning_levels``, ``experimental_supported_tools``,
``support_verbosity``, ``truncation_policy``, and — via the legacy base
instructions deserializer — ``base_instructions``. ``context_window`` is
optional and omitted when unknown. Every entry **requires**
``base_instructions`` (or ``model_messages.instructions_template``):
``ModelsResponse`` uses ``deserialize_model_infos_with_legacy_base``, which
fails the whole catalog otherwise (verified empirically against 0.172.0).

Other conservative defaults on creation: ``approval_policy = "on-request"``
(read-only-ish on shared HPC). Existing values of unrelated settings (and of
approval_policy itself) are preserved.

Sandbox mode: with a container home base (the default — the moat runner
mounts the home base at the container's home path) the container itself is
the sandbox, so ``sandbox_mode = "danger-full-access"`` is set when absent
to give Codex full access *within* the container. In host mode
(``--init-home-dir``, real home, no containment layer) the key is not
written, and a ``danger-full-access`` value is removed with a warning.

Daemon mode: with a container home base, ``[features]
daemon_auto_start = false`` is set when absent so the app-server daemon
does not auto-start inside the container (verified as a stable feature
flag against codex-cli 0.172.0, ``codex features list``; a user-set value
is preserved). In host mode the setting is left untouched either way — it
is a restriction, not a grant, so there is nothing to strip there.

When the default provider's specification sets
``codex_disable_response_storage``, the top-level ``disable_response_storage``
is set to ``true`` if absent (some gateways reject stored responses). Each
profile overlay carries its provider's value explicitly, so switching
profiles never inherits the wrong setting from the top level.

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

#: Suffix of codex Profile-V2 overlay files (``codex -p <name>`` loads
#: ``${CODEX_HOME}/<name>.config.toml``).
PROFILE_SUFFIX = ".config.toml"

#: Required per catalog entry (``ModelsResponse`` legacy deserializer rejects
#: entries that lack both this and ``model_messages.instructions_template``).
BASE_INSTRUCTIONS = "You are a helpful coding assistant."


class CodexAdapter(_base.Adapter):
    """Codex harness (see module docstring)."""

    name = "codex"
    label = "codex"

    def run(self) -> AdapterReport:
        report = AdapterReport(harness=self.name, key_mode="env-ref")
        try:
            profile_names = [sel.profile.codex_profile for sel in self.selections]
            if len(set(profile_names)) != len(profile_names):
                raise ValueError(f"duplicate codex profile names: {profile_names}")

            config_dir = self.home_base / ".codex"
            config_path = config_dir / "config.toml"
            catalog_path = config_dir / CATALOG_FILE_NAME
            self.dbg(f"config path: {config_path}")
            self.dbg(f"default catalog path (derived): {catalog_path}")

            doc = self._load_toml(config_path) if config_path.exists() else None
            created = doc is None
            if doc is None:
                import tomlkit

                doc = tomlkit.document()
            else:
                self._warn_legacy_profile_conflicts(doc, report)

            default_sel = self.default_selection

            # Conservative defaults, only ever set if absent.
            if "approval_policy" not in doc:
                doc["approval_policy"] = "on-request"
            if (
                default_sel.profile.codex_disable_response_storage
                and "disable_response_storage" not in doc
            ):
                doc["disable_response_storage"] = True

            # Sandbox mode: in container mode (default) moat provides the
            # containment, so Codex may have full access within the container.
            # In host mode (--init-home-dir) there is no containment layer:
            # never grant, and strip a previously written grant.
            if self.container_mode:
                if "sandbox_mode" not in doc:
                    doc["sandbox_mode"] = "danger-full-access"
                    self.dbg("sandbox_mode set to danger-full-access (container mode)")
            elif doc.get("sandbox_mode") == "danger-full-access":
                del doc["sandbox_mode"]
                self.dbg("removed sandbox_mode=danger-full-access (host mode)")
                report.warnings.append(
                    'removed sandbox_mode = "danger-full-access" from config.toml: '
                    "host mode (--init-home-dir) has no container isolation — "
                    "re-add it manually only if you accept the risk"
                )

            # Daemon mode: in container mode the app-server daemon must not
            # auto-start inside the container home. In host mode the setting
            # is left untouched (it is a restriction, not a grant — nothing
            # to strip there).
            if self.container_mode:
                features = self._ensure_table(doc, "features")
                if "daemon_auto_start" not in features:
                    features["daemon_auto_start"] = False
                    self.dbg("features.daemon_auto_start set to false (container mode)")

            # Default model + provider: first selection in order. The default
            # catalog holds only the default provider's models — the codex
            # catalog is process-global, so anything else would misroute.
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
                self._write_catalog(catalog_path, default_sel.profile, report)
                for sel in self.selections:
                    if sel is default_sel:
                        continue
                    self._write_catalog(
                        config_dir / self._catalog_file_name(sel.profile),
                        sel.profile,
                        report,
                    )
                for sel in self.selections:
                    self._write_profile_overlay(config_dir, sel, report)

            if created:
                note = (
                    "codex config.toml was created with approval_policy = "
                    '"on-request" (conservative default for shared HPC).'
                )
                if self.container_mode:
                    note += (
                        ' sandbox_mode = "danger-full-access" was set because the '
                        "home base is a container home: moat provides containment; "
                        "[features] daemon_auto_start = false disables the codex "
                        "daemon inside the container."
                    )
                report.manual_notes.append(note)
            switching = ", ".join(
                f"codex -p {sel.profile.codex_profile}"
                + (" (default)" if sel is default_sel else "")
                for sel in self.selections
            )
            report.manual_notes.append(
                "codex catalogs are per provider: switch with " + switching + "."
            )
        except Exception as exc:
            report.error = str(exc)
            report.status = "failed"
        self._finalize_status(report)
        return report

    # ------------------------------------------------------------------

    def _catalog_file_name(self, profile: Any) -> str:
        """Catalog file for a provider; the default reuses ``moat-models.json``."""
        if profile.name == self.default_selection.profile.name:
            return CATALOG_FILE_NAME
        return f"moat-models-{profile.name}.json"

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

    def _write_catalog(self, catalog_path: Path, profile: Any, report: AdapterReport) -> None:
        """Derive a single-provider model catalog JSON from the profile."""
        models: List[Dict[str, Any]] = []
        for model in profile.models:
            entry: Dict[str, Any] = {
                "slug": model.id,
                "display_name": model.display_name,
                "shell_type": "unified_exec",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 0,
                "supported_reasoning_levels": [],
                "experimental_supported_tools": [],
                "support_verbosity": False,
                "base_instructions": BASE_INSTRUCTIONS,
            }
            # ``truncation_policy`` is required by codex-cli (verified against
            # 0.167.0): an entry without it fails to parse. The limit caps at
            # 500k; for a model with unknown context window (discovered
            # metadata 0) the same cap is the fallback — a zero limit would
            # truncate everything.
            if model.context_window:
                entry["context_window"] = model.context_window
            entry["truncation_policy"] = {
                "mode": "tokens",
                "limit": min(model.context_window, 500_000) or 500_000,
            }
            models.append(entry)
        content = self._dump_json({"models": models})
        self._write_text(catalog_path, content, report, parse_check=self._parse_json_check)

    def _write_profile_overlay(
        self, config_dir: Path, sel: ProviderSelection, report: AdapterReport
    ) -> None:
        """Write the Profile-V2 overlay selecting this provider (``codex -p``).

        The overlay is a full config layer over ``config.toml``; only the
        moat-owned keys are set, everything else is preserved. Catalog and
        ``disable_response_storage`` are per profile so switching providers
        never misroutes a model or inherits the wrong setting.
        """
        import tomlkit

        profile = sel.profile
        path = config_dir / f"{profile.codex_profile}{PROFILE_SUFFIX}"
        self.dbg(f"profile overlay path: {path}")
        doc = self._load_toml(path) if path.exists() else tomlkit.document()
        doc["model"] = profile.default_model
        doc["model_provider"] = profile.name
        doc["model_catalog_json"] = self._catalog_file_name(profile)
        doc["disable_response_storage"] = profile.codex_disable_response_storage
        self._write_text(path, self._dump_toml(doc), report, parse_check=self._parse_toml_check)

    def _warn_legacy_profile_conflicts(self, doc: Any, report: AdapterReport) -> None:
        """``codex -p <name>`` refuses to run with a legacy profile of that name."""
        names = {sel.profile.codex_profile for sel in self.selections}
        conflicts: List[str] = []
        selector = doc.get("profile")
        if isinstance(selector, str) and selector in names:
            conflicts.append(f'profile = "{selector}"')
        profiles_table = doc.get("profiles")
        if isinstance(profiles_table, dict):
            for name in sorted(names.intersection(profiles_table)):
                conflicts.append(f"[profiles.{name}]")
        if conflicts:
            report.warnings.append(
                "config.toml contains legacy codex profile selector(s) "
                + ", ".join(conflicts)
                + ": `codex -p <name>` refuses to start while a legacy selector "
                "of the same name exists — remove or rename it"
            )

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
