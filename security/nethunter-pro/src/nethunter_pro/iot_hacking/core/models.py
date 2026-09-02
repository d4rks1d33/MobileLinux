"""Data model for the IoT Hacking pipeline.

Everything that flows through Discovery → Fingerprint → Vuln → Exploit is
one of the objects declared here. Sticking to plain dataclasses keeps the
model easy to serialise (SQLite + JSON audit log), easy to compare in
tests, and light enough that the UI can hold a couple of hundred devices
in memory without noticing.

The registry stores the canonical ``Device`` per MAC address. Ports,
vulns, credentials and attack runs are children -- kept as plain lists
on the ``Device`` for in-memory work, mirrored to SQLite tables by the
registry for persistence.

Nothing here does I/O. All the async work happens in ``discovery/``,
``fingerprint.py``, ``vuln.py`` and ``exploit_runner.py``; those modules
build these objects and hand them to the registry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DeviceType(str, Enum):
    """Device categories that map to the sidebar sections in the UI.

    Kept coarse on purpose -- one section per icon in the grid. Plugins
    stamp their fingerprints with one of these values, and unmatched
    devices fall through to ``UNKNOWN``.
    """
    ROUTER = "router"
    TV = "tv"
    STREAMER = "streamer"        # Chromecast / Fire TV / Roku dongles
    CAMERA = "camera"
    SMART_PLUG = "smart_plug"
    SMART_BULB = "smart_bulb"
    SMART_SWITCH = "smart_switch"
    THERMOSTAT = "thermostat"
    HVAC = "hvac"                 # AC units
    APPLIANCE = "appliance"       # fridge / washer / dryer / oven
    SPEAKER = "speaker"
    VACUUM = "vacuum"
    HUB = "hub"                   # HA, Node-RED, Zigbee2MQTT, HomeBridge
    PRINTER = "printer"
    NAS = "nas"
    DOORBELL = "doorbell"
    LOCK = "lock"
    EV_CHARGER = "ev_charger"
    SOLAR = "solar"
    POOL = "pool"
    PHONE = "phone"
    COMPUTER = "computer"
    UNKNOWN = "unknown"


class Confidence(int, Enum):
    """Buckets used when displaying match confidence.

    The engine works with raw 0-100 ints; these are the labels the UI
    picks up for the pill colour.
    """
    LOW = 30       # single weak signal (OUI only, port only)
    MEDIUM = 60    # two independent signals
    HIGH = 85      # strong signal (mDNS TXT with model, UPnP dd.xml)
    CERTAIN = 95   # protocol handshake succeeded with model name


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionCategory(str, Enum):
    """Playbook action categories.

    ``check_safe`` and ``recon`` never mutate state and can be run
    without confirmation. ``exploit_control`` toggles device state.
    ``exploit_destructive`` might brick the device or corrupt its
    config; the UI must double-prompt. ``persistence`` drops a
    credential/artifact for later reuse.
    """
    CHECK_SAFE = "check_safe"
    RECON = "recon"
    EXPLOIT_CONTROL = "exploit_control"
    EXPLOIT_DESTRUCTIVE = "exploit_destructive"
    PERSISTENCE = "persistence"


@dataclass
class Port:
    """One TCP/UDP port observed open on a host."""
    number: int
    protocol: str = "tcp"          # "tcp" | "udp"
    service: str = ""              # short label ("http", "kasa", ...)
    banner: str = ""               # first bytes of banner or HTTP body
    last_seen: float = field(default_factory=time.time)

    def key(self) -> tuple:
        return (self.number, self.protocol)


@dataclass
class Fingerprint:
    """One fingerprint rule that matched this device.

    The scoring engine can attach several fingerprints to the same
    device (a Kasa plug can match kasa-udp-9999 + oui-tplink + port-9999
    at once); we keep all of them so the detail view can show the
    breakdown to the user.
    """
    plugin_id: str                 # "kasa", "shelly-gen1", ...
    matcher_name: str              # which rule inside the plugin fired
    confidence: int                # 0-100 from the plugin's yaml
    extracted: dict[str, Any] = field(default_factory=dict)


@dataclass
class Vuln:
    """One vulnerability the vuln scanner attached to this device.

    Multiple sources contribute (nuclei -jsonl output, version-compare
    against the embedded CVE DB, playbook check_safe steps). Each keeps
    its own ``source`` and ``raw`` so the audit log can prove where the
    finding came from.
    """
    vuln_id: str                   # slug -- "no-auth-http", "cve-2023-1389"
    title: str
    severity: Severity
    cve: str = ""
    source: str = "internal"       # "internal" | "nuclei" | "playbook-check"
    detected_at: float = field(default_factory=time.time)
    evidence: str = ""             # short human-readable proof
    raw: dict[str, Any] = field(default_factory=dict)  # nuclei jsonl etc.


@dataclass
class Credential:
    """A username/password (or token) known to work against a device.

    ``source`` says how we got it: ``default_creds`` (from wordlist),
    ``bruteforce`` (from hydra output), ``dumped`` (found in a config
    dump), ``manual`` (user typed it), ``session`` (obtained after
    running a playbook auth step).
    """
    service: str                   # "http-admin", "ssh", "telnet", "adb"
    username: str = ""
    password: str = ""
    token: str = ""                # for bearer / session cookies
    source: str = "manual"
    found_at: float = field(default_factory=time.time)


@dataclass
class AttackRun:
    """A single execution of a playbook action against a device."""
    playbook_id: str
    action_id: str
    started_at: float
    ended_at: float = 0.0
    status: str = "pending"        # "pending" | "ok" | "fail" | "timeout" | "cancelled"
    extracted_data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)   # file paths
    output: str = ""


@dataclass
class Device:
    """Canonical device object; MAC is the primary key.

    ``ip`` changes across DHCP renews so we never key on it. Two scans a
    week apart against the same MAC merge into one row in the registry
    with updated ``last_seen`` and possibly a new ``last_ip``.

    ``device_type`` and ``vendor``/``model`` reflect the highest-
    confidence fingerprint. Everything the plugins found gets kept in
    ``fingerprints`` for transparency.
    """
    mac: str                              # "AA:BB:CC:DD:EE:FF" upper
    ip: str = ""
    hostname: str = ""
    vendor: str = ""
    model: str = ""
    firmware: str = ""
    device_type: DeviceType = DeviceType.UNKNOWN
    confidence: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    ports: list[Port] = field(default_factory=list)
    fingerprints: list[Fingerprint] = field(default_factory=list)
    vulns: list[Vuln] = field(default_factory=list)
    credentials: list[Credential] = field(default_factory=list)
    attack_history: list[AttackRun] = field(default_factory=list)

    # Free-form bucket for plugin-specific metadata (Kasa device_id,
    # miIO token when we obtain it, Chromecast build_version, ...).
    # Never used by the core; plugins read/write here.
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- helpers ---------------------------------------------------
    def has_port(self, port: int, protocol: str = "tcp") -> bool:
        return any(p.number == port and p.protocol == protocol
                   for p in self.ports)

    def get_port(self, port: int, protocol: str = "tcp") -> Port | None:
        for p in self.ports:
            if p.number == port and p.protocol == protocol:
                return p
        return None

    def add_port(self, port: Port) -> None:
        existing = self.get_port(port.number, port.protocol)
        if existing is None:
            self.ports.append(port)
        else:
            existing.last_seen = port.last_seen
            if port.banner and not existing.banner:
                existing.banner = port.banner
            if port.service and not existing.service:
                existing.service = port.service

    def add_fingerprint(self, fp: Fingerprint) -> None:
        # De-dup by (plugin_id, matcher_name). If we already have one
        # from the same matcher, keep the higher-confidence version.
        for existing in self.fingerprints:
            if (existing.plugin_id == fp.plugin_id
                    and existing.matcher_name == fp.matcher_name):
                if fp.confidence > existing.confidence:
                    existing.confidence = fp.confidence
                    existing.extracted.update(fp.extracted)
                return
        self.fingerprints.append(fp)

    def best_confidence(self) -> int:
        """Highest-confidence match across all fingerprints."""
        if not self.fingerprints:
            return 0
        return max(f.confidence for f in self.fingerprints)

    def add_vuln(self, v: Vuln) -> None:
        for existing in self.vulns:
            if existing.vuln_id == v.vuln_id:
                return
        self.vulns.append(v)

    def add_credential(self, c: Credential) -> None:
        for existing in self.credentials:
            if (existing.service == c.service
                    and existing.username == c.username
                    and existing.password == c.password
                    and existing.token == c.token):
                return
        self.credentials.append(c)

    def label(self) -> str:
        """Best human-readable label for the UI."""
        if self.hostname:
            return self.hostname
        if self.vendor and self.model:
            return "%s %s" % (self.vendor, self.model)
        if self.vendor:
            return self.vendor
        if self.model:
            return self.model
        return self.ip or self.mac
