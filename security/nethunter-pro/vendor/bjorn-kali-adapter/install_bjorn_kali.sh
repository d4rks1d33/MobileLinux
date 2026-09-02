#!/bin/bash
# Bjorn installer adapted for Kali on the rhodep phone.
#
# The upstream install_bjorn.sh assumes:
#   * user + hostname = "bjorn" (we're "kali")
#   * path /home/bjorn/Bjorn (we already have /opt/Bjorn)
#   * raspi-config to flip SPI/I2C (Kali doesn't ship raspi-config)
#   * /boot/firmware/ for USB gadget config (pmOS doesn't have that)
#   * a 2.13" e-paper HAT on GPIO (the phone has no GPIO header)
#   * RPi.GPIO + spidev pip packages (C extensions that don't build)
#
# We keep everything else -- system deps, Python deps, systemd
# service, port 8000 web UI, orchestrator. Bjorn works in headless
# mode: the e-paper display code is imported but degrades to no-ops
# through the stubs, so all reads/writes to shared_data still flow
# and the web UI shows the same info the physical display would.
#
# Run as root once. Idempotent.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0"
    exit 1
fi

BJORN_USER=kali
BJORN_PATH=/opt/Bjorn
STUB_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[bjorn-kali] using user=$BJORN_USER path=$BJORN_PATH stubs=$STUB_DIR"

# --- system packages ---
# Everything Bjorn needs at the OS level. libgpiod / libi2c are
# harmless on the phone (no /dev/gpiochip* -> no-op) and keep the
# apt block identical to upstream so we can diff easily.
echo "[bjorn-kali] apt install"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libjpeg-dev zlib1g-dev libpng-dev python3-dev libffi-dev libssl-dev \
    libgpiod-dev libi2c-dev build-essential \
    python3-pip wget lsof git libopenjp2-7 nmap libopenblas-dev \
    bluez-tools bluez python3-pil
# libatlas-base-dev was upstream for Raspberry Pi numpy speedups;
# Kali arm64 ships numpy with OpenBLAS already so it's redundant --
# and not shipped in the Kali arm64 repo anyway, which would abort
# the whole apt call.

nmap --script-updatedb 2>/dev/null || true

# --- Python deps ---
# requirements_kali.txt (which we drop next to install_bjorn.sh)
# strips RPi.GPIO + spidev. The stubs shipped with this installer
# stand in for both at import time.
echo "[bjorn-kali] pip install (requirements_kali.txt)"
if [ -f "$BJORN_PATH/requirements_kali.txt" ]; then
    pip3 install --break-system-packages -r "$BJORN_PATH/requirements_kali.txt"
else
    echo "[bjorn-kali] warning: $BJORN_PATH/requirements_kali.txt missing;"
    echo "            falling back to upstream requirements.txt with RPi bits filtered"
    grep -Ev '^(RPi\.GPIO|spidev)' "$BJORN_PATH/requirements.txt" > /tmp/req.txt
    pip3 install --break-system-packages -r /tmp/req.txt
    rm -f /tmp/req.txt
fi

# --- install stubs to a Python path Bjorn will see ---
# We drop RPi/ and spidev.py directly into Bjorn's own tree, so
# `import RPi.GPIO` and `import spidev` inside display.py /
# waveshare_epd/*.py resolve to our no-op versions instead of
# raising ModuleNotFoundError.
echo "[bjorn-kali] dropping RPi.GPIO + spidev stubs into $BJORN_PATH"
install -d -m 0755 "$BJORN_PATH/RPi"
install -m 0644 "$STUB_DIR/RPi/__init__.py" "$BJORN_PATH/RPi/__init__.py"
install -m 0644 "$STUB_DIR/RPi/GPIO.py" "$BJORN_PATH/RPi/GPIO.py"
install -m 0644 "$STUB_DIR/spidev.py" "$BJORN_PATH/spidev.py"

# Replace waveshare_epd/epdconfig.py with the headless backend.
# The upstream file grep-detects the platform and falls back to
# JetsonNano when none of RPi/SunriseX3 match -- on the phone that
# path then loads a Jetson-only .so and crashes at import.
# Back up the original first so a factory reset is one `mv` away.
EPDCONFIG="$BJORN_PATH/resources/waveshare_epd/epdconfig.py"
if [ -f "$EPDCONFIG" ] && [ ! -f "$EPDCONFIG.upstream" ]; then
    cp "$EPDCONFIG" "$EPDCONFIG.upstream"
    echo "[bjorn-kali] backed up upstream epdconfig.py -> epdconfig.py.upstream"
fi
if [ -f "$STUB_DIR/epdconfig.py" ]; then
    install -m 0644 "$STUB_DIR/epdconfig.py" "$EPDCONFIG"
    echo "[bjorn-kali] installed headless epdconfig.py"
fi

# --- systemd service ---
# Same shape as upstream, points at /opt/Bjorn instead of
# /home/bjorn/Bjorn. The ExecStartPost open-files watchdog is
# kept because it's genuinely useful.
echo "[bjorn-kali] writing systemd unit /etc/systemd/system/bjorn.service"
cat > /etc/systemd/system/bjorn.service <<UNIT
[Unit]
Description=Bjorn Service
DefaultDependencies=no
Before=basic.target
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$BJORN_PATH
ExecStartPre=$BJORN_PATH/kill_port_8000.sh
ExecStart=/usr/bin/python3 $BJORN_PATH/Bjorn.py
StandardOutput=journal
StandardError=journal
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

# Restart if we get near the fd limit.
ExecStartPost=/bin/bash -c 'F=\$(ulimit -n); T=\$((F-1000)); while :; do O=\$(lsof 2>/dev/null | wc -l); if [ "\$O" -ge "\$T" ]; then systemctl restart bjorn.service; exit 0; fi; sleep 10; done &'

[Install]
WantedBy=multi-user.target
UNIT

chmod +x "$BJORN_PATH/kill_port_8000.sh" 2>/dev/null || true

# --- fd limits (like upstream) ---
if ! grep -q "^\\* soft nofile 65535" /etc/security/limits.conf; then
    echo "[bjorn-kali] raising nofile limits in /etc/security/limits.conf"
    cat >> /etc/security/limits.conf <<LIMITS
* soft nofile 65535
* hard nofile 65535
root soft nofile 65535
root hard nofile 65535
LIMITS
fi

# ownership of the code tree
chown -R "$BJORN_USER":"$BJORN_USER" "$BJORN_PATH"

# --- reload + enable ---
systemctl daemon-reload
systemctl enable bjorn.service

echo
echo "[bjorn-kali] Bjorn is installed."
echo "[bjorn-kali] start it now with:   sudo systemctl start bjorn"
echo "[bjorn-kali] web UI:              http://\$(hostname -I | awk '{print \$1}'):8000"
echo "[bjorn-kali] logs:                journalctl -u bjorn -f"
