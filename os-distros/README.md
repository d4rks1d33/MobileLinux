# OS Distros

Each supported operating-system distribution lives here as a first-class,
swappable layer. A distro is built on top of the shared device support
(kernel/patches/DTB/firmware/device packages) — selecting a different distro
never touches hardware support.

| Distro | Base | Builder | Kernel flavor | Status |
|--------|------|---------|---------------|--------|
| [`postmarketos/`](postmarketos/) | Alpine | pmbootstrap | `pmos` | supported |
| [`kali/`](kali/) | Debian | debos | `kali` | supported |
| _debian/_ | Debian | debos | `debian` | planned |
| _ubuntu/_ | Debian | debos | `ubuntu` | planned |
| _arch/_ | Arch | pacman/PKGBUILD | (own) | planned |

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
