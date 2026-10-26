"""Generic merge helpers for JSON and TOML configuration files.

The merge policy everywhere is: add/update *only* the moat-managed keys;
preserve every unrelated setting, other providers, and safety policies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import tomlkit

from .logging_setup import get_logger

#: Config-file loads are traced at DEBUG level (see ``logging_setup.py``).
logger = get_logger(__name__)


def deep_merge(base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``update`` into a copy of ``base`` and return it.

    Nested mappings are merged key-by-key; every other type (scalars, lists)
    is replaced by the value from ``update``. Keys present only in ``base``
    are preserved. ``base`` is not mutated.
    """
    result: Dict[str, Any] = dict(base)
    for key, value in update.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(dict(result[key]), value)
        else:
            result[key] = value
    return result


def ensure_table(mapping: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Return ``mapping[key]`` as a dict, creating it (empty) if missing/wrong type.

    If the existing value is not a dict it is replaced by a fresh dict — the
    moat-managed subtree is ours to own. Used for both plain dicts (JSON) and
    tomlkit containers (TOML behaves dict-like after parse).
    """
    value = mapping.get(key)
    if not isinstance(value, dict):
        value = {}
        mapping[key] = value
    return value


def load_json(path: Path) -> Dict[str, Any]:
    """Load a JSON document as a dict (empty dict if the file is empty)."""
    import json

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        logger.debug("loaded empty JSON config: %s", path)
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object at the top level")
    logger.debug("loaded JSON config: %s (%d top-level keys)", path, len(data))
    return data


def dump_json(data: Dict[str, Any]) -> str:
    """Serialize ``data`` as the canonical on-disk JSON text used everywhere."""
    import json

    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def load_toml(path: Path) -> Any:
    """Load a TOML document with tomlkit (round-trip preserving)."""
    logger.debug("loading TOML config: %s", path)
    with open(path, "rb") as fh:
        return tomlkit.load(fh)


def dump_toml(doc: Any) -> str:
    """Serialize a tomlkit document back to TOML text (comments preserved)."""
    return tomlkit.dumps(doc)
