# Security Updates

This document explains how MobileLinux keeps a device **secure over time** — how
CVE fixes actually reach a phone. The short version: we **leverage upstream**
(Debian/Kali and the mainline kernel) instead of hand-patching vulnerabilities
ourselves.

Related docs:
[cve-management.md](cve-management.md) ·
[sbom.md](sbom.md) ·
[ota.md](ota.md) ·
[release-process.md](release-process.md) ·
[research-ota-architecture.md](research-ota-architecture.md) (Security / CVE
section)

Source: [security/status.py](../src/mobilelinux/security/status.py).

---

## 1. The realistic security-patching model: leverage upstream

A modern OS contains thousands of packages. New vulnerabilities (CVEs) are found
in them constantly. **You cannot hand-track and hand-patch hundreds of CVEs** —
that is a full-time job for a large team, and it is exactly the job the Debian and
Kali projects already do.

So MobileLinux does **not** maintain its own security patches for third-party
software. Instead it **rides the upstream security streams**:

- **Userspace packages** — security fixes flow from the **Debian/Kali archive**
  via **apt**. MobileLinux is a Debian/Kali system; when Debian/Kali fix a CVE and
  publish a new package, `apt` picks it up like on any other Debian machine.
- **The kernel** — MobileLinux ships a **mainline** kernel (see rhodep's
  `kernel.type: mainline` in
  [devices/motorola/rhodep/device.yaml](../devices/motorola/rhodep/device.yaml)
  line 20). That means we **ride upstream stable/CVE fixes** rather than
  maintaining a fork with hundreds of backported patches.

This is the central design decision from
[research-ota-architecture.md](research-ota-architecture.md) §8a: *"you leverage
the entire Debian/Kali security apparatus … instead of hand-patching CVEs."*

---

## 2. Debian stable vs Kali rolling

How security fixes are delivered depends on the distro base:

- **Debian (stable)** uses **DSAs** (Debian Security Advisories): targeted security
  fixes backported to the stable release, each mapping to CVEs and fixed package
  versions.
- **Kali is rolling** (it tracks Debian testing plus Kali-specific packages).
  Security fixes arrive by **rolling the whole archive forward**, not through a
  separate stable+DSA backport track. You stay current with
  `apt full-upgrade` against the signed `kali-rolling` repositories.

Either way, the *mechanism* on the device is the same: signed apt repositories,
verified by apt's own repository signing.

---

## 3. The two-track update model

MobileLinux deliberately splits security updates across two mechanisms (the full
rationale is [ota.md §2](ota.md#2-what-actually-gets-updated-and-why-we-separate-it)
and the table in [research-ota-architecture.md](research-ota-architecture.md)):

1. **In-place package/security updates — apt + `unattended-upgrades`.**
   Between image releases, the device pulls security fixes from the Debian/Kali
   archive in place. `unattended-upgrades` can install them automatically. This is
   fast and continuous but **not atomic and not rollback-safe** — a bad upgrade or
   power loss mid-`dpkg` can break the running system.

2. **Atomic base-image updates — signed OTA (RAUC A/B).**
   Periodically, the current package state is **re-baselined** into the next
   signed base image and shipped as an atomic A/B update via
   `mobilelinux update` ([ota.md](ota.md)). This keeps fresh flashes and A/B
   rollbacks current, and it means a broken in-place apt state is wiped by the
   next image update.

`security-status` reminds the operator of exactly this split
([security/status.py](../src/mobilelinux/security/status.py) lines 51–54):

> Userspace security fixes come from the Debian/Kali archive (apt).
> Atomic OS/kernel updates come via `mobilelinux update` (signed OTA).

---

## 4. The security pipeline

End to end, a security fix travels like this:

```
Debian/Kali security update            (upstream fixes a CVE, publishes a package)
        │
        ▼
package updates                        (new .deb available in the archive)
        │
   ┌────┴─────────────────────────┐
   ▼                              ▼
apt / unattended-upgrades      MobileLinux rebuild
(in place, immediately)        (fold packages into the next image)
                                   │
                                   ▼
                               device validation      (hardware test suite)
                                   │
                                   ▼
                               signed release          (mobilelinux release)
                                   │
                                   ▼
                               OTA                      (mobilelinux update)
```

The in-place branch gives users fixes quickly; the rebuild branch keeps the
atomic base image current so nothing regresses on a fresh flash or a rollback.

---

## 5. What is automatable vs manual

From [research-ota-architecture.md](research-ota-architecture.md) ("What can be
automated vs. manual"):

**Automatable** (CI + on-device cron):

- SBOM generation for every image ([sbom.md](sbom.md)).
- CVE mapping — `debsecan`, and/or `grype` on the SBOM, and/or diffing installed
  versions against the Debian Security Tracker JSON ([cve-management.md](cve-management.md)).
- Kernel CVE watch — ingesting `linux-cve-announce` and checking your kernel
  branch against supported stable/LTS.
- Applying fixes — `unattended-upgrades` on-device; rebuild+sign+publish base
  images in CI on a schedule or on high-severity CVEs.
- Release signing and publishing ([release-process.md](release-process.md)).

**Still manual / human judgment:**

- **Applicability triage** — deciding whether a flagged CVE actually affects this
  device's kernel config subset or reachable packages. Both kernel.org and
  `debsecan` explicitly push this decision onto you.
- **Risk acceptance / scheduling** — when to force a base-image roll vs. rely on
  in-place apt.
- **Kali rolling breakage judgment** — rolling can introduce regressions; deciding
  when to pin or hold.
- **Backports for out-of-distro code** — anything you carry outside the distro
  (custom kernel patches, custom packages) has no distro CVE stream covering it.

---

## 6. `mobilelinux security-status`

```
mobilelinux security-status
```

Implemented by `security_status_command()` in
[security/status.py](../src/mobilelinux/security/status.py). It prints:

1. **Version, security patch level, kernel, device** — read from the on-device OTA
   state (`/etc/mobilelinux/state.json`,
   [ota/state.py](../src/mobilelinux/ota/state.py)).
2. **Components** — the rootfs (OTA-managed) and the kernel version.
3. **Known affected CVEs** — if `debsecan` is installed, it runs
   `debsecan --format packages` and lists the affected packages (first 10, then a
   count). `debsecan` maps the local **dpkg** database to known Debian/Kali CVEs.
   If `debsecan` is not installed, it says so and suggests
   `apt install debsecan`.
4. **How updates work** — the apt-vs-OTA reminder, pointing here and to
   [cve-management.md](cve-management.md).

Example output shape:

```
── MobileLinux ──────────────
  Version:              1.0.1
  Security patch level: 2026-09-01
  Kernel:               7.2-rc5
  Device:               rhodep

── Known affected CVEs ──────
  3
    CVE-2026-1111 openssl
    …
```

---

## 7. Security patch level is a **date**

Android represents its "security patch level" as a date-like string tied to
Android's monthly bulletin model. MobileLinux borrows the **idea** (a single value
that says "how current is this system's security state?") but uses a plain **date**
because it is the Linux-appropriate representation:

- Debian/Kali security is continuous, not a monthly Android bulletin.
- A date cleanly answers "what is the security state as of when?" for a
  distro whose fixes flow constantly.

The value is a date such as `2026-09-01`
([manifest.schema.json](../schema/manifest.schema.json) line 57;
`_security_patch_level()` in [ota/release.py](../src/mobilelinux/ota/release.py)
lines 94–95 uses today's date). It is carried in the manifest's
`security.security_patch_level`, stored on the device
([ota/state.py](../src/mobilelinux/ota/state.py)), and shown by
`security-status`.

---

## 8. The kernel: ride upstream, don't hoard patches

Because the kernel is **mainline** (not an Android vendor fork with a large patch
stack), MobileLinux inherits the kernel's own CVE process
([research-ota-architecture.md](research-ota-architecture.md), "Linux kernel CVE
process"):

- The kernel is its own CNA; CVEs are auto-assigned to **stable-tree bugfixes**.
- A fix lands in mainline → is backported to active **stable/LTS** → gets a CVE →
  Debian picks it into its kernel package → reaches us via the archive.
- Guidance is to **take whole stable releases**, not cherry-picks, since a single
  fix often spans multiple commits.

So rather than maintaining hundreds of hand-picked kernel patches, MobileLinux
tracks the appropriate stable/LTS branch and pulls fixes as whole releases. See
[cve-management.md](cve-management.md) for how kernel CVEs are mapped.

### Indicating current vs update-available

- **Current** — `mobilelinux update --status` /
  `mobilelinux security-status` show the installed version, kernel version and
  patch-level date.
- **Update available** — `mobilelinux update --check`
  ([ota.md](ota.md#8-the-cli)) fetches the signed manifest and reports whether a
  newer signed release (with a newer patch level and any `fixed_cves`) exists for
  the channel. The manifest's `security.fixed_cves` and `security_patch_level`
  tell you what a pending update would fix.
