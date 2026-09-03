# Kali Linux for the Motorola Moto G 2015 (`osprey`)

Status: **work in progress / untested on hardware.** The device definition is
complete and the build pipeline plans correctly, but no images have been built
and booted yet. Build on a machine that has the phone, then flash and test.

- SoC: Qualcomm Snapdragon 410 (MSM8916), aarch64
- Kernel: mainline 6.6 (the `msm8916-mainline` fork, shared across many
  msm8916 devices)
- Install strategy: **fastboot** (unlockable bootloader)

> ⚠️ **At your own risk. See the disclaimer at the bottom. Back up first.**

## Build the images

On a Debian/Kali build host with the tools installed (see the repo
[required tools](../../README.md#required-tools)):

```bash
# from the repo root, with the venv active
mobilelinux check osprey                       # review hardware support
mobilelinux build osprey --distro kali --execute --allow-dangerous
# artifacts land in out/osprey/: kali-boot.img, kali-userdata.img, (rescue.img)
```

Notes for osprey specifically:
- The rootfs layout is **plain ext4** (not the sector-4096 GPT-in-partition that
  rhodep uses), so the distro initramfs is fine — no `--input` pmOS boot image
  is required.
- Only a `pmos` kernel flavor exists today (mainline msm8916). That already
  gives a working system; a dedicated `kali` NetHunter flavor (extra pentest
  kernel symbols) can be added later as a config fragment.

## Flash (fastboot)

```bash
# unlock the bootloader first if it isn't (Motorola unlock code + fastboot):
#   fastboot oem get_unlock_data   -> get code from Motorola -> fastboot oem unlock <code>
fastboot flash boot out/osprey/kali-boot.img
fastboot flash userdata out/osprey/kali-userdata.img
fastboot reboot
```

Or let the framework do it (detects the device, confirms, shows what it writes):

```bash
mobilelinux flash osprey --dry-run     # preview
mobilelinux flash osprey               # do it
```

Default login: **`kali` / `1234`** — change it immediately.

## Publish a release (once it boots)

1. Copy `out/osprey/rescue.img`, `out/osprey/kali-boot.img` into `release/osprey/`.
2. Compress the system image: `xz -6 -T0 -c out/osprey/kali-userdata.img >
   release/osprey/kali-userdata.img.xz` (Git LFS; must be < 2 GB).
3. `sha256sum kali-boot.img rescue.img *.xz > CHECKSUMS.sha256`.
4. In `release.yaml`, set `publish: true` and bump `version`/`tag`.
5. Commit + push to `main` → the workflow publishes `kali-osprey-v...`.

## Vendor firmware

Some firmware (GPU/Wi-Fi/BT/modem) is proprietary and extracted from the stock
device. After the build, `out/osprey/FIRMWARE.md` lists the blobs and how to
obtain them. Wi-Fi/BT/modem status is **partial/untested** — verify on hardware.

---

## Disclaimer

You flash this **entirely at your own risk**. The authors accept **no
liability** for bricked devices or lost data. This is an untested WIP build.
See the repository [DISCLAIMER.md](../../DISCLAIMER.md).
