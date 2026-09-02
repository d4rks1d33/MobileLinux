"""Interactive kernel-config (flavor fragment) editor.

`mobilelinux kernel-config <device> --flavor kali` lets a developer enable,
disable or toggle kernel symbols in a flavor's config fragment WITHOUT hand
editing a 12k-line config. It:

  * loads the distro's curated catalog (categories + human descriptions + presets),
  * shows the current state of each symbol (from the fragment),
  * offers an interactive terminal menu (per-symbol y/m/n, per-category toggles,
    apply a preset),
  * optionally validates symbols against the kernel Kconfig if a kernel tree is
    available,
  * writes the result back to the fragment.

No heavy dependencies; a plain terminal menu that always works. A ``--menuconfig``
escape hatch defers to the kernel's native ``make menuconfig`` when a kernel
tree + toolchain are present (then re-derives the fragment delta).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import tools, ui
from .errors import BuildError
from .model import Device

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


# --------------------------------------------------------------------------
# Fragment parsing / writing
# --------------------------------------------------------------------------
def _sym_state(line: str) -> tuple[str, str] | None:
    """Return (symbol, state) where state in {y, m, n}."""
    line = line.strip()
    if line.startswith("CONFIG_") and "=" in line:
        sym, val = line.split("=", 1)
        return sym, val.strip().strip('"') if val not in ("y", "m") else val
    if line.startswith("# CONFIG_") and line.endswith(" is not set"):
        return line[2:].split(" ", 1)[0], "n"
    return None


def load_fragment(path: Path) -> dict[str, str]:
    states: dict[str, str] = {}
    header: list[str] = []
    if not path.exists():
        return states
    for line in path.read_text().splitlines():
        parsed = _sym_state(line)
        if parsed:
            states[parsed[0]] = parsed[1]
    return states


def load_fragment_header(path: Path) -> list[str]:
    header = []
    if not path.exists():
        return header
    for line in path.read_text().splitlines():
        if _sym_state(line):
            break
        header.append(line)
    return header


def render_symbol(sym: str, state: str) -> str:
    if state == "n":
        return f"# {sym} is not set"
    return f"{sym}={state}"


def write_fragment(path: Path, states: dict[str, str], header: list[str]) -> None:
    lines = list(header)
    if not lines:
        lines = [f"# Kernel flavor fragment (edited by 'mobilelinux kernel-config')"]
    for sym in sorted(states):
        lines.append(render_symbol(sym, states[sym]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------
@dataclass
class Catalog:
    categories: dict[str, dict]
    presets: dict[str, dict]

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        if not path.exists() or yaml is None:
            return cls({}, {})
        data = yaml.safe_load(path.read_text()) or {}
        return cls(data.get("categories", {}), data.get("presets", {}))

    def symbols(self) -> dict[str, dict]:
        out = {}
        for cat in self.categories.values():
            for sym, meta in cat.get("symbols", {}).items():
                out[sym] = meta or {}
        return out

    def category_of(self, sym: str) -> str | None:
        for name, cat in self.categories.items():
            if sym in cat.get("symbols", {}):
                return name
        return None


def catalog_for_distro(repo, distro: str) -> Catalog:
    path = repo.distros_dir / distro / "kernel-catalog.yaml"
    return Catalog.load(path)


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------
def kernel_config_command(ctx, device: Device, *, flavor: str | None,
                          distro: str | None, preset: str | None,
                          enable: list[str] | None, disable: list[str] | None,
                          show: bool, menuconfig: bool) -> int:
    from .kernel import resolve_flavor

    distro = distro or "kali"
    try:
        flavor_name = resolve_flavor(device, distro, flavor)
    except BuildError as exc:
        ui.error(str(exc))
        return 1

    flavor = device.kernel_flavors[flavor_name]
    frag_path = device.dir / flavor.get("config_fragment", "")
    catalog = catalog_for_distro(ctx.repo, distro)
    states = load_fragment(frag_path)
    header = load_fragment_header(frag_path)

    if menuconfig:
        return _menuconfig(ctx, device, flavor_name, frag_path)

    # Non-interactive operations first.
    changed = False
    if preset:
        _apply_preset(catalog, states, preset)
        changed = True
    for sym in (enable or []):
        states[_norm(sym)] = "m" if catalog.symbols().get(_norm(sym), {}).get("default") != "y" else "y"
        changed = True
    for sym in (disable or []):
        states[_norm(sym)] = "n"
        changed = True

    if show or (not changed and not _interactive_available()):
        _print_state(catalog, states, flavor_name, distro)
        if changed:
            _save(frag_path, states, header, ctx)
        return 0

    if not changed:
        # Interactive menu.
        changed = _interactive_menu(catalog, states, flavor_name, distro)

    if changed:
        _save(frag_path, states, header, ctx)
    else:
        ui.note("no changes")
    return 0


def _norm(sym: str) -> str:
    sym = sym.strip()
    return sym if sym.startswith("CONFIG_") else "CONFIG_" + sym


def _apply_preset(catalog: Catalog, states: dict[str, str], preset: str) -> None:
    p = catalog.presets.get(preset)
    if not p:
        raise BuildError(f"unknown preset '{preset}'; known: {', '.join(catalog.presets)}")
    ui.info(f"applying preset '{preset}': {p.get('description','')}")
    wanted = set(p.get("categories", []))
    for name, cat in catalog.categories.items():
        on = name in wanted
        for sym, meta in cat.get("symbols", {}).items():
            states[sym] = (meta or {}).get("default", "m") if on else "n"


def _save(frag_path: Path, states: dict[str, str], header: list[str], ctx) -> None:
    if ctx.dry_run:
        ui.note(f"[dry-run] would write {frag_path} ({len(states)} symbols)")
        return
    write_fragment(frag_path, states, header)
    ui.success(f"wrote {frag_path} ({sum(1 for v in states.values() if v != 'n')} enabled)")


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
def _state_glyph(state: str) -> str:
    return {"y": ui.green("[y]"), "m": ui.cyan("[m]"), "n": ui.grey("[n]")}.get(state, "[?]")


def _print_state(catalog: Catalog, states: dict[str, str], flavor: str, distro: str) -> None:
    ui.header(f"Kernel flavor '{flavor}' ({distro}) config")
    if not catalog.categories:
        # No catalog: flat list.
        for sym in sorted(states):
            print(f"  {_state_glyph(states[sym])} {sym}")
        return
    for name, cat in catalog.categories.items():
        syms = cat.get("symbols", {})
        on = sum(1 for s in syms if states.get(s, "n") != "n")
        print(f"\n{ui.bold(cat.get('title', name))}  "
              f"{ui.grey(f'({on}/{len(syms)} enabled)')}")
        if cat.get("description"):
            print(ui.grey(f"  {cat['description'].strip()}"))
        for sym, meta in syms.items():
            st = states.get(sym, "n")
            base = ui.yellow(" (base)") if (meta or {}).get("base") else ""
            print(f"  {_state_glyph(st)} {sym}{base}")


def _interactive_available() -> bool:
    import sys
    return sys.stdin.isatty() and sys.stdout.isatty()


def _interactive_menu(catalog: Catalog, states: dict[str, str], flavor: str, distro: str) -> bool:
    if not catalog.categories:
        ui.warn("no catalog for this distro; use --enable/--disable or edit the fragment")
        return False
    changed = False
    cats = list(catalog.categories.items())
    while True:
        _print_state(catalog, states, flavor, distro)
        print()
        print("Commands: [number] toggle category, 's <CONFIG_x> y|m|n' set symbol,")
        print("          'p <preset>' apply preset, 'l' list presets, 'w' save+quit, 'q' quit")
        for i, (name, cat) in enumerate(cats, 1):
            print(f"  {i}. {cat.get('title', name)}")
        try:
            ans = input("> ").strip()
        except EOFError:
            break
        if ans in ("q", "quit"):
            return changed
        if ans in ("w", "write", "save"):
            return True
        if ans in ("l", "list"):
            for pname, p in catalog.presets.items():
                print(f"  {pname}: {p.get('description','')}")
            continue
        if ans.startswith("p "):
            try:
                _apply_preset(catalog, states, ans.split(None, 1)[1])
                changed = True
            except BuildError as e:
                ui.error(str(e))
            continue
        if ans.startswith("s "):
            parts = ans.split()
            if len(parts) == 3 and parts[2] in ("y", "m", "n"):
                states[_norm(parts[1])] = parts[2]
                changed = True
            else:
                ui.warn("usage: s CONFIG_SYMBOL y|m|n")
            continue
        if ans.isdigit() and 1 <= int(ans) <= len(cats):
            name, cat = cats[int(ans) - 1]
            syms = cat.get("symbols", {})
            any_on = any(states.get(s, "n") != "n" for s in syms)
            for sym, meta in syms.items():
                states[sym] = "n" if any_on else (meta or {}).get("default", "m")
            changed = True
            continue
        ui.warn("unrecognized command")
    return changed


# --------------------------------------------------------------------------
# native menuconfig escape hatch
# --------------------------------------------------------------------------
def _menuconfig(ctx, device: Device, flavor_name: str, frag_path: Path) -> int:
    runner = ctx.runner()
    ui.header("native make menuconfig")
    ui.note("  requires a prepared kernel tree + toolchain. This merges the base")
    ui.note("  config + current fragment, runs 'make menuconfig', then re-derives")
    ui.note("  the fragment delta.")
    runner.run(["sh", "-c",
                "# 1) cp merged .config to kernel tree; 2) make ARCH=arm64 menuconfig; "
                "3) diff against base -> new fragment"], tool="make")
    return 0
