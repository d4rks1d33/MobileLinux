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


def _load_defaults(repo_root: Path) -> dict:
    cfg = repo_root / "mobilelinux.toml"
    if cfg.is_file():
        with open(cfg, "rb") as fh:
            return tomllib.load(fh).get("defaults", {})
    return {}


def build_command(
    ctx, device: Device, *, distro: str | None, desktop: str | None, profile: str | None,
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

    # -- Stage: kernel ------------------------------------------------------
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
        _build_rescue(device, runner, out=rescue_img)

    # -- Artifacts manifest -------------------------------------------------
    aset = ArtifactSet(device=device.id, distro=distro, desktop=desktop)
    if boot_img.exists():
        aset.add("boot", boot_img, role="boot")
    if rootfs_img.exists():
        aset.add("rootfs", rootfs_img, role="rootfs")
    if rescue_img and rescue_img.exists():
        aset.add("rescue", rescue_img, role="rescue")
    manifest = aset.save(out_dir)

    ui.header("Result")
    if aset.artifacts:
        for key, art in aset.artifacts.items():
            print(f"  {ui.green('\u2713')} {key:<8} {art.filename}  "
                  f"({art.size} bytes, sha256 {art.sha256[:12]}…)")
        ui.success(f"artifact manifest: {manifest}")
    else:
        ui.warn("no artifacts were produced (tools missing / dry-run)")
        _emit_install_instructions(ctx, device, distro)

    for note in rootfs.notes:
        ui.note(f"  note: {note}")

    if runner.missing.any:
        runner.missing.report()
        ui.note("Install the tools above to produce real artifacts, then re-run.")
        return 2 if not ctx.dry_run else 0
    return 0


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


def _build_rescue(device: Device, runner: tools.Runner, *, out: Path) -> None:
    ui.header("Rescue image")
    rescue = device.install.get("rescue", {})
    ui.note(f"  method={rescue.get('method')} transport={rescue.get('transport')}")
    base = rescue.get("build_from", "")
    # Rescue = base pmOS boot image + debug-shell in cmdline. The real logic
    # lives in the device assets; here we plan the derivation.
    runner.run(["sh", "-c",
                f"# derive rescue image from {base}: split kernel+DTB+initramfs, "
                f"append pmos.debug-shell, repack Android boot v2 -> {out}"])


def _emit_install_instructions(ctx, device: Device, distro: str) -> None:
    """Generate device-specific install instructions (requirement #11)."""
    from ..installer.instructions import generate_instructions
    text = generate_instructions(device, distro)
    inst_path = ctx.repo.out_dir / device.id / "INSTALL.md"
    inst_path.parent.mkdir(parents=True, exist_ok=True)
    inst_path.write_text(text, encoding="utf-8")
    ui.note(f"  wrote install instructions: {inst_path}")
