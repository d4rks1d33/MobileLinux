# OTA Updates

This document explains **OTA (Over-The-Air) updates** from scratch and then
documents exactly how MobileLinux implements them. If you have never built an OS
update system before, start at the top; if you just want the commands, jump to
[The CLI](#the-cli).

Related docs:
[release-process.md](release-process.md) ·
[signing.md](signing.md) ·
[security-updates.md](security-updates.md) ·
[recovery.md](recovery.md) ·
[sbom.md](sbom.md) ·
[cve-management.md](cve-management.md) ·
[research-ota-architecture.md](research-ota-architecture.md)

---

## 1. What is an OTA?

"OTA" means delivering a software update to a device **over the network**,
without physically connecting it to a computer. Your phone does this every month:
it downloads a package, verifies it, installs it, reboots, and comes back with a
newer OS.

The core difference between "an OTA system" and "just copying files" is that an
OTA system must be **safe on a device you cannot physically reach**. If an update
bricks the phone, nobody can walk over and re-plug a USB cable. So an OTA system
is built around three guarantees:

1. **Authenticity** — the update really came from us (not an attacker). We prove
   this with a cryptographic **signature**.
2. **Integrity** — the bytes arrived intact (not corrupted mid-download). We
   prove this with a **hash** (SHA-256).
3. **Recoverability** — if the new OS does not boot, the device can go **back**
   to the version that worked. This is **rollback**.

MobileLinux's OTA server is nothing more than a **static file host** — GitHub
Releases, a plain HTTP server, or an object store. The phone *pulls* from it.
The server never connects *into* the phone (no SSH, no agent listening on the
device). This is a deliberate security property: there is no inbound attack
surface on the phone, and hosting costs nothing.

---

## 2. What actually gets updated (and why we separate it)

A phone is not one blob. It is several independent layers, and a good OTA system
updates each one **on its own schedule**. Bundling everything into a single
monolithic image means a one-line fix to a debugging tool forces you to re-ship
and re-flash the entire operating system — slow, risky, and wasteful.

MobileLinux therefore separates the update layers explicitly (this mirrors the
`artifacts` types in [manifest.schema.json](../schema/manifest.schema.json),
line 47):

| Layer | What it is | How it updates | Atomic? | Rollback? |
|---|---|---|---|---|
| **OS / rootfs** | The Debian/Kali root filesystem | RAUC A/B image | Yes | Yes (A/B) |
| **Kernel** | The mainline Linux kernel (inside the Android boot image) | Shipped with the rootfs in the same A/B update | Yes | Yes (same slot flip) |
| **initramfs** | The tiny early-boot filesystem that mounts the real root | Part of the boot image, same A/B update | Yes | Yes |
| **boot image** | The packed Android boot image (kernel + initramfs + dtb + cmdline) | Written to the inactive `boot_a`/`boot_b` | Yes | Yes |
| **device-packages** | The `rhodep-*` hardware-enablement packages | apt (in place) or folded into the next image | No (apt) | No (apt) |
| **firmware** | Proprietary Wi-Fi/BT/GPU/modem blobs | Its own artifact; updated deliberately and rarely | Yes (slot) | HW-dependent |
| **security** | CVE/security fixes for userspace packages | apt + unattended-upgrades in place | No | No |
| **apps / tools** | Optional bundles (e.g. the Kali toolset) | `systemd-sysext` overlay images | Yes (overlay) | Yes (unmerge) |

**Why a tool update should not replace the whole system:** if you install a new
version of a scanning tool, you want to swap *that package*, not re-flash the
kernel and root filesystem. Re-flashing the base OS to change a tool is:

- slow (hundreds of MB instead of a few),
- risky (a reboot and an A/B slot flip for a trivial change),
- and it throws away the whole point of layering.

So MobileLinux uses **apt** for packages/tools/security in place, and reserves
the heavy, atomic **A/B image** mechanism for the parts that are dangerous to get
wrong (the OS and kernel).

---

## 3. The core concepts

### A/B (dual-slot) updates

An **A/B device** has *two* copies of the bootable system: slot **A** and slot
**B**. At any moment one is **active** (running) and the other is **inactive**.

An A/B update works like this:

1. You are running slot A.
2. The updater writes the new OS entirely into slot B **while A keeps running**.
3. Only when B is fully written and verified does the updater tell the bootloader
   "boot B next time."
4. You reboot into B. If B works, great. If B fails, the bootloader falls back to
   A — which is still intact and untouched.

The reference device, Motorola *rhodep*, is A/B at the **Android bootloader
level**: the Qualcomm bootloader picks `boot_a` or `boot_b` and injects
`androidboot.slot_suffix=_a`/`_b` into the kernel command line. See
[research-ota-architecture.md](research-ota-architecture.md) §1 and §6 for the
full mechanism.

### Atomic updates

"**Atomic**" means the update either **fully happens or does not happen at all** —
there is never a half-updated system that boots. With A/B this is natural: the
running slot is never modified, and the switch to the new slot is a single
pointer flip. If power is lost while writing slot B, slot A is untouched and the
device still boots normally; the interrupted B write is simply discarded and
retried.

Contrast this with `apt upgrade`, which mutates the **running** filesystem
in place. If power is lost halfway through `dpkg`, you can end up with a
half-configured, unbootable system. That is why apt is fine for packages between
image releases but **not** for the base OS.

### Rollback

**Rollback** is returning to the previous known-good version. On an A/B device
this is trivial: point the bootloader back at the old slot and reboot. The old
slot was never touched, so it is guaranteed to be exactly what worked before.

### Health check → mark good / rollback

An update is not "confirmed" just because it was installed. After rebooting into
the new slot, the device runs a **health check** (see
[recovery.md](recovery.md#the-health-check)). Only if the system reaches a
healthy state does it **mark the slot good**. If the health check fails — or the
device never gets far enough to run it — the bootloader's fallback logic returns
to the previous slot. This is what turns "we wrote new bytes" into "we safely
committed an update."

---

## 4. Our chosen stack

The full technology evaluation is in
[research-ota-architecture.md](research-ota-architecture.md). The conclusion,
for the MVP:

- **RAUC** — the base-image updater. It writes signed A/B images (rootfs +
  kernel/boot) atomically to the inactive slot and drives rollback. On rhodep it
  uses RAUC's **`custom` bootloader backend** wired to the **Android slot
  metadata** (`qbootctl`/`bootctl` against the `misc` partition), so RAUC can
  read and set the active `_a`/`_b` slot. This is recorded in the device's
  `ota.bootloader_integration` block (`type: android-bootctl`,
  `backend: rauc-custom`).
- **apt + unattended-upgrades** — in-place package and **security** updates
  between image releases, pulled from the signed Debian/Kali archive. See
  [security-updates.md](security-updates.md).
- **systemd-sysext** — optional, overlay-based **tool layers** (e.g. a Kali
  toolset) that mount over `/usr` without rebuilding the rootfs.

A future v2 keeps **OSTree** on the roadmap for **single-rootfs** devices, where
its atomic-rollback-without-A/B-partitions model shines
([research-ota-architecture.md](research-ota-architecture.md) §3).

---

## 5. The device.yaml `ota:` block

Every device declares its OTA capabilities in its `device.yaml`. Here is the
rhodep block from
[devices/motorola/rhodep/device.yaml](../devices/motorola/rhodep/device.yaml)
(lines 316–329):

```yaml
ota:
  strategy: ab                    # 'ab' (dual-slot) or 'single-rootfs'
  rollback: true
  slots: [a, b]
  bootloader_integration:
    type: android-bootctl         # slot selection via Android misc (qbootctl)
    backend: rauc-custom          # RAUC's 'custom' backend drives the slots
  updatable:
    rootfs: true
    kernel: true
    initramfs: true
    device_packages: true
    firmware: false
    bootloader: false             # never auto-update the Motorola bootloader
```

Field by field:

- **`strategy`** — `ab` means the device has two slots and gets atomic updates
  with rollback. `single-rootfs` means it has only one rootfs (see
  [§9](#9-single-rootfs-devices-degrade-gracefully)).
- **`rollback`** — whether automatic rollback is available (true only on A/B).
- **`slots`** — the slot names.
- **`bootloader_integration.type`** — how slot selection is performed. On rhodep
  it is `android-bootctl`: the Android `misc` partition's A/B metadata, driven by
  `qbootctl`.
- **`bootloader_integration.backend`** — `rauc-custom`: RAUC's `custom` bootloader
  backend calls a handler script that implements the Android slot operations. This
  is the key integration described in
  [research-ota-architecture.md](research-ota-architecture.md) §1.
- **`updatable`** — which layers OTA is allowed to touch. Note `firmware: false`
  and `bootloader: false` — the Motorola bootloader is **never** auto-updated,
  because a bad bootloader write is unrecoverable.

---

## 6. The manifest format

The **manifest** is a small signed JSON file that fully describes one release for
one device+channel. The device downloads the manifest *first*, verifies it, and
only then decides whether (and what) to download. The schema is
[schema/manifest.schema.json](../schema/manifest.schema.json); the on-device
model is [ota/manifest.py](../src/mobilelinux/ota/manifest.py).

Here is a walk through the fields (see the schema for the authoritative
definition):

```json
{
  "manifest_version": 1,

  "release": {
    "version": "1.0.1",          // semantic-ish version
    "channel": "stable",         // stable | beta | nightly
    "date": "2026-09-02T...",    // ISO-8601 build date
    "distro": "kali",
    "desktop": "phosh",
    "notes": "..."
  },

  "device": {
    "id": "rhodep",              // MUST match this device or install is refused
    "codename": "rhodep",
    "architecture": "aarch64",   // MUST match the device arch
    "minimum_version": "0.0.0",  // client must be >= this to accept the update
    "ota_strategy": "ab"         // ab | single-rootfs
  },

  "artifacts": {                 // one entry per updatable component
    "rootfs": {
      "url": "rootfs.img",       // relative (rewritten to an asset URL) or absolute
      "sha256": "…",             // integrity check
      "size": 123456789,
      "type": "rootfs"           // rootfs|kernel|initramfs|boot|device-packages|firmware|rauc-bundle
    }
  },

  "security": {
    "security_patch_level": "2026-09-01",  // a DATE (see below)
    "kernel_version": "7.2-rc5",
    "sbom_url": "sbom.cyclonedx.json",     // links to the SBOM (see sbom.md)
    "fixed_cves": ["CVE-2026-1234"]        // CVEs fixed vs the previous release
  },

  "signature": {                 // NOT covered by the signature itself
    "algorithm": "ed25519",
    "key_id": "stable",          // which key signed it (for rotation)
    "value": "base64…"           // signature over the canonical body
  }
}
```

Key design points:

- **Each artifact is hashed independently.** This lets a client update only what
  changed (e.g. a new kernel but the same rootfs). See
  [manifest.schema.json](../schema/manifest.schema.json) line 38.
- **The `signature` field is excluded from what is signed.** The signature covers
  the *canonical body* — the manifest minus its own `signature`. See
  [signing.md](signing.md) and `canonical_body()` in
  [ota/manifest.py](../src/mobilelinux/ota/manifest.py) line 15.
- **`security_patch_level` is a date**, not an Android-style level. See
  [security-updates.md](security-updates.md#security-patch-level-is-a-date).

---

## 7. The full flow (build → install → confirm)

End to end, a release travels like this. The build/sign/publish half is on the
CI/maintainer side ([release-process.md](release-process.md)); the download/
install half is on the device ([ota/client.py](../src/mobilelinux/ota/client.py)).

```
   MAINTAINER / CI                          DEVICE
   ──────────────                           ──────
1. build artifacts (rootfs, boot image)
2. generate SBOM                            (see sbom.md)
3. compute security patch level
4. assemble manifest (device id/arch/
   min-version + per-artifact hashes)
5. sign manifest with the channel key       (see signing.md)
6. write release/ tree
7. publish: upload release/ as assets
   (GitHub Releases / static HTTP)
                                        8.  `mobilelinux update`
                                        9.  fetch manifest for the channel
                                        10. verify signature (release .pub)
                                        11. check device id / arch / min-version
                                        12. compare versions; newer? proceed
                                        13. download artifacts
                                        14. verify each sha256 hash
                                        15. install atomically to inactive slot
                                            (RAUC on A/B devices)
                                        16. reboot into the new slot
                                        17. health check
                                        18. pass -> mark slot GOOD
                                            fail -> bootloader ROLLS BACK to old slot
```

Steps 9–17 are exactly the `check → download → install → (reboot) → health
check` sequence documented at the top of
[ota/client.py](../src/mobilelinux/ota/client.py).

---

## 8. The CLI

The OTA client runs **on the device** and is driven by one command
(defined in [cli/main.py](../src/mobilelinux/cli/main.py) lines 81–87, dispatched
to [ota/client.py](../src/mobilelinux/ota/client.py)):

```
mobilelinux update [--check | --download | --install | --rollback | --status] [--channel <name>]
```

- **`mobilelinux update`** (no flags) — the full pipeline: check → download →
  install. In `client.py` this is because when no scoping flag is given, all
  three phases default to on (lines 44–46).
- **`--check`** — only fetch and verify the manifest and report whether an update
  is available; do **not** download or install (`client.py` line 47–48).
- **`--download`** — fetch and hash-verify the artifacts, but do not install.
- **`--install`** — install what was downloaded (implies download).
- **`--rollback`** — switch back to the previous good slot (A/B only). See
  `_rollback()` in [ota/client.py](../src/mobilelinux/ota/client.py) line 209; it
  refuses on `single-rootfs` devices.
- **`--status`** — print the current device id, version, channel, patch level,
  OTA strategy and last result (`_status()`, line 198). No network access.
- **`--channel <name>`** — override the channel for this invocation (see below).

Two related commands complete the picture:

- **`mobilelinux release <device> --version X --channel Y`** — the *maintainer*
  side that produces the signed release. See
  [release-process.md](release-process.md).
- **`mobilelinux security-status`** — shows version, patch level and affected
  CVEs. See [security-updates.md](security-updates.md).

The device's OTA state (version, channel, `metadata_url`, `public_key`,
`ota_strategy`) lives in `/etc/mobilelinux/state.json`, modeled by
[ota/state.py](../src/mobilelinux/ota/state.py).

---

## 9. Channels

A **channel** is a release track. MobileLinux has three (enforced by the schema
enum, [manifest.schema.json](../schema/manifest.schema.json) line 17):

- **`stable`** — production releases; the safest, least frequent.
- **`beta`** — pre-release testing.
- **`nightly`** — bleeding-edge, may break.

Each channel is just a separate path on the static host. The client builds the
manifest URL from the configured base plus the channel:
`"{metadata_url}/{channel}/manifest.json"` (see `_fetch_manifest()` in
[ota/client.py](../src/mobilelinux/ota/client.py) line 80). Switching channels is
therefore just changing which subtree the device polls — `--channel beta` makes
the device look at the beta manifest. Channels also map to separate **signing
keys** (`stable`, `beta`, `dev`); see [signing.md](signing.md).

---

## 10. How the client refuses a bad or wrong image

The most important safety property is that a device will **never** install
something it should not. The client enforces this in two functions in
[ota/client.py](../src/mobilelinux/ota/client.py):

### `_acceptable()` — is this update *for us*? (lines 111–126)

- **Wrong device** — if `manifest.device_id` is not exactly this device's id, the
  update is refused. This makes it impossible to flash another phone's image by
  accident.
- **Wrong architecture** — if the manifest's architecture does not match the local
  machine's (`aarch64`/`armv7`/`x86_64`), it is refused.
- **Version too old** — if the manifest declares a `minimum_version` and the
  device is below it, the update is refused with "install intermediate updates
  first." This blocks unsafe version skips.

### `_verify_signature()` — is this update *authentic*? (lines 94–108)

- **Unsigned** — a manifest with no `signature` is refused outright.
- **Missing key** — if the device has no public key at the configured path, it is
  refused.
- **Bad signature** — the signature is verified over the canonical body with the
  release public key. Any mismatch prints `SIGNATURE INVALID; refusing update
  (possible tampering)` and stops.

### Hash verification — did the bytes arrive intact? (`_download()`, lines 135–155)

After each artifact is downloaded, its SHA-256 is recomputed and compared to the
manifest's `sha256`. A mismatch prints `HASH MISMATCH … refusing (corrupt or
wrong artifact)` and aborts. Because the manifest itself is signed, a valid hash
in a valid manifest means the artifact is exactly what the maintainer built.

The net effect: the device installs an update **only** if it targets this exact
device and architecture, satisfies the minimum-version rule, is signed by the
trusted release key, and every artifact's hash matches.

---

## 11. Single-rootfs devices degrade gracefully

Not every device is A/B. A **single-rootfs** device has only one root
filesystem, so there is no inactive slot to write into and no known-good slot to
fall back to. MobileLinux still supports these devices, but with reduced safety:

- The manifest and signature/hash verification work identically.
- The `_install()` path (see [ota/client.py](../src/mobilelinux/ota/client.py)
  lines 180–185) prints a clear warning: *"single-rootfs device: update is NOT
  atomic and cannot roll back."*
- Because the write is destructive and in place, it **refuses to proceed without
  `--yes`** (or a dry run). This forces an explicit acknowledgement.
- `mobilelinux update --rollback` is **disabled** on single-rootfs devices
  (`_rollback()` returns an error, lines 210–212).

If a single-rootfs update fails, recovery is manual — you must re-flash via the
device's rescue path. See [recovery.md](recovery.md#single-rootfs-no-automatic-rollback).
This is exactly the case OSTree is intended to solve in a future v2
([research-ota-architecture.md](research-ota-architecture.md) §3).

---

## 12. The OTA server is just static files

To restate the hosting model, because it drives many of the design choices:

- The "server" can be **GitHub Releases**, a **static HTTP server**, or an
  **object store**. It only serves files.
- The device **pulls**: it fetches `manifest.json`, then the artifacts, over
  plain HTTP(S). See `urllib.request` usage in
  [ota/client.py](../src/mobilelinux/ota/client.py).
- There is **no SSH into the phone** and **no agent on the server** talking to the
  phone. All trust comes from the **signature**, not from the transport.

This is why signing is non-negotiable: since anyone could host a file that *looks*
like a manifest, the only thing that makes an update trustworthy is that it is
signed by a key the device already trusts. Continue to [signing.md](signing.md).
