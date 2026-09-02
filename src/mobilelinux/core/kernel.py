"""Kernel build stage: shared device-support base + per-distro config flavor.

This models the reference port's real mechanic precisely:

* The kernel (source + patches + DTB + base config) is the shared device-support
  base, provided by pmOS (official pmaports, or the porter's own fork/repo when
  the device isn't upstream yet).
* Each distro selects a **flavor** = a small config fragment merged onto the base
  config. pmOS and Kali share 108 byte-identical patches; only the config differs.
* pmbootstrap builds whatever aport currently sits at
  ``pmaports/device/testing/linux-<codename>/`` — the "active aport swap". This
  module renders the flavor config, stages the aport, runs
  ``pmbootstrap checksum`` + ``build --force``, and verifies the flavor
  discriminator (e.g. ``CONFIG_RT2800USB`` present ⇒ Kali) so you always know
  which flavor compiled.
* For Debian-based distros (Kali/Debian/Ubuntu) the resulting apk is repackaged
  into a ``linux-image-<KVER>.deb`` consumed by debos.

Every command is planned unless ``--execute``; pmbootstrap and mkfs-like ops are
``dangerous`` (need ``--allow-dangerous``). Missing tools are reported.
"""

from __future__ import annotations

from pathlib import Path

from . import tools, ui
from .errors import BuildError
from .model import Device


def resolve_flavor(device: Device, distro: str, flavor: str | None) -> str:
    if flavor:
        if flavor not in device.kernel_flavors:
            raise BuildError(f"unknown kernel flavor '{flavor}' for {device.id}; "
                             f"known: {', '.join(device.kernel_flavors) or '(none)'}")
        return flavor
    fl = device.flavor_for_distro(distro)
    if fl:
        return fl
    # No flavor mapping: fall back to a single implicit flavor if there is one.
    if len(device.kernel_flavors) == 1:
        return next(iter(device.kernel_flavors))
    raise BuildError(f"no kernel flavor maps to distro '{distro}' for {device.id}; "
                     f"pass --flavor or add flavors[].distros")


def build_kernel(device: Device, distro: str, flavor_name: str, *,
                 out_dir: Path, runner: tools.Runner) -> Path | None:
    """Build the kernel apk for a flavor. Returns the apk path (or None in plan)."""
    prov = device.kernel_provider
    kbuild = device.kernel.get("build", {})
    linux_pkg = prov.get("linux_pkg", f"linux-{device.codename}")
    dev_dir = device.dir

    ui.header(f"Kernel: {device.kernel.get('type')} {device.kernel.get('version')} "
              f"[flavor={flavor_name}]")

    if kbuild.get("method") != "pmbootstrap":
        ui.note("  non-pmbootstrap kernel build; see device kernel.build.method")
        runner.run(["make", "ARCH=arm64", kbuild.get("image", "Image")], tool="make")
        return None

    # 0. Provider source (where the aport comes from).
    _report_provider(prov)

    # 1. Render the flavor config: base_config + flavor fragment -> merged config.
    flavor = device.kernel_flavors[flavor_name]
    base_config = dev_dir / device.kernel.get("base_config", "")
    fragment = dev_dir / flavor.get("config_fragment", "")
    merged = out_dir / f"config-{device.codename}-{flavor_name}.aarch64"
    _merge_config(base_config, fragment, merged, runner)

    # 2. Stage the active aport: pmaports/device/testing/linux-<codename>/
    #    with the shared APKBUILD + patches + the merged config for this flavor.
    pmaports = _pmaports_linux_dir(linux_pkg)
    aport_dir = dev_dir / prov.get("aport_dir", "assets/kernel/provider")
    patches_dir = dev_dir / device.kernel.get("patches_dir", "assets/kernel/patches")
    ui.info("  staging active aport (the copied-in aport is the one that builds):")
    runner.run(["sh", "-c", f"mkdir -p {pmaports}"])
    runner.run(["cp", str(aport_dir / "APKBUILD"), str(pmaports / "APKBUILD")])
    runner.run(["sh", "-c", f"cp {patches_dir}/*.patch {pmaports}/"])
    runner.run(["cp", str(merged), str(pmaports / f"config-{device.codename}.aarch64")])

    # 3. Verify the discriminator so the operator knows which flavor is active.
    _verify_discriminator(flavor, merged, runner)

    # 4. checksum (mandatory after any config/patch change) + build.
    runner.run(["pmbootstrap", "checksum", linux_pkg], tool="pmbootstrap", dangerous=True)
    runner.run(["pmbootstrap", "build", "--force", linux_pkg], tool="pmbootstrap", dangerous=True)

    apk = Path.home() / ".local/var/pmbootstrap/packages/edge/aarch64" / \
        f"{linux_pkg}-{device.kernel.get('version','').replace('-','_')}-r0.apk"
    ui.note(f"  apk expected at: {apk}")
    return apk if apk.exists() else None


def _report_provider(prov: dict) -> None:
    kind = prov.get("kind", "postmarketos")
    if prov.get("upstreamed"):
        ui.note(f"  provider: {kind} (upstreamed): {prov.get('pmaports_ref','')}")
    else:
        ui.note(f"  provider: {kind} (NOT upstreamed) source={prov.get('source','')}")
        ui.note(f"    (device not in official pmaports; using porter fork/repo)")


def _merge_config(base: Path, fragment: Path, out: Path, runner: tools.Runner) -> None:
    ui.info(f"  merging config: base + {fragment.name} -> {out.name}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if not base.exists():
        ui.warn(f"    base config not found: {base}")
    # Prefer the kernel's merge_config.sh when a kernel tree is present; otherwise
    # do a portable merge (fragment symbols override base) as a plan/real step.
    if tools.have("merge_config.sh"):
        runner.run(["merge_config.sh", "-m", str(base), str(fragment)], tool="merge_config.sh")
    else:
        # Portable Python merge (executed only with --execute; safe on files).
        if runner.execute and not runner.dry_run and base.exists() and fragment.exists():
            _python_merge(base, fragment, out)
            ui.note("    (merged with built-in merger; run 'make olddefconfig' in-tree later)")
        else:
            ui.info(f"  [plan] merge {base.name} + {fragment.name} -> {out.name} "
                    f"(fragment symbols override base), then make olddefconfig")


def _python_merge(base: Path, fragment: Path, out: Path) -> None:
    def symbol(line: str) -> str | None:
        line = line.strip()
        if line.startswith("CONFIG_") and "=" in line:
            return line.split("=", 1)[0]
        if line.startswith("# CONFIG_") and line.endswith(" is not set"):
            return line[2:].split(" ", 1)[0]
        return None

    frag_syms: dict[str, str] = {}
    for line in fragment.read_text().splitlines():
        s = symbol(line)
        if s:
            frag_syms[s] = line.rstrip()

    merged_lines = []
    seen = set()
    for line in base.read_text().splitlines():
        s = symbol(line)
        if s and s in frag_syms:
            merged_lines.append(frag_syms[s])
            seen.add(s)
        else:
            merged_lines.append(line.rstrip())
    for s, line in frag_syms.items():
        if s not in seen:
            merged_lines.append(line)
    out.write_text("\n".join(merged_lines) + "\n")


def _verify_discriminator(flavor: dict, config: Path, runner: tools.Runner) -> None:
    disc = flavor.get("discriminator")
    if not disc or not config.exists():
        return
    symbol = disc.get("symbol", "")
    want_present = disc.get("present", True)
    text = config.read_text() if config.exists() else ""
    present = any(l.startswith(symbol + "=") for l in text.splitlines())
    ok = (present == want_present)
    state = "present" if present else "absent"
    want = "present" if want_present else "absent"
    if ok:
        ui.success(f"  flavor discriminator OK: {symbol} {state} (expected {want})")
    else:
        ui.warn(f"  flavor discriminator MISMATCH: {symbol} {state}, expected {want} "
                f"(wrong flavor config staged?)")


def _pmaports_linux_dir(linux_pkg: str) -> Path:
    return Path.home() / ".local/var/pmbootstrap/cache_git/pmaports/device/testing" / linux_pkg


def build_linux_image_deb(device: Device, apk: Path | None, *,
                          out_dir: Path, runner: tools.Runner) -> Path | None:
    """Repackage the kernel apk into a Debian linux-image .deb for debos distros."""
    kver = _uname_r(device)
    dtb = device.device_tree.get("dtb", "")
    soc = device.soc_family
    deb = out_dir / f"linux-image-{kver}_{device.kernel.get('version','')}-{device.id}_arm64.deb"
    ui.header("linux-image .deb (for debos / Debian-based distros)")
    ui.note(f"  extract apk -> vmlinuz (flat Image) + dtb {dtb} + /lib/modules/{kver}")
    src = str(apk) if apk else "<kernel apk>"
    runner.run(["sh", "-c",
                f"# extract {src}, assemble linux-image tree (boot/vmlinuz-{kver}, "
                f"usr/lib/linux-image-{kver}/{_dtb_subdir(soc)}/{dtb}, lib/modules/{kver}), "
                f"then: dpkg-deb --build -Zxz PKG {deb.name}"], dangerous=True)
    return deb if deb.exists() else None


def _uname_r(device: Device) -> str:
    v = device.kernel.get("version", "")
    # 7.2-rc5 -> 7.2.0-rc5 style used by the reference port
    if "-rc" in v and v.count(".") == 1:
        base, rc = v.split("-", 1)
        return f"{base}.0-{rc}"
    return v


def _dtb_subdir(soc_family: str) -> str:
    return "qcom" if soc_family.startswith(("sm", "sdm", "msm", "qcom")) else ""


def kernel_command(ctx, device: Device, *, distro: str | None, flavor: str | None) -> int:
    """`mobilelinux kernel <device> [--distro d] [--flavor f]`."""
    runner = ctx.runner()
    distro = distro or "kali"
    try:
        flavor_name = resolve_flavor(device, distro, flavor)
    except BuildError as exc:
        ui.error(str(exc))
        return 1
    out_dir = ctx.repo.out_dir / device.id
    out_dir.mkdir(parents=True, exist_ok=True)

    apk = build_kernel(device, distro, flavor_name, out_dir=out_dir, runner=runner)
    if device.kernel.get("build", {}).get("deb_package"):
        build_linux_image_deb(device, apk, out_dir=out_dir, runner=runner)

    if runner.missing.any:
        runner.missing.report()
        return 2 if not ctx.dry_run else 0
    return 0
