#!/bin/sh
# Installs the app, its desktop integration and the privileged DBus helper.
# Run as root on the phone.
set -e
here=$(dirname "$0")

pip install --break-system-packages "$here[tv,iot]"

# Desktop entry, icon, polkit action.
install -Dm644 "$here/data/org.kali.NetHunterPro.desktop" \
    /usr/share/applications/org.kali.NetHunterPro.desktop
# The NetHunter dragon, installed at a few sizes so Phosh picks a crisp one.
for size in 48 64 128 192; do
    if command -v convert >/dev/null 2>&1; then
        convert "$here/data/icons/org.kali.NetHunterPro.png" \
            -resize ${size}x${size} \
            "/tmp/nhpro-icon-${size}.png"
        install -Dm644 "/tmp/nhpro-icon-${size}.png" \
            "/usr/share/icons/hicolor/${size}x${size}/apps/org.kali.NetHunterPro.png"
        rm -f "/tmp/nhpro-icon-${size}.png"
    fi
done
# Always install the source PNG at its native size as a fallback.
install -Dm644 "$here/data/icons/org.kali.NetHunterPro.png" \
    /usr/share/icons/hicolor/192x192/apps/org.kali.NetHunterPro.png
# Remove any previous SVG placeholder.
rm -f /usr/share/icons/hicolor/scalable/apps/org.kali.NetHunterPro.svg

# Module icons that are not in any installed icon theme, so the app ships them:
# a car for CARsenal, a pumpkin for wifipumpkin3, the pwnagotchi face for the
# pwnagotchi module. Symbolic, under scalable/actions.
for ic in nethunter-carsenal nethunter-wifipumpkin nethunter-pwnagotchi \
          nethunter-docker nethunter-bjorn; do
    install -Dm644 "$here/data/icons/${ic}-symbolic.svg" \
        "/usr/share/icons/hicolor/scalable/actions/${ic}-symbolic.svg"
done

install -Dm644 "$here/data/org.kali.NetHunterPro.policy" \
    /usr/share/polkit-1/actions/org.kali.NetHunterPro.policy

# Privileged helper: script, dbus activation, bus policy, systemd unit.
install -Dm755 "$here/helper/nethunter_pro_helper.py" \
    /usr/libexec/nethunter-pro-helper
install -Dm755 "$here/helper/hid_type.py" \
    /usr/libexec/nethunter-pro-hid
install -Dm755 "$here/helper/rhodep-make-captiveportal" \
    /usr/libexec/nethunter-pro-make-captiveportal
install -Dm755 "$here/helper/rhodep-phishkin3-launch" \
    /usr/libexec/nethunter-pro-phishkin3-launch
install -Dm755 "$here/helper/blespam-runner" \
    /usr/libexec/nethunter-pro-blespam
install -Dm755 "$here/helper/blespam-recover" \
    /usr/libexec/nethunter-pro-blespam-recover

# Retire the stand-alone Bluetooth LE Spam launcher: the engine lives
# inside the NetHunter Pro app now (module `blespam`, backend at
# `nethunter_pro.vendor.blespam`) and the .desktop + /opt drop just
# duplicated it in the phone's app grid. Silent if it was never there.
# We also remove /opt/Bluetooth-LE-Spam (the upstream Android Kotlin
# source clone) since it is only there for reference and payloads.py
# has the whole packet catalogue ported already.
rm -f /usr/share/applications/bluetooth-le-spam.desktop
rm -rf /opt/Bluetooth-LE-Spam-linux
rm -rf /opt/Bluetooth-LE-Spam
# World-readable index directory the phishkin3 module reads to surface
# installed-cert domains in the picker. The launcher populates it on each run
# (see refresh_cert_index()); the install just creates the empty dir so the
# picker never sees ENOENT before the first launcher run.
install -d -m 0755 /var/lib/nethunter-phishkin3/certs
install -Dm644 "$here/data/dbus/org.kali.NetHunterPro.Helper.service" \
    /usr/share/dbus-1/system-services/org.kali.NetHunterPro.Helper.service
install -Dm644 "$here/data/dbus/org.kali.NetHunterPro.Helper.conf" \
    /usr/share/dbus-1/system.d/org.kali.NetHunterPro.Helper.conf
install -Dm644 "$here/data/dbus/nethunter-pro-helper.service" \
    /usr/lib/systemd/system/nethunter-pro-helper.service

# Refresh caches so the icon and the desktop entry show up in Phosh.
update-desktop-database /usr/share/applications 2>/dev/null || true
gtk-update-icon-cache -f /usr/share/icons/hicolor 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true
systemctl reload dbus 2>/dev/null || true

echo "installed; look for 'NetHunter Pro' in the app drawer"
