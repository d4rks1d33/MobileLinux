"""Tests for the interactive kernel-config editor + catalog + distros."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mobilelinux.core.repo import find_repo
from mobilelinux.core.registry import Registry
from mobilelinux.core.kconfig import (
    Catalog, catalog_for_distro, load_fragment, write_fragment,
    load_fragment_header, _apply_preset,
)
from mobilelinux.distros import known_distros, get_backend

REPO_ROOT = Path(__file__).resolve().parents[1]


def _repo():
    return find_repo(REPO_ROOT)


def test_postmarketos_is_a_distro():
    assert "postmarketos" in known_distros()
    assert "kali" in known_distros()


def test_pmos_alias():
    b = get_backend("pmos", REPO_ROOT)
    assert b.name == "postmarketos"


def test_kali_package_lists_are_real():
    from mobilelinux.distros.debian_base import read_package_list
    base = read_package_list(REPO_ROOT / "os-distros/kali/packages/base.list")
    assert "kali-menu" in base
    assert "initramfs-tools" in base
    phone = read_package_list(REPO_ROOT / "os-distros/kali/packages/phone-role.list")
    assert "mobian-phosh-phone" in phone


def test_kali_integration_config_loads():
    import yaml
    cfg = yaml.safe_load(
        (REPO_ROOT / "os-distros/kali/configuration/integration.yaml").read_text())
    assert "droid-juicer.service" in cfg["mask_services"]
    assert "qrtr-ns" in cfg["enable_services"]
    # apt holds must run last in the install order
    assert cfg["userspace_install_order"][-1] == "apt"


def test_future_distros_present():
    import yaml
    # arch is still planned; debian/ubuntu now have a backend (experimental).
    assert yaml.safe_load((REPO_ROOT / "os-distros/arch/distro.yaml").read_text())["status"] == "planned"
    for d in ("debian", "ubuntu"):
        data = yaml.safe_load((REPO_ROOT / f"os-distros/{d}/distro.yaml").read_text())
        assert data["status"] in ("experimental", "planned")
        assert data["family"] == "debian"     # reuse the .deb / debos path
    assert yaml.safe_load((REPO_ROOT / "os-distros/arch/distro.yaml").read_text())["family"] == "arch"


def test_debian_family_backends_share_pipeline():
    from mobilelinux.distros import get_backend
    from mobilelinux.distros.debian_base import DebianBackend
    for name, suite in [("kali", "kali-rolling"), ("debian", "trixie"), ("ubuntu", "noble")]:
        b = get_backend(name, REPO_ROOT)
        assert isinstance(b, DebianBackend)
        assert b.suite == suite
        assert b.family == "debian"


def test_catalog_loads_with_categories_and_presets():
    cat = catalog_for_distro(_repo(), "kali")
    assert "usb_wifi_injection" in cat.categories
    assert "sdr" in cat.categories
    assert "nethunter-full" in cat.presets
    assert "wifi-only" in cat.presets
    # RT2800USB must be catalogued under wifi injection
    assert cat.category_of("CONFIG_RT2800USB") == "usb_wifi_injection"


def test_fragment_roundtrip(tmp_path):
    src = _repo().devices_dir / "motorola/rhodep/assets/kernel/flavors/kali.fragment"
    frag = tmp_path / "kali.fragment"
    shutil.copy(src, frag)
    states = load_fragment(frag)
    header = load_fragment_header(frag)
    assert states.get("CONFIG_RT2800USB") == "m"
    # disable one, write back, re-read
    states["CONFIG_USB_HACKRF"] = "n"
    write_fragment(frag, states, header)
    states2 = load_fragment(frag)
    assert states2["CONFIG_USB_HACKRF"] == "n"
    assert states2["CONFIG_RT2800USB"] == "m"


def test_preset_wifi_only_disables_sdr(tmp_path):
    cat = catalog_for_distro(_repo(), "kali")
    states = {}
    _apply_preset(cat, states, "wifi-only")
    # wifi kept, SDR off
    assert states["CONFIG_RT2800USB"] in ("m", "y")
    assert states["CONFIG_USB_HACKRF"] == "n"
    # distro-compat base symbols kept
    assert states["CONFIG_MODULE_ALLOW_BTF_MISMATCH"] == "y"


def test_preset_full_enables_all_categories(tmp_path):
    cat = catalog_for_distro(_repo(), "kali")
    states = {}
    _apply_preset(cat, states, "nethunter-full")
    assert states["CONFIG_USB_HACKRF"] in ("m", "y")
    assert states["CONFIG_CAN_ISOTP"] in ("m", "y")
    assert states["CONFIG_NFSD"] in ("m", "y")
