# Release Process

This document explains how a MobileLinux **release** is produced, signed and
published. Read [ota.md](ota.md) first for the concepts (manifest, channels,
A/B, signing) — this document focuses on the maintainer-side workflow driven by
`mobilelinux release`.

Related docs:
[ota.md](ota.md) ·
[signing.md](signing.md) ·
[sbom.md](sbom.md) ·
[security-updates.md](security-updates.md)

Source: [ota/release.py](../src/mobilelinux/ota/release.py),
CLI wiring in [cli/main.py](../src/mobilelinux/cli/main.py) lines 76–79.

---

## 1. What a "release" is

A release is a self-contained, **publishable directory** describing one version
of one device on one channel. It contains:

- the release **artifacts** (rootfs image, boot image, etc.),
- an **SBOM** (software bill of materials — the list of everything inside; see
  [sbom.md](sbom.md)),
- a signed **manifest** (`manifest.json`) tying it all together with hashes and a
  signature.

Once produced, you upload this directory's contents to a static host (GitHub
Releases / HTTP / object store). Devices on that channel then discover and
install it via `mobilelinux update` ([ota.md](ota.md)).

---

## 2. The command

```
mobilelinux release <device> --version <X> --channel <Y>
```

- `<device>` — the device id, e.g. `rhodep`.
- `--version` — **required**; the version string for this release, e.g. `1.0.1`.
- `--channel` — the release track; defaults to `stable`
  (`stable` | `beta` | `nightly`). See [ota.md](ota.md#9-channels).

Example:

```
mobilelinux release rhodep --version 1.0.1 --channel stable
```

---

## 3. What the command does, step by step

The pipeline is `release_command()` in
[ota/release.py](../src/mobilelinux/ota/release.py). It is intentionally
**reproducible**: given the same inputs it produces the same manifest body, so
two builds can be compared and a signature is meaningful.

### Step 1 — Ensure build artifacts exist

It looks for `out/<device>/artifacts.json`
([release.py](../src/mobilelinux/ota/release.py) lines 36–47).

- If present, it loads the `ArtifactSet` and **verifies** the artifacts (hashes
  against the files on disk). If verification fails, the release **aborts** — you
  do not want to sign artifacts that do not match their recorded hashes.
- If absent, it warns and continues with placeholders, so you can dry-run the
  manifest shape before a real build. (`run mobilelinux build first`.)

### Step 2 — Generate the SBOM

It calls `generate_sbom()` (see [sbom.md](sbom.md)) against the rootfs and writes
`sbom.cyclonedx.json` into the release directory
([release.py](../src/mobilelinux/ota/release.py) lines 49–51). This records the
exact package versions shipped, which is what lets you later answer "which
releases contain a vulnerable version of package X?" ([cve-management.md](cve-management.md)).

### Step 3 — Compute the security patch level

The **security patch level** is a **date** representing the security state of the
release. In the current implementation it is today's date
(`_security_patch_level()`, [release.py](../src/mobilelinux/ota/release.py) lines
94–95). See [security-updates.md](security-updates.md#security-patch-level-is-a-date)
for why a date is the correct representation for a Linux distro.

### Step 4 — Assemble the manifest

`_build_manifest()` ([release.py](../src/mobilelinux/ota/release.py) lines
98–132) builds the manifest object:

- `release`: version, channel, ISO-8601 build date, distro, desktop.
- `device`: id, codename, architecture, `minimum_version` (currently `"0.0.0"`),
  and `ota_strategy` (from the device definition — `ab` for rhodep).
- `artifacts`: one entry per component, each with its `url` (a relative filename,
  rewritten to an asset URL on publish), `sha256`, `size` and `type`.
- `security`: the patch level, kernel version, `sbom_url` (the SBOM filename) and
  an initially empty `fixed_cves` list.

This matches [schema/manifest.schema.json](../schema/manifest.schema.json)
exactly — see the field walk-through in [ota.md](ota.md#6-the-manifest-format).

### Step 5 — Sign the manifest

It looks for the channel's private key at `keys/<channel>.ed25519.key`
([release.py](../src/mobilelinux/ota/release.py) lines 60–74):

- If present, it computes the **canonical body** (`manifest.canonical()` →
  everything except `signature`) and signs it with ed25519, then stores the
  result in `manifest.data["signature"]` with `algorithm: ed25519` and
  `key_id: <channel>`.
- If absent, it warns that the manifest is **UNSIGNED** and tells you to run
  `mobilelinux keygen --channel <channel>`. An unsigned manifest will be
  **refused** by every device ([ota/client.py](../src/mobilelinux/ota/client.py)
  `_verify_signature`), so this is only useful for local testing.

The private key is **never** embedded in the release. See [signing.md](signing.md).

### Step 6 — Write the publishable `release/` tree

It writes `manifest.json` and stages the artifacts into
`out/releases/<device>/<version>/` (`_stage_artifacts()`,
[release.py](../src/mobilelinux/ota/release.py) lines 135–141). Artifacts that
exist on disk are copied; artifacts that are not present are noted so you know to
upload them alongside the manifest.

The command finishes by printing the manifest and SBOM paths, the version/channel/
patch level, and a reminder that **publishing is a separate step**.

---

## 4. Reproducibility goal

The manifest body is serialized canonically (sorted keys, no incidental
whitespace — see `canonical_body()` in
[ota/manifest.py](../src/mobilelinux/ota/manifest.py) line 15). Combined with
deterministic artifact builds, this means:

- the same inputs produce the same signable bytes,
- a signature is verifiable independently of who serialized the JSON,
- two independent builds of the "same" release can be diffed to catch surprises.

This is why signing operates over a **canonical body** rather than the raw JSON
text — key ordering or whitespace differences must not change what is signed. See
[signing.md](signing.md).

---

## 5. The publish step

`mobilelinux release` **produces** a release directory; it does **not** upload
anything. Publishing is deliberately separate so that signing can happen on an
offline/HSM machine and uploading can happen anywhere.

To publish, upload the contents of `out/releases/<device>/<version>/` as release
assets to the channel's location — e.g. a GitHub Release, an HTTP directory, or an
object-store prefix — laid out so the device can fetch:

```
<metadata_url>/<channel>/manifest.json
<metadata_url>/<channel>/<artifact files…>
```

That URL shape is exactly what the client builds
([ota/client.py](../src/mobilelinux/ota/client.py) lines 80 and 137). The device
only needs the **manifest URL** and the **release public key** (already on the
device at `/etc/mobilelinux/keys/`); it never needs credentials to the phone.

---

## 6. Worked scenario: 1.0.0 → 1.0.1 reaches a device

This is the end-to-end story from the spec.

### Maintainer side

1. **Cut 1.0.0.** Build artifacts, then:

   ```
   mobilelinux release rhodep --version 1.0.0 --channel stable
   ```

   Upload `out/releases/rhodep/1.0.0/` to the `stable` channel. Devices flashed
   with 1.0.0 record `version: 1.0.0` in `/etc/mobilelinux/state.json`.

2. **Change something** (a package bump, a fix). Rebuild the artifacts.

3. **Cut 1.0.1.**

   ```
   mobilelinux release rhodep --version 1.0.1 --channel stable
   ```

   The pipeline regenerates the SBOM, recomputes the patch level, assembles a new
   manifest with fresh artifact hashes, and **signs** it with the stable key.

4. **Publish 1.0.1** to the `stable` channel (overwriting the channel's
   `manifest.json`, keeping the versioned artifacts).

### Device side

A device on 1.0.0 runs:

```
mobilelinux update
```

and the client ([ota/client.py](../src/mobilelinux/ota/client.py)):

1. **Detects** — fetches `<metadata_url>/stable/manifest.json`.
2. **Verifies device** — `_acceptable()` confirms `device.id == rhodep`,
   architecture matches, and `1.0.0 >= minimum_version`.
3. **Verifies signature** — `_verify_signature()` checks the ed25519 signature
   against the stable public key on the device.
4. **Compares versions** — `version_gt("1.0.1", "1.0.0")` is true, so an update
   is available.
5. **Downloads** the artifacts and **verifies each SHA-256**.
6. **Installs atomically** — because rhodep is `ab`, RAUC writes the new image to
   the inactive slot.
7. **Reboots** into the new slot.
8. **Health check** — on success the slot is marked **good**; on failure the
   bootloader **rolls back** to 1.0.0. See [recovery.md](recovery.md#the-health-check).

The device now reports `version: 1.0.1`. Running:

```
mobilelinux security-status
```

shows the new **version** and **security patch level** from the updated state
([security/status.py](../src/mobilelinux/security/status.py)), confirming the
device is current. See [security-updates.md](security-updates.md).
