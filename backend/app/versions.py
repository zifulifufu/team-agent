"""Catalog version comparison: handles both date-style versions (2026-09-20) and
dot-separated ones (1.10.0 is newer than 1.9.0)."""

from __future__ import annotations

import re


def _key(v: object) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def is_newer(candidate: object, current: object) -> bool:
    """True when candidate is strictly newer than current. Numeric segments are compared
    one by one; falls back to a string comparison when neither side has digits."""
    a, b = _key(candidate), _key(current)
    if a or b:
        return a > b
    return str(candidate or "") > str(current or "")
