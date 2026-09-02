# MobileLinux Documentation Index

Everything, organized by what you want to do. Start here.

![Kali + Phosh running on the Motorola Moto G82 5G (rhodep)](images/phosh-desktop.png)

> Proof of work: the reference device — a **Motorola Moto G82 5G (`rhodep`)** —
> running **Kali Linux + Phosh** on a mainline kernel. More screenshots in
> [build-and-flash.md](build-and-flash.md#proof-it-works).

---

## Start here

| I want to… | Read |
|------------|------|
| Understand the whole project in one page | [architecture.md](architecture.md) |
| **Build an image and flash it, end to end** | **[build-and-flash.md](build-and-flash.md)** |
| **Port a new device, end to end** | **[porting-guide.md](porting-guide.md)** |
| See what a device supports | [`mobilelinux check <device>`](testing.md) |
| Understand kernel flavors & providers | [kernel-flavors-and-providers.md](kernel-flavors-and-providers.md) |
| Understand distros (Kali, postmarketOS) | [os-distros.md](os-distros.md) |

---

## For users

Install a supported device and keep it running.

- [install.md](install.md) — install a supported device (the short path)
- [build-and-flash.md](build-and-flash.md) — **the full end-to-end build + flash guide**, including exactly **where every artifact lands** and which flashing step uses it
- [build.md](build.md) — build reference (pipeline stages, flags)
- [flash.md](flash.md) — flash reference (the 10 safety rules, per-strategy)
- [recovery.md](recovery.md) — recover a bricked/boot-looping device
- [troubleshooting.md](troubleshooting.md) — common problems and fixes

## For device porters

Add a new phone. The goal: adding a device is mostly adding one `device.yaml`.

- [porting-guide.md](porting-guide.md) — **the full end-to-end porting walkthrough**
- [porting.md](porting.md) — porting reference (field-by-field)
- [device-schema.md](device-schema.md) — the device definition schema reference
- [import-pmos.md](import-pmos.md) — import a device from postmarketOS pmaports
- [kernel-flavors-and-providers.md](kernel-flavors-and-providers.md) — the shared-kernel + per-distro-flavor model, the `kernel-config` module editor, and using your own (not-yet-upstream) pmOS repo as the provider

## For developers

Work on the framework itself.

- [architecture.md](architecture.md) — layers, components, execution/safety model, roadmap
- [os-distros.md](os-distros.md) — distribution backends (Kali/debos, postmarketOS/pmbootstrap) and how to add one
- [kernel-flavors-and-providers.md](kernel-flavors-and-providers.md) — kernel build model
- [testing.md](testing.md) — compatibility checker + modular hardware test suite

## OTA, releases & security

- [ota.md](ota.md) — what OTA is, A/B vs single-rootfs, atomic updates, rollback, the manifest, the update client
- [release-process.md](release-process.md) — produce a signed release + OTA metadata
- [signing.md](signing.md) — key generation, storage, rotation, revocation, recovery
- [security-updates.md](security-updates.md) — leveraging Debian/Kali security updates
- [cve-management.md](cve-management.md) — CVE tracking and the future rebuild pipeline
- [sbom.md](sbom.md) — Software Bill of Materials per release

## Research background

Why the key decisions were made (evidence, not assertion).

- [research-pmos-devices.md](research-pmos-devices.md) — the postmarketOS device model + the 5 selected devices and their flashing strategies
- [research-ota-architecture.md](research-ota-architecture.md) — the OTA technology comparison (RAUC vs OSTree vs Mender vs …) and the chosen architecture

---

## Command quick reference

```bash
# discover
mobilelinux list-devices                     # all devices + support % + strategy
mobilelinux device-info <device>             # full details
mobilelinux check <device>                   # objective hardware-support report
mobilelinux detect                           # identify a connected phone (fastboot/adb)

# kernel (shared base + per-distro flavor)
mobilelinux kernel-config <device> --flavor kali        # edit modules interactively
mobilelinux kernel <device> --flavor kali               # build the kernel (swap active aport)

# build (dry-run/plan by default; --execute to run; --allow-dangerous for block/loop ops)
mobilelinux build <device> --distro postmarketos        # Alpine + Phosh (apk)
mobilelinux build <device> --distro kali --profile security   # Kali + Phosh + tools (debos)

# flash (auto-selects the device's strategy; conservative + confirmations)
mobilelinux flash <device> --dry-run         # preview
mobilelinux flash <device>                   # do it

# verify
mobilelinux test <device>                    # on-device hardware tests

# release + OTA
mobilelinux keygen --channel stable
mobilelinux release <device> --version 1.0.0 --channel stable
mobilelinux update                           # on-device: check/download/verify/install
mobilelinux security-status                  # version + patch level + CVEs

# import / contribute
mobilelinux import <pmaports-device-dir>     # draft a device.yaml from pmOS
```

See the full flag reference in [build-and-flash.md](build-and-flash.md) and
[porting-guide.md](porting-guide.md).
