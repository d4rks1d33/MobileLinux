# OS Distros

Each supported operating-system distribution lives here as a first-class,
swappable layer. A distro is built on top of the shared device support
(kernel/patches/DTB/firmware/device packages) — selecting a different distro
never touches hardware support.

| Distro | Base | Builder | Kernel flavor | Status |
|--------|------|---------|---------------|--------|
| [`postmarketos/`](postmarketos/) | Alpine | pmbootstrap | `pmos` | **supported** |
| [`kali/`](kali/) | Debian | debos | `kali` | **supported** |
| [`debian/`](debian/) | Debian | debos | `debian` | experimental |
| [`ubuntu/`](ubuntu/) | Debian | debos | `ubuntu` | experimental |
| [`arch/`](arch/) | Arch | pacstrap/PKGBUILD | (own) | planned |

**Debian-family distros share one pipeline.** Kali, Debian and Ubuntu all
subclass a single `DebianBackend` (`src/mobilelinux/distros/debian_base.py`) and
differ only in the recipe URL + suite + package lists. Adding a new Debian-based
distro (e.g. **Pop!_OS** and other Ubuntu/Debian derivatives) is a ~10-line
subclass plus an `os-distros/<id>/` directory. Arch-based distros
(**EndeavourOS / Manjaro-ARM**, …) need their own kernel packaging
(`provider.kind: none`) and a pacstrap builder, so they are a bigger step.

`debian`/`ubuntu` are **experimental**: the backend exists and plans correctly,
but the end-to-end build has not been verified yet. `kali` is the proven one.

Each distro directory contains:

- `distro.yaml` — the distro descriptor (base, package manager, builder, suite,
  default desktop, security model, kernel flavor).
- `base/` — recipes / package lists / configuration for the rootfs.
- (Kali) `kernel-catalog.yaml` — the curated catalog of kernel modules
  (categories + descriptions + presets) used by
  `mobilelinux kernel-config <device> --flavor kali`.

## postmarketOS is special

postmarketOS is both:

1. the **device-support provider** — it supplies the shared kernel aport that
   every distro's kernel is built from (see
   [../docs/kernel-flavors-and-providers.md](../docs/kernel-flavors-and-providers.md)), and
2. a **build target** in its own right — a clean Alpine + Phosh image built with
   pmbootstrap, the pentest-free baseline.

Kali is that same kernel work plus the NetHunter config flavor and a Debian
userland. The relationship, verified in the reference port: pmOS and Kali share
108 byte-identical kernel patches and differ only in the `.config`.

## Building a distro

```bash
mobilelinux build rhodep --distro postmarketos   # Alpine + Phosh (pmos flavor)
mobilelinux build rhodep --distro kali           # Kali + Phosh (kali flavor, debos)
```

See [../docs/build.md](../docs/build.md) and
[../docs/os-distros.md](../docs/os-distros.md).
