"""SBOM generation for a release.

Prefers ``syft`` (reads a Debian/Kali rootfs and emits SPDX or CycloneDX). When
syft is absent, falls back to reading ``dpkg`` status from the rootfs to emit a
minimal CycloneDX document, so a release always has *some* SBOM answering
"which packages/versions ship in this release?".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..core import tools, ui


def generate_sbom(rootfs_dir: Path | None, out: Path, *, device: str, version: str,
                  runner: tools.Runner, fmt: str = "cyclonedx-json") -> Path:
    """Generate an SBOM to ``out``. Returns the path."""
    if tools.have("syft") and rootfs_dir and rootfs_dir.exists():
        runner.run(["syft", f"dir:{rootfs_dir}", "-o", f"{fmt}={out}"], tool="syft")
        return out

    # Fallback: parse dpkg status if a rootfs is present, else emit an empty shell.
    packages = []
    status = None
    if rootfs_dir:
        for cand in (rootfs_dir / "var/lib/dpkg/status",):
            if cand.exists():
                status = cand
                break
    if status:
        packages = _parse_dpkg_status(status)
        ui.note(f"  syft not found; generated minimal SBOM from {status} ({len(packages)} packages)")
    else:
        ui.note("  syft not found and no rootfs available; wrote SBOM shell (install syft for a full SBOM)")
        runner.missing.check("syft")

    doc = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": {"type": "operating-system",
                          "name": f"mobilelinux-{device}", "version": version},
            "tools": [{"name": "mobilelinux", "version": "0.1.0"}],
        },
        "components": [
            {"type": "library", "name": p["name"], "version": p["version"],
             "purl": f"pkg:deb/debian/{p['name']}@{p['version']}?arch={p.get('arch','')}"}
            for p in packages
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return out


def _parse_dpkg_status(path: Path) -> list[dict]:
    pkgs = []
    cur = {}
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            if cur.get("name") and cur.get("version"):
                pkgs.append(cur)
            cur = {}
            continue
        if line.startswith("Package:"):
            cur["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Version:"):
            cur["version"] = line.split(":", 1)[1].strip()
        elif line.startswith("Architecture:"):
            cur["arch"] = line.split(":", 1)[1].strip()
    if cur.get("name") and cur.get("version"):
        pkgs.append(cur)
    return pkgs
