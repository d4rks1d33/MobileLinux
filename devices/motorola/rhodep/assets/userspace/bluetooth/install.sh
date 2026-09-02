#!/bin/sh
# Bluetooth for motorola-rhodep. Run as root on the phone, or in the chroot.
#
# The controller and its firmware work out of the box: hci_qca loads
# qca/crbtfw21.tlv and qca/crnv21.bin and reports "QCA setup on UART is
# completed". Two things still needed doing.
#
# The service was found masked on this device, with a drop-in restricting
# bluetoothd to --noplugin=input,avdtp,a2dp,avrcp, which reads like it was
# turned off while the audio stack was being debugged and never turned back
# on. Unmasked and verified here: bluetoothd starts, hci0 goes UP RUNNING
# PSCAN, a scan finds devices, and the audio card and its PCMs are unchanged
# afterwards.
#
# And the address was random, see rhodep-bt-address.
set -e

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
install -d /usr/local/sbin

PROTECT=/usr/local/sbin/rhodep-protect-files
if [ -x "$PROTECT" ]; then
	"$PROTECT" release /usr/local/sbin/rhodep-bt-address \
		/etc/systemd/system/rhodep-bt-address.service 2>/dev/null || true
fi

# The drop-in that ran bluetoothd with --noplugin=input,avdtp,a2dp,avrcp, i.e.
# with Bluetooth audio and Bluetooth input turned off. Verified on the device
# with real earbuds that it is not needed: with all plugins loaded the phone's
# own speaker and its PCM list are unchanged, and the earbuds play in stereo
# over A2DP/SBC. Kept as a backup rather than deleted outright.
if [ -e /etc/systemd/system/bluetooth.service.d/noplugin.conf ]; then
	mv /etc/systemd/system/bluetooth.service.d/noplugin.conf \
	   /etc/systemd/system/bluetooth.service.d/noplugin.conf.disabled-by-rhodep
	echo "disabled the --noplugin drop-in; Bluetooth audio and input are now on"
fi

install -D -m 0755 "$here/rhodep-bt-address" /usr/local/sbin/rhodep-bt-address
install -D -m 0644 "$here/systemd/rhodep-bt-address.service" \
	/etc/systemd/system/rhodep-bt-address.service
install -D -m 0644 "$here/README.md" /usr/share/doc/rhodep-bluetooth/README.md 2>/dev/null || true

# NOTE: pairings do NOT survive the address change, and cannot be made to.
# See README.md: the link key is negotiated against the adapter address, so
# copying /var/lib/bluetooth/<old>/<device>/ across only produces a record that
# fails with br-connection-key-missing. Anything already paired has to be
# paired again, once. This was tried and measured, not assumed.

if [ -d /run/systemd/system ]; then
	systemctl unmask bluetooth >/dev/null 2>&1 || true
	systemctl daemon-reload
	systemctl enable rhodep-bt-address.service >/dev/null 2>&1
	systemctl enable bluetooth >/dev/null 2>&1 || true
	systemctl start rhodep-bt-address.service >/dev/null 2>&1 || true
	systemctl restart bluetooth >/dev/null 2>&1 || true
	# WirePlumber builds its Bluetooth device list when it starts. If it came up
	# while bluetooth.service was masked - which is exactly what happens the
	# first time this script is run on a running system - it has no bluez
	# monitor and a headset will pair, connect, and then be dropped by the
	# remote end a few seconds later because nothing offered it an audio
	# endpoint. Restarting the user's audio stack is what makes that work
	# without a reboot. Harmless if there is no session.
	for uid in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $2}' | sort -u); do
		user=$(id -nu "$uid" 2>/dev/null) || continue
		systemctl --user -M "$user@" restart wireplumber pipewire pipewire-pulse >/dev/null 2>&1 || true
	done
	echo "bluetooth: $(systemctl is-active bluetooth), address $(hciconfig hci0 2>/dev/null | sed -n 's/.*BD Address: \([0-9A-F:]*\).*/\1/p')"
else
	rm -f /etc/systemd/system/bluetooth.service   # the mask, if the image carries one
	install -d /etc/systemd/system/bluetooth.service.wants
	ln -sf /etc/systemd/system/rhodep-bt-address.service \
		/etc/systemd/system/bluetooth.service.wants/rhodep-bt-address.service
	echo "bluetooth staged (no running systemd)"
fi

if [ -x "$PROTECT" ]; then
	"$PROTECT" register bluetooth 0755 /usr/local/sbin/rhodep-bt-address 2>/dev/null || true
	"$PROTECT" register bluetooth-conf 0644 \
		/etc/systemd/system/rhodep-bt-address.service 2>/dev/null || true
	# bluetooth.service is included because this port found it masked: the
	# enabled state is the whole point of that half of the fix.
	"$PROTECT" register-units bluetooth \
		rhodep-bt-address.service bluetooth.service 2>/dev/null || true
fi
