#!/bin/sh
# rhodep-rtl8821au: driver for the TP-Link Archer T2U Nano (AC600), the
# RTL8811AU/8821AU dual-band USB adapter, with monitor mode + injection on the
# Kali NetHunter Pro rhodep port (Linux 7.2, aarch64). Run as root on the phone.
#
# Unlike the RTL8188EUS (single-band, uses the aircrack-ng rtl8188eus driver),
# this chip is served by the mac80211 driver rtw88. Kali's out-of-tree
# realtek-rtl88xxau-dkms does NOT build on 7.2 (dead kernel API: from_timer /
# del_timer_sync removed, and a broken include path), and aircrack-ng itself now
# marks that driver deprecated and points at rtw88. lwfinger/rtw88 compiles
# clean on 7.2 with no patches and, being mac80211, gives standard
# airmon-ng/monitor/inject with no flip-back quirks.
#
# The chip is USB 2357:011e, matched by the rtw_8821au module (rtw8821au.c).
set -e

VER=0.6
SRC=/usr/src/rtw88-$VER
REPO=https://github.com/lwfinger/rtw88

log() { echo "rhodep-rtl8821au: $*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

# --------------------------------------------------------------- kernel headers
# DKMS needs the headers for the running kernel. The pmbootstrap apk ships none,
# so this repo provides linux-headers-<KVER>.deb (see ../../kernel/ notes).
KVER=$(uname -r)
[ -e "/lib/modules/$KVER/build" ] || {
	log "ERROR: no kernel headers at /lib/modules/$KVER/build"
	log "       install linux-headers-$KVER first (see kernel/ build notes)"
	exit 1
}

# --------------------------------------------------------------- get the source
# Prefer a checkout vendored next to this script (offline installs); otherwise
# clone it. Either way it lands in /usr/src for DKMS.
here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
if [ ! -d "$SRC" ]; then
	if [ -d "$here/rtw88" ]; then
		log "using vendored source $here/rtw88"
		cp -a "$here/rtw88" "$SRC"
	else
		command -v git >/dev/null || { log "git not installed and no vendored source"; exit 1; }
		log "cloning $REPO"
		git clone --depth 1 "$REPO" "$SRC"
	fi
	rm -rf "$SRC/.git"
fi

# --------------------------------------------------------------- remove the
# broken Kali package if it is present, so its half-added DKMS entry cannot
# shadow this one.
if dpkg -l realtek-rtl88xxau-dkms 2>/dev/null | grep -q '^ii'; then
	log "removing the non-building realtek-rtl88xxau-dkms"
	apt-get remove -y realtek-rtl88xxau-dkms >/dev/null 2>&1 || true
	dkms remove realtek-rtl88xxau --all 2>/dev/null || true
fi

# --------------------------------------------------------------- dkms
if dkms status "rtw88/$VER" 2>/dev/null | grep -q installed; then
	log "rtw88/$VER already installed via DKMS"
else
	log "building rtw88/$VER (DKMS)"
	dkms add "rtw88/$VER" 2>/dev/null || true
	dkms build "rtw88/$VER" -k "$KVER"
	dkms install "rtw88/$VER" -k "$KVER" --force
fi
depmod -a "$KVER"

# --------------------------------------------------------------- LED off
# The adapter's LED is pointless on a phone and adds heat at the USB-C port.
# A udev rule switches it off whenever the LED class device appears.
install -m 0644 "$here/99-rhodep-rtw88-led-off.rules" \
	/etc/udev/rules.d/99-rhodep-rtw88-led-off.rules
udevadm control --reload-rules 2>/dev/null || true
# apply now if it is already present (udev only fires on the next add otherwise)
for led in /sys/class/leds/rtw88-*; do
	[ -e "$led" ] || continue
	echo none > "$led/trigger" 2>/dev/null || true
	echo 0    > "$led/brightness" 2>/dev/null || true
	log "LED $led -> off"
done

# --------------------------------------------------------------- protect (hold)
# rtw88 is a DKMS module, not an apt package, so there is nothing to apt-mark
# hold. Protect the pieces that matter with rhodep-protect-files (immutable +
# snapshot + auto-restore by rhodep-holds-enforce), the same layer the core
# port files use: the built .ko and the udev rule.
#
# CAUTION about the .ko: making it immutable freezes THIS build. That is what
# we want against accidental deletion, but it means a kernel change has to be a
# deliberate re-do -- DKMS cannot overwrite an immutable .ko. On a new kernel:
# `rhodep-protect-files release <ko>`, rebuild (dkms), then re-run this
# installer to re-register. This is fine because a new kernel already requires
# reapplying every out-of-tree patch this port carries; see the "Updating the
# kernel" note in the top-level README. Fails open if protect-files is absent.
if [ -x /usr/local/sbin/rhodep-protect-files ]; then
	KO=$(modinfo -n rtw_8821au 2>/dev/null || true)
	# shellcheck disable=SC2086
	/usr/local/sbin/rhodep-protect-files register rtl8821au 0644 \
		/etc/udev/rules.d/99-rhodep-rtw88-led-off.rules \
		${KO:+$KO} 2>/dev/null || true
	log "protected: udev rule${KO:+ + $KO} (immutable)"
	log "  note: the DKMS source at $SRC is rebuilt on kernel change; hold the"
	log "  headers (apt-mark hold linux-headers-$KVER) so DKMS can always build."
else
	log "rhodep-protect-files not installed; skipping immutable protection"
fi

# --------------------------------------------------------------- load + report
modprobe rtw_8821au 2>/dev/null || true
if [ -e /sys/class/net/wlan1 ] || iw dev 2>/dev/null | grep -q wlan1; then
	log "OK: wlan1 present"
	iw dev 2>/dev/null | awk '/Interface wlan1/{print "    "$0}'
else
	log "wlan1 not present yet -- plug the adapter in and run 'otg on'"
	log "  (the module autoloads on the USB modalias when it enumerates)"
fi

log "done. Put it in monitor with rhodep-pwn-monstart (or airmon-ng start wlan1)."
