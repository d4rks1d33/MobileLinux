# IoT Hacking module — architecture & extension guide

Unified LAN IoT discovery + auto-exploitation. Points at whatever WiFi the
phone is on, sweeps arp + mDNS + SSDP + a handful of UDP broadcast
protocols, classifies every reply against a plugin catalogue, and offers
per-device attack buttons drawn from playbooks that ship as YAML.

The module lives under `src/nethunter_pro/iot_hacking/` and is registered
by `modules/iot_hacking.py`. The GTK4 module is intentionally thin — the
whole engine can be exercised from a Python REPL for tests.

## Directory layout

```
iot_hacking/
├── core/
│   ├── models.py        Device, Port, Fingerprint, Vuln, Credential,
│   │                    AttackRun, DeviceType, Severity, ActionCategory
│   ├── registry.py      DeviceRegistry: in-memory dict + SQLite mirror
│   │                    with GObject signals for the UI
│   ├── discovery.py     Multi-protocol async discovery + TCP port sweep
│   │                    + HTTP banner grabs, followed by fingerprint
│   │                    scoring against every plugin
│   ├── fingerprint.py   YAML-driven scoring engine, 0-100 confidence
│   ├── exploit.py       PlaybookLibrary + Runner (safe YAML DSL)
│   └── __init__.py
├── data/
│   ├── fingerprints/*.yaml   one per device family
│   └── playbooks/*.yaml      one per device family (attacks)
├── plugins/                  reserved for python-side plugins that add
│                             custom probes or enrichment beyond YAML
└── ui/                       reserved for detail widgets we haven't
                              broken out yet
```

## Add a new device family

Two files: `data/fingerprints/<family>.yaml` and (optionally)
`data/playbooks/<family>.yaml`. That's it. No Python edits, no
registration step.

Fingerprint YAML:

```yaml
id: my-device            # stable string; also referenced from playbook
version: 1
device_type: smart_bulb  # one of the DeviceType enum values
vendor: Acme Lights
family: acme-cloud       # arbitrary; playbooks can match on this
threshold: 60            # sum of matcher confidence needed to declare
matchers:
  - name: mdns
    type: mdns
    mdns_type: _acme._tcp
    confidence: 70
  - name: port-1337
    type: port
    port: 1337
    confidence: 20
  - name: banner
    type: http_body
    path: /
    regex: 'Acme Lights bridge'
    confidence: 40
    extract:
      firmware: 'firmware=([0-9.]+)'
```

Matcher types supported: `port` (tcp+udp), `mac_oui`, `mdns`, `ssdp`,
`http_body`, `http_header`, `udp_reply`, `upnp_dd`. Each contributes its
`confidence` when it matches; the sum decides whether the device is
labeled.

Playbook YAML:

```yaml
id: my-device
device_type: smart_bulb
vendor: Acme
requires: { port: 80 }
actions:
  - id: dump-config
    label: "Dump bridge config"
    category: recon
    dangerous: false
    steps:
      - http_request:
          url: "http://{{target.ip}}/api/config"
          save_as: cfg
      - extract:
          from: $cfg
          fields:
            name: '"name":"([^"]+)"'
```

Step verbs: `http_request`, `tcp_send`, `udp_send`, `run_tool` (curl,
nmap, hydra, mosquitto_pub, ...), `extract`, `persist_credential`,
`note`. All params support `{{target.ip}}` / `{{target.mac}}` /
`{{state.<key>}}` templating.

## The pipeline

```
      arp-scan wlan0                     ────► on_arp_host()
      avahi-browse (mDNS)                ────► on_mdns()
      SSDP M-SEARCH × N targets          ────► on_ssdp()
      UDP broadcast probes                       │
        Kasa 9999 / WiZ 38899 / …        ────►   ▼
                                                 ▼
                                       host dict per IP
                                                 │
                                                 ▼
                              TCP top-40 port sweep per host
                                                 │
                                                 ▼
                             HTTP banner on any web-ish port
                                                 │
                                                 ▼
                           FingerprintEngine.score(dev, obs)
                                                 │
                                                 ▼
                            DeviceRegistry.attach_fingerprint()
                                                 │
                                                 ▼
                                    "device-updated" signal
                                                 │
                                                 ▼
                                    UI redraws the device row
```

## Persistence

`~/.local/share/nethunter-pro/iot/cache.db` — SQLite, WAL mode.
`devices` and `audit_log` tables. Every upsert also writes a JSON blob
so the schema doesn't have to grow every time a plugin invents a new
metadata field. The audit log is append-only; nothing in the app reads
it back yet but it's there when we want the "what did I do" tab.

## Verified working

Latest scan against the office LAN produced:

  * 192.168.1.1 → Router (Huawei), confidence **100** via 5 layered
    SSDP matchers (InternetGatewayDevice, WANIPConnection,
    WANCommonInterfaceConfig, Layer3Forwarding, UPnP dd.xml regex)
  * 192.168.1.4 → Philips Android TV (Chromecast built-in),
    confidence 100
  * 192.168.1.9 → Google Cast device, confidence 100
  * unclassified MACs (locally-administered = phone MAC randomising)
    correctly land in the Unclassified section

The stack now ships:

  * **30 fingerprint plugins**: airplay, bose-soundtouch, chromecast,
    dahua, daikin-ac, enphase-envoy, esphome, generic-router, hikvision,
    home-assistant, kasa, lifx, miio, nanoleaf, node-red, openevse,
    philips-hue, printer, qnap, roku, rtsp-generic, shelly-gen1,
    shelly-gen2, sonos, synology, tasmota, tuya, wemo, wiz, yeelight.
  * **27 playbooks**: airplay, bose-soundtouch, chromecast, dahua,
    daikin-ac, enphase, hikvision, home-assistant, kasa, lifx, miio,
    nanoleaf, node-red, openevse, philips-hue, printer, roku,
    router-recon, rtsp-camera, shelly-gen1, shelly-gen2, sonos,
    synology, tasmota, tuya, wemo, wiz.

## PRET (Printer Exploitation Toolkit) integration

Whenever a device is classified as a printer (via `mdns _ipp._tcp` /
`_printer._tcp` or the JetDirect port 9100), the device detail page
grows three extra buttons that shell out to /opt/pret:

  * **Print custom text (PJL)** — opens a text prompt, wraps the string
    in PRET's `print "..."` command, and sends it via
    `python2 /opt/pret/pret.py TARGET pjl -q -i cmds.txt`.
  * **Print image file (PCL)** — opens a file chooser, hands the path
    to PRET's `print <file>` which uses ImageMagick + Ghostscript to
    convert to PCL before shipping.
  * **Discover printers via SNMP** — runs `pret.py` with no args, which
    triggers PRET's built-in SNMP discovery mode. Handy when a printer
    has a static IP outside the DHCP pool that arp-scan missed.

The subprocess output streams to the same `OutputView` as the playbook
runner. If /opt/pret is missing the buttons still render but show
"PRET not installed" and don't fire.

## Known gaps / next work

* `python-kasa` / `python-miio` / `soco` / etc are optional deps
  declared in `pyproject.toml [iot]`. Playbooks still work without
  them via raw HTTP / TCP, but the richer per-vendor drivers are
  behind those extras.
* mDNS uses `avahi-browse` for now. That's already on the phone
  courtesy of the deauth/net_discovery modules. When we want to add
  cancellation mid-browse we'll wire `zeroconf` directly.
* Nuclei integration: the exploit runner's `run_tool` whitelist
  already lists `nuclei`; we haven't shipped a nuclei-templates
  wrapper yet. Should be a couple of playbook actions calling
  `nuclei -jsonl -t <template> -u {{target.ip}}` and streaming the
  jsonl through `extract`.
* The device list rebuilds the whole `Adw.PreferencesGroup` on every
  update. Fine at <30 devices; needs `Gtk.ListView` with a
  virtualised model when we start looking at bigger LANs.
* Batch mode (select N devices → apply same action) is on the plan
  but not yet built.

## Extending with Python

Beyond YAML, a full plugin can live at `plugins/<name>/__init__.py`
exposing a class that adds custom probes (protocol-specific
handshakes) or enrichment steps. The auto-discovery hook is intended
to enumerate `plugins/` at startup and register anything with the
right interface; the wiring hasn't landed yet because everything we
need so far fits in YAML.
