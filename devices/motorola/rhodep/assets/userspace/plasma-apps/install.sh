#!/bin/sh
# Makes the Kali menu launchers work under Plasma Mobile. Run as root.
#
# Two things are broken out of the box on this image, and both hit tools that
# open in a terminal, which is most of the Kali menu:
#
#   1. Hundreds of Kali launchers (Caido, bettercap, wfuzz, ...) run
#      /usr/share/kali-menu/exec-in-shell, which belongs to the kali-menu
#      package, and kali-menu is not installed. The launcher fails with
#      "could not find the program /usr/share/kali-menu/exec-in-shell".
#      446 launchers were affected on the reference image.
#
#   2. The ones marked Terminal=true need a terminal KDE knows how to drive with
#      -e. Plasma Mobile ships gnome-terminal, whose wrapper no longer supports
#      -e, so KDE cannot launch into it and reports e.g. "terminal Konsole not
#      found". Konsole itself pulls a partial KF6 upgrade on a rolling system and
#      is not worth the risk; gnome-console (kgx) is already installed and does
#      support -e.
#
# kali-menu is a single package that pulls in nothing else, checked on the
# reference image, so installing it is a clean fix. Konsole is deliberately not
# installed.
set -e

if [ -d /run/systemd/system ] && command -v apt-get >/dev/null 2>&1; then
	if ! [ -x /usr/share/kali-menu/exec-in-shell ]; then
		apt-get install -y --no-install-recommends kali-menu
	fi
else
	echo "no apt available here; ensure kali-menu is installed in the rootfs" >&2
fi

# Point KDE's "run in terminal" at kgx, which understands -e. This is written to
# the system kdeglobals so it applies to every user, including a freshly built
# image where no user has a config yet.
mkdir -p /etc/xdg
kdeglobals=/etc/xdg/kdeglobals
if command -v kwriteconfig6 >/dev/null 2>&1; then
	kwriteconfig6 --file "$kdeglobals" --group General \
		--key TerminalApplication kgx
	kwriteconfig6 --file "$kdeglobals" --group General \
		--key TerminalService org.gnome.Console.desktop
else
	# No kwriteconfig in a chroot; write the keys directly. Only touch the
	# General group, appending if the file already exists without it.
	if [ -f "$kdeglobals" ] && grep -q '^\[General\]' "$kdeglobals"; then
		if ! grep -q '^TerminalApplication=' "$kdeglobals"; then
			sed -i '/^\[General\]/a TerminalApplication=kgx\nTerminalService=org.gnome.Console.desktop' "$kdeglobals"
		fi
	else
		{
			echo "[General]"
			echo "TerminalApplication=kgx"
			echo "TerminalService=org.gnome.Console.desktop"
		} >> "$kdeglobals"
	fi
fi

echo "Kali menu launchers fixed: kali-menu present, terminal set to kgx"
