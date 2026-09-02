"""Build and run an nmap scan, with live output and a Quick preset.

Techniques used to live in a single ComboRow. In practice most real nmap
runs combine several techniques (UDP + a TCP scan + version detection),
and the man page groups them explicitly: there is one and only one
"payload-based TCP scan" (SYN vs. Connect vs. ACK vs. ...) but every
other technique family is independent and can be layered on. So the UI
mirrors that:

  * a ComboRow selects the single TCP scan mode (or "none"),
  * separate SwitchRows enable UDP, SCTP INIT, SCTP COOKIE-ECHO and
    IP-protocol scans in parallel,
  * host-discovery bypasses (-sn, -Pn) are switches too.

That is what nmap itself accepts on the command line: -sS -sU -sV runs
a SYN + UDP + version-detection scan in one invocation, so a user who
ticks the boxes here gets exactly the flags they'd type.
"""
from __future__ import annotations

from gi.repository import Adw, Gtk

from ..module import NHModule, register
from ..widgets import ToolRunner

# Only one of these can be active at a time -- they all use TCP payloads
# and nmap will refuse >1. "(none)" lets the user drop the TCP scan
# entirely to run e.g. a pure UDP scan or a ping-only sweep.
TCP_SCANS = {
    "(none)": "",
    "SYN scan (-sS)": "-sS",
    "Connect scan (-sT)": "-sT",
    "ACK scan (-sA)": "-sA",
    "Window scan (-sW)": "-sW",
    "Maimon scan (-sM)": "-sM",
    "Null scan (-sN)": "-sN",
    "FIN scan (-sF)": "-sF",
    "Xmas scan (-sX)": "-sX",
}

TIMING = {f"-T{i}": f"-T{i}" for i in range(6)}


@register
class Nmap(NHModule):
    title = "Nmap"
    icon = "network-workgroup-symbolic"
    description = "Scan hosts and networks"
    required_tools = ["nmap"]

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(12)

        # ---- target + core options -------------------------------
        group = Adw.PreferencesGroup(title="Target and options")
        self.target = Adw.EntryRow(title="Target (host, range or CIDR)")
        self.target.set_text("127.0.0.1")
        group.add(self.target)

        # Optional -p; leave empty to use nmap defaults.
        self.ports = Adw.EntryRow(
            title="Ports (-p, empty = nmap default)")
        group.add(self.ports)

        # Single mutually-exclusive TCP scan mode.
        self.tcp_scan = Adw.ComboRow(
            title="TCP scan technique",
            subtitle="Only one at a time; nmap only accepts one payload type",
            model=Gtk.StringList.new(list(TCP_SCANS)))
        self.tcp_scan.set_selected(1)  # SYN by default
        group.add(self.tcp_scan)

        self.timing = Adw.ComboRow(
            title="Timing",
            model=Gtk.StringList.new(list(TIMING)))
        self.timing.set_selected(3)  # -T3, nmap default
        group.add(self.timing)
        box.append(group)

        # ---- other scan families (multi-select) ------------------
        other_group = Adw.PreferencesGroup(
            title="Other scan techniques",
            description="These stack -- pick as many as you want; each one "
                        "adds its flag to the nmap command line.")
        self.udp = Adw.SwitchRow(
            title="UDP scan", subtitle="-sU (slow but essential for DNS/DHCP/SNMP)")
        other_group.add(self.udp)
        self.sctp_init = Adw.SwitchRow(
            title="SCTP INIT scan", subtitle="-sY")
        other_group.add(self.sctp_init)
        self.sctp_cookie = Adw.SwitchRow(
            title="SCTP COOKIE-ECHO scan", subtitle="-sZ")
        other_group.add(self.sctp_cookie)
        self.ip_proto = Adw.SwitchRow(
            title="IP protocol scan",
            subtitle="-sO (which IP protocols the host answers on)")
        other_group.add(self.ip_proto)
        box.append(other_group)

        # ---- host discovery / probes ------------------------------
        disc_group = Adw.PreferencesGroup(
            title="Host discovery",
            description="Change how nmap decides a host is up. Ping is on by "
                        "default; -Pn skips the check (needed on hosts that "
                        "drop ICMP), -sn does only the check (no port scan).")
        self.ping_only = Adw.SwitchRow(
            title="Ping scan only", subtitle="-sn (no ports)")
        disc_group.add(self.ping_only)
        self.no_ping = Adw.SwitchRow(
            title="Skip host discovery", subtitle="-Pn")
        disc_group.add(self.no_ping)
        box.append(disc_group)

        # ---- enrichment -------------------------------------------
        enrich_group = Adw.PreferencesGroup(
            title="Enrichment",
            description="Extra probes run after ports are found.")
        self.sv = Adw.SwitchRow(
            title="Service/version detection", subtitle="-sV")
        enrich_group.add(self.sv)
        self.osdetect = Adw.SwitchRow(
            title="OS detection", subtitle="-O")
        enrich_group.add(self.osdetect)
        self.default_scripts = Adw.SwitchRow(
            title="Default NSE scripts",
            subtitle="-sC (equivalent to --script=default)")
        enrich_group.add(self.default_scripts)
        self.aggressive = Adw.SwitchRow(
            title="Aggressive",
            subtitle="-A (enables -sV, -O, -sC and traceroute together)")
        enrich_group.add(self.aggressive)
        self.verbose = Adw.SwitchRow(
            title="Verbose output", subtitle="-v")
        enrich_group.add(self.verbose)
        box.append(enrich_group)

        # ---- actions ----------------------------------------------
        actions = Adw.PreferencesGroup()
        full = Adw.ActionRow(
            title="Run scan",
            subtitle="Use the options above")
        sb = Gtk.Button(label="Scan", valign=Gtk.Align.CENTER)
        sb.add_css_class("suggested-action")
        sb.connect("clicked", self._on_scan)
        full.add_suffix(sb)
        actions.add(full)
        quick = Adw.ActionRow(
            title="Quick scan",
            subtitle="nmap -T4 -F (top 100 ports)")
        qb = Gtk.Button(label="Quick", valign=Gtk.Align.CENTER)
        qb.connect("clicked", self._on_quick)
        quick.add_suffix(qb)
        actions.add(quick)
        box.append(actions)

        self.runner = ToolRunner()
        box.append(self.runner)
        return box

    def _target(self) -> str:
        return self.target.get_text().strip() or "127.0.0.1"

    def set_target(self, value: str) -> None:
        """Public entry point used by cross-module deep-links (e.g. Network
        Discovery's "Send to nmap" button). Fills the Target field so the
        user just has to press Scan."""
        if hasattr(self, "target"):
            self.target.set_text(value)

    def _on_quick(self, _b: Gtk.Button) -> None:
        self.runner.run(f"nmap -T4 -F {self._target()}", root=True)

    def _on_scan(self, _b: Gtk.Button) -> None:
        parts: list[str] = ["nmap"]

        # TCP scan technique (may be empty -> no TCP scan at all).
        tcp_flag = list(TCP_SCANS.values())[self.tcp_scan.get_selected()]
        if tcp_flag:
            parts.append(tcp_flag)

        # Layered "other" scans -- each adds its own flag.
        if self.udp.get_active():
            parts.append("-sU")
        if self.sctp_init.get_active():
            parts.append("-sY")
        if self.sctp_cookie.get_active():
            parts.append("-sZ")
        if self.ip_proto.get_active():
            parts.append("-sO")

        # Host discovery bypasses.
        if self.ping_only.get_active():
            parts.append("-sn")
        if self.no_ping.get_active():
            parts.append("-Pn")

        # Timing.
        parts.append(list(TIMING.values())[self.timing.get_selected()])

        # Enrichment.
        if self.sv.get_active():
            parts.append("-sV")
        if self.osdetect.get_active():
            parts.append("-O")
        if self.default_scripts.get_active():
            parts.append("-sC")
        if self.aggressive.get_active():
            parts.append("-A")
        if self.verbose.get_active():
            parts.append("-v")

        # Optional port spec.
        ports = self.ports.get_text().strip()
        if ports:
            parts.extend(["-p", ports])

        parts.append(self._target())
        self.runner.run(" ".join(parts), root=True)
