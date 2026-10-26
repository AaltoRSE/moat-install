"""Round-trip-preserving YAML helpers built on ruamel.yaml.

Both the aider (``.aider.conf.yml``) and goose (``config.yaml``) adapters
write YAML and must preserve user comments/formatting on merge, so a
round-trip-preserving loader is required (justified per AGENTS.md §10).
This module wraps ruamel.yaml's modern ``YAML(typ='rt')`` API in one place.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any


def _rt() -> Any:
    from ruamel.yaml import YAML

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True  # keep user's quoting style on round-trip
    return yaml


def load_round_trip(path: Path) -> Any:
    """Load YAML preserving comments/structure; returns None for an empty file."""
    yaml = _rt()
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh)


def dump_round_trip(doc: Any) -> str:
    """Serialize a round-trip document back to YAML text."""
    yaml = _rt()
    stream = io.StringIO()
    yaml.dump(doc, stream)
    return stream.getvalue()


def new_document() -> Any:
    """A fresh empty round-trip mapping."""
    from ruamel.yaml.comments import CommentedMap

    return CommentedMap()


def safe_parse(raw_path: str) -> Any:
    """Parse YAML with the safe loader (post-write validity check only).

    Signature matches the adapter ``parse_check`` callback contract
    (``Callable[[str], None]``).
    """
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe", pure=True)
    with open(raw_path, encoding="utf-8") as fh:
        return yaml.load(fh)
