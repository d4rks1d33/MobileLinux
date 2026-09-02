# Architecture

MobileLinux separates the parts of a phone port that are usually tangled
together, so hardware support can be reused across distributions and devices.

## Layers

```
                  DEVICE
                     │
             device definition        devices/<vendor>/<codename>/device.yaml
                     │                 (identity, SoC, kernel, DT, firmware,
                     │                  hardware status, boot, install, ota, tests)
             mainline kernel
                     │
              hardware layer           device_packages + firmware + userspace
                     │                 (the declarative form of rhodep-*.deb etc)
        ┌────────────┼────────────┐
        │            │            │
      Kali         Debian        Arch    distros/<id>/  (only Kali today)
        │            │            │
      Phosh        Plasma       Lomiri   desktops/<id>/ (swappable, HW-independent)
        │
   security tools                        security/<tool>/ (optional profile layer)
```

The golden rule: **selecting a different distro, desktop or security profile
never touches device support, and porting a new device never touches the
distros.**

## Component map

| Concern | Where | Notes |
|---------|-------|-------|
| Device schema (source of truth) | `schema/device.schema.json` | JSON Schema; validated in CI. |
| Device definitions | `devices/<vendor>/<codename>/device.yaml` + `assets/` | One file per device + its assets. |
| Core (models, registry, tools, images) | `src/mobilelinux/core/` | Repo discovery, loader, tool detection, image assembly, build orchestration. |
| Install/flash strategies | `src/mobilelinux/installer/` | `Strategy` base + `rescue-dd`, `fastboot`, `fastbootd`, `heimdall`, `sdcard`, `uuu`, `adb-shell-dd`. |
| Distro backends | `src/mobilelinux/distros/` + `os-distros/` | `KaliBackend` (debos) + `PostmarketosBackend` (pmbootstrap). |
| Desktops | `desktops/` | Manifests only; content is applied as a layer. |
| Security tools | `security/` | Optional, distro-level, not device-coupled. |
| Testing | `src/mobilelinux/testing/` | Compatibility checker + modular hardware tests. |
| OTA / releases | `src/mobilelinux/ota/` | Manifest, signing, release pipeline, update client. |
| Security status / CVE | `src/mobilelinux/security/` | `security-status`, debsecan mapping. |
| pmaports importer | `src/mobilelinux/importers/` | `deviceinfo` → draft `device.yaml`. |
| CLI | `src/mobilelinux/cli/` | Thin dispatch; no device logic. |

## Data flow

```
device.yaml ──parse+validate──▶ Device model
     │                              │
     │                     ┌────────┼───────────┬─────────────┐
     ▼                     ▼        ▼           ▼             ▼
  check (support %)     build     flash        test        release
                          │        │            │             │
                     distro+images strategy  test modules  manifest+sign
                          │        │                          │
                       artifacts  device                    OTA client
```

## Execution safety model

External build/flash tools are heavy and often absent. The `Runner`:

- **dry-run** (`--dry-run`): prints every command, runs nothing;
- **plan (default)**: prints commands; runs nothing unless `--execute`;
- **execute** (`--execute`): runs commands whose tool is present;
- **dangerous ops** (loop/mkfs/partitioning on real devices, pmbootstrap) also
  require `--allow-dangerous`.

Missing tools are always reported with install hints so the user can install
them and re-run. This is why merely having `losetup`/`pmbootstrap` installed
never causes an accidental destructive run.

## Why these choices

- **YAML + JSON Schema** for devices: readable, diffable, and validated
  automatically. It formalizes exactly the data pmOS leaves as wiki prose (the
  hardware matrix and install quirks).
- **Strategy pattern for install**: the reference device (`rhodep`) can't
  `fastboot flash userdata`, has no fastbootd, and uses a rescue-telnet + `dd`
  flow. That is one strategy among six; the difference is *data*, not forked
  scripts.
- **debos for Kali**: Kali is Debian-based, so the rootfs is built with debos
  (not apk/pacman). The backend reproduces the reference port's verified flow.
- **RAUC + apt + signed manifests** for OTA: atomic A/B rootfs/kernel where the
  hardware allows, in-place apt for security, signed metadata hostable on
  GitHub Releases. See [docs/ota.md](ota.md) and
  [docs/research-ota-architecture.md](research-ota-architecture.md).

## Roadmap

- [x] Device schema, registry, checker
- [x] Kali backend + build orchestration (dry-run + real)
- [x] Install/flash strategy abstraction (6 strategies)
- [x] Modular hardware test suite
- [x] 6 devices (rhodep + 5 pmOS), all distinct strategies
- [x] OTA MVP: signed manifest, update client, rollback, SBOM, security-status
- [x] pmaports importer, CI
- [ ] Real end-to-end build on a build host (debos/pmbootstrap present)
- [ ] RAUC custom Android-slot backend implementation on-device
- [ ] Additional distros (Debian, Arch), desktops (Plasma, Lomiri)
- [ ] CVE rebuild pipeline automation
