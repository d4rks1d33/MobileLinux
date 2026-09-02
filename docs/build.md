# Building an Image

`mobilelinux build` produces the images needed to install a device: a kernel, a
distribution root filesystem, a rootfs disk image, a boot image, and (where the
install strategy needs one) a rescue image, all recorded in an `artifacts.json`
manifest.

This guide is for users producing an image to flash, and for porters who want to
understand the pipeline.

## Command

```
mobilelinux build <device> --distro kali [--desktop phosh] [--profile security]
```

- `--distro` — the distribution backend. Defaults come from `mobilelinux.toml`
  (`[defaults]`), falling back to `kali`.
- `--desktop` — the desktop environment. Defaults to the backend's default
  (`phosh` for Kali), or the config default.
- `--profile` — an optional distro profile, e.g. `security` (adds the security
  tool layer).

Example:

```
mobilelinux build rhodep --distro kali --desktop phosh --profile security
```

Selecting a distro, desktop, or profile never changes device support, and it is
applied as a layer on top of the device-independent rootfs.

## Pipeline stages

The pipeline is designed so each stage leaves the project in a usable state and
dry-runs cleanly:

```
kernel → rootfs (distro backend) → rootfs image → boot image
       → rescue image (if the strategy needs one) → artifacts.json (+ INSTALL.md)
```

1. **Kernel.** Built from the device's `kernel` definition. If the build method
   is `pmbootstrap` (as on `rhodep`), the framework runs `pmbootstrap checksum`
   and `pmbootstrap build`; because pmbootstrap needs its own init/chroot, these
   are marked **dangerous** and only run with `--execute --allow-dangerous`.
   Otherwise a `make`-based cross build is planned (requires a cross toolchain).

2. **Rootfs (distro backend).** For Kali this is **debos** (see below). Produces
   a rootfs tarball, e.g. `rootfs-<arch>-<desktop>-nonfree.tar.xz`.

3. **Rootfs image.** The tarball is assembled into the device's rootfs disk
   image (`<device>-rootfs.img`). For `rhodep` this is a **GPT-in-partition**
   image with a **4096-byte sector size** (`storage.rootfs_layout:
   gpt-in-partition`, `storage.rootfs_sector_size: 4096`, matching the UFS
   native 4K): a full GPT Linux disk (boot vfat + root ext4) written *inside* the
   `userdata` partition.

4. **Boot image.** If the device's boot method is `android-bootimg` (as on
   `rhodep`), an Android boot image (`<device>-boot.img`) is built from the
   kernel and initramfs using the offsets declared in
   `boot.android_bootimg`. Other boot methods are handled by the rootfs image
   itself.

5. **Rescue image.** Built only when the install strategy requires one
   (`install.rescue.required: true`). For `rhodep` the rescue image is a pmOS
   kernel+DTB+initramfs with `pmos.debug-shell` appended, which brings up
   USB-gadget networking and a root shell **without mounting root** — this is
   what the rescue-dd flow uses to `dd` the rootfs into `userdata`.

6. **Artifacts manifest.** Every produced image is hashed and recorded in
   `artifacts.json` (role, filename, size, sha256). This manifest is what
   `mobilelinux flash` verifies before writing anything. If no artifacts were
   produced (tools missing / dry-run), the build also emits a device-specific
   `INSTALL.md` with the exact steps.

Output goes to the repository's output directory under `<device>/`.

## Kali uses debos (not apk/pacman)

Kali is **Debian-based**, so its rootfs is built with **debos**, not
`apk`/`pacman`. The backend reproduces the reference port's verified flow:

1. clone the upstream `kali-nethunter-pro` recipes;
2. drop in a generated device config (chipset/vendor/model + bootimg offsets,
   rendered from the device definition so it never drifts) and the device `.deb`
   packages;
3. run debos on `rootfs.yaml` → a `nonfree` rootfs tarball (~920 MB on
   `rhodep`);
4. run the **device-integration phase in a chroot** (next section).

## Why integration runs in a chroot

`image.yaml` cannot partition **inside a container** (loop devices report
`max_part=0`), so the device-integration phase runs in a **chroot** on the
extracted rootfs instead. That phase:

- installs `initramfs-tools` and the SoC support glue (e.g.
  `qcom-support-common`), plus the phone role for the desktop (e.g.
  `mobian-phosh-phone` for phosh);
- installs the device `.deb` packages;
- enables the SoC's firmware/modem services (on Qualcomm:
  `qrtr-ns`, `rmtfs`, `pd-mapper`, `tqftpserv`);
- **masks** first-boot services that would hang or fail (from
  `first_boot.mask_services`, e.g. `droid-juicer`, `systemd-repart`);
- runs the device's userspace installer/config layers (audio, sensors, etc.);
- applies **apt holds LAST**, pinning packages and protecting every laid-down
  file so later `apt` runs cannot clobber the port.

## Execution model: `--dry-run` / `--execute` / `--allow-dangerous`

Build tools are heavy and often absent, so nothing runs by accident:

- **Default (plan):** commands are printed; nothing runs.
- **`--dry-run`:** prints every command, runs nothing (always exits cleanly).
- **`--execute`:** runs commands whose tool is present.
- **`--allow-dangerous`:** additionally permits operations that touch real
  block/loop devices — `losetup`, `mkfs`, partitioning, chroot extraction, and
  `pmbootstrap`. Implies `--execute`.

Heavy tools (`debos`, `pmbootstrap`, `losetup`, `mkfs`, `chroot`) are gated
precisely so that having them installed can never cause an accidental
destructive or long-running build. You must ask for it explicitly.

## Tools you need for a real build, and how the framework tells you

For a real Kali build you will typically need:

- `debos` (and its dependencies, e.g. `go`) — Kali rootfs;
- `pmbootstrap` — kernel build on devices that use it (e.g. `rhodep`);
- `git` — fetching the upstream recipe;
- loop/partitioning tools (`losetup`, `mkfs`, etc.) — assembling the rootfs
  image;
- `chroot` — the device-integration phase.

You do **not** have to guess. Any missing tool is reported at the end of the run
with an install hint, and the affected steps are skipped. Install the tools it
names and re-run. When run as a real build with tools still missing, `build`
exits non-zero so scripts can detect the incomplete result.

## See also

- [install.md](install.md) — the full user path
- [flash.md](flash.md) — flashing the artifacts you built
- [architecture.md](architecture.md) — why the distro is decoupled from device
  support
