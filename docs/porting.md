# Porting a New Device

This is the step-by-step guide to adding a device to MobileLinux. The end goal
is a single `devices/<vendor>/<codename>/device.yaml` (plus its `assets/`) that
validates against `schema/device.schema.json` and honestly reflects what works.

Read [device-schema.md](device-schema.md) for the field reference and
[architecture.md](architecture.md) for how the definition flows through
build/flash/test/OTA. If the device already exists in postmarketOS, start with
[import-pmos.md](import-pmos.md) and then come back here to fill in the parts the
importer cannot.

Worked references (all shipped in `devices/`):

| Device | SoC | Boot | Install strategy | OTA |
|--------|-----|------|------------------|-----|
| `motorola/rhodep` (Moto G82 5G) | qualcomm sm6375 | android-bootimg | `rescue-dd` | `ab` |
| `oneplus/enchilada` (OnePlus 6) | qualcomm sdm845 | android-bootimg | `fastboot` | `ab` |
| `google/sargo` (Pixel 3a) | qualcomm sdm670 | android-bootimg | `fastbootd` | `ab` |
| `samsung/m0` (Galaxy S III) | samsung exynos4412 | android-bootimg | `heimdall` | `single-rootfs` |
| `pine64/pinephone` | allwinner a64 | uboot-extlinux | `sdcard` | `single-rootfs` |
| `purism/librem5` | nxp imx8mq | uboot-extlinux | `uuu` | `single-rootfs` |

---

## 1. Find existing support

Before writing anything, gather evidence:

- **postmarketOS wiki** — search for the device page. This is where pmOS records
  the per-feature hardware matrix as prose ("What works / doesn't work"). Note
  the wiki URL for `sources.postmarketos_wiki`.
- **pmaports.git** — look for `device/<tier>/device-<vendor>-<codename>/`
  (`deviceinfo` + `APKBUILD`) and the matching `linux-<vendor>-<codename>`
  kernel package. Note the path for `sources.pmaports`. See
  [research-pmos-devices.md](research-pmos-devices.md).

If pmaports has the device, run the importer to get a draft (see
[import-pmos.md](import-pmos.md)) — it fills identity, arch, boot geometry, DTB,
cmdline and maps the flash method to a strategy. It cannot fill the hardware
matrix or the exact install steps; those you complete from evidence.

## 2. Create the device file

Either import a draft:

```
mobilelinux import path/to/pmaports/device/community/device-<vendor>-<codename>
```

or start from scratch by creating
`devices/<vendor>/<codename>/device.yaml`. A **minimal skeleton** that satisfies
the required keys (`schema_version, id, vendor, model, codename, architecture,
soc, kernel, hardware, boot, install`):

```yaml
schema_version: 1

id: <codename>
vendor: <vendor>
model: <Human Model Name>
codename: <codename>
architecture: aarch64
maturity: testing

soc:
  vendor: <qualcomm|allwinner|samsung|nxp|mediatek|...>
  family: <sm6375|sdm845|a64|exynos4412|imx8mq|...>

kernel:
  type: mainline
  version: "6.6"
  provider:
    kind: postmarketos          # postmarketos | custom | none
    upstreamed: false           # true only if merged into official pmaports
    source: https://github.com/<user>/postmarketos-<codename>   # or pmaports
    aport_dir: assets/kernel/provider
    linux_pkg: linux-<vendor>-<codename>
    device_pkg: device-<vendor>-<codename>
  base_config: assets/kernel/provider/config-base.aarch64
  patches_dir: assets/kernel/patches
  flavors:
    pmos:
      config_fragment: assets/kernel/flavors/pmos.fragment   # empty = base
      distros: [postmarketos]
  build:
    method: pmbootstrap
    image: Image
    deb_package: false          # true for Debian-based distros (Kali/Debian/Ubuntu)

device_tree:
  dtb: <soc>-<vendor>-<codename>.dtb
  source: in-kernel

hardware:
  # Start everything you cannot yet prove at 'untested'.
  display:     { status: untested }
  touchscreen: { status: untested }
  gpu:         { status: untested }
  storage:     { status: untested }
  usb:         { status: untested }
  wifi:        { status: untested }
  bluetooth:   { status: untested }
  battery:     { status: untested }
  charging:    { status: untested }

boot:
  method: android-bootimg
  android_bootimg:
    header_version: 2
    pagesize: 2048
    cmdline: ""
  initramfs:
    type: postmarketos

install:
  strategy: fastboot
  unlock_required: true
  boot_partition: boot
  steps:
    - action: message
      description: "install.steps incomplete — fill from device evidence"

tests: [boot, display, touch, storage, usb, wifi]

sources:
  imported_from: manual
  postmarketos_wiki: <wiki url>
  pmaports: device/<tier>/device-<vendor>-<codename>
```

Place device assets (kernel config, patches, packages, scripts) under
`devices/<vendor>/<codename>/assets/` and reference them with paths relative to
the device directory (as rhodep does with `assets/kernel/...`).

## 3. Add the kernel (provider + flavor model)

The kernel for a device — **source + patches + device tree** — is the same no
matter which distribution runs on top. Only the kernel **`.config`** differs
per distro. MobileLinux encodes this as a **provider** (the shared
device-support base) plus per-distro **flavors** (config deltas). Read
[kernel-flavors-and-providers.md](kernel-flavors-and-providers.md) for the full
model; this section is how you fill it in for a new device.

### 3.1 Kernel basics

- **`type`** — `mainline` for torvalds/linux, `stable` for an x.y.z tree,
  `downstream`/`vendor` for a forked BSP.
- **`version`** — the version string (e.g. `7.2-rc5`, `6.6.30`).
- **`source`** — git/tarball URL of the kernel source.

### 3.2 The provider (`kernel.provider`)

A **provider** is where the shared kernel work — the pmaports-style *aport*
(APKBUILD + patches + base config + `deviceinfo`) — comes from. Fill:

- **`kind`** — `postmarketos` (a pmaports-style aport built with pmbootstrap),
  `custom` (same layout but from a non-upstream repo), or `none` (built
  directly with `make`, e.g. Arch-style PKGBUILD devices).
- **`upstreamed`** — `true` **only** if the device is merged into official
  pmaports; `false` if you are using your own fork/repo.
- **`source`** — the git URL of the aport source. Either official pmaports
  (`https://gitlab.com/postmarketOS/pmaports`) **or** the porter's own repo,
  e.g. `github.com/<user>/postmarketos-<codename>`.
- **`pmaports_ref`** — if it's upstream or an MR exists, the pmaports
  path/branch (e.g. `device/testing/device-<vendor>-<codename>`, or a fork
  branch like `<user>/pmaports@<vendor>-<codename>`).
- **`aport_dir`** — path (relative to the device dir) to the **migrated shared
  aport** you keep in this repo: APKBUILD + base config + patches + deviceinfo.
- **`linux_pkg`** — the `linux-*` package name pmbootstrap builds and the one
  swapped into the active aport dir (e.g. `linux-motorola-rhodep`).
- **`device_pkg`** — the `device-*` package name (e.g. `device-motorola-rhodep`).

**Important: a device does NOT have to be upstream in pmaports.** A porter who
already has working kernel support in their own repo simply sets
`upstreamed: false` and points `source` at that repo. rhodep is exactly this
case — it lives in `d4rks1d33/postmarketos-motorola-rhodep` (open MR
`postmarketOS/pmaports!9234`), **not** official pmaports yet:

```yaml
kernel:
  provider:
    kind: postmarketos
    upstreamed: false          # not merged upstream — use the fork below
    source: https://github.com/d4rks1d33/postmarketos-motorola-rhodep
    pmaports_ref: d4rks1d33/pmaports@motorola-rhodep
    aport_dir: assets/kernel/provider
    linux_pkg: linux-motorola-rhodep
    device_pkg: device-motorola-rhodep
```

When your MR merges upstream, flip `upstreamed: true` and set `pmaports_ref` to
the upstream path. Official pmOS devices set `upstreamed: true` and point
`source` at `gitlab.com/postmarketOS/pmaports`.

### 3.3 Shared base config + patches

The patches and base config are **shared by all flavors**:

- **`base_config`** — path to the shared BASE kernel config (the pmOS-clean
  config the provider ships). Flavors are fragments merged onto this.
- **`patches_dir`** — directory of ordered `*.patch` files applied to the tree,
  shared by **all** flavors. rhodep carries ~110 numbered patches in
  `assets/kernel/patches/` (108 are byte-identical across pmOS and Kali); keep
  them ordered and numbered.

### 3.4 Flavors (`kernel.flavors`)

A **flavor** is a per-distro config **delta**, expressed as a Kconfig
**fragment** merged onto `base_config` — never a full 12k-line config. Each
flavor maps to one or more `distros` and carries a `discriminator`: a symbol
whose presence proves that flavor's config is the active one, verified before
build.

```yaml
kernel:
  base_config: assets/kernel/provider/config-base.aarch64
  patches_dir: assets/kernel/patches
  flavors:
    pmos:
      config_fragment: assets/kernel/flavors/pmos.fragment   # empty = base
      distros: [postmarketos]
      discriminator: { symbol: CONFIG_RT2800USB, present: false }
    kali:
      config_fragment: assets/kernel/flavors/kali.fragment   # NetHunter delta
      distros: [kali]
      discriminator: { symbol: CONFIG_RT2800USB, present: true }
```

To support another distro on the same device you usually add just **one
fragment** and map it to the distro — no new patches, no new DTB, no fork.

### 3.5 Building a flavor

```bash
mobilelinux kernel <device> --flavor kali     # or: --distro kali
```

This merges `base_config + <flavor>.fragment`, stages the shared aport
(APKBUILD + patches + merged config) into the active aport dir pmbootstrap
builds, verifies the discriminator, then checksums and builds. `--distro`
selects the flavor whose `distros` list contains that distro; `--flavor` names
it explicitly.

### 3.6 Build options (`kernel.build`)

- **`method`** — `pmbootstrap` reuses the provider aport's APKBUILD;
  `make`/`kbuild` build directly (pair with `provider.kind: none`).
- **`image` gotcha** — pick the `arch/*/boot` image the bootloader accepts.
  Most devices take `Image` or `Image.gz`. **Some Android bootloaders (notably
  Motorola ABL) reject the self-decompressing `Image.gz` and require a flat
  `Image`.** rhodep sets `image: Image` for exactly this reason. If the device
  silently resets right after the bootloader hands off, suspect this.
- **`deb_package`** — set `true` for Debian-based distros (Kali/Debian/Ubuntu).
  Alpine/pmOS installs the kernel as an **apk**; Debian distros can't, so the
  apk is repackaged into a `linux-image-<KVER>.deb` that debos installs into the
  rootfs. The kernel work is never redone per distro — only the config flavor
  and the packaging change.

## 4. Add the device tree

Fill `device_tree`:

- **`dtb`** — the DTB filename the kernel build produces (e.g.
  `sm6375-motorola-rhodep.dtb`).
- **`source`** — `in-kernel` if the DTS is upstream/in your patch set, otherwise
  an in-tree or out-of-tree path.
- **`append_dtb`** — set `true` if the bootloader expects the DTB concatenated
  to the kernel `Image` (Android `append_dtb`). rhodep needs this; many
  U-Boot devices do not (they load the DTB separately).

```yaml
device_tree:
  dtb: sm6375-motorola-rhodep.dtb
  append_dtb: true
  source: in-kernel
```

## 5. Define firmware

Decide whether firmware is redistributable:

- **Redistributable** (default) — set `firmware.package` and list what it
  `provides`. The blobs can ship in git / a package.
- **Non-redistributable** — set `redistributable: false` and enumerate each blob
  under `extract_from_device` with its `source` (where on the stock device it
  lives), optional `dest`, and `provides`. These are pulled from the user's own
  device, never committed. rhodep's WiFi/BT/GPU/audio blobs are all extracted
  this way.
- **`runtime_mounts`** — if firmware is served live from a device partition
  (e.g. Qualcomm `.mbn` served from `modem_a`), declare a read-only mount with
  `source`, `target`, and safe `options` (default `ro,nosuid,nodev,noexec`).

```yaml
firmware:
  redistributable: false
  provides: [wifi, bluetooth, gpu, modem]
  extract_from_device:
    - { name: "ath10k/WCN3990/hw1.0/board-2.bin", source: "stock vendor", provides: wifi }
  runtime_mounts:
    - { source: /dev/disk/by-partlabel/modem_a, target: /readonly/firmware }
```

## 6. Set hardware statuses honestly (the evidence rule)

This is the heart of the port. Statuses **must be evidence-based**. **Never mark
a feature `supported` without a passing test or documented evidence.** Use the
enum precisely:

- `supported` — works; cite `evidence` (a test name, doc, or wiki line) and set
  `test:` to the module that verifies it.
- `partial` — works with caveats; list the `caveats`. (rhodep's `display`,
  `modem`, `gnss`, `nfc` are `partial`.)
- `broken` — hardware present but breaks/unsafe (rhodep `camera`).
- `untested` — no evidence yet. This is the honest default for anything you
  have not verified; imports arrive fully `untested`.
- `unsupported` — won't work on mainline.
- `not-present` — the device physically lacks the hardware. Use this (not
  `unsupported`) so the feature is **excluded from the support percentage**.

The support percentage is weighted (core features weigh more) and excludes
`not-present`; the exact weights and status credits live in
`src/mobilelinux/core/model.py` and are documented in
[device-schema.md](device-schema.md#support-percentage-weighted-not-present-excluded).
Because `partial` earns half credit and `untested`/`broken` earn none, an
honest matrix produces an honest score — don't inflate statuses to move the
number.

Always attach `evidence` when promoting above `untested`, and set `test:` to a
real test module (CI rejects a `test:` that has no module).

## 7. Choose the install strategy

Map the device's actual bootloader capabilities to a strategy. Do **not** assume
`fastboot flash userdata` works — verify on real hardware. Guide:

| Observed capability | `install.strategy` |
|---------------------|--------------------|
| Standard bootloader fastboot writes all target partitions | `fastboot` |
| Dynamic/super partitions; needs userspace fastboot | `fastbootd` |
| Bootloader denies writing the rootfs partition → boot a rescue env and `dd` | `rescue-dd` |
| Push over adb shell and `dd` from the running OS/recovery | `adb-shell-dd` |
| Samsung Odin / download mode | `heimdall` (or `heimdall-isorec`) |
| Boots from removable media | `sdcard` |
| NXP i.MX serial download | `uuu` |
| MediaTek BROM | `mtkclient` |
| Recovery-driven | `recovery` |
| Anything else | `custom` |

Record the hard facts in `storage.partitions[].writable_via` — this is what
drives the choice. rhodep's `userdata` is `writable_via: [dd]` (fastboot is
denied by ABL), which is precisely why its strategy is `rescue-dd` with a
`pmos-debug-shell` rescue env reached over `telnet://172.16.42.1:23`.

Then write ordered `install.steps` from the action enum (`flash-partition`,
`dd-partition`, `set-active-slot`, `reboot`, `reboot-bootloader`,
`reboot-fastbootd`, `enter-rescue`, `wait-transport`, `run-remote`, `sideload`,
`message`). Mark any partition-writing step `destructive: true` and set `via`
and `image` (`boot`, `rootfs`, `rescue`, or a filename). A `fastboot` device is
usually just flash-boot → flash-rootfs → reboot; a `rescue-dd` device is
flash-rescue → set-slot → reboot → wait-transport → dd-rootfs → flash-boot →
reboot (see rhodep's full sequence for the canonical example).

## 8. Declare OTA capabilities

Set `ota.strategy` to match the hardware:

- **`ab`** — A/B slots present; updates go to the inactive slot with rollback.
  Set `slots`, `rollback: true`, and `bootloader_integration.type`
  (`android-bootctl` on Android/Qualcomm devices via qbootctl). rhodep,
  `oneplus-enchilada`, `google-sargo` use this.
- **`single-rootfs`** — no second slot; in-place updates, usually no rollback.
  Default for U-Boot/SD-card devices (`pine64-pinephone`, `purism-librem5`,
  `samsung-m0`).

Use `ota.updatable` to declare which components an OTA may touch. Keep
`firmware` and especially `bootloader` at `false` unless you have an
extremely clear, safe mechanism — never auto-update a bootloader casually.

## 9. Declare which tests apply

List the applicable test modules in `tests` (see `testing/tests/`), and set
each `hardware.<feature>.test` to the module that verifies that feature. Only
declare tests that actually apply to this device. CI enforces that every test
name resolves to a real module — see step 10.

## 10. Validate

Run these before opening a PR:

```
mobilelinux validate <id>        # validate this device against the schema
mobilelinux validate             # validate all devices
mobilelinux check <id>           # hardware-support report + support %
mobilelinux flash <id> --dry-run # print the install plan; run nothing
python ci/validate.py            # full CI: schema self-check, every device,
                                 # strategy is implemented, every test resolves
```

`ci/validate.py` additionally verifies that the `install.strategy` is one of the
implemented strategy backends and that every `hardware.<f>.test` and `tests[]`
entry maps to a real test module. `flash --dry-run` walks your `install.steps`
without executing (see the execution-safety model in
[architecture.md](architecture.md)); real writes require `--execute` and
`--allow-dangerous`.

Iterate: as tests pass and evidence accumulates, promote hardware statuses from
`untested` upward and raise `maturity` (`testing` → `community` → `main`) to
match reality.
