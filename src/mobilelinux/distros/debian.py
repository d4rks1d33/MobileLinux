"""Debian distribution backend.

Debian reuses the entire Debian-family pipeline; it differs from Kali only in
the recipe source and suite. It uses the Mobian recipes (the upstream Debian
mobile recipes that kali-nethunter-pro itself forks), which produce a Phosh
mobile Debian rootfs.

This is the second Debian-family distro and validates that the shared pipeline
generalizes beyond Kali. Its content lives in os-distros/debian/.
"""

from __future__ import annotations

from .debian_base import DebianBackend


class DebianBackendDistro(DebianBackend):
    name = "debian"
    family = "debian"
    # Mobian recipes: the upstream Debian mobile debos recipes.
    recipe_url = "https://salsa.debian.org/Mobian-team/mobian-recipes.git"
    suite = "trixie"
    _default_desktop = "phosh"
