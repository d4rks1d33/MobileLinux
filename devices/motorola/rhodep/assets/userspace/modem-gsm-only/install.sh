#!/bin/sh
# Opt-in. Pins the modem to GSM so ModemManager can manage it without the SoC
# resetting, which is what an LTE attach does here.
#
# What this buys: ModemManager sees the modem, so dialer, SMS and mobile data
# work. Data is GPRS, because there is no LTE.
#
# What it costs: no LTE, and no 3G either -- 3G never registered in testing.
# Undo with:
#   systemctl disable --now rhodep-modem-gsm-only
#   mv /etc/modprobe.d/rhodep-ipa-hold.conf.disabled-by-gsm-only \
#      /etc/modprobe.d/rhodep-ipa-hold.conf
#   mv /usr/local/share/rhodep/protect.d/modem-conf.list.before-gsm-only \
#      /usr/local/share/rhodep/protect.d/modem-conf.list
#   rhodep-protect-files enforce
#   qmicli -d qrtr://0 --nas-set-system-selection-preference="lte"
# Put the hold back before allowing LTE again, or the next attach resets the
# phone.
set -e

install -m755 rhodep-modem-gsm-only /usr/local/sbin/rhodep-modem-gsm-only
install -m644 rhodep-modem-gsm-only.service \
	/etc/systemd/system/rhodep-modem-gsm-only.service

# IPA has to be allowed to load: without it the modem has no net port,
# ModemManager refuses to manage it, and the SIM is never provisioned.
#
# Deleting the hold is not enough. It is a protected file of the modem-conf
# component, so `rhodep-protect-files enforce` puts it straight back from its
# stored copy, and the holds-enforce timer runs enforce on a schedule. And
# `release` only drops the immutable bit; it does not unregister anything.
# The registration itself has to go.
SHARE=/usr/local/share/rhodep
LIST=$SHARE/protect.d/modem-conf.list
HOLD=/etc/modprobe.d/rhodep-ipa-hold.conf

if [ -f "$LIST" ] && grep -q "rhodep-ipa-hold.conf" "$LIST"; then
	cp "$LIST" "$LIST.before-gsm-only"
	grep -v "rhodep-ipa-hold.conf" "$LIST" > "$LIST.tmp"
	mv "$LIST.tmp" "$LIST"
	rm -f "$SHARE/files/%etc%modprobe.d%rhodep-ipa-hold.conf"
fi

if [ -f "$HOLD" ]; then
	chattr -i "$HOLD" 2>/dev/null || true
	mv "$HOLD" "$HOLD.disabled-by-gsm-only"
fi

systemctl daemon-reload
systemctl enable rhodep-modem-gsm-only.service

if [ -x /usr/local/sbin/rhodep-protect-files ]; then
	/usr/local/sbin/rhodep-protect-files register modem-gsm-only protected \
		/usr/local/sbin/rhodep-modem-gsm-only \
		/etc/systemd/system/rhodep-modem-gsm-only.service
	/usr/local/sbin/rhodep-protect-files register-units modem-gsm-only \
		rhodep-modem-gsm-only.service
	/usr/local/sbin/rhodep-protect-files enforce
fi

echo "modem pinned to GSM; reboot to bring it up"
