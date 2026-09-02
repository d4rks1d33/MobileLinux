# Kali base rootfs

- `rootfs.yaml` — points the Kali backend at the upstream `kali-nethunter-pro`
  debos recipe and records the build parameters (suite, variant). The backend
  clones the recipe, injects the generated device config + device `.deb`s, and
  runs debos to produce the rootfs tarball, then the chroot integration phase.

The upstream recipe is **referenced, not vendored** (it is large and maintained
upstream). Pin `recipe.ref` to a commit SHA for reproducible release builds.

See [../../../docs/build-and-flash.md](../../../docs/build-and-flash.md) for the
full pipeline and [../../../docs/os-distros.md](../../../docs/os-distros.md).
