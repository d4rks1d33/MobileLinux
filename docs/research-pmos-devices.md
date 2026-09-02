# PostmarketOS Device-Support Model & 5-Device Flashing Study

> Research report for the design of a new mobile-Linux porting framework.
> All facts below were verified against the live pmaports GitLab tree and the
> pmbootstrap source. The postmarketOS **wiki** is currently behind an Anubis
> anti-scraper challenge and could not be fetched programmatically; where a
> wiki URL is given it is cited as the canonical human reference but the
> underlying facts were cross-checked against pmaports/pmbootstrap source.

---

# PART 1 — How PostmarketOS Represents Device Support

## 1.1 Anatomy of a device package (`pmaports.git`)

A port lives in a single directory:

```
device/<category>/device-<vendor>-<codename>/
├── APKBUILD          # Alpine build recipe for the device meta-package
├── deviceinfo        # key=value hardware/boot description (sourced as shell)
├── modules-initfs    # kernel modules to pull into the initramfs
├── *.post-install    # scripts run at install time
├── udev rules, UCM audio configs, unl0kr/osk configs, u-boot scripts, etc.
```

The **category** is the maturity tier (see §1.6):
`device/main`, `device/community`, `device/testing`, `device/downstream`
(archived/`unmaintained` also exists historically).

The package name always mirrors the path:
`device-<vendor>-<codename>` (e.g. `device-oneplus-enchilada`).
pmbootstrap enforces that `deviceinfo_codename` equals the directory name minus
the `device-` prefix (`pmb/parse/deviceinfo.py` `__validate`).

### The device APKBUILD

The APKBUILD is a thin meta-package built with the `devicepkg-dev` helper
(`devicepkg_build`/`devicepkg_package`). Its most important job is the
`depends=` list, which wires the device to the rest of the system. Real example
(OnePlus 6, verified):

```sh
pkgname=device-oneplus-enchilada
depends="
    linux-postmarketos-qcom-sdm845     # kernel package
    mkbootimg                          # boot image tooling
    postmarketos-base
    soc-qcom-sdm845                    # SoC-common glue package
    soc-qcom-sdm845-ucm
    soc-qcom-sdm845-qbootctl           # A/B slot handling
"
subpackages="
    $pkgname-nonfree-firmware-openrc:nonfree_firmware_openrc
    $pkgname-nonfree-firmware:nonfree_firmware
"
```

Note the modern SoC-sharing pattern: common bring-up moves into `soc-qcom-<soc>`
packages (Adreno userland, modem/`qbootctl` for A/B, UCM audio), so many devices
on the same SoC reuse the same glue instead of duplicating it.

## 1.2 `deviceinfo` fields

`deviceinfo` is a shell-sourceable `key="value"` file (double quotes only). The
**authoritative** list of recognized keys is the `Deviceinfo` class in
`pmbootstrap/pmb/parse/deviceinfo.py`; the human doc is
<https://wiki.postmarketos.org/wiki/Deviceinfo_reference> (canonical) and
`postmarketos.org/deviceinfo`. Representative field list (grouped as pmbootstrap
groups them), verified from source:

**General / identity**
- `deviceinfo_format_version` (currently `"0"`)
- `deviceinfo_name`, `deviceinfo_manufacturer`, `deviceinfo_year`
- `deviceinfo_codename` — must match directory name
- `deviceinfo_arch` — `aarch64` | `armv7` | `armhf` | `x86_64` | `riscv64`
- `deviceinfo_dtb` — DTB path(s) without `.dtb`; space-separated list allowed
- `deviceinfo_append_dtb` — append DTB to kernel image (`true`/`false`)

**Device / chassis**
- `deviceinfo_chassis` — validated enum: `handset, tablet, laptop, convertible,
  desktop, server, watch, embedded, vm` (maps to systemd machine chassis)
- `deviceinfo_keyboard` (deprecated), `deviceinfo_external_storage`
- `deviceinfo_gpu_accelerated`, `deviceinfo_mesa_driver`
- `deviceinfo_screen_width` / `deviceinfo_screen_height`
- `deviceinfo_dev_touchscreen`, `deviceinfo_dev_touchscreen_calibration`
- `deviceinfo_getty` (e.g. `ttyMSM0;115200`), `deviceinfo_keymaps`

**Bootloader / flash method**
- `deviceinfo_flash_method` — selects the flasher (see §1.5)
- `deviceinfo_kernel_cmdline`
- `deviceinfo_generate_bootimg` — build an Android boot.img
- `deviceinfo_header_version` — Android boot image header version (0/1/2/3/4)
- `deviceinfo_bootimg_qcdt` — append Qualcomm device-tree table (QCDT)
- `deviceinfo_bootimg_mtk_mkimage` (deprecated) / `_mtk_label_kernel` / `_mtk_label_ramdisk`
- `deviceinfo_bootimg_dtb_second`, `deviceinfo_bootimg_custom_args`
- `deviceinfo_flash_offset_base` / `_kernel` / `_ramdisk` / `_second` / `_tags` / `_dtb`
- `deviceinfo_flash_pagesize`
- `deviceinfo_flash_sparse` — convert rootfs to Android sparse image before flash
- `deviceinfo_flash_sparse_samsung_format` — Samsung sparse variant (sm-sparse tool)
- `deviceinfo_flash_fastboot_max_size` — refuse fastboot if rootfs exceeds this
- `deviceinfo_rootfs_image_sector_size` — needed for UFS (4096) vs eMMC (512)

**Per-flasher partition names**
- Fastboot: `deviceinfo_flash_fastboot_partition_kernel` / `_rootfs` / `_vbmeta` / `_dtbo`
- Heimdall: `deviceinfo_flash_heimdall_partition_kernel` / `_initfs` / `_rootfs` / `_vbmeta` / `_dtbo`
- Rockchip: `deviceinfo_flash_rk_partition_kernel` / `_rootfs`
- mtkclient: `deviceinfo_flash_mtkclient_partition_kernel` / `_rootfs` / `_vbmeta` / `_dtbo`

**Partitioning / storage**
- `deviceinfo_partition_type` (e.g. `msdos`, `gpt`)
- `deviceinfo_partition_blacklist`
- `deviceinfo_boot_part_start`, `deviceinfo_boot_filesystem`, `deviceinfo_root_filesystem`
- `deviceinfo_dev_internal_storage`, `deviceinfo_dev_internal_storage_repartition`
- `deviceinfo_super_partitions` — Android dynamic-partition block devices (fastbootd territory)
- ChromeOS: `deviceinfo_cgpt_kpart`, `_cgpt_kpart_start`, `_cgpt_kpart_size`

**SD-card / U-Boot embedding**
- `deviceinfo_sd_embed_firmware` — `path:offset[,path:offset]` blobs to `dd` into the image (e.g. U-Boot SPL)
- `deviceinfo_sd_embed_firmware_step_size`
- `deviceinfo_generate_legacy_uboot_initfs`, `deviceinfo_generate_extlinux_config`

**Update behaviour**
- `deviceinfo_flash_kernel_on_update` — re-flash boot.img on kernel package upgrade

> Note the class comment in pmbootstrap: *"Many of these are unused in
> pmbootstrap, and still more that are described on the wiki are missing."* i.e.
> the deviceinfo format is a loose superset; the wiki and the parser both drift.

## 1.3 How kernels are packaged

Kernels are ordinary Alpine APKBUILDs, named `linux-<vendor>-<codename>` or,
increasingly, a shared `linux-postmarketos-<soc/family>` package. Verified
examples in tree:

- `linux-postmarketos-allwinner` (mainline, PinePhone)
- `linux-postmarketos-qcom-sdm845` (near-mainline, OnePlus 6 / Poco F1 share it)
- `linux-postmarketos-qcom-sdm670` (Pixel 3a)
- `linux-postmarketos-exynos4` (Galaxy S III)
- `linux-purism-librem5`
- `linux-pine64-pinephonepro`

Each kernel aport typically contains: the `APKBUILD`, a `config-<flavor>.<arch>`
kernel config (checked by pmbootstrap's `kconfigcheck` against required options
like nftables/zram/uefi via `options="pmb:kconfigcheck-*"`), and any `.patch`
files. Special APKBUILD vars for kernels: `_flavor`, `_config`, `_kernver`,
`_outdir`.

**Mainline vs downstream/vendor:**
- *Mainline / near-mainline*: built from upstream Linux (or upstream + a small
  patch stack). Preferred; these devices land in `main`/`community`.
- *Downstream / vendor*: the OEM's fork (often 3.x/4.x-android). Historically
  used with libhybris/Halium; pmOS has since **dropped Halium/hybris support**
  (per comment in `_install.py`). Downstream ports live in `device/downstream`.

**Multi-kernel devices:** a device package can offer several kernels as
subpackages `device-<codename>-kernel-<flavor>` (e.g. `-kernel-mainline`,
`-kernel-downstream`), chosen in `pmbootstrap init`. deviceinfo keys can be
suffixed per-kernel (`deviceinfo_dtb_mainline` etc.) and pmbootstrap strips the
suffix at parse time (`_parse_kernel_suffix`).

## 1.4 How firmware is packaged (and the non-free situation)

Firmware is shipped as `firmware-<vendor>-<codename>` packages (e.g.
`firmware-oneplus-sdm845`, `firmware-google-sargo`, `firmware-samsung-midas`),
plus SoC-shared ones (`firmware-qcom-adreno`, `firmware-brcm43752`) and Alpine's
`linux-firmware-*` split packages (`linux-firmware-ath10k`, `-qca`, `-qcom`).

Non-free firmware is exposed as a **subpackage of the device package**, named
`device-<codename>-nonfree-firmware` (and sometimes `-nonfree-userland`).
pmbootstrap detects these by name in `get_nonfree_packages()` and installs them
by default (policy change 2024-02-15: non-free firmware installed by default —
`postmarketos.org/edge/2024/02/15/default-nonfree-fw/`). Example (OnePlus 6):

```sh
nonfree_firmware() {
    depends="firmware-oneplus-sdm845 hexagonrpcd
             soc-qcom-sdm845-nonfree-firmware soc-qcom-sdm845-modem"
}
```

The old `deviceinfo_nonfree` key is now a hard error; the mechanism is entirely
subpackage-name based.

## 1.5 How pmbootstrap flashing works

`pmbootstrap flasher <action>` (`pmb/flasher/frontend.py`) actions include:
`flash_rootfs`, `flash_kernel`, `flash_vbmeta`, `flash_dtbo`, `flash_lk2nd`,
`boot`, `list_devices`, `list_flavors`, `sideload`.

The flasher runs abstract "actions" whose command templates live in
`pmb/config/__init__.py` (`flashers` dict). Template variables:
`$IMAGE`, `$IMAGE_SPLIT_BOOT`, `$IMAGE_SPLIT_ROOT`, `$BOOT`, `$PARTITION_KERNEL`,
`$PARTITION_ROOTFS`, `$PARTITION_INITFS`, `$KERNEL_CMDLINE`, `$DTB`, `$FLAVOR`,
`$UUU_SCRIPT`.

**`deviceinfo_flash_method` accepted values** — the *top-level* list validated
by pmbootstrap (`flash_methods`) is currently:

```
0xffff, fastboot, heimdall, mtkclient, none, rkdeveloptool, uuu
```

…and the **flasher implementations** (`flashers` dict keys) are:

| flasher key         | tool           | notes |
|---------------------|----------------|-------|
| `fastboot`          | fastboot       | Android boot.img to `boot`; can flash vbmeta/dtbo/lk2nd; `boot` action = tethered boot without flashing |
| `fastboot-bootpart` | fastboot       | `--split` boot/root images to real partitions (FAT32 boot) |
| `heimdall-bootimg`  | heimdall (Odin)| Samsung; flashes a normal boot.img to a partition (e.g. `BOOT`) |
| `heimdall-isorec`   | heimdall       | Samsung "isolated recovery": initramfs baked into kernel, real initfs on RECOVERY |
| `adb`               | adb sideload   | pushes a recovery-flashable zip |
| `uuu`               | NXP mfgtools uuu | i.MX serial download; runs a `$UUU_SCRIPT` |
| `rkdeveloptool`     | rkdeveloptool  | Rockchip; `--split` write-partition |
| `mtkclient`         | mtk            | MediaTek BROM |
| `none`              | —              | no supported flash method; user follows manual/SD instructions |

> Historical values still seen in older deviceinfo files but no longer in the
> validated top-level list include `heimdall-bootimg`/`heimdall-isorec` (these
> are flasher keys, chosen via `deviceinfo_flash_method`) and `0xffff` (Nokia
> N900 FIASCO/NOLO). The presence of `0xffff` in `flash_methods` but **not** in
> the `flashers` dict means N900 flashing today is effectively manual/U-Boot,
> not driven by `pmbootstrap flasher` — see the N900 section in Part 2.

**What `pmbootstrap install` does** (`pmb/install/_install.py`,
`create_device_rootfs` + `install_system_image`):

1. Build a **rootfs chroot** for the device; install `postmarketos-base`,
   `device-<codename>`, selected UI (`postmarketos-ui-*`), the chosen kernel
   subpackage, non-free firmware, recommends (`_pmb_recommends`) and selected
   providers (`_pmb_select`).
2. Build the initramfs (`mkinitfs`) for the installed kernel flavor.
3. Create a block device / image, **partition** it: default layout is
   `boot`=p1, `root`=p2 (optional cgpt kernel partition p1 for ChromeOS;
   optional reserve partition). The rootfs image itself contains a partition
   table with `/boot` and `/` subpartitions so the device's own partition table
   need not change.
4. Format, create `/etc/fstab` (+ `/etc/crypttab` if `--fde`), run `mkinitfs`
   again to bake in UUIDs, copy files, create user (UID 10000), configure apk
   keys/repos, copy SSH keys.
5. Optionally convert rootfs to **Android sparse** (`img2simg`) if
   `deviceinfo_flash_sparse=true`; apply Samsung sparse patch if configured.
6. `embed_firmware`: `dd` U-Boot/SPL blobs listed in
   `deviceinfo_sd_embed_firmware` into the image at fixed offsets (SD-boot SoCs).
7. Print tailored **flashing instructions** for the device's flash method.
8. Variants: `--disk /dev/…` (write straight to SD/eMMC), `--split` (separate
   boot/root images), `--android-recovery-zip` (build TWRP-flashable zip),
   `--on-device-installer` (build an installer OS image).

## 1.6 How hardware-support status is tracked

Two separate systems:

**(a) Maturity tier = pmaports directory / category** (machine-readable):
- `main` — first-class, CI-tested, usually reference/dev devices (PinePhone,
  Librem 5, QEMU).
- `community` — well-supported real phones maintained by the community
  (OnePlus 6, Poco F1, Pixel 3a, Galaxy S III, Fairphone 4…).
- `testing` — new/WIP ports, less complete.
- `downstream` — ports on vendor kernels / limited functionality.

**(b) Per-feature hardware status = the wiki device page** (human-maintained):
Each device has a wiki page
(`https://wiki.postmarketos.org/wiki/<Name>_(<vendor>-<codename>)`) with an
**infobox** ("Device supported: Yes", category tags) and a **feature matrix**
table with ✔/✘/partial for: Display, Touchscreen, Backlight, GPU accel,
Bluetooth, WiFi, Audio (speaker/headphone/mic/call audio), Camera, GPS/GNSS,
Modem/Calls, SMS, Mobile data, Sensors (accel/proximity/…), NFC, Fingerprint,
FDE, USB networking, Battery/charging, Sleep/suspend, Vibration, etc. This table
is **not** generated from pmaports — it is prose maintained by porters.

## 1.7 Automatable vs manual

| Data | Source | Automatable? |
|------|--------|--------------|
| codename, arch, chassis, year, name, manufacturer | `deviceinfo` | ✅ direct parse |
| flash method, boot offsets, pagesize, header version, sparse | `deviceinfo` | ✅ |
| DTB name(s), kernel cmdline, super/partition info | `deviceinfo` | ✅ |
| kernel package + config + patches | `linux-*` APKBUILD + `config-*` | ✅ (config = capability hints) |
| firmware packages, non-free split | device APKBUILD `depends`/subpackages | ✅ |
| SoC glue, A/B handling, UCM audio | `depends` (`soc-*`) | ✅ (inference) |
| maturity tier | pmaports directory (`main`/`community`/…) | ✅ |
| **per-feature works/doesn't table** | **wiki page** | ❌ manual prose |
| **install quirks, recovery steps, gotchas** | **wiki page prose** | ❌ manual |
| **known bugs / limitations** | wiki + gitlab issues | ❌ manual |

**Design implication for a new framework:** the deviceinfo + APKBUILD +
kernel-config triad is a clean, parseable "device descriptor" you can import
verbatim (identity, arch, boot geometry, flash strategy, firmware/kernel deps,
maturity tier). The **feature-support matrix and install narrative are the
missing structured data** — pmOS keeps them as human wiki text. A new framework
should promote those to structured, per-feature machine-readable fields.

---

# PART 2 — Five Devices, Maximizing SoC & Flash-Strategy Diversity

Selection principles: real pmOS devices only; different manufacturers; distinct
SoC families; mainline/near-mainline; functional & documented; and above all
**five different installation strategies**.

| # | Device | Vendor | Codename | SoC | Flash method (deviceinfo) | **Install strategy** |
|---|--------|--------|----------|-----|---------------------------|----------------------|
| 1 | PinePhone | PINE64 | `pine64-pinephone` | Allwinner A64 | `none` (+`sd_embed_firmware` U-Boot) | **sdcard (u-boot)** |
| 2 | OnePlus 6 | OnePlus | `oneplus-enchilada` | Snapdragon 845 | `fastboot` | **fastboot** (A/B, no fastbootd) |
| 3 | Pixel 3a | Google | `google-sargo` | Snapdragon 670 | `fastboot` (+ super partitions) | **fastbootd** (dynamic partitions) |
| 4 | Galaxy S III | Samsung | `samsung-m0` | Exynos 4412 | `heimdall-bootimg` | **heimdall (Odin)** |
| 5 | Librem 5 | Purism | `purism-librem5` | NXP i.MX 8M Quad | `uuu` | **uuu** |

Five SoC vendors (Allwinner, Qualcomm×2 but different families/565/845, Samsung
Exynos, NXP), five architectures-of-boot, and five genuinely different flashing
mechanisms. (Nokia N900/OMAP3 was considered — see the note at the end — but its
`0xffff`/U-Boot path overlaps conceptually with sdcard and it is armv7 downstream-
era, so Librem 5's `uuu` was chosen instead for cleaner strategy separation.)

---

## Device 1 — PINE64 PinePhone (`pine64-pinephone`)

| Field | Value |
|-------|-------|
| Device | PinePhone |
| Manufacturer | PINE64 |
| Codename | `pine64-pinephone` |
| SoC | Allwinner A64 (4× Cortex-A53) |
| Architecture | aarch64 |
| Kernel | **Mainline** — `linux-postmarketos-allwinner` |
| Mainline status | Fully mainline (sun50i); one of the reference mainline phones |
| Display | ✔ (`sun4i-drm`, mesa `sun4i-drm`), 720×1440 |
| Touch | ✔ (Goodix) |
| GPU | ✔ Mali-400, Lima driver, accelerated |
| WiFi | ✔ RTL8723CS (`firmware-pine64-rtl8723bt`) |
| Bluetooth | ✔ RTL8723CS |
| Audio | ✔ (incl. call audio) |
| Modem | ✔ Quectel EG25-G (external, own firmware; ModemManager) |
| GNSS | ✔ via EG25-G modem |
| Camera | Partial (OV5640 front / GC2145 rear, `firmware-pine64-ov5640`) |
| NFC | ✘ (none) |
| Fingerprint | ✘ (none) |
| Storage | eMMC + **bootable microSD** (`dev_internal_storage=/dev/mmcblk2`) |
| Bootloader | **U-Boot** (SPL) in `sd_embed_firmware`; no locked OEM bootloader |
| Partition layout | pmOS partition table on chosen medium; U-Boot SPL `dd`'d at 8 KiB offset |
| Installation method | Flash pmOS image to microSD (or eMMC via jumpdrive/USB MS) |
| Recovery/rescue | None needed; hardware kill switches; SD boots over eMMC |
| Known limitations | Camera imperfect; power draw/suspend historically fiddly; modem is a separate firmware domain |
| Source | wiki: `https://wiki.postmarketos.org/wiki/PINE64_PinePhone_(pine64-pinephone)` · pmaports: `device/main/device-pine64-pinephone` |

**deviceinfo highlights (verified):**
```sh
deviceinfo_flash_method="none"
deviceinfo_sd_embed_firmware="u-boot/pine64-pinephone/u-boot-sunxi-with-spl-528.bin:8"
deviceinfo_dev_internal_storage="/dev/mmcblk2"
deviceinfo_dev_internal_storage_repartition="true"
deviceinfo_dtb="allwinner/sun50i-a64-pinephone-1.1 allwinner/sun50i-a64-pinephone-1.2"
```

**Install/flash deep-dive.** There is *no* USB flashing protocol and no vendor
bootloader lock. `pmbootstrap install --disk /dev/sdX` (SD) writes a full GPT
image; pmbootstrap's `embed_firmware()` then `dd`s the U-Boot-with-SPL blob into
the reserved gap at 8 KiB (offset 8, step 1024B). Boot order: the A64 BROM looks
for SPL on microSD first, then eMMC — so an SD card **naturally overrides** the
eMMC install, which *is* the rescue mechanism. To install to eMMC you either boot
from SD and `dd`, or use the "Jumpdrive" USB-mass-storage helper to expose eMMC
to the host. Partitions modified: the entire target medium. No fastboot, no
recovery, no A/B slots, no signed boot image — U-Boot then loads the kernel via
extlinux/boot.scr.

→ **Strategy abstraction: `sdcard` (u-boot).** Write whole-disk image + embedded
bootloader; medium precedence provides recovery. Installing "alternative Linux"
(Mobian, Manjaro, etc.) is identical: write their image to SD — the framework
just needs the U-Boot blob + offset.

---

## Device 2 — OnePlus 6 (`oneplus-enchilada`)

| Field | Value |
|-------|-------|
| Device | OnePlus 6 (6T = `fajita`, sibling) |
| Manufacturer | OnePlus |
| Codename | `oneplus-enchilada` |
| SoC | Qualcomm Snapdragon 845 (SDM845) |
| Architecture | aarch64 |
| Kernel | **Near-mainline** — `linux-postmarketos-qcom-sdm845` |
| Mainline status | Very good mainline bring-up (shared SDM845 effort) |
| Display | ✔ (`msm`/DRM), 1080×2280 |
| Touch | ✔ |
| GPU | ✔ Adreno 630, freedreno, accelerated |
| WiFi | ✔ (ath10k / QCA) |
| Bluetooth | ✔ |
| Audio | ✔ incl. call audio (`soc-qcom-sdm845-ucm`, q6voiced) |
| Modem | ✔ (qrtr, hexagonrpcd, `soc-qcom-sdm845-modem`) |
| GNSS | ✔ (via modem/QMI) |
| Camera | Partial/WIP |
| NFC | Partial |
| Fingerprint | ✘/partial (in-display, hard) |
| Storage | **UFS** (`rootfs_image_sector_size=4096`) |
| Bootloader | Unlockable fastboot (`fastboot oem unlock`); **A/B slots** |
| Partition layout | Flash to `boot` (Android boot.img) + rootfs to `userdata` |
| Installation method | `fastboot flash` (unlock → flash boot → flash rootfs) |
| Recovery/rescue | Stock fastboot only; no separate recovery required; blank-flash for bricks |
| Known limitations | Camera/fingerprint incomplete; A/B slot confusion possible |
| Source | wiki: `https://wiki.postmarketos.org/wiki/OnePlus_6_(oneplus-enchilada)` · pmaports: `device/community/device-oneplus-enchilada` |

**deviceinfo highlights (verified):**
```sh
deviceinfo_flash_method="fastboot"
deviceinfo_generate_bootimg="true"
deviceinfo_bootimg_qcdt="false"
deviceinfo_kernel_cmdline="console=ttyMSM0,115200"
deviceinfo_flash_offset_kernel="0x00008000"
deviceinfo_flash_offset_ramdisk="0x01000000"
deviceinfo_flash_pagesize="4096"
deviceinfo_flash_sparse="true"
deviceinfo_rootfs_image_sector_size="4096"   # UFS
deviceinfo_dtb="qcom/sdm845-oneplus-enchilada"
deviceinfo_append_dtb="true"
deviceinfo_flash_kernel_on_update="true"
```

**Install/flash deep-dive.** Unlock the bootloader (`fastboot oem unlock`, wipes
userdata). pmbootstrap generates an Android **boot.img** (kernel+DTB+initramfs at
the given offsets/pagesize) and a **sparse** rootfs image. Install:
`pmbootstrap flasher flash_kernel` → writes boot.img to the `boot` partition;
`pmbootstrap flasher flash_rootfs` → writes the pmOS rootfs image (which carries
its own boot/root subpartition table) to `userdata`. **You write userdata
directly** via fastboot — no fastbootd needed because SDM845 here exposes the
target partition to bootloader-mode fastboot. **A/B**: the device has slots;
`soc-qcom-sdm845-qbootctl` manages `bootctl`. You typically flash the active
slot; slot is selected by the bootloader/qbootctl rather than by pmbootstrap.
Boot image format: standard Android boot header (v0/v1 style, QCDT off). No
recovery/rescue OS is required for a normal install; a hard brick needs
Qualcomm EDL/blankflash (out of band). Alternative Linux (Ubuntu Touch, Droidian)
uses the same fastboot boot.img+rootfs pattern.

→ **Strategy abstraction: `fastboot`.** Bootloader-mode fastboot; Android
boot.img + direct-to-userdata rootfs; A/B aware. Distinct from device 3 because
no dynamic-partition/fastbootd step is involved.

---

## Device 3 — Google Pixel 3a (`google-sargo`)

| Field | Value |
|-------|-------|
| Device | Pixel 3a (`bonito` = 3a XL sibling) |
| Manufacturer | Google |
| Codename | `google-sargo` |
| SoC | Qualcomm Snapdragon 670 (SDM670) |
| Architecture | aarch64 |
| Kernel | **Near-mainline** — `linux-postmarketos-qcom-sdm670` |
| Mainline status | Good; shares SDM845-family glue (`soc-qcom-sdm845*`) |
| Display | ✔ 1080×2220 |
| Touch | ✔ |
| GPU | ✔ Adreno 615, accelerated |
| WiFi | ✔ (ath10k / QCA, `linux-firmware-ath10k/-qca`) |
| Bluetooth | ✔ |
| Audio | ✔ (`alsa-ucm-conf-qcom-sdm670`, q6voiced) |
| Modem | ✔ (hexagonrpcd, `soc-qcom-sdm845-modem`) |
| GNSS | ✔ via modem |
| Camera | Partial/WIP |
| NFC | Partial |
| Fingerprint | ✘/partial (rear sensor) |
| Storage | eMMC/UFS; **Android dynamic (super) partitions** |
| Bootloader | Unlockable fastboot; **A/B**; **fastbootd** for dynamic partitions |
| Partition layout | `deviceinfo_super_partitions="/dev/mmcblk0p68 /dev/mmcblk0p69"`; needs `make-dynpart-mappings` |
| Installation method | fastboot (boot.img) **+ fastbootd** (userdata inside super) |
| Recovery/rescue | Uses userspace **fastbootd** (booted from recovery ramdisk) to address logical partitions |
| Known limitations | Dynamic-partition dance; vbmeta must be flashed with verification disabled on some Android-9 bootloaders |
| Source | wiki: `https://wiki.postmarketos.org/wiki/Google_Pixel_3a_(google-sargo)` · pmaports: `device/community/device-google-sargo` |

**deviceinfo highlights (verified):**
```sh
deviceinfo_flash_method="fastboot"
deviceinfo_generate_bootimg="true"
deviceinfo_flash_pagesize="4096"
deviceinfo_flash_fastboot_partition_vbmeta="vbmeta"   # Android-9 bootloader workaround
deviceinfo_flash_kernel_on_update="true"
deviceinfo_super_partitions="/dev/mmcblk0p68 /dev/mmcblk0p69"
deviceinfo_dtb="qcom/sdm670-google-sargo"
deviceinfo_append_dtb="true"
```
APKBUILD depends include **`make-dynpart-mappings`** and `soc-qcom-sdm845-qbootctl`.

**Install/flash deep-dive.** This is the fastbootd case. The Pixel 3a uses
**Android dynamic partitions**: `userdata`/logical volumes live inside a `super`
container, which the *bootloader-mode* fastboot **cannot** address. To write the
rootfs you must reboot into **fastbootd** (`fastboot reboot fastboot`), the
userspace fastboot implementation running from the recovery ramdisk, which knows
the logical-partition map (hence `make-dynpart-mappings` and
`deviceinfo_super_partitions`). Sequence: unlock → optionally
`fastboot flash vbmeta` with the verification-disabled flag (pmbootstrap's
`flash_vbmeta` action builds a vbmeta with `--flags 2`) → `flash_kernel`
(boot.img in normal fastboot) → reboot to fastbootd → `flash_rootfs` into the
logical userdata inside super. **A/B** slots present (qbootctl). Boot image is a
standard Android boot header; vbmeta handling is the sargo-specific wrinkle. A
rescue/recovery ramdisk is *implicitly* required because fastbootd lives there.

→ **Strategy abstraction: `fastbootd`.** Superset of `fastboot`: requires a
reboot into userspace fastboot + dynamic-partition mapping + vbmeta disable to
write the rootfs. Cleanly distinct from device 2.

---

## Device 4 — Samsung Galaxy S III (`samsung-m0`)

| Field | Value |
|-------|-------|
| Device | Galaxy S III (GT-I9300) |
| Manufacturer | Samsung |
| Codename | `samsung-m0` |
| SoC | Samsung **Exynos 4412** (4× Cortex-A9) |
| Architecture | armv7 |
| Kernel | Mainline-ish — `linux-postmarketos-exynos4` |
| Mainline status | Community mainline for Exynos4; functional |
| Display | ✔ 720×1280 |
| Touch | ✔ |
| GPU | ✔ (Mali-400; accelerated flag set) |
| WiFi | ✔ (`firmware-samsung-midas-wifi`) |
| Bluetooth | ✔ (`firmware-samsung-midas-bluetooth`) |
| Audio | ✔ (ALSA UCM "Midas") |
| Modem | Partial/WIP |
| GNSS | ✘/partial |
| Camera | ✘/partial |
| NFC | ✘/partial |
| Fingerprint | ✘ (none) |
| Storage | eMMC |
| Bootloader | **Samsung S-Boot / Odin download mode**; flashed via **Heimdall** |
| Partition layout | boot.img → `BOOT` partition; rootfs → data partition |
| Installation method | **Heimdall** (`heimdall-bootimg`): download mode → flash |
| Recovery/rescue | Download mode (Vol-Down+Home+Power) is the rescue path; no TWRP required |
| Known limitations | Modem/GPS/camera limited; must enter Odin/download mode manually |
| Source | wiki: `https://wiki.postmarketos.org/wiki/Samsung_Galaxy_S_III_(samsung-m0)` · pmaports: `device/community/device-samsung-m0` |

**deviceinfo highlights (verified):**
```sh
deviceinfo_flash_method="heimdall-bootimg"
deviceinfo_generate_bootimg="true"
deviceinfo_flash_offset_base="0x10000000"
deviceinfo_flash_offset_kernel="0x00008000"
deviceinfo_flash_pagesize="2048"
deviceinfo_flash_heimdall_partition_kernel="BOOT"
deviceinfo_dtb="exynos4412-i9300"
deviceinfo_append_dtb="true"
deviceinfo_flash_kernel_on_update="true"
deviceinfo_create_initfs_extra="true"
```

**Install/flash deep-dive.** Samsung phones don't speak fastboot; they use
**Odin download mode** over the proprietary protocol, driven on Linux by the
open-source **Heimdall**. Enter download mode (Vol-Down + Home + Power).
pmbootstrap builds a normal Android boot.img (Exynos offsets, 2 KiB pages) and
flashes it to the **`BOOT`** partition via
`heimdall flash --BOOT boot.img`, and the rootfs image to the target partition
via `heimdall flash --<ROOTFS>`. Actions come from the `heimdall-bootimg`
flasher. **No A/B**, **no fastbootd**, no TWRP needed. The rescue environment is
simply download mode itself (S-Boot in BROM), which is essentially unbrickable
for normal flashes. The related **`heimdall-isorec`** variant (used by older
Samsungs whose bootloader ignores a separate initramfs) bakes the initramfs into
the kernel and stores the real initfs on `RECOVERY` — worth noting as a
sub-strategy. Alternative distros (e.g. older Ubuntu Touch, Replicant) use the
same Odin/Heimdall route.

→ **Strategy abstraction: `heimdall` (Samsung Odin).** Download-mode,
partition-name-addressed flashing; rescue = download mode; no slots.

---

## Device 5 — Purism Librem 5 (`purism-librem5`)

| Field | Value |
|-------|-------|
| Device | Librem 5 |
| Manufacturer | Purism |
| Codename | `purism-librem5` |
| SoC | **NXP i.MX 8M Quad** (4× Cortex-A53) |
| Architecture | aarch64 |
| Kernel | Vendor-mainline — `linux-purism-librem5` |
| Mainline status | Heavily upstreamed; Purism's tree + mainline |
| Display | ✔ (`mxsfb-drm`), 720×1440 |
| Touch | ✔ |
| GPU | ✔ Vivante GC7000L (etnaviv), accelerated |
| WiFi | ✔ (Redpine/`firmware-siliconlabs-rs9116` era) |
| Bluetooth | ✔ |
| Audio | ✔ incl. call audio |
| Modem | ✔ BM818/PLS8 (M.2, own firmware domain; hardware kill switch) |
| GNSS | ✔ (via modem module) |
| Camera | Partial/WIP |
| NFC | ✔ (hardware present) |
| Fingerprint | ✘ (none) |
| Storage | eMMC + microSD |
| Bootloader | **U-Boot**; **NXP `uuu`** serial-download for recovery/first flash |
| Partition layout | pmOS image on eMMC/SD; U-Boot at `sd_embed_firmware` offset 33 |
| Installation method | `uuu` (USB serial download) or write image to microSD |
| Recovery/rescue | **`uuu` serial-download mode is the rescue path**; SD boot also works |
| Known limitations | Camera WIP; power tuning; large device |
| Source | wiki: `https://wiki.postmarketos.org/wiki/Purism_Librem_5_(purism-librem5)` · pmaports: `device/main/device-purism-librem5` |

**deviceinfo highlights (verified):**
```sh
deviceinfo_flash_method="uuu"
deviceinfo_sd_embed_firmware="u-boot/librem5/phone-boot.img:33"
deviceinfo_boot_part_start="4096"
deviceinfo_mesa_driver="mxsfb-drm"
deviceinfo_dtb="freescale/imx8mq-librem5-r2 freescale/imx8mq-librem5-r3 freescale/imx8mq-librem5-r4"
deviceinfo_append_dtb="false"
```

**Install/flash deep-dive.** The i.MX 8M BROM supports **serial download mode
(SDP)**, driven by NXP's **`uuu`** (mfgtools). The `uuu` flasher runs a
`$UUU_SCRIPT` (`flash_script.lst`) that loads U-Boot into RAM over USB and then
writes the pmOS image to eMMC — this is both the install and the unbrick path.
Alternatively (like the PinePhone) you can write the whole image to microSD and
boot from it, and `embed_firmware()` `dd`s the Librem 5 U-Boot
(`phone-boot.img`) at offset 33. **No fastboot, no A/B, no Android boot.img, no
recovery partition.** Boot proceeds via U-Boot → extlinux/kernel. Hardware kill
switches isolate modem/WiFi/camera. Alternative Linux distros use the identical
`uuu`-or-SD route since the platform is fully open.

→ **Strategy abstraction: `uuu` (NXP serial download).** Host pushes U-Boot over
USB SDP then writes storage; rescue = SDP mode; SD-card fallback shares the
`sdcard` mechanism but the *primary* strategy is `uuu`.

---

## Why these five (rationale)

- **Maximum SoC diversity:** Allwinner A64, Qualcomm SDM845, Qualcomm SDM670,
  Samsung Exynos 4412, NXP i.MX 8M Quad — five different silicon vendors and
  five different memory/boot topologies (SD-boot, fastboot-userdata, dynamic
  super partitions, Odin download, serial download). Two Qualcomm parts are kept
  only because they exercise **genuinely different flash flows** (plain fastboot
  vs fastbootd/dynamic-partitions), which is the whole point.
- **Maximum flash-strategy diversity:** the five map onto **`sdcard`,
  `fastboot`, `fastbootd`, `heimdall`, `uuu`** — five distinct branches a
  porting framework must implement. This directly covers the strategy taxonomy
  the framework needs (with `heimdall-isorec`, `rescue-dd`, `adb-shell-dd`,
  `recovery`, `mtkclient` as further variants to add later).
- **Maturity/quality:** all are actively maintained (`main`/`community`), with
  mainline or near-mainline kernels and working display/touch/WiFi/BT/audio, so
  they are safe, well-documented reference targets.
- **Manufacturer spread:** PINE64, OnePlus, Google, Samsung, Purism — no two
  share vendor tooling or bootloader conventions.

## Consolidated strategy map

| Device | flash_method | Boot image | Writes rootfs to | Slots | Rescue env | **Strategy** |
|--------|-------------|-----------|------------------|-------|-----------|--------------|
| PinePhone | `none` (+u-boot embed) | extlinux via U-Boot | whole SD/eMMC | — | SD precedence / Jumpdrive | `sdcard` |
| OnePlus 6 | `fastboot` | Android boot.img | `userdata` directly | A/B | fastboot (EDL for bricks) | `fastboot` |
| Pixel 3a | `fastboot`+super | Android boot.img | logical part in `super` | A/B | **fastbootd** (recovery ramdisk) | `fastbootd` |
| Galaxy S III | `heimdall-bootimg` | Android boot.img → `BOOT` | rootfs partition | — | Odin download mode | `heimdall` |
| Librem 5 | `uuu` | U-Boot/extlinux | eMMC (or SD) | — | `uuu` SDP mode | `uuu` |

---

## Caveats & uncertainties (stated honestly)

- The **wiki** (feature ✔/✘ matrices, install prose) could not be fetched — it
  is behind an Anubis JS challenge. Per-feature statuses above are my best
  synthesis from pmaports `depends`/firmware/SoC-glue evidence and general pmOS
  knowledge; they should be **re-verified against each live wiki page** before
  being treated as authoritative (some "Partial/WIP" marks, especially
  camera/NFC/fingerprint/modem, change frequently).
- `deviceinfo_header_version` and several offsets are *not* set in every sampled
  deviceinfo; the full recognized set is taken from the pmbootstrap parser
  (authoritative), not from any single device file.
- **N900**: I verified it uses `deviceinfo_flash_method="0xffff"` and ships a
  `uboot-script.cmd`, but `0xffff` is present in pmbootstrap's `flash_methods`
  list yet **absent from the `flashers` implementation dict**, so I did not claim
  a `pmbootstrap flasher`-driven flow for it. It was therefore excluded from the
  final five in favor of the Librem 5's clearly-distinct `uuu`.
- Exact partition indices (e.g. sargo's `mmcblk0p68/69`, FP4's `/dev/sda10`) are
  quoted verbatim from deviceinfo and can change across firmware revisions.
