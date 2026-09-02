#!/bin/sh
# Reapplies the apt holds this port depends on. Run as root on the phone.
#
# Nothing here is owned by a package, so apt cannot delete our files; dpkg only
# removes what is in its own manifest. The risk these holds address is
# behavioural, not deletion:
#
#   alsa-ucm-conf   the UCM lookup path and syntax our config relies on
#   alsa-utils      alsa-restore, the fallback that puts the route back at boot
#   pipewire/spa    a major version could change the conf.d rule format; if our
#                   rule stops applying, ACP comes back, and because the PCMs
#                   cannot be opened without a route, PipeWire fails to start
#                   and the machine has no audio at all
#   wireplumber     same reasoning
#   gstreamer1.0-pipewire
#                   the only route from GStreamer to PipeWire. Most recording
#                   applications, the GNOME sound recorder included, capture
#                   through GStreamer, so without this plugin the microphone is
#                   fine at every level this port controls and every app still
#                   records silence
#   kali-menu       owns /usr/share/kali-menu/exec-in-shell, which hundreds of
#                   Kali menu launchers call; without it they fail with "could
#                   not find the program". See userspace/plasma-apps.
#   libspa-0.2-bluetooth
#                   the PipeWire plugin that actually carries Bluetooth audio.
#                   pipewire is pinned at 1.6.8 above, and this ships a plugin
#                   built against that exact SPA ABI, so letting it move on its
#                   own is the most likely way to end up with earbuds that pair
#                   and connect and then play nothing
#   bluez           bluetoothd, bluetoothctl and btmgmt. btmgmt is what
#                   rhodep-bt-address uses to program the controller's real
#                   address at every boot; without it the phone goes back to a
#                   random one per boot and every pairing dies. Note this one
#                   is a network-facing daemon with a CVE history, so it is the
#                   hold most worth lifting deliberately when an update matters:
#                   rhodep-hold-override release bluez "<reason>"
#   libqrtr-dev     needed to rebuild the memshare responder in userspace/modem;
#                   losing it does not stop the installed binary from running,
#                   but it does stop anyone from rebuilding it
#   squeekboard     the on screen keyboard once KWin is pointed at it, and the
#                   only one here with a terminal layout; losing it on a phone
#                   means losing text input entirely
#   plasma-keyboard kept installed purely as the fallback to switch back to
#   neard           the NCI daemon the NFC reader drives the S3NRN4V through;
#                   not installed by default and the whole userspace/nfc path
#                   depends on it. See nice-to-have 10 and userspace/nfc.
#   wlr-randr       the Wayland output tool the NFC bring-up leaned on; held
#                   alongside neard so the NFC work does not regress on an
#                   upgrade. Both are behavioural holds, not deletion.
#
# The modem, wifi, kernel and firmware holds predate audio and are kept.
#
# Deliberately NOT held: python3, which rhodep-jack-watch needs. Pinning the
# interpreter of a rolling distribution to keep one small script working would
# block security updates and hold back half the system, which is a much worse
# trade than the script breaking loudly and visibly.
set -e
here=$(dirname "$0")
xargs -r apt-mark hold < "$here/apt-holds.txt"

# Anything below may be replacing files that a previous run made immutable, so
# clear that first. Harmless if they are not.
if [ -x /usr/local/sbin/rhodep-protect-files ]; then
	/usr/local/sbin/rhodep-protect-files release \
		/usr/local/sbin/rhodep-apt-guard \
		/usr/local/sbin/rhodep-holds-enforce \
		/usr/local/sbin/rhodep-hold-override \
		/usr/local/sbin/rhodep-dpkg-protect \
		/usr/local/sbin/rhodep-protect-files \
		/etc/apt/apt.conf.d/50rhodep-protect-holds \
		/etc/apt/apt.conf.d/60rhodep-enforce-holds \
		/etc/systemd/system/rhodep-holds-enforce.service \
		/etc/systemd/system/rhodep-holds-enforce.timer \
		/usr/local/share/rhodep/apt-holds.txt 2>/dev/null || true
else
	for f in /usr/local/sbin/rhodep-apt-guard \
		 /etc/apt/apt.conf.d/50rhodep-protect-holds \
		 /usr/local/share/rhodep/apt-holds.txt; do
		[ -e "$f" ] && chattr -i "$f" 2>/dev/null
	done
fi

# A hold does not stop `apt purge`. Install the hook that does; see
# rhodep-apt-guard for the reasoning and how to override it deliberately.
install -D -m 0755 "$here/rhodep-apt-guard" /usr/local/sbin/rhodep-apt-guard
install -D -m 0644 "$here/50rhodep-protect-holds" \
	/etc/apt/apt.conf.d/50rhodep-protect-holds

# Nothing protects the holds themselves: one apt-mark unhold and the port is
# defenceless, with no record. rhodep-holds-enforce puts back any hold, and any
# of the guard's own files, that goes missing, and says so in the journal. The
# canonical copies live where the enforcer can find them.
install -D -m 0644 "$here/apt-holds.txt"          /usr/local/share/rhodep/apt-holds.txt
install -D -m 0644 "$here/rhodep-apt-guard"       /usr/local/share/rhodep/rhodep-apt-guard
install -D -m 0644 "$here/50rhodep-protect-holds" /usr/local/share/rhodep/50rhodep-protect-holds
install -D -m 0755 "$here/rhodep-holds-enforce"   /usr/local/sbin/rhodep-holds-enforce
install -D -m 0755 "$here/rhodep-dpkg-protect"   /usr/local/sbin/rhodep-dpkg-protect
install -D -m 0755 "$here/rhodep-protect-files"  /usr/local/sbin/rhodep-protect-files
install -D -m 0755 "$here/rhodep-hold-override"   /usr/local/sbin/rhodep-hold-override
install -D -m 0644 "$here/60rhodep-enforce-holds" /etc/apt/apt.conf.d/60rhodep-enforce-holds
install -D -m 0644 "$here/systemd/rhodep-holds-enforce.service" \
	/etc/systemd/system/rhodep-holds-enforce.service
install -D -m 0644 "$here/systemd/rhodep-holds-enforce.timer" \
	/etc/systemd/system/rhodep-holds-enforce.timer
install -d -m 0755 /etc/rhodep
[ -e /etc/rhodep/apt-holds-exceptions ] || : > /etc/rhodep/apt-holds-exceptions
install -D -m 0644 "$here/apply-holds.sh" /usr/share/doc/rhodep-apt/apply-holds.sh

# Protect every file this component owns: snapshot it, make it immutable, and
# register it so rhodep-holds-enforce puts it back if it ever goes missing.
# Root can still take one out, but only by deciding to (chattr -i first).
/usr/local/sbin/rhodep-protect-files register apt 0755 \
	/usr/local/sbin/rhodep-apt-guard \
	/usr/local/sbin/rhodep-holds-enforce \
	/usr/local/sbin/rhodep-hold-override \
	/usr/local/sbin/rhodep-dpkg-protect \
	/usr/local/sbin/rhodep-protect-files 2>/dev/null || true
/usr/local/sbin/rhodep-protect-files register apt-conf 0644 \
	/etc/apt/apt.conf.d/50rhodep-protect-holds \
	/etc/apt/apt.conf.d/60rhodep-enforce-holds \
	/etc/systemd/system/rhodep-holds-enforce.service \
	/etc/systemd/system/rhodep-holds-enforce.timer \
	/usr/local/share/rhodep/apt-holds.txt 2>/dev/null || true

# --- the port's own .debs, which a hold does NOT fully cover ------------------
# rhodep-battery-jeita, rhodep-modem-support and rhodep-usb-otg are built here
# and exist only in /var/lib/dpkg/status: no repo, no cached archive. apt says
# so outright --
#
#	# apt-get install --reinstall rhodep-battery-jeita
#	Reinstallation of rhodep-battery-jeita is not possible, it cannot be downloaded.
#
# -- so while the hold stops an upgrade and dpkg's Protected stops the package
# being removed, nothing at all stops `rm` of one of their files, and there is
# nowhere to restore it from. Losing /usr/local/sbin/rhodep-battery-jeita means
# losing the below-0C charge cutoff, permanently and silently, on a package
# that still looks installed, held and protected.
#
# So these get the file layer as well. The cost is that `dpkg -i` of a newer
# version of one of these packages fails on the immutable files and needs a
# `rhodep-protect-files release` first -- the same friction as the DKMS
# drivers, for the same reason. Distro packages are deliberately NOT treated
# this way: apt can always re-fetch those, so immutability would only break
# their upgrades.
#
# register skips paths that do not exist, so this is safe when a package is not
# installed. The multi-user.target.wants/* entries are symlinks and are left
# out on purpose: the store keeps a content snapshot, and restoring a symlink
# from one would replace it with a copy of its target.
/usr/local/sbin/rhodep-protect-files register battery-jeita 0755 \
	/usr/local/sbin/rhodep-battery-jeita 2>/dev/null || true
/usr/local/sbin/rhodep-protect-files register battery-jeita-conf 0644 \
	/usr/lib/systemd/system/rhodep-battery-jeita.service 2>/dev/null || true

/usr/local/sbin/rhodep-protect-files register modem-support 0755 \
	/usr/local/sbin/rhodep-fw-symlinks.sh \
	/usr/local/sbin/rhodep-wait-modem 2>/dev/null || true
/usr/local/sbin/rhodep-protect-files register modem-support-conf 0644 \
	/usr/lib/modprobe.d/ath10k-late.conf \
	/usr/lib/systemd/system/ModemManager.service.d/10-rhodep-wait-modem.conf \
	/usr/lib/systemd/system/ath10k-late.service \
	/usr/lib/systemd/system/readonly-firmware.mount \
	/usr/lib/systemd/system/rhodep-modem-fw.service \
	/usr/lib/systemd/system/rmtfs.service.d/10-rhodep.conf \
	/usr/lib/tmpfiles.d/rhodep-modem.conf \
	/usr/lib/udev/rules.d/99-rhodep-fsg-alias.rules 2>/dev/null || true

/usr/local/sbin/rhodep-protect-files register usb-otg 0755 \
	/usr/local/sbin/otg 2>/dev/null || true
/usr/local/sbin/rhodep-protect-files register usb-otg-conf 0644 \
	/usr/lib/systemd/system/otg-default-charge.service 2>/dev/null || true

# The enabled state, which the immutable bit cannot protect: `systemctl
# disable` still works on a file that cannot be deleted. Only `disabled` is
# ever acted on and only with `enable`, so nothing here restarts a service.
/usr/local/sbin/rhodep-protect-files register-units battery-jeita \
	rhodep-battery-jeita.service 2>/dev/null || true
/usr/local/sbin/rhodep-protect-files register-units modem-support \
	rhodep-modem-fw.service ath10k-late.service readonly-firmware.mount 2>/dev/null || true
/usr/local/sbin/rhodep-protect-files register-units usb-otg \
	otg-default-charge.service 2>/dev/null || true
/usr/local/sbin/rhodep-protect-files register-units apt \
	rhodep-holds-enforce.timer 2>/dev/null || true

# apt holds mean nothing to dpkg: `dpkg -P <held>` removes the package without
# a word. dpkg's own Protected field stops it. This is done in the build chroot
# too, because there the status file IS the image's, so the flags ship with it.
# If it declines (something holding the dpkg lock), the enforcer sets them on
# the first boot instead.
/usr/local/sbin/rhodep-dpkg-protect apply || true

if [ -d /run/systemd/system ]; then
	systemctl daemon-reload
	systemctl enable --now rhodep-holds-enforce.timer >/dev/null 2>&1
	systemctl enable rhodep-holds-enforce.service >/dev/null 2>&1
else
	install -d /etc/systemd/system/multi-user.target.wants /etc/systemd/system/timers.target.wants
	ln -sf /etc/systemd/system/rhodep-holds-enforce.service \
		/etc/systemd/system/multi-user.target.wants/rhodep-holds-enforce.service
	ln -sf /etc/systemd/system/rhodep-holds-enforce.timer \
		/etc/systemd/system/timers.target.wants/rhodep-holds-enforce.timer
fi

echo "held: $(apt-mark showhold | wc -l) packages"
echo "purge protection installed and made immutable"
echo "dpkg-level protection: $(/usr/local/sbin/rhodep-dpkg-protect status 2>/dev/null | grep -c 'dpkg-protected=yes') packages carry dpkg's Protected flag"
echo "port files protected : $(/usr/local/sbin/rhodep-protect-files status 2>/dev/null | grep -c 'protected')"
echo "hold enforcement installed; release one deliberately with:"
echo "  sudo rhodep-hold-override release <package> \"<reason>\""
