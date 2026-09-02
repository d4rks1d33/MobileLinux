"""In-memory + SQLite registry of discovered devices.

Everything the pipeline produces (Discovery emits IPs/MACs, Fingerprint
attaches labels, Vuln attaches findings, Exploit records runs) ends up
here. The UI observes the registry via GObject signals so it repaints
without having to poll.

Design points:

* MAC is the primary key -- IPs drift across DHCP renewals, MACs don't
  unless the device is randomising (which routers/TVs/IoT don't do).
* Two tiers: a live ``dict[mac, Device]`` for O(1) UI reads, and a
  SQLite database at ~/.local/share/nethunter-pro/iot/cache.db for
  persistence across sessions and audit-log queries.
* Writes go through :meth:`upsert` which merges into the live dict and
  writes-through to SQLite. Reads always hit the live dict.
* GObject signals: ``device-added``, ``device-updated``, ``device-vuln``.
  The UI connects to these and re-renders the affected row only.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from gi.repository import GLib, GObject

from .models import (
    Credential,
    Device,
    DeviceType,
    Fingerprint,
    Port,
    Vuln,
)

CACHE_DIR = Path.home() / ".local" / "share" / "nethunter-pro" / "iot"


def _schema() -> str:
    """DDL for the cache database.

    Kept minimal; the details of a device live in JSON columns so we
    don't have to migrate every time a plugin invents a new metadata
    field. The queries the UI actually runs are simple (list-by-type,
    fetch-by-mac); the JSON blobs are only decoded on demand.
    """
    return """
    CREATE TABLE IF NOT EXISTS devices (
        mac TEXT PRIMARY KEY,
        ip TEXT,
        hostname TEXT,
        vendor TEXT,
        model TEXT,
        firmware TEXT,
        device_type TEXT,
        confidence INTEGER,
        first_seen REAL,
        last_seen REAL,
        blob TEXT      -- json dump of the whole Device
    );
    CREATE INDEX IF NOT EXISTS idx_dev_type ON devices(device_type);
    CREATE INDEX IF NOT EXISTS idx_dev_ip ON devices(ip);
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL,
        actor TEXT,
        action TEXT,
        mac TEXT,
        detail TEXT
    );
    """


class DeviceRegistry(GObject.Object):
    __gtype_name__ = "IoTDeviceRegistry"

    __gsignals__ = {
        # Emitted when a MAC never seen before is inserted.
        "device-added": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # Emitted after any upsert / attach_ports / add_vuln etc.
        "device-updated": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        # Cleared / new scan started.
        "cleared": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        # The live cache. Access from any thread; the ``lock`` protects
        # concurrent modifications, and the ``_signal_from_thread``
        # helper is what pipelines use to notify the UI thread-safely.
        self._live: dict[str, Device] = {}
        self._lock = threading.Lock()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._db_path = CACHE_DIR / "cache.db"
        self._db = sqlite3.connect(
            self._db_path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        for stmt in _schema().split(";"):
            if stmt.strip():
                self._db.execute(stmt)

    # ------------------------------------------------------------ life
    def load_all(self) -> int:
        """Warm the live cache from the on-disk database.

        Called once at module startup so re-opening the tab still shows
        yesterday's finds. Returns the number of devices loaded.
        """
        rows = self._db.execute(
            "SELECT blob FROM devices ORDER BY last_seen DESC").fetchall()
        for (blob,) in rows:
            try:
                dev = self._decode(blob)
            except Exception:  # noqa: BLE001
                # Corrupted row -- skip; the next upsert will overwrite.
                continue
            self._live[dev.mac.upper()] = dev
        return len(self._live)

    def clear(self) -> None:
        """Blow away the live cache. Does not touch the on-disk DB.

        Emitted before starting a new scan when the UI's "Reset" is
        pressed. History stays queryable through the audit log.
        """
        with self._lock:
            self._live.clear()
        self._signal_from_thread("cleared")

    # --------------------------------------------------------- accessors
    def snapshot(self) -> list[Device]:
        """Copy of the live cache. Safe to iterate in the UI."""
        with self._lock:
            return list(self._live.values())

    def by_type(self, dtype: DeviceType) -> list[Device]:
        with self._lock:
            return [d for d in self._live.values()
                    if d.device_type == dtype]

    def by_mac(self, mac: str) -> Device | None:
        return self._live.get(mac.upper())

    def counts_by_type(self) -> dict[DeviceType, int]:
        counts: dict[DeviceType, int] = {}
        with self._lock:
            for d in self._live.values():
                counts[d.device_type] = counts.get(d.device_type, 0) + 1
        return counts

    # --------------------------------------------------------- mutations
    def upsert(self, dev: Device) -> None:
        """Insert or update ``dev`` in both tiers.

        Merges into an existing Device if the MAC is already known:
        keeps whichever fields have data (never blanks a good value),
        appends unseen ports/fingerprints/vulns/credentials, refreshes
        ``last_seen``.
        """
        mac = dev.mac.upper()
        added = False
        with self._lock:
            existing = self._live.get(mac)
            if existing is None:
                dev.mac = mac
                self._live[mac] = dev
                added = True
                merged = dev
            else:
                merged = self._merge(existing, dev)
        self._persist(merged)
        if added:
            self._signal_from_thread("device-added", mac)
        else:
            self._signal_from_thread("device-updated", mac)

    def attach_ports(self, mac: str, ports: list[Port]) -> None:
        mac = mac.upper()
        with self._lock:
            dev = self._live.get(mac)
            if dev is None:
                dev = Device(mac=mac)
                self._live[mac] = dev
            for p in ports:
                dev.add_port(p)
        self._persist(dev)
        self._signal_from_thread("device-updated", mac)

    def attach_fingerprint(self, mac: str, fp: Fingerprint,
                           device_type: DeviceType | None = None,
                           vendor: str = "", model: str = "",
                           firmware: str = "") -> None:
        mac = mac.upper()
        with self._lock:
            dev = self._live.get(mac)
            if dev is None:
                dev = Device(mac=mac)
                self._live[mac] = dev
            dev.add_fingerprint(fp)
            # Highest-confidence fingerprint wins the display fields.
            if fp.confidence >= dev.confidence:
                dev.confidence = fp.confidence
                if device_type is not None:
                    dev.device_type = device_type
                if vendor:
                    dev.vendor = vendor
                if model:
                    dev.model = model
                if firmware:
                    dev.firmware = firmware
                for k, v in fp.extracted.items():
                    if v and not dev.metadata.get(k):
                        dev.metadata[k] = v
        self._persist(dev)
        self._signal_from_thread("device-updated", mac)

    def add_vuln(self, mac: str, vuln: Vuln) -> None:
        mac = mac.upper()
        with self._lock:
            dev = self._live.get(mac)
            if dev is None:
                return
            dev.add_vuln(vuln)
        self._persist(dev)
        self._signal_from_thread("device-updated", mac)

    def add_credential(self, mac: str, cred: Credential) -> None:
        mac = mac.upper()
        with self._lock:
            dev = self._live.get(mac)
            if dev is None:
                return
            dev.add_credential(cred)
        self._persist(dev)
        self._signal_from_thread("device-updated", mac)

    def audit(self, actor: str, action: str, mac: str = "",
              detail: dict | None = None) -> None:
        """Append one line to the audit log."""
        self._db.execute(
            "INSERT INTO audit_log(ts, actor, action, mac, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), actor, action, mac.upper(),
             json.dumps(detail or {})))

    # ------------------------------------------------------ internals
    def _merge(self, old: Device, new: Device) -> Device:
        old.last_seen = time.time()
        if new.ip:
            old.ip = new.ip
        if new.hostname and not old.hostname:
            old.hostname = new.hostname
        if new.vendor and not old.vendor:
            old.vendor = new.vendor
        if new.model and not old.model:
            old.model = new.model
        if new.firmware and not old.firmware:
            old.firmware = new.firmware
        if (new.device_type != DeviceType.UNKNOWN
                and (old.device_type == DeviceType.UNKNOWN
                     or new.confidence > old.confidence)):
            old.device_type = new.device_type
            old.confidence = new.confidence
        for p in new.ports:
            old.add_port(p)
        for fp in new.fingerprints:
            old.add_fingerprint(fp)
        for v in new.vulns:
            old.add_vuln(v)
        for c in new.credentials:
            old.add_credential(c)
        for k, v in new.metadata.items():
            if v and not old.metadata.get(k):
                old.metadata[k] = v
        return old

    def _persist(self, dev: Device) -> None:
        blob = self._encode(dev)
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO devices "
                "(mac, ip, hostname, vendor, model, firmware, "
                "device_type, confidence, first_seen, last_seen, blob) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (dev.mac, dev.ip, dev.hostname, dev.vendor, dev.model,
                 dev.firmware, dev.device_type.value, dev.confidence,
                 dev.first_seen, dev.last_seen, blob))
        except sqlite3.Error:
            # A corrupt DB file shouldn't kill the app; live cache
            # remains authoritative.
            pass

    @staticmethod
    def _encode(dev: Device) -> str:
        def dc(o):  # dataclass or enum → json
            if isinstance(o, DeviceType):
                return o.value
            try:
                d = o.__dict__
            except AttributeError:
                return str(o)
            return d
        payload = {
            "mac": dev.mac, "ip": dev.ip, "hostname": dev.hostname,
            "vendor": dev.vendor, "model": dev.model,
            "firmware": dev.firmware,
            "device_type": dev.device_type.value,
            "confidence": dev.confidence,
            "first_seen": dev.first_seen, "last_seen": dev.last_seen,
            "ports": [p.__dict__ for p in dev.ports],
            "fingerprints": [f.__dict__ for f in dev.fingerprints],
            "vulns": [{**v.__dict__,
                       "severity": v.severity.value}
                      for v in dev.vulns],
            "credentials": [c.__dict__ for c in dev.credentials],
            "metadata": dev.metadata,
        }
        return json.dumps(payload, default=dc)

    @staticmethod
    def _decode(blob: str) -> Device:
        raw = json.loads(blob)
        dev = Device(
            mac=raw["mac"], ip=raw.get("ip", ""),
            hostname=raw.get("hostname", ""),
            vendor=raw.get("vendor", ""), model=raw.get("model", ""),
            firmware=raw.get("firmware", ""),
            device_type=DeviceType(raw.get("device_type", "unknown")),
            confidence=raw.get("confidence", 0),
            first_seen=raw.get("first_seen", time.time()),
            last_seen=raw.get("last_seen", time.time()),
            metadata=raw.get("metadata", {}),
        )
        for p in raw.get("ports", []):
            dev.ports.append(Port(**p))
        for f in raw.get("fingerprints", []):
            dev.fingerprints.append(Fingerprint(**f))
        # Vulns / credentials could contain enum values; be defensive.
        from .models import Severity   # local import to avoid cycle
        for v in raw.get("vulns", []):
            v = dict(v)
            v["severity"] = Severity(v.get("severity", "info"))
            dev.vulns.append(Vuln(**v))
        for c in raw.get("credentials", []):
            dev.credentials.append(Credential(**c))
        return dev

    def _signal_from_thread(self, name: str, *args) -> None:
        """Emit a GObject signal from any thread.

        The pipelines run in ``asyncio``/threads; GObject signals must
        be delivered on the GTK main loop or the UI will crash. Wrap
        every emit in ``GLib.idle_add``.
        """
        def emit() -> bool:
            self.emit(name, *args)
            return False
        GLib.idle_add(emit)
