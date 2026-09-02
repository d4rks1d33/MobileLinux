"""Strategy + flash-plan tests (no device required, no destructive ops)."""

from __future__ import annotations

from pathlib import Path

from mobilelinux.core.repo import find_repo
from mobilelinux.core.registry import Registry
from mobilelinux.installer.artifacts import ArtifactSet
from mobilelinux.installer.strategies import get_strategy, known_strategies

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_all_device_strategies_are_implemented():
    reg = Registry(find_repo(REPO_ROOT))
    for d in reg.load_all(validate_schema=False):
        assert d.install_strategy in known_strategies(), d.install_strategy


def test_rhodep_rescue_dd_plan():
    reg = Registry(find_repo(REPO_ROOT))
    d = reg.get("rhodep")
    aset = ArtifactSet(device="rhodep")
    strat_cls = get_strategy(d.install_strategy)
    strat = strat_cls(d, aset, REPO_ROOT / "out" / "rhodep")
    ops = strat.plan()
    # The plan must include flashing the rescue image, a dd to userdata, and a
    # final boot flash.
    descs = " ".join(o.description.lower() for o in ops)
    assert "rescue" in descs
    assert any(o.partition == "userdata" and o.destructive for o in ops)
    assert any(o.partition == "boot_a" and o.destructive for o in ops)


def test_modified_partitions_only_declared():
    reg = Registry(find_repo(REPO_ROOT))
    d = reg.get("rhodep")
    strat_cls = get_strategy(d.install_strategy)
    strat = strat_cls(d, ArtifactSet(device="rhodep"), REPO_ROOT / "out" / "rhodep")
    parts = strat.modified_partitions()
    # rhodep must only ever write boot_a and userdata.
    assert set(parts) == {"boot_a", "userdata"}
