"""Kali Services -- one place to start / stop the systemd units the
rest of the app depends on.

Design constraints the operator asked for:

* Every service the toolset needs is centralised here. Individual
  modules never call ``systemctl start`` on their own; they show a
  banner listing which services they need and a "Open Kali
  Services" deep-link.
* Start / Stop only. **Never enable / disable** unless the operator
  does it from a terminal by hand -- persistent services burn
  battery on a phone.
* Live status: every row polls ``systemctl is-active`` on a short
  timer and reflects the truth even if the operator toggled
  something from another shell.
* Groups by category so it's clear what each service is for.

The list of services below is derived from the audit done across
every module in ``modules/``. If you add a module that needs a new
service, add it here and add a banner to the module.
"""
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from ..executor import Result, run_async
from ..module import NHModule, register
from ..widgets import OutputView, toast


# Grouped catalog. Each entry is (unit_name, human_label, subtitle,
# list_of_modules_that_use_it). The subtitle explains which modules
# in the app depend on the service so the operator has one screen
# that answers "why is this here?".
_SERVICE_GROUPS: list[tuple[str, list[tuple[str, str, str, list[str]]]]] = [
    ("Bluetooth", [
        ("bluetooth",
         "BlueZ (bluetoothd)",
         "BR/EDR + BLE stack. Needed by: bluetooth, bluetoolkit, "
         "ble_track, ble_prov, ble_attack, blueducky. "
         "MUST BE OFF for blespam (uses user-channel HCI).",
         ["bluetooth", "bluetoolkit", "ble_track", "ble_prov",
          "ble_attack", "blueducky"]),
    ]),
    ("Network / mDNS", [
        ("NetworkManager",
         "NetworkManager",
         "Wi-Fi + managed interfaces. Needed by: iot_setup, "
         "wifi_direct. Optional but improves: deauth, handshake, "
         "pmkid, net_discovery, phishkin3, net_control.",
         ["iot_setup", "wifi_direct", "deauth", "handshake",
          "pmkid", "net_discovery", "phishkin3", "net_control"]),
        ("avahi-daemon",
         "Avahi (mDNS)",
         "Local DNS-SD. Needed by: android_rce (adb-tls mDNS "
         "discovery), tv_remote (SSDP/mDNS TV discovery), "
         "net_discovery (avahi-browse of the LAN), deauth "
         "(client hostname resolve).",
         ["android_rce", "tv_remote", "net_discovery", "deauth"]),
        ("gpsd",
         "gpsd",
         "GPS location daemon. Needed by: gps, pmkid "
         "(--rds=1 tags PMKIDs with location).",
         ["gps", "pmkid"]),
    ]),
    ("Web + database (offensive toolchain)", [
        ("postgresql",
         "PostgreSQL",
         "Metasploit database. Optional but improves: bjorn, "
         "payloads, routersploit, set_tool, wifipumpkin.",
         ["bjorn", "payloads", "routersploit", "set_tool",
          "wifipumpkin"]),
        ("apache2",
         "Apache HTTP",
         "Serves phishing pages / Malware URLs. Optional but "
         "improves: set_tool (SET clones sites via Apache).",
         ["set_tool"]),
        ("nginx",
         "nginx",
         "Reverse proxy for custom captive portals; wifipumpkin3 "
         "and evilginx sometimes want it as an upstream.",
         []),
        ("redis-server",
         "Redis",
         "Cache backend used by some modern offensive frameworks "
         "(BloodHound, custom).",
         []),
    ]),
    ("Containers", [
        ("docker",
         "Docker",
         "Container engine. Needed by: docker module. Also useful "
         "for running third-party toolchains that ship as images.",
         ["docker"]),
        ("docker.socket",
         "Docker socket",
         "Companion to docker.service; on-demand socket activation.",
         ["docker"]),
    ]),
    ("Remote access", [
        ("ssh",
         "OpenSSH server",
         "Incoming SSH. Off by default on the phone; turn on for "
         "your own tethered access. Not required by any module.",
         []),
        ("vncserver@:1",
         "VNC server (:1)",
         "Systemd template for a display :1 VNC. The vnc module "
         "spawns a per-session vncserver too; use this only if "
         "you want VNC to survive across app restarts.",
         []),
    ]),
    ("File sharing", [
        ("nfs-server",
         "NFS server",
         "Optional. Handy for exporting loot to a lab machine.",
         []),
        ("smbd",
         "Samba (smbd)",
         "Optional. Similar use as NFS but Windows-friendly.",
         []),
    ]),
    ("Pwnagotchi (custom units)", [
        ("rhodep-pwnagotchi",
         "rhodep-pwnagotchi",
         "The pwnagotchi agent proper. Needed by: pwnagotchi. "
         "Starts the two support units below by dependency.",
         ["pwnagotchi"]),
        ("rhodep-pwn-bettercap",
         "rhodep-pwn-bettercap",
         "bettercap in the pwnagotchi setup; you rarely need to "
         "touch it directly.",
         ["pwnagotchi"]),
        ("rhodep-pwngrid-peer",
         "rhodep-pwngrid-peer",
         "pwngrid peer daemon. Optional; useful if pwngrid is "
         "part of your engagement.",
         ["pwnagotchi"]),
    ]),
]


@register
class KaliServices(NHModule):
    title = "Kali Services"
    icon = "system-run-symbolic"
    description = ("Start / Stop every systemd service the app "
                   "modules use. No enable-on-boot; battery-friendly.")

    def __init__(self, app_window):
        super().__init__(app_window)
        # unit_name -> Adw.SwitchRow
        self._rows: dict[str, Adw.SwitchRow] = {}
        # Poll timer id so we can cancel it on module teardown; the
        # base class doesn't wire that but we keep it clean anyway.
        self._poll_id: int | None = None

    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        intro = Adw.PreferencesGroup(
            title="How this screen works",
            description="Every service listed below is used by one "
                        "or more modules. Flip the switch to start "
                        "it; flip it off to stop. Enable-on-boot is "
                        "intentionally not exposed here -- if you "
                        "want a service to persist across reboots, "
                        "do ``sudo systemctl enable <svc>`` from a "
                        "terminal.")
        box.append(intro)

        # A single Refresh button up top instead of per-row polling.
        # Live polling burned battery on the last version; refresh
        # on demand + on module open is enough.
        refresh_row = Adw.ActionRow(
            title="Refresh all statuses",
            subtitle="Re-read `systemctl is-active` for every unit")
        refresh_btn = Gtk.Button(label="Refresh",
                                 valign=Gtk.Align.CENTER)
        refresh_btn.connect("clicked",
                            lambda _b: self._refresh_states())
        refresh_row.add_suffix(refresh_btn)
        box.append(Adw.PreferencesGroup(
            title="", description=""))  # spacer
        intro.add(refresh_row)

        # Per-group PreferencesGroup, one switch row per unit.
        for group_title, entries in _SERVICE_GROUPS:
            grp = Adw.PreferencesGroup(title=group_title)
            for unit, label, subtitle, _modules in entries:
                row = Adw.SwitchRow(title=label,
                                     subtitle=subtitle)
                row.connect("notify::active",
                            self._on_toggle, unit)
                self._rows[unit] = row
                grp.add(row)
            box.append(grp)

        # Output log at the bottom for the actual systemctl output.
        self.output = OutputView()
        box.append(self.output)

        # Initial state read.
        self._refresh_states()
        return box

    # ------------------------------------------------- state polling
    def _refresh_states(self) -> None:
        """One-shot: fires ``systemctl is-active <unit>`` for every
        row and reflects the result. Non-blocking; each row updates
        as its own callback comes back."""
        for unit, row in self._rows.items():
            def done(result: Result, r=row, u=unit) -> None:
                # ``is-active`` returns ``active`` on stdout when
                # the unit is running; any other word (``inactive``,
                # ``failed``, ``unknown``) means it's not.
                out = (result.stdout or "").strip()
                r.handler_block_by_func(self._on_toggle)
                r.set_active(out == "active")
                r.handler_unblock_by_func(self._on_toggle)
                # Add a small tag to the subtitle when the unit is
                # in a failed state so the operator sees it without
                # having to open journalctl.
                base_sub = _find_subtitle(u)
                if out == "failed":
                    r.set_subtitle(base_sub + "  [failed]")
                elif out and out != "active" and out != "inactive":
                    r.set_subtitle(base_sub + "  [" + out + "]")
                else:
                    r.set_subtitle(base_sub)

            run_async(["systemctl", "is-active", unit],
                      done, timeout=5)

    # ------------------------------------------------- toggles
    def _on_toggle(self, row: Adw.SwitchRow, _param,
                   unit: str) -> None:
        action = "start" if row.get_active() else "stop"
        self.output.append("$ systemctl %s %s\n" % (action, unit))

        def done(result: Result) -> None:
            if result.stdout:
                self.output.append(result.stdout)
            if result.stderr:
                self.output.append(result.stderr)
            self.output.append(
                "[exit %d]\n\n" % result.returncode)
            if result.returncode == 0:
                toast(self.app_window,
                      "%s %sed" % (unit, action))
            else:
                toast(self.app_window,
                      "%s failed to %s" % (unit, action))
                # Re-read state so the switch reflects reality.
                self._refresh_states()

        run_async(["systemctl", action, unit],
                  done, root=True, timeout=30)

    # ------------------------------------------------- deep-link
    def set_target(self, target: str) -> None:
        """Deep-link contract: modules can call
        ``activate_module("kali_services", "svc1,svc2")`` to
        highlight the specific services they need. We just refresh
        state; the row visibility doesn't change (all rows stay
        listed) but a subtle CSS class could be added later."""
        # For now, just re-poll on open so the display is current.
        self._refresh_states()


def _find_subtitle(unit: str) -> str:
    """Look up the canonical subtitle for a unit from the catalog.
    Used to reset a row's subtitle after a transient status tag."""
    for _group_title, entries in _SERVICE_GROUPS:
        for u, _label, subtitle, _mods in entries:
            if u == unit:
                return subtitle
    return ""


# ---------------------------------------------------- shared helper
def modules_requiring(unit: str) -> list[str]:
    """Return the list of module names that declare a dependency on
    the given systemd unit. Consumed by ``ServicesBanner`` (see
    widgets.py) so the banner text stays in sync with this catalog."""
    for _group_title, entries in _SERVICE_GROUPS:
        for u, _label, _subtitle, mods in entries:
            if u == unit:
                return list(mods)
    return []
