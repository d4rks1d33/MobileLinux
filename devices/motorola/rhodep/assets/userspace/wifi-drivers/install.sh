#!/bin/sh
# rhodep-wifi-drivers: install the external USB Wi-Fi adapter drivers that give
# monitor mode + packet injection (which the internal WCN3990 cannot do), so a
# freshly flashed image comes up with them instead of needing them built by hand.
#
# Two adapters are supported, two different drivers:
#
#   TP-Link TL-WN722N v2/v3 (RTL8188EUS)  -> realtek-rtl8188eus-dkms, patched
#                                            for Linux 7.2 (packages/rhodep-rtl8188eus-fix)
#   TP-Link Archer T2U Nano (RTL8811AU)   -> rtw88 (rtw_8821au), lwfinger's tree
#                                            (packages/rhodep-rtl8821au)
#
# Both are DKMS modules: they are COMPILED AGAINST THE RUNNING KERNEL, not
# shipped prebuilt, because a loadable .ko has to match the exact kernel it
# loads into (see "Building out-of-tree drivers" and "Updating the kernel" in
# the top-level README). That has one consequence this script is built around:
#
#   * DKMS can only build when the target kernel's headers are present AND the
#     build runs against the kernel that will actually run. In the debos build
#     chroot the running kernel is the build host's, not the phone's, so this
#     script does NOT compile there -- it stages the sources and arms a
#     first-boot service that compiles once, on the device, against the kernel
#     that actually booted.
#   * On a live system it builds immediately.
#
# Run as root. In the debos chroot:  sh install.sh        (stage + arm firstboot)
# On the device:                     sudo sh install.sh   (build now)
set -e

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO=$(CDPATH= cd -- "$here/.." 2>/dev/null && pwd)
STAGE=/usr/local/share/rhodep/wifi-drivers
KVER=${KVER:-$(uname -r)}

log() { echo "rhodep-wifi-drivers: $*"; }
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

# The driver packages live under packages/ at the repo root; find them whether
# this runs from the repo or from a staged copy under /srv or $STAGE.
find_pkg() {
	for base in "$REPO/.." "$REPO" "$here" "$STAGE" /srv; do
		[ -d "$base/packages/$1" ] && { echo "$base/packages/$1"; return 0; }
		[ -d "$base/$1" ] && { echo "$base/$1"; return 0; }
	done
	return 1
}

# DKMS needs /lib/modules/$KVER/build. The pmbootstrap apk does not ship it;
# this port builds a matching linux-headers-<KVER>.deb (scripts/build-kernel-
# headers.sh). Install it if a copy is staged next to this script or in /srv.
ensure_headers() {
	[ -e "/lib/modules/$KVER/build" ] && return 0
	hdr=$(ls "$here"/linux-headers-"$KVER"_*.deb /srv/linux-headers-"$KVER"_*.deb \
		"$STAGE"/linux-headers-"$KVER"_*.deb 2>/dev/null | head -1 || true)
	if [ -n "$hdr" ]; then
		log "installing kernel headers: $hdr"
		dpkg -i "$hdr" || apt-get -y --fix-broken install || true
	fi
	[ -e "/lib/modules/$KVER/build" ]
}

# rhodep-rtl8188eus-fix ships a patch script, not a full installer, so drive its
# documented sequence here (idempotent).
install_8188eus() {
	FIXDIR="$STAGE/rhodep-rtl8188eus-fix"
	[ -d "$FIXDIR" ] || { log "rhodep-rtl8188eus-fix not staged -- skipping"; return 0; }
	apt-get install -y realtek-rtl8188eus-dkms >/dev/null 2>&1 || {
		log "could not apt-install realtek-rtl8188eus-dkms (no network?)"; return 1; }
	sh "$FIXDIR/apply-rtl8188eus-fix.sh"
	SRCDIR=$(ls -d /usr/src/realtek-rtl8188eus-*/ 2>/dev/null | head -1)
	[ -n "$SRCDIR" ] || { log "no rtl8188eus DKMS source dir"; return 1; }
	VER=$(basename "${SRCDIR%/}" | sed 's/^realtek-rtl8188eus-//')
	dkms build   -m realtek-rtl8188eus -v "$VER" -k "$KVER" --force
	dkms install -m realtek-rtl8188eus -v "$VER" -k "$KVER" --force
	depmod -a "$KVER"
	# prefer 8188eu over the generic rtl8xxxu, and autoload it
	echo "blacklist rtl8xxxu" > /etc/modprobe.d/blacklist-rtl8xxxu.conf
	echo "8188eu"             > /etc/modules-load.d/8188eu.conf
	apt-mark hold realtek-rtl8188eus-dkms "linux-headers-$KVER" 2>/dev/null || true
	log "rtl8188eus built for $KVER"
}

install_8821au() {
	AUDIR="$STAGE/rhodep-rtl8821au"
	[ -d "$AUDIR" ] || { log "rhodep-rtl8821au not staged -- skipping"; return 0; }
	sh "$AUDIR/install.sh"
}

# Copy the firstboot unit + on-device builder into place and enable them, so
# the modules build (or rebuild, after a kernel change) on the device.
arm_firstboot() {
	install -m 0644 "$here/rhodep-wifi-drivers-firstboot.service" \
		/etc/systemd/system/rhodep-wifi-drivers-firstboot.service
	install -D -m 0755 "$here/build-on-device.sh" \
		/usr/local/sbin/rhodep-wifi-drivers-build
	if [ -d /run/systemd/system ]; then
		systemctl daemon-reload
		systemctl enable rhodep-wifi-drivers-firstboot.service >/dev/null 2>&1 || true
	else
		mkdir -p /etc/systemd/system/multi-user.target.wants
		ln -sf /etc/systemd/system/rhodep-wifi-drivers-firstboot.service \
			/etc/systemd/system/multi-user.target.wants/rhodep-wifi-drivers-firstboot.service
	fi
	log "armed rhodep-wifi-drivers-firstboot.service"
}

# --------------------------------------------------------------- stage sources
mkdir -p "$STAGE"
P8188=$(find_pkg rhodep-rtl8188eus-fix || true)
P8821=$(find_pkg rhodep-rtl8821au || true)
[ -n "$P8188" ] && rm -rf "$STAGE/rhodep-rtl8188eus-fix" && cp -a "$P8188" "$STAGE/rhodep-rtl8188eus-fix"
[ -n "$P8821" ] && rm -rf "$STAGE/rhodep-rtl8821au" && cp -a "$P8821" "$STAGE/rhodep-rtl8821au"
log "staged driver sources under $STAGE"

# --------------------------------------------------------------- chroot: defer
if [ ! -d /run/systemd/system ]; then
	log "no running systemd (chroot) -- staging only; first boot will build."
	arm_firstboot
	log "done (chroot). On the device's first boot the modules are compiled;"
	log "  after that wlan1 appears when either TP-Link adapter is plugged in."
	exit 0
fi

# --------------------------------------------------------------- live: build now
if ! ensure_headers; then
	log "ERROR: no kernel headers at /lib/modules/$KVER/build and none staged."
	log "       build one with scripts/build-kernel-headers.sh, install it, re-run."
	arm_firstboot   # a later headers install lets firstboot finish the job
	exit 1
fi

apt-get install -y build-essential bc bison flex libelf-dev libssl-dev dkms git \
	>/dev/null 2>&1 || true

rc=0
log "=== RTL8811AU / Archer T2U Nano (rtw88) ==="
install_8821au || { rc=1; log "rtl8821au install failed"; }

log "=== RTL8188EUS / TL-WN722N (rtl8188eus / 8188eu) ==="
install_8188eus || { rc=1; log "rtl8188eus install failed"; }

arm_firstboot   # so a future kernel change rebuilds both

if [ "$rc" -eq 0 ]; then
	log "done -- plug an adapter and 'otg on'; it comes up as wlan1."
else
	log "finished with errors -- see the log above."
fi
exit "$rc"
