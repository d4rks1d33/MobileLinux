"""Data-driven fingerprint scoring engine.

Every plugin ships a ``fingerprints.yaml`` alongside its ``playbook.yaml``.
The YAML describes N matcher rules, each of which contributes a score
(0-100) when it matches. The engine sums scores per rule, caps at 100,
and stamps the winning device_type/vendor/model onto the ``Device`` if
the total crosses the plugin's threshold.

The point of doing this as data rather than code is:

* Adding a new device family = drop a YAML in ``data/fingerprints/`` and
  it's picked up automatically at startup.
* The scoring rules are inspectable in the UI ("why did you think this
  is a Kasa plug?") -- we list every matched rule with its score.
* No new Python code = no new attack surface / no restart needed for
  hot reloading during development.

The matcher types accepted:

  * ``port``        port + protocol open
  * ``mac_oui``     first three octets match one of ``values``
  * ``mdns``        an ``_type._proto`` service was seen with optional
                    ``txt_regex`` on any TXT record value
  * ``ssdp``        one of ``st`` search targets responded, optional
                    ``server_regex`` on the SERVER header
  * ``http_body``   HTTP request to ``path`` returned body matching
                    ``regex`` (case-insensitive)
  * ``http_header`` HTTP request to ``path`` returned a header matching
                    ``header_regex`` (``name: value``)
  * ``udp_reply``   the UDP probe on ``port`` returned bytes matching
                    ``body_regex`` (hex or raw)
  * ``upnp_dd``     UPnP device description XML at LOCATION contains
                    ``regex`` (on manufacturer/modelName/deviceType)

Rule extracts follow the same regex; named groups end up in
``Fingerprint.extracted``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .models import Device, DeviceType, Fingerprint

FINGERPRINT_DIR = Path(__file__).resolve().parent.parent / "data" / "fingerprints"


@dataclass
class MatcherRule:
    """One rule inside a fingerprint plugin YAML."""
    name: str
    kind: str                       # "port" | "mdns" | "ssdp" | ...
    confidence: int                 # 0-100
    params: dict[str, Any] = field(default_factory=dict)
    extract: dict[str, str] = field(default_factory=dict)  # name → regex


@dataclass
class PluginFingerprint:
    """One plugin's whole fingerprint block."""
    id: str
    device_type: DeviceType
    vendor: str
    family: str = ""
    threshold: int = 60             # minimum sum to consider a match
    matchers: list[MatcherRule] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class FingerprintEngine:
    """Loads plugin YAMLs at startup, scores devices on demand.

    A single engine is shared across the app. Plugins are hot-reloaded
    when the user hits the "Reload plugins" gear menu -- useful during
    development.
    """
    def __init__(self, extra_dirs: Iterable[Path] = ()) -> None:
        self._plugins: dict[str, PluginFingerprint] = {}
        self._dirs = [FINGERPRINT_DIR, *extra_dirs]
        self.reload()

    def reload(self) -> None:
        self._plugins.clear()
        for d in self._dirs:
            if not d.exists():
                continue
            for path in sorted(d.glob("*.yaml")):
                try:
                    plugin = self._load_one(path)
                except Exception as exc:  # noqa: BLE001
                    # Bad YAML shouldn't take down the whole engine.
                    print("[fingerprint] failed to load %s: %s"
                          % (path, exc))
                    continue
                self._plugins[plugin.id] = plugin

    def plugin_ids(self) -> list[str]:
        return list(self._plugins.keys())

    def get(self, plugin_id: str) -> PluginFingerprint | None:
        return self._plugins.get(plugin_id)

    def _load_one(self, path: Path) -> PluginFingerprint:
        raw = yaml.safe_load(path.read_text())
        matchers = []
        for m in raw.get("matchers", []) or []:
            matchers.append(MatcherRule(
                name=m.get("name", "unnamed"),
                kind=m["type"],
                confidence=int(m.get("confidence", 0)),
                params={k: v for k, v in m.items()
                        if k not in ("name", "type", "confidence",
                                     "extract")},
                extract={k: v for k, v in
                         (m.get("extract") or {}).items()},
            ))
        return PluginFingerprint(
            id=raw["id"],
            device_type=DeviceType(raw.get("device_type", "unknown")),
            vendor=raw.get("vendor", ""),
            family=raw.get("family", ""),
            threshold=int(raw.get("threshold", 60)),
            matchers=matchers,
            metadata=raw.get("metadata", {}) or {},
        )

    # ---------------------------------------------------------- score
    def score(self, dev: Device,
              observations: "Observations") -> list[Fingerprint]:
        """Evaluate every plugin against a device.

        Returns the list of fingerprints that matched (fresh objects,
        not yet attached to the device). The caller (usually the
        Fingerprint stage of the pipeline) decides how to persist them
        via the registry -- see ``DeviceRegistry.attach_fingerprint``.
        """
        matches: list[Fingerprint] = []
        for plugin in self._plugins.values():
            total = 0
            hits: list[tuple[MatcherRule, dict]] = []
            for rule in plugin.matchers:
                extracted = self._eval_rule(rule, dev, observations)
                if extracted is None:
                    continue
                total += rule.confidence
                hits.append((rule, extracted))
            if total < plugin.threshold:
                continue
            total = min(total, 100)
            merged: dict[str, Any] = {}
            for _rule, extracted in hits:
                merged.update(extracted)
            # Emit one Fingerprint per hit rule so the UI can show the
            # per-rule breakdown, plus one aggregate stamped with the
            # capped total for the picker.
            for rule, extracted in hits:
                matches.append(Fingerprint(
                    plugin_id=plugin.id,
                    matcher_name=rule.name,
                    confidence=rule.confidence,
                    extracted=extracted,
                ))
            matches.append(Fingerprint(
                plugin_id=plugin.id,
                matcher_name="_aggregate",
                confidence=total,
                extracted={
                    "vendor": plugin.vendor,
                    "device_type": plugin.device_type.value,
                    "family": plugin.family,
                    **merged,
                },
            ))
        return matches

    # ---------------------------------------------------------- eval
    def _eval_rule(self, rule: MatcherRule, dev: Device,
                   obs: "Observations") -> dict[str, str] | None:
        try:
            return _MATCHERS[rule.kind](rule, dev, obs)
        except KeyError:
            return None

    def winning_plugin(self, matches: list[Fingerprint]
                       ) -> Fingerprint | None:
        """Pick the highest-confidence ``_aggregate`` from a match list."""
        best: Fingerprint | None = None
        for fp in matches:
            if fp.matcher_name != "_aggregate":
                continue
            if best is None or fp.confidence > best.confidence:
                best = fp
        return best


# --------------------------------------------------------- observations
class Observations:
    """Everything the discovery layer collected about a single device.

    Bundled up so matchers can pull whatever they need without the
    engine having to know each protocol individually. The discovery
    orchestrator fills one of these per IP/MAC and calls
    ``FingerprintEngine.score(dev, obs)``.

    All fields are best-effort; probes that timed out just leave their
    slot empty.
    """
    def __init__(self) -> None:
        # mDNS: {service_type: [{name, txt, port, ip}, ...]}
        self.mdns: dict[str, list[dict]] = {}
        # SSDP: [{st, location, server, usn}, ...]
        self.ssdp: list[dict] = []
        # UPnP dd.xml, keyed by LOCATION URL
        self.upnp_dd: dict[str, str] = {}
        # HTTP responses, keyed by full URL: {"body": str, "headers":
        # dict}
        self.http: dict[str, dict] = {}
        # UDP replies, keyed by (ip, port): raw bytes
        self.udp_reply: dict[tuple[str, int], bytes] = {}
        # SNMP sysDescr if any
        self.snmp_sysdescr: str = ""
        # nmap -O -sV output normalised: {vendor, os, os_accuracy,
        # hostname, services:[{port,service,version}]}
        self.nmap: dict = {}


# ------------------------------------------------------- matchers
def _m_port(rule: MatcherRule, dev: Device, _obs: Observations
            ) -> dict[str, str] | None:
    port = int(rule.params.get("port", 0))
    proto = rule.params.get("protocol", "tcp")
    if dev.has_port(port, proto):
        return {}
    return None


def _m_mac_oui(rule: MatcherRule, dev: Device, _obs: Observations
               ) -> dict[str, str] | None:
    values = rule.params.get("values", []) or []
    if not dev.mac:
        return None
    prefix = dev.mac.upper().replace("-", ":")[:8]  # AA:BB:CC
    for v in values:
        if v.upper().startswith(prefix.rstrip(":")):
            return {}
    # Also accept exact 3-octet prefix with dashes or colons.
    for v in values:
        if prefix == v.upper().replace("-", ":")[:8]:
            return {}
    return None


def _m_mdns(rule: MatcherRule, _dev: Device, obs: Observations
            ) -> dict[str, str] | None:
    # The YAML uses `mdns_type` because `type` is already used to name
    # the matcher kind (mdns / port / http_body / ...). Older YAMLs may
    # still have `service`; accept both for compatibility.
    svc_type = (rule.params.get("mdns_type")
                or rule.params.get("service"))
    if not svc_type:
        return None
    hits = obs.mdns.get(svc_type, [])
    if not hits:
        return None
    txt_regex = rule.params.get("txt_regex")
    extracted: dict[str, str] = {}
    for hit in hits:
        txt = hit.get("txt", "")
        if txt_regex and not re.search(txt_regex, txt, re.I):
            continue
        # Apply named-group extracts from the rule
        for field_name, regex in rule.extract.items():
            m = re.search(regex, txt, re.I)
            if m:
                extracted[field_name] = _first_group(m)
        return extracted
    return None


def _m_ssdp(rule: MatcherRule, _dev: Device, obs: Observations
            ) -> dict[str, str] | None:
    st_list = rule.params.get("st", [])
    if isinstance(st_list, str):
        st_list = [st_list]
    server_regex = rule.params.get("server_regex")
    for reply in obs.ssdp:
        st = reply.get("st", "")
        server = reply.get("server", "")
        if st_list and not any(s in st for s in st_list):
            continue
        if server_regex and not re.search(server_regex, server, re.I):
            continue
        extracted = {}
        for field_name, regex in rule.extract.items():
            m = re.search(regex, server, re.I)
            if m:
                extracted[field_name] = _first_group(m)
        return extracted
    return None


def _url_matches_path(url: str, want_path: str,
                      response: dict) -> bool:
    """Match a probed URL against a rule's ``path`` component.

    A rule can either say ``/`` (match any URL) or a specific path
    (``/api/config``). When the path is specific, we also require a
    successful response (2xx or 401) -- a 404 / 500 response body
    means the endpoint doesn't exist here and any accidental regex
    match on the error page is a false positive.
    """
    status = response.get("status", 0)
    if want_path in ("/", ""):
        # Root probe: any URL that lives at scheme://host:port/ counts.
        return url.rstrip("/").count("/") == 2
    # Specific path: URL must contain it AND the response must not be
    # an obvious error.
    if want_path not in url:
        return False
    if status and status >= 400 and status != 401:
        return False
    return True


def _m_http_body(rule: MatcherRule, _dev: Device, obs: Observations
                 ) -> dict[str, str] | None:
    path = rule.params.get("path", "/")
    regex = rule.params.get("regex")
    if not regex:
        return None
    for url, response in obs.http.items():
        if not _url_matches_path(url, path, response):
            continue
        body = response.get("body", "")
        m = re.search(regex, body, re.I)
        if not m:
            continue
        extracted = {}
        for field_name, r in rule.extract.items():
            em = re.search(r, body, re.I)
            if em:
                extracted[field_name] = _first_group(em)
        return extracted
    return None


def _m_http_header(rule: MatcherRule, _dev: Device, obs: Observations
                   ) -> dict[str, str] | None:
    path = rule.params.get("path", "/")
    regex = rule.params.get("header_regex")
    if not regex:
        return None
    for url, response in obs.http.items():
        if not _url_matches_path(url, path, response):
            continue
        headers = response.get("headers", {})
        for name, val in headers.items():
            line = "%s: %s" % (name, val)
            if re.search(regex, line, re.I):
                extracted = {}
                for field_name, r in rule.extract.items():
                    em = re.search(r, line, re.I)
                    if em:
                        extracted[field_name] = _first_group(em)
                return extracted
    return None


def _m_udp_reply(rule: MatcherRule, dev: Device, obs: Observations
                 ) -> dict[str, str] | None:
    port = int(rule.params.get("port", 0))
    body_regex = rule.params.get("body_regex")
    if not body_regex:
        return None
    reply = obs.udp_reply.get((dev.ip, port))
    if reply is None:
        return None
    # UDP payloads are often binary -- try to decode as latin1 so
    # regex matches on raw bytes work.
    txt = reply.decode("latin1", errors="replace")
    if not re.search(body_regex, txt):
        return None
    extracted = {}
    for field_name, r in rule.extract.items():
        em = re.search(r, txt)
        if em:
            extracted[field_name] = _first_group(em)
    return extracted


def _m_upnp_dd(rule: MatcherRule, _dev: Device, obs: Observations
               ) -> dict[str, str] | None:
    regex = rule.params.get("regex")
    if not regex or not obs.upnp_dd:
        return None
    for _url, body in obs.upnp_dd.items():
        if not re.search(regex, body, re.I):
            continue
        extracted = {}
        for field_name, r in rule.extract.items():
            em = re.search(r, body, re.I)
            if em:
                extracted[field_name] = _first_group(em)
        return extracted
    return None


def _first_group(m: re.Match) -> str:
    """Extract the first captured group (or the whole match)."""
    try:
        return m.group(1)
    except IndexError:
        return m.group(0)


def _m_nmap_os(rule: MatcherRule, _dev: Device, obs: Observations
                ) -> dict[str, str] | None:
    """Match nmap's OS-detection string against a regex.

    Useful for fingerprinting printers, phones, routers by their
    TCP/IP stack signature -- nmap emits things like
    "HP LaserJet 4250 printer", "Apple iOS 14.X", "Linux 4.19",
    "Cisco IOS 12.4", straight from its nmap-os-db.
    """
    regex = rule.params.get("os_regex")
    if not regex or not obs.nmap:
        return None
    os_name = obs.nmap.get("os", "")
    if not os_name:
        return None
    m = re.search(regex, os_name, re.I)
    if not m:
        return None
    extracted = {"os": os_name}
    for field_name, r in rule.extract.items():
        em = re.search(r, os_name, re.I)
        if em:
            extracted[field_name] = _first_group(em)
    return extracted


def _m_nmap_service(rule: MatcherRule, _dev: Device, obs: Observations
                     ) -> dict[str, str] | None:
    """Match on a nmap service banner ("HP JetDirect", "Apache 2.4")."""
    regex = rule.params.get("banner_regex")
    if not regex or not obs.nmap:
        return None
    want_port = rule.params.get("port")
    for svc in obs.nmap.get("services", []):
        if want_port and int(want_port) != svc.get("port"):
            continue
        banner = "%s %s" % (svc.get("service", ""),
                             svc.get("version", ""))
        m = re.search(regex, banner, re.I)
        if m:
            return {"banner": banner.strip()}
    return None


_MATCHERS = {
    "port": _m_port,
    "mac_oui": _m_mac_oui,
    "mdns": _m_mdns,
    "ssdp": _m_ssdp,
    "http_body": _m_http_body,
    "http_header": _m_http_header,
    "udp_reply": _m_udp_reply,
    "upnp_dd": _m_upnp_dd,
    "nmap_os": _m_nmap_os,
    "nmap_service": _m_nmap_service,
}
