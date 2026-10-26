"""Adapter registry: harness name → adapter class.

``cli.py`` contains zero harness-specific branches: it resolves selections,
iterates this registry, and collects reports. Adding a harness = adding one
adapter module + its test, then registering the class here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Type

from ._base import Adapter, AdapterReport, ProviderSelection
from .aider import AiderAdapter
from .claude import ClaudeAdapter
from .cline import ClineAdapter
from .codex import CodexAdapter
from .goose import GooseAdapter
from .oh_my_pi import OhMyPiAdapter
from .opencode import OpencodeAdapter
from .pi import PiAdapter

#: Registry of all implemented harness adapters, keyed by harness name.
ADAPTERS: Dict[str, Type[Adapter]] = {
    AiderAdapter.name: AiderAdapter,
    ClaudeAdapter.name: ClaudeAdapter,
    ClineAdapter.name: ClineAdapter,
    CodexAdapter.name: CodexAdapter,
    GooseAdapter.name: GooseAdapter,
    OhMyPiAdapter.name: OhMyPiAdapter,
    OpencodeAdapter.name: OpencodeAdapter,
    PiAdapter.name: PiAdapter,
}

__all__ = [
    "ADAPTERS",
    "Adapter",
    "AdapterReport",
    "ProviderSelection",
    "adapter_names",
    "build_adapters",
]


def adapter_names() -> List[str]:
    """Names of all registered adapters, sorted."""
    return sorted(ADAPTERS)


def build_adapters(
    names: List[str],
    home_base: Path,
    selections: List[ProviderSelection],
    dry_run: bool = False,
) -> List[Adapter]:
    """Instantiate the selected adapters in registry (sorted) order."""
    return [ADAPTERS[name](home_base, selections, dry_run=dry_run) for name in names]
