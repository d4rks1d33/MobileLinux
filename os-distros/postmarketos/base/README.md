# postmarketOS base rootfs

- `rootfs.yaml` — records the pmbootstrap build steps. Unlike Kali, pmOS is
  Alpine-based: no debos, no apt package lists, no chroot integration phase.
  `pmbootstrap install` builds and images the rootfs directly, installing the
  kernel as an **apk** (no `linux-image` `.deb`).

UI packages come from the pmbootstrap UI selection
(`postmarketos-ui-<ui>`), so there is no per-distro package list here. Add
extras with `pmbootstrap config extra_packages`.

See [../../../docs/os-distros.md](../../../docs/os-distros.md).
