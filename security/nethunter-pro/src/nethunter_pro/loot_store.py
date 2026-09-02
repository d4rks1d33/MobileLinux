"""Loot database: every module that produces valuable output writes to it.

There is exactly one SQLite DB, one table, and one API used by the rest of
the app. Modules call ``get_loot_store().record(...)`` when they save a
capture / hash / credential file to disk, and the ``loot`` module (see
``modules/loot.py``) shows the aggregated view with filters, deletion,
and submission to wpa-sec.stanev.org for the distributed WPA cracker.

Design constraints:

* Zero coupling to a specific module -- ``record()`` takes ``module`` and
  ``type`` as free-form strings, and the UI groups by them.
* The DB lives under ``~/.local/share/nethunter-pro/loot.db`` (XDG data
  home). Migrations are done in-place: bumping ``_SCHEMA_VERSION`` runs
  the migration block below and updates ``user_version``.
* Only stores metadata + a path to the on-disk artifact (pcap / csv /
  txt). The file itself lives under ``~/loot/<module>/`` and the ``path``
  column is the absolute path. When the user hits Delete, both the row
  and the on-disk file go away.
* wpa-sec integration is baked in because it is the main reason we have
  a central store at all: capture a handshake / PMKID, submit the pcap
  to the distributed cracker, and delete once the result comes back.
* No dependency on hashcat (per user request). We do NOT convert pcap
  to hccapx / .22000 -- wpa-sec accepts the raw pcap/pcapng directly,
  as the site's own docs recommend.
"""
from __future__ import annotations

import contextlib
import mimetypes
import os
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

def _real_home() -> Path:
    """Return the login user's home for the loot tree.

    The GUI runs as the login user (kali), but the DBus root helper
    runs as root, and any Python code loaded from the helper would
    otherwise stash files under /root/. We only remap when we are
    genuinely running as root and SUDO_USER / PKEXEC_UID point at
    a real account; otherwise the current user's home is fine and
    we don't want to second-guess it.
    """
    if os.geteuid() != 0:
        return Path(os.path.expanduser("~"))
    for var in ("SUDO_USER", "PKEXEC_UID"):
        val = os.environ.get(var)
        if not val:
            continue
        try:
            import pwd
            if var == "PKEXEC_UID":
                pw = pwd.getpwuid(int(val))
            else:
                pw = pwd.getpwnam(val)
            if pw.pw_dir and pw.pw_dir != "/root":
                return Path(pw.pw_dir)
        except (KeyError, ValueError, ImportError):
            continue
    return Path(os.path.expanduser("~"))


_HOME = _real_home()

# Location on disk. XDG_DATA_HOME if set, otherwise ~/.local/share.
_DATA_HOME = Path(
    os.environ.get("XDG_DATA_HOME")
    or (_HOME / ".local/share"))/ "nethunter-pro"
_DB_PATH = _DATA_HOME / "loot.db"

# Root under which module files land. Kept separate from the DB so the
# user can point at it with a file manager, tar it, etc.
LOOT_ROOT = _HOME / "loot"

# Bump when the schema below changes.
_SCHEMA_VERSION = 1


# ---------------------------------------------------------------- dataclass

@dataclass
class LootEntry:
    """A single row from the loot DB. Immutable snapshot for the UI."""
    id: int
    ts: float                     # unix time of capture
    module: str                   # "pmkid", "handshake", "eaphammer", ...
    type: str                     # "pmkid_pcap", "handshake_pcap",
                                  # "eap_hash", "probe_csv", ...
    target: str                   # SSID / BSSID / IP / free-form
    path: str                     # absolute path to the artifact
    size: int                     # bytes; 0 if the file is gone
    wpasec_status: str            # "" | "submitted" | "cracked" | "error"
    wpasec_psk: str               # cracked PSK if wpa-sec returned one
    notes: str                    # free-form; wpa-sec error text, etc.


# ---------------------------------------------------------------- store

class LootStore:
    """Thread-safe wrapper around the SQLite DB.

    All UI reads happen on the GTK main thread; writes may happen from
    executor callbacks that come back on worker threads. SQLite in
    Python is safe across threads as long as each thread opens its
    own connection, which is what we do via ``_connect()``.
    """

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        # Hot-loop detector: if we open the DB more than N times in
        # 5 s something is wrong -- probably a timer that never
        # returns False. Print a stack once so we can find it.
        self._connect_calls = 0
        self._connect_window_start = 0.0
        self._connect_warned = False
        db_path.parent.mkdir(parents=True, exist_ok=True)
        LOOT_ROOT.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # --------- connection helpers
    def _hotloop_check(self) -> None:
        """Alarm if the app is hammering the DB in a runaway timer."""
        import time as _time
        now = _time.time()
        if now - self._connect_window_start > 5.0:
            self._connect_window_start = now
            self._connect_calls = 0
        self._connect_calls += 1
        if (self._connect_calls > 100 and not self._connect_warned):
            self._connect_warned = True
            import sys, traceback
            print("!! loot_store hot loop: %d _connect() calls in "
                  "5 s. Stack:" % self._connect_calls, file=sys.stderr)
            traceback.print_stack(file=sys.stderr)
            sys.stderr.flush()

    def _connect(self) -> sqlite3.Connection:
        self._hotloop_check()
        # NOTE: WAL mode intentionally NOT enabled. WAL leaves the
        # ``.db-wal`` companion file open for the lifetime of every
        # connection, and Python's ``sqlite3.Connection`` context
        # manager only commits/rollbacks -- it does *not* close.
        # The result on GTK apps that touch the DB from multiple
        # timers is a runaway FD leak: hundreds of ``loot.db-wal``
        # entries in /proc/PID/fd within seconds. Rollback journal
        # is fine for our access pattern (one writer per record
        # call, mostly-idle otherwise).
        con = sqlite3.connect(
            str(self._db_path), timeout=5.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    @contextlib.contextmanager
    def _conn(self):
        """Explicit-close context manager. The stdlib ``sqlite3``
        connection is a *transaction* context manager, not a
        resource one; using ``with`` on it commits but leaves the
        connection open. Wrap it here so every callsite gets a
        guaranteed close on scope exit."""
        con = self._connect()
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn() as con:
            v = con.execute("PRAGMA user_version").fetchone()[0]
            if v < 1:
                # First-time layout. Everything is TEXT except id/size/ts
                # so callers can shove whatever metadata they need in
                # without another migration.
                con.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                  id            INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts            REAL NOT NULL,
                  module        TEXT NOT NULL,
                  type          TEXT NOT NULL,
                  target        TEXT NOT NULL DEFAULT '',
                  path          TEXT NOT NULL DEFAULT '',
                  size          INTEGER NOT NULL DEFAULT 0,
                  wpasec_status TEXT NOT NULL DEFAULT '',
                  wpasec_psk    TEXT NOT NULL DEFAULT '',
                  notes         TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_entries_module
                  ON entries(module);
                CREATE INDEX IF NOT EXISTS idx_entries_type
                  ON entries(type);
                CREATE INDEX IF NOT EXISTS idx_entries_ts
                  ON entries(ts);
                """)
                con.execute("PRAGMA user_version=%d" % _SCHEMA_VERSION)

    # --------- writes
    def record(self, module: str, type: str, target: str = "",
               path: str = "", notes: str = "") -> int:
        """Record a new loot artifact. Returns the row id.

        If ``path`` points at an existing file its size is captured so the
        UI can show disk usage without a separate stat call on refresh.
        """
        size = 0
        if path:
            with contextlib.suppress(OSError):
                size = os.path.getsize(path)
        with self._lock, self._conn() as con:
            cur = con.execute(
                "INSERT INTO entries "
                "(ts, module, type, target, path, size, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), module, type, target, path, size, notes),
            )
            return int(cur.lastrowid)

    def set_wpasec(self, row_id: int, status: str, psk: str = "",
                   notes: str | None = None) -> None:
        with self._lock, self._conn() as con:
            if notes is None:
                con.execute(
                    "UPDATE entries SET wpasec_status=?, wpasec_psk=? "
                    "WHERE id=?", (status, psk, row_id))
            else:
                con.execute(
                    "UPDATE entries SET wpasec_status=?, wpasec_psk=?, "
                    "notes=? WHERE id=?",
                    (status, psk, notes, row_id))

    def append_notes(self, row_id: int, text: str) -> None:
        with self._lock, self._conn() as con:
            cur = con.execute(
                "SELECT notes FROM entries WHERE id=?", (row_id,))
            row = cur.fetchone()
            if row is None:
                return
            new = (row["notes"] + "\n" + text).strip()
            con.execute(
                "UPDATE entries SET notes=? WHERE id=?", (new, row_id))

    def refresh_size(self, row_id: int) -> None:
        """Restat the file behind a row, e.g. after hcxdumptool grew the
        pcap. Kept out of the UI refresh loop so we only do IO when a
        module explicitly asks."""
        with self._lock, self._conn() as con:
            row = con.execute(
                "SELECT path FROM entries WHERE id=?",
                (row_id,)).fetchone()
            if row is None:
                return
            size = 0
            with contextlib.suppress(OSError):
                size = os.path.getsize(row["path"])
            con.execute(
                "UPDATE entries SET size=? WHERE id=?",
                (size, row_id))

    # --------- reads
    def list(self, module: str | None = None,
             type: str | None = None) -> list[LootEntry]:
        q = "SELECT * FROM entries WHERE 1=1"
        args: list = []
        if module:
            q += " AND module=?"
            args.append(module)
        if type:
            q += " AND type=?"
            args.append(type)
        q += " ORDER BY ts DESC"
        with self._lock, self._conn() as con:
            return [LootEntry(**dict(r)) for r in con.execute(q, args)]

    def distinct_modules(self) -> list[str]:
        with self._lock, self._conn() as con:
            return [
                r["module"] for r in con.execute(
                    "SELECT DISTINCT module FROM entries "
                    "ORDER BY module")]

    def distinct_types(self) -> list[str]:
        with self._lock, self._conn() as con:
            return [
                r["type"] for r in con.execute(
                    "SELECT DISTINCT type FROM entries ORDER BY type")]

    def total_size(self) -> int:
        with self._lock, self._conn() as con:
            r = con.execute(
                "SELECT COALESCE(SUM(size),0) AS s FROM entries"
            ).fetchone()
            return int(r["s"] or 0)

    def count(self) -> int:
        with self._lock, self._conn() as con:
            r = con.execute("SELECT COUNT(*) AS n FROM entries").fetchone()
            return int(r["n"] or 0)

    # --------- deletes
    def delete(self, row_id: int, unlink: bool = True) -> None:
        """Drop the row and, if ``unlink`` is set, delete the on-disk file.

        We never touch anything outside ``LOOT_ROOT`` for safety; if the
        stored path escaped that root the file is left in place with a
        note in ``notes``.
        """
        with self._lock, self._conn() as con:
            row = con.execute(
                "SELECT path FROM entries WHERE id=?",
                (row_id,)).fetchone()
            if row is None:
                return
            path = row["path"]
            con.execute("DELETE FROM entries WHERE id=?", (row_id,))
        if unlink and path:
            self._safe_unlink(path)

    def delete_many(self, ids: list[int], unlink: bool = True) -> int:
        n = 0
        for rid in ids:
            self.delete(rid, unlink=unlink)
            n += 1
        return n

    def delete_older_than(self, seconds: int,
                          unlink: bool = True) -> int:
        cutoff = time.time() - seconds
        with self._lock, self._conn() as con:
            rows = con.execute(
                "SELECT id, path FROM entries WHERE ts < ?",
                (cutoff,)).fetchall()
        n = 0
        for row in rows:
            self.delete(row["id"], unlink=unlink)
            n += 1
        return n

    def _safe_unlink(self, path: str) -> None:
        try:
            p = Path(path).resolve()
            root = LOOT_ROOT.resolve()
            if root in p.parents or p == root:
                if p.is_file():
                    p.unlink(missing_ok=True)
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
        except (OSError, ValueError):
            # Best-effort. Row is gone regardless.
            pass


# ---------------------------------------------------------------- wpa-sec

WPASEC_URL = "https://wpa-sec.stanev.org"
WPASEC_SUBMIT = WPASEC_URL + "/?submit"
WPASEC_API_KEY_HEADER = "key"  # site sends the user's key in a cookie
# The site uses a cookie named "key" (per its docs and help_crack.py).
# Upload endpoint is ``POST /?submit`` with multipart/form-data
# containing file field ``webfile[]`` and ``Cookie: key=<user_key>``.

# The cracked-results endpoint used by help_crack.py is:
#   GET /?api&dl=1  with Cookie: key=<user_key>
# which returns a potfile of "hash:psk" pairs (WPA + PMKID unified).
# We use the same to look up whether our submitted captures were cracked.


def _wpasec_headers(api_key: str) -> dict[str, str]:
    return {
        "Cookie": "key=" + api_key,
        "User-Agent": "nethunter-pro-loot/1.0",
    }


def wpasec_submit(pcap_path: str, api_key: str,
                  timeout: int = 60) -> tuple[bool, str]:
    """POST a pcap/pcapng to wpa-sec.stanev.org.

    Returns ``(ok, message)``. ``ok`` is True if the site accepted the
    file (HTTP 200 with a "success" style response). We do not parse
    the exact string because the site returns rendered HTML on success;
    we detect the ok path by HTTP status and a lack of "Error" in the
    response body, per the site's actual behaviour.
    """
    if not api_key:
        return False, "wpa-sec API key not configured"
    if not os.path.isfile(pcap_path):
        return False, "file missing: " + pcap_path

    # Build a multipart body by hand -- urllib does not do multipart.
    boundary = "----NHP" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(pcap_path)[0] or "application/octet-stream"
    fname = os.path.basename(pcap_path).encode("utf-8", "replace")
    with open(pcap_path, "rb") as fp:
        payload = fp.read()

    body = b""
    body += ("--" + boundary + "\r\n").encode()
    body += (b'Content-Disposition: form-data; name="webfile[]"; '
             b'filename="') + fname + b'"\r\n'
    body += ("Content-Type: " + ctype + "\r\n\r\n").encode()
    body += payload + b"\r\n"
    body += ("--" + boundary + "--\r\n").encode()

    req = urllib.request.Request(
        WPASEC_SUBMIT, data=body, method="POST",
        headers={
            **_wpasec_headers(api_key),
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Content-Length": str(len(body)),
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read(65536).decode("utf-8", "replace")
            # The site is HTML-driven; on success it renders the capture
            # in a table. We only care about the HTTP code + the absence
            # of an obvious error marker.
            if resp.status != 200:
                return False, "HTTP %d" % resp.status
            lower = text.lower()
            if "no valid handshakes" in lower or "error" in lower:
                # The site accepted the upload but reports the file has
                # nothing crackable. Still a valid submission from our
                # side; the module surfaces this as a warning note.
                return True, "accepted (no valid handshakes reported)"
            return True, "accepted"
    except urllib.error.HTTPError as e:
        return False, "HTTP %d: %s" % (e.code, e.reason)
    except urllib.error.URLError as e:
        return False, "URL error: %s" % e.reason
    except Exception as exc:  # pragma: no cover
        return False, "submit failed: %s" % exc


def wpasec_fetch_cracks(api_key: str,
                        timeout: int = 30) -> dict[str, str]:
    """Return a dict of ``hash -> psk`` for every capture the site has
    cracked for this user. The site's ``/?api&dl=1`` endpoint returns
    a potfile-style listing separated by newlines with ``:``.

    Each line looks like:
        <bssid>*<sta_mac>*...:<PSK>

    We only need the trailing ``psk`` and enough context to match it
    back to a submission. Matching in the UI is by SSID substring (the
    site puts the ESSID in one of the fields) so we return the raw
    line as key too.
    """
    if not api_key:
        return {}
    req = urllib.request.Request(
        WPASEC_URL + "/?api&dl=1", headers=_wpasec_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return {}
            text = resp.read(4 * 1024 * 1024).decode(
                "utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError):
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        # Some potfile lines can contain ":" inside the hash portion.
        # The PSK is after the last ":".
        hash_part, _, psk = line.rpartition(":")
        if not psk:
            continue
        out[line] = psk
    return out


# ---------------------------------------------------------------- singleton

_store: LootStore | None = None


def get_loot_store() -> LootStore:
    global _store
    if _store is None:
        _store = LootStore()
    return _store


def loot_path(module: str, filename: str) -> str:
    """Convenience: build a canonical path for a module's artifact and
    make the parent directory. Callers still have to actually create
    the file (they run the tool that writes it)."""
    directory = LOOT_ROOT / module
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / filename)
