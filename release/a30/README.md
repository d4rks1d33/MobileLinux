# Kali Linux for the Samsung Galaxy A30 (`a30`)

Status: **work in progress / untested on hardware.** The device definition is a
completed draft with conservative (mostly `untested`) hardware statuses, because
this is a **downstream** Exynos port and the details haven't been verified on
real hardware yet. Build, flash, and test to fill in what actually works.

- SoC: Samsung Exynos 7904, aarch64
- Kernel: **downstream** vendor kernel 4.4.177 (not mainline)
- Install strategy: **heimdall** (Samsung download mode, open-source Odin)

> ⚠️ **At your own risk. See the disclaimer at the bottom. Back up first.**
> Unlocking / flashing a Samsung device trips the **KNOX warranty bit**
> permanently.

## Requirements

- **`heimdall`** on your computer (`apt install heimdall-flash`).
- The build tools (see the repo [required tools](../../README.md#required-tools)).

## Build the images

```bash
# from the repo root, with the venv active
mobilelinux check a30                          # review (mostly untested) status
mobilelinux build a30 --distro kali --execute --allow-dangerous
# artifacts land in out/a30/: kali-boot.img, kali-userdata.img
```

Notes for a30 specifically:
- Downstream 4.4 Exynos kernel — the port lives in pmOS `device/testing`
  (early tier). Expect to iterate.
- Rootfs layout is **plain ext4**, so the distro initramfs is fine (no `--input`
  pmOS boot image needed).

## Flash (heimdall / download mode)

Put the phone in **download mode**: power off, then hold **Volume Down + Volume
Up** while plugging in the USB cable. Then:

```bash
heimdall flash --boot   out/a30/kali-boot.img
heimdall flash --system out/a30/kali-userdata.img
# then reboot the phone (Volume Down + Power to exit download mode)
```

Or let the framework do it:

```bash
mobilelinux flash a30 --dry-run     # preview
mobilelinux flash a30               # do it
```

Default login: **`kali` / `1234`** — change it immediately.

## Publish a release (once it boots)

1. Copy `out/a30/kali-boot.img` into `release/a30/`.
2. Compress the system image: `xz -6 -T0 -c out/a30/kali-userdata.img >
   release/a30/kali-userdata.img.xz` (Git LFS; must be < 2 GB).
3. `sha256sum kali-boot.img *.xz > CHECKSUMS.sha256`.
4. In `release.yaml`, set `publish: true` and bump `version`/`tag`.
5. Commit + push to `main` → the workflow publishes `kali-a30-v...`.

## Vendor firmware

Some firmware (Wi-Fi/BT/GPU) is proprietary and extracted from the stock device.
After the build, `out/a30/FIRMWARE.md` lists the blobs. Most peripheral statuses
are **untested** — verify on hardware and update the device definition.

---

## Disclaimer

You flash this **entirely at your own risk**. The authors accept **no
liability** for bricked devices, lost data, or a tripped KNOX warranty bit. This
is an untested WIP build. See the repository [DISCLAIMER.md](../../DISCLAIMER.md).
