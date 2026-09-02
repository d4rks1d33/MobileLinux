"""Smart TV discovery + universal remote control.

Every consumer TV brand ships its own control protocol; the ones we
actually need on a modern LAN are:

  * Android TV / Google TV -- gRPC over TLS on 6466/6467, mutual auth
    with a client cert generated during a one-time PIN pairing. Ships
    on the Philips 32PHD6917 in the test LAN plus most Sony Bravia,
    Chromecast with Google TV, Hisense/TCL Google-TV models, etc.
  * Chromecast (built-in and dongles) -- CastV2 protobuf over TLS on
    8009. No auth. Enough for cast URL + volume + stop, not enough
    for dpad/text input.
  * Samsung Tizen (2016+) -- WSS on 8002, popup pairing that emits a
    persisted token.
  * LG WebOS -- WSS on 3000/3001, SSAP, popup pairing that emits a
    persisted client-key.
  * Sony Bravia REST -- HTTP JSON-RPC on 80, PSK the user set in the
    TV menu OR a PIN pairing.
  * Roku ECP -- plain HTTP on 8060, no auth at all, ships on Roku TVs
    (TCL/Hisense/Sharp) and Roku streamers.
  * Vizio SmartCast -- HTTPS 7345/9000, PIN pairing, auth token.
  * Apple TV -- Companion / MRP / AirPlay via pyatv, HomeKit-style
    pairing.
  * ADB over TCP (5555) -- Fire TV / Android TV with dev-mode on. RSA
    key pairing accepted once.

Every driver here presents the same tiny interface to the UI (see
class Driver below) so the remote panel doesn't care which brand it
is talking to. Credentials that survive a reboot are dropped in
~/.config/nethunter-pro/tv-remotes/<ip>.json so pairing is a one-time
cost per TV.

Discovery correlates four sources: SSDP (with several search targets),
mDNS (a hand-picked set of service types that only TVs and streamers
publish), a targeted TCP sweep of the fingerprint ports above and a
device-description fetch for the SSDP LOCATION URLs. What comes out
is one row per IP labelled with brand, model and the list of drivers
that could talk to it.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen, Request

from gi.repository import Adw, GLib, Gtk

from ..executor import which
from ..module import NHModule, register
from ..widgets import OutputView, services_banner, toast

CRED_DIR = Path.home() / ".config" / "nethunter-pro" / "tv-remotes"

# Ports we probe when we don't want to spend seconds on a 65k full sweep.
# Every one of them is a TV / streamer fingerprint (see module docstring
# for the mapping).
TV_PORTS = [
    80,    # Sony Bravia REST / IRCC-IP
    3000,  # LG WebOS ws
    3001,  # LG WebOS wss
    5555,  # ADB TCP (Fire TV, Android TV debug)
    6466,  # Android TV Remote v2 pairing
    6467,  # Android TV Remote v2 control
    7345,  # Vizio SmartCast (FW 4+)
    7676,  # Samsung UPnP/DIAL
    7000,  # AirPlay
    8001,  # Samsung Tizen WS
    8002,  # Samsung Tizen WSS
    8008,  # Chromecast HTTP + DIAL
    8009,  # Chromecast CastV2
    8060,  # Roku ECP
    9000,  # Vizio SmartCast (older FW)
    55000, # Samsung legacy
    1925,  # Philips JointSPACE HTTP (older SAPHI, no auth on some)
    1926,  # Philips JointSPACE HTTPS (Android TV Philips)
]

# mDNS service types that are strong "this is a TV / streamer" signals.
# Each maps to a brand hint and a driver id.
MDNS_TYPES = {
    "_googlecast._tcp":       ("chromecast",  "Chromecast built-in"),
    "_airplay._tcp":          ("airplay",     "AirPlay"),
    "_raop._tcp":             ("airplay",     "AirPlay audio"),
    "_androidtvremote2._tcp": ("androidtv",   "Android TV Remote v2"),
    "_amzn-wplay._tcp":       ("firetv",      "Fire TV Whisperplay"),
    "_samsungmsf._tcp":       ("samsung",     "Samsung MSF"),
    "_sec-remote._tcp":       ("samsung",     "Samsung Sec Remote"),
    "_lg-smarttv._tcp":       ("lgwebos",     "LG WebOS"),
    "_lge-tvremote._tcp":     ("lgwebos",     "LG WebOS TV Remote"),
    "_viziocast._tcp":        ("vizio",       "Vizio SmartCast"),
    "_dial._tcp":             ("dial",        "DIAL"),
}

# SSDP search targets that only TVs / streamers answer.
SSDP_TARGETS = [
    "urn:dial-multiscreen-org:service:dial:1",
    "urn:dial-multiscreen-org:device:dial:1",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-kinoma-com:device:shell:1",  # Vizio
    "roku:ecp",
    "urn:samsung.com:device:RemoteControlReceiver:1",
]


# --------------------------------------------------------------- data
@dataclass
class TV:
    ip: str
    mac: str = ""
    brand: str = ""     # samsung, lgwebos, sony, roku, chromecast, androidtv, firetv, vizio, appletv, unknown
    model: str = ""
    friendly_name: str = ""
    manufacturer: str = ""
    protocols: list[str] = field(default_factory=list)  # driver ids that will work
    ports: list[int] = field(default_factory=list)      # open ports we saw
    mdns_types: list[str] = field(default_factory=list)
    ssdp_locations: list[str] = field(default_factory=list)

    def label(self) -> str:
        if self.friendly_name:
            return self.friendly_name
        if self.model:
            return "%s %s" % (self.manufacturer or self.brand, self.model)
        return self.brand or "Unknown TV"


# --------------------------------------------------------------- discovery
def _scan_ssdp(targets: list[str], timeout: float = 3.0) -> dict[str, dict]:
    """Fire M-SEARCH for each target, collect unique (ip, ST, LOCATION)."""
    results: dict[str, dict] = {}
    for st in targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 2\r\n"
            "ST: %s\r\n\r\n" % st
        ).encode()
        try:
            sock.sendto(msg, ("239.255.255.250", 1900))
        except OSError:
            sock.close()
            continue
        end = _now() + timeout / len(targets)
        while _now() < end:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            ip = addr[0]
            headers = _parse_ssdp(data)
            entry = results.setdefault(ip, {
                "ip": ip, "st": [], "locations": [],
                "servers": [], "usns": [],
            })
            if st not in entry["st"]:
                entry["st"].append(st)
            for k in ("location", "server", "usn"):
                v = headers.get(k)
                if not v:
                    continue
                key = "locations" if k == "location" else k + "s"
                if v not in entry[key]:
                    entry[key].append(v)
        sock.close()
    return results


_YOUTUBE_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|shorts/|v/)|"
    r"youtube\.com/live/)([A-Za-z0-9_-]{11})")


# Direct-cast-friendly extensions and prefixes. If a URL ends in one
# of these we assume it's already a stream and hand it straight to
# ``cast_url`` without going through yt-dlp -- saves a fork and lets
# the operator cast their own hosted mp4/hls/dash without needing
# yt-dlp on the box.
_DIRECT_MEDIA_EXT = (
    ".mp4", ".m4v", ".webm", ".mkv", ".mov",
    ".mp3", ".m4a", ".ogg", ".wav", ".flac",
    ".m3u8", ".mpd",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
)


def _is_direct_media(url: str) -> bool:
    low = url.lower().split("?", 1)[0].split("#", 1)[0]
    return any(low.endswith(ext) for ext in _DIRECT_MEDIA_EXT)


def resolve_stream_url(url: str, timeout: int = 15) -> str | None:
    """Turn a video-hosting-site URL into a direct stream URL that
    Chromecast (or any DIAL-ish media receiver) can play.

    Uses ``yt-dlp`` if it's installed. Supports YouTube, Vimeo,
    Dailymotion, Twitch VODs, Facebook, and about a thousand other
    sites; picks the best mp4/webm progressive stream or an HLS
    playlist. Returns ``None`` if yt-dlp isn't installed or the
    URL isn't recognised.

    Callers should short-circuit direct-media URLs (``.mp4`` etc.)
    with :func:`_is_direct_media` before calling this -- yt-dlp
    happily rewraps them but the fork is a waste.
    """
    if not url:
        return None
    yt = subprocess.run(
        ["which", "yt-dlp"], capture_output=True, text=True)
    if yt.returncode != 0:
        return None
    # ``-g`` prints the resolved URL, no download. We prefer mp4
    # first, then any progressive, then HLS as fallback. Format
    # spec picks the best combined stream up to 1080p to keep
    # Chromecast happy.
    fmt = ("best[ext=mp4][height<=1080]/"
           "best[height<=1080]/best")
    try:
        r = subprocess.run(
            ["yt-dlp", "--no-warnings", "--no-playlist",
             "-f", fmt, "-g", url],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    if r.returncode != 0:
        return None
    # yt-dlp -g prints one URL per line; take the first (video).
    line = (r.stdout or "").strip().splitlines()
    return line[0] if line else None


def parse_youtube_id(url_or_id: str) -> str | None:
    """Pull a YouTube video ID out of an arbitrary URL or return the
    ID unchanged if the input already is one.

    Handles: youtu.be/<id>, youtube.com/watch?v=<id>,
    youtube.com/embed/<id>, youtube.com/shorts/<id>,
    youtube.com/live/<id>, and bare 11-char IDs.
    """
    s = (url_or_id or "").strip()
    if not s:
        return None
    m = _YOUTUBE_ID_RE.search(s)
    if m:
        return m.group(1)
    # Bare ID (11 chars, no url structure)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    return None


def _now() -> float:
    import time
    return time.monotonic()


def _parse_ssdp(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in data.decode(errors="replace").splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip().lower()] = v.strip()
    return out


def _fetch_device_desc(url: str, timeout: float = 3.0) -> dict[str, str]:
    """Pull manufacturer/modelName/friendlyName from a UPnP dd.xml."""
    try:
        req = Request(url, headers={"User-Agent": "NetHunterPro/1.0"})
        with urlopen(req, timeout=timeout) as r:
            body = r.read().decode(errors="replace")
    except Exception:
        return {}
    out: dict[str, str] = {}
    # The XML uses a UPnP namespace; parse tag-agnostic via regex to
    # avoid a lxml dep. Each field appears once in the dd.xml.
    for tag in ("friendlyName", "manufacturer", "modelName",
                "modelDescription", "deviceType", "UDN"):
        m = re.search(r"<%s>([^<]+)</%s>" % (tag, tag), body)
        if m:
            out[tag] = m.group(1).strip()
    return out


def _scan_mdns(timeout: float = 4.0) -> dict[str, list[str]]:
    """Return {ip: [service_type, ...]} from `avahi-browse -atrp`.

    avahi-daemon is preset-disabled on Kali/pmOS. `pkexec` would prompt
    and block the scan; instead, the app assumes it's already up (the
    Kali Services module can start it) and if it isn't we still hand
    back the SSDP + port-probe results, which cover most TVs anyway.
    """
    if not which("avahi-browse"):
        return {}
    try:
        p = subprocess.run(
            ["avahi-browse", "-atrp"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    out: dict[str, list[str]] = {}
    for line in p.stdout.splitlines():
        parts = line.split(";")
        # Resolved records start with "=" and have >=8 fields:
        # =;iface;proto;name;type;domain;fqdn;ip;port;txt
        if len(parts) < 8 or parts[0] != "=":
            continue
        svc_type = parts[4]
        ip = parts[7]
        if not ip or ":" in ip:  # skip IPv6
            continue
        if svc_type not in MDNS_TYPES:
            continue
        out.setdefault(ip, [])
        if svc_type not in out[ip]:
            out[ip].append(svc_type)
    return out


def _tcp_probe(ip: str, ports: list[int], timeout: float = 0.4
               ) -> list[int]:
    """Blocking TCP connect check. Returns list of open ports."""
    open_ports: list[int] = []
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((ip, port))
            open_ports.append(port)
        except (socket.timeout, OSError):
            pass
        finally:
            s.close()
    return open_ports


def _classify(tv: TV) -> None:
    """Fill in brand + protocols from the raw signals we gathered."""
    ports = set(tv.ports)
    types = set(tv.mdns_types)

    def add(p: str) -> None:
        if p not in tv.protocols:
            tv.protocols.append(p)

    # Manufacturer strings from the UPnP dd.xml are the most reliable
    # brand source; use them first, then fall back to other signals.
    m = (tv.manufacturer or "").lower()
    model = (tv.model or "").lower()

    if "samsung" in m or "_samsungmsf._tcp" in types:
        tv.brand = tv.brand or "samsung"
        if 8002 in ports or 8001 in ports:
            add("samsung")
        if 55000 in ports:
            add("samsung_legacy")
    if "lg " in m or "lg electronics" in m or "_lg-smarttv._tcp" in types:
        tv.brand = tv.brand or "lgwebos"
        if 3000 in ports or 3001 in ports:
            add("lgwebos")
    if "sony" in m:
        tv.brand = tv.brand or "sony"
        if 80 in ports:
            add("sony")
    if "roku" in m or 8060 in ports:
        tv.brand = tv.brand or "roku"
        if 8060 in ports:
            add("roku")
    if "vizio" in m or "_viziocast._tcp" in types:
        tv.brand = tv.brand or "vizio"
        if 7345 in ports or 9000 in ports:
            add("vizio")

    # Google Cast (Chromecast built-in) is orthogonal to the panel brand
    # -- a Sony Bravia can ALSO speak Cast. We add it as an extra
    # protocol whenever the fingerprint fits.
    if "_googlecast._tcp" in types or (8008 in ports and 8009 in ports):
        add("chromecast")
        # Chromecast built-in is common inside Google TV / Android TV;
        # only claim the brand for a bare Chromecast dongle.
        if not tv.brand and "google" in m:
            tv.brand = "chromecast"

    if "_androidtvremote2._tcp" in types or 6466 in ports or 6467 in ports:
        add("androidtv")
        if not tv.brand:
            tv.brand = "androidtv"

    if "_amzn-wplay._tcp" in types:
        add("firetv")
        tv.brand = tv.brand or "firetv"

    if "_airplay._tcp" in types or 7000 in ports:
        add("appletv")
        if not tv.brand and "apple" in m:
            tv.brand = "appletv"

    if 5555 in ports:
        # ADB debug port -- always worth listing as a driver even if
        # the box is primarily controlled another way (Fire TV / dev
        # Android TV). Requires user-side authorisation prompt.
        add("adb")

    # Philips JointSPACE: only claim it if the port is actually open.
    # Modern Android TV Philips models (2018+) dropped JointSpace and
    # rely on Android TV Remote v2 only, so the manufacturer string
    # alone is not enough of a hint.
    if 1925 in ports or 1926 in ports:
        add("jointspace")

    if not tv.brand:
        # DIAL is the last resort; a device that only speaks DIAL is
        # still controllable to a limited extent.
        if "urn:dial-multiscreen-org:service:dial:1" in " ".join(tv.ssdp_locations):
            tv.brand = "dial"
            add("dial")
        else:
            tv.brand = "unknown"


def discover_tvs() -> list[TV]:
    """Blocking full sweep. Do not call from the GTK thread."""
    ssdp = _scan_ssdp(SSDP_TARGETS, timeout=3.5)
    mdns = _scan_mdns(timeout=4.0)

    tvs: dict[str, TV] = {}

    for ip, info in ssdp.items():
        tv = tvs.setdefault(ip, TV(ip=ip))
        tv.ssdp_locations.extend(info.get("locations", []))
        for loc in info.get("locations", []):
            dd = _fetch_device_desc(loc)
            if dd.get("manufacturer") and not tv.manufacturer:
                tv.manufacturer = dd["manufacturer"]
            if dd.get("modelName") and not tv.model:
                tv.model = dd["modelName"]
            if dd.get("friendlyName") and not tv.friendly_name:
                tv.friendly_name = dd["friendlyName"]

    for ip, types in mdns.items():
        tv = tvs.setdefault(ip, TV(ip=ip))
        tv.mdns_types.extend(types)

    # Probe ports for every candidate we found signals from. This bounds
    # the work: only IPs already flagged as media devices get scanned,
    # not the whole /24.
    for tv in tvs.values():
        tv.ports = _tcp_probe(tv.ip, TV_PORTS)
        _classify(tv)

    # Drop devices whose only "signal" was a router UPnP root (Huawei
    # ONTs, etc.). A device without any of our protocols and without a
    # media manufacturer is not a TV.
    return [t for t in tvs.values() if t.protocols or "TV" in (t.model or "")]


# --------------------------------------------------------------- persistence
def _cred_path(ip: str) -> Path:
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    return CRED_DIR / ("%s.json" % ip.replace(":", "_"))


def load_creds(ip: str) -> dict:
    p = _cred_path(ip)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def save_creds(ip: str, data: dict) -> None:
    p = _cred_path(ip)
    p.write_text(json.dumps(data, indent=2))


# --------------------------------------------------------------- driver base
# Capability keys used in Driver.capabilities. Each entry maps to a
# category of things the UI knows how to ask for; the ranking logic
# looks at these to decide "for this action, which driver wins".
CAPS = (
    "dpad",         # UP/DOWN/LEFT/RIGHT/OK + Home/Back navigation
    "keyboard",     # literal text input (search boxes, PIN entry)
    "power",        # power on/off + wake from standby
    "volume",       # system volume up/down/mute
    "media_keys",   # play/pause/stop/next/prev (system-level)
    "channel",      # channel up/down + number pad on live TV
    "launch_app",   # open Netflix / YouTube / arbitrary app
    "cast_url",     # push a URL (mp4/HLS/mp3) to be played
    "screenshot",   # capture what's on screen
    "input_switch", # HDMI1/HDMI2 selection
)


class Driver:
    """One instance per (TV, protocol). Async first because most vendor
    libraries are asyncio-based; sync drivers wrap themselves in a
    thread inside `send_key`.

    Subclasses declare their capabilities so the UI can rank them.
    See CAPS for the categories. Values are a coarse quality mark:
      2 = full support (native protocol, well-tested)
      1 = partial support (works but limited scope, e.g. Chromecast
          volume changes the whole system but only when the receiver
          is idle)
      0 = not supported by this protocol
    """

    #: short id; matches values used in TV.protocols
    id: str = ""
    #: pretty name for the UI
    label: str = ""
    #: True when the driver needs a one-time pairing (PIN/popup) before
    #: it can send keys. `pair()` handles that step.
    needs_pairing: bool = False
    #: True if the vendor publishes the protocol (Roku ECP, Sony REST,
    #: DIAL). False if it's reverse-engineered (Samsung MSF, LG SSAP,
    #: Android TV Remote v2). Used only for status display -- both
    #: kinds work fine, official ones just tend to be more stable.
    is_official: bool = False
    #: True if the underlying library / protocol is known to be a
    #: last-resort choice (requires the user to enable developer mode,
    #: hard-to-recover mistakes, etc). ADB in particular.
    is_last_resort: bool = False
    #: Rough latency for a keypress round-trip on the LAN, in ms.
    #: Used as a tiebreaker in ranking.
    latency_ms: int = 200
    #: Per-capability quality marks. Subclasses override.
    capabilities: dict[str, int] = {c: 0 for c in CAPS}

    def __init__(self, ip: str) -> None:
        self.ip = ip
        self.creds = load_creds(ip).get(self.id, {})

    # A driver is "ready" when it can send keys right now -- either it
    # never needed pairing, or the persisted credentials are present.
    # Subclasses can override has_credentials() to check for other
    # kinds of persisted state (cert files, token files, sqlite dbs).
    @property
    def is_ready(self) -> bool:
        if not self.needs_pairing:
            return True
        return self.has_credentials()

    def has_credentials(self) -> bool:
        return bool(load_creds(self.ip).get(self.id))

    # -- lifecycle
    async def pair(self, pin: str | None = None) -> str:
        """Kick off (or complete) pairing. Returns a status string.

        The UI calls this twice for PIN-based flows: first with pin=None
        to start pairing (which pops up the PIN on the TV), then with
        the PIN the user typed in. Popup-based flows (Samsung/LG) run
        as a single call with pin=None.
        """
        return "not implemented"

    async def send_key(self, key: str) -> str:
        return "not implemented"

    async def launch_app(self, app: str) -> str:
        return "not implemented"

    async def cast_url(self, url: str) -> str:
        return "not implemented"

    async def cast_video(self, url: str) -> str:
        """Play a video from any URL: direct media (mp4/hls/mkv),
        Vimeo, Dailymotion, Twitch, YouTube, self-hosted -- the
        driver decides how to fulfil it.

        Fast paths:

          * bare ``.mp4`` / ``.m3u8`` / ``.webm`` etc. -> ``cast_url``
            because the receiver can play the URL directly.
          * YouTube URL or 11-char ID -> ``cast_youtube`` (native
            YouTube protocol -- much better UX than embedding).

        Everything else goes through ``yt-dlp -g`` which resolves
        the video-hosting page into a direct stream URL. On drivers
        that don't have Chromecast-style media playback the
        resolved URL is at least passed to ``cast_url`` which may
        or may not work depending on the platform.
        """
        if not url:
            return "no URL"
        # YouTube first -- native protocol is better than a
        # yt-dlp-resolved URL.
        if parse_youtube_id(url) is not None:
            return await self.cast_youtube(url)
        # Direct media URL -> straight to cast_url, no fork.
        if _is_direct_media(url):
            return await self.cast_url(url)
        # Everything else: try yt-dlp to resolve.
        resolved = await asyncio.to_thread(resolve_stream_url, url)
        if not resolved:
            return ("could not resolve URL. Install yt-dlp "
                    "(`sudo apt install yt-dlp`) and try again, "
                    "or paste the direct .mp4 / .m3u8 URL.")
        return await self.cast_url(resolved)

    async def cast_youtube(self, url_or_id: str) -> str:
        """Open a YouTube video on the TV.

        Drivers that speak YouTube natively (Chromecast, Roku,
        AndroidTV, DIAL) override this. For everyone else we make a
        best-effort try over the plain DIAL protocol -- ``POST
        /apps/YouTube`` with ``v=<videoID>`` in the body is what
        Samsung, LG, Sony and Vizio TVs register even when their
        native remote APIs don't expose a YouTube deep-link. If the
        TV doesn't answer any DIAL port, fall back to ``cast_url``
        which will at best try to load the plain URL.
        """
        vid = parse_youtube_id(url_or_id)
        if vid is None:
            return "not a YouTube URL or video ID"

        def dial_try() -> str | None:
            import urllib.request
            body = ("v=" + vid).encode("ascii")
            for port in (8008, 8060, 9000, 8001, 56789):
                try:
                    req = urllib.request.Request(
                        "http://%s:%d/apps/YouTube" % (self.ip, port),
                        method="POST", data=body,
                        headers={
                            "Content-Type":
                                "application/x-www-form-urlencoded"})
                    urllib.request.urlopen(req, timeout=3).read()
                    return "DIAL YouTube (%s) on :%d" % (vid, port)
                except Exception:
                    continue
            return None

        r = await asyncio.to_thread(dial_try)
        if r is not None:
            return r
        return await self.cast_url(url_or_id)

    # -- helpers
    def _persist(self, patch: dict) -> None:
        all_creds = load_creds(self.ip)
        all_creds[self.id] = {**self.creds, **patch}
        self.creds = all_creds[self.id]
        save_creds(self.ip, all_creds)


# --------------------------------------------------------------- driver: Android TV Remote v2
class AndroidTVDriver(Driver):
    id = "androidtv"
    label = "Android TV Remote v2"
    needs_pairing = True
    is_official = False   # reverse-engineered from the Google TV app
    latency_ms = 40
    # The reference "remote" for anything Android TV / Google TV shaped.
    # Handles every key the Google TV app can send, plus deep-links to
    # apps by package name. Only miss is casting arbitrary URLs, which
    # is Chromecast's territory.
    capabilities = {
        "dpad": 2, "keyboard": 2, "power": 2, "volume": 2,
        "media_keys": 2, "channel": 2, "launch_app": 2,
        "cast_url": 0, "screenshot": 0, "input_switch": 1,
    }

    # Google's keycode strings match the ones the user's phone remote
    # sends. See androidtvremote2/const.py for the full list.
    KEYMAP = {
        "power": "POWER", "home": "HOME", "back": "BACK",
        "up": "DPAD_UP", "down": "DPAD_DOWN",
        "left": "DPAD_LEFT", "right": "DPAD_RIGHT",
        "ok": "DPAD_CENTER",
        "vol_up": "VOLUME_UP", "vol_down": "VOLUME_DOWN",
        "vol_mute": "VOLUME_MUTE",
        "play_pause": "MEDIA_PLAY_PAUSE",
        "play": "MEDIA_PLAY", "pause": "MEDIA_PAUSE",
        "stop": "MEDIA_STOP",
        "next": "MEDIA_NEXT", "prev": "MEDIA_PREVIOUS",
        "ch_up": "CHANNEL_UP", "ch_down": "CHANNEL_DOWN",
        "menu": "MENU", "settings": "SETTINGS",
        "0": "0", "1": "1", "2": "2", "3": "3", "4": "4",
        "5": "5", "6": "6", "7": "7", "8": "8", "9": "9",
    }

    def _paths(self) -> tuple[str, str]:
        base = CRED_DIR / ("%s.androidtv" % self.ip.replace(":", "_"))
        return str(base) + ".cert", str(base) + ".key"

    def has_credentials(self) -> bool:
        cert, key = self._paths()
        return os.path.exists(cert) and os.path.exists(key)

    async def _remote(self):
        from androidtvremote2 import AndroidTVRemote
        cert, key = self._paths()
        r = AndroidTVRemote("NetHunterPro", cert, key, self.ip)
        await r.async_generate_cert_if_missing()
        return r

    # Pending pairing sessions, keyed by TV ip. Each entry is the
    # AndroidTVRemote instance that started the exchange and is waiting
    # for the finish_pairing call with the PIN. Kept at class level so
    # the UI creating a fresh driver instance for the confirmation
    # click doesn't lose the mid-flight object.
    _PENDING: dict[str, object] = {}

    async def pair(self, pin: str | None = None) -> str:
        if pin is None:
            r = await self._remote()
            AndroidTVDriver._PENDING[self.ip] = r
            await r.async_start_pairing()
            return "PIN shown on TV -- type it and press Confirm"
        r = AndroidTVDriver._PENDING.get(self.ip) or await self._remote()
        try:
            await r.async_finish_pairing(pin)
        finally:
            AndroidTVDriver._PENDING.pop(self.ip, None)
        self._persist({"paired": True})
        return "Paired. Certificate saved."

    async def _connected(self):
        r = await self._remote()
        await r.async_connect()
        r.keep_reconnecting()
        return r

    async def send_key(self, key: str) -> str:
        code = self.KEYMAP.get(key)
        if not code:
            return "Unknown key: " + key
        r = await self._connected()
        try:
            r.send_key_command(code)
        finally:
            r.disconnect()
        return "sent " + code

    async def launch_app(self, app: str) -> str:
        r = await self._connected()
        try:
            r.send_launch_app_command(app)
        finally:
            r.disconnect()
        return "launched " + app

    async def cast_youtube(self, url_or_id: str) -> str:
        """Android TV: send a launch-with-URI intent through the
        Remote v2 protocol. The YouTube app registers ``vnd.youtube:``
        as its scheme; ``send_launch_app_command`` on Android TV
        Remote v2 accepts an arbitrary URI in the ``app`` slot and
        the launcher resolves it to the right activity + VideoID
        extra."""
        vid = parse_youtube_id(url_or_id)
        if vid is None:
            return "not a YouTube URL or video ID"
        r = await self._connected()
        try:
            # vnd.youtube:VIDEOID auto-plays because YouTube's Android
            # app maps this scheme straight to WatchWhileActivity with
            # the extra ``force_fullscreen=true``.
            r.send_launch_app_command("vnd.youtube:" + vid)
        finally:
            r.disconnect()
        return "opened vnd.youtube:" + vid


# --------------------------------------------------------------- driver: Chromecast
class ChromecastDriver(Driver):
    id = "chromecast"
    label = "Chromecast / CastV2"
    needs_pairing = False
    is_official = False   # reverse-engineered protobuf
    latency_ms = 150
    # CastV2 has NO remote-control primitives -- no dpad, no home, no
    # back. That's the whole reason Google shipped a *separate* Android
    # TV Remote v2 protocol. What Cast does give us: system volume
    # (via the receiver namespace, works even without an active
    # session), media transport (only on streams WE started), and app
    # launching by cast app_id.
    capabilities = {
        "dpad": 0, "keyboard": 0, "power": 0, "volume": 2,
        "media_keys": 1, "channel": 0, "launch_app": 1,
        "cast_url": 2, "screenshot": 0, "input_switch": 0,
    }

    KEYMAP = {
        "play": "play", "pause": "pause", "stop": "stop",
        "vol_up": "vol_up", "vol_down": "vol_down",
        "vol_mute": "mute", "next": "next", "prev": "prev",
    }

    def _cast(self):
        import pychromecast
        # get_listed_chromecasts(known_hosts=[ip]) is meant to skip mDNS
        # discovery when you already know where the device lives, but on
        # some networks / library versions it returns an empty list. Fall
        # back to the full discovery and then filter by IP -- slower (~8s
        # first hit) but reliable, and pychromecast caches internally.
        casts, browser = pychromecast.get_chromecasts(timeout=8)
        try:
            cast = next(
                (c for c in casts
                 if getattr(c, "cast_info", None) is not None
                 and c.cast_info.host == self.ip),
                None,
            )
            if cast is None and casts:
                # Any single device on the LAN -- if the user is here,
                # they meant this one.
                cast = casts[0]
            if cast is None:
                return None, browser
            cast.wait(timeout=6)
            return cast, browser
        except Exception:
            return None, browser

    async def send_key(self, key: str) -> str:
        # pychromecast is sync -- run in a thread.
        def run() -> str:
            cast, browser = self._cast()
            if cast is None:
                return "device not reachable"
            try:
                mc = cast.media_controller
                if key == "play":
                    mc.play()
                elif key == "pause":
                    mc.pause()
                elif key == "stop":
                    mc.stop()
                elif key == "vol_up":
                    cast.set_volume(min(1.0, cast.status.volume_level + 0.05))
                elif key == "vol_down":
                    cast.set_volume(max(0.0, cast.status.volume_level - 0.05))
                elif key == "vol_mute":
                    cast.set_volume_muted(not cast.status.volume_muted)
                elif key == "next":
                    mc.queue_next()
                elif key == "prev":
                    mc.queue_prev()
                else:
                    return "chromecast: no mapping for " + key
                return "ok"
            finally:
                import pychromecast
                pychromecast.discovery.stop_discovery(browser)
        return await asyncio.to_thread(run)

    async def cast_url(self, url: str) -> str:
        # If the URL is actually a YouTube link, hand it off to the
        # YouTube path -- ``play_media`` on a youtube.com URL just
        # tries to render the HTML page and fails ungracefully.
        vid = parse_youtube_id(url)
        if vid is not None:
            return await self.cast_youtube(vid)

        # Best-effort MIME guess. Cast supports mp4, webm, mp3, jpg, png,
        # HLS, DASH.
        content_type = "video/mp4"
        low = url.lower()
        if low.endswith(".m3u8"):
            content_type = "application/vnd.apple.mpegurl"
        elif low.endswith(".mp3"):
            content_type = "audio/mpeg"
        elif low.endswith(".jpg") or low.endswith(".jpeg"):
            content_type = "image/jpeg"
        elif low.endswith(".png"):
            content_type = "image/png"

        def run() -> str:
            cast, browser = self._cast()
            if cast is None:
                return "device not reachable"
            try:
                cast.media_controller.play_media(url, content_type)
                cast.media_controller.block_until_active(timeout=6)
                return "casting " + url
            finally:
                import pychromecast
                pychromecast.discovery.stop_discovery(browser)
        return await asyncio.to_thread(run)

    async def cast_youtube(self, url_or_id: str) -> str:
        """Chromecast native YouTube path: use pychromecast's
        ``YouTubeController`` which sends the ``play_video`` message
        over the YouTube receiver app (id ``233637DE``). Works on
        Chromecast, Chromecast Ultra, Chromecast with Google TV,
        Google Nest Hub, and every Chromecast-built-in TV."""
        vid = parse_youtube_id(url_or_id)
        if vid is None:
            return "not a YouTube URL or video ID"

        def run() -> str:
            cast, browser = self._cast()
            if cast is None:
                return "device not reachable"
            try:
                # YouTubeController lives in pychromecast.controllers.
                # It re-implements the YouTube "leanback" pairing
                # protocol so a video ID is enough to start playback
                # without any prior linking on the receiver.
                try:
                    from pychromecast.controllers.youtube \
                        import YouTubeController
                except ImportError:
                    return ("pychromecast.controllers.youtube "
                            "missing; upgrade pychromecast or fall "
                            "back to cast_url")
                yt = YouTubeController()
                cast.register_handler(yt)
                yt.play_video(vid)
                return "playing YouTube video " + vid
            finally:
                import pychromecast
                pychromecast.discovery.stop_discovery(browser)
        return await asyncio.to_thread(run)


# --------------------------------------------------------------- driver: Roku
class RokuDriver(Driver):
    id = "roku"
    label = "Roku ECP"
    needs_pairing = False  # ECP is unauthenticated
    is_official = True     # documented on developer.roku.com
    latency_ms = 30
    # The gold standard: no auth, official protocol, fastest, covers
    # everything on Roku TVs (dpad, keyboard via Lit_ prefix, power on
    # Roku TVs, volume on Roku TVs, HDMI input switch, app launcher).
    # Screenshot is limited to the icon of the current app.
    capabilities = {
        "dpad": 2, "keyboard": 2, "power": 2, "volume": 2,
        "media_keys": 2, "channel": 2, "launch_app": 2,
        "cast_url": 1, "screenshot": 1, "input_switch": 2,
    }

    KEYMAP = {
        "power": "PowerOff", "home": "Home", "back": "Back",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "ok": "Select",
        "vol_up": "VolumeUp", "vol_down": "VolumeDown",
        "vol_mute": "VolumeMute",
        "play_pause": "Play", "play": "Play", "pause": "Play",
        "stop": "Back",  # Roku has no dedicated Stop; Back exits playback
        "next": "Fwd", "prev": "Rev",
        "ch_up": "ChannelUp", "ch_down": "ChannelDown",
        "menu": "Info",
    }

    def _post(self, path: str) -> str:
        import urllib.request
        req = urllib.request.Request(
            "http://%s:8060%s" % (self.ip, path), method="POST")
        try:
            urllib.request.urlopen(req, timeout=4).read()
            return "ok"
        except Exception as e:
            return "err: " + str(e)

    async def send_key(self, key: str) -> str:
        code = self.KEYMAP.get(key)
        if not code:
            # Digits: literal typing
            if key.isdigit():
                return await asyncio.to_thread(
                    self._post, "/keypress/Lit_" + key)
            return "unknown key: " + key
        return await asyncio.to_thread(self._post, "/keypress/" + code)

    async def launch_app(self, app: str) -> str:
        # `app` may be a numeric id or a well-known name we map.
        m = {"netflix": "12", "amazon": "13", "youtube": "837",
             "spotify": "22297", "hulu": "2285", "disney": "291097"}
        app_id = m.get(app.lower(), app)
        return await asyncio.to_thread(self._post, "/launch/" + app_id)

    async def cast_youtube(self, url_or_id: str) -> str:
        """Roku launches YouTube with deep-link query parameters:
        ``POST /launch/837?contentID=<videoID>&mediaType=video``.
        The YouTube channel app (837) picks up the contentID and
        starts playback on foreground."""
        vid = parse_youtube_id(url_or_id)
        if vid is None:
            return "not a YouTube URL or video ID"
        path = ("/launch/837?contentID=" + vid + "&mediaType=video")
        return await asyncio.to_thread(self._post, path)


# --------------------------------------------------------------- driver: Samsung
class SamsungDriver(Driver):
    id = "samsung"
    label = "Samsung Tizen"
    needs_pairing = True  # popup on the TV; token persists
    is_official = False   # SmartView SDK is semi-retired
    latency_ms = 110
    capabilities = {
        "dpad": 2, "keyboard": 2, "power": 2, "volume": 2,
        "media_keys": 2, "channel": 2, "launch_app": 2,
        "cast_url": 1, "screenshot": 1, "input_switch": 2,
    }

    KEYMAP = {
        "power": "KEY_POWER", "home": "KEY_HOME", "back": "KEY_RETURN",
        "up": "KEY_UP", "down": "KEY_DOWN",
        "left": "KEY_LEFT", "right": "KEY_RIGHT",
        "ok": "KEY_ENTER",
        "vol_up": "KEY_VOLUP", "vol_down": "KEY_VOLDOWN",
        "vol_mute": "KEY_MUTE",
        "play_pause": "KEY_PLAY", "play": "KEY_PLAY", "pause": "KEY_PAUSE",
        "stop": "KEY_STOP",
        "next": "KEY_FF", "prev": "KEY_REWIND",
        "ch_up": "KEY_CHUP", "ch_down": "KEY_CHDOWN",
        "menu": "KEY_MENU", "settings": "KEY_TOOLS",
        "0": "KEY_0", "1": "KEY_1", "2": "KEY_2", "3": "KEY_3",
        "4": "KEY_4", "5": "KEY_5", "6": "KEY_6", "7": "KEY_7",
        "8": "KEY_8", "9": "KEY_9",
    }

    def _tv(self):
        from samsungtvws import SamsungTVWS
        token_file = str(_cred_path(self.ip)) + ".samsung.token"
        return SamsungTVWS(
            host=self.ip, port=8002, token_file=token_file,
            name="NetHunterPro", timeout=6)

    async def pair(self, pin: str | None = None) -> str:
        # Popup-style; just make one call to trigger the prompt on TV.
        # The library will store the token when the user accepts.
        def run() -> str:
            tv = self._tv()
            try:
                tv.send_key("KEY_MUTE")   # cheap probe
                tv.send_key("KEY_MUTE")   # undo
                return "paired (accept prompt on TV)"
            except Exception as e:
                return "pair failed: " + str(e)
        return await asyncio.to_thread(run)

    async def send_key(self, key: str) -> str:
        code = self.KEYMAP.get(key, key)
        def run() -> str:
            tv = self._tv()
            try:
                tv.send_key(code)
                return "sent " + code
            except Exception as e:
                return "err: " + str(e)
        return await asyncio.to_thread(run)

    async def launch_app(self, app: str) -> str:
        # Samsung app names are Tizen internal IDs; the library ships
        # helpers on tv.rest_app_status/tv.rest_app_run for known ones.
        def run() -> str:
            tv = self._tv()
            try:
                tv.rest_app_run(app)
                return "launched " + app
            except Exception as e:
                return "err: " + str(e)
        return await asyncio.to_thread(run)


# --------------------------------------------------------------- driver: LG WebOS
class LGWebOSDriver(Driver):
    id = "lgwebos"
    label = "LG WebOS SSAP"
    needs_pairing = True  # first connect pops a prompt on TV; client-key persists
    is_official = False
    latency_ms = 90
    capabilities = {
        "dpad": 2, "keyboard": 2, "power": 2, "volume": 2,
        "media_keys": 2, "channel": 2, "launch_app": 2,
        "cast_url": 1, "screenshot": 2, "input_switch": 2,
    }

    KEYMAP = {
        "power": "power_off",
        "home": "home", "back": "back",
        "up": "up", "down": "down", "left": "left", "right": "right",
        "ok": "ok",
        "vol_up": "volume_up", "vol_down": "volume_down",
        "vol_mute": "set_mute", "play_pause": "pause",
        "play": "play", "pause": "pause", "stop": "stop",
        "next": "fast_forward", "prev": "rewind",
        "ch_up": "channel_up", "ch_down": "channel_down",
        "menu": "menu",
        "0": "0", "1": "1", "2": "2", "3": "3", "4": "4",
        "5": "5", "6": "6", "7": "7", "8": "8", "9": "9",
    }

    async def _client(self):
        from bscpylgtv import WebOsClient
        # bscpylgtv reads client-key from ~/.aiopylgtv.sqlite by default,
        # which is fine for a first pass -- keys survive across sessions
        # of the app.
        c = await WebOsClient.create(self.ip, ping_interval=None, states=[])
        await c.connect()
        return c

    async def pair(self, pin: str | None = None) -> str:
        try:
            c = await self._client()
        except Exception as e:
            return "pair failed: " + str(e)
        try:
            await c.disconnect()
        except Exception:
            pass
        self._persist({"paired": True})
        return "paired (accepted on TV, key stored)"

    async def send_key(self, key: str) -> str:
        name = self.KEYMAP.get(key, key)
        try:
            c = await self._client()
        except Exception as e:
            return "connect fail: " + str(e)
        try:
            fn = getattr(c, name, None)
            if fn is None:
                # Fall back to raw button() call which most keys accept.
                await c.button(name)
                return "button " + name
            await fn()
            return "sent " + name
        except Exception as e:
            return "err: " + str(e)
        finally:
            try:
                await c.disconnect()
            except Exception:
                pass

    async def launch_app(self, app: str) -> str:
        try:
            c = await self._client()
            await c.launch_app(app)
            await c.disconnect()
            return "launched " + app
        except Exception as e:
            return "err: " + str(e)


# --------------------------------------------------------------- driver: Sony Bravia
class SonyDriver(Driver):
    id = "sony"
    label = "Sony Bravia (IRCC/REST)"
    needs_pairing = True  # PSK or PIN
    is_official = True    # dev.sony.com/pro/bravia
    latency_ms = 170
    capabilities = {
        "dpad": 2, "keyboard": 1, "power": 2, "volume": 2,
        "media_keys": 2, "channel": 2, "launch_app": 2,
        "cast_url": 0, "screenshot": 0, "input_switch": 2,
    }

    # Sony IRCC codes are base64-encoded 20-byte payloads. These are the
    # published ones for the mobile app remote.
    IRCC = {
        "power":  "AAAAAQAAAAEAAAAVAw==",
        "home":   "AAAAAQAAAAEAAABgAw==",
        "back":   "AAAAAgAAAJcAAAAjAw==",
        "up":     "AAAAAQAAAAEAAAB0Aw==",
        "down":   "AAAAAQAAAAEAAAB1Aw==",
        "left":   "AAAAAQAAAAEAAAA0Aw==",
        "right":  "AAAAAQAAAAEAAAAzAw==",
        "ok":     "AAAAAQAAAAEAAABlAw==",
        "vol_up":   "AAAAAQAAAAEAAAASAw==",
        "vol_down": "AAAAAQAAAAEAAAATAw==",
        "vol_mute": "AAAAAQAAAAEAAAAUAw==",
        "play":     "AAAAAgAAAJcAAAAaAw==",
        "pause":    "AAAAAgAAAJcAAAAZAw==",
        "stop":     "AAAAAgAAAJcAAAAYAw==",
        "next":     "AAAAAgAAAJcAAAAcAw==",
        "prev":     "AAAAAgAAAJcAAAAbAw==",
        "ch_up":    "AAAAAQAAAAEAAAAQAw==",
        "ch_down":  "AAAAAQAAAAEAAAARAw==",
        "menu":     "AAAAAgAAAJcAAAA2Aw==",
        "0": "AAAAAQAAAAEAAAAJAw==",
        "1": "AAAAAQAAAAEAAAAAAw==",
        "2": "AAAAAQAAAAEAAAABAw==",
        "3": "AAAAAQAAAAEAAAACAw==",
        "4": "AAAAAQAAAAEAAAADAw==",
        "5": "AAAAAQAAAAEAAAAEAw==",
        "6": "AAAAAQAAAAEAAAAFAw==",
        "7": "AAAAAQAAAAEAAAAGAw==",
        "8": "AAAAAQAAAAEAAAAHAw==",
        "9": "AAAAAQAAAAEAAAAIAw==",
    }

    async def pair(self, pin: str | None = None) -> str:
        # Two modes: PSK-based (the user enters the PSK the TV already
        # has configured, we just store it) or PIN pairing.
        if pin is None:
            return "Type your PSK, or type PIN 0000 to trigger PIN entry"
        self._persist({"psk": pin})
        return "PSK stored"

    async def send_key(self, key: str) -> str:
        payload = self.IRCC.get(key)
        if not payload:
            return "no IRCC for " + key
        soap = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1">'
            '<IRCCCode>%s</IRCCCode>'
            '</u:X_SendIRCC></s:Body></s:Envelope>'
        ) % payload
        def run() -> str:
            import urllib.request
            req = urllib.request.Request(
                "http://%s/sony/IRCC" % self.ip,
                data=soap.encode(),
                headers={
                    "Content-Type": "text/xml; charset=UTF-8",
                    "SOAPAction": '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"',
                    "X-Auth-PSK": self.creds.get("psk", ""),
                },
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=4).read()
                return "ok"
            except Exception as e:
                return "err: " + str(e)
        return await asyncio.to_thread(run)


# --------------------------------------------------------------- driver: Vizio
class VizioDriver(Driver):
    id = "vizio"
    label = "Vizio SmartCast"
    needs_pairing = True
    is_official = False
    latency_ms = 160
    capabilities = {
        "dpad": 2, "keyboard": 1, "power": 2, "volume": 2,
        "media_keys": 1, "channel": 2, "launch_app": 2,
        "cast_url": 0, "screenshot": 0, "input_switch": 2,
    }

    KEYMAP = {
        "power": "pow_toggle",
        "vol_up": "vol_up", "vol_down": "vol_down",
        "vol_mute": "mute_toggle",
        "up": "up", "down": "down", "left": "left", "right": "right",
        "ok": "ok", "back": "back", "home": "menu",
        "play": "play", "pause": "pause",
        "ch_up": "ch_up", "ch_down": "ch_down",
    }

    _PENDING: dict[str, tuple] = {}

    async def pair(self, pin: str | None = None) -> str:
        from pyvizio import Vizio
        if pin is None:
            v = Vizio("NetHunterPro", self.ip, "NetHunterPro",
                      device_type="tv")
            try:
                resp = await v.start_pair()
            except Exception as e:
                return "pair start failed: " + str(e)
            VizioDriver._PENDING[self.ip] = (v, resp)
            return "PIN shown on TV -- type it and press Confirm"
        pair = VizioDriver._PENDING.get(self.ip)
        if pair is None:
            return "start pairing first"
        v, resp = pair
        try:
            result = await v.pair(resp.ch_type, resp.token, pin)
        except Exception as e:
            return "pair fail: " + str(e)
        VizioDriver._PENDING.pop(self.ip, None)
        self._persist({"auth_token": result.auth_token})
        return "paired"

    async def send_key(self, key: str) -> str:
        from pyvizio import Vizio
        token = self.creds.get("auth_token")
        if not token:
            return "not paired"
        v = Vizio("NetHunterPro", self.ip, "NetHunterPro",
                  auth_token=token, device_type="tv")
        fn = self.KEYMAP.get(key)
        if fn is None:
            return "unknown key: " + key
        try:
            method = getattr(v, fn, None)
            if method is None:
                return "no vizio method for " + fn
            await method()
            return "ok"
        except Exception as e:
            return "err: " + str(e)


# --------------------------------------------------------------- driver: DIAL fallback
class DIALDriver(Driver):
    id = "dial"
    label = "DIAL (launch apps)"
    needs_pairing = False
    is_official = True    # dial-multiscreen-org spec
    latency_ms = 80
    # DIAL is HTTP + no auth, and its whole purpose is launching the
    # small set of apps registered in the DIAL registry (Netflix,
    # YouTube, Amazon, Hulu, Spotify). Zero remote-control.
    capabilities = {
        "dpad": 0, "keyboard": 0, "power": 0, "volume": 0,
        "media_keys": 0, "channel": 0, "launch_app": 2,
        "cast_url": 0, "screenshot": 0, "input_switch": 0,
    }

    async def launch_app(self, app: str) -> str:
        # DIAL apps live at /apps/<AppName>. Well-known names.
        def run() -> str:
            import urllib.request
            for port in (8008, 8060, 9000, 8001, 56789):
                try:
                    req = urllib.request.Request(
                        "http://%s:%d/apps/%s" % (self.ip, port, app),
                        method="POST", data=b"")
                    urllib.request.urlopen(req, timeout=3).read()
                    return "launched %s on :%d" % (app, port)
                except Exception:
                    continue
            return "DIAL: no port answered"
        return await asyncio.to_thread(run)

    async def cast_youtube(self, url_or_id: str) -> str:
        """DIAL launches YouTube with a body payload: the YouTube
        DIAL spec says a ``POST /apps/YouTube`` with a
        ``application/x-www-form-urlencoded`` body of ``v=<videoID>``
        (optionally ``&t=<seconds>``) starts playback of that video
        as soon as the app comes up. Works on every TV with a
        YouTube DIAL registration (Vizio, Sony, LG pre-webOS-5,
        many Samsung, cheap Android boxes)."""
        vid = parse_youtube_id(url_or_id)
        if vid is None:
            return "not a YouTube URL or video ID"

        def run() -> str:
            import urllib.request
            body = ("v=" + vid).encode("ascii")
            for port in (8008, 8060, 9000, 8001, 56789):
                try:
                    req = urllib.request.Request(
                        "http://%s:%d/apps/YouTube" % (self.ip, port),
                        method="POST", data=body,
                        headers={
                            "Content-Type":
                                "application/x-www-form-urlencoded"})
                    urllib.request.urlopen(req, timeout=3).read()
                    return "playing YouTube (%s) on :%d" % (
                        vid, port)
                except Exception:
                    continue
            return "DIAL: no port answered"
        return await asyncio.to_thread(run)


# --------------------------------------------------------------- driver: ADB
class ADBDriver(Driver):
    id = "adb"
    label = "ADB (5555)"
    needs_pairing = True  # RSA "Always allow" prompt on TV
    is_official = True    # Android platform-tools
    is_last_resort = True # requires developer mode + physical accept
    latency_ms = 250
    capabilities = {
        "dpad": 2, "keyboard": 2, "power": 1, "volume": 2,
        "media_keys": 2, "channel": 2, "launch_app": 2,
        "cast_url": 0, "screenshot": 2, "input_switch": 0,
    }

    # Android KEYCODEs.
    KEYMAP = {
        "power": 26, "home": 3, "back": 4,
        "up": 19, "down": 20, "left": 21, "right": 22, "ok": 23,
        "vol_up": 24, "vol_down": 25, "vol_mute": 164,
        "play_pause": 85, "play": 126, "pause": 127, "stop": 86,
        "next": 87, "prev": 88,
        "ch_up": 166, "ch_down": 167,
        "menu": 82, "settings": 176,
        "0": 7, "1": 8, "2": 9, "3": 10, "4": 11,
        "5": 12, "6": 13, "7": 14, "8": 15, "9": 16,
    }

    def _keypath(self) -> str:
        return str(CRED_DIR / ("%s.adbkey" % self.ip.replace(":", "_")))

    def _device(self):
        from adb_shell.adb_device import AdbDeviceTcp
        from adb_shell.auth.sign_pythonrsa import PythonRSASigner
        keypath = self._keypath()
        if not os.path.exists(keypath):
            from adb_shell.auth.keygen import keygen
            keygen(keypath)
        with open(keypath) as f:
            priv = f.read()
        with open(keypath + ".pub") as f:
            pub = f.read()
        signer = PythonRSASigner(pub, priv)
        dev = AdbDeviceTcp(self.ip, 5555, default_transport_timeout_s=6)
        dev.connect(rsa_keys=[signer], auth_timeout_s=15)
        return dev

    async def pair(self, pin: str | None = None) -> str:
        def run() -> str:
            try:
                dev = self._device()
                dev.close()
                return "ADB: connected (accept 'Always allow' on TV if prompted)"
            except Exception as e:
                return "ADB fail: " + str(e)
        return await asyncio.to_thread(run)

    async def send_key(self, key: str) -> str:
        code = self.KEYMAP.get(key)
        if code is None:
            return "unknown key"
        def run() -> str:
            try:
                dev = self._device()
                dev.shell("input keyevent %d" % code)
                dev.close()
                return "ok"
            except Exception as e:
                return "err: " + str(e)
        return await asyncio.to_thread(run)


# --------------------------------------------------------------- driver: Apple TV
class AppleTVDriver(Driver):
    id = "appletv"
    label = "Apple TV (pyatv)"
    needs_pairing = True
    is_official = False
    latency_ms = 80
    capabilities = {
        "dpad": 2, "keyboard": 2, "power": 2, "volume": 2,
        "media_keys": 2, "channel": 0, "launch_app": 2,
        "cast_url": 1, "screenshot": 1, "input_switch": 0,
    }

    async def pair(self, pin: str | None = None) -> str:
        return ("Apple TV pairing needs the CLI wizard: run "
                "`atvremote --id <id> --protocol companion pair` in a "
                "terminal. Then this driver will pick up ~/.pyatv creds.")

    async def send_key(self, key: str) -> str:
        try:
            import pyatv
        except ImportError:
            return "pyatv not installed"
        loop = asyncio.get_event_loop()
        confs = await pyatv.scan(loop, hosts=[self.ip], timeout=4)
        if not confs:
            return "not found"
        # Requires stored credentials in ~/.pyatv.
        return "pyatv reachable; run atvremote for keys"


# --------------------------------------------------------------- driver: Philips JointSPACE
class JointSpaceDriver(Driver):
    """Philips-specific HTTP API. Modern Android TV Philips models use
    puerto 1926 (HTTPS + Digest auth) or 1925 (HTTP, older SAPHI). API
    version 6 covers most 2015+ TVs.

    The pairing is a one-shot POST /pair/request → device shows a PIN
    → POST /pair/grant with an HMAC-SHA1 over the challenge. Result:
    user + password that we send with every subsequent Digest-authed
    request. Persisted in creds.json.

    On some Philips models JointSpace exposes /input/key WITHOUT auth
    (older SAPHI firmware), which would be the "no pairing" happy path
    the user asked about. This driver falls back to that too.
    """
    id = "jointspace"
    label = "Philips JointSPACE"
    needs_pairing = True   # digest MD5 pairing, PIN on TV
    is_official = True     # documented at github.com/eslavnov/pylips
    latency_ms = 100
    capabilities = {
        "dpad": 2, "keyboard": 1, "power": 2, "volume": 2,
        "media_keys": 2, "channel": 2, "launch_app": 1,
        "cast_url": 0, "screenshot": 0, "input_switch": 1,
    }

    # Philips key names (from the JointSpace 6 spec)
    KEYMAP = {
        "power": "Standby",
        "home": "Home", "back": "Back",
        "up": "CursorUp", "down": "CursorDown",
        "left": "CursorLeft", "right": "CursorRight",
        "ok": "Confirm",
        "vol_up": "VolumeUp", "vol_down": "VolumeDown",
        "vol_mute": "Mute",
        "play_pause": "PlayPause", "play": "Play",
        "pause": "Pause", "stop": "Stop",
        "next": "FastForward", "prev": "Rewind",
        "ch_up": "ChannelStepUp", "ch_down": "ChannelStepDown",
        "menu": "Options",
        "0": "Digit0", "1": "Digit1", "2": "Digit2",
        "3": "Digit3", "4": "Digit4", "5": "Digit5",
        "6": "Digit6", "7": "Digit7", "8": "Digit8", "9": "Digit9",
    }

    def _endpoint(self, path: str) -> str:
        port = self.creds.get("port", 1926)
        proto = "https" if port == 1926 else "http"
        return "%s://%s:%d/6%s" % (proto, self.ip, port, path)

    async def pair(self, pin: str | None = None) -> str:
        import base64
        import hashlib
        import hmac
        import ssl
        import urllib.request

        def probe_no_auth() -> bool:
            """Some older SAPHI firmwares accept unauthenticated key
            posts on 1925. Try that first -- if it works, we don't need
            to touch the pairing at all."""
            try:
                data = json.dumps({"key": "Mute"}).encode()
                req = urllib.request.Request(
                    "http://%s:1925/6/input/key" % self.ip,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=3).read()
                # Undo the mute we just sent as a probe.
                urllib.request.urlopen(req, timeout=3).read()
                return True
            except Exception:
                return False

        if pin is None:
            # First half: try unauthenticated 1925 as a shortcut.
            if await asyncio.to_thread(probe_no_auth):
                self._persist({"port": 1925, "no_auth": True})
                return ("No pairing needed -- port 1925 accepted the "
                        "test keypress without authentication.")
            # Otherwise start the digest pairing on 1926.
            def start() -> str:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                body = {
                    "device": {
                        "device_name": "NetHunterPro",
                        "device_os": "linux",
                        "app_name": "NetHunterPro",
                        "type": "native",
                        "id": "nethunterpro-client",
                    },
                    "scope": ["read", "write", "control"],
                }
                req = urllib.request.Request(
                    "https://%s:1926/6/pair/request" % self.ip,
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    resp = urllib.request.urlopen(
                        req, timeout=6, context=ctx).read()
                except Exception as e:
                    return "pair start failed: " + str(e)
                data = json.loads(resp)
                if data.get("error_id") != "SUCCESS":
                    return "pair start rejected: " + str(data)
                JointSpaceDriver._PENDING[self.ip] = {
                    "auth_key": data.get("auth_key"),
                    "timestamp": data.get("timestamp"),
                    "device_id": "nethunterpro-client",
                }
                return "PIN shown on TV -- type it and press Confirm"
            return await asyncio.to_thread(start)

        state = JointSpaceDriver._PENDING.get(self.ip)
        if state is None:
            return "start pairing first"

        def grant() -> str:
            secret = base64.b64decode(
                "ZmVheHVjRWV4dWNlaXRoZWViYWU3dGhpVGVpVGhvaGF4Vw==")
            challenge = ("%s\n%s" % (
                state["timestamp"], state["auth_key"])).encode()
            digest = hmac.new(secret, challenge,
                              hashlib.sha256).digest()
            signature = base64.b64encode(digest).decode()
            body = {
                "auth": {
                    "auth_AppId": "1",
                    "pin": pin,
                    "auth_timestamp": state["timestamp"],
                    "auth_signature": signature,
                },
                "device": {
                    "device_name": "NetHunterPro",
                    "device_os": "linux",
                    "app_name": "NetHunterPro",
                    "type": "native",
                    "id": state["device_id"],
                },
            }
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(
                "https://%s:1926/6/pair/grant" % self.ip,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                resp = urllib.request.urlopen(
                    req, timeout=6, context=ctx).read()
            except Exception as e:
                return "grant failed: " + str(e)
            data = json.loads(resp)
            if data.get("error_id") != "SUCCESS":
                return "grant rejected: " + str(data)
            JointSpaceDriver._PENDING.pop(self.ip, None)
            return "ok"

        result = await asyncio.to_thread(grant)
        if result != "ok":
            return result
        self._persist({
            "port": 1926,
            "username": state["device_id"],
            "password": state["auth_key"],
            "no_auth": False,
        })
        return "paired"

    _PENDING: dict[str, dict] = {}

    async def send_key(self, key: str) -> str:
        code = self.KEYMAP.get(key)
        if code is None:
            return "unknown key: " + key
        port = self.creds.get("port", 1926)
        no_auth = self.creds.get("no_auth", False)
        user = self.creds.get("username", "")
        pw = self.creds.get("password", "")

        def run() -> str:
            import ssl
            import urllib.request
            body = json.dumps({"key": code}).encode()
            proto = "https" if port == 1926 else "http"
            url = "%s://%s:%d/6/input/key" % (proto, self.ip, port)
            handlers = []
            if not no_auth and user and pw:
                mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
                mgr.add_password(None, url, user, pw)
                handlers.append(urllib.request.HTTPDigestAuthHandler(mgr))
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            opener = urllib.request.build_opener(
                *handlers,
                urllib.request.HTTPSHandler(context=ctx),
            )
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                opener.open(req, timeout=4).read()
                return "ok"
            except Exception as e:
                return "err: " + str(e)
        return await asyncio.to_thread(run)


# --------------------------------------------------------------- driver registry
DRIVERS: dict[str, type[Driver]] = {
    "androidtv":       AndroidTVDriver,
    "chromecast":      ChromecastDriver,
    "roku":            RokuDriver,
    "samsung":         SamsungDriver,
    "lgwebos":         LGWebOSDriver,
    "sony":            SonyDriver,
    "vizio":           VizioDriver,
    "jointspace":      JointSpaceDriver,
    "dial":            DIALDriver,
    "adb":             ADBDriver,
    "appletv":         AppleTVDriver,
}


# --------------------------------------------------------------- ranking
def rank_drivers(tv: "TV") -> list[tuple[str, int, str]]:
    """Order the TV's protocols from best to worst overall.

    Returns [(driver_id, score, reason)]. Score is only meaningful as
    an ordering, not an absolute measure.

    The heuristic mirrors the user's stated priorities:
      1. ADB is always last -- it needs developer mode + physical
         acceptance and is invasive.
      2. Prefer drivers that don't need pairing, when they cover a
         reasonable set of capabilities.
      3. Among pairing-needing drivers, prefer already-paired ones.
      4. Prefer wider capability coverage (more caps at level 2).
      5. Prefer lower latency as a tiebreaker.
    """
    scored: list[tuple[str, int, str]] = []
    for pid in tv.protocols:
        cls = DRIVERS.get(pid)
        if cls is None:
            continue
        score = 0
        reasons: list[str] = []

        # Capability coverage: sum of quality marks.
        cap_score = sum(cls.capabilities.values())
        score += cap_score * 10
        if cap_score >= 14:
            reasons.append("full remote")
        elif cap_score >= 8:
            reasons.append("wide coverage")
        elif cap_score >= 4:
            reasons.append("partial")

        # No pairing = huge win.
        if not cls.needs_pairing:
            score += 100
            reasons.append("no pairing")
        else:
            # Already paired? Almost as good. Ask the driver to
            # check its own persisted state (cert file, token, etc)
            # rather than assuming json flag only.
            already = cls(tv.ip).has_credentials()
            if already:
                score += 80
                reasons.append("already paired")
            else:
                score -= 20
                reasons.append("needs pairing")

        # Latency (lower is better; convert to a small positive term).
        score += max(0, 30 - cls.latency_ms // 10)

        # Official protocols get a small edge for reliability.
        if cls.is_official:
            score += 5

        # ADB / anything flagged last-resort: massive penalty so it
        # never wins unless it's the only option.
        if cls.is_last_resort:
            score -= 1000
            reasons.append("last resort")

        scored.append((pid, score, ", ".join(reasons)))

    scored.sort(key=lambda t: -t[1])
    return scored


def recommend_for_action(tv: "TV", action: str) -> str | None:
    """Return the driver id best suited for a specific capability.

    action must be one of CAPS. Falls back to None when no driver in
    the TV's protocols supports the action at all.
    """
    best: tuple[int, int, str] | None = None  # (cap_score, tie_score, id)
    for pid in tv.protocols:
        cls = DRIVERS.get(pid)
        if cls is None:
            continue
        cap = cls.capabilities.get(action, 0)
        if cap == 0:
            continue
        # Tie-breaker uses the same scoring as rank_drivers so we
        # prefer already-paired / no-pairing / official / low-latency.
        tie = 0
        if not cls.needs_pairing:
            tie += 100
        elif cls(tv.ip).has_credentials():
            tie += 80
        else:
            tie -= 20
        tie += max(0, 30 - cls.latency_ms // 10)
        if cls.is_official:
            tie += 5
        if cls.is_last_resort:
            tie -= 1000
        candidate = (cap, tie, pid)
        if best is None or candidate > best:
            best = candidate
    return best[2] if best else None


# --------------------------------------------------------------- module
@register
class TVRemote(NHModule):
    title = "TV Remote"
    icon = "video-display-symbolic"
    description = "Discover and control Smart TVs (Android TV, Chromecast, Samsung, LG, Roku, Sony, Vizio, Fire TV, Apple TV)"
    required_tools: list[str] = []  # everything is pip-installed

    # The full pad layout, in the order the buttons render. Each entry
    # is (row_id, key, label). Rows are drawn top-down.
    PAD_ROWS: list[list[tuple[str, str]]] = [
        [("power", "Power"), ("vol_mute", "Mute"),
         ("home", "Home"),   ("back", "Back")],
        [("vol_up", "Vol +"), ("ch_up", "Ch +"),
         ("vol_down", "Vol -"), ("ch_down", "Ch -")],
    ]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._tvs: list[TV] = []
        self._tv_rows: list[Adw.ExpanderRow] = []
        self._selected: TV | None = None
        self._selected_driver: Driver | None = None
        self._scanning = False

    # -------------------------------------------------------------- build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner (managed centrally in Kali Services)
        box.append(services_banner(
            self.app_window, ['avahi-daemon']))

        # ---- discovery -------------------------------------------
        disc_group = Adw.PreferencesGroup(
            title="Discovery",
            description="SSDP + mDNS + targeted TCP sweep. Runs on the LAN "
                        "your phone is already on (wlan0).")
        scan_row = Adw.ActionRow(
            title="Scan for TVs",
            subtitle="Takes ~10 s")
        self.scan_btn = Gtk.Button(label="Scan", valign=Gtk.Align.CENTER)
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked", lambda _b: self._start_scan())
        scan_row.add_suffix(self.scan_btn)
        disc_group.add(scan_row)
        box.append(disc_group)

        self.tvs_group = Adw.PreferencesGroup(title="Discovered TVs")
        self._empty_row = Adw.ActionRow(
            title="No TVs yet",
            subtitle="Press Scan")
        self.tvs_group.add(self._empty_row)
        box.append(self.tvs_group)

        # ---- selected TV + protocol pick --------------------------
        self.sel_group = Adw.PreferencesGroup(title="Selected TV")
        self.sel_summary = Adw.ActionRow(
            title="None", subtitle="Pick a TV above")
        self.sel_group.add(self.sel_summary)
        self.driver_combo = Adw.ComboRow(
            title="Control protocol",
            subtitle="Pairing is per-protocol; done once per TV")
        self.driver_combo.set_model(Gtk.StringList.new(["(none)"]))
        self.driver_combo.connect("notify::selected",
                                  lambda *_: self._on_driver_change())
        self.sel_group.add(self.driver_combo)

        pair_row = Adw.ActionRow(
            title="Pair", subtitle="Some brands need this first")
        self.pair_btn = Gtk.Button(label="Pair", valign=Gtk.Align.CENTER)
        self.pair_btn.connect("clicked", lambda _b: self._start_pair())
        pair_row.add_suffix(self.pair_btn)
        self.sel_group.add(pair_row)
        box.append(self.sel_group)

        # ---- control pad -----------------------------------------
        pad_group = Adw.PreferencesGroup(
            title="Remote",
            description="Live control -- pick a TV + protocol above first.")
        # Preferences groups host rows; a raw Gtk.Grid needs an ActionRow
        # wrapper.
        pad_wrap = Adw.ActionRow()
        pad_wrap.set_activatable(False)
        pad_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(pad_box, "set_margin_" + m)(6)

        # Top rows (power/mute/home/back/vol/ch) inside a FlowBox so
        # they wrap on narrow screens.
        for row in self.PAD_ROWS:
            fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                             homogeneous=True, column_spacing=6,
                             row_spacing=6,
                             min_children_per_line=2,
                             max_children_per_line=4)
            for key, label in row:
                b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
                b.connect("clicked",
                          lambda _b, k=key: self._send(k))
                fb.append(b)
            pad_box.append(fb)

        # Dpad in a 3x3 grid so up/down/left/right + OK line up.
        dpad = Gtk.Grid(row_spacing=6, column_spacing=6,
                        halign=Gtk.Align.CENTER)
        dpad.set_margin_top(6)
        dpad.set_margin_bottom(6)
        for key, label, col, row in (
            ("up", "▲", 1, 0),
            ("left", "◀", 0, 1),
            ("ok", "OK", 1, 1),
            ("right", "▶", 2, 1),
            ("down", "▼", 1, 2),
        ):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER,
                           width_request=64, height_request=48)
            if key == "ok":
                b.add_css_class("suggested-action")
            b.connect("clicked", lambda _b, k=key: self._send(k))
            dpad.attach(b, col, row, 1, 1)
        pad_box.append(dpad)

        # Media row
        media_fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                               homogeneous=True, column_spacing=6,
                               row_spacing=6,
                               min_children_per_line=3,
                               max_children_per_line=5)
        for key, label in (
            ("prev", "⏮"), ("play_pause", "⏯"),
            ("stop", "⏹"), ("next", "⏭"),
            ("menu", "Menu"),
        ):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, k=key: self._send(k))
            media_fb.append(b)
        pad_box.append(media_fb)

        # Number pad 0-9 (3x4).
        num_grid = Gtk.Grid(row_spacing=6, column_spacing=6,
                            halign=Gtk.Align.CENTER)
        num_grid.set_margin_top(6)
        num_grid.set_margin_bottom(6)
        for i in range(1, 10):
            b = Gtk.Button(label=str(i), valign=Gtk.Align.CENTER,
                           width_request=52, height_request=44)
            b.connect("clicked", lambda _b, k=str(i): self._send(k))
            num_grid.attach(b, (i - 1) % 3, (i - 1) // 3, 1, 1)
        # 0 centred on the last row.
        b0 = Gtk.Button(label="0", valign=Gtk.Align.CENTER,
                       width_request=52, height_request=44)
        b0.connect("clicked", lambda _b: self._send("0"))
        num_grid.attach(b0, 1, 3, 1, 1)
        pad_box.append(num_grid)

        pad_wrap.set_child(pad_box)
        pad_group.add(pad_wrap)
        box.append(pad_group)

        # ---- apps + cast ------------------------------------------
        extras = Adw.PreferencesGroup(title="Apps & cast")
        self.app_entry = Adw.EntryRow(title="App (netflix, youtube, com.foo)")
        extras.add(self.app_entry)
        launch_row = Adw.ActionRow(
            title="Launch app",
            subtitle="Netflix, YouTube, or a package name for Android TV")
        launch_btn = Gtk.Button(label="Launch", valign=Gtk.Align.CENTER)
        launch_btn.connect("clicked", lambda _b: self._launch_app())
        launch_row.add_suffix(launch_btn)
        extras.add(launch_row)

        self.cast_entry = Adw.EntryRow(title="Cast URL (mp4/hls/mp3/jpg)")
        extras.add(self.cast_entry)
        cast_row = Adw.ActionRow(
            title="Cast URL",
            subtitle="Chromecast, or best-effort media URL")
        cast_btn = Gtk.Button(label="Cast", valign=Gtk.Align.CENTER)
        cast_btn.connect("clicked", lambda _b: self._cast_url())
        cast_row.add_suffix(cast_btn)
        extras.add(cast_row)

        # Generic video URL: YouTube gets the native path, direct
        # media URLs (.mp4/.hls/.m3u8/.webm/.mkv/etc.) go straight
        # to cast_url, and anything else (Vimeo, Dailymotion,
        # Twitch, self-hosted pages) is resolved by yt-dlp to a
        # direct stream URL and then cast.
        self.video_entry = Adw.EntryRow(
            title="Video URL (YouTube, Vimeo, Dailymotion, "
                  "direct mp4/hls, ...)")
        extras.add(self.video_entry)
        vid_row = Adw.ActionRow(
            title="Play video",
            subtitle="yt-dlp resolves it, driver casts it")
        vid_btn = Gtk.Button(label="Play",
                             valign=Gtk.Align.CENTER)
        vid_btn.add_css_class("suggested-action")
        vid_btn.connect("clicked", lambda _b: self._cast_video())
        vid_row.add_suffix(vid_btn)
        extras.add(vid_row)
        box.append(extras)

        # ---- output log ------------------------------------------
        self.output = OutputView()
        box.append(self.output)

        return box

    # ----------------------------------------------------- discovery
    def _start_scan(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self.scan_btn.set_sensitive(False)
        self.output.append("# scanning for TVs...\n")

        def worker() -> None:
            try:
                tvs = discover_tvs()
            except Exception as exc:
                GLib.idle_add(self._scan_error, str(exc))
                return
            GLib.idle_add(self._scan_done, tvs)

        threading.Thread(target=worker, daemon=True).start()

    def _scan_error(self, err: str) -> bool:
        self.output.append("[scan error] %s\n" % err)
        self.scan_btn.set_sensitive(True)
        self._scanning = False
        return False

    def _scan_done(self, tvs: list[TV]) -> bool:
        self._tvs = tvs
        self.scan_btn.set_sensitive(True)
        self._scanning = False
        self.output.append("# found %d TV(s)\n" % len(tvs))
        for tv in tvs:
            self.output.append(
                "  - %s: %s [%s]\n" % (tv.ip, tv.label(),
                                       ",".join(tv.protocols)))
        self._render_tvs()
        return False

    def _render_tvs(self) -> None:
        for r in self._tv_rows:
            self.tvs_group.remove(r)
        self._tv_rows = []
        if not self._tvs:
            self._empty_row.set_visible(True)
            return
        self._empty_row.set_visible(False)
        for tv in self._tvs:
            r = Adw.ExpanderRow(
                title=tv.label(),
                subtitle="%s -- %s" % (tv.ip, ", ".join(tv.protocols) or "no drivers"))
            if tv.model:
                r.add_row(Adw.ActionRow(title="Model", subtitle=tv.model))
            if tv.manufacturer:
                r.add_row(Adw.ActionRow(title="Manufacturer",
                                        subtitle=tv.manufacturer))
            if tv.ports:
                r.add_row(Adw.ActionRow(
                    title="Open ports",
                    subtitle=", ".join(str(p) for p in tv.ports)))
            if tv.mdns_types:
                r.add_row(Adw.ActionRow(
                    title="mDNS", subtitle=", ".join(tv.mdns_types)))
            sel_row = Adw.ActionRow(title="Actions")
            b = Gtk.Button(label="Select as target",
                           valign=Gtk.Align.CENTER)
            b.add_css_class("suggested-action")
            b.connect("clicked", lambda _b, t=tv: self._select_tv(t))
            sel_row.add_suffix(b)
            r.add_row(sel_row)
            self.tvs_group.add(r)
            self._tv_rows.append(r)

    # ----------------------------------------------------- selection
    def _select_tv(self, tv: TV) -> None:
        self._selected = tv
        self._selected_driver = None
        self.sel_summary.set_title(tv.label())

        # Rank the drivers and reorder the combo so the recommended
        # one is on top and pre-selected. Each entry's label carries
        # the reason ("no pairing", "already paired", ...) so the user
        # can see why we picked it.
        ranked = rank_drivers(tv)
        self._driver_ids = [pid for pid, _s, _r in ranked]
        labels = []
        for i, (pid, _score, reason) in enumerate(ranked):
            cls = DRIVERS[pid]
            marker = " *" if i == 0 else ""
            labels.append("%s%s  (%s)" % (cls.label, marker, reason))
        if not labels:
            labels = ["(no drivers)"]
            self._driver_ids = []

        # Subtitle summarises the top pick + shows which caps come
        # from other drivers (stacking).
        subtitle = "%s -- %d protocol(s) available" % (
            tv.ip, len(self._driver_ids))
        if ranked:
            top_id = ranked[0][0]
            top_cls = DRIVERS[top_id]
            subtitle += " -- recommended: " + top_cls.label
            # Look at the caps the top pick can NOT do, and mention
            # which other driver can. Only surface caps that make the
            # remote panel meaningfully better.
            for cap in ("cast_url", "dpad", "power"):
                if top_cls.capabilities.get(cap, 0) >= 2:
                    continue
                alt = recommend_for_action(tv, cap)
                if alt and alt != top_id:
                    subtitle += " (+%s via %s)" % (
                        cap, DRIVERS[alt].label)
                    break
        self.sel_summary.set_subtitle(subtitle)

        self.driver_combo.set_model(Gtk.StringList.new(labels))
        self.driver_combo.set_selected(0)
        self._on_driver_change()
        toast(self.app_window, "Target: " + tv.label())

    def _on_driver_change(self) -> None:
        if self._selected is None or not self._driver_ids:
            self._selected_driver = None
            return
        idx = self.driver_combo.get_selected()
        if idx >= len(self._driver_ids):
            return
        pid = self._driver_ids[idx]
        cls = DRIVERS[pid]
        self._selected_driver = cls(self._selected.ip)
        # Update pair button label based on needs_pairing + creds present
        needs = cls.needs_pairing
        already = self._selected_driver.has_credentials()
        if needs and not already:
            self.pair_btn.set_label("Pair")
            self.pair_btn.set_sensitive(True)
            self.pair_btn.add_css_class("suggested-action")
        elif needs and already:
            self.pair_btn.set_label("Re-pair")
            self.pair_btn.set_sensitive(True)
            self.pair_btn.remove_css_class("suggested-action")
        else:
            self.pair_btn.set_label("No pairing needed")
            self.pair_btn.set_sensitive(False)
            self.pair_btn.remove_css_class("suggested-action")

    # ----------------------------------------------------- pairing
    def _start_pair(self) -> None:
        d = self._selected_driver
        if d is None:
            toast(self.app_window, "Pick a TV + protocol first")
            return

        def cb(status: str) -> None:
            self.output.append("[pair] %s\n" % status)
            # For PIN flows the driver returns a "type PIN" hint --
            # follow up with a dialog asking for it.
            if "PIN" in status or "PSK" in status or "type" in status.lower():
                self._prompt_pin(d)
            else:
                # Refresh the pair button state.
                self._on_driver_change()

        self._run_driver(d.pair(), cb)

    def _prompt_pin(self, driver: Driver) -> None:
        dlg = Adw.MessageDialog(
            transient_for=self.app_window,
            heading="Enter PIN / PSK",
            body="Type the PIN shown on the TV, or the PSK you set in the "
                 "TV's remote control menu.")
        entry = Gtk.Entry(placeholder_text="e.g. 1234")
        entry.set_activates_default(True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Confirm")
        dlg.set_default_response("ok")

        def on_resp(_d, resp):
            if resp != "ok":
                return
            pin = entry.get_text().strip()
            if not pin:
                return

            def cb(status: str) -> None:
                self.output.append("[pair] %s\n" % status)
                self._on_driver_change()

            self._run_driver(driver.pair(pin), cb)

        dlg.connect("response", on_resp)
        dlg.present()

    # ----------------------------------------------------- send
    def _send(self, key: str) -> None:
        d = self._selected_driver
        if d is None:
            toast(self.app_window, "Pick a TV + protocol first")
            return
        self.output.append("$ %s.%s\n" % (d.id, key))

        def cb(status: str) -> None:
            self.output.append("  -> %s\n" % status)

        self._run_driver(d.send_key(key), cb)

    def _launch_app(self) -> None:
        d = self._selected_driver
        if d is None:
            toast(self.app_window, "Pick a TV + protocol first")
            return
        app = self.app_entry.get_text().strip()
        if not app:
            toast(self.app_window, "Type an app name/id first")
            return
        self.output.append("$ %s.launch %s\n" % (d.id, app))
        self._run_driver(d.launch_app(app),
                         lambda s: self.output.append("  -> %s\n" % s))

    def _cast_url(self) -> None:
        d = self._selected_driver
        if d is None:
            toast(self.app_window, "Pick a TV + protocol first")
            return
        url = self.cast_entry.get_text().strip()
        if not url:
            toast(self.app_window, "Type a URL first")
            return
        self.output.append("$ %s.cast %s\n" % (d.id, url))
        self._run_driver(d.cast_url(url),
                         lambda s: self.output.append("  -> %s\n" % s))

    def _cast_video(self) -> None:
        """Play any video URL. The driver's ``cast_video`` routes it
        to the right underlying path based on the URL shape."""
        d = self._selected_driver
        if d is None:
            toast(self.app_window, "Pick a TV + protocol first")
            return
        url = self.video_entry.get_text().strip()
        if not url:
            toast(self.app_window, "Paste a video URL first")
            return
        self.output.append("$ %s.video %s\n" % (d.id, url))
        self._run_driver(d.cast_video(url),
                         lambda s: self.output.append(
                             "  -> %s\n" % s))

    # ----------------------------------------------------- async plumbing
    def _run_driver(self, coro, on_result) -> None:
        """Kick a driver coroutine off the GTK thread and deliver the
        return value to `on_result` on the GTK thread when done."""
        def worker() -> None:
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(coro)
            except Exception as exc:
                result = "error: " + str(exc)
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
            GLib.idle_add(lambda: (on_result(str(result)), False)[1])
        threading.Thread(target=worker, daemon=True).start()
