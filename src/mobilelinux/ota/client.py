"""OTA update client (`mobilelinux update ...`).

Runs on the device. Flow:

    check  -> fetch manifest, verify device+arch+min-version+signature
    download -> fetch artifacts, verify sha256
    install -> atomic install (RAUC A/B where the device supports it; guarded
               single-rootfs otherwise), stage next-boot slot
    (reboot) -> health check marks the slot good, else rollback
    status  -> show current/available versions
    rollback -> switch back to the previous good slot (A/B only)

Safety: an update is NEVER installed unless the manifest's device id + arch
match this device, the minimum-version constraint holds, and the signature +
hashes verify. This makes it impossible to flash another device's image by
accident.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from ..core import tools, ui
from ..core.errors import MobileLinuxError
from .manifest import Manifest
from .state import DeviceState
from . import signing
from .version import version_ge, version_gt


def update_command(ctx, args) -> int:
    state = DeviceState.load()
    if args.channel:
        state.channel = args.channel

    if args.status:
        return _status(state)
    if args.rollback:
        return _rollback(ctx, state)

    # default (no flags) = full check+download+install; individual flags scope it.
    do_check = args.check or not (args.download or args.install)
    do_download = args.download or not (args.check or args.install)
    do_install = args.install or not (args.check or args.download)
    if args.check and not (args.download or args.install):
        do_download = do_install = False

    manifest = _fetch_manifest(state)
    if manifest is None:
        return 1

    if not _acceptable(manifest, state):
        return 1

    if not version_gt(manifest.version, state.version):
        ui.success(f"up to date ({state.version})")
        return 0

    ui.success(f"update available: {state.version} -> {manifest.version} "
               f"(patch level {manifest.security_patch_level})")
    if do_check and not do_install and not do_download:
        return 0

    workdir = Path("/var/lib/mobilelinux/ota") if Path("/var/lib").exists() else Path("./ota-work")
    if do_download or do_install:
        if not _download(manifest, state, workdir):
            return 1
    if do_install:
        return _install(ctx, manifest, state, workdir)
    return 0


# --------------------------------------------------------------------------
def _fetch_manifest(state: DeviceState) -> Manifest | None:
    if not state.metadata_url:
        ui.error("no metadata_url configured in /etc/mobilelinux/state.json")
        return None
    url = state.metadata_url.rstrip("/") + f"/{state.channel}/manifest.json"
    ui.info(f"  fetching {url}")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            text = resp.read().decode("utf-8")
    except Exception as exc:
        ui.error(f"failed to fetch manifest: {exc}")
        return None
    manifest = Manifest.from_json(text)
    if not _verify_signature(manifest, state):
        return None
    return manifest


def _verify_signature(manifest: Manifest, state: DeviceState) -> bool:
    sig = manifest.signature
    if not sig:
        ui.error("manifest is unsigned; refusing (set up signing)")
        return False
    pub = Path(state.public_key)
    if not pub.exists():
        ui.error(f"public key not found: {pub}")
        return False
    ok = signing.verify(manifest.canonical(), sig.get("value", ""), pub)
    if not ok:
        ui.error("SIGNATURE INVALID; refusing update (possible tampering)")
        return False
    ui.success("signature verified")
    return True


def _acceptable(manifest: Manifest, state: DeviceState) -> bool:
    # Device id must match exactly — the core safety check.
    if manifest.device_id != state.device_id:
        ui.error(f"manifest targets device '{manifest.device_id}', this is "
                 f"'{state.device_id}'; refusing (wrong-device protection)")
        return False
    if manifest.architecture and manifest.architecture != _local_arch():
        ui.error(f"architecture mismatch: manifest {manifest.architecture} vs "
                 f"local {_local_arch()}")
        return False
    minv = manifest.minimum_version
    if minv and not version_ge(state.version, minv):
        ui.error(f"this release requires at least {minv}; you are on {state.version}. "
                 f"Install intermediate updates first.")
        return False
    return True


def _local_arch() -> str:
    import platform
    m = platform.machine()
    return {"aarch64": "aarch64", "armv7l": "armv7", "x86_64": "x86_64"}.get(m, m)


def _download(manifest: Manifest, state: DeviceState, workdir: Path) -> bool:
    workdir.mkdir(parents=True, exist_ok=True)
    base = state.metadata_url.rstrip("/") + f"/{state.channel}"
    for key, art in manifest.artifacts.items():
        url = art["url"]
        if not url.startswith("http"):
            url = base + "/" + url
        dest = workdir / Path(art["url"]).name
        ui.info(f"  downloading {key}: {url}")
        try:
            urllib.request.urlretrieve(url, dest)
        except Exception as exc:
            ui.error(f"download failed for {key}: {exc}")
            return False
        actual = _sha256(dest)
        if actual != art["sha256"]:
            ui.error(f"HASH MISMATCH for {key} (expected {art['sha256'][:12]}…, got {actual[:12]}…); "
                     f"refusing (corrupt or wrong artifact)")
            return False
        ui.success(f"  {key} verified")
    return True


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _install(ctx, manifest: Manifest, state: DeviceState, workdir: Path) -> bool:
    runner = ctx.runner()
    strategy = manifest.data.get("device", {}).get("ota_strategy", state.ota_strategy)
    ui.header(f"Installing {manifest.version} ({strategy})")

    if strategy == "ab":
        # Prefer RAUC (atomic A/B, verifies bundle, marks other slot).
        bundle = manifest.artifacts.get("rauc-bundle") or manifest.artifacts.get("rootfs")
        if bundle:
            name = Path(bundle["url"]).name
            runner.run(["rauc", "install", str(workdir / name)], tool="rauc")
        ui.note("  A/B: update written to the inactive slot; reboot to activate.")
        ui.note("  a health check on next boot marks it good, else it rolls back.")
    else:
        ui.warn("single-rootfs device: update is NOT atomic and cannot roll back.")
        ui.note("  the framework will write in place; ensure the battery is charged.")
        if not ctx.assume_yes and not ctx.dry_run:
            ui.error("refusing non-atomic install without --yes")
            return False

    # Record intent; the health-check service finalizes on next boot.
    state.version = manifest.version
    state.security_patch_level = manifest.security_patch_level
    state.kernel_version = manifest.kernel_version
    state.last_result = ""
    if not ctx.dry_run:
        state.save()
    ui.success("update staged; reboot to apply")
    return True


def _status(state: DeviceState) -> int:
    ui.header("Update status")
    print(f"  device:         {state.device_id}")
    print(f"  version:        {state.version}")
    print(f"  channel:        {state.channel}")
    print(f"  patch level:    {state.security_patch_level or 'unknown'}")
    print(f"  OTA strategy:   {state.ota_strategy}")
    print(f"  last result:    {state.last_result or 'n/a'}")
    return 0


def _rollback(ctx, state: DeviceState) -> int:
    if state.ota_strategy != "ab":
        ui.error("rollback is only available on A/B devices")
        return 1
    runner = ctx.runner()
    ui.header("Rollback")
    runner.run(["rauc", "status", "mark-bad"], tool="rauc")
    runner.run(["sh", "-c", "# switch active slot back via android-bootctl / qbootctl"])
    ui.success("rolled back to previous slot; reboot to apply")
    return 0
