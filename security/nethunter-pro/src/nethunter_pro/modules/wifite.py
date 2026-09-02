"""wifite3 automated Wi-Fi auditor (/opt/wifite3, the 2.9.9-beta fork).

Built from this fork's real --help, which has more than the packaged wifite:
Evil Twin, WPA3/Dragonblood, honeypot detection, dual-interface, PMKID options
and attack monitoring. wifite is interactive, so it opens in a terminal with
the chosen options already applied. Defaults are set so it is useful out of
the box.
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner

WIFITE = "python3 /opt/wifite3/wifite.py"

ATTACK_TYPES = {
    "All networks": [],
    "WPA/WPA2 only": ["--wpa"],
    "WPA3 only (SAE/OWE)": ["--wpa3"],
    "WPS only": ["--wps"],
    "WPS PIN/Pixie only": ["--wps-only"],
    "PMKID only": ["--pmkid"],
    "WEP only": ["--wep"],
    "Evil Twin (all targets)": ["--eviltwin"],
}


@register
class Wifite(NHModule):
    title = "wifite"
    icon = "network-wireless-symbolic"
    description = "Automated Wi-Fi auditor (wifite3)"

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        scan = Adw.PreferencesGroup(
            title="Scan",
            description="Opens wifite in a terminal; pick targets there.",
        )
        self.iface = Adw.EntryRow(title="Interface")
        self.iface.set_text("wlan1")
        scan.add(self.iface)
        self.channel = Adw.EntryRow(title="Channel (blank = all 2.4GHz)")
        scan.add(self.channel)
        self.attack = Adw.ComboRow(
            title="Attack type", model=Gtk.StringList.new(list(ATTACK_TYPES)))
        scan.add(self.attack)
        box.append(scan)

        opts = Adw.PreferencesGroup(title="Options")
        self.kill = Adw.SwitchRow(
            title="Kill conflicting processes", subtitle="--kill (recommended)")
        self.kill.set_active(True)
        opts.add(self.kill)
        self.random_mac = Adw.SwitchRow(
            title="Randomize MAC", subtitle="-mac (better privacy)")
        self.random_mac.set_active(True)
        opts.add(self.random_mac)
        self.dual = Adw.SwitchRow(
            title="Dual-interface mode",
            subtitle="--dual-interface: no mode-switching in Evil Twin (needs 2 adapters)")
        opts.add(self.dual)
        self.clients_only = Adw.SwitchRow(
            title="Only targets with clients", subtitle="--clients-only")
        opts.add(self.clients_only)
        self.honeypots = Adw.SwitchRow(
            title="Detect honeypots/rogue APs", subtitle="--detect-honeypots")
        opts.add(self.honeypots)
        self.pillage = Adw.SwitchRow(
            title="Pillage (auto-attack all)", subtitle="-p 60: attack all after 60s")
        opts.add(self.pillage)
        box.append(opts)

        crack = Adw.PreferencesGroup(title="Cracking")
        self.dict = Adw.EntryRow(title="Wordlist (file or directory)")
        self.dict.set_text("/opt/wifite3/wordlists/wordlist-probable.txt")
        crack.add(self.dict)
        self.skip_crack = Adw.SwitchRow(
            title="Skip cracking", subtitle="--skip-crack: only capture")
        crack.add(self.skip_crack)
        box.append(crack)

        actions = Adw.PreferencesGroup()
        row = Adw.ActionRow(title="Launch wifite", subtitle="Opens in the system terminal")
        b = Gtk.Button(label="Launch", valign=Gtk.Align.CENTER)
        b.add_css_class("suggested-action")
        b.connect("clicked", self._launch)
        row.add_suffix(b)
        actions.add(row)
        check = Adw.ActionRow(title="System check", subtitle="--syscheck: tools and interfaces")
        cb = Gtk.Button(label="Check", valign=Gtk.Align.CENTER)
        cb.connect("clicked", lambda _b: self.runner.run(
            f"{WIFITE} --syscheck", root=True))
        check.add_suffix(cb)
        actions.add(check)
        box.append(actions)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _launch(self, _b: Gtk.Button) -> None:
        cmd = WIFITE.split()
        cmd += ["-i", self.iface.get_text().strip() or "wlan1"]
        if self.channel.get_text().strip():
            cmd += ["-c", self.channel.get_text().strip()]
        cmd += ATTACK_TYPES[list(ATTACK_TYPES)[self.attack.get_selected()]]
        if self.kill.get_active():
            cmd.append("--kill")
        if self.random_mac.get_active():
            cmd.append("-mac")
        if self.dual.get_active():
            cmd.append("--dual-interface")
        if self.clients_only.get_active():
            cmd.append("--clients-only")
        if self.honeypots.get_active():
            cmd.append("--detect-honeypots")
        if self.pillage.get_active():
            cmd += ["-p", "60"]
        if self.skip_crack.get_active():
            cmd.append("--skip-crack")
        if self.dict.get_text().strip():
            cmd += ["--dict", self.dict.get_text().strip()]
        self.runner.run_in_terminal(cmd, root=True)
