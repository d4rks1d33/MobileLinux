# SBOM (Software Bill of Materials)

This document explains what an **SBOM** is, why MobileLinux generates one for
every release, and how the generator works.

Related docs:
[cve-management.md](cve-management.md) ·
[security-updates.md](security-updates.md) ·
[release-process.md](release-process.md) ·
[ota.md](ota.md)

Source: [ota/sbom.py](../src/mobilelinux/ota/sbom.py).

---

## 1. What is an SBOM?

An **SBOM** is a *Software Bill of Materials* — a structured, machine-readable
**inventory of everything inside a piece of software**: every package, its
version, and (ideally) its origin and license. Think of it like the ingredients
list on food packaging, but for an OS image.

An SBOM is the artifact that lets you answer, months after a release ships and
without the build machine in front of you:

> **"Which of our devices/releases contain a vulnerable version of package X?"**

Without an SBOM you would have to rebuild or unpack each image to find out. With an
SBOM per release, a new CVE in (say) `openssl` becomes a quick query: search each
release's SBOM for `openssl`, read the version, compare against the fixed version.
This is the backbone of [cve-management.md](cve-management.md).

---

## 2. How MobileLinux generates the SBOM

Generation is `generate_sbom()` in
[ota/sbom.py](../src/mobilelinux/ota/sbom.py), called from the release pipeline
([release.py](../src/mobilelinux/ota/release.py) lines 49–51). It has a preferred
path and a fallback, so **a release always has some SBOM**.

### Preferred: syft

If **`syft`** (Anchore's SBOM tool) is available and a rootfs directory exists,
the generator runs it against the rootfs
([ota/sbom.py](../src/mobilelinux/ota/sbom.py) lines 21–23):

```
syft dir:<rootfs> -o cyclonedx-json=<out>
```

`syft` understands **Debian `dpkg`** (and dozens of other ecosystems) and can emit
**SPDX** (`spdx-json`) or **CycloneDX** (`cyclonedx-json`). The default format in
the code is `cyclonedx-json`. This is the full-fidelity path and is preferred for
real releases.

### Fallback: parse dpkg status

If `syft` is not installed, the generator falls back to reading the rootfs's
**dpkg status** file directly
([ota/sbom.py](../src/mobilelinux/ota/sbom.py) lines 25–57):

- It looks for `<rootfs>/var/lib/dpkg/status`.
- `_parse_dpkg_status()` (lines 60–77) walks the stanzas and extracts each
  package's **name**, **version** and **architecture**.
- It emits a **minimal CycloneDX 1.5** document: an `operating-system` metadata
  component named `mobilelinux-<device>` at the release version, plus one
  `library` component per package, each with a Debian **purl**
  (`pkg:deb/debian/<name>@<version>?arch=<arch>`).

If there is no rootfs at all, it writes an empty SBOM "shell" and notes that
`syft` should be installed for a full SBOM. Either way the release proceeds — the
release must never fail just because the richer tool is missing, but a maintainer
is told to install `syft` for completeness.

---

## 3. SPDX vs CycloneDX (brief)

There are two dominant SBOM formats
([research-ota-architecture.md](research-ota-architecture.md), "SBOM" section):

- **SPDX** (ISO/IEC 5962, Linux Foundation) — broad, with a licensing/compliance
  heritage; an ISO standard.
- **CycloneDX** (OWASP) — security/vulnerability-centric (supports VEX,
  dependency/vulnerability focus).

They express largely the same inventory with different emphases, and most tooling
(including `syft`) can emit **both**. MobileLinux defaults to **CycloneDX** because
its security/vulnerability orientation matches the primary use case here
(CVE detection — see [cve-management.md](cve-management.md)). The `fmt` parameter
in [ota/sbom.py](../src/mobilelinux/ota/sbom.py) allows requesting SPDX from
`syft` when needed.

---

## 4. Where the SBOM lands, and how it links from the manifest

The release pipeline writes the SBOM into the release directory as
`sbom.cyclonedx.json`
([release.py](../src/mobilelinux/ota/release.py) line 50):

```
out/releases/<device>/<version>/
├── manifest.json
├── sbom.cyclonedx.json      ← the SBOM
└── <artifacts…>
```

The manifest then **links** to it via `security.sbom_url`
([manifest.schema.json](../schema/manifest.schema.json) line 59;
`_build_manifest()` in [release.py](../src/mobilelinux/ota/release.py) line 128).
When the release is published, the SBOM is uploaded alongside the manifest and
artifacts ([release-process.md](release-process.md#5-the-publish-step)), so
`security.sbom_url` resolves to a fetchable URL.

Because the SBOM URL is part of the manifest **body**, it is covered by the
release **signature** ([signing.md](signing.md)) — you can trust that the SBOM a
device (or an auditor) fetches is the one that goes with the signed release, not a
substitute.
