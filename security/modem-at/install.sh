#!/bin/sh
# Install the modem AT console. Run on the phone, as root.
set -e
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# If a previous run made it immutable, install(1) cannot overwrite it. Drop the
# protection first; it goes back on at the end.
if [ -x /usr/local/sbin/rhodep-protect-files ]; then
	/usr/local/sbin/rhodep-protect-files release /usr/local/sbin/rhodep-modem-at 2>/dev/null || true
fi

install -D -m 0755 "$here/rhodep-modem-at" /usr/local/sbin/rhodep-modem-at

# sudo's secure_path here is /usr/sbin:/usr/bin:/sbin:/bin, with no
# /usr/local/sbin, so "sudo rhodep-modem-at" would not find it. Link it into a
# directory sudo does search, rather than making everyone type the full path.
ln -sf /usr/local/sbin/rhodep-modem-at /usr/sbin/rhodep-modem-at

# Flush it. On a phone that is being made to reset on purpose, a file still in
# the page cache does not survive, and you are left with a zero-byte tool that
# exits 0 and prints nothing. That has happened here three times.
sync

# rpmsg_ctrl is what exposes /dev/rpmsg_ctrl2. It is normally already loaded as
# a dependency of rpmsg_char, but make it explicit so the tool works on a boot
# where nothing else pulled it in.
modprobe rpmsg_ctrl 2>/dev/null || true

# The same two layers the rest of the port uses: immutable on the live file so
# removing it has to be deliberate, and a canonical copy that
# rhodep-holds-enforce puts back if the live one goes missing. Nothing owns
# this file otherwise: dpkg has never heard of it.
if [ -x /usr/local/sbin/rhodep-protect-files ]; then
	/usr/local/sbin/rhodep-protect-files register modem-at protected \
		/usr/local/sbin/rhodep-modem-at
	/usr/local/sbin/rhodep-protect-files enforce
fi

echo "installed: /usr/local/sbin/rhodep-modem-at"
echo
if [ -e /dev/rpmsg_ctrl2 ]; then
	echo "try:  sudo rhodep-modem-at -l"
	echo "      sudo rhodep-modem-at ATI"
else
	echo "warning: /dev/rpmsg_ctrl2 is not there yet."
	echo "It appears once the modem remoteproc is up; check with"
	echo "  cat /sys/class/remoteproc/remoteproc0/state"
fi
