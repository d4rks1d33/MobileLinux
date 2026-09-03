# Contributing to MobileLinux

**PRs are open to everyone.** The single most valuable contribution is adding
support for **more devices** — and the best way to do that is to have the phone
and test on it. You don't need to be a kernel hacker: much of a port is a
declarative `device.yaml`.

## The quickest way to help

- **Have a phone that's already supported by postmarketOS?** Great — you can
  likely add a MobileLinux device definition for it. See the
  [porting guide](docs/porting-guide.md).
- **Ported a device to pmOS yourself (even if it's not upstream yet)?** You can
  point the device's `kernel.provider.source` at your own fork/repo
  (`upstreamed: false`) — no need to wait for it to be merged. The reference
  device (`rhodep`) works exactly this way.
- **Just have hardware and time to test?** Open a
  [New device issue](.github/ISSUE_TEMPLATE/new-device.yml) — testers are as
  useful as authors.

## Ground rules

1. **Evidence-based hardware status.** Never mark a feature `supported` without
   proof (a passing `mobilelinux test`, a photo, a log). Use `partial` /
   `untested` when unsure. This keeps the support numbers honest.
2. **Installation is per-device.** Declare how the device is really flashed
   (`fastboot`, `fastbootd`, `rescue-dd`, `heimdall`, `sdcard`, `uuu`, ...) — do
   not assume `fastboot flash userdata` works everywhere.
3. **No secrets or non-redistributable blobs.** Proprietary firmware stays out
   of git; declare it under `firmware.extract_from_device`. Never commit private
   keys.
4. **Don't duplicate work.** Kernel/patches/DTB are shared across distros via
   the provider + per-distro config *flavor* model; a distro is a config
   fragment on top, not a fork. See
   [kernel flavors & providers](docs/kernel-flavors-and-providers.md).

## Adding a device (short version)

```bash
# 1. import a draft from postmarketOS (or write from scratch)
mobilelinux import <path-to-pmaports-device-dir>

# 2. fill in the device.yaml (kernel provider/flavor, hardware statuses,
#    install strategy, tests) — see docs/porting-guide.md

# 3. validate
mobilelinux validate <codename>
mobilelinux check <codename>
python ci/validate.py
pytest -q

# 4. (optional) dry-run the build/flash to sanity-check
mobilelinux build <codename> --distro kali --dry-run
mobilelinux flash <codename> --dry-run
```

Then open a PR — the [PR template](.github/PULL_REQUEST_TEMPLATE.md) lists the
minimum things a change to `main` should satisfy.

## Checklist before your PR to `main`

- `mobilelinux validate <device>` passes
- `python ci/validate.py` passes
- `pytest -q` passes
- Hardware statuses are evidence-based
- The install strategy matches reality
- No secrets / private keys / non-redistributable blobs
- Docs updated if behavior changed

## Development setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

See the [architecture doc](docs/architecture.md) for how the pieces fit
together, and the [required tools](README.md#required-tools) for real builds.

## Code of conduct & safety

Be respectful and constructive. Remember these images flash real hardware and
are provided **as-is, at the user's own risk** (see [DISCLAIMER.md](DISCLAIMER.md)).
Security/pentest tooling shipped by some distros must only be used on systems
you own or are authorized to test.
