"""Async multi-protocol discovery orchestrator.

Fires arp-scan + mDNS + SSDP + UDP broadcast probes + TCP port sweeps in
parallel, feeds the results into a :class:`~models.Device` per MAC and
hands them to the :class:`~registry.DeviceRegistry`.

The design is intentionally boring: everything is coroutines on the same
event loop, semaphores keep the phone's Wi-Fi chip from drowning, and a
global ``asyncio.TaskGroup`` (Python 3.11+) means one failing probe
doesn't take the run down.

Cancellation: ``Discovery.stop()`` cancels the task group. Every probe
handles ``CancelledError`` by closing its socket in ``finally`` -- so
Stop takes <500 ms from the UI's perspective.
"""
from __future__ import annotations

import asyncio
import re
import socket
import struct
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Callable

from .fingerprint import FingerprintEngine, Observations
from .models import Device, Port
from .registry import DeviceRegistry


# ----------------------------------------------------------- helpers
def _now() -> float:
    return time.monotonic()


async def _iface_subnet(iface: str) -> str | None:
    """Return the subnet the interface lives on, e.g. ``192.168.1.0/24``.

    Reads ``ip -4 -o addr show dev IFACE`` and translates the
    ``a.b.c.d/PREFIX`` field into ``network/PREFIX``. Returns None on
    a downed interface or a /32 address.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip", "-4", "-o", "addr", "show", "dev", iface,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (FileNotFoundError, asyncio.TimeoutError):
        return None
    text = out.decode(errors="replace")
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", text)
    if not m:
        return None
    ip = m.group(1)
    prefix = int(m.group(2))
    if prefix >= 32:
        return None
    octs = [int(x) for x in ip.split(".")]
    # Mask off host bits so we return the network address
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    ip_int = (octs[0] << 24) | (octs[1] << 16) | (octs[2] << 8) | octs[3]
    net_int = ip_int & mask
    net = "%d.%d.%d.%d" % (
        (net_int >> 24) & 0xFF, (net_int >> 16) & 0xFF,
        (net_int >> 8) & 0xFF, net_int & 0xFF)
    return "%s/%d" % (net, prefix)


async def _sh(cmd: list[str], timeout: float = 10.0) -> str:
    """Run a subprocess and return stdout as text.

    Wraps ``asyncio.create_subprocess_exec`` with a hard timeout and
    guaranteed cancellation cleanup.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return ""
    try:
        out, _err = await asyncio.wait_for(proc.communicate(),
                                           timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    except asyncio.CancelledError:
        proc.kill()
        raise
    return out.decode("utf-8", errors="replace")


# ----------------------------------------------------------- probes
async def probe_arp(iface: str,
                    on_host: Callable[[str, str, str], None],
                    root_runner=None) -> None:
    """arp-scan the local network, emit (ip, mac, vendor) triples.

    ``arp-scan`` needs CAP_NET_RAW to open a raw packet socket, so it
    fails silently when the GUI process runs as unprivileged ``kali``.
    ``root_runner`` (when provided) is an async callable that runs the
    command via the DBus root helper -- see :class:`Discovery` for the
    wiring. We fall back to the plain subprocess for the CLI usage.
    """
    argv = ["arp-scan", "--interface=" + iface, "--localnet",
            "--ouifile=/usr/share/arp-scan/ieee-oui.txt",
            "--macfile=/dev/null"]
    if root_runner is not None:
        out = await root_runner(argv, timeout=20)
    else:
        out = await _sh(argv, timeout=15)
    _parse_arp_scan_output(out, on_host)


def _parse_arp_scan_output(
        out: str, on_host: Callable[[str, str, str], None]) -> None:
    for line in out.splitlines():
        # arp-scan lines: "IP\tMAC\tVENDOR"
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        ip = parts[0].strip()
        mac = parts[1].strip()
        vendor = parts[2].strip() if len(parts) >= 3 else ""
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            continue
        if not re.match(r"^[0-9a-fA-F:]{17}$", mac):
            continue
        on_host(ip, mac.upper(), vendor)


async def probe_icmp_sweep(subnet: str,
                            on_host: Callable[[str], None]) -> None:
    """Ping every host in ``subnet`` and emit responsive IPs.

    Backup discovery for when arp-scan can't run (unprivileged app,
    misconfigured Wi-Fi) or when arp-scan misses a host (Windows PC
    with the firewall dropping ARP replies to unknown senders is more
    common than one would hope). ``ping -c 1 -W 1`` is what iputils
    ships and works without root on Linux since it uses SOCK_DGRAM.
    """
    if "/" not in subnet:
        return
    try:
        base, prefix = subnet.split("/")
        prefix = int(prefix)
    except ValueError:
        return
    if prefix < 24:
        return  # avoid pinging a /16 by accident
    octets = base.split(".")
    if len(octets) != 4:
        return
    net = ".".join(octets[:3])
    sem = asyncio.Semaphore(30)

    async def one(ip: str) -> None:
        async with sem:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ping", "-c", "1", "-W", "1", ip,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                rc = await proc.wait()
                if rc == 0:
                    on_host(ip)
            except FileNotFoundError:
                return

    hosts = 2 ** (32 - prefix) - 2
    tasks = [one("%s.%d" % (net, i)) for i in range(1, min(hosts, 254) + 1)]
    await asyncio.gather(*tasks, return_exceptions=True)


async def probe_nmap_os(hosts: list[str], root_runner=None
                          ) -> dict[str, dict]:
    """Run nmap with OS + service detection against ``hosts``.

    Returns ``{ip: {"vendor": str, "os": str, "os_accuracy": int,
                     "hostname": str, "services": [{"port": int,
                     "service": str, "version": str}, ...]}}``.

    ``-O`` (OS detection) needs raw sockets, so this only produces
    useful results through the DBus root runner. The plain-subprocess
    fallback still runs the scan but nmap prints a warning and skips
    the OS block -- the service versions are still valuable.
    """
    if not hosts:
        return {}
    argv = [
        "nmap",
        "-Pn",              # skip discovery: we already know hosts are up
        "-sS",              # SYN scan
        "-O",               # OS detection
        "-sV",              # service + version
        "--osscan-guess",   # accept a "close enough" OS match
        "--max-os-tries", "1",
        "--top-ports", "40",
        "--host-timeout", "20s",
        "-T4",
        "--max-retries", "1",
        "--min-parallelism", "10",
        "-oX", "-",         # XML on stdout so we can parse
    ] + list(hosts)
    if root_runner is not None:
        out = await root_runner(argv, timeout=90)
    else:
        out = await _sh(argv, timeout=90)
    return _parse_nmap_xml(out)


def _parse_nmap_xml(xml: str) -> dict[str, dict]:
    """Best-effort parse of nmap's XML output.

    Uses ElementTree from stdlib; we only look at ``<host>`` /
    ``<address>`` / ``<hostname>`` / ``<os>`` / ``<port>`` / ``<service>``
    so a mangled XML tail (nmap sometimes gets killed mid-write) still
    yields whatever hosts landed before the crash.
    """
    if not xml or "<nmaprun" not in xml:
        return {}
    out: dict[str, dict] = {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        # Truncated output; drop trailing garbage until it parses.
        cut = xml.rfind("</host>")
        if cut < 0:
            return {}
        try:
            root = ET.fromstring(xml[:cut] + "</host></nmaprun>")
        except ET.ParseError:
            return {}
    for host in root.findall("host"):
        ip = ""
        vendor = ""
        for addr in host.findall("address"):
            atype = addr.get("addrtype", "")
            if atype == "ipv4":
                ip = addr.get("addr", "")
            elif atype == "mac":
                vendor = addr.get("vendor", "") or vendor
        if not ip:
            continue
        rec: dict = {"vendor": vendor, "os": "", "os_accuracy": 0,
                      "hostname": "", "services": []}
        for hn in host.findall("hostnames/hostname"):
            name = hn.get("name", "")
            if name:
                rec["hostname"] = name
                break
        # OS detection: take the highest-accuracy match.
        best = None
        best_acc = 0
        for match in host.findall("os/osmatch"):
            try:
                acc = int(match.get("accuracy", "0"))
            except ValueError:
                acc = 0
            if acc > best_acc:
                best_acc = acc
                best = match.get("name", "")
        if best:
            rec["os"] = best
            rec["os_accuracy"] = best_acc
        for p in host.findall("ports/port"):
            state = p.find("state")
            if state is None or state.get("state") != "open":
                continue
            try:
                num = int(p.get("portid", "0"))
            except ValueError:
                continue
            svc = p.find("service")
            svc_name = svc.get("name", "") if svc is not None else ""
            svc_ver = svc.get("product", "") if svc is not None else ""
            if svc is not None and svc.get("version"):
                svc_ver = (svc_ver + " " + svc.get("version")).strip()
            rec["services"].append({
                "port": num, "service": svc_name,
                "version": svc_ver,
            })
        out[ip] = rec
    return out


async def probe_arp_neigh(iface: str,
                            on_host: Callable[[str, str, str],
                                              None]) -> None:
    """Read the kernel's ARP cache and emit (ip, mac, "") triples.

    After an ICMP sweep the kernel populated its neighbour table with
    every host that answered. No root needed. Complementary to a real
    arp-scan probe: catches Windows machines that answer ping but drop
    arp-scan's crafted request.
    """
    out = await _sh(["ip", "-4", "neigh", "show", "dev", iface],
                    timeout=5)
    # Expected line shape: "IP lladdr MAC STATE" -- 4 tokens minimum.
    # The upstream port was looking for >=5 which is why it never
    # emitted any host in the field even though the cache was full.
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or "lladdr" not in parts:
            continue
        ip = parts[0]
        idx = parts.index("lladdr")
        if idx + 1 >= len(parts):
            continue
        mac = parts[idx + 1]
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            continue
        if not re.match(r"^[0-9a-fA-F:]{17}$", mac):
            continue
        on_host(ip, mac.upper(), "")


async def probe_mdns(timeout: float,
                     on_service: Callable[[str, dict], None]) -> None:
    """Enumerate mDNS services via avahi-browse.

    Rather than rolling our own zeroconf browser (which is doable but
    brings in a dep), we shell out to ``avahi-browse -atrp`` which is
    already available on the phone.
    """
    out = await _sh(
        ["avahi-browse", "-a", "-t", "-r", "-p", "-l"],
        timeout=timeout + 2)
    for line in out.splitlines():
        parts = line.split(";")
        # Resolved records start with "=", have >=8 fields.
        if len(parts) < 10 or parts[0] != "=":
            continue
        svc_type = parts[4]
        name = parts[3]
        fqdn = parts[6]
        ip = parts[7]
        port = parts[8]
        txt = parts[9] if len(parts) > 9 else ""
        if ":" in ip:   # skip IPv6
            continue
        on_service(svc_type, {
            "name": name, "fqdn": fqdn, "ip": ip,
            "port": port, "txt": txt,
        })


async def probe_ssdp(targets: list[str], timeout: float,
                     on_reply: Callable[[str, dict], None]) -> None:
    """Fire M-SEARCH for each target, forward every reply.

    ``on_reply(ip, {st, location, server, usn})``.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setblocking(False)
    loop = asyncio.get_event_loop()
    try:
        for st in targets:
            msg = (
                "M-SEARCH * HTTP/1.1\r\n"
                "HOST: 239.255.255.250:1900\r\n"
                'MAN: "ssdp:discover"\r\n'
                "MX: 2\r\n"
                "ST: %s\r\n\r\n" % st
            ).encode()
            try:
                await loop.sock_sendto(
                    sock, msg, ("239.255.255.250", 1900))
            except (OSError, NotImplementedError):
                # Some kernels want the sendto through non-async path.
                sock.sendto(msg, ("239.255.255.250", 1900))
        deadline = _now() + timeout
        while _now() < deadline:
            try:
                data = await asyncio.wait_for(
                    loop.sock_recv(sock, 4096),
                    timeout=deadline - _now())
            except asyncio.TimeoutError:
                break
            except (OSError, ConnectionError):
                break
            # sock_recv returns bytes; we need the addr, so use a
            # helper socket call directly.
            try:
                data, addr = sock.recvfrom(4096)
            except BlockingIOError:
                continue
            except OSError:
                break
            headers = _parse_ssdp_headers(data)
            on_reply(addr[0], headers)
    finally:
        sock.close()


def _parse_ssdp_headers(data: bytes) -> dict:
    out = {"st": "", "location": "", "server": "", "usn": ""}
    for line in data.decode("latin1", errors="replace").splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip().lower()
        v = v.strip()
        if k in out:
            out[k] = v
    return out


# Kasa / TP-Link discovery: XOR-encrypted JSON to UDP/9999
def _kasa_encrypt(payload: str) -> bytes:
    key = 0xAB
    out = bytearray()
    for ch in payload.encode():
        key ^= ch
        out.append(key)
    return bytes(out)


def _kasa_decrypt(data: bytes) -> str:
    key = 0xAB
    out = bytearray()
    for b in data:
        out.append(b ^ key)
        key = b
    return out.decode("utf-8", errors="replace")


async def probe_kasa(timeout: float,
                     on_reply: Callable[[str, bytes], None]) -> None:
    """Broadcast Kasa discovery request on UDP 9999.

    Kasa/TP-Link plugs listen on 9999 UDP; the JSON body is XOR'd with
    key 0xAB. Cheap and reliable identifier.
    """
    payload = _kasa_encrypt('{"system":{"get_sysinfo":{}}}')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    try:
        try:
            sock.sendto(payload, ("255.255.255.255", 9999))
        except OSError:
            return
        deadline = _now() + timeout
        while _now() < deadline:
            try:
                remaining = deadline - _now()
                if remaining <= 0:
                    break
                await asyncio.wait_for(
                    asyncio.get_event_loop().sock_recv(sock, 4096),
                    timeout=remaining)
            except asyncio.TimeoutError:
                break
            except OSError:
                break
            try:
                data, addr = sock.recvfrom(4096)
            except (BlockingIOError, OSError):
                continue
            on_reply(addr[0], data)
    finally:
        sock.close()


async def probe_wiz(timeout: float,
                    on_reply: Callable[[str, bytes], None]) -> None:
    """WiZ bulbs broadcast on UDP 38899 with a JSON getPilot request."""
    await _udp_broadcast(38899,
                          b'{"method":"getPilot","params":{}}',
                          timeout, on_reply)


async def probe_lifx(timeout: float,
                     on_reply: Callable[[str, bytes], None]) -> None:
    """LIFX GetService packet on UDP 56700.

    Header format is documented at
    lan.developer.lifx.com/docs/header-description. We send GetService
    (type 2) with an all-broadcast target, source=1.
    """
    # Frame (8 bytes) + Frame Address (16) + Protocol Header (12) = 36
    # size(2 LE) | protocol/origin/tagged/addressable (2) |
    #   source(4) | target(8) | reserved(6) | ack/res(1) | seq(1) |
    #   reserved(8) | type(2) | reserved(2)
    frame = struct.pack(
        "<HHIQ6sBBQHH",
        36,          # size
        0x3400,      # flags: tagged=1, addressable=1, protocol=1024
        1,           # source
        0,           # target (broadcast)
        b"\x00" * 6, # reserved
        0,           # res_required
        0,           # seq
        0,           # reserved
        2,           # type=GetService
        0,           # reserved
    )
    await _udp_broadcast(56700, frame, timeout, on_reply)


async def probe_tuya(timeout: float,
                     on_reply: Callable[[str, bytes], None]) -> None:
    """Tuya devices announce themselves on UDP 6666 / 6667 continuously.

    Just listen -- they broadcast every ~10s.
    """
    for port in (6666, 6667):
        await _udp_listen(port, timeout / 2, on_reply)


async def probe_miio(timeout: float,
                     on_reply: Callable[[str, bytes], None]) -> None:
    """Xiaomi miIO devices reply to a handshake on UDP 54321.

    Payload: `21 31` magic, length 0x0020, then 12 bytes of `ff` for the
    unencrypted handshake variant, then 16 zeros for the token slot.
    """
    payload = (b"\x21\x31\x00\x20"          # magic + length
               + b"\xff\xff\xff\xff"        # device id (broadcast)
               + b"\xff" * 8                # timestamp / checksum slot
               + b"\x00" * 16)              # empty token
    await _udp_broadcast(54321, payload, timeout, on_reply)


async def _udp_broadcast(port: int, payload: bytes, timeout: float,
                         on_reply: Callable[[str, bytes], None]
                         ) -> None:
    """Common broadcast + collect pattern used by all UDP discovery probes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    try:
        try:
            sock.sendto(payload, ("255.255.255.255", port))
        except OSError:
            return
        deadline = _now() + timeout
        while _now() < deadline:
            try:
                remaining = deadline - _now()
                if remaining <= 0:
                    break
                await asyncio.wait_for(
                    asyncio.get_event_loop().sock_recv(sock, 4096),
                    timeout=remaining)
            except asyncio.TimeoutError:
                break
            except OSError:
                break
            try:
                data, addr = sock.recvfrom(4096)
            except (BlockingIOError, OSError):
                continue
            on_reply(addr[0], data)
    finally:
        sock.close()


async def _udp_listen(port: int, timeout: float,
                      on_reply: Callable[[str, bytes], None]) -> None:
    """Bind to ``port`` and wait for whatever shows up.

    Used for protocols that broadcast periodically (Tuya, Chromecast
    hello messages, ...). Bind may fail with EADDRINUSE if another
    daemon owns the port; treat as best-effort.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(False)
    try:
        try:
            sock.bind(("", port))
        except OSError:
            return
        deadline = _now() + timeout
        while _now() < deadline:
            try:
                remaining = deadline - _now()
                if remaining <= 0:
                    break
                await asyncio.wait_for(
                    asyncio.get_event_loop().sock_recv(sock, 4096),
                    timeout=remaining)
            except asyncio.TimeoutError:
                break
            except OSError:
                break
            try:
                data, addr = sock.recvfrom(4096)
            except (BlockingIOError, OSError):
                continue
            on_reply(addr[0], data)
    finally:
        sock.close()


async def probe_tcp_ports(ip: str, ports: list[int],
                          timeout: float = 1.5,
                          semaphore: asyncio.Semaphore | None = None
                          ) -> list[int]:
    """Rapid TCP connect scan of one host.

    Returns the list of open ports. Used sparsely -- only against hosts
    the arp-scan already found alive.
    """
    open_ports: list[int] = []

    async def one(port: int) -> None:
        if semaphore is not None:
            await semaphore.acquire()
        try:
            try:
                fut = asyncio.open_connection(ip, port)
                reader, writer = await asyncio.wait_for(
                    fut, timeout=timeout)
                open_ports.append(port)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
            except (OSError, asyncio.TimeoutError,
                    ConnectionRefusedError):
                pass
        finally:
            if semaphore is not None:
                semaphore.release()

    await asyncio.gather(*(one(p) for p in ports))
    return sorted(open_ports)


async def http_banner(ip: str, port: int = 80, path: str = "/",
                      timeout: float = 3.0) -> dict | None:
    """Fetch a single HTTP endpoint. Returns {status, body, headers}.

    Rolled with the stdlib rather than pulling aiohttp so the module
    works out of the box; aiohttp will come in when we start driving
    plugins that need it (Kasa/Shelly login, etc).
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        req = ("GET %s HTTP/1.0\r\n"
               "Host: %s\r\n"
               "User-Agent: NetHunterPro/1.0\r\n"
               "Accept: */*\r\n"
               "Connection: close\r\n\r\n") % (path, ip)
        writer.write(req.encode())
        await writer.drain()
        try:
            data = await asyncio.wait_for(reader.read(65536),
                                          timeout=timeout)
        except asyncio.TimeoutError:
            return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    # Parse HTTP response
    text = data.decode("utf-8", errors="replace")
    if "\r\n\r\n" in text:
        head, body = text.split("\r\n\r\n", 1)
    else:
        head, body = text, ""
    # Status code lives on the first line: "HTTP/1.0 200 OK"
    status = 0
    lines = head.splitlines()
    if lines:
        parts = lines[0].split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return {"status": status, "body": body[:32000], "headers": headers}


# --------------------------------------------------------- orchestrator
# Ports we probe by default. Curated for IoT devices; not exhaustive.
DEFAULT_TCP_PORTS = [
    21, 22, 23, 53, 80, 81, 88, 443, 445, 554, 631, 1400, 1880, 1883,
    1900, 5000, 5001, 5555, 6053, 6100, 6667, 6668, 7443, 7676, 8000,
    8001, 8008, 8009, 8060, 8080, 8081, 8123, 8181, 8443, 8484, 8580,
    8883, 9000, 9100, 9999, 16021, 20002, 32400, 49152, 49153, 55443,
    55000,
]

# The SSDP search targets that give us broad IoT coverage. Kept short
# so a broadcast round-trip fits in ~2s.
SSDP_TARGETS = [
    "ssdp:all",
    "upnp:rootdevice",
    "urn:dial-multiscreen-org:service:dial:1",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:device:MediaServer:1",
    "urn:schemas-upnp-org:device:Basic:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:Belkin:device:controllee:1",
    "urn:Belkin:device:insight:1",
    "urn:schemas-kinoma-com:device:shell:1",
    "roku:ecp",
]


class Discovery:
    """Async coordinator running all probes for one scan.

    Callers create one instance per scan run, hand it a registry, then
    ``await discovery.run(iface)``. Signals fire on the registry as
    devices are found -- the UI subscribes to those, not to Discovery
    directly.

    ``root_runner`` is an async ``(argv, timeout) -> str`` callable
    that runs privileged commands via the DBus helper. When ``None``
    (CLI usage / tests) we fall back to a plain subprocess -- which
    means arp-scan produces zero rows under an unprivileged user, and
    the ICMP-sweep + kernel ARP-cache fallbacks take over.
    """
    def __init__(self, registry: DeviceRegistry,
                 fingerprint: FingerprintEngine,
                 iface: str = "wlan0",
                 tcp_ports: list[int] | None = None,
                 root_runner=None) -> None:
        self.registry = registry
        self.fingerprint = fingerprint
        self.iface = iface
        self.tcp_ports = tcp_ports or DEFAULT_TCP_PORTS
        self.root_runner = root_runner
        self._task: asyncio.Task | None = None
        self._stopping = False
        # per-host: {ip: {mac, vendor, ports:[], observations, ...}}
        self._hosts: dict[str, dict] = {}
        self._obs: dict[str, Observations] = {}

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def stop(self) -> None:
        self._stopping = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def run(self, on_progress: Callable[[str], None] | None = None
                  ) -> None:
        """Execute the full discovery pipeline once."""
        prog = on_progress or (lambda _msg: None)

        # ---- L2 sweep: arp-scan (root) + ICMP + neigh table --------
        # We layer three passes so the run works whether or not we
        # have CAP_NET_RAW:
        #   1. arp-scan through the root helper if available -- the
        #      most complete answer, catches devices even if they
        #      block ICMP.
        #   2. ICMP sweep of the /24 (SOCK_DGRAM ping, no root
        #      needed) -- catches Windows PCs that answer ping but
        #      not crafted ARP.
        #   3. Read /proc/net/arp (or `ip neigh`) after the sweep --
        #      the kernel just populated it with everything that
        #      answered ping.
        prog("arp-scan on " + self.iface)
        await probe_arp(self.iface, self._on_arp_host,
                          root_runner=self.root_runner)
        subnet = await _iface_subnet(self.iface)
        if subnet:
            prog("ICMP sweep " + subnet)
            hit_ips: set[str] = set()
            await probe_icmp_sweep(subnet, hit_ips.add)
            prog("reading kernel ARP cache")
            await probe_arp_neigh(self.iface, self._on_arp_host)
            for ip in hit_ips:
                if ip not in self._hosts:
                    self._hosts[ip] = {"mac": "", "vendor": "",
                                        "ports": []}

        # ---- nmap OS + service detection (root-only) --------------
        # Nmap's OS fingerprinter gives us: vendor from the MAC OUI
        # (even when arp-scan missed the row), an OS family guess
        # ("HP LaserJet Printer", "Linux 4.19", "Apple iOS"),
        # and service-version banners on the top 40 ports. All three
        # feed the fingerprint engine -- an OS regex like "printer"
        # /"iOS"/"Linux 3\." are much cheaper hints than crafting
        # per-vendor mDNS or HTTP probes.
        if self.root_runner is not None and self._hosts:
            prog("nmap OS + services")
            live_ips = [ip for ip in self._hosts]
            try:
                nmap = await probe_nmap_os(live_ips,
                                            root_runner=self.root_runner)
            except Exception:  # noqa: BLE001
                nmap = {}
            for ip, info in nmap.items():
                host = self._hosts.setdefault(
                    ip, {"mac": "", "vendor": "", "ports": []})
                if info.get("vendor") and not host.get("vendor"):
                    host["vendor"] = info["vendor"]
                obs = self._obs.setdefault(ip, Observations())
                obs.nmap = info
                # If nmap discovered ports arp-scan/mdns didn't
                # already list, add them so the fingerprint engine
                # sees them.
                existing_ports = {p.number for p in host["ports"]}
                for svc in info.get("services", []):
                    if svc["port"] in existing_ports:
                        continue
                    host["ports"].append(Port(
                        number=svc["port"], protocol="tcp",
                        service=svc.get("service", ""),
                        banner=svc.get("version", "")))

        # ---- mDNS + SSDP + broadcast probes in parallel ---------
        prog("mDNS + SSDP + UDP broadcasts")
        tcp_sem = asyncio.Semaphore(20)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(probe_mdns(6, self._on_mdns))
                tg.create_task(probe_ssdp(SSDP_TARGETS, 4,
                                          self._on_ssdp))
                tg.create_task(probe_kasa(3, self._on_kasa))
                tg.create_task(probe_wiz(3, self._on_wiz))
                tg.create_task(probe_lifx(3, self._on_lifx))
                tg.create_task(probe_tuya(4, self._on_tuya))
                tg.create_task(probe_miio(3, self._on_miio))
                # TCP port sweep on every known host, semaphored
                for ip in list(self._hosts.keys()):
                    tg.create_task(self._sweep_ports(ip, tcp_sem))
        except* asyncio.CancelledError:
            # Propagate cancellation cleanly
            raise

        # ---- HTTP banner grab on any open web port ---------------
        prog("HTTP banners")
        await self._http_banners_pass()

        # ---- Score every host against every fingerprint plugin ---
        prog("fingerprint scoring")
        for ip, host in self._hosts.items():
            obs = self._obs.get(ip)
            if obs is None:
                obs = Observations()
            # If arp discovery couldn't get a MAC, fall back to the
            # IP as the registry key. Otherwise every mac-less host
            # collapses into a single dict entry and the UI shows
            # exactly one row for the whole /24.
            mac = host.get("mac", "") or ("ip:" + ip)
            dev = Device(
                mac=mac,
                ip=ip,
                vendor=host.get("vendor", ""),
            )
            for p in host.get("ports", []):
                dev.add_port(p)
            # Nmap output goes into device metadata unconditionally so
            # the UI can show ("Linux 4.19") on unclassified hosts.
            if obs.nmap:
                if obs.nmap.get("hostname"):
                    dev.hostname = obs.nmap["hostname"]
                if obs.nmap.get("os"):
                    dev.metadata["os"] = obs.nmap["os"]
                if obs.nmap.get("vendor") and not dev.vendor:
                    dev.vendor = obs.nmap["vendor"]
            matches = self.fingerprint.score(dev, obs)
            for fp in matches:
                if fp.matcher_name == "_aggregate":
                    from .models import DeviceType
                    self.registry.attach_fingerprint(
                        dev.mac, fp,
                        device_type=DeviceType(
                            fp.extracted.get("device_type", "unknown")),
                        vendor=fp.extracted.get("vendor", ""),
                    )
                else:
                    self.registry.attach_fingerprint(dev.mac, fp)
            # Even without a match, register the device so the UI can
            # show "unknown" hosts under a section.
            if not matches:
                self.registry.upsert(dev)
            else:
                self.registry.upsert(dev)
                self.registry.attach_ports(dev.mac, dev.ports)

        prog("done")

    # ------------------------------------------------ callbacks
    def _on_arp_host(self, ip: str, mac: str, vendor: str) -> None:
        self._hosts.setdefault(ip, {
            "mac": mac, "vendor": vendor, "ports": []})
        self._hosts[ip]["mac"] = mac
        if vendor and not self._hosts[ip].get("vendor"):
            self._hosts[ip]["vendor"] = vendor
        self._obs.setdefault(ip, Observations())

    def _on_mdns(self, svc_type: str, hit: dict) -> None:
        ip = hit.get("ip", "")
        if not ip:
            return
        obs = self._obs.setdefault(ip, Observations())
        obs.mdns.setdefault(svc_type, []).append(hit)
        # Even if arp missed this IP, register it now.
        self._hosts.setdefault(ip, {"mac": "", "vendor": "",
                                    "ports": []})

    def _on_ssdp(self, ip: str, headers: dict) -> None:
        obs = self._obs.setdefault(ip, Observations())
        obs.ssdp.append(headers)
        self._hosts.setdefault(ip, {"mac": "", "vendor": "",
                                    "ports": []})

    def _on_kasa(self, ip: str, data: bytes) -> None:
        obs = self._obs.setdefault(ip, Observations())
        obs.udp_reply[(ip, 9999)] = data
        # Decode for later scoring convenience
        try:
            obs.udp_reply[(ip, 9999)] = _kasa_decrypt(data).encode()
        except Exception:  # noqa: BLE001
            pass
        self._hosts.setdefault(ip, {"mac": "", "vendor": "",
                                    "ports": []})

    def _on_wiz(self, ip: str, data: bytes) -> None:
        obs = self._obs.setdefault(ip, Observations())
        obs.udp_reply[(ip, 38899)] = data
        self._hosts.setdefault(ip, {"mac": "", "vendor": "",
                                    "ports": []})

    def _on_lifx(self, ip: str, data: bytes) -> None:
        obs = self._obs.setdefault(ip, Observations())
        obs.udp_reply[(ip, 56700)] = data
        self._hosts.setdefault(ip, {"mac": "", "vendor": "",
                                    "ports": []})

    def _on_tuya(self, ip: str, data: bytes) -> None:
        obs = self._obs.setdefault(ip, Observations())
        # Tuya broadcasts arrive on both 6666 and 6667 depending on
        # protocol version; key the whichever was seen most recently.
        obs.udp_reply[(ip, 6666)] = data
        self._hosts.setdefault(ip, {"mac": "", "vendor": "",
                                    "ports": []})

    def _on_miio(self, ip: str, data: bytes) -> None:
        obs = self._obs.setdefault(ip, Observations())
        obs.udp_reply[(ip, 54321)] = data
        self._hosts.setdefault(ip, {"mac": "", "vendor": "",
                                    "ports": []})

    # ------------------------------------------------ sweep helpers
    async def _sweep_ports(self, ip: str,
                           sem: asyncio.Semaphore) -> None:
        open_ports = await probe_tcp_ports(
            ip, self.tcp_ports, timeout=1.5, semaphore=sem)
        host = self._hosts.setdefault(
            ip, {"mac": "", "vendor": "", "ports": []})
        host["ports"] = [Port(number=p, protocol="tcp",
                              service=_common_service(p))
                         for p in open_ports]

    async def _http_banners_pass(self) -> None:
        """Grab HTTP body for every host with an interesting web port.

        The fingerprint DB targets several well-known paths beyond ``/``
        (``/shelly``, ``/api/config``, ``/setup/eureka_info``,
        ``/manifest.json``, ``/production.json``, ``/aircon/basic_info``
        ...). We probe each of them when the corresponding port is
        open so the plugin YAMLs can match on the body / headers.
        """
        # (port, path) tuples: the small set of URLs the shipped
        # fingerprints look at. Keep it curated; each request is a
        # round-trip we're going to pay for.
        probes: list[tuple[int, str]] = [
            (80, "/"),
            (80, "/shelly"),
            (80, "/api/config"),
            (80, "/production.json"),
            (80, "/aircon/basic_info"),
            (80, "/common/basic_info"),
            (80, "/rpc/Shelly.GetDeviceInfo"),
            (80, "/cgi-bin/magicBox.cgi?action=getDeviceType"),
            (443, "/"),
            (8008, "/setup/eureka_info?options=detail"),
            (8060, "/query/device-info"),
            (8080, "/"),
            (8090, "/info"),
            (8123, "/manifest.json"),
            (1400, "/xml/device_description.xml"),
            (1880, "/red/settings.json"),
            (5000, "/webman/index.cgi"),
            (16021, "/"),
        ]
        tasks = []
        sem = asyncio.Semaphore(10)

        async def guarded(coro):
            async with sem:
                await coro

        for ip, host in self._hosts.items():
            open_ports = {p.number for p in host.get("ports", [])}
            for port, path in probes:
                if port in open_ports:
                    tasks.append(guarded(self._one_http(ip, port, path)))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _one_http(self, ip: str, port: int, path: str = "/"
                        ) -> None:
        resp = await http_banner(ip, port, path, timeout=3)
        if resp is None:
            return
        obs = self._obs.setdefault(ip, Observations())
        scheme = "https" if port in (443, 8443) else "http"
        obs.http["%s://%s:%d%s" % (scheme, ip, port, path)] = resp


# -------------------------------------------------- port name lookup
_COMMON_SERVICES = {
    21: "ftp", 22: "ssh", 23: "telnet", 53: "dns", 80: "http",
    81: "http-alt", 88: "kerberos", 443: "https", 445: "smb",
    554: "rtsp", 631: "ipp", 1400: "sonos", 1880: "node-red",
    1883: "mqtt", 1900: "ssdp", 5000: "synology-dsm", 5001: "dsm-tls",
    5555: "adb", 6053: "esphome", 6100: "shelly-coiot", 6667: "tuya",
    6668: "tuya-ctl", 7443: "hue-hap", 7676: "samsung-upnp",
    8000: "dahua/http-alt", 8001: "samsung-tv-ws",
    8008: "chromecast", 8009: "chromecast-tls", 8060: "roku-ecp",
    8080: "http-alt", 8081: "sonoff-diy", 8090: "bose-soundtouch",
    8123: "home-assistant", 8181: "openhab", 8443: "https-alt",
    8484: "shelly-ws", 8580: "shelly", 8883: "mqtt-tls",
    9000: "vizio", 9100: "printer-raw",
    9999: "kasa", 16021: "nanoleaf", 20002: "tapo",
    32400: "plex", 49152: "wemo", 49153: "wemo",
    55443: "yeelight", 55000: "samsung-legacy-tv",
}


def _common_service(port: int) -> str:
    return _COMMON_SERVICES.get(port, "")
