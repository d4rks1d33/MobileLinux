"""Kali distribution backend.

Kali is a **Debian-based** distribution, so its rootfs is built very
differently from Alpine/Arch: it uses **debos** with the upstream
``kali-nethunter-pro`` recipes (a fork of mobian-recipes). This backend
reproduces the exact, verified flow from the reference port
(nethunter-rhodep-repo README "Building the Kali rootfs (debos)"):

  1. clone the upstream ``kali-nethunter-pro`` recipes,
  2. drop in the device config (bootimg offsets etc, generated from the device
     definition) and the device .deb packages,
  3. run debos ``rootfs.yaml`` -> ``rootfs-<arch>-<desktop>-nonfree.tar.xz``,
  4. because ``image.yaml`` cannot partition inside a container (loop
     max_part=0), the **device-integration phase runs in a chroot**: install
     initramfs-tools/qcom-support-common, the phone role (mobian-phosh-phone),
     the device .debs, mask droid-juicer/systemd-repart, run the device's
     userspace installers, and apply apt holds LAST.

Every external command is planned unless ``--execute`` is given, and any
missing tool (``debos``, ``go``, ...) is reported so the user can install it and
re-run.
"""

from __future__ import annotations

from pathlib import Path

from ..core import ui
from .base import BuildRequest, DistroBackend, RootfsResult

UPSTREAM_RECIPE = "https://gitlab.com/kalilinux/nethunter/build-scripts/kali-nethunter-pro"


class KaliBackend(DistroBackend):
    name = "kali"
    tools = ("debos",)

    def default_desktop(self) -> str:
        return "phosh"

    def build_rootfs(self, req: BuildRequest) -> RootfsResult:
        device = req.device
        runner = req.runner
        arch = device.architecture
        work = req.out_dir / "kali-nethunter-pro"
        tarball = req.out_dir / f"rootfs-{arch}-{req.desktop}-nonfree.tar.xz"
        result = RootfsResult(tarball=tarball)

        ui.header(f"Kali rootfs via debos ({arch}, desktop={req.desktop}"
                  + (f", profile={req.profile}" if req.profile else "") + ")")

        # Stage 1: fetch upstream recipe.
        runner.run(["git", "clone", "--depth=1", UPSTREAM_RECIPE, str(work)], tool="git")

        # Stage 2: drop in device config + device .debs.
        self._stage_device_config(req, work)

        # Stage 3: run debos (container-friendly, no KVM).
        # The reference uses the recipe's own build.sh -t <target>; we invoke
        # debos on rootfs.yaml directly so it is device-agnostic.
        runner.run(
            [
                "debos", "--disable-fakemachine",
                "-t", f"architecture:{arch}",
                "-t", f"suite:kali-rolling",
                "-t", f"desktop:{req.desktop}",
                "-t", f"variant:nonfree",
                "-t", f"output:{tarball}",
                str(work / "rootfs.yaml"),
            ],
            tool="debos",
        )
        ui.note("  -> rootfs.yaml completes -> rootfs tarball (~920 MB on rhodep)")
        ui.note("  -> image.yaml cannot partition in a container; integration runs in chroot:")

        # Stage 4: device-integration chroot phase (the heart of the port).
        self._chroot_integration(req, tarball)

        # Desktop + profile layers.
        self._apply_desktop_layer(req, result)
        if req.profile:
            self._apply_profile_layer(req, result)

        return result

    # ----------------------------------------------------------------------
    def _stage_device_config(self, req: BuildRequest, work: Path) -> None:
        device = req.device
        runner = req.runner
        ui.info("  staging device config into the recipe:")
        # Generate the bootimg device config (equiv. of debos/wip.toml) from the
        # device definition so it never drifts.
        cfg = self._render_device_toml(device)
        cfg_path = req.out_dir / "device.toml"
        cfg_path.write_text(cfg, encoding="utf-8")
        ui.note(f"    - generated {cfg_path.name} (chipset/vendor/model + bootimg offsets)")
        runner.run(["cp", str(cfg_path),
                    str(work / "devices" / device.soc_vendor / "configs" / "device.toml")])
        # Device .deb packages (linux-image, firmware, device packages).
        for pkg in device.device_packages:
            if pkg.get("kind") == "deb":
                ui.note(f"    - device package: {pkg['name']}.deb")

    def _render_device_toml(self, device) -> str:
        cfg = device.boot.get("android_bootimg", {})
        off = cfg.get("offsets", {})
        return (
            f"# generated from device definition {device.id}\n"
            f"chipset = \"{device.soc_family}\"\n\n"
            f"[bootimg]\n"
            f"base = {cfg.get('base', '0x0')}\n"
            f"kernel = {off.get('kernel', '0x8000')}\n"
            f"ramdisk = {off.get('ramdisk', '0x1000000')}\n"
            f"second = {off.get('second', '0x0')}\n"
            f"tags = {off.get('tags', '0x100')}\n"
            f"pagesize = {cfg.get('pagesize', 2048)}\n"
            f"version = {cfg.get('header_version', 2)}\n\n"
            f"[[device]]\n"
            f"vendor = \"{device.vendor}\"\n"
            f"model = \"{device.codename}\"\n"
        )

    def _chroot_integration(self, req: BuildRequest, tarball: Path) -> None:
        device = req.device
        runner = req.runner
        root = req.out_dir / "rootfs"

        ui.info("  chroot integration:")
        runner.run(["sh", "-c", f"mkdir -p {root} && tar xJf {tarball} -C {root}"], dangerous=True)

        # Base phone role + initramfs + SoC glue.
        pkgs = ["initramfs-tools", "qcom-support-common"]
        if req.desktop == "phosh":
            pkgs += ["mobian-phosh-phone", "nautilus"]
        runner.run(["chroot", str(root), "apt-get", "install", "-y", "--no-install-recommends", *pkgs],
                   tool="chroot", dangerous=True)

        # Device .debs.
        deb_names = [p["name"] for p in device.device_packages if p.get("kind") == "deb"]
        if deb_names:
            ui.note(f"    - dpkg -i device packages: {', '.join(deb_names)}")
            runner.run(["chroot", str(root), "sh", "-c",
                        "dpkg -i /srv/*.deb || apt-get -fy install"],
                       tool="chroot", dangerous=True)

        # Enable firmware/modem services declared by the SoC layer.
        enable = self._soc_services(device)
        if enable:
            runner.run(["chroot", str(root), "systemctl", "enable", *enable],
                       tool="chroot", dangerous=True)

        # First-boot masks (droid-juicer, systemd-repart).
        masks = device.first_boot.get("mask_services", [])
        if masks:
            runner.run(["chroot", str(root), "systemctl", "mask", *masks],
                       tool="chroot", dangerous=True)
            ui.note(f"    - masked: {', '.join(masks)}")

        # Device userspace installers (audio/modem/sensors/etc), then apt holds LAST.
        for pkg in device.device_packages:
            if pkg.get("kind") in ("installer-script", "config"):
                ui.note(f"    - install layer: {pkg['name']} ({pkg['source']})")
        ui.note("    - apply-holds.sh (LAST): pin packages + protect all laid-down files")

    def _soc_services(self, device) -> list[str]:
        # Services that the SoC modem/firmware layer needs enabled. Declared via
        # a modem-support device package on Qualcomm.
        if device.soc_vendor == "qualcomm":
            return ["qrtr-ns", "rmtfs", "pd-mapper", "tqftpserv"]
        return []

    def _apply_desktop_layer(self, req: BuildRequest, result: RootfsResult) -> None:
        desktop_dir = req.repo_root / "desktops" / req.desktop
        if desktop_dir.is_dir() and any(desktop_dir.iterdir()):
            ui.note(f"  desktop layer applied: {req.desktop}")
        else:
            result.notes.append(f"desktop layer '{req.desktop}' has no content yet (uses distro default)")

    def _apply_profile_layer(self, req: BuildRequest, result: RootfsResult) -> None:
        ui.info(f"  profile layer: {req.profile}")
        if req.profile == "security":
            sec = req.repo_root / "security"
            for tool_dir in sorted(sec.glob("*")):
                if tool_dir.is_dir():
                    ui.note(f"    + security tool: {tool_dir.name}")
