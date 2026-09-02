#!/bin/sh
# rhodep-wifi-drivers-build: compile the external USB Wi-Fi drivers on the
# device, against the kernel that actually booted. Run by
# rhodep-wifi-drivers-firstboot.service on first boot, and safe to run by hand
# after a kernel change.
#
# Why this exists as a boot-time step rather than a bake-in: the drivers are
# DKMS modules, and a loadable .ko must match the running kernel exactly (same
# build, not just same version string -- see "Building out-of-tree drivers" in
# the README). The debos chroot cannot produce a .ko for a kernel it is not
# running, so the build is deferred to the device.
#
# It marks itself done in a stamp file keyed to the kernel release, so it builds
# once per kernel and then gets out of the way. Delete the stamp (or change
# kernels) to make it run again.
set -e

KVER=$(uname -r)
STAGE=/usr/local/share/rhodep/wifi-drivers
STAMP="/var/lib/rhodep/wifi-drivers.$KVER.done"
log() { echo "rhodep-wifi-drivers-build: $*"; logger -t rhodep-wifi-drivers "$*" 2>/dev/null || true; }

mkdir -p /var/lib/rhodep

if [ -f "$STAMP" ]; then
	log "already built for $KVER (stamp $STAMP) -- nothing to do"
	exit 0
fi

# Headers must be present. If they are not, do not mark done -- leave the
# service enabled so it retries once they are installed.
if [ ! -e "/lib/modules/$KVER/build" ]; then
	# try a staged headers .deb first
	hdr=$(ls "$STAGE"/linux-headers-"$KVER"_*.deb /srv/linux-headers-"$KVER"_*.deb \
		2>/dev/null | head -1 || true)
	if [ -n "$hdr" ]; then
		log "installing staged kernel headers: $hdr"
		dpkg -i "$hdr" || apt-get -y --fix-broken install || true
	fi
fi
if [ ! -e "/lib/modules/$KVER/build" ]; then
	log "no kernel headers for $KVER -- cannot build yet. Install"
	log "  linux-headers-$KVER and this service will finish on the next boot,"
	log "  or run 'rhodep-wifi-drivers-build' by hand."
	exit 0   # exit 0 so the unit is not marked failed; it stays enabled
fi

# Build deps (idempotent; needs network the first time on a fresh image).
apt-get install -y build-essential bc bison flex libelf-dev libssl-dev dkms git \
	>/dev/null 2>&1 || true

rc=0

log "=== RTL8811AU / Archer T2U Nano (rtw88) ==="
if [ -d "$STAGE/rhodep-rtl8821au" ]; then
	sh "$STAGE/rhodep-rtl8821au/install.sh" || { rc=1; log "rtl8821au failed"; }
else
	log "rhodep-rtl8821au not staged -- skip"
fi

log "=== RTL8188EUS / TL-WN722N (8188eu) ==="
if [ -d "$STAGE/rhodep-rtl8188eus-fix" ]; then
	if apt-get install -y realtek-rtl8188eus-dkms >/dev/null 2>&1; then
		sh "$STAGE/rhodep-rtl8188eus-fix/apply-rtl8188eus-fix.sh" || rc=1
		SRCDIR=$(ls -d /usr/src/realtek-rtl8188eus-*/ 2>/dev/null | head -1)
		if [ -n "$SRCDIR" ]; then
			VER=$(basename "${SRCDIR%/}" | sed 's/^realtek-rtl8188eus-//')
			dkms build   -m realtek-rtl8188eus -v "$VER" -k "$KVER" --force || rc=1
			dkms install -m realtek-rtl8188eus -v "$VER" -k "$KVER" --force || rc=1
			echo "blacklist rtl8xxxu" > /etc/modprobe.d/blacklist-rtl8xxxu.conf
			echo "8188eu"             > /etc/modules-load.d/8188eu.conf
			apt-mark hold realtek-rtl8188eus-dkms "linux-headers-$KVER" 2>/dev/null || true
		else
			log "no rtl8188eus DKMS source dir"; rc=1
		fi
	else
		log "could not apt-install realtek-rtl8188eus-dkms (no network?) -- will retry next boot"
		exit 0   # leave enabled to retry when the network is up
	fi
else
	log "rhodep-rtl8188eus-fix not staged -- skip"
fi

depmod -a "$KVER" || true

if [ "$rc" -eq 0 ]; then
	: > "$STAMP"
	log "built both drivers for $KVER; disabling the first-boot service"
	systemctl disable rhodep-wifi-drivers-firstboot.service >/dev/null 2>&1 || true
	log "done. Plug a TP-Link adapter and 'otg on' -- it comes up as wlan1."
else
	log "finished with errors; leaving the service enabled to retry next boot"
fi
exit 0
