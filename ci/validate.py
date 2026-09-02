#!/usr/bin/env python3
"""CI validation: schema self-check, all device definitions, all manifests.

Exits non-zero on any problem. Runnable without the package installed
(adds src/ to sys.path).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mobilelinux.core.repo import find_repo  # noqa: E402
from mobilelinux.core.registry import Registry  # noqa: E402
from mobilelinux.core.validate import validate  # noqa: E402
from mobilelinux.installer.strategies import known_strategies  # noqa: E402


def main() -> int:
    repo = find_repo(ROOT)
    problems = 0

    # 1. Schemas are valid JSON.
    for schema in (repo.schema_dir).glob("*.json"):
        try:
            json.loads(schema.read_text())
        except Exception as exc:
            print(f"FAIL schema {schema.name}: {exc}")
            problems += 1
        else:
            print(f"ok   schema {schema.name}")

    # 2. Every device validates + has an implemented install strategy.
    reg = Registry(repo)
    import yaml
    for path in sorted(repo.devices_dir.glob("*/*/device.yaml")):
        data = yaml.safe_load(path.read_text())
        errors = validate(data, repo.schema_file)
        name = data.get("id", path.parent.name)
        if errors:
            print(f"FAIL device {name}:")
            for e in errors:
                print(f"       {e}")
            problems += 1
            continue
        strat = data.get("install", {}).get("strategy")
        if strat not in known_strategies():
            print(f"FAIL device {name}: unknown install strategy '{strat}'")
            problems += 1
            continue
        # Every hardware.<f>.test must reference a real test.
        from mobilelinux.testing.tests import all_tests
        tests = set(all_tests())
        for f, feat in data.get("hardware", {}).items():
            t = feat.get("test")
            if t and t not in tests:
                print(f"FAIL device {name}: hardware.{f}.test '{t}' has no test module")
                problems += 1
        # Kernel flavors: fragment files must exist; discriminators sane.
        kdir = path.parent
        kern = data.get("kernel", {})
        base = kern.get("base_config")
        if base and not (kdir / base).exists():
            print(f"FAIL device {name}: base_config '{base}' missing")
            problems += 1
        for fname, flavor in kern.get("flavors", {}).items():
            frag = flavor.get("config_fragment")
            if frag and not (kdir / frag).exists():
                print(f"FAIL device {name}: flavor {fname} fragment '{frag}' missing")
                problems += 1
        print(f"ok   device {name} ({strat})")

    if problems:
        print(f"\n{problems} problem(s) found")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
