# Porting Guide: A Worked, End-to-End Walkthrough

This is the **narrative, worked walkthrough** for a developer porting a *new*
device to MobileLinux, start to finish. It complements the field-by-field
reference in [porting.md](porting.md) and [device-schema.md](device-schema.md):
where those tell you *what every key means*, this guide tells you *what to do, in
order, and why*, using the real devices already in this repo as evidence.

Read these alongside it — this guide links into them rather than repeating them:

- [device-schema.md](device-schema.md) — the authoritative field reference.
- [porting.md](porting.md) — the step-by-step reference (numbered fields).
- [kernel-flavors-and-providers.md](kernel-flavors-and-providers.md) — the kernel model.
- [import-pmos.md](import-pmos.md) — the pmaports importer in detail.
- [os-distros.md](os-distros.md) — how a distro is layered on shared device support.

The end goal is one file — `devices/<vendor>/<codename>/device.yaml` — plus its
`assets/`, that validates against `schema/device.schema.json` and **honestly**
reflects what works.

Worked references you'll see cited throughout (all shipped under `devices/`):

| Device | SoC | Boot | Install strategy | OTA |
|--------|-----|------|------------------|-----|
| `motorola/rhodep` (Moto G82 5G) | qualcomm sm6375 | android-bootimg | `rescue-dd` | `ab` |
| `oneplus/enchilada` (OnePlus 6) | qualcomm sdm845 | android-bootimg | `fastboot` | `ab` |
| `google/sargo` (Pixel 3a) | qualcomm sdm670 | android-bootimg | `fastbootd` | `ab` |
| `samsung/m0` (Galaxy S III) | samsung exynos4412 | android-bootimg | `heimdall` | `single-rootfs` |
| `pine64/pinephone` | allwinner a64 | uboot-extlinux | `sdcard` | `single-rootfs` |
| `purism/librem5` | nxp imx8mq | uboot-extlinux | `uuu` | `single-rootfs` |

`rhodep` is the flagship, fully-worked example (an out-of-tree port that is *not*
yet in official pmaports); the other five were seeded by importing from pmaports.

---

## The mental model (read this first)

Everything below only makes sense once this clicks:

> **Hardware support is shared. A distro is just a config flavor + userland on
> top of it.**

Concretely, the *device-support base* for a phone is:

```
kernel source  +  patches  +  DTB  +  base kernel config  +  firmware  +  device packages
```

That base is **identical no matter which distribution you run**. postmarketOS
(pmOS) is the **provider** of that base — it ships the pmaports-style *aport*
(an `APKBUILD` + `deviceinfo` + base config + ordered `*.patch` files) that
pmbootstrap builds. A distribution (Kali, Debian, Ubuntu, …) sits on top and
contributes only two things:

1. a **kernel config flavor** — a small Kconfig *delta* (fragment) merged onto
   the shared base config, and
2. **userland** — the packages/desktop the distro ships.

Nothing else changes between distros. In the reference `rhodep` port, pmOS
(Alpine) and Kali (Debian) **share 108 byte-identical patches and the same DTB**;
the *only* kernel difference is the `.config`. Kali's flavor adds the NetHunter
symbols (USB Wi-Fi injection, SDR, BadUSB HID, CAN bus, NFS server, extended
netfilter) plus `CONFIG_MODULE_ALLOW_BTF_MISMATCH`. That delta is captured in
`devices/motorola/rhodep/assets/kernel/flavors/kali.fragment`, and its presence
is proven by a single **discriminator** symbol: `CONFIG_RT2800USB` is `=m` for
Kali and absent for pmOS. That one symbol tells you which flavor actually
compiled (`grep -c RT2800USB <config>`: 1+ = Kali, 0 = pmOS).

Picture it:

```
kernel source + patches + DTB + base config      ← shared device-support base (provider = pmOS)
            │
            ├── flavor: pmos   = base config (empty fragment)          → postmarketOS
            └── flavor: kali   = base config + kali.fragment (delta)    → Kali (Debian)
```

Two consequences that will save you time:

- **A device does NOT need to be in official pmaports.** The provider's `source`
  can point at *your own fork/repo*. `rhodep` lives in
  `github.com/d4rks1d33/postmarketos-motorola-rhodep` with `upstreamed: false`
  and an open MR (`postmarketOS/pmaports!9234`). You can have working support in
  a fork long before (or instead of) upstreaming.
- **Adding a new distro to a working device is usually one fragment.** No new
  patches, no new DTB, no forked kernel — just a config delta mapped to the
  distro. See [os-distros.md](os-distros.md).

Now the steps.

---

## Step 0 — Find existing support

Before writing anything, gather evidence. You are looking for two things: a
kernel that boots the device, and a record of what hardware works.

- **postmarketOS wiki.** Search for the device page. pmOS records the per-feature
  hardware matrix here **as prose** ("What works / What doesn't"). This is the
  single richest source for your `hardware:` statuses — but it's text, so it
  cannot be imported automatically. Save the URL for `sources.postmarketos_wiki`.
  (rhodep's is `https://wiki.postmarketos.org/wiki/Motorola_Moto_G82_5G_(motorola-rhodep)`.)
- **pmaports.git.** Look for `device/<tier>/device-<vendor>-<codename>/` and the
  matching `linux-<vendor>-<codename>` kernel package. See
  [research-pmos-devices.md](research-pmos-devices.md) for how pmOS lays this out.
- **Your own pmOS port.** If you already have a working kernel in your own repo
  (the rhodep situation), *that* is your provider — you don't need upstream at all.

### What a pmaports aport actually contains

A pmOS device aport is a directory with:

```
device/<tier>/device-<vendor>-<codename>/
    APKBUILD        # packaging metadata for the device-* package
    deviceinfo      # shell-sourceable key=value hardware/boot facts
```

and a **separate** kernel package `linux-<vendor>-<codename>/` with:

```
linux-<vendor>-<codename>/
    APKBUILD                    # kernel package build recipe
    config-<codename>.aarch64   # the kernel .config
    *.patch                     # ordered patches applied to the kernel tree
```

The `deviceinfo` is a flat set of `deviceinfo_<key>="value"` lines: identity,
`flash_method`, boot geometry (`header_version`, `flash_pagesize`,
`flash_offset_*`, `generate_bootimg`), `dtb`, `append_dtb`, `kernel_cmdline`,
`rootfs_image_sector_size`. These are exactly the structured facts the importer
consumes. What `deviceinfo` does **not** contain is the hardware matrix or the
install quirks — those live in wiki prose and in your head.

---

## Step 1 — Create the device definition

There are two ways in. If pmaports already has the device, **import**; otherwise,
start **from scratch**.

### Path (a): Import from pmaports

```bash
mobilelinux import path/to/pmaports/device/<tier>/device-<vendor>-<codename>
# or point directly at the deviceinfo file:
mobilelinux import path/to/.../device-<vendor>-<codename>/deviceinfo
```

The importer (`src/mobilelinux/importers/pmaports.py`) parses `deviceinfo` and
writes a **draft** to `devices/<vendor>/<id>/device.yaml`. It refuses to
overwrite an existing file, so re-running is safe.

**What it auto-fills** (from structured `deviceinfo` fields):

| Auto-filled | From |
|-------------|------|
| `vendor`, `id`, `model`, `codename`, `aliases`, `architecture`, `chassis` | identity keys (`id` = last codename segment: `oneplus-enchilada` → `enchilada`) |
| `install.strategy` | `flash_method` via `_FLASH_TO_STRATEGY` |
| `install.unlock_required` | `true` only when `flash_method == fastboot` |
| `boot.method` | `android-bootimg` if `generate_bootimg == true`, else `uboot-extlinux` |
| `boot.android_bootimg.*` | `header_version`, `flash_pagesize`, `flash_offset_*`, `kernel_cmdline` |
| `device_tree.dtb` / `append_dtb` | `dtb` (with `.dtb` appended) / `append_dtb` |
| `storage.rootfs_sector_size` / `rootfs_layout` | `rootfs_image_sector_size` (`4096` → `gpt-in-partition`, else `plain`) |
| `kernel` block | a conservative provider (`kind: postmarketos`, `upstreamed: false`, `source` = pmaports) + a single `pmos` flavor + `build.method: pmbootstrap` |
| `ota` | default `single-rootfs`, `rollback: false` |
| `tests` | starter list `[boot, display, touch, storage, usb, wifi]` |

**What is left as TODO** (because it isn't in `deviceinfo`):

- **The hardware matrix.** The importer writes `status: untested` for exactly
  `display, touchscreen, gpu, storage, usb, wifi, bluetooth, audio, battery,
  charging` and *nothing else*. You add the rest and promote from evidence.
- **`install.steps`** — seeded with a single `message` action saying the import
  is incomplete. You write the real ordered steps.
- **`soc.vendor` / `soc.family`** — come in as `unknown`.
- **`kernel.version`** — comes in as `unknown`.
- **Provider upstream status + extra flavors** — the importer cannot know if the
  device is upstream, nor which distros you want. You set `upstreamed`/
  `pmaports_ref` (if upstream) or `kind: custom` + `source: <your repo>` (if
  not), and add flavors (e.g. a `kali` fragment) yourself.

The draft's own `sources.notes` reminds you: *"AUTO-IMPORTED DRAFT. Hardware
statuses are 'untested' — fill them in from the wiki + real tests. Verify
install.steps and SoC."* All five non-rhodep reference devices were seeded this
way. Full details: [import-pmos.md](import-pmos.md).

### Path (b): From scratch

If pmaports doesn't have the device (or you'd rather write it by hand), create
`devices/<vendor>/<codename>/device.yaml`. The required top-level keys are
`schema_version, id, vendor, model, codename, architecture, soc, kernel,
hardware, boot, install`. A **minimal skeleton**:

```yaml
schema_version: 1

id: <codename>
vendor: <vendor>
model: <Human Model Name>
codename: <codename>
architecture: aarch64          # aarch64 | armv7 | armhf | x86_64 | riscv64
maturity: testing              # main | community | testing | downstream | experimental

soc:
  vendor: <qualcomm|allwinner|samsung|nxp|mediatek|...>
  family: <sm6375|sdm845|a64|exynos4412|imx8mq|...>
  # marketing_name / platform optional

kernel:
  type: mainline               # mainline | stable | downstream | vendor
  version: "6.6"
  source: https://cdn.kernel.org/pub/linux/kernel/
  provider:
    kind: postmarketos         # postmarketos | custom | none
    upstreamed: false          # true ONLY if merged into official pmaports
    source: https://github.com/<you>/postmarketos-<codename>   # or official pmaports
    aport_dir: assets/kernel/provider
    linux_pkg: linux-<vendor>-<codename>
    device_pkg: device-<vendor>-<codename>
  base_config: assets/kernel/provider/config-base.aarch64      # shared base
  patches_dir: assets/kernel/patches                           # shared by all flavors
  flavors:
    pmos:
      config_fragment: assets/kernel/flavors/pmos.fragment     # empty = base
      distros: [postmarketos]
  build:
    method: pmbootstrap
    image: Image               # some Android bootloaders reject Image.gz — see Step 2
    deb_package: false         # true for Debian-based distros (Kali/Debian/Ubuntu)

device_tree:
  dtb: <soc>-<vendor>-<codename>.dtb
  source: in-kernel            # in-kernel | in-tree path | out-of-tree path
  append_dtb: false            # true if bootloader wants DTB concatenated to Image

firmware:
  redistributable: true        # false if blobs must be extracted from the device
  provides: [wifi, bluetooth, gpu, modem]

hardware:
  # Start everything you cannot PROVE at 'untested'. Never inflate.
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
  method: android-bootimg      # android-bootimg | uboot-extlinux | uboot-raw | efi | custom
  android_bootimg:
    header_version: 2
    pagesize: 2048
    cmdline: ""
  initramfs:
    type: postmarketos         # postmarketos | dracut | mkinitcpio | initramfs-tools | custom

storage:
  rootfs_layout: plain         # plain | gpt-in-partition | whole-disk
  rootfs_sector_size: 512      # 512 | 4096 (UFS devices use 4096)

install:
  strategy: fastboot           # see Step 5 for the full enum
  unlock_required: true
  boot_partition: boot
  steps:
    - action: message
      description: "install.steps incomplete — fill from device evidence"

ota:
  strategy: single-rootfs      # single-rootfs | ab
  rollback: false

tests: [boot, display, touch, storage, usb, wifi]

sources:
  imported_from: manual        # pmaports | manual | nethunter-rhodep-repo
  postmarketos_wiki: <wiki url>
  pmaports: device/<tier>/device-<vendor>-<codename>
```

Each block, briefly (full reference in [device-schema.md](device-schema.md)):

- **identity** (`id`/`vendor`/`model`/`codename`/`architecture`/`chassis`/
  `maturity`) — who the device is; [device-schema.md#id](device-schema.md#id).
- **`soc`** — vendor + family + optional marketing/platform; drives DTB subdir
  and firmware layout.
- **`kernel`** — the shared base + per-distro flavors (Step 2 & 3).
- **`device_tree`** — the DTB filename and how it's delivered.
- **`firmware`** — redistributable or extracted-from-device; runtime mounts.
- **`hardware`** — the evidence-based support matrix (Step 4).
- **`boot`** — how the kernel is booted (bootimg geometry / U-Boot / EFI) + initramfs.
- **`storage`** — partition facts install/OTA need.
- **`install`** — the flash strategy + ordered steps (Step 5).
- **`ota`** — update capability (Step 6).
- **`tests`** — which hardware test modules apply (Step 6).

Place all assets under `devices/<vendor>/<codename>/assets/` and reference them
with **paths relative to the device directory**, exactly as rhodep does
(`assets/kernel/...`, `assets/packages/...`, `assets/userspace/...`).

---

## Step 2 — Wire the kernel provider + shared base

This is where you tell MobileLinux where the shared device-support base comes
from. Read [kernel-flavors-and-providers.md](kernel-flavors-and-providers.md) for
the full model; here's how to fill it for a new device.

### The provider block

```yaml
kernel:
  provider:
    kind: postmarketos          # postmarketos | custom | none
    upstreamed: false
    source: https://github.com/<you>/postmarketos-<codename>
    pmaports_ref: <you>/pmaports@<vendor>-<codename>   # fork branch, or upstream path
    aport_dir: assets/kernel/provider
    linux_pkg: linux-<vendor>-<codename>
    device_pkg: device-<vendor>-<codename>
```

- **`kind`** — `postmarketos` = a pmaports-style aport built with pmbootstrap;
  `custom` = same layout but from a non-upstream repo; `none` = built directly
  with `make` (pair with `build.method: make`, e.g. Arch-style PKGBUILD devices).
- **`upstreamed`** — `true` **only** if merged into official pmaports. For a new
  port this is almost always `false`. rhodep is `false`.
- **`source`** — the git URL of the aport. Either official pmaports
  (`https://gitlab.com/postmarketOS/pmaports`) **or your own fork/repo**. rhodep
  points at `github.com/d4rks1d33/postmarketos-motorola-rhodep`.
- **`pmaports_ref`** — the upstream path if merged
  (`device/testing/device-<vendor>-<codename>`), or a fork branch. rhodep uses
  `d4rks1d33/pmaports@motorola-rhodep` (open MR `postmarketOS/pmaports!9234`).
- **`aport_dir`** — where you **migrate the shared aport into this repo**: the
  `APKBUILD`, base config, patches, and `deviceinfo`. rhodep keeps this in
  `assets/kernel/provider/` (with a `device-motorola-rhodep/` subdir).
- **`linux_pkg`** / **`device_pkg`** — the `linux-*` package pmbootstrap builds
  and swaps into the active aport dir, and the `device-*` package.

**Migrate your working kernel here.** If you already have a booting kernel (your
own repo or a pmaports fork), copy its `APKBUILD`, its base `.config`, and its
ordered `*.patch` files into `aport_dir` / `patches_dir`. rhodep carries ~110
numbered patches in `assets/kernel/patches/` (108 are byte-identical across pmOS
and Kali). Keep them ordered and numbered.

### Shared base config + patches

```yaml
kernel:
  base_config: assets/kernel/provider/config-base.aarch64   # pmOS-clean base
  patches_dir: assets/kernel/patches                        # shared by ALL flavors
```

- **`base_config`** is the pmOS-clean config the provider ships. Every flavor is
  a fragment merged **onto** this — never a full duplicate config.
- **`patches_dir`** is the ordered patch set applied to the tree, shared by all
  flavors.

### The `Image` vs `Image.gz` gotcha

```yaml
kernel:
  build:
    image: Image     # NOT Image.gz for some bootloaders
```

Pick the `arch/*/boot` image the bootloader accepts. Most devices take `Image` or
`Image.gz`. **Some Android bootloaders — notably Motorola ABL — reject the
self-decompressing `Image.gz`/EFI-zboot image and require a flat `Image`.**
rhodep sets `image: Image` for exactly this reason (its `device.yaml` note reads:
*"the Motorola ABL bootloader resets on a self-decompressing EFI-zboot image, so
a FLAT Image must be used"*). **Symptom:** the device silently resets right after
the bootloader hands off. If you see that, suspect this first.

---

## Step 3 — Add kernel flavors (per distro)

A **flavor** is a per-distro config **delta**, expressed as a Kconfig fragment
merged onto `base_config`. Never a full 12k-line config — a fragment is the
*intended* delta, reviewable on its own, and it prevents silent drift between two
near-identical full configs.

```yaml
kernel:
  flavors:
    pmos:
      config_fragment: assets/kernel/flavors/pmos.fragment   # empty = base
      distros: [postmarketos]
      discriminator: { symbol: CONFIG_RT2800USB, present: false }
    kali:
      config_fragment: assets/kernel/flavors/kali.fragment   # NetHunter delta
      distros: [kali]
      discriminator: { symbol: CONFIG_RT2800USB, present: true }
      notes: NetHunter delta (USB WiFi inject, SDR, BadUSB HID, CAN, NFS, netfilter) + BTF-mismatch
```

### The pmOS flavor: empty fragment = base

The `pmos` flavor's fragment is **empty** — pmOS just uses the base config as-is.
That's the whole point: pmOS *is* the provider, so its config is the base.

### The distro flavor: a config fragment (delta)

For Kali/Debian, the flavor is a fragment containing only the symbols that
differ. You produce it two ways:

**Generate a fragment from an existing full distro config** (the common starting
point when you already have a working Kali `.config`): diff it against the base.
Conceptually, the fragment is *"every symbol where the Kali config differs from
the base config"* — enabled symbols become `CONFIG_X=m`/`=y` lines, disabled ones
become `# CONFIG_X is not set`. MobileLinux's merge is the inverse: it takes
`base + fragment` and the fragment symbols override the base (see
`_merge_config` / `_python_merge` in `src/mobilelinux/core/kernel.py`), then runs
`make olddefconfig`. In the reference port, reconstructing the Kali config from
`base + kali.fragment` matches the original to within one cosmetic line.

**Edit it interactively** rather than by hand, using the curated catalog:

```bash
# show current state, grouped by category
mobilelinux kernel-config <device> --flavor kali --show

# interactive terminal menu (toggle categories, set symbols, apply presets)
mobilelinux kernel-config <device> --flavor kali

# non-interactive one-liners
mobilelinux kernel-config <device> --flavor kali --preset wifi-only
mobilelinux kernel-config <device> --flavor kali --enable CONFIG_USB_HACKRF
mobilelinux kernel-config <device> --flavor kali --disable CONFIG_CAN_ISOTP

# defer to the kernel's native menuconfig (needs a prepared kernel tree)
mobilelinux kernel-config <device> --flavor kali --menuconfig
```

The editor (`src/mobilelinux/core/kconfig.py`) is backed by a **catalog** at
`os-distros/kali/kernel-catalog.yaml` that groups the pentest symbols into human
**categories** (USB Wi-Fi injection, SDR, BadUSB/HID, CAN bus, NFS server,
extended netfilter, USB Bluetooth, USB serial, Android binder, module/debug
tooling) with descriptions, and offers **presets**:

| Preset | What it enables |
|--------|-----------------|
| `nethunter-full` | everything (the reference Kali flavor) |
| `nethunter-minimal` | WiFi injection + netfilter + HID |
| `wifi-only` | just USB Wi-Fi injection |

Changes are written back to the flavor's fragment, so they flow into the next
`mobilelinux kernel <device> --flavor kali` build. To add categories/symbols for
a new distro, drop a `kernel-catalog.yaml` in that distro's `os-distros/<distro>/`.

### The discriminator (proves which flavor is active)

Each flavor carries a `discriminator`: a symbol whose presence proves *this*
flavor's config is the one that compiled. For rhodep, `CONFIG_RT2800USB` is `=m`
only in Kali. `build_kernel` verifies it (`_verify_discriminator` in
`src/mobilelinux/core/kernel.py`) so you never build the wrong flavor by
accident — the same `grep -c RT2800USB <config>` check the reference documents
(1+ = Kali, 0 = pmOS). Pick a symbol that is *unique to the flavor* and unlikely
to be pulled in by dependencies.

### Distro-compat symbols you must NOT remove

The catalog has a special **`distro_compat`** category (marked `base: true`) that
presets never disable, because removing them breaks the distro:

- `CONFIG_MODULE_ALLOW_BTF_MISMATCH` — required for Debian/Kali module loading.
- `CONFIG_INTERCONNECT_QCOM_SM6375`, `CONFIG_REGULATOR_FAN53870` — hardware-enabling
  base symbols for this SoC.

If you author a fragment by hand, keep these in.

### `deb_package` for Debian-family distros

```yaml
kernel:
  build:
    deb_package: true    # apk -> linux-image .deb for debos
```

Alpine/pmOS installs the kernel as an **apk**. Debian-based distros
(Kali/Debian/Ubuntu) can't consume an apk, so MobileLinux repackages the built
apk into a `linux-image-<KVER>.deb` (`build_linux_image_deb` in
`src/mobilelinux/core/kernel.py`) that debos installs into the rootfs. Set
`deb_package: true` for any debian-family distro. The kernel work is **never
redone** per distro — only the config flavor and the packaging change. (Arch-based
distros build the kernel with `make`/PKGBUILD instead and set
`provider.kind: none`; see [os-distros.md](os-distros.md).)

### Building a flavor (dry-run to see the plan)

```bash
mobilelinux kernel <device> --flavor kali      # or: --distro kali
```

This: merges `base_config + kali.fragment`; **stages the active aport** (copies
the shared APKBUILD + patches + merged config into
`~/.local/var/pmbootstrap/cache_git/pmaports/device/testing/linux-<codename>/` —
*whatever sits there is what pmbootstrap builds*); verifies the discriminator;
then `pmbootstrap checksum` + `pmbootstrap build --force`. `--distro` selects the
flavor whose `distros` list contains that distro; `--flavor` names it explicitly.
Everything is planned unless `--execute` (pmbootstrap ops are dangerous and need
`--allow-dangerous`).

---

## Step 4 — Set hardware statuses honestly (the evidence rule)

This is the heart of the port and the one place you must not cut corners.

> **Never mark a feature `supported` without a passing test or documented evidence.**

Use the status enum precisely (from `$defs.hardwareFeature.status`):

- **`supported`** — works. Cite `evidence` (a test name, doc path, or wiki line)
  and set `test:` to the module that verifies it.
- **`partial`** — works with caveats; list them under `caveats`. (rhodep's
  `display`, `modem`, `gnss`, `nfc` are `partial` — e.g. modem does GSM/2G
  voice+SMS but LTE watchdog-resets the SoC.)
- **`broken`** — hardware present but breaks/unsafe (rhodep `camera`: sensor
  probe fails on mainline).
- **`untested`** — no evidence yet. The honest default; imports arrive fully
  `untested`.
- **`unsupported`** — won't work on mainline.
- **`not-present`** — the device physically lacks the hardware. Use this (not
  `unsupported`) so the feature is **excluded from the support percentage**.

Each feature can carry `driver`, `evidence`, `notes`, `caveats[]`, `weight`, and
`test`. Look at rhodep's matrix for the gold standard: every promoted status has
`evidence` and a `test`, and caveats are specific ("BT address is firmware
default", "monitor mode not feasible on internal chip; use USB WiFi").

**The support percentage is weighted and excludes `not-present`.** Core features
(display/boot) weigh more; `partial` earns half credit; `untested`/`broken` earn
none. So an honest matrix produces an honest score — don't inflate statuses to
move the number. Weights and credits live in `src/mobilelinux/core/model.py` and
are documented at
[device-schema.md#support-percentage-weighted-not-present-excluded](device-schema.md#support-percentage-weighted-not-present-excluded).

Check your work two ways:

```bash
mobilelinux check <device>     # reads the DECLARED matrix + prints the support %
mobilelinux test <device>      # PROBES the real hardware on the device itself
mobilelinux test <device> --only wifi,bluetooth   # a subset
```

`check` reads what you declared; `test` (run on the device) proves it. Promote a
status from `untested` only after `test` passes or you have a cited doc. See
[testing.md](testing.md).

---

## Step 5 — Declare the install strategy

Do **not** assume `fastboot flash userdata` works — verify on real hardware.
First record the hard facts in `storage.partitions[].writable_via`; that is what
drives the choice. Then map the observed bootloader capability to a strategy:

| Observed capability | `install.strategy` | Real example |
|---------------------|--------------------|--------------|
| Standard bootloader fastboot writes all target partitions | `fastboot` | `oneplus/enchilada` |
| Dynamic/super partitions; needs userspace fastboot | `fastbootd` | `google/sargo` |
| Bootloader denies writing the rootfs partition → boot a rescue env and `dd` | `rescue-dd` | `motorola/rhodep` |
| Push over adb shell and `dd` from the running OS/recovery | `adb-shell-dd` | — |
| Samsung Odin / download mode | `heimdall` (or `heimdall-isorec`) | `samsung/m0` |
| Boots from removable media | `sdcard` | `pine64/pinephone` |
| NXP i.MX serial download | `uuu` | `purism/librem5` |
| MediaTek BROM | `mtkclient` | — |
| Recovery-driven | `recovery` | — |
| Anything else | `custom` | — |

The six shipped devices deliberately cover six different strategies — study the
one closest to your bootloader.

Then write ordered `install.steps` from the action enum (`flash-partition`,
`dd-partition`, `set-active-slot`, `reboot`, `reboot-bootloader`,
`reboot-fastbootd`, `enter-rescue`, `wait-transport`, `run-remote`, `sideload`,
`message`). Each step can set `partition`, `image` (`boot`/`rootfs`/`rescue`/a
filename), `slot`, `via` (`fastboot`/`fastbootd`/`dd`/`heimdall`/`uuu`/`recovery`),
`destructive`, and (for `run-remote`) `command`. Mark every partition-writing
step `destructive: true`.

A simple `fastboot` device is just flash-boot → flash-rootfs → reboot. The
`rescue-dd` case is the instructive hard one — rhodep's `userdata` is
`writable_via: [dd]` (fastboot is *denied* by Motorola ABL), so it needs a
**rescue block** plus a longer sequence:

```yaml
install:
  strategy: rescue-dd
  ab_slots: true
  slots: [a, b]
  boot_partition: boot_a
  rootfs_target: { partition: userdata, method: dd }
  rescue:
    required: true
    method: pmos-debug-shell        # pmos-debug-shell | recovery | jumpdrive | uuu-ramboot | sdcard-boot | custom
    boot_image: build
    build_from: "@INPUT_BOOT_IMG@"
    transport: telnet://172.16.42.1:23
    notes: >
      pmOS kernel+DTB+initramfs with 'pmos.debug-shell' appended. Brings up
      USB-gadget networking + root telnet WITHOUT mounting root, so userdata can
      be dd-written. Also used for recovery.
  steps:
    - { action: message, description: "rescue-dd: userdata cannot be written by fastboot on this device" }
    - { action: flash-partition, partition: boot_a, image: rescue, via: fastboot, destructive: true }
    - { action: set-active-slot, slot: a, via: fastboot }
    - { action: reboot, via: fastboot }
    - { action: wait-transport, description: "Wait for the rescue telnet at 172.16.42.1:23" }
    - { action: dd-partition, partition: userdata, image: rootfs, via: dd, destructive: true }
    - { action: flash-partition, partition: boot_a, image: boot, via: fastboot, destructive: true }
    - { action: set-active-slot, slot: a, via: fastboot }
    - { action: reboot, via: fastboot }
    - { action: message, description: "First boot resizes the rootfs and starts the desktop" }
```

Note how `storage` backs this up: `userdata` is `writable_via: [dd]` with a note
that `fastboot flash userdata` is denied by ABL, and `rootfs_layout:
gpt-in-partition` (the rootfs image is itself a GPT disk written *inside* the
Android `userdata` partition, sector size 4096 for UFS). See
[install.md](install.md), [flash.md](flash.md), and the rescue mechanics in
[recovery.md](recovery.md).

---

## Step 6 — Declare OTA capabilities and tests

### OTA

Match `ota.strategy` to the hardware:

- **`ab`** — A/B slots present; updates go to the inactive slot with rollback.
  Set `slots`, `rollback: true`, and `bootloader_integration.type`
  (`android-bootctl` on Android/Qualcomm via qbootctl) + `backend` (e.g.
  `rauc-custom`). rhodep, `oneplus/enchilada`, `google/sargo` use this.
- **`single-rootfs`** — no second slot; in-place updates, usually no rollback.
  Default for U-Boot/SD-card devices (`pine64/pinephone`, `purism/librem5`,
  `samsung/m0`).

Use `ota.updatable` to declare what an OTA may touch (`rootfs`, `kernel`,
`initramfs`, `device_packages`, `firmware`, `bootloader`). Keep `firmware` and
especially `bootloader` `false` unless you have an extremely clear, safe
mechanism — **never auto-update a bootloader casually**. rhodep's example:

```yaml
ota:
  strategy: ab
  rollback: true
  slots: [a, b]
  bootloader_integration: { type: android-bootctl, backend: rauc-custom }
  updatable:
    rootfs: true
    kernel: true
    initramfs: true
    device_packages: true
    firmware: false
    bootloader: false        # never auto-update the Motorola bootloader
```

Full model: [ota.md](ota.md) and
[device-schema.md#ab-vs-single-rootfs](device-schema.md).

### Tests

List the applicable test modules in `tests[]` (see `testing/tests/`) and set each
`hardware.<feature>.test` to the module that verifies it. Only declare tests that
actually apply — **CI rejects a `test:` or `tests[]` entry that has no matching
module**.

---

## Step 7 — Validate and iterate

Run these locally before opening a PR. Everything supports a plan/dry-run mode so
nothing touches a device until you say so.

```bash
mobilelinux validate <id>                    # validate this device against the schema
mobilelinux validate                         # validate ALL devices
mobilelinux check <id>                        # hardware-support report + support %
mobilelinux kernel <id> --flavor <f>          # dry-run: see the kernel build plan
mobilelinux build <id> --distro <d> --dry-run # dry-run: see the full build plan
mobilelinux flash <id> --dry-run              # print the install plan; write nothing
python ci/validate.py                         # full CI gate
```

`ci/validate.py` additionally verifies the schema self-check, every device, that
`install.strategy` is one of the *implemented* strategy backends, and that every
`hardware.<f>.test` and `tests[]` entry resolves to a real test module. The
`--dry-run` mode prints every command and runs nothing (always exits cleanly);
real writes require `--execute` and `--allow-dangerous` (see the execution-safety
model in [build.md](build.md#execution-model-dry-run--execute--allow-dangerous)
and [architecture.md](architecture.md)).

Iterate: as tests pass and evidence accumulates, promote hardware statuses from
`untested` upward and raise `maturity` (`testing` → `community` → `main`) to
match reality.

---

## Step 8 — Contribute upstream (optional)

Two independent things you can upstream:

1. **The pmOS device-support base → official pmaports.** Open an MR against
   `postmarketOS/pmaports` with your `device-*` and `linux-*` aports (this is the
   rhodep situation: open MR `postmarketOS/pmaports!9234`, still in a fork). While
   it's a fork, keep `provider.upstreamed: false` and `provider.source` pointing
   at your repo. **When the MR merges, flip `provider.upstreamed: true`** and set
   `pmaports_ref` to the upstream path
   (`device/<tier>/device-<vendor>-<codename>`), leaving `source` as pmaports.
   Official pmOS devices already do this.

2. **The device definition → this repo.** Add your
   `devices/<vendor>/<codename>/device.yaml` plus its `assets/`. Make sure
   `mobilelinux validate <id>` and `python ci/validate.py` pass, statuses are
   evidence-based, and asset paths are relative to the device directory.

---

## Checklist — the minimal artifacts a new device needs

Before you call a port done, you should have all of these:

- [ ] **`APKBUILD`** — the provider aport's kernel build recipe (migrated into
      `assets/kernel/provider/`).
- [ ] **Patches** — the ordered, numbered `*.patch` set shared by all flavors
      (`assets/kernel/patches/`, referenced by `kernel.patches_dir`).
- [ ] **Base kernel config** — the pmOS-clean base
      (`assets/kernel/provider/config-base.aarch64`, referenced by
      `kernel.base_config`).
- [ ] **`deviceinfo`** — migrated into the provider aport dir alongside the APKBUILD.
- [ ] **DTB name** — `device_tree.dtb`, with `append_dtb` set correctly for the
      bootloader.
- [ ] **Per-distro fragment(s)** — at least a `pmos` flavor (empty = base); a
      `kali`/`debian` fragment + `build.deb_package: true` for Debian-family
      distros, each with a `discriminator`.
- [ ] **Install strategy** — `install.strategy` mapped from real
      `storage.partitions[].writable_via` facts, with ordered `install.steps`
      (and a `rescue` block if the bootloader won't write the rootfs).
- [ ] **Hardware statuses** — an evidence-based `hardware:` matrix; nothing
      `supported` without a passing test; absent hardware marked `not-present`.
- [ ] **Tests** — a `tests[]` list and per-feature `test:` modules that all
      resolve (CI-checked).
- [ ] **Provenance** — `sources.postmarketos_wiki` + `sources.pmaports`, and the
      correct `kernel.provider` (`upstreamed` + `source`/`pmaports_ref`).
- [ ] **Green validation** — `mobilelinux validate <id>`, `mobilelinux check <id>`,
      `mobilelinux flash <id> --dry-run`, and `python ci/validate.py` all pass.

---

*See also:* [porting.md](porting.md) (field reference),
[device-schema.md](device-schema.md) (schema),
[kernel-flavors-and-providers.md](kernel-flavors-and-providers.md),
[import-pmos.md](import-pmos.md), [os-distros.md](os-distros.md),
[install.md](install.md), [flash.md](flash.md), [recovery.md](recovery.md),
[ota.md](ota.md), [testing.md](testing.md), [build.md](build.md),
[architecture.md](architecture.md).
