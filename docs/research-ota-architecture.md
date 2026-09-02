# Update / OTA & Security-Patching Architecture Report

**Target platform:** mobile Linux, Debian/Kali-based rootfs, mainline kernel, Qualcomm A/B-slotted phones (reference device: Motorola *rhodep*), some devices single-rootfs only.

**Reference device facts that constrain everything below:**

- Rootfs is a **GPT disk written inside the Android `userdata` partition** (starts at sector 4096).
- Boot is an **Android boot image v2 flashed to `boot_a`** (mainline kernel + initramfs + cmdline + dtb packed and signed as an Android Boot Image).
- Device is **A/B slotted** at the Android-bootloader level: the Qualcomm/Android bootloader (aboot/ABL) picks `boot_a`/`boot_b` based on the active slot and injects `androidboot.slot_suffix=_a`/`_b` into the kernel command line.
- **No GRUB, no U-Boot.** The bootloader is a closed Android bootloader; you cannot script it, and you do not necessarily have a U-Boot-style bootcounter you can freely write.

> Accuracy note: I verified each technology against official docs (URLs cited inline). Where I state something the official docs do not explicitly confirm for *this exact* Android-boot-image-in-userdata layout, I say so. The Android `update_engine` internals below are described from general knowledge plus the OSTree "aboot" doc; the official `source.android.com/docs/core/ota` pages timed out during research and are cited but not quoted.

---

## 1. RAUC — https://rauc.io / https://rauc.readthedocs.io

### How it works
RAUC is an image-based A/B (or A/B/C/…) updater. Updates ship as a signed **bundle**: a SquashFS image containing a **manifest** (`manifest.raucm`), the payload image(s)/archive(s), and optional hooks/scripts. On the target, the SquashFS is mounted (not unpacked to intermediate storage) and images are written to the appropriate inactive **slot**.
Refs: https://rauc.readthedocs.io/en/latest/basic.html

### Atomicity model
Per-slot atomicity. RAUC deactivates the target slot in the bootloader, writes the full image, then flips the boot selection. The rootfs itself is written as a whole image (raw/ext4) or extracted from a tar into a freshly-created filesystem. There is no partial/half-updated visible state on the active slot.

### Rollback support
Yes — this is core. RAUC drives a **redundant boot** scheme: it sets boot priority/attempts for the inactive slot, and after boot the running system must call `rauc status mark-good` (or `mark-bad`). If the new slot fails to boot, the bootloader's remaining-attempts logic falls back to the previous slot. RAUC recommends a hardware watchdog to catch hangs.
Refs: https://rauc.readthedocs.io/en/latest/basic.html (Boot Confirmation and Fallback)

### A/B vs single
Native A/B (and N-way). It also supports **recovery/A** (asymmetric) and can bootstrap A/B from an external/factory system (`rauc.external`). For a **single-rootfs** device RAUC gives you little — you would need at least a recovery slot to update the single rootfs safely; RAUC does not do in-place atomic rootfs replace on a single partition.

### Signing model
**Mandatory** bundle signing using **CMS** (RFC 5652), backed by **OpenSSL / x509**. Supports self-signed single key, separate dev/release keys, single or separate CAs, intermediate certificates, CRLs, and **PKCS#11** (YubiKey/HSM/AWS-KMS) for the signing key. Verification is against a keyring on the device (`/etc/rauc/keyring.pem`). The `verity` and `crypt` bundle formats additionally get dm-verity integrity (and optional dm-crypt encryption).
Refs: https://rauc.readthedocs.io/en/latest/advanced.html (Security, PKCS#11)

### Delta support
- **Adaptive updates** (`block-hash-index`): a per-4KiB-block SHA256 index in the bundle; during install RAUC reuses matching blocks already present on either slot, downloading only changed blocks over HTTP streaming. ~10% download for a single-package change in an ext4 image. Works on block devices only (not tar).
- **casync** integration (chunk store) also exists.
Refs: https://rauc.readthedocs.io/en/latest/advanced.html (Adaptive Updates, casync)

### HTTP / hosting model
Since 1.7, **HTTP(S) streaming** installs bundles directly from a server that supports **Range Requests** (needs `verity` format + NBD in the kernel). This means **plain static HTTP hosting works** — e.g. GitHub Releases / a CDN / an S3 bucket — no server-side agent, no SSH into the phone. Supports bearer tokens, custom headers, TLS client certs. There is also a **preview polling** feature (`[polling]` in `system.conf`) that periodically fetches a fixed URL and can auto-install.
Refs: https://rauc.readthedocs.io/en/latest/advanced.html (HTTP Streaming, Update Polling)

### Bootloader integration — the key question for this project
RAUC selects a bootloader backend via `bootloader=` in `system.conf`. Built-ins:
- `barebox` (recommended by RAUC; uses bootchooser + state) — **we don't have barebox**.
- `uboot` (fw_setenv/fw_getenv + a boot.scr script) — **we don't have U-Boot**.
- `grub` (grub-editenv) — **no GRUB**.
- `efi` (efibootmgr) — **not applicable on Qualcomm Android boot**.
- **`custom`** — RAUC calls an external handler script for all boot-state operations.
Refs: https://rauc.readthedocs.io/en/latest/integration.html (Interfacing with the Bootloader)

**The `custom` backend is the answer for an Android A/B bootloader.** With `bootloader=custom` and `[handlers] bootloader-custom-backend=<script>`, RAUC invokes your script with:
- `get-primary` → print the bootname of the current primary slot
- `set-primary <bootname>` → make a slot primary
- `get-state <bootname>` / `set-state <bootname> good|bad`
- (optional) `get-current-booted-slot`

Your script implements these by driving the **Android slot-selection mechanism** — i.e. reading/writing the A/B slot metadata the Qualcomm bootloader uses. In practice that means calling `bootctl` (from Android's `boot_control` HAL) or writing the misc/`bootloader_control` A/B metadata directly, plus setting the active slot / successful flag. RAUC's booted-slot detection can also key off the kernel cmdline: the Android bootloader already injects `androidboot.slot_suffix=_a`, and RAUC supports **explicit identification via `rauc.slot=` on the kernel cmdline** — so you can map the active slot to a RAUC `bootname` cleanly.
Refs: https://rauc.readthedocs.io/en/latest/integration.html (Custom, Booted Slot Detection)

**Can RAUC drive a bootloader that just boots `boot_a`/`boot_b` by slot suffix? Yes**, via the custom backend, provided you have a userspace way to (a) read the active slot and (b) set the active slot + mark-good/bad. On Qualcomm this is exactly what Android's `bootctl`/`boot_control` HAL does against the A/B metadata; you wire those calls into the custom handler. This is a real integration task, but it is the intended extension point.

**Kernel requirements:** loop + SquashFS (+ dm-verity for verity format, + NBD for streaming, + dm-crypt for crypt). All available in mainline.
Refs: https://rauc.readthedocs.io/en/latest/integration.html (Kernel Configuration)

### Where the kernel goes
RAUC treats the **boot image as just another slot**. Because your kernel lives in the Android boot image on `boot_a`/`boot_b`, you define a `boot` slot (type `raw`, or the dedicated `boot-*` switch types) and ship a new signed Android boot image as a payload alongside the rootfs image, grouped so rootfs+boot update together. RAUC does not build the Android boot image — your CI does (mkbootimg) — RAUC just writes it to the inactive boot partition.

### Fit for this project
**Strong.** Debian rootfs is fine (RAUC is distro-agnostic C/GLib/OpenSSL, packaged in Debian). Signed bundles, static HTTP/GitHub hosting, atomic A/B rootfs+kernel, rollback via custom Android-slot backend, delta via adaptive updates. Weak on single-rootfs devices (needs a recovery slot).

**Pros:** purpose-built for exactly this (A/B image + bootloader abstraction), mandatory signing with real PKI/HSM, static hosting, D-Bus API for a UI, delta, Debian-friendly.
**Cons:** you must write and maintain the custom Android-slot handler; single-rootfs devices need a recovery partition or lose atomicity; RAUC does not manage in-place package/security updates (that's apt's job).

---

## 2. Mender — https://mender.io / https://docs.mender.io

### How it works
Client/server system. A **build system** produces **Mender Artifacts** (`.mender` files); you upload them to the **Mender Server**; each device runs the **Mender Client** which polls the server, downloads, installs to the inactive rootfs partition, verifies checksum, sets a bootloader flag, reboots, and **commits** on success.
Refs: https://docs.mender.io/overview/introduction

### Atomicity / rollback / A/B
Dual-redundant **A/B rootfs**. Writes to inactive partition, verifies checksum, sets bootloader flag to swap, reboots; on first boot the client **commits**; if the device reboots before commit, the bootloader **rolls back** by swapping partitions back. Same conceptual model as RAUC. Requires a **stateless** rootfs (state lives on a separate data partition).

### Artifact format & signing
`mender-artifact` format (a tar with headers, provides/depends metadata, and payloads). Artifacts can be **signed and verified** (`mender-artifact sign`, key on device).
Refs: https://docs.mender.io/artifact-creation/sign-and-verify

### Server dependency — the key question
Mender is **fundamentally server-oriented** (managed/*Hosted Mender* SaaS or self-hosted). BUT:
- **Self-hosting is fully supported**: Docker-Compose evaluation, or Kubernetes production. So you are **not** locked into their SaaS.
  Refs: https://docs.mender.io/server-installation/overview
- **Standalone mode** exists: the client installs a local or URL-fetched artifact with **no server** (`mender install <url|file>`), typical for USB/manual updates.
  Refs: https://docs.mender.io/orchestrate-updates/standalone-mode, https://docs.mender.io/artifact-creation/standalone-deployment

So Mender *can* run without their SaaS: either self-hosted server, or standalone client pulling a signed artifact off static HTTP. Bootloader integration for A/B is via U-Boot/GRUB (`meta-mender`) — and it has a Debian family flow (`mender-convert`) but that flow assumes U-Boot/GRUB boot, **not an Android boot image**. There is no built-in "Android A/B bootloader" backend equivalent to RAUC's `custom` script.
Refs: https://docs.mender.io/operating-system-updates-debian-family/overview

### Fit for this project
**Moderate/weak for MVP.** Debian family is supported and standalone mode removes the server, but its A/B bootloader integration targets U-Boot/GRUB. Reproducing RAUC's `custom` Android-slot backend in Mender is more DIY (Update Modules/state scripts) and less first-class. The managed-fleet features (deployments, inventory, RBAC) are real strengths if you later want fleet management — but they add a server you said you don't want for MVP.

**Pros:** mature fleet management, self-hostable, standalone mode, Debian flow, delta (`mender-binary-delta`), signed artifacts.
**Cons:** heavier; best value needs the server; Android-boot-image A/B is not a first-class backend; more moving parts than RAUC for a static-hosted MVP.

---

## 3. OSTree / libostree — https://ostreedev.github.io/ostree

### How it works
"Git for OS binaries." A **content-addressed object store** at `/ostree/repo`; bootable filesystem trees are tracked as **refs**. Deployments live at `/ostree/deploy/$stateroot/$checksum`, composed of **hardlinks** into the store (so multiple versions are deduplicated; an upgrade costs only the size of changed files). `/usr` is a read-only bind mount; only `/etc` (3-way merged on upgrade) and `/var` (shared) persist.
Refs: https://ostreedev.github.io/ostree/introduction/

### Atomicity / rollback
Fully atomic: a new deployment is staged and the bootloader config is swapped as the last step (`ostree-finalize-staged.service` on shutdown). Rollback is just booting the previous deployment (still on disk). **No A/B *partition* needed** — multiple deployments coexist on one filesystem. This is the standout property for **single-rootfs** devices.
Refs: https://ostreedev.github.io/ostree/atomic-rollbacks/

### How kernel/bootloader are handled — and Android relevance
OSTree normally writes BLS entries into `/boot/loader/entries` and expects GRUB/systemd-boot/etc. **Critically, OSTree has an explicit Android-boot (`aboot`) integration:**
- `aboot-update` generates **Android Boot Images** on the server side.
- `aboot-deploy` reads the current slot from `androidboot.slot_suffix=`, writes the new boot image to the alternate `boot_a`/`boot_b`, and sets a symlink `/ostree/root.a` or `/ostree/root.b` so the right userspace deployment is booted for each slot.
- Docs note the Android bootloader injects kargs, and **patches to the Android bootloader may be needed** so firmware doesn't inject a conflicting `root=` or a `ro` karg (incompatible with OSTree).
Refs: https://ostreedev.github.io/ostree/bootloaders/ (OSTree and aboot)

**This is essentially your exact scenario** (mainline kernel in an Android boot image, A/B slots, slot_suffix karg). CentOS Automotive Stream Distro uses precisely this aboot path.

### Signing model
OSTree commits can be **GPG-signed**, and newer versions support **ed25519** signatures; repos are served over plain **HTTP/HTTPS** (static). **composefs** (optional) adds fs-verity-backed integrity for the deployment. So static hosting (GitHub Releases / object storage) + signed commits works.
Refs: https://ostreedev.github.io/ostree/composefs/, https://ostreedev.github.io/ostree/formats/

### Delta support
**Static deltas** for efficient offline/HTTP updates (precomputed diffs between commits). Plus inherent dedup (only changed files transferred).
Refs: https://ostreedev.github.io/ostree/copying-deltas/

### Works on Debian?
Yes in principle: OSTree is packaged in Debian (`libostree`/`ostree`), and the docs describe **adapting existing dpkg-based distros** to OSTree — but this is **a significant re-architecture**: UsrMove, read-only `/usr`, `/home`→`/var/home` etc. symlink layout, moving package DB into `/usr`, static or systemd-managed users (`nss-altfiles`/`nss-systemd`/`sysusers.d`), and an initramfs (dracut or custom) that parses `ostree=` and does the bind-mount/`mount(MS_MOVE)` switchroot. **apt does not "just work"** — you'd need an rpm-ostree-equivalent layering flow for Debian, which does not exist as a turnkey product (dpkg-ostree/apt-ostree efforts exist but are niche).
Refs: https://ostreedev.github.io/ostree/adapting-existing/

### Fit for this project
**Technically the best fit for single-rootfs atomicity AND it has native Android-boot-image support** — but the **Debian/Kali adaptation cost is high** and it changes the fundamental relationship with apt/security updates (you no longer do in-place `apt upgrade`; you rebuild trees server-side). How Endless OS and Fedora Silverblue use it: they compose OS trees on a server and clients replicate + atomically deploy; app installs go through Flatpak, not the base package manager.

**Pros:** atomic + rollback **without** A/B partitions (huge for single-rootfs devices), dedup + static deltas, native `aboot` Android-boot-image backend matching your layout, static HTTP hosting, signed commits.
**Cons:** requires re-architecting a Debian/Kali rootfs (UsrMove, read-only /usr, /var layout, initramfs integration, user handling); **breaks the "just use apt" model** — you'd compose trees, not run apt in place; may need Android-bootloader patches to avoid karg conflicts; less turnkey than RAUC for a Debian shop.

---

## 4. systemd-sysupdate — https://man.archlinux.org/man/systemd-sysupdate.8.en

### How it works
Declarative A/B updater driven by **transfer files** (`*.transfer` in `sysupdate.d/`). Each transfer maps a **source** (HTTP/HTTPS file/tar, or local file) to a **target** (GPT **partition**, regular file, directory, subvolume). A set of transfers sharing a version (`@v`) constitute one OS update (e.g. rootfs partition + verity partition + kernel/UKI file). Supports A/B/C/…; picks the newest source version, writes into an inactive partition/slot, then renames into place.
Refs: https://man.archlinux.org/man/sysupdate.d.5.en

### Atomicity / partitions
Partition-based via **GPT partition type UUIDs** and the **Discoverable Partitions Spec**; a partition labeled `_empty` is a target. **It will not create partitions** — they must pre-exist (use `systemd-repart`). Writes are robust (incomplete downloads detected and flushed), and the entry point (kernel/UKI) is deliberately written last.

### Rollback / boot assessment
Integrates with **systemd's Automatic Boot Assessment** via `@l`/`@d` (tries-left/tries-done) counters embedded in kernel/UKI filenames — the boot loader decrements tries and falls back. This assumes a **BLS/UKI + sd-boot/GRUB** style boot, **not** an Android boot image.
Refs: https://systemd.io/AUTOMATIC_BOOT_ASSESSMENT

### Signing model
Source integrity via a **`SHA256SUMS`** manifest, authenticated by a **detached GPG signature `SHA256SUMS.gpg`**, verified against `/usr/lib/systemd/import-pubring.pgp` (or `/etc/systemd/...`). Payloads are checked against the SHA256 hashes in the manifest. So: **GPG-signed manifest over static HTTP** — works with GitHub Releases.
Refs: https://man.archlinux.org/man/sysupdate.d.5.en (Verify=)

### Delta / maturity
**No delta** (whole partition/file transfers). Maturity: usable and shipping in systemd (≥ v251, with ongoing additions through v257+), but it is **GPT-partition-centric** and its rollback story is tied to systemd boot-assessment / BLS. There is **no Android-boot-image backend**.

### Fit for this project
**Weak for this exact device.** Your rootfs is a GPT *inside* Android `userdata`, and your kernel is in an Android boot image, not a UKI on an ESP. sysupdate could write the rootfs partition, but its boot-assessment/rollback assumes systemd-managed boot entries you don't have, and there's no hook to drive Android slot metadata. You'd be fighting the model. GPG static hosting is nice, but RAUC/OSTree fit the Android boot reality far better.

**Pros:** in-box with systemd, declarative, GPG-signed static HTTP, clean A/B GPT model.
**Cons:** no Android-bootloader integration, rollback tied to systemd boot assessment/UKI, no delta, partitions must pre-exist, less proven than RAUC for phones.

---

## 5. systemd-sysext / confext — https://man.archlinux.org/man/systemd-sysext.8.en

### What it is
**Not a full-system updater.** `systemd-sysext` activates **system extension images** that overlay **`/usr` and `/opt`** (via overlayfs) at runtime; `systemd-confext` does the same for **`/etc`**. Extensions are `.raw` disk images (optionally dm-verity/signed) or directories in `/var/lib/extensions/` (`/var/lib/confexts/` for confext). Merged additively; strictly read-only by default; version-matched against the host `os-release` via an `extension-release` file.
Refs: https://man.archlinux.org/man/systemd-sysext.8.en

### Atomicity / rollback / signing
Activation is atomic (overlay mount) and trivially reversible (unmerge). Images can carry **Verity + signature** and be gated by image policy. There is no A/B/partition concept — it's a layering mechanism.

### Fit for this project
**Complementary, not a base updater.** Good for shipping **optional tool bundles / debug tools / add-on packages** (e.g. a Kali toolset extension) on top of an immutable base **without** rebuilding the rootfs — which maps nicely onto your "app/tool updates" separation. Cannot update the base OS, kernel, or firmware. Requires a `/usr` (+`/opt`) you're willing to overlay; works best when the base is immutable.

**Pros:** cheap, atomic, signed, great for optional/tool layers; matches "separate app/tool updates".
**Cons:** additive only (not for base OS/kernel/firmware); needs an immutable-ish base to be meaningful; not a rollback mechanism for the OS itself.

---

## 6. Android A/B OTA / update_engine

### How it works
Android's `update_engine` consumes a **`payload.bin`** (inside an OTA zip) described by `payload_metadata` protobuf: a list of **partition operations** (REPLACE, BSDIFF/PUFFDIFF deltas, zero, copy) applied block-by-block to the **inactive slot** of each dynamic/partition. It is tightly integrated with Android's **`boot_control` HAL** and **`update_verifier`/`dm-verity`**, drives **A/B slot metadata** (`bootloader_control` in the `misc` partition), and marks slots successful after a verified boot. Delta payloads are generated by Android's `ota_from_target_files`.
Refs: https://source.android.com/docs/core/ota/ab, https://source.android.com/docs/core/ota (both cited; pages timed out during research so not quoted — treat internals as general knowledge).

### Why it's tied to Android userspace
`update_engine` expects the **Android build system's target-files**, `payload.bin` format, the `boot_control`/`hardware` HAL stack, Android's `dm-verity`/AVB metadata, `postinstall` semantics, and (for dynamic partitions) `liblp`/`libsnapshot` (virtual A/B). It is not designed to lay down a Debian ext4 rootfs or a GPT-inside-userdata; it operates on Android's partition/HAL abstractions. Reusing it standalone means porting large chunks of Android userspace.

### Why we probably can't reuse it directly
- Payload generation is bound to `ota_from_target_files` / Android target-files, not a Debian image pipeline.
- It assumes Android's HAL + AVB + dm-verity chain and `misc` A/B metadata semantics.
- Signing is Android's OTA signing (payload signed with keys checked by `update_verifier`), not a general x509/CMS/GPG flow you'd host on GitHub.
- Bringing it up on a Debian rootfs is far more effort than writing RAUC's ~5-command custom slot handler that *reuses the same A/B metadata mechanism* at a much smaller surface.

**However:** the **A/B slot metadata mechanism itself** (`bootctl`/`boot_control`, the `misc` partition `bootloader_control` struct, slot_suffix) is exactly what your RAUC `custom` backend or OSTree `aboot-deploy` should drive. So we **reuse the Android slot-switching primitive**, not `update_engine` the daemon.

**Fit:** Not a base updater for a Debian rootfs. Its slot-control primitive is reused by RAUC/OSTree.

---

## 7. Ubuntu Core / snapd — https://documentation.ubuntu.com/core

### How it works
Ubuntu Core is an **all-snap** OS: the root, kernel, and gadget are **snaps** (`core`/`base` snap, **kernel snap**, **gadget snap**), plus app snaps. Transactional updates and rollback are handled by **snapd** with a read-only squashfs-per-snap model and A/B-style refresh + automatic revert on failure. Boot assets and partition layout are defined by the **gadget snap**; the kernel by the **kernel snap**. Everything is gated by **assertions** (model, serial, snap-declaration/revision) signed in Canonical's/your brand store.
Refs: https://documentation.ubuntu.com/core/explanation/core-elements/, .../reference/gadget-snap-format/, .../reference/assertions/

### Why it's ecosystem lock-in
- The update/identity model is centered on a **Snap Store** (Canonical's, or a paid **brand/dedicated store**) and **signed assertions** (model assertion, serial vault). Truly store-free operation is not the design center.
- Everything must be **snapped**: to run a Debian/Kali rootfs you'd effectively abandon apt and repackage the world as snaps (base/kernel/gadget + app snaps).
- CVE remediation, refresh control, remodeling, FDE — all are snapd/Canonical-ecosystem concepts.
Refs: https://documentation.ubuntu.com/core/explanation/stores/, .../explanation/cve-remediation/

### Fit for this project
**Poor.** It's a different OS and a different packaging universe; it discards the Debian/Kali + apt premise and pulls in store/assertion infrastructure. Not appropriate for a Debian/Kali phone MVP.

**Pros:** excellent transactional model, strong signing/identity, mature.
**Cons:** wholesale ecosystem adoption (snaps, store, assertions); abandons apt/Debian; store-centric.

---

## 8. Debian-based approaches

### 8a. apt + unattended-upgrades (package-level security updates)
- `apt`/`dpkg` do **in-place, non-atomic** package updates. `unattended-upgrades` automatically installs security (and optionally other) updates from configured origins; Debian's own docs recommend it as the way to stay current.
  Refs: https://www.debian.org/security/, https://wiki.debian.org/UnattendedUpgrades
- **Kali is rolling** (tracks Debian testing + Kali-specific packages); "security updates" are delivered continuously by rolling the whole archive rather than a stable+DSA backport stream. Kali provides `kali-rolling` and signed repos; you get fixes by `apt full-upgrade`.
- **Strengths:** you **leverage the entire Debian/Kali security apparatus** — thousands of maintained packages, CVE triage, DSAs — instead of hand-patching CVEs. Perfect for "OS/app/tool package + security updates."
- **Weaknesses:** **not atomic**, **not rollback-safe** (a bad upgrade or power loss mid-`dpkg` can break the system), and it mutates the running rootfs — which is exactly what an A/B image updater is good at avoiding.

### 8b. Image-based full updates
- Build a full rootfs image in CI (debootstrap/mmdebstrap + your kernel/boot image), sign it, ship it as an A/B image. This **is** what RAUC/Mender/OSTree do; "image-based Debian" is not a distinct tool so much as the practice of pairing Debian image builds with one of the above updaters.
- **Strengths:** atomic, rollback-capable, reproducible, testable as a unit.
- **Weaknesses:** larger downloads (mitigated by delta/adaptive/OSTree-dedup); you must decide how per-CVE security fixes flow (rebuild image on each fix vs. allow in-place apt between image releases).

### The natural synthesis
**Image-based A/B for the base OS + kernel (atomic, rollback) AND apt for in-place package/security updates between image releases.** This directly satisfies your "leverage Debian/Kali apt security, don't hand-patch CVEs" requirement while keeping atomic base updates. (See recommendation.)

---

# RECOMMENDATION (MVP)

## Chosen architecture: **RAUC (A/B image, custom Android-slot backend) + apt/unattended-upgrades (in-place security), layered.**

I recommend **RAUC as the base-image updater**, not OSTree, for the MVP — **because it preserves the Debian/Kali + apt model with the least re-architecture**, and its `custom` bootloader backend maps cleanly onto the Android `boot_a`/`boot_b` slot-suffix mechanism you already have. OSTree is arguably *more elegant* (atomic + rollback with no A/B partitions, plus a native `aboot` backend that matches your layout), and it's the better answer for **single-rootfs** devices — but adopting it forces UsrMove, read-only `/usr`, `/var` layout changes, custom user handling, initramfs `ostree=` integration, and **abandoning in-place apt** for a compose-trees-server-side workflow that has no turnkey Debian tooling. That's too much for an MVP on a Debian/Kali base whose whole value proposition is "it's Debian/Kali."

### Why RAUC over the alternatives (for this project, concretely)
- **vs Mender:** RAUC's `custom` backend is a first-class, ~5-verb script for Android slot control; RAUC streams signed bundles from **static HTTP/GitHub Releases** with no server. Mender's best features need a server, and its A/B bootloader story targets U-Boot/GRUB. (Mender's *standalone mode* is a viable fallback, but RAUC is a tighter fit.)
- **vs systemd-sysupdate:** sysupdate's rollback is tied to systemd boot-assessment/UKI, which you don't have on an Android boot image; RAUC's boot abstraction is designed for exactly this.
- **vs Android update_engine:** we **reuse Android's slot-switch primitive** (`bootctl`/`misc` A/B metadata) *inside* RAUC's custom handler, without dragging in Android userspace.
- **vs OSTree:** keep OSTree on the roadmap for a v2 / single-rootfs story; not for MVP.

### The layered update model (separate concerns explicitly)

| Layer | Mechanism | Atomic? | Rollback? | Hosting | Signing |
|---|---|---|---|---|---|
| **OS/rootfs image** | **RAUC** A/B bundle (verity format) → inactive rootfs slot in userdata GPT | Yes | Yes (Android slot fallback via custom backend) | Static HTTP / GitHub Releases (HTTP streaming, Range) | CMS/x509, PKCS#11/HSM |
| **Kernel / boot** | Ship a new **signed Android boot image** as a payload in the **same RAUC bundle**, grouped with rootfs → inactive `boot_a`/`boot_b` | Yes | Yes (same slot flip) | same bundle | CMS/x509 (bundle) + your Android boot-image signing |
| **Device packages + security/CVE** | **apt + unattended-upgrades** in place, between image releases; periodically **re-baseline** into the next RAUC image | No (in-place) | No (mitigated by A/B: bad apt state is wiped on next image update / rollback) | Debian/Kali signed repos | Debian/Kali repo signing |
| **Apps / tools** | **systemd-sysext** optional extension images (e.g. Kali toolset), or plain apt | Yes (sysext overlay) | Yes (unmerge) | static HTTP | Verity + signature |
| **Firmware** | Treated as its own RAUC slot/artifact (raw), or vendor tooling; updated deliberately, rarely | Yes (slot) | depends on HW | same bundle | CMS/x509 |

**Why this split is correct for your goals:**
- **Atomic rootfs + rollback where HW allows:** RAUC A/B on Qualcomm devices. On **single-rootfs** devices, RAUC still installs, but you **lose atomic rollback** — mitigate with a small **recovery slot** (recovery/A asymmetric setup) or accept in-place updates there. (This is the one place OSTree would shine; note it for those SKUs.)
- **Leverage Debian/Kali security, don't hand-patch CVEs:** apt/unattended-upgrades pulls the entire distro security stream in place; you periodically **fold the current package state into the next signed base image** so fresh flashes and A/B rollbacks stay current. You never manually track CVEs to patch — the distro does.
- **Signed releases on GitHub/static HTTP, no SSH into the phone:** RAUC HTTP streaming + polling from a fixed URL; apt over signed repos. No inbound access to the device.
- **Separation of OS vs kernel vs packages vs firmware vs tools:** encoded in the table — RAUC bundle (OS+kernel+firmware, grouped slots), apt (packages/CVE), sysext (tools).

### Concrete MVP build-out
1. **Partitioning:** within the userdata GPT, define rootfs A/B slots; use existing `boot_a`/`boot_b` for the Android boot images; a small **data partition** for `/etc` overrides + `/var` state (RAUC data migration hook or redundant data partitions).
2. **RAUC `system.conf`:** `bootloader=custom`; slots `rootfs.0/1` (ext4 or verity) with `bootname` mapped to `_a`/`_b`; grouped `boot.0/1` (raw) parented to rootfs; booted-slot detection via `rauc.slot=` derived from `androidboot.slot_suffix`.
3. **Custom backend script:** implement `get/set-primary`, `get/set-state`, `get-current-booted-slot` by calling `bootctl`/writing the `misc` `bootloader_control` A/B metadata (mark-active, mark-successful, decrement tries).
4. **CI:** build Debian/Kali rootfs image + mainline kernel Android boot image (your existing `mkbootv2b.py`/mkbootimg flow) → `rauc bundle` (verity) → sign with release key (PKCS#11/HSM) → publish to GitHub Releases.
5. **On device:** `unattended-upgrades` for security between releases; RAUC polling for base images; `mark-good` after a health check + hardware watchdog for hang detection.
6. **Tools:** optionally ship the heavy Kali toolset as a signed `systemd-sysext` image so the base stays lean and the toolset updates independently.

---

# Security / CVE side (brief, sourced)

### Debian Security Tracker & data feeds — https://security-tracker.debian.org
- The tracker maps CVEs ↔ Debian **source** packages ↔ fixed versions ↔ per-suite status (vulnerable/fixed/not-affected). Web UI at `/tracker/<CVE|DSA|package>`.
- **Machine-readable feeds** (for automation): the tracker publishes data you can pull, notably:
  - `https://security-tracker.debian.org/tracker/data/json` — full CVE↔package↔fixed-version JSON.
  - The raw source lists (`CVE/list`, `DSA/list`) from the `security-tracker` data repo.
- Debian also **publishes OVAL** definitions and **DSAs are CVE-compatible**.
  Refs: https://www.debian.org/security/ (OVAL, CVE-compatibility, DSA list)

### Debian DSA
- **Debian Security Advisories** are issued for **stable**; each maps to CVEs and fixed package versions, announced on `debian-security-announce` and linked from the tracker. Example current linux DSAs seen in research (e.g. DSA-6477-1 linux). Kali (rolling) does **not** use the stable DSA stream the same way.
  Refs: https://www.debian.org/security/

### Kali rolling & security
- Kali is **rolling** (Debian testing-based). Security fixes arrive by **rolling the archive**, not a separate stable+DSA backport track; you stay patched via `apt full-upgrade` against signed `kali-rolling`. (Kali's dedicated security-policy page returned 404 during research; this reflects the well-documented rolling model — flag as not-doc-confirmed here.)

### Mapping installed packages → CVEs
- **`debsecan`** (Debian Security Analyzer): reads the local dpkg status, downloads Debian vulnerability data, and reports vulnerabilities; `--only-fixed`/`--format packages` yields exactly the packages with available fixes (pipe into `apt-get install`). Good for automated per-device/per-image reporting.
  Refs: https://manpages.debian.org/bookworm/debsecan/debsecan.1.en.html
- **Security-tracker JSON** (above): script your own "installed version vs fixed version" diff for a rootfs — most robust for CI against an image.
- Caveat (from debsecan docs): tracking is by **source** package, so all binaries of a source get flagged (some false positives, e.g. docs-only binaries), and backported/unknown versions compare against unstable.

### Linux kernel CVE process — https://www.kernel.org/doc/html/latest/process/cve.html
- The kernel is now its **own CNA**. CVEs are **assigned automatically to stable-tree bugfixes**, tracked by the **git commit id** of the fix, announced on **`linux-cve-announce`** (`https://lore.kernel.org/linux-cve-announce/`).
- **No CVE for unfixed issues**; **no CVE for versions not on a supported Stable/LTS branch**. Distro-specific kernels' issues must be CVE'd by the distro, not kernel.org.
- **How mainline stable fixes flow:** fix lands in mainline → backported to active **stable/LTS** branches → CVE auto-assigned → distros (Debian) pick up into their kernel packages → DSA/rolling update. Guidance: **take whole stable releases**, not cherry-picks, since fixes often span multiple commits.
- **Applicability is the user's job:** kernel.org explicitly won't say whether a given CVE affects your config; huge numbers of kernel CVEs won't apply to your subset.

### SBOM: SPDX vs CycloneDX, and generation from a Debian rootfs
- **SPDX** (ISO/IEC 5962, Linux Foundation) and **CycloneDX** (OWASP) are the two dominant SBOM formats. SPDX is broad (licensing/compliance heritage, ISO standard); CycloneDX is security/vuln-centric (VEX, dependency/vulnerability focus). Most tooling emits both.
- **`syft`** (Anchore) generates SBOMs from **container images, filesystems, and archives**, understands **Debian `dpkg`** (and dozens of ecosystems), and outputs **SPDX (spdx-json), CycloneDX (cyclonedx-json), and syft-json**; can create signed **in-toto attestations**. Pair with **`grype`** to turn the SBOM into a vulnerability report.
  Refs: https://github.com/anchore/syft

### What can be automated vs. manual
**Automatable (put in CI + on-device cron):**
- SBOM generation of every rootfs image (`syft dir:<rootfs> -o spdx-json -o cyclonedx-json`).
- CVE mapping: `grype` on the SBOM, and/or `debsecan --only-fixed`, and/or diffing installed versions against the **security-tracker JSON**.
- Kernel CVE watch: subscribe/ingest `linux-cve-announce`; check your kernel branch against supported Stable/LTS.
- Applying fixes: `unattended-upgrades` on-device; rebuild+sign+publish RAUC base images in CI on a schedule or on high-severity CVEs.
- Release signing (PKCS#11/HSM) and publishing to GitHub Releases.

**Still manual / human-judgment:**
- **Applicability triage** — deciding whether a flagged CVE actually affects your device (kernel config subset, package actually reachable). Both kernel.org and debsecan explicitly push this onto you.
- **Risk acceptance / scheduling** — when to force a base-image roll vs. rely on in-place apt; embargoed/hardware issues.
- **Kali rolling breakage judgment** — rolling can introduce regressions; deciding when to pin or hold.
- **Backport decisions** for anything you carry out-of-distro (your kernel patches, custom packages) — no distro CVE stream covers those.

---

## One-line answer
For the MVP: **RAUC A/B images (custom Android-slot backend, verity bundles, static/GitHub hosting, HSM-signed) for atomic rootfs+kernel updates with rollback, plus apt/unattended-upgrades for in-place Debian/Kali security patching, plus systemd-sysext for optional tool layers** — keeping OSTree (with its native `aboot` backend) as the future path for single-rootfs devices where partition-free atomic rollback matters most.
