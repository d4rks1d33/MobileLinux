#!/bin/sh
# Sensors for motorola-rhodep. Run as root on the phone, or in the chroot.
#
# The accelerometer, gyroscope, magnetometer, proximity and ambient light
# sensors are not on any bus Linux owns: they hang off the SSC inside the ADSP,
# the IMU on I3C. Every SSC driver reads its bus, address, interrupt and
# calibration out of a registry that lives on the application processor, and
# the ADSP fetches that registry over FastRPC. Nothing on a mainline system
# answers, so the sensor core comes up with no sensors at all.
#
# This installs the missing half:
#   - Qualcomm's own FastRPC userspace (quic/fastrpc, BSD-3), built against the
#     mainline driver, providing the reverse-RPC listener and the apps_std file
#     server the DSP calls back into
#   - the registry tree itself, copied out of the stock vendor and persist
#     partitions
#   - a udev rule so iio-sensor-proxy probes the accelerometer and proximity
#     drivers it already has
#
# Read README.md before changing anything: a failure inside the sensors PD is a
# fatal error that restarts the whole ADSP and takes audio down with it.
set -e

here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)

# None of what this installs belongs to a package, so dpkg will never put any of
# it back. rhodep-protect-files gives it the immutable bit and a snapshot the
# enforcer restores from. Release before overwriting, register after.
PROTECT=/usr/local/sbin/rhodep-protect-files
if [ -x "$PROTECT" ]; then
	"$PROTECT" release \
		/usr/local/sbin/rhodep-ssc-populate \
		/usr/local/sbin/rhodep-ssc-links \
		/usr/local/sbin/rhodep-ssc-wait \
		/usr/local/sbin/rhodep-sscrpcd \
		/usr/local/bin/rhodep-autobrightness \
		/usr/local/lib/libadsprpc.so.1.0.0 \
		/usr/local/lib/libadsp_default_listener.so.1.0.0 \
		/etc/udev/rules.d/81-rhodep-ssc.rules \
		/etc/systemd/system/rhodep-sscrpcd.service \
		/etc/systemd/system/rhodep-ssc-populate.service \
		/etc/systemd/system/iio-sensor-proxy.service.d/10-rhodep-ssc.conf \
		/etc/systemd/user/rhodep-autobrightness.service \
		/usr/lib/systemd/system-sleep/rhodep-adsp \
		2>/dev/null || true
fi

# ------------------------------------------------------------- the FastRPC bits
# rhodep-sscrpcd, libadsprpc and libadsp_default_listener come from
# build-fastrpc.sh. It needs network and a compiler, so it is not run from here
# in a chroot; the image build should run it separately.
if [ ! -x /usr/local/sbin/rhodep-sscrpcd ]; then
	echo "rhodep-sensors: /usr/local/sbin/rhodep-sscrpcd is missing."
	echo "                run ./build-fastrpc.sh --install first."
	MISSING_DAEMON=1
fi

install -d /usr/local/sbin
install -m 0755 "$here/rhodep-ssc-populate" /usr/local/sbin/rhodep-ssc-populate
install -m 0755 "$here/rhodep-ssc-links"    /usr/local/sbin/rhodep-ssc-links
install -m 0755 "$here/rhodep-ssc-wait"     /usr/local/sbin/rhodep-ssc-wait
install -D -m 0755 "$here/rhodep-autobrightness" /usr/local/bin/rhodep-autobrightness

install -D -m 0644 "$here/udev/81-rhodep-ssc.rules" \
	/etc/udev/rules.d/81-rhodep-ssc.rules
install -D -m 0644 "$here/systemd/rhodep-sscrpcd.service" \
	/etc/systemd/system/rhodep-sscrpcd.service
install -D -m 0644 "$here/systemd/rhodep-ssc-populate.service" \
	/etc/systemd/system/rhodep-ssc-populate.service
install -D -m 0644 "$here/systemd/10-rhodep-ssc.conf" \
	/etc/systemd/system/iio-sensor-proxy.service.d/10-rhodep-ssc.conf
# A USER unit: the sensor claim is polkit-gated on an active seat session, and
# the brightness goes through PowerDevil on the session bus. It refuses to start
# unless XDG_CURRENT_DESKTOP=KDE, so enabling it cannot break a Phosh session.
install -D -m 0644 "$here/systemd/rhodep-autobrightness.service" \
	/etc/systemd/user/rhodep-autobrightness.service
# The suspend hook. Without it a suspend freezes this listener, the ADSP's
# sensors PD asserts for want of an answer, and the whole ADSP restarts --
# taking audio with it. See the comment at the top of the script.
install -D -m 0755 "$here/systemd/rhodep-adsp-sleep-hook" \
	/usr/lib/systemd/system-sleep/rhodep-adsp

install -D -m 0644 "$here/README.md" \
	/usr/share/doc/rhodep-sensors/README.md 2>/dev/null || true

# iio-sensor-proxy is the consumer. It is already installed on this image and
# already speaks SSC through libssc; it just never had any sensors to find.
if command -v apt-get >/dev/null 2>&1; then
	for p in iio-sensor-proxy libssc2; do
		dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'ok installed' \
			|| echo "rhodep-sensors: note: $p is not installed"
	done
fi

if [ -d /run/systemd/system ]; then
	udevadm control --reload >/dev/null 2>&1 || true
	udevadm trigger --subsystem-match=misc --action=change >/dev/null 2>&1 || true
	systemctl daemon-reload
	systemctl enable rhodep-ssc-populate.service rhodep-sscrpcd.service >/dev/null 2>&1

	if [ -z "$MISSING_DAEMON" ]; then
		systemctl start rhodep-ssc-populate.service || true
		systemctl restart rhodep-sscrpcd.service || true
		# iio-sensor-proxy asks the sensor core once and believes the answer,
		# so it has to be restarted after the registry is in.
		systemctl restart iio-sensor-proxy.service >/dev/null 2>&1 || true
	fi

	echo
	echo "rhodep-sensors: installed."
	echo "  check:  systemctl status rhodep-sscrpcd"
	echo "          monitor-sensor"
	echo
	# Ambient brightness is installed but NOT enabled. KWin 6.4 and later does
	# this natively -- libkwin.so carries ClaimLight/AutomaticBrightness/
	# LightLevel -- and it is the switch the Settings app actually drives, so
	# running both means two things fighting over the same control. PowerDevil
	# still has no ambient support (0 references), which is what the original
	# note here was about and is no longer the whole picture.
	# Enable it deliberately only if you want its step limiting; see the README.
	echo "  rhodep-autobrightness installed but left disabled: KWin does ambient"
	echo "  brightness natively now. Enable it only if you turn KWin's own"
	echo "  'Automatic brightness' off:"
	echo "      systemctl --user enable --now rhodep-autobrightness"
	echo

	echo "  NOTE: iio-sensor-proxy refcounts claims, and a client that claims"
	echo "  the accelerometer before the SSC device is discovered leaves it"
	echo "  claimed but never started. Restarting the proxy under a running"
	echo "  Phosh session hits that; a reboot does not. If the accelerometer"
	echo "  reads 'undefined' after a restart, reboot before believing it."
else
	echo "rhodep-sensors: no running systemd, files installed only"
	# systemctl enable is just symlinking, so do it by hand for the image build.
	install -d /etc/systemd/system/multi-user.target.wants
	ln -sf /etc/systemd/system/rhodep-sscrpcd.service \
		/etc/systemd/system/multi-user.target.wants/rhodep-sscrpcd.service
	ln -sf /etc/systemd/system/rhodep-ssc-populate.service \
		/etc/systemd/system/multi-user.target.wants/rhodep-ssc-populate.service
fi

if [ -x "$PROTECT" ]; then
	"$PROTECT" register sensors 0755 \
		/usr/local/sbin/rhodep-ssc-populate \
		/usr/local/sbin/rhodep-ssc-links \
		/usr/local/sbin/rhodep-ssc-wait \
		/usr/local/sbin/rhodep-sscrpcd \
		/usr/local/bin/rhodep-autobrightness \
		2>/dev/null || true
	# The FastRPC libraries are built here, not packaged, so nothing reinstalls
	# them either. Only the real files: the soname symlinks next to them are
	# recreated by rhodep-ssc-links, because restoring a symlink from a content
	# snapshot would replace it with a copy of the library.
	"$PROTECT" register sensors-lib 0755 \
		/usr/local/lib/libadsprpc.so.1.0.0 \
		/usr/local/lib/libadsp_default_listener.so.1.0.0 \
		2>/dev/null || true
	"$PROTECT" register sensors-conf 0644 \
		/etc/udev/rules.d/81-rhodep-ssc.rules \
		/etc/systemd/system/rhodep-sscrpcd.service \
		/etc/systemd/system/rhodep-ssc-populate.service \
		/etc/systemd/system/iio-sensor-proxy.service.d/10-rhodep-ssc.conf \
		/etc/systemd/user/rhodep-autobrightness.service \
		2>/dev/null || true
	"$PROTECT" register adsp-suspend 0755 \
		/usr/lib/systemd/system-sleep/rhodep-adsp \
		2>/dev/null || true
	# The enabled state too: chattr +i stops a unit file being deleted, and
	# `systemctl disable` still works. Losing rhodep-sscrpcd means no sensors
	# and an ADSP that asserts on the next suspend.
	"$PROTECT" register-units sensors \
		rhodep-sscrpcd.service rhodep-ssc-populate.service \
		2>/dev/null || true
fi

# The registry tree under /var/lib/rhodep-ssc is deliberately NOT protected:
# it is 250-odd files and the DSP writes calibration back into it, so the
# immutable bit would break the thing it is meant to defend. It heals itself
# instead -- delete it and rhodep-ssc-populate rebuilds it from the stock
# vendor and persist partitions on the next boot.

if command -v apt-mark >/dev/null 2>&1 && [ -f /usr/local/share/rhodep/apt-holds.txt ]; then
	# apply-holds.sh owns the list; this only catches the case where sensors
	# are installed after it has already run.
	for p in iio-sensor-proxy libssc2 libqrtr-glib0; do
		grep -qx "$p" /usr/local/share/rhodep/apt-holds.txt 2>/dev/null \
			|| echo "rhodep-sensors: note: $p is not in the hold list, re-run userspace/apt/apply-holds.sh"
	done
fi
