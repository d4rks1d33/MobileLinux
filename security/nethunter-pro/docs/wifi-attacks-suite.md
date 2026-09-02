# Wi-Fi attack suite

Consolidated documentation for the Wi-Fi + BLE modules added to
NetHunter Pro on the rhodep port. This document is the map: which
module does what, which hardware it needs, and the natural pentester
flow through them.

## Module catalog

### Passive recon (nothing transmitted)

| Module | What it does | Requires |
|---|---|---|
| **Probe harvester** | Sniffs 802.11 probe requests, aggregates PNL per client, classifies OS via IE fingerprints, cross-refs with Wigle.net for geolocation. CSV -> loot. | `tcpdump`, `scapy`; monitor iface |
| **BLE tracker** | Passive BLE advertisement sniffer with Apple Continuity / Google Fast Pair / Microsoft Swift Pair parsing. Correlates BLE MAC + name with Wi-Fi probes to deanonymise randomised clients. CSV + pcap -> loot. | `btmon` (bluez); phone's Bluetooth adapter or nRF52840 dongle |

### Active recon / survey

| Module | What it does | Requires |
|---|---|---|
| **Net discovery** | LAN scanner (arp-scan / avahi / DNS PTR / nbtscan). Includes the Wi-Fi AP surveyor: airodump one-shot + per-AP verdicts + one-click deep-link to the attack module (PMKID / handshake / evil twin / WPS / etc.). Also displays adapter band capabilities (2.4 / 5 / 6 GHz). | `arp-scan`, `airodump-ng`; monitor iface |
| **Wi-Fi Direct / P2P** | Scan for soft-APs from IoT setup mode, Miracast sinks, Chromecast, printers, cameras. Categorises by SSID prefix, deep-links to attach + PRET / bettercap workflows. | `wpa_cli`, `iw` |
| **IoT setup-AP hunter** | Periodic scan for onboarding SSIDs (`HP-Setup-*`, `Wyze_*`, `DIRECT-*Chromecast`, `Roomba-*`, `tasmota-*`, ...). One-click attach + launch mitmproxy in a separate terminal to catch the target's PSK during setup POST. | `nmcli`, `mitmproxy` |

### Capture attacks (feed the loot pipeline)

| Module | What it does | Requires |
|---|---|---|
| **PMKID capture** | hcxdumptool active/passive on the monitor iface; harvests PMKIDs from every AP that leaks one. pcapng -> loot -> Submit to wpa-sec. | `hcxdumptool` |
| **Handshake capture** | airodump-ng locked to a picked BSSID + targeted aireplay-ng deauth. Watches for `[WPA handshake: BSSID]` in the log. pcap -> loot -> Submit to wpa-sec. | `airodump-ng`, `aireplay-ng` |
| **Wi-Fi (WPS)** | wash JSON scan; per-AP verdict; Pixie Dust (`reaver -K`), online brute-force, PBC race hijack, bully. Cracked PSK -> loot. | `wash`, `reaver`, `bully`, `pixiewps` |

### Utility (channel disruption, coercion)

| Module | What it does | Requires |
|---|---|---|
| **Deauth** | Airodump scan + client picker + `aireplay-ng --deauth`. Extended with mdk4 modes (a/b/d/e/m/p/w/x) and a CSA (Channel Switch Announcement) attack via Scapy that bypasses PMF. | `aireplay-ng`, `mdk4`, `scapy` |

### Rogue AP

| Module | What it does | Requires |
|---|---|---|
| **Evil Twin** | Three profiles: airbase-ng (open/WEP), hostapd (WPA2-PSK), hostapd-mana WPA3->WPA2 downgrade / KARMA loud. Handshakes captured -> loot. | `airbase-ng`, `hostapd`, `hostapd-mana` |
| **KARMA responder** | hostapd-mana with `mana_loud=1`, live probe log + PNL harvest table. Every SSID seen becomes a one-click Evil Twin clone target. | `hostapd-mana` |
| **EAP relay** | eaphammer wrapper for WPA2/WPA3-Enterprise phishing. Cert wizard, GTC downgrade, ESSID stripping, hostile portal. MSCHAPv2 hashes -> loot. | `eaphammer` |
| **Wifipumpkin / phishkin3** | Existing captive-portal frameworks; unchanged. | `wifipumpkin3`, `phishkin3` |

### Post-exploit / research

| Module | What it does | Requires |
|---|---|---|
| **FragAttacks** | Wrapper around Vanhoef's test suite (CVE-2020-24586..24588 + 26139..26147). One-click install of the upstream repo. **Requires ath9k_htc external USB dongle** — the phone's internal ath10k does not work. | `git`, `python3`; external `ath9k_htc` dongle |
| **SSID Confusion** | CVE-2023-52424 rogue AP with SSID-A/SSID-B split, exploiting the SSID-not-in-KDF flaw. | `hostapd` |
| **KRACK Attack** | Two modes in one module. **Tester** (`krackattacks-scripts`): rogue AP + user-enrolled target, 7 client checks + 802.11r AP test. **Live attack** (`krackattacks-poc-zerokey`): channel-based MITM against a real AP, forces the victim to reinstall an all-zero PTK, decrypts client→AP traffic into a pcap via `tap0`. Live attack needs 2 injection-capable interfaces. | `git`, `python3`; ideally 2× AR9271 dongles for live attack |
| **Kr00k tester** | CVE-2019-15126: force disassoc + capture post-disassoc encrypted frames; tshark verdict. Broadcom/Cypress info-leak. | `airodump-ng`, `aireplay-ng`, `tshark` |

### All-in-one and long-lived

| Module | What it does |
|---|---|
| **Wifite** | Existing wrapper; orchestrates PMKID/handshake/WPS. |
| **Pineapple / Pwnagotchi / Bjorn** | External toolchain connectors + Bjorn's LAN scanner-brute-forcer daemon. |
| **Driftnet** | Existing passive image scraper. |

### BLE

| Module | What it does |
|---|---|
| **Bluetooth arsenal / BlueDucky / BLE spam** | Existing modules; unchanged. |
| **BLE provisioning** | bettercap `ble.recon` + `ble.enum` to catch GATT writes during Tuya / ESP-IDF / Nest onboarding. pcap + logs -> loot. |
| **BLE tracker** | Passive BLE with Continuity parsing (see Passive recon above). |

### Loot + wpa-sec

Every module that produces a capture (pcap, pcapng, hash file, CSV,
JSON) records the artefact in a central SQLite DB at
``~/.local/share/nethunter-pro/loot.db``. The **Loot** module in the
sidebar shows the aggregate: filter by module or type, delete
individually or by age, submit pcaps to
[wpa-sec.stanev.org](https://wpa-sec.stanev.org) for distributed
WPA cracking (no hashcat on the phone), poll the site for cracked
results.

Files live under ``~/loot/<module>/`` on disk.

## Natural pentester flow

1. **Loot** -- confirm the wpa-sec API key is set.
2. **Net discovery** -- glance at adapter band capabilities. Sweep
   the LAN if we are already on it. Run the AP surveyor with the
   monitor iface for per-AP verdicts.
3. **Probe harvester** (in parallel) -- passive PNL + Wigle
   correlation. Feeds the KARMA module and points at Evil Twin
   candidates.
4. **PMKID capture** -- the first attack to try. Clientless, no
   deauth, huge hit rate. Submit to wpa-sec.
5. **Handshake capture** -- fallback when PMKID does not leak.
   Deauth a client, catch the 4-way. Submit to wpa-sec.
6. **Wi-Fi (WPS)** -- if `wash` reports unlocked WPS or a Ralink /
   Realtek SoC. Pixie Dust first, brute force second.
7. **Evil Twin / KARMA / EAPHammer** -- when the target has clients
   we can steer. WPA3-Transition downgrade sits in Evil Twin;
   Enterprise MSCHAPv2 harvest sits in EAP relay.
8. **FragAttacks** -- only with `ath9k_htc` dongle. Test known-
   vulnerable IoT for aggregation packet injection -> LAN pivot
   without the PSK.
9. **Kr00k tester / SSID Confusion** -- opportunistic; run against
   specific IoT / enterprise flotas.
10. Long-lived: leave **Bjorn** or **Pwnagotchi** running for a
    site-survey engagement.

## Hardware notes

* **Phone's built-in Wi-Fi** (Qualcomm ath10k_snoc, `wlan0`):
  managed mode only for most attacks. Monitor works but injection
  is spotty. Fine for AP survey passive scanning, poor for
  aireplay/mdk4 injection.
* **External adapter recommended** for real work. Best chipsets in
  order of injection reliability on Kali 2024:
    * **AR9271** (`ath9k_htc`) -- TP-Link WN722N v1, Alfa AWUS036NHA.
      2.4 GHz only. Required for FragAttacks.
    * **MT7612U** (`mt76`) -- Alfa AWUS036ACM/ACHM. 2.4 + 5 GHz.
    * **MT7921AU** (`mt76`) -- Alfa AWUS036AXML. 2.4 + 5 + 6 GHz.
    * Realtek RTL88xxAU: usable via out-of-tree DKMS but flakier.
* **BLE sniffing between two other devices**: needs an
  **nRF52840 dongle** or **TI CC1352R1 + Sniffle firmware**. The
  phone's own Bluetooth stack only sees advertisements + its own
  connections.

## Loot / wpa-sec workflow

```
Capture module runs  ->  writes pcap to ~/loot/<mod>/
                     ->  loot_store.record(...) inserts row
                     
Loot module          ->  UI shows unsubmitted pcaps + "Submit"
                     ->  POST to wpa-sec /?submit with API key
                     ->  wpasec_status = "submitted"
                     
User comes back later:
                     ->  Loot's "Check cracked results"
                     ->  GET wpa-sec /?api&dl=1
                     ->  matches by SSID substring
                     ->  wpasec_status = "cracked", psk populated
                     
User frees space:
                     ->  Loot's "Delete" per row OR "Prune older
                         than N days" bulk
                     ->  file removed from ~/loot/ + row removed
```

wpa-sec API key file: ``~/.config/nethunter-pro/wpasec.key``
(mode 600, plain text). If the file is missing but the system has
Pwnagotchi installed, Loot auto-loads the key from
``/etc/pwnagotchi/config.toml`` under the
``[main.plugins.wpa-sec]`` section (read-only; never touches the
Pwnagotchi config).

Wigle API pair: ``~/.config/nethunter-pro/wigle.key`` (mode 600,
``user:token``).

## Upstream repo layout

The two modules that ``git clone`` third-party repos land under
``/opt/`` so they persist across ``~`` cleanups:

  * ``/opt/fragattacks`` -- vanhoefm/fragattacks
  * ``/opt/krackattacks-scripts`` -- vanhoefm/krackattacks-scripts
    (vulnerability tester)
  * ``/opt/krackattacks-poc-zerokey`` -- vanhoefm/krackattacks-
    poc-zerokey (offensive PoC used by the KRACK live attack)

The installers run as root because ``/opt`` is not writable by
the login user, then ``chown -R kali:kali`` the tree so the app
can source their Python venvs without another privilege dance.

## Deep-link map

Modules exchange targets through ``window.activate_module(id,
target)``:

```
NetDiscovery AP Surveyor
   |-- pmkid          BSSID
   |-- handshake      BSSID|channel|SSID
   |-- eaphammer      SSID|BSSID|channel
   |-- wifi_attacks   BSSID
   |-- phishkin3      SSID|BSSID|channel
   |-- evil_twin      SSID|BSSID|channel
   `-- wifite         BSSID

KARMA
   `-- evil_twin      SSID          (single SSID from probes)

Probe harvester
   `-- evil_twin      SSID

Wi-Fi Direct
   |-- iot_hacking    SSID  (for printers -> PRET)
   `-- (browser)      https://<setup ip>

IoT setup-AP hunter
   `-- iot_hacking    SSID

Loot
   `-- (self)         module:<name>   -> filter view
```

## Zero hashcat

Per the operator's request, hashcat is intentionally not integrated.
All captured hashes and pcaps live in the loot store and go to
external crackers (wpa-sec for WPA/PMKID, the user's own workstation
for eaphammer MSCHAPv2). This keeps the phone from wasting CPU on
GPU-shaped work and matches the "capture-on-mobile, crack-offline"
engagement style.
