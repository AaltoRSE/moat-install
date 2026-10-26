"""deep_merge semantics: preserve unrelated keys, idempotent re-merge."""

from __future__ import annotations

from moat_install.merge import deep_merge


def test_replaces_scalars() -> None:
    assert deep_merge({"a": 1}, {"a": 2}) == {"a": 2}


def test_preserves_unrelated_keys() -> None:
    base = {"keep": "me", "model": "old"}
    merged = deep_merge(base, {"model": "new"})
    assert merged == {"keep": "me", "model": "new"}
    assert base == {"keep": "me", "model": "old"}  # not mutated


def test_nested_dicts_merged_keywise() -> None:
    base = {"providers": {"a": {"x": 1, "y": 2}, "b": {"z": 3}}}
    update = {"providers": {"a": {"y": 9}}}
    merged = deep_merge(base, update)
    assert merged["providers"]["a"] == {"x": 1, "y": 9}
    assert merged["providers"]["b"] == {"z": 3}


def test_lists_are_replaced_not_concatenated() -> None:
    assert deep_merge({"m": [1, 2]}, {"m": [3]}) == {"m": [3]}


def test_idempotent_remerge() -> None:
    base = {"a": {"b": 1, "c": 2}}
    update = {"a": {"b": 5}}
    once = deep_merge(base, update)
    twice = deep_merge(once, update)
    assert once == twice


def test_update_adds_new_nested_keys() -> None:
    merged = deep_merge({"a": 1}, {"b": {"c": 2}})
    assert merged == {"a": 1, "b": {"c": 2}}
