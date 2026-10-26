"""Adapter contract shared by every harness adapter.

Every adapter implements :class:`Adapter`: it receives the home base, the
ordered list of selected ``(provider, key)`` pairs, and the dry-run flag, and
must:

1. Resolve the harness's global config path(s) under the home base.
2. Load existing files if present (tomlkit for TOML, json for JSON).
3. Merge only the moat provider entries, model list, and default-model
   selection; preserve everything else; update existing moat entries in place.
4. Handle keys per its declared mode (env-ref or literal).
5. Serialize and write, backing up pre-existing files first (timestamped).
6. Re-read and parse the written file (post-write parse check).
7. Return an :class:`AdapterReport`.

Hard constraints (from AGENTS.md):

* Path rule — never serialize the host-side absolute path of the home base
  into a harness config; reference companion files relative to the config file
  or via ``$HOME``-style references only.
* Secrets — keys appear at most once (in the managed env file) unless the
  adapter's harness requires literal keys, in which case the report must flag
  the file as containing a plaintext secret.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..logging_setup import get_logger
from ..merge import dump_json, dump_toml, load_json, load_toml
from ..providers import ProviderProfile


@dataclass
class ProviderSelection:
    """One selected provider together with its API key."""

    profile: ProviderProfile
    key: str


@dataclass
class AdapterReport:
    """Uniform outcome of one adapter run."""

    harness: str
    status: str = "no-change"  # created | updated | no-change | failed
    files_created: List[str] = field(default_factory=list)
    files_updated: List[str] = field(default_factory=list)
    files_unchanged: List[str] = field(default_factory=list)
    backups: List[str] = field(default_factory=list)
    key_mode: str = "env-ref"  # env-ref | literal
    parse_check: str = "not-run"  # ok | failed:<reason> | skipped (dry-run)
    provider_status: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    manual_notes: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Adapter(ABC):
    """Base class / protocol for harness adapters."""

    #: Registry key of the harness (e.g. "pi", "codex").
    name: str = ""
    #: Human-readable harness label for summaries.
    label: str = ""

    def __init__(
        self,
        home_base: Path,
        selections: List[ProviderSelection],
        dry_run: bool = False,
    ) -> None:
        if not selections:
            raise ValueError(f"adapter {self.name!r} requires at least one provider selection")
        self.home_base = home_base
        self.selections = selections
        self.dry_run = dry_run
        # Per-harness child logger (e.g. ``moat_install.adapters.pi``) so log
        # records identify the harness; all output goes through ``logging``
        # (see ``logging_setup.py``).
        self.log = get_logger(f"{__name__}.{self.name or 'unnamed'}")

    def dbg(self, msg: str) -> None:
        """Emit a debug log record. Callers must pre-mask any secrets."""
        self.log.debug(msg)

    @property
    def default_selection(self) -> ProviderSelection:
        """First selection in order — becomes the harness's default provider."""
        return self.selections[0]

    @classmethod
    def env_aliases(cls, default_profile: ProviderProfile) -> Dict[str, str]:
        """Extra managed env-file exports this harness needs at runtime.

        Maps variable name → template (may reference other managed variables,
        e.g. ``"$GATEWAY_API_KEY"``). The key *value* is still persisted
        exactly once; aliases are references expanded at source time.
        """
        return {}

    @abstractmethod
    def run(self) -> AdapterReport:
        """Configure the harness; never raises on harness-level failure,
        returns a failed AdapterReport instead."""

    # ------------------------------------------------------------------
    # Shared file plumbing
    # ------------------------------------------------------------------

    def _backup(self, path: Path) -> Optional[Path]:
        """Timestamped backup of a pre-existing file (no-op in dry-run)."""
        if self.dry_run or not path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
        self.log.debug("backup: %s -> %s", path, backup)
        return backup

    def _write_text(
        self,
        path: Path,
        content: str,
        report: AdapterReport,
        parse_check: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Serialize-and-write with idempotency, backup, and parse check.

        * If the on-disk content is already identical, nothing is written and
          the file is reported as unchanged (idempotent re-runs).
        * A pre-existing file that does change is backed up first.
        * After a real write, ``parse_check`` re-reads and parses the file to
          prove validity; failure marks the report as failed (exit code 4).
        """
        parent = path.parent
        exists = path.exists()
        if exists and path.read_text(encoding="utf-8") == content:
            self.log.debug("unchanged: %s", path)
            report.files_unchanged.append(str(path))
            return

        if self.dry_run:
            self.log.debug("dry run: would %s %s", "update" if exists else "create", path)
            return

        if not parent.exists():
            self.log.debug("creating directory: %s", parent)
        parent.mkdir(parents=True, exist_ok=True)
        if exists:
            backup = self._backup(path)
            if backup is not None:
                report.backups.append(str(backup))

        tmp_path = path.with_name(path.name + ".moat-tmp")
        self.log.debug("writing temporary file: %s", tmp_path)
        tmp_path.write_text(content, encoding="utf-8")
        self.log.debug("atomic rename: %s -> %s", tmp_path, path)
        tmp_path.replace(path)
        if exists:
            self.log.debug("updated: %s", path)
            report.files_updated.append(str(path))
        else:
            self.log.debug("created: %s", path)
            report.files_created.append(str(path))

        if parse_check is not None:
            try:
                parse_check(str(path))
                self.log.debug("parse check ok: %s", path)
                report.parse_check = "ok"
            except Exception as exc:
                self.log.debug("parse check failed for %s: %s", path, exc)
                report.parse_check = f"failed: {exc}"
                report.error = f"post-write parse check failed for {path}: {exc}"
                report.status = "failed"

    # Convenience loaders shared by subclasses ------------------------

    def _finalize_status(self, report: AdapterReport) -> None:
        """Derive the aggregate report status from its file lists."""
        if report.error is not None:
            report.status = "failed"
        elif report.files_created:
            report.status = "created"
        elif report.files_updated:
            report.status = "updated"
        else:
            report.status = "no-change"
            # Nothing was written: every per-provider entry is unchanged too.
            for key in report.provider_status:
                report.provider_status[key] = "no-change"
        self.log.debug(
            "aggregate status: %s (created=%d, updated=%d, unchanged=%d)",
            report.status,
            len(report.files_created),
            len(report.files_updated),
            len(report.files_unchanged),
        )

    @staticmethod
    def _load_json(path: Path) -> Dict[str, Any]:
        return load_json(path)

    @staticmethod
    def _dump_json(data: Dict[str, Any]) -> str:
        return dump_json(data)

    @staticmethod
    def _load_toml(path: Path) -> Any:
        return load_toml(path)

    @staticmethod
    def _dump_toml(doc: Any) -> str:
        return dump_toml(doc)
