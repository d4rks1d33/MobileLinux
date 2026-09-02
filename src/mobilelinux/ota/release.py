"""`mobilelinux release <device> --version X` — produce a signed release.

Pipeline (reproducible):
  1. ensure build artifacts exist (or build them),
  2. generate the SBOM,
  3. compute the security patch level,
  4. assemble the OTA manifest (device id/arch/min-version + artifact hashes),
  5. sign the manifest (release key; dev key for testing),
  6. write release/ tree ready to publish (GitHub Releases / static HTTP).

The release NEVER embeds private keys. Publishing is a separate step (upload
the release/ directory as assets).
"""

from __future__ import annotations

import datetime
from pathlib import Path

from ..core import ui
from ..core.model import Device
from ..installer.artifacts import ArtifactSet
from .manifest import Manifest
from . import signing
from .sbom import generate_sbom


def release_command(ctx, device: Device, *, version: str, channel: str) -> int:
    runner = ctx.runner()
    out_dir = ctx.repo.out_dir / device.id
    release_dir = ctx.repo.out_dir / "releases" / device.id / version
    release_dir.mkdir(parents=True, exist_ok=True)

    ui.header(f"Release {device.id} {version} ({channel})")

    # 1. artifacts
    if not (out_dir / "artifacts.json").exists():
        ui.warn("no build artifacts; run `mobilelinux build` first (continuing with placeholders)")
        aset = ArtifactSet(device=device.id)
    else:
        aset = ArtifactSet.load(out_dir)
        problems = aset.verify(out_dir)
        if problems:
            ui.error("artifact verification failed:")
            for p in problems:
                print(f"    {p}")
            return 1

    # 2. SBOM
    sbom_path = release_dir / "sbom.cyclonedx.json"
    generate_sbom(out_dir / "rootfs", sbom_path, device=device.id, version=version, runner=runner)

    # 3. security patch level (today, unless overridden by a real build date)
    patch_level = _security_patch_level()

    # 4. manifest
    manifest = _build_manifest(device, aset, version, channel, patch_level, sbom_path)

    # 5. sign
    keys = ctx.repo.root / "keys"
    priv = keys / f"{channel}.ed25519.key"
    pub = keys / f"{channel}.ed25519.pub"
    if priv.exists():
        body = manifest.canonical()
        sig = signing.sign(body, priv)
        manifest.data["signature"] = {
            "algorithm": "ed25519",
            "key_id": f"{channel}",
            "value": sig,
        }
        ui.success("manifest signed")
    else:
        ui.warn(f"no signing key at {priv}; manifest is UNSIGNED")
        ui.note(f"  generate one with: mobilelinux keygen --channel {channel}")

    # 6. write release tree
    manifest_path = release_dir / "manifest.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")

    # Also copy the artifacts referenced (or note they must be uploaded).
    _stage_artifacts(aset, out_dir, release_dir, runner)

    ui.header("Result")
    print(f"  {ui.green('\u2713')} manifest:  {manifest_path}")
    print(f"  {ui.green('\u2713')} sbom:      {sbom_path}")
    print(f"  version={version} channel={channel} patch_level={patch_level}")
    ui.note("  publish: upload the release/ directory contents as release assets "
            "(GitHub Releases / static HTTP). The client only needs the manifest URL + public key.")
    if runner.missing.any:
        runner.missing.report()
    return 0


def _security_patch_level() -> str:
    return datetime.date.today().isoformat()


def _build_manifest(device: Device, aset: ArtifactSet, version: str, channel: str,
                    patch_level: str, sbom_path: Path) -> Manifest:
    artifacts = {}
    for key, art in aset.artifacts.items():
        artifacts[key] = {
            "url": art.filename,          # relative; rewritten to asset URL on publish
            "sha256": art.sha256,
            "size": art.size,
            "type": art.role or key,
        }
    data = {
        "manifest_version": 1,
        "release": {
            "version": version,
            "channel": channel,
            "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "distro": aset.distro,
            "desktop": aset.desktop,
        },
        "device": {
            "id": device.id,
            "codename": device.codename,
            "architecture": device.architecture,
            "minimum_version": "0.0.0",
            "ota_strategy": device.ota_strategy,
        },
        "artifacts": artifacts,
        "security": {
            "security_patch_level": patch_level,
            "kernel_version": device.kernel.get("version", ""),
            "sbom_url": sbom_path.name,
            "fixed_cves": [],
        },
    }
    return Manifest(data)


def _stage_artifacts(aset: ArtifactSet, out_dir: Path, release_dir: Path, runner) -> None:
    for key, art in aset.artifacts.items():
        src = out_dir / art.filename
        if src.exists():
            runner.run(["cp", str(src), str(release_dir / art.filename)])
        else:
            ui.note(f"  artifact '{key}' ({art.filename}) not present; upload it alongside the manifest")
