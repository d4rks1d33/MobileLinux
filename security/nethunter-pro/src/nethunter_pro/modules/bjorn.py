"""Bjorn -- the "Tamagotchi-like" LAN scanner + brute-forcer.

Upstream is https://github.com/infinition/Bjorn: a Python service that
runs a fleet of scanning and brute-force actions against every host it
finds on the local subnet, with a web UI on port 8000 for monitoring.
It was written for a Raspberry Pi Zero with a 2.13" e-paper HAT
attached; on this phone we run it headless -- an installer at
/opt/Bjorn/kali-adapter/ ships stubs for RPi.GPIO/spidev and replaces
the waveshare_epd/epdconfig.py with a no-op backend so the imports
resolve without hardware. See docs/bjorn-kali-adapter.md for the
mechanics of that adaptation.

This module is the on/off switch + a pre-configuration surface for
the settings the user is likely to want before Bjorn starts driving
attacks: how often to scan, whether to steal files from vulnerable
hosts, the network CIDR to focus on. Nothing is wired to systemd --
Bjorn runs as a subprocess owned by this module, so closing the app
or toggling off cleanly kills it and nothing survives on the host.

The Bjorn config lives in JSON at ``/opt/Bjorn/config/shared_config.
json``, which the daemon reads once at startup and writes back to
whenever the web UI or the CSV pipeline changes something. We edit
it in place so the values we put in the UI stick across restarts.

Web UI: http://<phone>:8000 -- served by Bjorn itself, this module
just links out to it once Bjorn is up.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async
from ..module import NHModule, register
from ..widgets import OutputView, toast

BJORN_ROOT = "/opt/Bjorn"
BJORN_MAIN = "/opt/Bjorn/Bjorn.py"
BJORN_CONFIG = "/opt/Bjorn/config/shared_config.json"
BJORN_LOG_DIR = "/opt/Bjorn/data/logs"
BJORN_WEB_URL = "http://127.0.0.1:8000"
BJORN_PORT = 8000


@register
class Bjorn(NHModule):
    title = "Bjorn"
    icon = "nethunter-bjorn-symbolic"
    description = "LAN network scanner + brute-forcer with a web UI (Bjorn)"
    # Bjorn's install script lives under /opt/Bjorn/kali-adapter and
    # writes the RPi/spidev stubs the daemon needs; if Bjorn.py is
    # missing we surface that in the missing-tools banner rather than
    # in a runtime crash.
    required_tools = ["nmap"]

    def __init__(self, app_window):
        super().__init__(app_window)
        # The Bjorn subprocess; None when stopped. Tail runs in the
        # same Process because Bjorn logs to stdout when launched
        # from the command line the way we do.
        self._proc: Process | None = None
        self._poll_id: int | None = None

    # -------------------------------------------------------------- build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        if not os.path.exists(BJORN_MAIN):
            return self._not_installed_page()

        # ---- power + status --------------------------------------
        power = Adw.PreferencesGroup(
            title="Bjorn",
            description="Runs as a subprocess owned by this app. "
                        "Turning it off kills the daemon and its "
                        "orchestrator threads immediately.")
        self.power_row = Adw.SwitchRow(
            title="Enable Bjorn",
            subtitle="Starts the Python daemon and its web UI")
        self.power_row.connect("notify::active", self._on_power)
        power.add(self.power_row)

        self.state_row = Adw.ActionRow(
            title="Status", subtitle="stopped")
        self.state_icon = Gtk.Image.new_from_icon_name(
            "media-playback-stop-symbolic")
        self.state_row.add_prefix(self.state_icon)
        power.add(self.state_row)

        ui_row = Adw.ActionRow(
            title="Web UI",
            subtitle="Available on port 8000 once Bjorn is running")
        ui_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        ui_btn.connect("clicked", lambda _b: self._open_ui())
        ui_row.add_suffix(ui_btn)
        power.add(ui_row)
        box.append(power)

        # ---- pre-configuration -----------------------------------
        # These are the settings people actually want to touch before
        # letting Bjorn loose. Everything else stays at upstream
        # defaults; the config file is huge and most of it is
        # display / cosmetic.
        cfg = Adw.PreferencesGroup(
            title="Configuration",
            description="Written to shared_config.json. Changes take "
                        "effect the next time Bjorn starts.")

        self.manual_mode = Adw.SwitchRow(
            title="Manual mode",
            subtitle="If off, Bjorn scans and attacks automatically. "
                     "If on, only actions triggered from the web UI run.")
        self.manual_mode.connect("notify::active", self._on_manual_mode)
        cfg.add(self.manual_mode)

        safety_row = Adw.ActionRow(
            title="Force manual mode on first start",
            subtitle="Set manual mode before Bjorn starts, in case "
                     "the config has it disabled from a prior run.")
        safety_btn = Gtk.Button(label="Enable manual",
                                valign=Gtk.Align.CENTER)
        safety_btn.add_css_class("suggested-action")
        safety_btn.connect(
            "clicked",
            lambda _b: (
                self._save_key("manual_mode", True),
                self._set_switch(self.manual_mode,
                                 self._on_manual_mode, True),
                toast(self.app_window, "Manual mode set")))
        safety_row.add_suffix(safety_btn)
        cfg.add(safety_row)

        self.scan_interval = Adw.SpinRow.new_with_range(30, 3600, 30)
        self.scan_interval.set_title("Network scan interval (s)")
        self.scan_interval.set_subtitle(
            "How often to run the network scanner. Default 180.")
        self.scan_interval.set_value(180)
        self.scan_interval.connect(
            "notify::value", lambda *_: self._save_key(
                "scan_interval", int(self.scan_interval.get_value())))
        cfg.add(self.scan_interval)

        self.vuln_interval = Adw.SpinRow.new_with_range(60, 86400, 60)
        self.vuln_interval.set_title("Vulnerability scan interval (s)")
        self.vuln_interval.set_subtitle(
            "How often to re-run nmap vulns against known hosts. "
            "Default 900.")
        self.vuln_interval.set_value(900)
        self.vuln_interval.connect(
            "notify::value", lambda *_: self._save_key(
                "scan_vuln_interval",
                int(self.vuln_interval.get_value())))
        cfg.add(self.vuln_interval)

        self.startup_delay = Adw.SpinRow.new_with_range(0, 300, 5)
        self.startup_delay.set_title("Startup delay (s)")
        self.startup_delay.set_subtitle(
            "Seconds to wait for Wi-Fi before the orchestrator "
            "starts.")
        self.startup_delay.set_value(10)
        self.startup_delay.connect(
            "notify::value", lambda *_: self._save_key(
                "startup_delay",
                int(self.startup_delay.get_value())))
        cfg.add(self.startup_delay)

        self.retry_failed = Adw.SwitchRow(
            title="Retry failed actions",
            subtitle="Re-attempt actions that failed after the retry "
                     "delay.")
        self.retry_failed.connect(
            "notify::active", lambda *_: self._save_key(
                "retry_failed_actions",
                self.retry_failed.get_active()))
        cfg.add(self.retry_failed)

        self.retry_success = Adw.SwitchRow(
            title="Retry successful actions",
            subtitle="Re-run actions that already succeeded (usually "
                     "off unless you want more data)")
        self.retry_success.connect(
            "notify::active", lambda *_: self._save_key(
                "retry_success_actions",
                self.retry_success.get_active()))
        cfg.add(self.retry_success)

        self.blacklist_check = Adw.SwitchRow(
            title="Skip blacklisted hosts",
            subtitle="Excludes IPs / MACs in the blacklist from "
                     "scanning. This device's MAC is auto-added.")
        self.blacklist_check.connect(
            "notify::active", lambda *_: self._save_key(
                "blacklistcheck",
                self.blacklist_check.get_active()))
        cfg.add(self.blacklist_check)

        self.debug_mode = Adw.SwitchRow(
            title="Debug mode",
            subtitle="Verbose logging in the daemon output.")
        self.debug_mode.connect(
            "notify::active", lambda *_: self._save_key(
                "debug_mode", self.debug_mode.get_active()))
        cfg.add(self.debug_mode)

        box.append(cfg)

        # ---- data / results --------------------------------------
        data = Adw.PreferencesGroup(
            title="Data",
            description="Bjorn writes the knowledge base and cracked "
                        "credentials under /opt/Bjorn/data.")

        self.hosts_row = Adw.ActionRow(
            title="Known hosts", subtitle="—")
        data.add(self.hosts_row)

        self.creds_row = Adw.ActionRow(
            title="Cracked credentials", subtitle="—")
        data.add(self.creds_row)

        reset_row = Adw.ActionRow(
            title="Reset Bjorn state",
            subtitle="Wipes the network knowledge base and cracked "
                     "passwords. Keeps the config.")
        reset_btn = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
        reset_btn.add_css_class("destructive-action")
        reset_btn.connect("clicked", lambda _b: self._reset_state())
        reset_row.add_suffix(reset_btn)
        data.add(reset_row)

        box.append(data)

        # ---- live log --------------------------------------------
        self.output = OutputView()
        box.append(self.output)

        # Read current config into the UI so switches reflect what's
        # actually on disk.
        self._load_config()
        # Refresh the hosts / creds counters every few seconds so the
        # user sees progress without touching anything.
        self._refresh_data_counts()
        self._poll_id = GLib.timeout_add_seconds(
            5, self._refresh_data_counts)
        return box

    def _not_installed_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            icon_name="dialog-warning-symbolic",
            title="Bjorn is not installed",
            description=(
                "Expected the daemon at " + BJORN_MAIN +
                ".\nInstall it with:\n\n"
                "  sudo /opt/Bjorn/kali-adapter/install_bjorn_kali.sh"))
        return page

    # ------------------------------------------------------------- power
    def _on_power(self, row: Adw.SwitchRow, _param) -> None:
        if row.get_active():
            self._start()
        else:
            self._stop()

    def _start(self) -> None:
        if self._proc is not None and self._proc.running:
            return
        self.output.append("# starting Bjorn\n")
        # Bjorn expects to run from its own directory (it reads
        # ``config/shared_config.json`` and ``data/`` with relative
        # paths). Also inject PYTHONPATH so the RPi/ + spidev.py
        # stubs the adapter dropped in /opt/Bjorn resolve before
        # anything else. Bjorn needs root for raw sockets (ARP,
        # nmap SYN) and CAP_NET_ADMIN.
        argv = ["sh", "-c",
                "cd %s && PYTHONPATH=%s PYTHONUNBUFFERED=1 "
                "exec python3 %s"
                % (BJORN_ROOT, BJORN_ROOT, BJORN_MAIN)]
        self.output.append("$ %s\n" % argv[-1])
        self._proc = Process(
            argv,
            self.output.append,
            self._on_stop_done,
            root=True,
        )
        self._proc.start()
        self._set_state("starting…", "content-loading-symbolic")
        # Bjorn takes ~15s from "python3 Bjorn.py" to "Serving at
        # port 8000" -- flip the state pill from "starting" to
        # "running" once the port is up.
        GLib.timeout_add_seconds(20, self._check_running)

    def _check_running(self) -> bool:
        if self._proc is None or not self._proc.running:
            return False
        self._set_state("running -- web UI on :%d" % BJORN_PORT,
                         "emblem-ok-symbolic")
        return False

    def _stop(self) -> None:
        if self._proc is None:
            return
        self.output.append("[stopping Bjorn]\n")
        self._proc.stop()
        # The stop handler updates the state; also try to free port
        # 8000 in case anything else grabbed it via Bjorn's port-
        # bumping fallback.
        run_async(
            ["sh", "-c",
             "fuser -k %d/tcp 2>/dev/null || true" % BJORN_PORT],
            lambda _r: None, root=True, timeout=5)

    def _on_stop_done(self, code: int) -> None:
        self._proc = None
        self.output.append("[Bjorn exited: %d]\n" % code)
        # Sync the switch back to off in case the daemon died on its
        # own (e.g. missing dep) rather than being stopped.
        self.power_row.handler_block_by_func(self._on_power)
        self.power_row.set_active(False)
        self.power_row.handler_unblock_by_func(self._on_power)
        self._set_state("stopped", "media-playback-stop-symbolic")

    def _set_state(self, text: str, icon: str) -> None:
        self.state_row.set_subtitle(text)
        self.state_icon.set_from_icon_name(icon)
        if text.startswith("running"):
            # The web UI page finishes binding a few seconds after
            # "Serving at port 8000" prints. There's no cleaner
            # readiness signal we can grep for.
            pass

    def _open_ui(self) -> None:
        try:
            Gtk.UriLauncher.new(BJORN_WEB_URL).launch(
                self.app_window, None, None, None)
        except Exception:
            toast(self.app_window,
                  "Open %s in a browser" % BJORN_WEB_URL)

    # ------------------------------------------------------- config I/O
    def _load_config(self) -> None:
        """Read shared_config.json and pre-set the UI switches.

        The file is root-owned when Bjorn was started by systemd, so
        we go through the helper. In practice it's readable to kali
        (the installer chowns the tree) but we don't want to assume.
        """
        def done(result: Result) -> None:
            try:
                cfg = json.loads(result.stdout or "{}")
            except ValueError:
                cfg = {}
            self._set_switch(self.manual_mode, self._on_manual_mode,
                             bool(cfg.get("manual_mode", False)))
            self._set_switch(self.retry_failed, None,
                             bool(cfg.get("retry_failed_actions",
                                          True)))
            self._set_switch(self.retry_success, None,
                             bool(cfg.get("retry_success_actions",
                                          False)))
            self._set_switch(self.blacklist_check, None,
                             bool(cfg.get("blacklistcheck", True)))
            self._set_switch(self.debug_mode, None,
                             bool(cfg.get("debug_mode", True)))
            # SpinRows: freeze the ::value signal while we prefill,
            # otherwise every set_value() bounces through _save_key()
            # and rewrites the config on load.
            for row, key, default in (
                (self.scan_interval, "scan_interval", 180),
                (self.vuln_interval, "scan_vuln_interval", 900),
                (self.startup_delay, "startup_delay", 10),
            ):
                row.freeze_notify()
                row.set_value(int(cfg.get(key, default)))
                row.thaw_notify()

        run_async(["cat", BJORN_CONFIG], done, root=True, timeout=8)

    def _set_switch(self, row: Adw.SwitchRow, handler, value: bool
                    ) -> None:
        if handler is not None:
            row.handler_block_by_func(handler)
        row.set_active(value)
        if handler is not None:
            row.handler_unblock_by_func(handler)

    def _save_key(self, key: str, value) -> None:
        """Write ``key = value`` back to shared_config.json.

        Uses a tiny python one-liner as root so we don't have to
        worry about ownership or JSON quoting from a shell heredoc.
        Bjorn re-reads the config live on some keys and on daemon
        restart for others; we don't bother distinguishing.
        """
        script = (
            "import json,sys\n"
            "p='%s'\n"
            "d=json.load(open(p))\n"
            "d[%r]=%s\n"
            "json.dump(d, open(p,'w'), indent=4)\n"
        ) % (BJORN_CONFIG, key, json.dumps(value))
        run_async(["python3", "-c", script],
                  lambda _r: None, root=True, timeout=8)

    def _on_manual_mode(self, row, _p) -> None:
        self._save_key("manual_mode", row.get_active())
        if self._proc is not None and self._proc.running:
            toast(self.app_window,
                  "Restart Bjorn to apply the mode change")

    # ----------------------------------------------------- data counts
    def _refresh_data_counts(self) -> bool:
        # Count entries in the CSVs Bjorn maintains. Cheap; runs in
        # a shell in the root helper so root-owned files work.
        script = (
            "printf 'hosts:%s\\ncreds:%s\\n' "
            "\"$(test -f /opt/Bjorn/data/netkb.csv && "
            "wc -l < /opt/Bjorn/data/netkb.csv || echo 0)\" "
            "\"$(find /opt/Bjorn/data/output/crackedpwd -name '*.csv' "
            "  -exec awk 'NR>1' {} \\; 2>/dev/null | wc -l)\""
        )

        def done(result: Result) -> None:
            hosts = "0"
            creds = "0"
            for line in (result.stdout or "").splitlines():
                if line.startswith("hosts:"):
                    hosts = str(max(0, int(line[6:] or "1") - 1))
                elif line.startswith("creds:"):
                    creds = line[6:]
            self.hosts_row.set_subtitle(
                "%s in netkb.csv" % hosts)
            self.creds_row.set_subtitle(
                "%s in data/output/crackedpwd/" % creds)

        run_async(["sh", "-c", script], done, root=True, timeout=6)
        return True

    def _reset_state(self) -> None:
        dlg = Adw.MessageDialog(
            transient_for=self.app_window,
            heading="Reset Bjorn state?",
            body="Deletes the network knowledge base "
                 "(data/netkb.csv), all cracked credentials "
                 "(data/output/crackedpwd/*.csv), and any stolen "
                 "files (data/output/). The configuration is kept. "
                 "This cannot be undone.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("reset", "Reset")
        dlg.set_response_appearance(
            "reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def on_resp(_d, resp: str) -> None:
            if resp != "reset":
                return
            script = (
                "rm -f /opt/Bjorn/data/netkb.csv; "
                "rm -rf /opt/Bjorn/data/output; "
                "mkdir -p /opt/Bjorn/data/output/crackedpwd; "
                "chown -R kali:kali /opt/Bjorn/data 2>/dev/null || true; "
                "echo reset"
            )
            run_async(["sh", "-c", script],
                      lambda _r: (toast(self.app_window,
                                        "Bjorn state cleared"),
                                  self._refresh_data_counts()),
                      root=True, timeout=8)
        dlg.connect("response", on_resp)
        dlg.present()
