#!/bin/sh
# Hand the display over to GDM (a real login screen with a user chooser) instead
# of the phosh.service autologin. Run as root on the phone, or inside the rootfs
# chroot while building the image.
#
# Why this exists
# ---------------
# Kali NetHunter Pro / Mobian start the shell with phosh.service: a systemd unit
# with User=1000 and PAMName=login that opens a session for uid 1000 on tty7 and
# does `chvt 7`. That is an autologin; there is no login screen, so the first
# thing the user sees is Phosh's *lock* screen (the PIN keypad), not a chooser.
#
# It only becomes a visible problem once gdm3 is on the system. gdm3 arrives on
# its own with several Kali metapackages (kali-linux-default, kali-themes-mobile
# pulled it in here), and its postinst creates
# /etc/systemd/system/display-manager.service, which systemd's graphical.target
# wants. From then on BOTH start at every boot: GDM puts its greeter on tty1 and
# phosh.service takes the display back with `chvt 7`. The greeter is only seen
# when it happens to win the VT, and logging in from it then fails with "a
# session is already open, force close?" because the session on tty7 was created
# by phosh.service's own PAM stack and GDM knows nothing about it.
#
# Fix: one owner of the display. phosh.service is disabled and masked, GDM is the
# display manager, and the kali user defaults to the Phosh session, so GDM starts
# exactly the same shell after the user picks an account.
#
# Notes
# -----
#   * Masking (not just disabling) matters: an upgrade of the phosh package
#     re-enables the unit otherwise.
#   * Screen lock still asks for the current user's password. That is how session
#     locking works everywhere; the chooser belongs to login, not to unlock. Use
#     "Log out" (power menu) to get back to it, or the switch-user launcher this
#     script installs, which asks GDM for a greeter on another VT via
#     CreateTransientDisplay and leaves the running session alone.
#   * rhodep-phosh-wifi-guard >= 2 is required: v1's airmon-safe restarts
#     phosh.service when it cannot find Phosh, which would recreate the very
#     conflict this script removes.
set -e

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

USER_NAME="${1:-kali}"

echo "[*] disabling the phosh.service autologin"
systemctl disable phosh.service 2>/dev/null || true
systemctl mask phosh.service

echo "[*] making gdm3 the display manager"
ln -sf /lib/systemd/system/gdm3.service /etc/systemd/system/display-manager.service
echo "/usr/sbin/gdm3" > /etc/X11/default-display-manager
# Anything that pulls in another DM later (lomiri and xfce4 recommend lightdm,
# some plasma sets recommend sddm) must not take the seat.
for dm in lightdm sddm lxdm nodm; do
  if systemctl list-unit-files "$dm.service" >/dev/null 2>&1; then
    systemctl disable "$dm.service" 2>/dev/null || true
    systemctl mask "$dm.service" 2>/dev/null || true
  fi
  echo "$dm shared/default-x-display-manager select gdm3" | debconf-set-selections 2>/dev/null || true
done

echo "[*] defaulting $USER_NAME to the Phosh session"
mkdir -p /var/lib/AccountsService/users
cat > "/var/lib/AccountsService/users/$USER_NAME" <<EOF
[User]
Session=phosh
SessionType=wayland
Icon=/var/lib/AccountsService/icons/$USER_NAME
SystemAccount=false
EOF
chmod 600 "/var/lib/AccountsService/users/$USER_NAME"

echo "[*] installing the switch-user launcher"
install -m 0755 "$(dirname "$0")/switch-user" /usr/local/bin/switch-user
install -m 0644 "$(dirname "$0")/switch-user.desktop" \
        /usr/share/applications/switch-user.desktop
update-desktop-database /usr/share/applications 2>/dev/null || true

echo
echo "[OK] reboot to get the login screen."
echo "     revert with: systemctl unmask phosh.service && systemctl enable phosh.service"
echo "                  rm /var/lib/AccountsService/users/$USER_NAME"
