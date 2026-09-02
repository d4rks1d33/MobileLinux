#!/bin/sh
# Diagnostic tools for the display and modem investigations, plus the one
# thing here that is not a tool: rhodep-pstore-keep, which copies pstore
# records aside on every boot so a crash log is not overwritten by the next
# boot before anyone reads it. Three captures of the same bug were lost that
# way before it existed.
#
# None of these run on their own except rhodep-pstore-keep, and that one only
# copies files. Nothing here is needed to boot or to place a call.
set -e

SBIN=/usr/local/sbin
SHARE=/usr/local/share

# A previous run leaves these immutable, and install(1) cannot overwrite an
# immutable file: the second run of this script would fail on the first line
# below. Drop the protection first; it is put back at the end.
if [ -x /usr/local/sbin/rhodep-protect-files ]; then
	/usr/local/sbin/rhodep-protect-files release \
		"$SBIN/rhodep-glitch-capture" \
		"$SBIN/rhodep-repaint-bench" \
		"$SHARE/rhodep-repaint-bench.py" \
		"$SBIN/rhodep-idle-power-ab" \
		"$SBIN/rhodep-fwdump" \
		"$SBIN/rhodep-wdt-arm" \
		"$SBIN/rhodep-pon-dump" \
		"$SBIN/rhodep-glink-uaf-check" \
		"$SBIN/rhodep-glink-refcount-test" \
		"$SBIN/rhodep-glink-hammer.py" \
		"$SBIN/rhodep-modem-restart-watch.py" \
		"$SBIN/rhodep-pstore-keep" \
		/etc/systemd/system/rhodep-pstore-keep.service 2>/dev/null || true
fi

install -m755 rhodep-glitch-capture   "$SBIN/rhodep-glitch-capture"
install -m755 rhodep-repaint-bench    "$SBIN/rhodep-repaint-bench"
install -m755 rhodep-repaint-bench.py "$SHARE/rhodep-repaint-bench.py"
install -m755 rhodep-idle-power-ab    "$SBIN/rhodep-idle-power-ab"
install -m755 rhodep-fwdump           "$SBIN/rhodep-fwdump"
install -m755 rhodep-wdt-arm          "$SBIN/rhodep-wdt-arm"
install -m755 rhodep-pstore-keep      "$SBIN/rhodep-pstore-keep"
# PMIC power-on registers: the fingerprint that says which kind of reset just
# happened. It was being copied over by hand every time the phone reset, which
# is exactly the sort of thing that gets lost.
install -m755 rhodep-pon-dump         "$SBIN/rhodep-pon-dump"
# The glink reference-count work: one checks the fix, one reproduces the crash,
# one crashes the modem and watches whether the SoC survives the bring-up.
install -m755 rhodep-glink-uaf-check       "$SBIN/rhodep-glink-uaf-check"
install -m755 rhodep-glink-refcount-test   "$SBIN/rhodep-glink-refcount-test"
install -m755 rhodep-glink-hammer.py       "$SBIN/rhodep-glink-hammer.py"
install -m755 rhodep-modem-restart-watch.py "$SBIN/rhodep-modem-restart-watch.py"
# sudo's secure_path has no /usr/local/sbin, so link the ones meant to be run
# with sudo into somewhere it looks.
for t in rhodep-pon-dump rhodep-glink-uaf-check rhodep-glink-refcount-test \
         rhodep-glink-hammer.py rhodep-modem-restart-watch.py; do
	ln -sf "$SBIN/$t" "/usr/sbin/$t"
done
install -m644 rhodep-pstore-keep.service /etc/systemd/system/rhodep-pstore-keep.service

sync   # a tool still in the page cache does not survive a deliberate reset

systemctl daemon-reload
systemctl enable rhodep-pstore-keep.service

# Same two layers everything else in this port gets: immutable on the live
# file, and a record so an apt purge does not quietly take them away.
if [ -x /usr/local/sbin/rhodep-protect-files ]; then
	/usr/local/sbin/rhodep-protect-files register debug-tools protected \
		"$SBIN/rhodep-glitch-capture" \
		"$SBIN/rhodep-repaint-bench" \
		"$SHARE/rhodep-repaint-bench.py" \
		"$SBIN/rhodep-idle-power-ab" \
		"$SBIN/rhodep-fwdump" \
		"$SBIN/rhodep-wdt-arm" \
		"$SBIN/rhodep-pon-dump" \
		"$SBIN/rhodep-glink-uaf-check" \
		"$SBIN/rhodep-glink-refcount-test" \
		"$SBIN/rhodep-glink-hammer.py" \
		"$SBIN/rhodep-modem-restart-watch.py"
	/usr/local/sbin/rhodep-protect-files register pstore-keep protected \
		"$SBIN/rhodep-pstore-keep" \
		/etc/systemd/system/rhodep-pstore-keep.service
	/usr/local/sbin/rhodep-protect-files register-units pstore-keep \
		rhodep-pstore-keep.service
	/usr/local/sbin/rhodep-protect-files enforce
fi

echo "debug-tools installed and registered"
