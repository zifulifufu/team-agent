"""目录版本比较:既要认日期式版本(2026-09-20),也要认点分版本(1.10.0 比 1.9.0 新)。"""

from __future__ import annotations

import re


def _key(v: object) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def is_newer(candidate: object, current: object) -> bool:
    """candidate 严格比 current 新。数字段逐段比较;两边都没有数字时退回按字符串比。"""
    a, b = _key(candidate), _key(current)
    if a or b:
        return a > b
    return str(candidate or "") > str(current or "")
