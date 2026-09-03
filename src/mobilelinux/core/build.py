"""`mobilelinux build <device> --distro <d>` — build orchestration.

Pipeline (each stage leaves the project in a usable state / dry-runs cleanly):

    kernel  ->  rootfs (distro backend)  ->  rootfs image  ->  boot image
            ->  rescue image (if the install strategy needs one)  ->  artifacts.json

Real tools are used when present; otherwise the exact commands are printed and
the missing tools are reported so the user can install them and re-run.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from . import images, tools, ui
from .errors import BuildError
from .model import Device
from ..distros import get_backend
from ..installer.artifacts import ArtifactSet


def _is_debian_based(ctx, distro: str) -> bool:
    """True if the distro's family is debian (consumes .deb kernels)."""
    import yaml
    dpath = ctx.repo.distros_dir / distro / "distro.yaml"
    if dpath.is_file():
        d = yaml.safe_load(dpath.read_text()) or {}
        return d.get("family") == "debian"
    # Fallback: known debian-based names.
    return distro in ("kali", "debian", "ubuntu")


def _load_defaults(repo_root: Path) -> dict:
    cfg = repo_root / "mobilelinux.toml"
    if cfg.is_file():
        with open(cfg, "rb") as fh:
            return tomllib.load(fh).get("defaults", {})
    return {}


def build_command(
    ctx, device: Device, *, distro: str | None, desktop: str | None, profile: str | None,
    input_boot: str | None = None,
) -> int:
    runner = ctx.runner()
    defaults = _load_defaults(ctx.repo.root)
    distro = distro or defaults.get("distro", "kali")
    desktop = desktop or defaults.get("desktop", "phosh")

    out_dir = ctx.repo.out_dir / device.id
    out_dir.mkdir(parents=True, exist_ok=True)

    ui.header(f"Build: {device.pretty_name()}")
    print(f"  distro={distro}  desktop={desktop}"
          + (f"  profile={profile}" if profile else ""))

    backend = get_backend(distro, ctx.repo.root)

    # -- Stage: kernel (shared base + per-distro flavor) --------------------
    from . import kernel as kernelstage
    flavor_name = None
    kernel_apk = None
    if device.kernel_flavors:
        try:
            flavor_name = kernelstage.resolve_flavor(device, distro, None)
        except BuildError as exc:
            ui.warn(str(exc))
    if flavor_name:
        kernel_apk = kernelstage.build_kernel(device, distro, flavor_name,
                                              out_dir=out_dir, runner=runner)
        # Only Debian-based distros consume a linux-image .deb; Alpine/pmOS uses
        # the apk directly. Gate on the distro family, not just the device flag.
        if device.kernel.get("build", {}).get("deb_package") and _is_debian_based(ctx, distro):
            kernelstage.build_linux_image_deb(device, kernel_apk, out_dir=out_dir, runner=runner)
    else:
        _build_kernel(device, runner)

    # -- Stage: rootfs ------------------------------------------------------
    from ..distros.base import BuildRequest
    req = BuildRequest(
        device=device, desktop=desktop, profile=profile,
        out_dir=out_dir, repo_root=ctx.repo.root, runner=runner,
    )
    rootfs = backend.build_rootfs(req)

    # -- Stage: rootfs image ------------------------------------------------
    ui.header("Images")
    rootfs_img = out_dir / f"{device.id}-rootfs.img"
    images.build_rootfs_image(
        device, runner,
        rootfs_tar=rootfs.tarball or (out_dir / "rootfs.tar"),
        out=rootfs_img,
    )

    # -- Stage: boot image --------------------------------------------------
    boot_img = out_dir / f"{device.id}-boot.img"
    if device.boot.get("method") == "android-bootimg":
        images.build_android_bootimg(
            device, runner,
            kernel=out_dir / "vmlinuz", ramdisk=out_dir / "initramfs",
            out=boot_img,
        )
    else:
        ui.note(f"  boot method '{device.boot.get('method')}' handled by rootfs image")

    # -- Stage: rescue image (if required) ----------------------------------
    rescue_img = None
    rescue = device.install.get("rescue", {})
    if rescue.get("required"):
        rescue_img = out_dir / f"{device.id}-rescue.img"
        _build_rescue(device, runner, out=rescue_img, base_boot=input_boot)

    # -- Artifacts manifest -------------------------------------------------
    aset = ArtifactSet(device=device.id, distro=distro, desktop=desktop)
    if boot_img.exists():
        aset.add("boot", boot_img, role="boot")
    if rootfs_img.exists():
        aset.add("rootfs", rootfs_img, role="rootfs")
    if rescue_img and rescue_img.exists():
        aset.add("rescue", rescue_img, role="rescue")
    manifest = aset.save(out_dir)

    # Always emit device-specific install + firmware instructions.
    _emit_install_instructions(ctx, device, distro)
    _emit_firmware_instructions(ctx, device)

    # Keep out/<device>/ clean: only flashable artifacts + docs at the top; move
    # build scratch into out/<device>/work/.
    if not ctx.dry_run:
        _finalize_out_dir(out_dir, distro)

    ui.header("Result")
    if aset.artifacts:
        ui.note(f"  flashable artifacts in {out_dir}:")
        for key, art in aset.artifacts.items():
            print(f"  {ui.green('\u2713')} {key:<8} {art.filename}  "
                  f"({art.size} bytes, sha256 {art.sha256[:12]}…)")
        ui.success(f"artifact manifest: {manifest}")
        ui.note("  see INSTALL.md (how to flash) and FIRMWARE.md (extract vendor blobs)")
    else:
        ui.warn("no artifacts were produced (tools missing / dry-run)")

    for note in rootfs.notes:
        ui.note(f"  note: {note}")

    if runner.missing.any:
        runner.missing.report()
        ui.note("Install the tools above to produce real artifacts, then re-run.")
        return 2 if not ctx.dry_run else 0
    return 0


#: files/dirs that are flashable artifacts or user docs (kept at out/<device>/)
_FLASHABLE_SUFFIXES = (".img", "-boot.img", "-userdata.img", "-rescue.img")
_KEEP_NAMES = {"INSTALL.md", "FIRMWARE.md", "CHECKSUMS.sha256", "artifacts.json"}
_KEEP_GLOBS = ("*-boot.img", "*-userdata.img", "*-rescue.img", "*.img",
               "modules-*.tar.gz")


def _finalize_out_dir(out_dir: Path, distro: str) -> None:
    """Move build scratch into out/<device>/work/, leaving only flashable
    artifacts + docs at the top level, and write CHECKSUMS.sha256."""
    import hashlib

    work = out_dir / "work"
    work.mkdir(exist_ok=True)

    def is_flashable(p: Path) -> bool:
        if p.name in _KEEP_NAMES:
            return True
        return any(p.match(g) for g in _KEEP_GLOBS)

    for entry in list(out_dir.iterdir()):
        if entry == work:
            continue
        if entry.is_file() and is_flashable(entry):
            continue
        # everything else is scratch (apk-extract/, linux-image-pkg/, *.deb,
        # config-*.aarch64, device.toml, recipe clone, rootfs/, tarballs, ...)
        dest = work / entry.name
        if dest.exists():
            import shutil
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        entry.rename(dest)

    # (Re)write CHECKSUMS.sha256 over the flashable artifacts.
    lines = []
    for p in sorted(out_dir.iterdir()):
        if p.is_file() and (any(p.match(g) for g in _KEEP_GLOBS)):
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            lines.append(f"{h.hexdigest()}  {p.name}")
    if lines:
        (out_dir / "CHECKSUMS.sha256").write_text("\n".join(lines) + "\n")
    ui.note(f"  finalized: flashable artifacts kept in {out_dir}, scratch moved to {work}/")


def _emit_firmware_instructions(ctx, device: Device) -> None:
    """Generate device-specific firmware-extraction instructions when the
    firmware is non-redistributable (blobs must be pulled from the device)."""
    fw = device.firmware
    if fw.get("redistributable", True):
        return
    from ..installer.instructions import generate_firmware_instructions
    text = generate_firmware_instructions(device)
    path = ctx.repo.out_dir / device.id / "FIRMWARE.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    ui.note(f"  wrote firmware instructions: {path}")


def _build_kernel(device: Device, runner: tools.Runner) -> None:
    k = device.kernel
    ui.header(f"Kernel ({k.get('type')} {k.get('version')})")
    method = k.get("build", {}).get("method", "make")
    if method == "pmbootstrap":
        pkg = k.get("build", {}).get("pmaports_pkg", f"linux-{device.codename}")
        # pmbootstrap needs its own init/chroot; treat as dangerous so it is
        # only run with an explicit --execute --allow-dangerous.
        runner.run(["pmbootstrap", "checksum", pkg], tool="pmbootstrap", dangerous=True)
        runner.run(["pmbootstrap", "build", "--force", pkg], tool="pmbootstrap", dangerous=True)
    else:
        ui.note("  (make-based kernel build; requires cross toolchain)")
        runner.run(["make", "ARCH=" + _karch(device.architecture),
                    k.get("build", {}).get("image", "Image")], tool="make")


def _karch(arch: str) -> str:
    return {"aarch64": "arm64", "armv7": "arm", "armhf": "arm"}.get(arch, arch)


def _build_rescue(device: Device, runner: tools.Runner, *, out: Path,
                  base_boot: str | None = None) -> None:
    """Build the rescue image by deriving it from a known-good boot image.

    Uses the device's rescue build script (e.g. build-rescue-boot.sh, which
    splits kernel+DTB+initramfs, appends pmos.debug-shell, and repacks an
    Android boot v2). Needs a base boot image: pass one via --input, or set
    install.rescue.build_from in the device (a literal path). If neither is a
    real file, the step is planned.
    """
    ui.header("Rescue image")
    rescue = device.install.get("rescue", {})
    ui.note(f"  method={rescue.get('method')} transport={rescue.get('transport')}")

    base = base_boot or rescue.get("build_from", "")
    script = device.dir / "assets" / "scripts" / "build-rescue-boot.sh"
    if base and base != "@INPUT_BOOT_IMG@" and Path(base).is_file() and script.is_file():
        runner.run(["sh", str(script), base, str(out)], tool="sh", dangerous=True)
    else:
        ui.note("  no base boot image available (pass --input <pmos-boot.img> or set "
                "install.rescue.build_from); planning the derivation:")
        runner.run(["sh", "-c",
                    f"# {script} <base-pmos-boot.img> {out}: split kernel+DTB+initramfs, "
                    f"append pmos.debug-shell, repack Android boot v2"])


def _emit_install_instructions(ctx, device: Device, distro: str) -> None:
    """Generate device-specific install instructions (requirement #11)."""
    from ..installer.instructions import generate_instructions
    text = generate_instructions(device, distro)
    inst_path = ctx.repo.out_dir / device.id / "INSTALL.md"
    inst_path.parent.mkdir(parents=True, exist_ok=True)
    inst_path.write_text(text, encoding="utf-8")
    ui.note(f"  wrote install instructions: {inst_path}")
