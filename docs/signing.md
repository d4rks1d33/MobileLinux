# Release Signing

This document explains **why** MobileLinux signs releases, **what** exactly is
signed, and **how** to manage the keys. If you have never worked with signing
before, read the concepts first; the commands and operational rules follow.

Related docs:
[ota.md](ota.md) ·
[release-process.md](release-process.md) ·
[security-updates.md](security-updates.md)

Source: [ota/signing.py](../src/mobilelinux/ota/signing.py),
[ota/manifest.py](../src/mobilelinux/ota/manifest.py),
keygen in [cli/commands.py](../src/mobilelinux/cli/commands.py) lines 191–205.

---

## 1. Why sign at all?

The MobileLinux OTA server is just a **static file host** — GitHub Releases, a
plain HTTP server, an object store ([ota.md](ota.md#12-the-ota-server-is-just-static-files)).
That means **anyone** could put a file that *looks* like a manifest at a URL the
device might fetch. Nothing about the transport proves who created the update.

A **digital signature** solves this. The maintainer holds a **private key** and
uses it to sign each release. The device holds only the matching **public key**
and uses it to verify. A signature that verifies proves two things:

1. **Authenticity** — the release was signed by the holder of the private key
   (us), not an attacker.
2. **Integrity** — not one byte of what was signed has changed since signing.

The device **refuses any unsigned or badly-signed manifest**
([ota/client.py](../src/mobilelinux/ota/client.py) `_verify_signature`,
lines 94–108). Signing is therefore the single root of trust for the entire OTA
system.

---

## 2. What exactly is signed

Not the whole manifest — the **canonical manifest body**: every field *except*
the `signature` field itself. This is computed by `canonical_body()` in
[ota/manifest.py](../src/mobilelinux/ota/manifest.py) (lines 15–18):

```python
def canonical_body(manifest):
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

Two important properties:

- **The `signature` field is excluded.** You cannot sign a document that already
  contains its own signature (chicken-and-egg). So the signer removes it, signs
  the rest, and then attaches the signature.
- **The body is canonicalized** — keys are sorted and whitespace is minimized. JSON
  allows the same data to be written many ways (different key orders, spacing).
  If the signer and verifier disagreed on the exact bytes, verification would
  fail even for a legitimate release. Canonicalization guarantees both sides hash
  **the same bytes**, so signing and verification agree byte-for-byte.

Because the signature covers the whole body, it protects **everything** that
matters: the device id, architecture, minimum version, every artifact URL and
**hash**, and the security metadata. An attacker cannot swap an artifact hash or
retarget the device without invalidating the signature.

---

## 3. The algorithm: ed25519

MobileLinux signs with **ed25519** (an elliptic-curve signature scheme). It was
chosen because it is small, fast, and has **no parameter pitfalls** — unlike RSA
you cannot accidentally pick a weak key size or padding. See the module docstring
in [ota/signing.py](../src/mobilelinux/ota/signing.py) lines 1–13.

The schema also permits `rsa-pss-sha256` and `ecdsa-p256-sha256`
([manifest.schema.json](../schema/manifest.schema.json) line 68), but the
implementation signs and verifies **ed25519** today.

Two backends are used transparently
([ota/signing.py](../src/mobilelinux/ota/signing.py)):

- the **`cryptography`** Python library (preferred; allows pure-Python verification
  on the phone),
- the **`openssl`** CLI as a fallback.

---

## 4. Generating keys

```
mobilelinux keygen --channel <dev|beta|stable> [--key-id <id>]
```

- `--channel` — which channel the key is for; defaults to `dev`.
- `--key-id` — an optional identifier stored with the key (defaults to the channel
  name). The `key_id` also lands in the manifest's `signature.key_id`, which is
  what makes **rotation** possible (see below).

This calls `generate_keypair()`
([ota/signing.py](../src/mobilelinux/ota/signing.py) lines 39–69), which:

- writes the private key to `keys/<channel>.ed25519.key`,
- writes the public key to `keys/<channel>.ed25519.pub`,
- **refuses to overwrite an existing private key** (so you cannot clobber a key by
  accident),
- `chmod 0600` on the private key.

When the channel is `stable` or `beta`, `cmd_keygen`
([cli/commands.py](../src/mobilelinux/cli/commands.py) lines 202–204) prints a
loud warning that this is a **RELEASE** key: keep it offline, never commit it,
and ship only the `.pub` to devices.

---

## 5. Key storage rules

These rules are the difference between a secure OTA channel and a compromised
one. Follow them exactly.

- **`keys/` is git-ignored.** Private keys must never enter version control. The
  release pipeline never embeds a private key
  ([release.py](../src/mobilelinux/ota/release.py) docstring, lines 11–13).
- **Development keys** (`dev`) are self-generated and used only for local testing.
  They may live on your workstation.
- **Release keys** (`stable`, `beta`) are kept **offline** — an air-gapped machine
  or an **HSM** (hardware security module). The signing of a real release should
  happen on that machine.
- **Only the public key ships on the device**, at
  `/etc/mobilelinux/keys/<channel>.ed25519.pub`. The device's
  `state.public_key` points at it (default in
  [ota/state.py](../src/mobilelinux/ota/state.py) line 26). The private key
  **never** touches the phone.

### Dev vs release key separation

Keeping `dev` and `stable`/`beta` keys separate means a leaked development key
cannot be used to sign something a production device will trust. Production
devices carry only the release public key(s), so only the release private key —
kept offline — can produce updates they accept.

> **Never commit a private release key.** If you do, treat it as compromised and
> follow [§8 Recovery](#8-recovery-if-a-key-is-compromised) immediately.

---

## 6. Rotation

**Rotation** is replacing a signing key on a schedule (or after suspicion of
compromise) *before* anything goes wrong. Because each manifest records which key
signed it (`signature.key_id`,
[manifest.schema.json](../schema/manifest.schema.json) line 69), you can migrate
cleanly:

1. Generate a **new** keypair for the channel (a new `key_id`).
2. **Publish the new public key** so devices can be updated to trust it (ship it
   in the next release image / via configuration).
3. Once devices trust the new key, **re-sign** releases with the new private key.
4. Retire the old private key.

The `key_id` in the signature lets a device (in a future multi-key setup) select
the right public key to verify against during the transition window.

---

## 7. Revocation

**Revocation** is declaring an existing key *no longer trusted*. Where rotation is
proactive, revocation is reactive — you use it when a key must stop being honored
(e.g. it was exposed). Operationally, revocation means:

1. Remove the compromised public key from devices (ship an updated key set).
2. Ensure no new release is ever signed with the revoked key.
3. Publish a new trusted key and re-sign the current release with it.

Until a device has dropped the revoked key from its trust set, it would still
accept anything signed by it — which is why revocation must be paired with getting
the new public key onto devices as fast as possible.

---

## 8. Recovery if a key is compromised

If a **private release key** is exposed (committed, leaked, stolen), assume an
attacker can sign updates your devices will accept. Act immediately:

1. **Stop** publishing anything signed with the compromised key.
2. **Generate a new keypair** on a clean, offline/HSM machine
   (`mobilelinux keygen --channel <channel>` on that machine).
3. **Distribute the new public key** to devices as fast as possible — this is the
   slow, hard part, because a static-hosted OTA has no push channel. The new key
   must reach devices before they can be protected, which is why the private key
   must never be exposed in the first place.
4. **Re-sign** the current, known-good release with the new key and publish it.
5. **Revoke** the old key (drop it from device trust; never sign with it again).
6. **Audit** what was published while the key was exposed, and consider whether a
   forced re-flash is warranted for at-risk devices.

The best recovery is prevention: keep release private keys **offline/HSM**, never
in git, and separate from dev keys.

---

## 9. How verification runs on the device

For completeness, the device side ([ota/signing.py](../src/mobilelinux/ota/signing.py)
`verify`, lines 92–114, called from
[ota/client.py](../src/mobilelinux/ota/client.py) line 103):

1. The client fetches the manifest and recomputes the **canonical body**
   (`manifest.canonical()`).
2. It base64-decodes `signature.value` and verifies it against the body using the
   ed25519 **public key** at `state.public_key`.
3. On success it prints `signature verified`; on any failure it refuses the update.

This is the check that turns "a file on a static host" into "an update this device
is willing to install." See [ota.md](ota.md#10-how-the-client-refuses-a-bad-or-wrong-image).
