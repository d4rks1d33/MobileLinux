# MobileLinux

**Automated device-porting platform for mobile Linux.**

MobileLinux turns the hard, one-off work of porting a Linux distribution to a
phone into a declarative, repeatable pipeline. The hardware support for a
device lives in a single **device definition**; distributions, desktops and
security tools are separate, swappable layers on top.

```
                  DEVICE
                     │
             device definition   (devices/<vendor>/<codename>/device.yaml)
                     │
             mainline kernel
                     │
              hardware layer      (device packages, firmware, boot, install strategy)
                     │
        ┌────────────┼────────────┐
        │            │            │
      Kali         Debian        Arch        (os-distros/ — Kali + postmarketOS today)
        │            │            │
      Phosh        Plasma       Lomiri       (desktops/ — swappable, not coupled to HW)
```

The reference implementation is the **Motorola Moto G82 5G (`rhodep`,
Qualcomm SM6375)** running Kali + Phosh on a mainline 7.2-rc5 kernel, migrated
from the original `nethunter-rhodep-repo`.

## Why this exists

Porting a phone today means: fork a whole distro, edit dozens of scripts,
hardcode device paths, flash random partitions and hope it boots. MobileLinux
replaces that with:

```
Find existing Linux/pmOS device support
        ↓
Import / create a device definition
        ↓
Build → Test → Flash (device-specific strategy) → Boot → Hardware report
```

Key ideas:

- **Hardware support is independent of the rootfs.** The same device works with
  Kali, Debian, Arch, … without duplicating the port.
- **The kernel is a shared base with per-distro config flavors.** postmarketOS
  provides the device-support base (source + patches + DTB + base config); each
  distro is just a config **fragment** on top (Kali = base + NetHunter delta).
  The same 108 patches and DTB serve every distro. A device need not be in
  official pmaports — the provider can point at the porter's own fork/repo.
  See [docs/kernel-flavors-and-providers.md](docs/kernel-flavors-and-providers.md).
- **Installation is a first-class, per-device concern.** Not every phone
  supports `fastboot flash userdata`; not every phone has fastbootd; partition
  names and A/B schemes differ. Each device declares its **install strategy**.
- **Nothing is claimed as "supported" without evidence.** Hardware status is
  evidence-based and drives an objective support percentage.

## ⚠️ Disclaimer

**Flashing phones is risky. You use MobileLinux entirely at your own risk.** The
authors accept **no liability for bricked devices or lost data**. By design this
tool never touches the bootloader itself and only writes the partitions a device
declares (with confirmation, `--dry-run`, and hash checks), so recovery is
*normally* possible via the device's rescue/fastboot flow — but **no outcome is
guaranteed**. **Back up first.** Read **[DISCLAIMER.md](DISCLAIMER.md)** and
[docs/recovery.md](docs/recovery.md) before flashing.

## Quick start

```bash
# from the repo root
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

mobilelinux list-devices
mobilelinux device-info rhodep
mobilelinux check rhodep
mobilelinux kernel rhodep --flavor kali      # build just the kernel (swap active aport)
mobilelinux build rhodep --distro kali       # dry-run when build tools are absent
mobilelinux flash rhodep --dry-run           # never destructive without confirmation
```

The framework **detects external tools** (fastboot, adb, debos, pmbootstrap,
rauc, …). When a tool is missing it prints the exact command it would have run
and tells you how to install what's missing, so you can install it and re-run.

## Repository layout

| Path | Purpose |
|------|---------|
| `schema/device.schema.json` | The device-definition JSON Schema (single source of truth). |
| `src/mobilelinux/core/` | Schema, models, registry, tool detection, build orchestration. |
| `src/mobilelinux/installer/` | Install/flash strategy abstraction (fastboot, rescue-dd, heimdall, …). |
| `src/mobilelinux/distros/` | Distribution backends (Kali, postmarketOS). |
| `src/mobilelinux/desktops/` | Desktop layers (Phosh/Plasma/Lomiri). |
| `src/mobilelinux/security/` | Optional security-tool layers (NetHunter Pro, pwnagotchi). |
| `src/mobilelinux/testing/` | Compatibility checker + modular hardware test suite. |
| `src/mobilelinux/ota/` | Release/OTA/security-update machinery. |
| `src/mobilelinux/importers/` | pmaports → device-definition importer. |
| `devices/<vendor>/<codename>/` | Device definitions + assets. |
| `os-distros/` `desktops/` `security/` | Layer content (recipes, package lists, config). |
| `docs/` | User, porter and developer documentation. |

## Documentation

**Start at the [documentation index](docs/README.md).** The two end-to-end guides:

- **[Build & flash, end to end](docs/build-and-flash.md)** — build an image, see
  exactly where every artifact lands, and flash with the right strategy.
- **[Porting a new device, end to end](docs/porting-guide.md)** — for developers
  adding a phone.

By audience:

- User: [install](docs/install.md), [build+flash](docs/build-and-flash.md), [build ref](docs/build.md), [flash ref](docs/flash.md), [recover](docs/recovery.md), [troubleshooting](docs/troubleshooting.md)
- Porter: [porting guide](docs/porting-guide.md), [porting ref](docs/porting.md), [device schema](docs/device-schema.md), [import from pmOS](docs/import-pmos.md)
- Developer: [architecture](docs/architecture.md), [kernel flavors & providers](docs/kernel-flavors-and-providers.md), [OS distros](docs/os-distros.md), [testing](docs/testing.md)
- OTA/security: [ota](docs/ota.md), [security updates](docs/security-updates.md), [release process](docs/release-process.md), [signing](docs/signing.md), [CVE management](docs/cve-management.md), [SBOM](docs/sbom.md), [recovery](docs/recovery.md)
- Research background: [pmOS device model + 5 devices](docs/research-pmos-devices.md), [OTA architecture decision](docs/research-ota-architecture.md)

## Status

Early. `list-devices`, `device-info`, `check`, `validate` are functional today.
Build/flash/test/release orchestrate real tools with dry-run fallback. See the
[roadmap in docs/architecture.md](docs/architecture.md).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
