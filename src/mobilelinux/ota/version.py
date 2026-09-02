"""Minimal semantic-version comparison (no external dep).

Supports X.Y.Z with optional pre-release suffix ignored for ordering.
"""

from __future__ import annotations


def _parse(v: str) -> tuple[int, ...]:
    core = v.split("-", 1)[0].split("+", 1)[0]
    parts = []
    for p in core.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def version_gt(a: str, b: str) -> bool:
    return _parse(a) > _parse(b)


def version_ge(a: str, b: str) -> bool:
    return _parse(a) >= _parse(b)


def version_lt(a: str, b: str) -> bool:
    return _parse(a) < _parse(b)
