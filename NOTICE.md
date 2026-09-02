# Provenance

This repository was bootstrapped from **`nethunter-rhodep-repo`**, a working
Kali NetHunter Pro port for the Motorola Moto G82 5G (`rhodep`, Qualcomm
SM6375) on a mainline Linux kernel. That repository is used **read-only** as a
reference and as the source of migrated assets; it is never modified by this
project.

Migrated assets and where they now live:

| From `nethunter-rhodep-repo` | To `mobilelinux` | Audit class |
|------------------------------|------------------|-------------|
| `kernel/patches`, `kernel/diag-modules` | `devices/motorola/rhodep/assets/kernel/` | B/D |
| pmOS aport `postmarketos/aports/linux-motorola-rhodep/{APKBUILD,config}` + `device-motorola-rhodep/` | `.../assets/kernel/provider/` (shared device-support base) | B/D |
| Kali config delta (vs pmOS base) | `.../assets/kernel/flavors/kali.fragment` (generated from the real diff) | E |
| `packages/rhodep-*`, `packages/firmware-motorola-rhodep` | `devices/motorola/rhodep/assets/packages/` | B/C/D |
| `userspace/*` (audio, modem, sensors, power, bluetooth, nfc, usb-net, ...) | `devices/motorola/rhodep/assets/userspace/` | B/C/D |
| `scripts/mkbootv2b.py`, `build-rescue-boot.sh`, `make-boot-from-apk.sh`, ... | `devices/motorola/rhodep/assets/scripts/` | F |
| `extra-tools/nethunter-pro-app` | `security/nethunter-pro/` | E |
| `extra-tools/pwnagotchi` | `security/pwnagotchi/` | E |
| `extra-tools/{terminal-keyboard,terminal-clipboard,cleanup,claude-free,modem-at}` | `security/*` | E/A |
| debos flow + `wip.toml` | `os-distros/kali/` + `KaliBackend` (device.toml generated) | E/F |

Non-redistributable blobs (proprietary firmware: WCN3990 WiFi/BT, Adreno zap,
modem/adsp/cdsp `.mbn`, AW88261 ACF, NFC S3NRN4V) are **not** copied; they must
be extracted from the device as declared in the device definition
(`firmware.extract_from_device`).

Hardware support statuses in `devices/motorola/rhodep/device.yaml` are
evidence-based, drawn from that repository's `docs/` WIP notes and README
"What works" as of 2026-09.

## Kernel provider model

The reference port's Kali is built **on top of** the porter's own postmarketOS
port (both currently private, to be public):

- `github.com/d4rks1d33/postmarketos-motorola-rhodep` — the pmOS device-support
  base (shared aport: APKBUILD + 108 patches + DTB + base config + deviceinfo).
- `github.com/d4rks1d33/kali-nethunter-rhodep` — Kali on top (same kernel, Kali
  config flavor + Debian userland).

rhodep is **not yet in official pmaports** (open MR `postmarketOS/pmaports!9234`,
fork `d4rks1d33/pmaports` branch `motorola-rhodep`), so the device's
`kernel.provider` points at the porter's fork with `upstreamed: false`. This is
modeled as a first-class case so any porter can build against their own
not-yet-upstream device support. See
[docs/kernel-flavors-and-providers.md](docs/kernel-flavors-and-providers.md).
