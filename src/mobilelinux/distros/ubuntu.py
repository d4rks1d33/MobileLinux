"""Ubuntu distribution backend.

Ubuntu is Debian-family, so it inherits the shared pipeline. A plain mainline
Ubuntu mobile rootfs fits the debos flow (Ubuntu Touch's own system-image OTA
model may be integrated separately later). Content lives in os-distros/ubuntu/.
"""

from __future__ import annotations

from .debian_base import DebianBackend


class UbuntuBackend(DebianBackend):
    name = "ubuntu"
    family = "debian"
    # Reuse the Mobian-style recipe by default; can be pointed at an
    # Ubuntu-Touch/mainline recipe when one is standardized.
    recipe_url = "https://salsa.debian.org/Mobian-team/mobian-recipes.git"
    suite = "noble"
    _default_desktop = "phosh"
