"""Kernel provider + flavor tests.

Verifies the shared-base + per-distro-fragment model that mirrors the reference
port (pmOS base config + Kali fragment -> Kali config)."""

from __future__ import annotations

from pathlib import Path

from mobilelinux.core.repo import find_repo
from mobilelinux.core.registry import Registry
from mobilelinux.core.kernel import resolve_flavor, _python_merge

REPO_ROOT = Path(__file__).resolve().parents[1]


def _rhodep():
    return Registry(find_repo(REPO_ROOT)).get("rhodep")


def test_rhodep_has_provider_and_flavors():
    d = _rhodep()
    prov = d.kernel_provider
    assert prov["linux_pkg"] == "linux-motorola-rhodep"
    # rhodep is not upstream -> provider points at the porter's fork
    assert prov["upstreamed"] is False
    assert "github.com/d4rks1d33" in prov["source"]
    assert set(d.kernel_flavors) == {"pmos", "kali"}


def test_flavor_maps_to_distro():
    d = _rhodep()
    assert resolve_flavor(d, "kali", None) == "kali"
    assert resolve_flavor(d, "postmarketos", None) == "pmos"
    assert resolve_flavor(d, "anything", "kali") == "kali"


def test_kali_fragment_reconstructs_config(tmp_path):
    d = _rhodep()
    base = d.dir / d.kernel["base_config"]
    frag = d.dir / d.kernel_flavors["kali"]["config_fragment"]
    assert base.exists() and frag.exists()
    out = tmp_path / "merged.config"
    _python_merge(base, frag, out)
    text = out.read_text()
    # base is pmOS-clean (no RT2800USB); merged Kali config must enable it.
    assert "CONFIG_RT2800USB" not in base.read_text().replace("# CONFIG_RT2800USB", "")
    assert "CONFIG_RT2800USB=m" in text
    # the critical Debian/Kali symbol must be present
    assert "CONFIG_MODULE_ALLOW_BTF_MISMATCH=y" in text


def test_discriminator_declared_per_flavor():
    d = _rhodep()
    assert d.kernel_flavors["kali"]["discriminator"]["symbol"] == "CONFIG_RT2800USB"
    assert d.kernel_flavors["kali"]["discriminator"]["present"] is True
    assert d.kernel_flavors["pmos"]["discriminator"]["present"] is False


def test_all_devices_have_a_provider():
    reg = Registry(find_repo(REPO_ROOT))
    for dev in reg.load_all(validate_schema=False):
        assert dev.kernel_provider.get("linux_pkg"), dev.id
