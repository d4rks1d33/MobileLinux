#!/bin/sh
# Brings up SSH over the USB cable, at the address pmOS and NetHunter use.
# Run as root, on the phone or inside the build chroot.
#
# Why this is worth having, beyond convenience: **the modem cannot be restarted
# without dropping WiFi**, because the WLAN protection domain lives inside it.
# Any work on the modem therefore has to happen over a link that does not depend
# on the modem, and the USB gadget is the only one. Debugging the modem over
# WiFi means losing the connection at the exact moment something interesting
# happens.
#
# The gadget itself already exists (ncm.usb0, which Linux, macOS and Windows 10+
# all support natively). What is missing on a stock Kali rootfs is any address
# on usb0: NetworkManager treats it as an ordinary wired interface and sits
# there asking for DHCP from a machine that is not offering any, so the
# interface stays up with no IPv4 and nothing answers on 172.16.42.1.
set -e

PROTECT=/usr/local/sbin/rhodep-protect-files
# Clear any immutability a previous run set (see userspace/apt: these files
# belong to no package, so the port protects them itself). Re-registered below.
if [ -x "$PROTECT" ]; then
	"$PROTECT" release \
		/etc/NetworkManager/dnsmasq-shared.d/rhodep-usb-no-default-route.conf \
		/etc/NetworkManager/system-connections/usb-gadget.nmconnection 2>/dev/null || true
fi


here=$(dirname "$0")
KEYFILE=/etc/NetworkManager/system-connections/usb-gadget.nmconnection

# Do not advertise a default route, see the file itself for why that matters.
install -D -m 0644 "$here/dnsmasq-no-default-route.conf" \
	/etc/NetworkManager/dnsmasq-shared.d/rhodep-usb-no-default-route.conf

# The profile is written as a keyfile rather than through nmcli, because nmcli
# needs the daemon and this has to work in the build chroot too. NetworkManager
# refuses to load a keyfile that is group or world readable.
install -d -m 0755 /etc/NetworkManager/system-connections
cat > "$KEYFILE" <<'EOF'
[connection]
id=usb-gadget
type=ethernet
interface-name=usb0
autoconnect=true
# There are two other profiles to beat for usb0, both of which leave the far end
# unable to get an address on its own:
#
#   "Wired connection 1"  NetworkManager's own autocreated profile, which asks
#                         for DHCP from a machine that is not serving any
#   "usb0"                a volatile profile NetHunter Pro drops in
#                         /run/NetworkManager/system-connections each boot,
#                         static 172.16.42.1/16 and no DHCP server, so the
#                         machine at the other end has to be configured by hand
#
# The /16 in that last one is also worth avoiding: it claims all of
# 172.16.0.0/16 on whatever is plugged in, which is a large chunk of private
# space to take over for a point to point cable.
autoconnect-priority=100

[ipv4]
# "shared" is what runs the DHCP server. The dnsmasq drop-in installed above is
# what stops it from also announcing itself as router and DNS.
method=shared
address1=172.16.42.1/24

[ipv6]
method=ignore
EOF
chmod 0600 "$KEYFILE"

if [ -d /run/systemd/system ] && command -v nmcli >/dev/null 2>&1; then
	nmcli connection reload >/dev/null 2>&1 || true
	nmcli connection up usb-gadget >/dev/null 2>&1 || true
	echo "usb0 is 172.16.42.1; ssh kali@172.16.42.1 from the machine on the other end"
else
	echo "no running NetworkManager: profile installed, active on next boot"
fi

# Protect what we installed: nothing in dpkg owns it, so nothing else would
# ever put it back. rhodep-holds-enforce restores it if it goes missing.
if [ -x "$PROTECT" ]; then
	"$PROTECT" register usb-net-conf 0644 \
		/etc/NetworkManager/dnsmasq-shared.d/rhodep-usb-no-default-route.conf \
		/etc/NetworkManager/system-connections/usb-gadget.nmconnection 2>/dev/null || true
fi
