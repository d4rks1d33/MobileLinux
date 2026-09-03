# MobileLinux

**Automated device-porting platform for mobile Linux.**

MobileLinux is an open platform for bringing mainline Linux to mobile hardware.
It separates device support from distributions and desktop environments,
enabling the same device platform to run Kali, Debian, Ubuntu, Arch, and other
Linux distributions with reproducible builds, hardware testing, and OTA updates.

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

## Required tools

MobileLinux itself only needs Python 3.10+ and PyYAML. The **build and flash
tools are external** and only needed for the operations that use them. The CLI
detects them and tells you exactly what to install; this table is the summary
(Debian/Kali host, which is also a native `arm64` build host for phones):

| Tool | Needed for | Install (Debian/Kali) |
|------|------------|-----------------------|
| `pmbootstrap` | build the mainline kernel (apk) | `pipx install pmbootstrap` |
| `go` + `debos` | build the Debian/Kali rootfs | `apt install golang-go` then `go install github.com/go-debos/debos/cmd/debos@latest` (needs Go ≥1.24; it auto-fetches a newer toolchain) |
| `debootstrap`, `systemd-container` | rootfs bootstrap + nspawn (used by debos) | `apt install debootstrap systemd-container` |
| `mkbootimg` | build Android boot images | `apt install android-sdk-libsparse-utils mkbootimg` (or `android-tools`) |
| `sgdisk` (gdisk) | GPT partitioning (gpt-in-partition) | `apt install gdisk` |
| `mkfs.ext4`, `resize2fs` | build/grow the rootfs image | `apt install e2fsprogs` |
| `dosfstools` (`mkfs.vfat`) | the boot vfat partition | `apt install dosfstools` |
| `fakeroot`, `dpkg-deb` | package the kernel `.deb` | `apt install fakeroot dpkg` |
| `fastboot`, `adb` | detect + flash the device | `apt install android-sdk-platform-tools` |
| `rauc` (optional) | atomic A/B OTA bundles | `apt install rauc` |
| `openssl` / `python3-cryptography` | sign/verify OTA releases | `apt install openssl python3-cryptography` |
| `syft` (optional) | SBOM per release | see https://github.com/anchore/syft |
| `debsecan` (optional) | map installed packages → CVEs | `apt install debsecan` |

One-liner for a full Debian/Kali build host:

```bash
sudo apt install -y golang-go debootstrap systemd-container \
     android-sdk-libsparse-utils mkbootimg gdisk e2fsprogs dosfstools \
     fakeroot dpkg android-sdk-platform-tools openssl python3-cryptography \
     libglib2.0-dev libostree-dev jq
go install github.com/go-debos/debos/cmd/debos@latest   # ~/go/bin/debos
pipx install pmbootstrap
```

**Execution model:** builds are **plan-only by default** (they print what they
would run). Add `--execute` to actually run steps whose tools are present, and
`--allow-dangerous` for operations that touch real block/loop devices
(pmbootstrap, debos, losetup, mkfs). `--dry-run` prints and runs nothing.

**Non-redistributable firmware:** for some devices (e.g. `rhodep`) certain
vendor blobs cannot be shipped. After a build, the device's `out/<device>/
FIRMWARE.md` lists exactly which blobs to extract from your phone and where to
put them. See [docs/build-and-flash.md](docs/build-and-flash.md).

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
