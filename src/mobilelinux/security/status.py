"""`mobilelinux security-status` — show version, security patch level, CVEs.

On-device it reads the local OTA state for version/patch-level, and (when
available) uses ``debsecan`` to map installed Debian/Kali packages to known
CVEs. Off-device it reports what it can from the repo/state and explains how to
get the full picture.
"""

from __future__ import annotations

from ..core import tools, ui
from ..ota.state import DeviceState


def security_status_command(ctx) -> int:
    state = DeviceState.load()

    ui.header("MobileLinux")
    print(f"  Version:              {state.version}")
    print(f"  Security patch level: {state.security_patch_level or 'unknown'}")
    print(f"  Kernel:               {state.kernel_version or 'unknown'}")
    print(f"  Device:               {state.device_id or 'unknown'}")

    ui.header("Components")
    print(f"  {ui.green('\u2713')} Rootfs   (managed by OTA)")
    print(f"  {ui.green('\u2713')} Kernel   ({state.kernel_version or 'unknown'})")

    # CVE mapping via debsecan (Debian/Kali map installed dpkg -> CVEs).
    ui.header("Known affected CVEs")
    if tools.have("debsecan"):
        out = tools.Runner(execute=True, verbose=False).capture(
            ["debsecan", "--format", "packages"], tool="debsecan")
        if out is not None:
            cves = [l for l in out.splitlines() if l.strip()]
            n = len(cves)
            if n == 0:
                print(f"  {ui.green('0')}")
            else:
                print(f"  {ui.red(str(n))}")
                for line in cves[:10]:
                    print(f"    {line}")
                if n > 10:
                    ui.note(f"    … and {n - 10} more")
        else:
            print("  (debsecan produced no output)")
    else:
        ui.note("  debsecan not installed; cannot enumerate CVEs here.")
        ui.note("  install: apt install debsecan   (Debian/Kali)")
        tools.Runner().missing.check("debsecan")

    ui.header("How updates work")
    ui.note("  Userspace security fixes come from the Debian/Kali archive (apt).")
    ui.note("  Atomic OS/kernel updates come via `mobilelinux update` (signed OTA).")
    ui.note("  See docs/security-updates.md and docs/cve-management.md.")
    return 0
