"""Import a device definition from postmarketOS pmaports.

pmOS stores device support as ``device/<tier>/device-<vendor>-<codename>/`` with
a shell-sourceable ``deviceinfo`` + APKBUILD. This importer parses ``deviceinfo``
and produces a *draft* ``device.yaml``.

What imports automatically: identity, arch, chassis, flash method + boot
geometry, DTB, cmdline, kernel/firmware hints. What CANNOT be imported (and is
left as TODO/untested): the per-feature hardware status matrix (that lives on
the wiki as prose) and install quirks. The porter fills those in with evidence.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core import ui

# Map pmOS flash_method -> our install strategy (best-effort; porter confirms).
_FLASH_TO_STRATEGY = {
    "fastboot": "fastboot",
    "heimdall-bootimg": "heimdall",
    "heimdall-isorec": "heimdall-isorec",
    "uuu": "uuu",
    "rkdeveloptool": "custom",
    "0xffff": "custom",
    "none": "sdcard",
    "mtkclient": "mtkclient",
}


def parse_deviceinfo(text: str) -> dict[str, str]:
    info: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^deviceinfo_([a-z0-9_]+)="?(.*?)"?$', line)
        if m:
            info[m.group(1)] = m.group(2)
    return info


def deviceinfo_to_schema(info: dict[str, str]) -> dict:
    codename_full = info.get("codename", "")
    # pmOS codenames are vendor-codename; split heuristically.
    vendor = (info.get("manufacturer", "") or codename_full.split("-")[0]).lower()
    codename = codename_full
    short = codename_full.split("-", 1)[-1] if "-" in codename_full else codename_full

    arch = info.get("arch", "aarch64")
    flash_method = info.get("flash_method", "fastboot")
    strategy = _FLASH_TO_STRATEGY.get(flash_method, "custom")
    header_version = int(info.get("header_version", "2") or "2")
    pagesize = int(info.get("flash_pagesize", "2048") or "2048")

    data = {
        "schema_version": 1,
        "id": short,
        "vendor": vendor,
        "model": info.get("name", codename_full),
        "codename": codename_full,
        "aliases": [codename_full],
        "architecture": arch,
        "chassis": info.get("chassis", "handset"),
        "maturity": "testing",
        "soc": {
            "vendor": "unknown",
            "family": "unknown",
            "marketing_name": "",
        },
        "kernel": {
            "type": "mainline",
            "version": "unknown",
            "provider": {
                # If the device is upstream in pmaports, set upstreamed: true and
                # point pmaports_ref at it. If it's your own not-yet-merged port,
                # set kind: custom and source: <your repo>.
                "kind": "postmarketos",
                "upstreamed": False,
                "source": "https://gitlab.com/postmarketOS/pmaports",
                "linux_pkg": f"linux-{codename_full}",
                "device_pkg": f"device-{codename_full}",
            },
            "flavors": {
                "pmos": {"config_fragment": "", "distros": ["postmarketos"]},
            },
            "build": {"method": "pmbootstrap"},
        },
        "device_tree": {
            "dtb": (info.get("dtb", "") + ".dtb") if info.get("dtb") else "",
            "append_dtb": info.get("append_dtb", "false") == "true",
            "source": "in-kernel",
        },
        "hardware": {
            # Statuses are NOT known from deviceinfo; mark untested for the porter.
            f: {"status": "untested"} for f in (
                "display", "touchscreen", "gpu", "storage", "usb", "wifi",
                "bluetooth", "audio", "battery", "charging")
        },
        "boot": {
            "method": "android-bootimg" if info.get("generate_bootimg") == "true" else "uboot-extlinux",
            "android_bootimg": {
                "header_version": header_version,
                "pagesize": pagesize,
                "base": info.get("flash_offset_base", "0x00000000"),
                "offsets": {
                    "kernel": info.get("flash_offset_kernel", "0x00008000"),
                    "ramdisk": info.get("flash_offset_ramdisk", "0x01000000"),
                    "tags": info.get("flash_offset_tags", "0x00000100"),
                    "second": info.get("flash_offset_second", "0x00000000"),
                },
                "cmdline": info.get("kernel_cmdline", ""),
            },
            "initramfs": {"type": "postmarketos"},
        },
        "storage": {
            "rootfs_layout": "gpt-in-partition" if info.get("rootfs_image_sector_size") == "4096" else "plain",
            "rootfs_sector_size": int(info.get("rootfs_image_sector_size", "512") or "512"),
        },
        "install": {
            "strategy": strategy,
            "ab_slots": False,
            "unlock_required": flash_method == "fastboot",
            "steps": [
                {"action": "message", "description": f"imported from pmaports (flash_method={flash_method}); porter must complete install.steps"}
            ],
        },
        "ota": {"strategy": "single-rootfs", "rollback": False},
        "tests": ["boot", "display", "touch", "storage", "usb", "wifi"],
        "sources": {
            "imported_from": "pmaports",
            "notes": "AUTO-IMPORTED DRAFT. Hardware statuses are 'untested' — fill them in from the wiki + real tests. Verify install.steps and SoC.",
        },
    }
    if info.get("header_version") is None and data["boot"]["method"] != "android-bootimg":
        data["boot"].pop("android_bootimg", None)
    return data


def import_command(ctx, source: str) -> int:
    import yaml

    # source may be a path to a deviceinfo, a device dir, or (future) a codename.
    path = Path(source)
    if path.is_dir():
        path = path / "deviceinfo"
    if not path.is_file():
        ui.error(f"deviceinfo not found at {source}")
        ui.note("  pass a path to a pmaports device directory or its deviceinfo file")
        return 1

    info = parse_deviceinfo(path.read_text())
    data = deviceinfo_to_schema(info)

    vendor = data["vendor"]
    codename = data["codename"]
    out_dir = ctx.repo.devices_dir / vendor / data["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "device.yaml"
    if out_file.exists():
        ui.error(f"{out_file} already exists; refusing to overwrite")
        return 1
    out_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    ui.success(f"imported draft: {out_file}")
    ui.warn("this is a DRAFT: hardware statuses are 'untested' and install.steps "
            "are incomplete. Fill them in from the wiki and real tests, then "
            "`mobilelinux validate " + data["id"] + "`.")
    return 0
