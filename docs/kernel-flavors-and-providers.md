# Kernel: device-support providers and per-distro flavors

This is the most important architectural idea in MobileLinux, and it comes
directly from how the reference port actually works.

## The insight

The kernel for a device — **source + patches + device tree** — is the same no
matter which distribution runs on top. In the reference port, postmarketOS
(Alpine) and Kali (Debian) share **108 byte-identical patches and the same
DTB**. The *only* difference between them is the kernel **`.config`**: Kali adds
the NetHunter symbols (USB WiFi injection, SDR, BadUSB HID, CAN, NFS server,
extended netfilter) plus `CONFIG_MODULE_ALLOW_BTF_MISMATCH`.

So the model is:

```
kernel source + patches + DTB     ← shared "device-support base" (the provider)
            │
            ├── flavor: pmos   = base config                     → postmarketOS
            └── flavor: kali   = base config + kali.fragment      → Kali (Debian)
```

## Providers

A **provider** is where the shared kernel work (the pmaports-style *aport*:
APKBUILD + patches + base config + `deviceinfo`) comes from.

```yaml
kernel:
  provider:
    kind: postmarketos            # postmarketos | custom | none
    upstreamed: false             # is it merged into official pmaports?
    source: https://github.com/d4rks1d33/postmarketos-motorola-rhodep
    pmaports_ref: d4rks1d33/pmaports@motorola-rhodep   # open MR: pmaports!9234
    aport_dir: assets/kernel/provider   # migrated shared aport in this repo
    linux_pkg: linux-motorola-rhodep
    device_pkg: device-motorola-rhodep
```

Key point: **a device does not have to be in official pmaports.** `rhodep`
isn't — it lives in the porter's own fork/repo. Set `upstreamed: false` and
point `source` at your repo. When your MR merges, flip `upstreamed: true` and
set `pmaports_ref` to the upstream path. This is exactly the situation a new
porter is in: they have working kernel support in their own repo before (or
instead of) upstreaming it.

Official pmOS devices (the 5 imported ones) set `upstreamed: true` and point at
`gitlab.com/postmarketOS/pmaports`.

## Flavors

A **flavor** is a per-distro config **delta**, expressed as a Kconfig
**fragment** merged onto the base config — not a full 12k-line config. This
keeps the delta auditable (94 symbols for Kali) and prevents silent drift
between two near-identical full configs.

```yaml
kernel:
  base_config: assets/kernel/provider/config-base.aarch64   # pmOS-clean base
  patches_dir: assets/kernel/patches                        # shared by all flavors
  flavors:
    pmos:
      config_fragment: assets/kernel/flavors/pmos.fragment   # empty = base
      distros: [postmarketos]
      discriminator: { symbol: CONFIG_RT2800USB, present: false }
    kali:
      config_fragment: assets/kernel/flavors/kali.fragment
      distros: [kali]
      discriminator: { symbol: CONFIG_RT2800USB, present: true }
```

The **discriminator** is a symbol that proves which flavor's config is active.
For rhodep, `CONFIG_RT2800USB` is present (`=m`) only in Kali. The build verifies
it, so you always know which flavor compiled — the same
`grep -c RT2800USB <config>` check the reference port documents (1+ = Kali,
0 = pmOS).

## Building a flavor

```bash
mobilelinux kernel rhodep --flavor kali      # or: --distro kali
```

What it does (the real "active-aport swap" mechanic):

1. **Merge** `base_config + kali.fragment` → the flavor config (+ `make olddefconfig`).
2. **Stage the active aport**: copy the shared APKBUILD + patches + the merged
   config into `~/.local/var/pmbootstrap/cache_git/pmaports/device/testing/linux-<codename>/`.
   *Whatever sits in that directory is what pmbootstrap builds* — that is the
   swap.
3. **Verify the discriminator** (so you don't build the wrong flavor by mistake).
4. `pmbootstrap checksum <linux_pkg>` (mandatory after any config/patch change —
   the config sha is in the APKBUILD `source=`) then `pmbootstrap build --force`.
5. The apk lands in `~/.local/var/pmbootstrap/packages/edge/aarch64/`.

## Handoff to Debian-based distros

Alpine/pmOS installs the kernel as an **apk**. Debian-based distros
(Kali/Debian/Ubuntu) can't use an apk, so the apk is repackaged into a
`linux-image-<KVER>.deb` that debos installs into the rootfs:

```yaml
kernel:
  build:
    deb_package: true    # apk -> linux-image .deb for debos
```

`mobilelinux build rhodep --distro kali` runs the kernel stage with the `kali`
flavor, produces the `.deb`, and hands it to the Kali debos backend. The kernel
work is **never redone** per distro — only the config flavor and the packaging
change. (Arch-based distros would build the kernel with `make`/PKGBUILD instead;
they don't inherit the pmOS aport the same way — set `provider.kind: none`.)

## Why fragments, not full configs

Storing two full configs (as the reference repos do today) risks silent drift
— the reference had a 63-line unexplained drift between its two configs. A
fragment is the *intended* delta, reviewed on its own. MobileLinux reconstructs
the Kali config from `base + kali.fragment` and it matches the original to
within one cosmetic line.

## Editing modules interactively

You rarely want to hand-edit a fragment. Use the interactive editor, which is
backed by a **curated catalog** (`os-distros/kali/kernel-catalog.yaml`) that
groups the pentest symbols into human categories with descriptions and presets:

```bash
# show the current state, grouped by category
mobilelinux kernel-config rhodep --flavor kali --show

# interactive terminal menu (toggle categories, set symbols, apply presets)
mobilelinux kernel-config rhodep --flavor kali

# non-interactive one-liners
mobilelinux kernel-config rhodep --flavor kali --preset wifi-only
mobilelinux kernel-config rhodep --flavor kali --enable CONFIG_USB_HACKRF
mobilelinux kernel-config rhodep --flavor kali --disable CONFIG_CAN_ISOTP

# defer to the kernel's native menuconfig (needs a prepared kernel tree)
mobilelinux kernel-config rhodep --flavor kali --menuconfig
```

Categories in the Kali catalog include: **USB Wi-Fi injection** (RT2800USB,
RTL8187, CARL9170, MT7601U, ZD1211RW…), **SDR** (HackRF/Airspy/MSI2500),
**BadUSB/HID gadget**, **CAN bus**, **NFS server**, **extended netfilter /
legacy iptables**, **USB Bluetooth**, **USB serial**, **Android binder**, and
**module/debug tooling**. A special **distro-compat** category holds the
symbols that must never be removed (`MODULE_ALLOW_BTF_MISMATCH`,
`INTERCONNECT_QCOM_SM6375`, `REGULATOR_FAN53870`).

Presets:

| Preset | What it enables |
|--------|-----------------|
| `nethunter-full` | everything (the reference Kali flavor) |
| `nethunter-minimal` | WiFi injection + netfilter + HID |
| `wifi-only` | just USB Wi-Fi injection |

Changes are written back to the flavor's fragment, so they flow into the next
`mobilelinux kernel <device> --flavor kali` build. To add categories/symbols for
a new distro, drop a `kernel-catalog.yaml` in that distro's `os-distros/<distro>/`.

## For porters

To support a new distro on an existing device, you usually only add **one
fragment** and map it to the distro:

```yaml
flavors:
  debian:
    config_fragment: assets/kernel/flavors/debian.fragment
    distros: [debian, ubuntu]
```

No new patches, no new DTB, no forked kernel. See [porting.md](porting.md) and
[import-pmos.md](import-pmos.md).
