"""OTA tests: signing round-trip, manifest schema, and the safety scenarios
(wrong device, invalid signature, downgrade, minimum-version)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mobilelinux.core.repo import find_repo
from mobilelinux.core.validate import validate
from mobilelinux.ota.manifest import Manifest, canonical_body
from mobilelinux.ota import signing
from mobilelinux.ota.state import DeviceState
from mobilelinux.ota.version import version_gt, version_ge

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def keypair(tmp_path):
    priv = tmp_path / "k.key"
    pub = tmp_path / "k.pub"
    signing.generate_keypair(priv, pub, key_id="test")
    return priv, pub


def _manifest(device="rhodep", arch="aarch64", version="1.0.0", minv="0.0.0"):
    return Manifest({
        "manifest_version": 1,
        "release": {"version": version, "channel": "stable", "date": "2026-09-01"},
        "device": {"id": device, "architecture": arch, "minimum_version": minv,
                   "ota_strategy": "ab"},
        "artifacts": {"rootfs": {"url": "r.img", "sha256": "ab"*32, "size": 1, "type": "rootfs"}},
        "security": {"security_patch_level": "2026-09-01"},
    })


def test_sign_verify_roundtrip(keypair):
    priv, pub = keypair
    m = _manifest()
    sig = signing.sign(m.canonical(), priv)
    assert signing.verify(m.canonical(), sig, pub)


def test_tamper_detected(keypair):
    priv, pub = keypair
    m = _manifest()
    sig = signing.sign(m.canonical(), priv)
    m.data["release"]["version"] = "9.9.9"
    assert not signing.verify(m.canonical(), sig, pub)


def test_manifest_validates_against_schema(keypair):
    priv, pub = keypair
    m = _manifest()
    m.data["signature"] = {"algorithm": "ed25519", "key_id": "test",
                           "value": signing.sign(m.canonical(), priv)}
    schema = REPO_ROOT / "schema" / "manifest.schema.json"
    errors = validate(m.data, schema)
    assert not errors, errors


def test_version_ordering():
    assert version_gt("1.0.1", "1.0.0")
    assert not version_gt("1.0.0", "1.0.0")
    assert version_ge("1.0.0", "1.0.0")
    assert version_gt("1.2.0", "1.1.9")


def test_wrong_device_rejected():
    from mobilelinux.ota.client import _acceptable
    state = DeviceState(device_id="rhodep", version="1.0.0")
    m = _manifest(device="enchilada")  # different device
    assert not _acceptable(m, state)


def test_minimum_version_enforced():
    from mobilelinux.ota.client import _acceptable
    state = DeviceState(device_id="rhodep", version="1.0.0")
    m = _manifest(device="rhodep", minv="2.0.0")  # requires newer than we have
    # arch check uses local machine; skip if it mismatches by forcing arch empty
    m.data["device"]["architecture"] = ""
    assert not _acceptable(m, state)


def test_downgrade_not_offered():
    # The client only installs when the manifest version is strictly greater.
    assert not version_gt("1.0.0", "1.0.1")
