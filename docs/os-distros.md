# OS distros

Distributions live under [`os-distros/`](../os-distros/). Each is a first-class,
swappable target built on top of the shared device support. Selecting a distro
never changes hardware support; it only changes the userland, the builder, and
the kernel **config flavor**.

## Supported today

### postmarketOS (`--distro postmarketos`, alias `pmos`)

- Base: Alpine. Builder: **pmbootstrap**. Kernel flavor: `pmos` (base config, no
  NetHunter delta).
- `pmbootstrap install` produces the rootfs image directly (it carries its own
  boot/root subpartitions); there is no debos/chroot phase and no `.deb`
  conversion — the kernel is installed as an **apk**.
- This is the clean, pentest-free baseline. It is also the **device-support
  provider** for every device (it supplies the shared kernel aport).

```bash
mobilelinux build rhodep --distro postmarketos
```

### Kali (`--distro kali`)

- Base: Debian. Builder: **debos**. Kernel flavor: `kali` (base +
  `kali.fragment`, the NetHunter delta).
- Because Kali is Debian-based, the kernel built by pmbootstrap is repackaged
  into a `linux-image-<KVER>.deb` and installed into the debos rootfs (see
  [kernel-flavors-and-providers.md](kernel-flavors-and-providers.md)).
- Ships the NetHunter tooling (see [`security/`](../security/)) when built with
  `--profile security`.

```bash
mobilelinux build rhodep --distro kali --desktop phosh --profile security
```

## The distro descriptor

Each distro has a `distro.yaml`:

```yaml
id: kali
base: debian
family: debian          # 'debian' distros consume a linux-image .deb kernel
package_manager: apt
builder: debos
suite: kali-rolling
default_desktop: phosh
kernel_flavor: kali     # which kernel flavor this distro uses
security:
  model: rolling
  update_command: [apt-get, -y, full-upgrade]
```

`family: debian` is what tells the build to produce a `.deb` kernel; Alpine/pmOS
does not. Arch-based distros (planned) build the kernel with `make`/PKGBUILD and
set `provider.kind: none` on the device.

## Adding a distro (future)

1. Create `os-distros/<id>/distro.yaml` + `base/` recipes.
2. Add a `DistroBackend` subclass in `src/mobilelinux/distros/` and register it.
3. Add a kernel **flavor** to the devices you want to support (usually just a
   config fragment; the kernel/patches/DTB are shared). For debian-based distros
   reuse the apk→.deb handoff; for others implement the appropriate packaging.
4. Optionally add a `kernel-catalog.yaml` so `kernel-config` can present its
   modules by category.

See [architecture.md](architecture.md) and
[kernel-flavors-and-providers.md](kernel-flavors-and-providers.md).
