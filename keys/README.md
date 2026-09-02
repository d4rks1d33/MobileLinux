# Signing keys

**Private keys are never committed.** `.gitignore` excludes `*.key`/`*.pem`;
only this README is tracked here.

- Generate a keypair: `mobilelinux keygen --channel <dev|beta|stable>`
- `keys/<channel>.ed25519.key` — **private**, keep offline (HSM / air-gapped for
  release channels). Mode 0600.
- `keys/<channel>.ed25519.pub` — public; ships on the device at
  `/etc/mobilelinux/keys/<channel>.ed25519.pub` and is referenced by the OTA
  state's `public_key`.

**Dev vs release:** use a throwaway `dev` key for local testing. `beta`/`stable`
keys are release keys — losing or leaking one requires rotation (publish a new
public key via a signed transition and re-sign). See `docs/signing.md` for
generation, storage, rotation, revocation and recovery procedures.
