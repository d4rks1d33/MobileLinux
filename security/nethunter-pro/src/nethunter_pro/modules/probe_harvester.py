"""Probe harvester -- passive-only PNL + client-fingerprint recon.

Devices that are not currently associated to Wi-Fi periodically
broadcast Probe Requests advertising the SSIDs they remember (their
Preferred Network List, PNL). Even fully randomised-MAC devices leak
their PNL and a lot of information-element fingerprints in these
frames -- enough to identify OS/model and, cross-referenced with
Wigle.net, roughly where the device has been.

Unlike KARMA, this module never *responds* to anything. It listens
via ``tcpdump -i <mon> -e -s0 type mgt subtype probereq`` and parses
the frames with Scapy. Two views:

* **Probe requests** -- every probe with a non-empty SSID, aggregated
  by (client MAC, SSID). This is the PNL harvest.
* **Clients** -- unique client MACs with a rough OS classifier based
  on the tag list (Apple / Microsoft / Samsung vendor IEs, HT/VHT
  capabilities that vary by chipset, and interframe spacing).

Everything is written to a CSV in the loot tree so the operator can
feed it back into other tools (KARMA "clone this SSID", Evil Twin,
Wigle correlation, or just export for a report).
"""
from __future__ import annotations

import base64
import csv
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, toast

DEFAULT_IFACE = "wlan1mon"

# Wigle.net API key file: "user:token" saved once, reused across
# probes. Wigle uses HTTP Basic with the pair -- easier to reason
# about than an OAuth flow.
WIGLE_KEY_FILE = Path(
    os.environ.get("XDG_CONFIG_HOME")
    or os.path.expanduser("~/.config"))/ "nethunter-pro" / "wigle.key"


def _load_wigle_key() -> str:
    try:
        return WIGLE_KEY_FILE.read_text().strip()
    except OSError:
        return ""


def _save_wigle_key(key: str) -> None:
    WIGLE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    WIGLE_KEY_FILE.write_text(key.strip() + "\n")
    try:
        os.chmod(WIGLE_KEY_FILE, 0o600)
    except OSError:
        pass


def wigle_search_ssid(ssid: str, api_pair: str,
                      timeout: int = 15) -> list[dict]:
    """Query wigle.net for a given SSID. Returns the ``results`` array
    from the JSON response, or ``[]`` on any error.

    ``api_pair`` is the ``user:token`` string wigle hands out at
    https://wigle.net/account.
    """
    if not ssid or not api_pair or ":" not in api_pair:
        return []
    from urllib.parse import quote_plus
    url = ("https://api.wigle.net/api/v2/network/search"
           "?onlymine=false&freenet=false&paynet=false"
           "&ssid=" + quote_plus(ssid))
    b = base64.b64encode(api_pair.encode()).decode()
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Basic " + b,
            "User-Agent": "nethunter-pro-probe-harvester/1.0",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return []
            data = json.loads(resp.read(2 * 1024 * 1024))
            return list(data.get("results", []))
    except (urllib.error.URLError, urllib.error.HTTPError,
            ValueError, OSError):
        return []


@register
class ProbeHarvester(NHModule):
    title = "Probe harvester"
    icon = "network-wireless-acquiring-symbolic"
    description = ("Passive capture of Wi-Fi probe requests; aggregate "
                   "the PNL of every client heard, classify OS from IE "
                   "fingerprints.")
    required_tools = ["tcpdump"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._loot_id: int | None = None
        # (mac, ssid) -> {"count": n, "first": ts, "last": ts, "os": s}
        self._probes: dict[tuple[str, str], dict] = {}
        # mac -> {"os": s, "vendor": s, "count": n}
        self._clients: dict[str, dict] = {}
        self._csv_path: str | None = None
        # row widgets we manage so we can clear/rerender
        self._probe_rows: list[Adw.ActionRow] = []
        self._client_rows: list[Adw.ActionRow] = []

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        cfg_group = Adw.PreferencesGroup(title="Capture")
        self.iface = Adw.EntryRow(title="Monitor interface")
        self.iface.set_text(DEFAULT_IFACE)
        cfg_group.add(self.iface)

        self.hop = Adw.SwitchRow(
            title="Channel hopping",
            subtitle="Rotate through 2.4/5 GHz channels once per second")
        self.hop.set_active(True)
        cfg_group.add(self.hop)

        self.hop_band = Adw.ComboRow(title="Hop band")
        self.hop_band.set_model(Gtk.StringList.new([
            "2.4 GHz + 5 GHz",
            "2.4 GHz only",
            "5 GHz only",
        ]))
        cfg_group.add(self.hop_band)

        run_row = Adw.ActionRow(
            title="Start / Stop harvesting",
            subtitle="Purely passive; nothing transmitted")
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

        # ---- stats
        stat_group = Adw.PreferencesGroup(title="Session")
        self.state_row = Adw.ActionRow(
            title="Status", subtitle="idle")
        stat_group.add(self.state_row)
        self.clients_row = Adw.ActionRow(
            title="Unique clients", subtitle="0")
        stat_group.add(self.clients_row)
        self.probes_row = Adw.ActionRow(
            title="Unique (client, SSID) pairs", subtitle="0")
        stat_group.add(self.probes_row)
        self.csv_row = Adw.ActionRow(
            title="CSV output", subtitle="—")
        stat_group.add(self.csv_row)
        box.append(stat_group)

        # ---- clients
        self._clients_group = Adw.PreferencesGroup(
            title="Clients heard",
            description="MAC · OS guess · SSIDs probed")
        self._clients_empty = Adw.ActionRow(
            title="No clients yet",
            subtitle="Start harvesting and wait for probes")
        self._clients_group.add(self._clients_empty)
        box.append(self._clients_group)

        # ---- probes
        self._probes_group = Adw.PreferencesGroup(
            title="Probed SSIDs (PNL)",
            description="Every non-empty SSID any client has probed")
        self._probes_empty = Adw.ActionRow(
            title="No SSIDs yet", subtitle="")
        self._probes_group.add(self._probes_empty)
        box.append(self._probes_group)

        # ---- Wigle correlation
        wigle_group = Adw.PreferencesGroup(
            title="Wigle.net correlation",
            description="Look up an SSID in wigle.net's crowdsourced "
                        "database to see where in the world the "
                        "matching networks live. Useful when the "
                        "PNL leaks a distinctive name.")
        self.wigle_key_row = Adw.PasswordEntryRow(
            title="Wigle API key (user:token)")
        self.wigle_key_row.set_show_apply_button(True)
        self.wigle_key_row.set_text(_load_wigle_key())
        self.wigle_key_row.connect(
            "apply",
            lambda _r: (_save_wigle_key(_r.get_text()),
                        toast(self.app_window,
                              "Wigle key saved")))
        wigle_group.add(self.wigle_key_row)

        self.wigle_ssid = Adw.EntryRow(title="SSID to look up")
        self.wigle_ssid.set_show_apply_button(True)
        self.wigle_ssid.connect(
            "apply",
            lambda _r: self._wigle_search(_r.get_text().strip()))
        wigle_group.add(self.wigle_ssid)

        get_key_row = Adw.ActionRow(
            title="Don't have a key?",
            subtitle="Register at wigle.net/account")
        get_key_btn = Gtk.Button(label="Open site",
                                 valign=Gtk.Align.CENTER)
        get_key_btn.connect(
            "clicked",
            lambda _b: Gtk.UriLauncher.new(
                "https://wigle.net/account"
            ).launch(self.app_window, None, None, None))
        get_key_row.add_suffix(get_key_btn)
        wigle_group.add(get_key_row)

        self.wigle_results = Adw.PreferencesGroup(
            title="Wigle results",
            description="Networks matching the query")
        self._wigle_empty = Adw.ActionRow(
            title="No query yet",
            subtitle="Type an SSID above and press Enter")
        self.wigle_results.add(self._wigle_empty)
        self._wigle_rows: list[Adw.ActionRow] = []
        box.append(wigle_group)
        box.append(self.wigle_results)

        self.output = OutputView()
        box.append(self.output)
        return box

    # ------------------------------------------------------ Wigle
    def _wigle_search(self, ssid: str) -> None:
        api_pair = _load_wigle_key()
        if not api_pair or ":" not in api_pair:
            toast(self.app_window,
                  "Set a Wigle API key first (user:token)")
            return
        if not ssid:
            return
        toast(self.app_window, "Querying Wigle for " + ssid + "…")

        def worker():
            results = wigle_search_ssid(ssid, api_pair)
            GLib.idle_add(self._render_wigle, ssid, results)

        threading.Thread(target=worker, daemon=True).start()

    def _render_wigle(self, ssid: str, results: list) -> bool:
        # Clear old rows.
        for r in self._wigle_rows:
            self.wigle_results.remove(r)
        self._wigle_rows = []
        self._wigle_empty.set_visible(not results)
        if not results:
            self._wigle_empty.set_title(
                "No results for '" + ssid + "'")
            return False

        # Save the full result to loot so the user can dig later.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = ssid.replace("/", "_")[:60]
        wig_path = loot_path(
            "wigle", "wigle-%s-%s.json" % (safe, stamp))
        try:
            with open(wig_path, "w") as fp:
                json.dump(results, fp, indent=2)
        except OSError:
            pass
        get_loot_store().record(
            module="probe_harvester", type="wigle_json",
            target=ssid, path=wig_path,
            notes="%d results" % len(results))

        for r in results[:20]:
            title = r.get("ssid", "?") + " · " + r.get("netid", "?")
            subtitle = "%.5f, %.5f · %s · %d obs" % (
                r.get("trilat", 0.0), r.get("trilong", 0.0),
                r.get("country", "?"),
                r.get("qos", 0))
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            # Copy coords to clipboard on click for quick pastes.
            btn = Gtk.Button.new_from_icon_name(
                "edit-copy-symbolic")
            btn.set_valign(Gtk.Align.CENTER)
            btn.add_css_class("flat")
            btn.set_tooltip_text("Copy lat,lng")

            def on_copy(_b, lat=r.get("trilat"),
                         lng=r.get("trilong")):
                self.app_window.get_clipboard().set(
                    "%s,%s" % (lat, lng))
                toast(self.app_window, "Coords copied")

            btn.connect("clicked", on_copy)
            row.add_suffix(btn)
            self.wigle_results.add(row)
            self._wigle_rows.append(row)
        return False

    # ---------------------------------------------------------- start
    def _start(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        iface = self.iface.get_text().strip() or DEFAULT_IFACE

        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._csv_path = loot_path(
            "probes", "probes-%s.csv" % stamp)
        self.csv_row.set_subtitle(self._csv_path)
        # Write CSV header.
        try:
            with open(self._csv_path, "w", newline="") as fp:
                w = csv.writer(fp)
                w.writerow(["ts", "client_mac", "ssid",
                            "rssi", "vendor_ie", "os_guess"])
        except OSError:
            pass

        # We use a small Python one-liner as the actual capture -- it
        # uses Scapy and can extract SSID + tags + RSSI directly. The
        # loop yields JSON-per-line so we can parse safely.
        script = (
            "python3 - <<'PY'\n"
            "import json, sys, time\n"
            "from scapy.all import sniff, Dot11, Dot11Elt, "
            "Dot11ProbeReq, RadioTap\n"
            "IF = %r\n"
            "def cb(pkt):\n"
            "    if not pkt.haslayer(Dot11ProbeReq): return\n"
            "    ssid = ''\n"
            "    tags = []\n"
            "    e = pkt.getlayer(Dot11Elt)\n"
            "    while isinstance(e, Dot11Elt):\n"
            "        if e.ID == 0:\n"
            "            try: ssid = e.info.decode('utf-8', 'replace')\n"
            "            except Exception: ssid = ''\n"
            "        tags.append(e.ID)\n"
            "        e = e.payload.getlayer(Dot11Elt)\n"
            "    mac = pkt.addr2 or ''\n"
            "    rssi = ''\n"
            "    if pkt.haslayer(RadioTap):\n"
            "        try: rssi = pkt.dBm_AntSignal\n"
            "        except Exception: pass\n"
            "    vendor = ''\n"
            "    if 221 in tags: vendor = 'vendor_ie'\n"
            "    print(json.dumps({\n"
            "      'ts': time.time(), 'mac': mac, 'ssid': ssid,\n"
            "      'rssi': rssi, 'tags': tags, 'vendor': vendor,\n"
            "    }), flush=True)\n"
            "sniff(iface=IF, prn=cb, store=0, "
            "  lfilter=lambda p: p.haslayer(Dot11ProbeReq))\n"
            "PY\n"
        ) % iface

        self.output.append("# harvesting probes on %s\n" % iface)

        self._probes = {}
        self._clients = {}
        self._render_probes()
        self._render_clients()

        self._loot_id = get_loot_store().record(
            module="probe_harvester", type="probe_csv",
            target=iface, path=self._csv_path,
            notes="passive probe capture")

        def on_line(text: str) -> None:
            for line in (text or "").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    # tcpdump / scapy status noise -> log tail only
                    self.output.append(line + "\n")
                    continue
                try:
                    obj = __import__("json").loads(line)
                except ValueError:
                    continue
                self._on_probe(obj)

        def on_done(code: int) -> None:
            self.output.append("[harvester exited: %d]\n" % code)
            self._proc = None
            self.start_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)
            self.state_row.set_subtitle("stopped")
            if self._loot_id is not None:
                get_loot_store().refresh_size(self._loot_id)

        # Bring the interface to the correct channel-hop mode first.
        # scapy sniff itself doesn't hop; we run a small hopper in
        # the background if the switch is on.
        if self.hop.get_active():
            self._start_hopper(iface)

        self._proc = Process(
            ["sh", "-c", script], on_line, on_done, root=True)
        self._proc.start()
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.state_row.set_subtitle("running")

    def _start_hopper(self, iface: str) -> None:
        band = self.hop_band.get_selected()
        if band == 0:
            channels = ("1 2 3 4 5 6 7 8 9 10 11 36 40 44 48 52 56 60 "
                        "64 100 149 153 157 161")
        elif band == 1:
            channels = "1 2 3 4 5 6 7 8 9 10 11"
        else:
            channels = ("36 40 44 48 52 56 60 64 100 104 108 112 "
                        "116 120 124 128 132 136 140 144 149 153 "
                        "157 161 165")
        script = (
            "IF=%s; CHANS='%s'; "
            "while true; do "
            "  for ch in $CHANS; do "
            "    iw dev $IF set channel $ch 2>/dev/null || true; "
            "    sleep 1; "
            "  done; "
            "done"
        ) % (iface, channels)
        # Fire-and-forget; the hopper dies when the harvester stops
        # because we tear down the iface via the deauth module.
        self._hopper = Process(
            ["sh", "-c", script], lambda _t: None,
            lambda _c: None, root=True)
        self._hopper.start()

    def _stop(self) -> None:
        if self._proc is not None:
            self._proc.stop()
        hop = getattr(self, "_hopper", None)
        if hop is not None:
            try:
                hop.stop()
            except Exception:
                pass
        self.stop_btn.set_sensitive(False)
        self.state_row.set_subtitle("stopping…")

    # -------------------------------------------------- probe handler
    def _on_probe(self, obj: dict) -> None:
        mac = (obj.get("mac") or "").lower()
        ssid = obj.get("ssid") or ""
        tags = obj.get("tags") or []
        os_guess = self._classify_os(mac, tags, obj.get("vendor", ""))
        now = obj.get("ts") or time.time()
        rssi = obj.get("rssi", "")

        # Update state.
        if ssid:
            key = (mac, ssid)
            d = self._probes.setdefault(
                key,
                {"count": 0, "first": now, "last": now,
                 "os": os_guess})
            d["count"] += 1
            d["last"] = now
        c = self._clients.setdefault(
            mac, {"count": 0, "os": os_guess, "ssids": set()})
        c["count"] += 1
        if os_guess != "?":
            c["os"] = os_guess
        if ssid:
            c["ssids"].add(ssid)

        # Append to CSV.
        if self._csv_path:
            try:
                with open(self._csv_path, "a", newline="") as fp:
                    w = csv.writer(fp)
                    w.writerow([now, mac, ssid, rssi,
                                obj.get("vendor", ""), os_guess])
            except OSError:
                pass

        # UI refresh (rate-limited: repaint at most every 2 s).
        if not hasattr(self, "_last_paint"):
            self._last_paint = 0.0
        if time.time() - self._last_paint > 2.0:
            self._last_paint = time.time()
            self._render_probes()
            self._render_clients()
            self.probes_row.set_subtitle(str(len(self._probes)))
            self.clients_row.set_subtitle(str(len(self._clients)))

    def _classify_os(self, mac: str, tags: list,
                     vendor: str) -> str:
        """Best-effort OS classifier. Not conclusive; just enough for
        the UI to say 'this looks like an iPhone'."""
        # Apple randomised MACs use certain OUI + specific IE order.
        # A "locally administered" MAC (bit 0x02 in first octet)
        # tells us it's randomised; the vendor OUI is then useless.
        if not mac:
            return "?"
        oct0 = int(mac.split(":")[0], 16) if ":" in mac else 0
        randomised = bool(oct0 & 0x02)
        tag_set = set(tags)
        # Apple: HT+VHT+HE+221 and no Microsoft WPS tag.
        if 221 in tag_set and (191 in tag_set or 255 in tag_set):
            if randomised:
                return "Apple (randomised)"
            return "Apple"
        # Windows tends to include tag 51 (Neighbor Report) + Microsoft
        # vendor.
        if 51 in tag_set and 221 in tag_set:
            return "Windows"
        # Android usually has tag 191 (VHT) + 221 (vendor) with a
        # different OUI pattern; hard to distinguish without OUI DB.
        if 191 in tag_set:
            return "Android?"
        return "?"

    # -------------------------------------------------- UI rendering
    def _render_probes(self) -> None:
        for r in self._probe_rows:
            self._probes_group.remove(r)
        self._probe_rows = []
        self._probes_empty.set_visible(not self._probes)
        # Sort by count desc, top 30.
        top = sorted(self._probes.items(),
                     key=lambda kv: kv[1]["count"], reverse=True)[:30]
        for (mac, ssid), info in top:
            row = Adw.ActionRow(
                title=ssid,
                subtitle="%s · %d probe(s) · %s"
                        % (mac, info["count"], info.get("os", "?")))
            btn = Gtk.Button(label="Clone",
                             valign=Gtk.Align.CENTER)
            btn.set_tooltip_text(
                "Copy to Evil Twin as target ESSID")
            btn.connect(
                "clicked",
                lambda _b, s=ssid:
                    self.app_window.activate_module(
                        "evil_twin", s))
            row.add_suffix(btn)
            self._probes_group.add(row)
            self._probe_rows.append(row)

    def _render_clients(self) -> None:
        for r in self._client_rows:
            self._clients_group.remove(r)
        self._client_rows = []
        self._clients_empty.set_visible(not self._clients)
        top = sorted(self._clients.items(),
                     key=lambda kv: kv[1]["count"], reverse=True)[:30]
        for mac, info in top:
            ssids = ", ".join(sorted(info["ssids"]))[:80] \
                    or "(no SSIDs yet)"
            row = Adw.ActionRow(
                title=mac,
                subtitle="%s · %d probes · known: %s"
                        % (info.get("os", "?"),
                           info["count"], ssids))
            self._clients_group.add(row)
            self._client_rows.append(row)
