"""Device-definition validation against the JSON Schema.

Uses the ``jsonschema`` package when available (full Draft 2020-12 support).
Falls back to a small built-in validator that checks the essentials
(required keys, enums, types for the fields the framework relies on) so the
CLI still works on a machine without jsonschema installed.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def _load_schema(schema_path: str) -> dict[str, Any]:
    with open(schema_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate(data: dict[str, Any], schema_path: Path) -> list[str]:
    """Return a list of human-readable validation error strings (empty = ok)."""
    schema = _load_schema(str(schema_path))
    try:
        import jsonschema  # type: ignore
    except Exception:
        return _fallback_validate(data, schema)

    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    errors = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{loc}: {err.message}")
    return errors


def _fallback_validate(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Minimal validator: required top-level keys + a few critical enums.

    This is intentionally conservative; it catches the mistakes that would
    break the framework, and is superseded by full jsonschema in CI.
    """
    errors: list[str] = []

    required = schema.get("required", [])
    for key in required:
        if key not in data:
            errors.append(f"(root): missing required property '{key}'")

    # id pattern
    _id = data.get("id", "")
    if _id and not _is_slug(_id):
        errors.append("id: must match ^[a-z0-9][a-z0-9-]*$")

    # architecture enum
    arch_enum = ["aarch64", "armv7", "armhf", "x86_64", "riscv64"]
    if "architecture" in data and data["architecture"] not in arch_enum:
        errors.append(f"architecture: must be one of {arch_enum}")

    # install.strategy enum
    strat_enum = [
        "fastboot", "fastbootd", "rescue-dd", "adb-shell-dd", "heimdall",
        "heimdall-isorec", "sdcard", "recovery", "uuu", "mtkclient", "custom",
    ]
    inst = data.get("install", {})
    if isinstance(inst, dict):
        if "strategy" not in inst:
            errors.append("install: missing required property 'strategy'")
        elif inst["strategy"] not in strat_enum:
            errors.append(f"install/strategy: must be one of {strat_enum}")

    # hardware feature statuses
    status_enum = [
        "supported", "partial", "broken", "untested", "unsupported", "not-present",
    ]
    hw = data.get("hardware", {})
    if isinstance(hw, dict):
        for name, feat in hw.items():
            if not isinstance(feat, dict):
                errors.append(f"hardware/{name}: must be an object")
                continue
            st = feat.get("status")
            if st is None:
                errors.append(f"hardware/{name}: missing 'status'")
            elif st not in status_enum:
                errors.append(f"hardware/{name}/status: must be one of {status_enum}")

    # soc requireds
    soc = data.get("soc", {})
    if isinstance(soc, dict):
        for k in ("vendor", "family"):
            if k not in soc:
                errors.append(f"soc: missing required property '{k}'")

    return errors


def _is_slug(s: str) -> bool:
    if not s:
        return False
    if not (s[0].islower() or s[0].isdigit()):
        return False
    return all(c.islower() or c.isdigit() or c == "-" for c in s)
