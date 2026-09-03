<!--
Thanks for contributing to MobileLinux! PRs are open to everyone.
Fill in the sections that apply and tick the checklist. Delete what doesn't apply.
-->

## What does this PR do?

<!-- One or two sentences. -->

## Type of change

- [ ] New device port (adds `devices/<vendor>/<codename>/device.yaml`)
- [ ] Improvement to an existing device
- [ ] Framework / CLI / build-system change
- [ ] Distro / desktop / security layer
- [ ] OTA / release / security
- [ ] Documentation only
- [ ] Other:

---

## For a NEW DEVICE (or device changes)

**Device:** <!-- e.g. OnePlus 6 (oneplus-enchilada), Qualcomm SDM845 -->

**postmarketOS status of this device:**
- [ ] Already upstream in official pmaports, and I built/tested against it
- [ ] NOT upstream yet — the device definition points at my own fork/repo
      (`kernel.provider.source`, `upstreamed: false`)

**I actually tested this on real hardware:**
- [ ] Yes — it boots and works on my physical device
- [ ] No (please explain why, e.g. definition-only draft for review)

**What I verified on the device (tick what you tested):**
- [ ] Boots to the desktop
- [ ] Display / touch
- [ ] Wi-Fi
- [ ] Bluetooth
- [ ] Audio
- [ ] Modem / calls / SMS
- [ ] Battery / charging
- [ ] USB
- [ ] Other:

**Evidence (required if you claim `supported` for any feature):**
<!-- Link a screenshot/photo/log, or say which `mobilelinux test` checks passed.
     Hardware statuses must be evidence-based — never mark a feature `supported`
     without proof. Use partial/untested when unsure. -->

**Install strategy declared:** <!-- fastboot / fastbootd / rescue-dd / heimdall / sdcard / uuu / ... -->

---

## Checklist (required before merge to `main`)

- [ ] `mobilelinux validate <device>` passes (schema valid)
- [ ] `python ci/validate.py` passes (schema + strategy + flavor integrity)
- [ ] `pytest -q` passes
- [ ] Hardware statuses are **evidence-based** (no unproven `supported`)
- [ ] The install strategy matches how the device is really flashed
- [ ] No secrets / private keys / non-redistributable blobs committed
      (proprietary firmware stays out; declare it under `firmware.extract_from_device`)
- [ ] Docs updated if behavior changed
- [ ] I understand these images are provided **as-is, at the user's own risk**
      (see [DISCLAIMER.md](../DISCLAIMER.md))

## Anything else?

<!-- Known limitations, follow-ups, questions for reviewers. -->
