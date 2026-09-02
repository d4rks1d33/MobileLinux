"""postmarketOS distribution backend.

pmOS is Alpine-based, so its rootfs is built directly with **pmbootstrap**
(``pmbootstrap install``) — no debos, no .deb conversion. This is the clean,
pentest-free baseline that shares the exact same kernel work as Kali (the
``pmos`` kernel flavor = the base config, no NetHunter delta).

pmOS is also the device-support *provider* for every device (it supplies the
shared kernel aport); this backend is its role as a build TARGET.
"""

from __future__ import annotations

from pathlib import Path

from ..core import ui
from .base import BuildRequest, DistroBackend, RootfsResult


class PostmarketosBackend(DistroBackend):
    name = "postmarketos"
    tools = ("pmbootstrap",)

    def default_desktop(self) -> str:
        return "phosh"

    def build_rootfs(self, req: BuildRequest) -> RootfsResult:
        device = req.device
        runner = req.runner
        codename = device.codename
        result = RootfsResult()

        ui.header(f"postmarketOS rootfs via pmbootstrap "
                  f"(desktop={req.desktop})")

        # pmbootstrap is configured for the device/kernel/UI, then install builds
        # the rootfs image directly (it carries its own boot/root subpartitions).
        ui.note("  pmbootstrap builds Alpine + the pmos kernel flavor + UI, then "
                "images the rootfs itself (no debos/chroot phase).")
        runner.run(
            ["pmbootstrap", "config", "device", codename],
            tool="pmbootstrap", dangerous=True,
        )
        runner.run(
            ["pmbootstrap", "config", "ui", req.desktop],
            tool="pmbootstrap", dangerous=True,
        )
        runner.run(
            ["pmbootstrap", "install", "--split"],
            tool="pmbootstrap", dangerous=True,
        )
        ui.note("  rootfs image lands under ~/.local/var/pmbootstrap/chroot_native/"
                "home/pmos/rootfs/{}.img".format(codename))

        # Optional profile layer (pmOS security tooling is minimal; most security
        # tools live in security/ and target Debian/Kali).
        if req.profile:
            result.notes.append(
                f"profile '{req.profile}' has limited support on pmOS "
                f"(security tools target Debian/Kali); applied best-effort")

        result.notes.append("pmOS installs the kernel as an apk (no linux-image .deb)")
        return result
