#!/bin/sh
# Installs everything userspace needs for sound on the Moto G82. Run as root.
#
# Works in two situations:
#
#   on the phone      the normal case, applies everything immediately
#   in a build chroot for baking the image, where there is no running systemd,
#                     no udev and no sound card. Detected automatically, so the
#                     rootfs build can just call this and get a device that
#                     comes up with working audio on first boot.
#
# The kernel side must already be in place. The AW88261 tuning blob is the one
# thing this cannot do for you: it is not redistributable, so it has to be
# pulled off the stock ROM on the device with scripts/extract-aw88261-acf.sh.
# Without it the amplifiers refuse to probe and there is no sound at all.
set -e

PROTECT=/usr/local/sbin/rhodep-protect-files
# Clear any immutability a previous run set (see userspace/apt: these files
# belong to no package, so the port protects them itself). Re-registered below.
if [ -x "$PROTECT" ]; then
	"$PROTECT" release \
		/usr/local/bin/rhodep-audio-out \
		/usr/local/bin/rhodep-audio-route.sh \
		/usr/local/bin/rhodep-jack-watch \
		/usr/local/bin/rhodep-wait-audio-card \
		/etc/systemd/system/rhodep-audio-route.service \
		/etc/systemd/system/rhodep-jack-watch.service \
		/etc/pipewire/pipewire.conf.d/51-rhodep-sink.conf \
		/etc/wireplumber/wireplumber.conf.d/51-rhodep-no-acp.conf \
		/etc/udev/rules.d/90-rhodep-wcd937x-jack.rules \
		/etc/systemd/user/pipewire.service.d/10-rhodep-wait-card.conf 2>/dev/null || true
fi


here=$(dirname "$0")

# /run/systemd/system only exists when systemd is actually running as PID 1,
# which is the standard way to tell a live system from a chroot.
if [ -d /run/systemd/system ]; then
	offline=no
else
	offline=yes
	echo "no running systemd: installing files only (build chroot mode)"
fi

install -D -m 0644 "$here/ucm2/conf.d/sm6375/Motorola-Moto-G82-5G.conf" \
	/usr/share/alsa/ucm2/conf.d/sm6375/Motorola-Moto-G82-5G.conf
install -D -m 0644 "$here/ucm2/conf.d/sm6375/Motorola-Moto-G82-5G-HiFi.conf" \
	/usr/share/alsa/ucm2/conf.d/sm6375/Motorola-Moto-G82-5G-HiFi.conf

install -D -m 0644 "$here/wireplumber/51-rhodep-no-acp.conf" \
	/etc/wireplumber/wireplumber.conf.d/51-rhodep-no-acp.conf
install -D -m 0644 "$here/pipewire/51-rhodep-sink.conf" \
	/etc/pipewire/pipewire.conf.d/51-rhodep-sink.conf

# PipeWire exits if it cannot open its statically declared sink, and on a cold
# boot it can beat the ADSP to it. Make it wait, and make a failure survivable.
install -D -m 0755 "$here/systemd/rhodep-wait-audio-card.sh" \
	/usr/local/bin/rhodep-wait-audio-card
install -D -m 0644 "$here/systemd/10-rhodep-wait-card.conf" \
	/etc/systemd/user/pipewire.service.d/10-rhodep-wait-card.conf

install -D -m 0755 "$here/systemd/rhodep-audio-route.sh" \
	/usr/local/bin/rhodep-audio-route.sh
install -D -m 0644 "$here/systemd/rhodep-audio-route.service" \
	/etc/systemd/system/rhodep-audio-route.service

install -D -m 0755 "$here/systemd/rhodep-audio-out.sh" \
	/usr/local/bin/rhodep-audio-out

# Headset jack: keep the codec awake so the MBHC block can detect at all, then
# follow the resulting input events and switch the output.
install -D -m 0644 "$here/udev/90-rhodep-wcd937x-jack.rules" \
	/etc/udev/rules.d/90-rhodep-wcd937x-jack.rules
install -D -m 0755 "$here/systemd/rhodep-jack-watch.py" \
	/usr/local/bin/rhodep-jack-watch
install -D -m 0644 "$here/systemd/rhodep-jack-watch.service" \
	/etc/systemd/system/rhodep-jack-watch.service

# Enabling is just symlinking, so it works in a chroot too and is what makes the
# baked image come up correctly without anyone logging in to finish the job.
systemctl enable rhodep-audio-route.service
systemctl enable rhodep-jack-watch.service

if [ "$offline" = yes ]; then
	echo "done; audio will come up on first boot"
	echo "remember: extract the AW88261 blob on the device, or there is no sound"
	exit 0
fi

udevadm control --reload-rules
udevadm trigger --subsystem-match=soundwire

systemctl daemon-reload

# Apply now so the current session does not have to wait for a reboot.
/usr/local/bin/rhodep-audio-route.sh
alsactl store
systemctl restart rhodep-jack-watch.service

echo "done; restart pipewire/wireplumber or reboot"

# Protect what we installed: nothing in dpkg owns it, so nothing else would
# ever put it back. rhodep-holds-enforce restores it if it goes missing.
if [ -x "$PROTECT" ]; then
	"$PROTECT" register audio 0755 \
		/usr/local/bin/rhodep-audio-out \
		/usr/local/bin/rhodep-audio-route.sh \
		/usr/local/bin/rhodep-jack-watch \
		/usr/local/bin/rhodep-wait-audio-card 2>/dev/null || true
	"$PROTECT" register audio-conf 0644 \
		/etc/systemd/system/rhodep-audio-route.service \
		/etc/systemd/system/rhodep-jack-watch.service \
		/etc/pipewire/pipewire.conf.d/51-rhodep-sink.conf \
		/etc/wireplumber/wireplumber.conf.d/51-rhodep-no-acp.conf \
		/etc/udev/rules.d/90-rhodep-wcd937x-jack.rules \
		/etc/systemd/user/pipewire.service.d/10-rhodep-wait-card.conf 2>/dev/null || true
	# The enabled state as well: the route unit is what enables a mixer path
	# before PipeWire opens the sink, and without it PipeWire gives up for good.
	"$PROTECT" register-units audio \
		rhodep-audio-route.service rhodep-jack-watch.service 2>/dev/null || true
fi
