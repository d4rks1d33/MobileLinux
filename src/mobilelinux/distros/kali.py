"""Kali (NetHunter Pro) distribution backend.

Kali is a Debian-family distro, so it inherits the whole rootfs pipeline from
:class:`DebianBackend` and only sets the upstream recipe + suite + the security
profile. This mirrors the reference port: the kernel is built once (shared pmOS
aport, kali flavor) and handed over as a linux-image .deb; the rootfs is the
kali-nethunter-pro debos recipe finished with a chroot integration phase.
"""

from __future__ import annotations

from .debian_base import DebianBackend


class KaliBackend(DebianBackend):
    name = "kali"
    family = "debian"
    recipe_url = "https://gitlab.com/kalilinux/nethunter/build-scripts/kali-nethunter-pro"
    suite = "kali-rolling"
    _default_desktop = "phosh"
