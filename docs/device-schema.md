# Device Definition Schema

The device definition (`devices/<vendor>/<codename>/device.yaml`) is the single
source of truth for a device: identity, SoC, kernel, device-tree, firmware,
hardware status, boot, install/flash strategy, OTA capabilities and hardware
tests. Adding a new device should mostly mean adding one of these files plus its
`assets/`.

The authoritative field list is `schema/device.schema.json` (JSON Schema, draft
2020-12), validated in CI. This document is a reference for humans; when the two
disagree, the schema wins. See also [architecture.md](architecture.md) for how
the definition flows through the framework, and [porting.md](porting.md) for a
step-by-step porter guide.

Top-level `additionalProperties` is `false`: unknown keys are a validation
error. Required top-level keys are:

```
schema_version, id, vendor, model, codename, architecture, soc, kernel,
hardware, boot, install
```

Examples throughout are drawn from the flagship device
`devices/motorola/rhodep/device.yaml` (Moto G82 5G).

---

## `schema_version`

Integer, `const: 1`. Version of the schema the file targets. Bump only when the
schema changes incompatibly.

```yaml
schema_version: 1
```

## `id`

String matching `^[a-z0-9][a-z0-9-]*$`. Short unique id, usually the codename.
Note that for pmaports imports the id is the *full* pmOS codename (e.g.
`oneplus-enchilada`), while the hand-written rhodep uses the short form
`rhodep`.

```yaml
id: rhodep
```

## `vendor`

String. Manufacturer slug, e.g. `motorola`, `pine64`, `qualcomm`. Also the first
path segment under `devices/`.

```yaml
vendor: motorola
```

## `model`

String. Human model name.

```yaml
model: Moto G82 5G
```

## `codename`

String. Vendor / pmOS codename.

```yaml
codename: rhodep
```

## `aliases`

Array of strings. Other names the device is known by (marketing names, alternate
codenames).

```yaml
aliases: [motorola-rhodep, XT2225-1]
```

## `year`

Integer. Release year.

```yaml
year: 2022
```

## `chassis`

String enum, default `handset`:

```
handset, tablet, laptop, convertible, desktop, watch, embedded
```

```yaml
chassis: handset
```

## `architecture`

String enum (required). CPU architecture (`aarch64` for arm64 phones):

```
aarch64, armv7, armhf, x86_64, riscv64
```

```yaml
architecture: aarch64
```

## `soc`

Object (required). Requires `vendor` and `family`. `additionalProperties: false`.

| Field | Meaning |
|-------|---------|
| `vendor` | SoC vendor: `qualcomm`, `allwinner`, `samsung`, `nxp`, `mediatek`, ... |
| `family` | SoC family/platform, e.g. `sm6375`, `sdm845`, `a64`, `exynos4412`, `imx8mq`. |
| `marketing_name` | e.g. `Snapdragon 695 5G`. |
| `platform` | pmOS soc-glue / platform group, e.g. `holi`, `qcom-sdm845`. |

```yaml
soc:
  vendor: qualcomm
  family: sm6375
  marketing_name: Snapdragon 695 5G
  platform: holi
```

## `kernel`

Object (required). Requires `type` and `version`. `additionalProperties: false`.

| Field | Meaning |
|-------|---------|
| `type` | enum: `mainline` (torvalds/linux), `stable` (x.y.z stable tree), `downstream` / `vendor` (forked BSP kernel). |
| `version` | Kernel version string, e.g. `7.2-rc5`, `6.6.30`. |
| `source` | Git URL or tarball URL of the kernel source. |
| `branch` | Git branch. |
| `tag` | Git tag. |
| `config` | Path (relative to device dir) to the kernel `.config`, or a shared config name. |
| `config_fragments` | Array of Kconfig fragments merged on top of `config` (e.g. distro-specific like nethunter). |
| `patches_dir` | Path (relative to device dir) to a directory of ordered `*.patch` files. |
| `build` | Build sub-object (see below). |

### `kernel.build`

`additionalProperties: false`.

| Field | Enum / default | Meaning |
|-------|----------------|---------|
| `toolchain` | `gcc`, `llvm` (default `gcc`) | Compiler toolchain. |
| `method` | `pmbootstrap`, `make`, `kbuild` (default `make`) | How the kernel image is produced. `pmbootstrap` reuses a pmaports APKBUILD. |
| `pmaports_pkg` | string | If `method=pmbootstrap`, the `linux-*` package name. |
| `image` | `Image`, `Image.gz`, `zImage`, `uImage`, `Image.gz-dtb` (default `Image`) | Which `arch/*/boot` image to use. |

**Gotcha:** some bootloaders (Motorola ABL) require a flat `Image`, not
`Image.gz`. rhodep's ABL resets on a self-decompressing EFI-zboot image, so the
flat `Image` is mandatory.

```yaml
kernel:
  type: mainline
  version: 7.2-rc5
  source: https://cdn.kernel.org/pub/linux/kernel/
  config: assets/kernel/config-motorola-rhodep.aarch64
  config_fragments: [assets/kernel/nethunter-config.fragment]
  patches_dir: assets/kernel/patches
  build:
    toolchain: llvm
    method: pmbootstrap
    pmaports_pkg: linux-motorola-rhodep
    image: Image
```

## `device_tree`

Object. `additionalProperties: false`.

| Field | Meaning |
|-------|---------|
| `dtb` | DTB filename produced by the kernel build, e.g. `sm6375-motorola-rhodep.dtb`. |
| `append_dtb` | boolean (default `false`). If true, the DTB is concatenated to the kernel `Image` (Android `append_dtb`). Required by some Motorola bootloaders. |
| `overlays` | Array of overlay filenames. |
| `source` | `in-kernel`, an in-tree path, or an out-of-tree path. |

```yaml
device_tree:
  dtb: sm6375-motorola-rhodep.dtb
  append_dtb: true
  source: in-kernel
```

## `firmware`

Object. `additionalProperties: false`. Describes proprietary/redistributable
blobs and how they reach the rootfs.

| Field | Meaning |
|-------|---------|
| `redistributable` | boolean (default `true`). `false` if blobs are proprietary and must be extracted from the device (never in git). |
| `package` | Name of the firmware package, e.g. `firmware-motorola-rhodep`. |
| `provides` | Array of subsystems the firmware serves: `wifi`, `bluetooth`, `gpu`, `modem`, `adsp`, `cdsp`, `audio`, `nfc`, `display`. |
| `extract_from_device` | Array of blobs that must be extracted from the running/stock device (see below). |
| `runtime_mounts` | Array of partitions mounted read-only at runtime to serve firmware (see below). |

### `firmware.extract_from_device`

For **non-redistributable** blobs. When `redistributable: false`, the firmware
cannot ship in git; each entry documents a blob the porter/user must pull from
the stock device. Each item requires `name` and `source`:

| Field | Meaning |
|-------|---------|
| `name` | Blob path/name in the firmware tree. |
| `source` | Where on the stock device it lives (partition/path). |
| `dest` | Where it must land in the rootfs firmware tree. |
| `provides` | Which subsystem it serves. |

### `firmware.runtime_mounts`

Partitions mounted read-only at runtime to serve firmware (e.g. `modem_a` for
`.mbn`/`.bNN` blobs). Each item requires `source` and `target`:

| Field | Default | Meaning |
|-------|---------|---------|
| `source` | — | e.g. `/dev/disk/by-partlabel/modem_a`. |
| `target` | — | e.g. `/readonly/firmware`. |
| `fstype` | `ext4` | Filesystem type. |
| `options` | `ro,nosuid,nodev,noexec` | Mount options. |

```yaml
firmware:
  redistributable: false     # proprietary; extracted from the device
  package: firmware-motorola-rhodep
  provides: [wifi, bluetooth, gpu, modem, adsp, cdsp, audio, nfc]
  extract_from_device:
    - { name: "ath10k/WCN3990/hw1.0/board-2.bin", source: "stock vendor", provides: wifi }
    - { name: "qca/crbtfw21.tlv", source: "stock vendor", provides: bluetooth }
    - { name: "aw88261_acf.bin", source: "stock vendor (super)", dest: "firmware/aw88261_acf.bin", provides: audio }
  runtime_mounts:
    - { source: /dev/disk/by-partlabel/modem_a, target: /readonly/firmware, fstype: ext4, options: "ro,nosuid,nodev,noexec" }
```

## `hardware`

Object (required). Per-feature support status. `additionalProperties` is the
`hardwareFeature` schema (so extra features beyond the named ones are allowed).
Named features include: `display`, `touchscreen`, `gpu`, `storage`, `usb`,
`wifi`, `bluetooth`, `audio`, `battery`, `charging`, `modem`, `gnss`, `nfc`,
`fingerprint`, `camera`, `sensors`, `vibrator`.

Each feature (`hardwareFeature`, `additionalProperties: false`) requires
`status` and supports:

| Field | Meaning |
|-------|---------|
| `status` | enum (see below). |
| `weight` | number (default `1.0`). Relative importance for the overall support percentage. |
| `driver` | Kernel driver / mechanism, e.g. `ath10k_snoc`. |
| `evidence` | Where the status comes from (doc path, test name, wiki). Required *in spirit* for `supported`. |
| `notes` | Free text. |
| `caveats` | Array of strings. |
| `test` | Name of the test module that verifies this feature (must reference a real test module — CI enforces this). |

### Status enum — statuses MUST be evidence-based

```
supported    works
partial      works with caveats
broken       present but breaks/unsafe
untested     no evidence
unsupported  won't work on mainline
not-present  device lacks the hardware
```

**Never claim `supported` without a passing test or documented evidence.** The
schema description states this explicitly, and the whole point of the framework
is to formalize the hardware matrix that pmOS leaves as wiki prose. A
freshly-imported device starts every feature at `untested` (see
[import-pmos.md](import-pmos.md)); the porter promotes each status only as
evidence appears.

### Support percentage (weighted; `not-present` excluded)

The overall support percentage is computed in
`src/mobilelinux/core/model.py`. Each feature's `weight` defaults to a
per-feature value in `DEFAULT_WEIGHTS` (core bring-up features weigh more):

```
display 3.0  touchscreen 3.0  storage 3.0  usb 2.0  gpu 2.0  wifi 2.0
battery 2.0  charging 2.0  audio 1.5  modem 1.5  bluetooth 1.0  sensors 1.0
gnss 0.5  nfc 0.5  fingerprint 0.5  camera 0.5  vibrator 0.25
```

Each status earns "credit" toward the score via `STATUS_SCORE`:

```
supported   1.0
partial     0.5
broken      0.0
untested    0.0
unsupported 0.0
not-present None  (excluded from the denominator)
```

The computation (`Device.support_score` / `support_percent`) sums
`weight * credit` over every feature *except* `not-present` ones, which are
excluded from both numerator and denominator:

```
percent = round(100 * sum(weight*credit) / sum(weight))   # over non-"not-present" features
```

So a device that genuinely lacks NFC should mark it `not-present` (not
`unsupported`) so it doesn't drag the score down. `partial` earns half credit;
`broken`, `untested` and `unsupported` earn none but still count against the
maximum.

```yaml
hardware:
  display:
    status: partial
    driver: msm-dsi + DSC1.1 cmd-mode + Novatek NT37701 panel
    evidence: docs/display-cmd-dma-window-wip; patch 0098 auto-recovery verified
    caveats:
      - "brightness-change underflow glitch under compositor; auto-recovers"
    test: display
  gpu:
    status: supported
    driver: Adreno 619 (freedreno/msm, a6xx GMU-wrapper)
    evidence: OpenGL(KWin composites), Vulkan(Turnip), OpenCL(rusticl) all working
    test: gpu
  fingerprint:
    status: untested
    evidence: no driver or documentation found
  camera:
    status: broken
    driver: CAMSS present; sensor probe fails
    evidence: CAMERA-SENSORS-FEASIBILITY.md; tools/camdiag (s5kjn1 probe fails)
    test: camera
```

## `device_packages`

Array. Device-specific packages/config that make the hardware work — the
declarative form of the old `rhodep-*.deb` and userspace installers, applied on
top of any distro rootfs. Each item requires `name`, `layer`, `purpose`;
`additionalProperties: false`.

| Field | Enum / default | Meaning |
|-------|----------------|---------|
| `name` | — | Package name. |
| `layer` | `generic`, `soc`, `vendor`, `device` | Reuse scope (generic=any phone → device=this device only). Maps to audit classes A/B/C/D. |
| `purpose` | — | One line: what breaks if this is absent. |
| `source` | — | Path (relative to device or shared layer dir) to the package source/installer. |
| `kind` | `deb`, `installer-script`, `systemd-dropin`, `modprobe`, `udev`, `config` (default `deb`) | Form of the package. |
| `critical` | boolean (default `false`) | If true, must be present for a usable phone (radio/boot/display). |

```yaml
device_packages:
  - name: rhodep-modem-support
    layer: soc
    critical: true
    kind: deb
    purpose: "mounts modem_a, symlinks .mbn firmware, starts remoteprocs; without it: no radio/WiFi/BT"
    source: assets/packages/rhodep-modem-support
  - name: rhodep-ipa-hold
    layer: soc
    critical: true
    kind: modprobe
    purpose: "blacklists ipa.ko (LTE attach watchdog-resets the SoC)"
    source: assets/userspace/modem/rhodep-ipa-hold.conf
```

## `first_boot`

Object. `additionalProperties: false`. Actions applied on first boot (the
declarative form of manual "first boot fixes").

| Field | Meaning |
|-------|---------|
| `mask_services` | systemd units to mask, e.g. `droid-juicer.service`, `systemd-repart.service`. |
| `enable_services` | systemd units to enable. |
| `resize_rootfs` | boolean (default `false`). Grow the root filesystem to fill its partition on first boot. |
| `run` | Extra commands to run once. |

```yaml
first_boot:
  mask_services:
    - droid-juicer.service
    - systemd-repart.service
  resize_rootfs: true
  enable_services: []
```

## `boot`

Object (required). Requires `method`. `additionalProperties: false`.

| Field | Meaning |
|-------|---------|
| `method` | enum: `android-bootimg`, `uboot-extlinux`, `uboot-raw`, `efi`, `custom`. How the kernel is booted. |
| `android_bootimg` | Parameters for building an Android boot image (when `method=android-bootimg`). |
| `initramfs` | Initramfs configuration. |

### `boot.android_bootimg`

`additionalProperties: false`.

| Field | Enum / default | Meaning |
|-------|----------------|---------|
| `header_version` | `0`,`1`,`2`,`3`,`4` (default `2`) | Android boot image header version. |
| `pagesize` | integer (default `2048`) | Flash page size. |
| `base` | string | Base address. |
| `offsets` | object with `kernel`, `ramdisk`, `tags`, `second`, `dtb` | Load offsets. |
| `cmdline` | string | Kernel command line baked into the boot image. |
| `os_version` | string | |
| `os_patch_level` | string | |
| `extra_args` | string | |

### `boot.initramfs`

`additionalProperties: false`.

| Field | Enum / default | Meaning |
|-------|----------------|---------|
| `type` | `postmarketos`, `dracut`, `mkinitcpio`, `initramfs-tools`, `custom` (default `postmarketos`) | Initramfs generator. |
| `modules` | array | Kernel modules needed in the initramfs (e.g. `spi_geni_qcom`, `goodix_berlin` for touch). |
| `features` | array | e.g. `usb-gadget-net`, `debug-shell`, `loop-gpt-4096`. |

```yaml
boot:
  method: android-bootimg
  android_bootimg:
    header_version: 2
    pagesize: 4096
    base: "0x00000000"
    offsets:
      kernel: "0x00008000"
      ramdisk: "0x01000000"
      tags: "0x00000100"
      dtb: "0x01f00000"
    cmdline: "earlycon pmos_root_uuid=@ROOT_UUID@ ... rootwait"
  initramfs:
    type: postmarketos
    modules: [spi_geni_qcom, goodix_berlin_core, goodix_berlin_spi]
    features: [usb-gadget-net, debug-shell, loop-gpt-4096]
```

## `storage`

Object. `additionalProperties: false`. Storage/partition facts needed by install
and OTA.

| Field | Enum / default | Meaning |
|-------|----------------|---------|
| `rootfs_layout` | `plain`, `gpt-in-partition`, `whole-disk` (default `plain`) | `gpt-in-partition` = the rootfs image is itself a GPT disk written inside an Android partition (rhodep userdata case). |
| `rootfs_sector_size` | `512`, `4096` (default `512`) | Logical sector size of the rootfs image (UFS devices use `4096`). |
| `partitions` | array | Named partitions relevant to install/OTA (NOT a full GPT dump). |

### `storage.partitions[]`

Requires `name`, `role`. `additionalProperties: false`.

| Field | Meaning |
|-------|---------|
| `name` | Partition label, e.g. `userdata`, `boot_a`, `modem_a`. |
| `role` | enum: `boot`, `rootfs`, `firmware`, `vendor`, `misc`, `vbmeta`, `dtbo`, `persist`, `other`. |
| `device_path` | e.g. `/dev/disk/by-partlabel/userdata`. |
| `writable_via` | array of enum `fastboot`, `fastbootd`, `dd`, `heimdall`, `uuu`, `recovery`. Which mechanisms are **allowed** to write this partition on this device. |
| `notes` | Free text. |

The `writable_via` list encodes hard device facts: rhodep's `userdata` is
`[dd]` only — `fastboot flash userdata` is denied by the Motorola ABL, which is
exactly why the install strategy is `rescue-dd`.

```yaml
storage:
  rootfs_layout: gpt-in-partition
  rootfs_sector_size: 4096
  partitions:
    - name: boot_a
      role: boot
      device_path: /dev/disk/by-partlabel/boot_a
      writable_via: [fastboot]
    - name: userdata
      role: rootfs
      device_path: /dev/disk/by-partlabel/userdata
      writable_via: [dd]            # fastboot flash userdata is DENIED by Motorola ABL
```

## `install`

Object (required). Requires `strategy`. `additionalProperties: false`. How the
device is installed/flashed — a first-class, per-device concern. Do **not**
assume `fastboot flash userdata` works.

### `install.strategy` enum

```
fastboot, fastbootd, rescue-dd, adb-shell-dd, heimdall, heimdall-isorec,
sdcard, recovery, uuu, mtkclient, custom
```

Each maps to an installer backend and is chosen from strategies observed on
**real** devices. Reference strategies across the shipped devices:

| Device | Strategy | Why |
|--------|----------|-----|
| `rhodep` | `rescue-dd` | ABL denies `fastboot flash userdata`; boot a rescue telnet + `dd`. |
| `oneplus-enchilada` | `fastboot` | Plain bootloader fastboot works. |
| `google-sargo` | `fastbootd` | Dynamic/super partitions need userspace fastboot. |
| `samsung-m0` | `heimdall` | Samsung Odin/download mode. |
| `pine64-pinephone` | `sdcard` | Boots from removable media. |
| `purism-librem5` | `uuu` | NXP i.MX serial-download (uuu). |

### Other `install` fields

| Field | Default | Meaning |
|-------|---------|---------|
| `ab_slots` | `false` | Does the device use Android A/B slots? |
| `slots` | — | e.g. `[a, b]`. |
| `fastbootd` | `false` | Does the device have fastbootd (userspace fastboot)? |
| `unlock_required` | `true` | Bootloader unlock required before flashing. |
| `boot_partition` | — | Partition the boot image is flashed to, e.g. `boot_a`. |
| `rootfs_target` | — | `{ partition, method }` where `method` ∈ `fastboot`, `fastbootd`, `dd`, `heimdall`, `uuu`, `sdcard-image`. |
| `rescue` | — | Rescue/debug environment (see below). |
| `steps` | — | Ordered install steps (see below). |

### `install.rescue`

`additionalProperties: false`. Optional rescue/debug environment used to write
partitions the bootloader won't (reused for recovery).

| Field | Enum / default | Meaning |
|-------|----------------|---------|
| `required` | `false` | Is a rescue env required to install? |
| `method` | `pmos-debug-shell`, `recovery`, `jumpdrive`, `uuu-ramboot`, `sdcard-boot`, `custom` | Rescue mechanism. |
| `boot_image` | — | Asset path of the prebuilt rescue image, or `build` to build it. |
| `build_from` | — | Base boot image the rescue image is derived from. |
| `transport` | — | How the host reaches the rescue env, e.g. `telnet://172.16.42.1:23`. |
| `notes` | — | Free text. |

### `install.steps[]` — action enum

Ordered, declarative install steps interpreted by the installer backend (not raw
shell). Each requires `action`. `additionalProperties: false`.

```
flash-partition, dd-partition, set-active-slot, reboot, reboot-bootloader,
reboot-fastbootd, enter-rescue, wait-transport, run-remote, sideload, message
```

Other step fields: `description`, `partition`, `image` (artifact key: `boot`,
`rootfs`, `rescue`, or a filename), `slot`, `via` (enum `fastboot`, `fastbootd`,
`dd`, `heimdall`, `uuu`, `recovery`), `destructive` (boolean), `command` (for
`run-remote`, executed inside the rescue/OS env).

```yaml
install:
  strategy: rescue-dd
  ab_slots: true
  slots: [a, b]
  unlock_required: true
  boot_partition: boot_a
  rootfs_target: { partition: userdata, method: dd }
  rescue:
    required: true
    method: pmos-debug-shell
    boot_image: build
    transport: telnet://172.16.42.1:23
  steps:
    - action: flash-partition
      partition: boot_a
      image: rescue
      via: fastboot
      destructive: true
    - action: set-active-slot
      slot: a
      via: fastboot
    - action: reboot
      via: fastboot
    - action: wait-transport
      description: "Wait for the rescue telnet at 172.16.42.1:23"
    - action: dd-partition
      partition: userdata
      image: rootfs
      via: dd
      destructive: true
```

## `ota`

Object. `additionalProperties: false`. Over-the-air update capabilities —
declares what the hardware allows; the framework degrades gracefully.

| Field | Enum / default | Meaning |
|-------|----------------|---------|
| `strategy` | `ab`, `single-rootfs` (default `single-rootfs`) | See below. |
| `rollback` | boolean (default `false`) | Can a failed update roll back? |
| `slots` | array | e.g. `[a, b]`. |
| `bootloader_integration` | object | How slot selection / boot success is recorded. |
| `updatable` | object | Which components an OTA may update. |

### `ab` vs `single-rootfs`

- **`ab`** — the device has two rootfs/boot slots (A/B). An update is written to
  the inactive slot and the device reboots into it; if boot fails, it rolls back
  to the known-good slot. Requires slot tracking (see
  `bootloader_integration`). rhodep and the two Snapdragon imports
  (`oneplus-enchilada`, `google-sargo`) use `ab`.
- **`single-rootfs`** — no second slot; updates are applied in place. Rollback is
  generally not available. The pmaports importer defaults every device to
  `single-rootfs` (safe default); U-Boot/SD-card devices such as
  `pine64-pinephone`, `purism-librem5` and `samsung-m0` stay here.

### `ota.bootloader_integration`

`additionalProperties: false`.

| Field | Enum / default | Meaning |
|-------|----------------|---------|
| `type` | `android-bootctl`, `uboot-bootcount`, `efi-boot-assessment`, `none` (default `none`) | How slot selection / boot success is recorded. `android-bootctl` uses the Android misc partition (qbootctl). |
| `backend` | `rauc-custom`, `rauc-uboot`, `rauc-grub`, `none` (default `none`) | Update backend. |

### `ota.updatable`

`additionalProperties: false`. Booleans (with defaults): `rootfs` (`true`),
`kernel` (`true`), `initramfs` (`true`), `device_packages` (`true`), `firmware`
(`false`), `bootloader` (`false` — almost always; never auto-update a bootloader
without an extremely clear, safe mechanism).

```yaml
ota:
  strategy: ab
  rollback: true
  slots: [a, b]
  bootloader_integration:
    type: android-bootctl        # slot selection via Android misc (qbootctl)
    backend: rauc-custom
  updatable:
    rootfs: true
    kernel: true
    firmware: false
    bootloader: false            # never auto-update the Motorola bootloader
```

## `tests`

Array of strings. Which hardware test modules apply to this device (see
`testing/tests/`). Any name referenced here — and any `hardware.<f>.test` — must
resolve to a real test module; CI (`ci/validate.py`) fails otherwise.

```yaml
tests:
  - boot
  - display
  - touch
  - gpu
  # ...
```

## `sources`

Object. `additionalProperties: false`. Provenance: where this device support
comes from.

| Field | Meaning |
|-------|---------|
| `postmarketos_wiki` | Wiki URL. |
| `pmaports` | Path in pmaports.git, e.g. `device/community/device-oneplus-enchilada`. |
| `imported_from` | enum: `pmaports`, `manual`, `nethunter-rhodep-repo` (default `manual`). |
| `notes` | Free text. |

```yaml
sources:
  imported_from: nethunter-rhodep-repo
  postmarketos_wiki: https://wiki.postmarketos.org/wiki/Motorola_Moto_G82_5G_(motorola-rhodep)
  pmaports: device/testing/device-motorola-rhodep
```

## `maturity`

String enum (default `testing`). Overall maturity tier (mirrors pmOS
categories):

```
main, community, testing, downstream, experimental
```

```yaml
maturity: community
```
