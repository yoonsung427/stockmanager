"""Stockmanager-owned workspace files that are not the books (v2 S4)."""

from __future__ import annotations

from stockmanager.prefs.prefs import (
    MAX_PREFS,
    MAX_STORED_PREFS,
    Preference,
    PrefError,
    active_prefs,
    apply_set,
    dump_prefs,
    load_prefs,
)

__all__ = [
    "MAX_PREFS",
    "MAX_STORED_PREFS",
    "PrefError",
    "Preference",
    "active_prefs",
    "apply_set",
    "dump_prefs",
    "load_prefs",
]
