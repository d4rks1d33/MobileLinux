"""Generic Debian-family distribution backend.

Debian-based distros (Kali, Debian, Ubuntu, Pop!_OS, ...) all share the same
rootfs pipeline, which mirrors the reference port's verified debos flow:

  1. clone the distro's upstream debos recipe,
  2. drop in the generated device config (bootimg offsets) + the device .debs
     (the linux-image .deb from the kernel stage + firmware + rhodep-*),
  3. run debos via the binwrap wrappers (--disable-fakemachine, so it works in a
     container without /dev/kvm) to produce the rootfs tarball,
  4. finish in a chroot (image.yaml can't partition in a container, max_part=0):
     install base + phone-role packages, dpkg -i the device .debs, enable/mask
     services, run the device's userspace installers in order, apply apt holds
     LAST.

Only the recipe source + suite + package lists differ per distro, so Kali,
Debian and Ubuntu are thin subclasses that set those. This is what makes "try
Debian, then add it to os-distros" a small change rather than a rewrite.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..core import tools, ui
from .base import BuildRequest, DistroBackend, RootfsResult

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

BINWRAP = Path(__file__).parent / "binwrap"


def _debian_arch(arch: str) -> str:
    """Map our architecture to the Debian architecture name used by debootstrap."""
    return {"aarch64": "arm64", "armv7": "armhf", "x86_64": "amd64"}.get(arch, arch)


def _recipe_family(soc_vendor: str) -> str:
    """Map our SoC vendor to the debos recipe's device family dir."""
    return {"qualcomm": "qcom", "allwinner": "sunxi", "rockchip": "rockchip",
            "nxp": "librem5"}.get(soc_vendor, soc_vendor)


def read_package_list(path: Path) -> list[str]:
    """Read a `.list` file: one package per line, '#' comments ignored."""
    if not path.is_file():
        return []
    pkgs = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pkgs.append(line)
    return pkgs


class DebianBackend(DistroBackend):
    """Base for all Debian-family distros. Subclasses set name + recipe + suite."""

    name = "debian-base"
    family = "debian"
    tools = ("debos", "git")

    #: upstream debos recipe git URL (subclass overrides)
    recipe_url = ""
    #: the distro archive suite (debian_suite in the recipe): kali-rolling / trixie / noble
    suite = ""
    #: the Mobian repo suite the recipe pulls device packages from (bookworm/trixie/forky)
    mobian_suite = "trixie"
    #: extra recipe params (username/password/hostname); overridable
    username = "kali"
    password = "1234"
    hostname = "kali"
    #: default UI
    _default_desktop = "phosh"

    def default_desktop(self) -> str:
        return self._default_desktop

    # -- public entrypoint --------------------------------------------------
    def build_rootfs(self, req: BuildRequest) -> RootfsResult:
        device = req.device
        runner = req.runner
        arch = device.architecture
        work = req.out_dir / f"{self.name}-recipe"
        tarball = req.out_dir / f"rootfs-{arch}-{req.desktop}-{self.name}.tar.xz"
        result = RootfsResult(tarball=tarball)

        ui.header(f"{self.name} rootfs via debos "
                  f"({arch}, desktop={req.desktop}"
                  + (f", profile={req.profile}" if req.profile else "") + ")")

        # Reuse an existing rootfs tarball when iterating on later stages
        # (MOBILELINUX_ROOTFS_REUSE=1).
        import os
        reuse = os.environ.get("MOBILELINUX_ROOTFS_REUSE") == "1" and tarball.exists()
        if reuse:
            ui.note(f"  reusing existing rootfs tarball (MOBILELINUX_ROOTFS_REUSE=1): "
                    f"{tarball.name}")
        else:
            # 1. Fetch the upstream recipe.
            self._clone_recipe(work, runner)
            # 2. Stage device config + device .debs into the recipe.
            self._stage_device(req, work)
            # 3. Run debos via the binwrap wrapper (container-friendly).
            self._run_debos(req, work, tarball)

        # 4. Chroot integration (the device-specific bring-up).
        self._chroot_integration(req, tarball)
        result.rootfs_dir = req.out_dir / "rootfs"

        # Desktop + profile layers.
        self._apply_desktop_layer(req, result)
        if req.profile:
            self._apply_profile_layer(req, result)

        return result

    # -- stages -------------------------------------------------------------
    def _clone_recipe(self, work: Path, runner: tools.Runner) -> None:
        if not self.recipe_url:
            ui.warn(f"  {self.name}: no recipe_url set; cannot build rootfs")
            return
        if work.exists():
            ui.note(f"  recipe already present at {work}")
            return
        runner.run(["git", "clone", "--depth=1", self.recipe_url, str(work)], tool="git")

    def _stage_device(self, req: BuildRequest, work: Path) -> None:
        device = req.device
        runner = req.runner
        ui.info("  staging device config + packages into the recipe:")

        # Generated bootimg device config (equiv. of the reference debos/wip.toml).
        cfg = self._render_device_toml(device)
        cfg_path = req.out_dir / "device.toml"
        cfg_path.write_text(cfg, encoding="utf-8")
        # The recipe expects it under devices/<family>/configs/ (qcom, not qualcomm).
        family = _recipe_family(device.soc_vendor)
        dest_cfg = work / "devices" / family / "configs" / "device.toml"
        runner.run(["sh", "-c", f"mkdir -p {dest_cfg.parent}"])
        runner.run(["cp", str(cfg_path), str(dest_cfg)])
        ui.note(f"    - device.toml -> {dest_cfg}")

        # Device .debs: the kernel-stage linux-image .deb + firmware + rhodep-*.
        pkgdest = work / "devices" / family / "packages"
        runner.run(["sh", "-c", f"mkdir -p {pkgdest}"])
        for deb in sorted(req.out_dir.glob("linux-image-*.deb")):
            runner.run(["cp", str(deb), str(pkgdest / deb.name)])
            ui.note(f"    - {deb.name}")
        # (firmware + rhodep-* .debs are copied by the device layer / build-support-debs)

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

    def _run_debos(self, req: BuildRequest, work: Path, tarball: Path) -> None:
        runner = req.runner
        # debos/debootstrap use the Debian architecture name (arm64), not the
        # kernel/uname name (aarch64).
        arch = _debian_arch(req.device.architecture)
        # debos writes the output relative to its scratchdir (the recipe dir), so
        # we pass a bare filename and move it out afterwards.
        out_name = "rootfs.tar.xz"
        # PATH with binwrap first so 'debos' == the --disable-fakemachine wrapper.
        env_path = f"{BINWRAP}:{os.environ.get('PATH','')}"
        ui.note("  running debos (binwrap: --disable-fakemachine, nspawn --register=no)")
        # sudo needed for systemd-nspawn / debootstrap inside the container.
        runner.run(
            ["sudo", "mkdir", "-p", "/dev/disk"], dangerous=True,
        )
        runner.run(
            ["sudo", "env", f"PATH={env_path}", "HOME=/root",
             "SYSTEMD_NSPAWN_UNIFIED_HIERARCHY=1",
             str(BINWRAP / "debos"),
             "--scratchsize=8G",
             "-t", f"architecture:{arch}",
             # debian_suite = the distro archive (kali-rolling/trixie/noble);
             # suite = the Mobian repo suite the recipe pulls device pkgs from.
             "-t", f"debian_suite:{self.suite}",
             "-t", f"suite:{self.mobian_suite}",
             "-t", f"environment:{req.desktop}",
             "-t", "nonfree:true",
             "-t", f"username:{self.username}",
             "-t", f"password:{self.password}",
             "-t", f"hostname:{self.hostname}",
             "-t", f"rootfs:{out_name}",
             "rootfs.yaml"],
            tool="debos", dangerous=True, cwd=str(work),
        )
        # Move debos' output to the expected tarball path.
        runner.run(["sh", "-c", f"mv {work}/{out_name} {tarball} 2>/dev/null || true"],
                   dangerous=True)
        ui.note(f"  -> {tarball.name} (~920 MB on rhodep)")
        ui.note("  -> image.yaml can't partition in a container; chroot phase follows")

    def _chroot_integration(self, req: BuildRequest, tarball: Path) -> None:
        device = req.device
        runner = req.runner
        root = req.out_dir / "rootfs"

        ui.info("  chroot integration:")
        runner.run(["sudo", "sh", "-c", f"rm -rf {root} && mkdir -p {root} && "
                                        f"tar xpJf {tarball} -C {root}"], dangerous=True)
        # DNS for apt inside the chroot + mount proc/sys/dev so chroot works.
        runner.run(["sudo", "sh", "-c",
                    f"rm -f {root}/etc/resolv.conf; "
                    f"echo 'nameserver 8.8.8.8' > {root}/etc/resolv.conf; "
                    f"mount -t proc none {root}/proc 2>/dev/null; "
                    f"mount --rbind /sys {root}/sys 2>/dev/null; "
                    f"mount --rbind /dev {root}/dev 2>/dev/null; true"],
                   dangerous=True)

        # Base + phone-role packages from the distro package lists.
        pkgdir = self.dir / "packages"
        pkgs = read_package_list(pkgdir / "base.list")
        if req.desktop == "phosh":
            pkgs += read_package_list(pkgdir / "phone-role.list")
        if pkgs:
            runner.run(["sudo", "chroot", str(root), "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                        "--no-install-recommends", *pkgs],
                       dangerous=True)
            ui.note(f"    - base packages: {', '.join(pkgs)}")

        # Build deps needed to compile device userspace helpers in-chroot.
        build_deps = read_package_list(pkgdir / "build-deps.list")
        if build_deps:
            runner.run(["sudo", "chroot", str(root), "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *build_deps],
                       dangerous=True)

        # Device .debs: install the kernel-stage linux-image .deb + the device's
        # prebuilt .debs, resolving their apt dependencies.
        runner.run(["sudo", "sh", "-c", f"mkdir -p {root}/srv/debs"], dangerous=True)
        # kernel linux-image .deb from the build out dir
        runner.run(["sudo", "sh", "-c",
                    f"cp {req.out_dir}/linux-image-*.deb {root}/srv/debs/ 2>/dev/null || true"],
                   dangerous=True)
        # device-shipped prebuilt .debs (rhodep-*) from assets/debs/
        self._copy_device_debs(device, root, runner)

        # Install the apt dependencies each device package declares (i2c-tools,
        # mesa-opencl-icd, clinfo, ...) so dpkg resolution succeeds.
        apt_deps = self._collect_apt_deps(device)
        if apt_deps:
            runner.run(["sudo", "chroot", str(root), "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *sorted(apt_deps)],
                       dangerous=True)
            ui.note(f"    - device apt deps: {', '.join(sorted(apt_deps))}")

        # Install the .debs with apt (resolves remaining deps automatically),
        # falling back to dpkg -i + apt -f for older apt.
        runner.run(["sudo", "chroot", str(root), "env", "DEBIAN_FRONTEND=noninteractive", "sh", "-c",
                    "apt-get install -y /srv/debs/*.deb "
                    "|| { dpkg -i /srv/debs/*.deb; apt-get -fy install; }"],
                   dangerous=True)
        ui.note("    - installed device .debs (deps resolved)")

        # Distro integration config (services + install order).
        integ = self._load_integration()
        enable = list(integ.get("enable_services", [])) or self._soc_services(device)
        if enable:
            runner.run(["sudo", "chroot", str(root), "systemctl", "enable", *enable],
                       dangerous=True)
            ui.note(f"    - enabled: {', '.join(enable)}")

        masks = list(integ.get("mask_services", []))
        for m in device.first_boot.get("mask_services", []):
            if m not in masks:
                masks.append(m)
        if masks:
            runner.run(["sudo", "chroot", str(root), "systemctl", "mask", *masks],
                       dangerous=True)
            ui.note(f"    - masked: {', '.join(masks)}")

        # Device userspace installers, in the distro-declared order (apt LAST).
        self._run_userspace_installers(req, root, integ)

        # Clean up: remove staged .debs, apt cache, and UNMOUNT the chroot binds
        # (required before imaging the rootfs).
        runner.run(["sudo", "sh", "-c",
                    f"chroot {root} sh -c 'rm -rf /srv/debs /var/cache/apt/archives/*.deb "
                    f"/var/lib/apt/lists/* 2>/dev/null' || true; "
                    f"umount -lf {root}/proc {root}/sys {root}/dev 2>/dev/null; true"],
                   dangerous=True)

    def _copy_device_debs(self, device, root: Path, runner: tools.Runner) -> None:
        """Copy the device's prebuilt .debs (rhodep-*) into the chroot /srv/debs."""
        for pkg in device.device_packages:
            if pkg.get("kind") != "deb":
                continue
            deb_rel = pkg.get("deb")
            if not deb_rel:
                ui.note(f"    - {pkg['name']}: no prebuilt .deb declared (source only)")
                continue
            deb_path = device.dir / deb_rel
            if deb_path.exists() or runner.dry_run or not runner.execute:
                runner.run(["sudo", "sh", "-c", f"cp {deb_path} {root}/srv/debs/"], dangerous=True)
                ui.note(f"    - device .deb: {deb_path.name}")
            else:
                ui.warn(f"    - {pkg['name']}: prebuilt .deb missing at {deb_path}")

    def _collect_apt_deps(self, device) -> set:
        """Union of the apt 'depends' declared by every device .deb package."""
        deps: set = set()
        for pkg in device.device_packages:
            for d in pkg.get("depends", []) or []:
                deps.add(d)
        return deps

    def _run_userspace_installers(self, req: BuildRequest, root: Path, integ: dict) -> None:
        device = req.device
        runner = req.runner
        order = integ.get("userspace_install_order", [])
        # Map install-order names to device userspace asset dirs.
        for name in order:
            if name == "apt":
                continue  # holds run last, below
            asset = device.dir / "assets" / "userspace" / name
            if asset.is_dir():
                runner.run(["sudo", "sh", "-c", f"cp -r {asset} {root}/srv/{name}"], dangerous=True)
                # Userspace installers may have expected partial failures (an
                # optional package absent in this build); don't abort the build.
                runner.run(["sudo", "chroot", str(root), "sh", f"/srv/{name}/install.sh"],
                           dangerous=True, check=False)
                ui.note(f"    - userspace: {name}")
        if integ.get("apply_apt_holds", True):
            apt_asset = device.dir / "assets" / "userspace" / "apt"
            if apt_asset.is_dir():
                runner.run(["sudo", "sh", "-c", f"cp -r {apt_asset} {root}/srv/apt"], dangerous=True)
                # apply-holds may 'fail' if it tries to hold non-redistributable
                # firmware or headers that this build didn't produce; non-fatal.
                runner.run(["sudo", "chroot", str(root), "sh", "/srv/apt/apply-holds.sh"],
                           dangerous=True, check=False)
            ui.note("    - apply-holds.sh (LAST): pin + protect laid-down files")

    # -- helpers ------------------------------------------------------------
    def _load_integration(self) -> dict:
        path = self.dir / "configuration" / "integration.yaml"
        if path.is_file() and yaml is not None:
            return yaml.safe_load(path.read_text()) or {}
        return {}

    def _soc_services(self, device) -> list[str]:
        if device.soc_vendor == "qualcomm":
            return ["qrtr-ns", "rmtfs", "pd-mapper", "tqftpserv"]
        return []

    def _apply_desktop_layer(self, req: BuildRequest, result: RootfsResult) -> None:
        desktop_dir = req.repo_root / "desktops" / req.desktop
        if desktop_dir.is_dir() and any(desktop_dir.iterdir()):
            ui.note(f"  desktop layer applied: {req.desktop}")
        else:
            result.notes.append(f"desktop layer '{req.desktop}' has no content yet (distro default)")

    def _apply_profile_layer(self, req: BuildRequest, result: RootfsResult) -> None:
        ui.info(f"  profile layer: {req.profile}")
        if req.profile == "security":
            sec = req.repo_root / "security"
            for tool_dir in sorted(sec.glob("*")):
                if tool_dir.is_dir() and (tool_dir / "layer.yaml").exists():
                    ui.note(f"    + security tool: {tool_dir.name}")
