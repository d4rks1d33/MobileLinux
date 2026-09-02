"""Core tests: repo discovery, registry, schema validation, support score."""

from __future__ import annotations

from pathlib import Path

import pytest

from mobilelinux.core.repo import find_repo
from mobilelinux.core.registry import Registry
from mobilelinux.core.validate import validate

REPO_ROOT = Path(__file__).resolve().parents[1]


def repo():
    return find_repo(REPO_ROOT)


def test_repo_discovery():
    r = repo()
    assert (r.root / "mobilelinux.toml").is_file()
    assert r.schema_file.is_file()


def test_all_devices_validate():
    r = repo()
    import yaml
    for path in r.devices_dir.glob("*/*/device.yaml"):
        data = yaml.safe_load(path.read_text())
        errors = validate(data, r.schema_file)
        assert not errors, f"{path}: {errors}"


def test_rhodep_loads():
    reg = Registry(repo())
    d = reg.get("rhodep")
    assert d.vendor == "motorola"
    assert d.soc_family == "sm6375"
    assert d.install_strategy == "rescue-dd"
    assert d.ota_strategy == "ab"


def test_rhodep_aliases():
    reg = Registry(repo())
    assert reg.get("motorola-rhodep").id == "rhodep"
    assert reg.get("XT2225-1").id == "rhodep"


def test_support_score_excludes_not_present():
    reg = Registry(repo())
    d = reg.get("rhodep")
    pct = d.support_percent()
    # rhodep has broken camera + untested fingerprint, several partials.
    assert 70 <= pct <= 95


def test_support_score_math():
    # A device with one supported (weight 1) and one broken (weight 1) = 50%.
    from mobilelinux.core.model import Device
    data = {
        "hardware": {
            "wifi": {"status": "supported", "weight": 1.0},
            "modem": {"status": "broken", "weight": 1.0},
            "camera": {"status": "not-present", "weight": 1.0},
        }
    }
    d = Device(data, REPO_ROOT / "x")
    assert d.support_percent() == 50
