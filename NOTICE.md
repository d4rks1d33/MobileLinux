# Provenance

This repository was bootstrapped from **`nethunter-rhodep-repo`**, a working
Kali NetHunter Pro port for the Motorola Moto G82 5G (`rhodep`, Qualcomm
SM6375) on a mainline Linux kernel. That repository is used **read-only** as a
reference and as the source of migrated assets; it is never modified by this
project.

Migrated assets and where they now live:

| From `nethunter-rhodep-repo` | To `mobilelinux` | Audit class |
|------------------------------|------------------|-------------|
| `kernel/config`, `kernel/patches`, `kernel/diag-modules` | `devices/motorola/rhodep/assets/kernel/` | B/D |
| `packages/rhodep-*`, `packages/firmware-motorola-rhodep` | `devices/motorola/rhodep/assets/packages/` | B/C/D |
| `userspace/*` (audio, modem, sensors, power, bluetooth, nfc, usb-net, ...) | `devices/motorola/rhodep/assets/userspace/` | B/C/D |
| `scripts/mkbootv2b.py`, `build-rescue-boot.sh`, `make-boot-from-apk.sh`, ... | `devices/motorola/rhodep/assets/scripts/` | F |
| `extra-tools/nethunter-pro-app` | `security/nethunter-pro/` | E |
| `extra-tools/pwnagotchi` | `security/pwnagotchi/` | E |
| `extra-tools/{terminal-keyboard,terminal-clipboard,cleanup,claude-free,modem-at}` | `security/*` | E/A |
| debos flow + `wip.toml` | `distros/kali/` + `KaliBackend` (device.toml generated) | E/F |

Non-redistributable blobs (proprietary firmware: WCN3990 WiFi/BT, Adreno zap,
modem/adsp/cdsp `.mbn`, AW88261 ACF, NFC S3NRN4V) are **not** copied; they must
be extracted from the device as declared in the device definition
(`firmware.extract_from_device`).

Hardware support statuses in `devices/motorola/rhodep/device.yaml` are
evidence-based, drawn from that repository's `docs/` WIP notes and README
"What works" as of 2026-09.
