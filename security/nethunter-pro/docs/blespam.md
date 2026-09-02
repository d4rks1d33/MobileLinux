# Bluetooth LE Spam — status and open work

Notes on the state of the `blespam` module (`modules/blespam.py` +
`vendor/blespam/*` + `helper/blespam-runner` + `helper/blespam-recover`).
This is a working document for the module inside NetHunter Pro, not a
general Bluetooth writeup — the underlying stack works, the questions
here are about how our packet flood behaves once it hits the air.

## Where we are today

Everything below is on the phone (rhodep, Motorola Edge 20 Fusion,
Qualcomm WCN399x internal BT on UART, kernel 7.2.0-rc5 pmOS).

### What is done and verified working end to end

* The stand-alone GTK3 app that used to live at
  `/opt/Bluetooth-LE-Spam-linux/` is gone; `install.sh` deletes it
  along with `/opt/Bluetooth-LE-Spam` and
  `/usr/share/applications/bluetooth-le-spam.desktop`.
* The backend (`hci.py`, `payloads.py`, `engine.py`, `radio.py`) is
  vendored under `src/nethunter_pro/vendor/blespam/`, imported by the
  module and the runner.
* The runner (`/usr/libexec/nethunter-pro-blespam`) reads a JSON
  config on argv, prepares the radio, drives the engine, streams
  NDJSON events on stdout, and restores the radio on SIGTERM.
* The recovery helper (`/usr/libexec/nethunter-pro-blespam-recover`)
  is the serdev unbind/bind dance for `hci_uart_qca` on `serial0-0`.
  It brought hci0 back from the "Operation not supported (95)" wedge
  in every attempt so far, no reboot needed. It also restarts
  bluetooth.service on the way out so the Plasma tray icon comes
  back.
* Fixed several HCI-level bugs the upstream port carried:
  * `set_advertising_data` / `set_scan_response_data` now pad the
    payload to the spec-required 31 octets (QCA rejects the short
    form with `0x12`).
  * `CMD_RESET` used the wrong OGF: `OGF_LINK_CTL | 0x0003` is
    `0x0403` (Periodic Inquiry Mode), not Reset. Fixed to use
    `OGF_HOST_CTL | 0x0003` = `0x0C03` (real HCI Reset).
  * `HciHardwareError` (event 0x10) and drain of Num Completed
    Packets (0x13) added to `command()` so unexpected events don't
    clog the recv queue.
  * `EVT_LE_META_EVENT` and its sub-events are drained silently.
* Ships two spin rows: **Advertising interval** (radio-level,
  100 ms default) and **Payload change rate** (host loop, 200 ms
  default, min 100 ms). The min clamp is what keeps the QCA from
  wedging after a few minutes; the old port had these coupled and
  ran at ~57 Hz, which wedged the chip in seconds.
* Loop was tried both ways:
  1. Android-style `disable → set_data → enable` per payload.
     The chip stayed happy for hours but there was a ~8 ms window
     of "advertising off" per swap and receivers missed the ads
     during those windows — this is what the user observed as
     "the popups no longer arrive".
  2. **Current behaviour**: advertising is enabled once at start-up
     and left enabled for the whole run; `set_advertising_data` is
     called live at the payload_change rate. Verified with `btmon`:
     one `Set_Advertise_Enable(true)` at start, N `Set_Advertising_Data`
     mid-flight, one `Set_Advertise_Enable(false)` at stop.

### The stand-alone app is retired

* `.desktop`, `/opt/Bluetooth-LE-Spam-linux/` and `/opt/Bluetooth-LE-Spam/`
  are removed by `install.sh` on install.
* Users find the module inside the NetHunter Pro sidebar under
  "USB / radio attacks", above BlueDucky.

## The bug still open

**Symptom the user described in the last session**:

  * Fire the module from the GUI with any packet ticked (e.g.
    "New AirPods Pro White" from Apple - New Device).
  * Plasma's Bluetooth icon disappears (expected — the runner masks
    `bluetooth.service` and kills bluetoothd so it can take the
    controller under `HCI_CHANNEL_USER`).
  * On a Motorola phone next to the rhodep with Fast Pair enabled,
    no "Nearby device" popup appears.
  * However: `btmon` on the rhodep shows the chip is actually TXing.
    Setting advertising through `bluetoothctl` / `btmgmt add-adv`
    directly also works — the target phone sees that. Something
    about the packet payloads the module ships makes them invisible
    to the Motorola.

**What we ruled out during that session**:

  * The radio is not off during a run. `btmon` shows one
    `Set_Advertise_Enable(true)` at start, ~90 `Set_Advertising_Data`
    events during the run, and one `Set_Advertise_Enable(false)` at
    stop. TX bytes in `hciconfig hci0` go up. There is no
    Hardware_Error event on the wire.
  * The HCI commands are formed correctly: 31-byte padded data,
    channel map 0x07 (channels 37/38/39), ADV_SCAN_IND type,
    100 ms advertising interval, Success (0x00) on every
    `Command Complete`.
  * The engine is not silently wedging: after ~90 packets on the
    wire, `hciconfig hci0` still reports `errors:0`.
  * The stand-alone app *from before* the wedge fix could produce
    popups on the same target with the same packet catalogue. So
    the packet bytes themselves are known-good against this
    hardware pair — the port did not break them.
  * The channel names Plasma shows for the target device
    ("TV de la sala de estar") are actually **our advertisements**
    getting picked up by the Motorola with a very generic label,
    not a different device on the LAN. Confirmed by killing the
    runner: the entry disappears from the Motorola.

**What is likely wrong**:

  1. The Motorola treats our advertisements as generic BLE devices
     rather than "Fast Pair" / "AirPods" popups. The rhodep chip
     is broadcasting the bytes; the target scanner sees them but
     does not upgrade them to the vendor-specific popup UX.
  2. Candidate causes to investigate next session:
     * **Advertising type**: we use `ADV_SCAN_IND` (0x02,
       scannable + non-connectable). Apple/Fast Pair specs assume
       `ADV_IND` (0x00, connectable + scannable) for the pairing
       popups. Try switching for the Apple/Fast Pair categories,
       leave `ADV_SCAN_IND` for the pure spam (Lovespouse, iOS 17
       crash).
     * **Own address type**: we hand `own_addr_type=0` (Public
       address = the chip's stored BD_ADDR, `02:00:60:3B:D3:94`).
       Fast Pair and Apple Continuity both key the popup on the
       address never having been seen before; Android caches the
       LAST address that hosted a given payload for 30-60 s so
       repeat popups get suppressed. The Android app the
       catalogue was ported from rotates through **random static
       addresses** (`own_addr_type=1` + fresh MAC per payload)
       exactly to work around this cache. We do not rotate.
       That is very likely why "one popup, then nothing" is what
       users see.
     * **TX power AD**: the `TX_POWER_BYTES` field in the AD only
       tells the target what dBm to expect; the actual radio TX
       power is set by NVM on the QCA and is not configurable at
       runtime. If NVM says +8 dBm and the AD says +6, some Fast
       Pair implementations reject the packet. Try dropping the
       TX Power AD entirely for a test.
     * **Scan response**: we `set_scan_response_data(b"")` when
       the packet has no scan response. Some chips treat an empty
       scan response as "no scan response set" and behave
       differently from clearing it explicitly with the "no scan
       response allowed" ADV type. The upstream Android app never
       clears the scan response — it just doesn't set one for
       `ADV_NONCONN_IND` payloads. Try removing the empty-scan-
       response write for packets without a scan response.
  3. Regression bisect target: the last time popups worked was
     right before the "wedge prevention" work landed. Before the
     current `main`, blespam-from-terminal produced popups; from
     the GUI it did too. The wedge fix landed several changes at
     once (rate limit, disable/enable cycle, own_addr_type
     wording, adv_type constant reused from radio.py). Revert them
     one at a time on top of `main` and rerun the Motorola test to
     pin the exact regression.

## Where to pick up

Concrete work items for next session, in order:

1. **Rotate the advertiser address per payload.** Cheapest fix, most
   likely to restore the popup UX. Change
   `HciDevice.set_advertising_parameters(own_addr_type=...)` to accept
   `1` (random static) and pass a fresh `Set_Random_Address` before
   each `Set_Advertising_Data` in the engine's payload loop.
2. **Add a per-category adv_type override.** Apple Continuity /
   Fast Pair go via `ADV_IND` (0x00). Everything else stays
   `ADV_SCAN_IND`. `payloads.Packet` already exposes `target`; use
   that as the switch.
3. **Reproduce with `bluetoothctl` from userland** to confirm the
   above is what Android really wants. If `add-adv` with random
   static + ADV_IND produces a popup on the Motorola using the
   same bytes we're sending, we know exactly which shape to
   emulate.
4. **Do NOT reintroduce the disable/enable per payload cycle** —
   the previous session confirmed that closes an 8 ms hole every
   time and receivers miss the ad. Keep advertising enabled;
   throttle the payload swap rate to 5 Hz to protect the chip.
5. **Keep the recovery helper** — even after the popup regression
   is fixed, the QCA does wedge occasionally under other tools
   (BlueDucky, wpa_supplicant contention), and the helper is the
   only way back short of a reboot.

## Files touched in this session series

* `src/nethunter_pro/modules/blespam.py` — GTK4 UI, two rate rows,
  Recover button.
* `src/nethunter_pro/vendor/blespam/hci.py` — Reset opcode fix,
  31-octet pad, Hardware Error event class, drain of
  Num Completed Packets.
* `src/nethunter_pro/vendor/blespam/engine.py` — separate radio
  interval vs payload change interval, live-update advertising
  data (current), self-heal loop on Hardware Error /
  COMMAND_DISALLOWED streaks.
* `src/nethunter_pro/vendor/blespam/radio.py` — `restore_radio`
  now really starts bluetoothd (used to have a broken
  `... & || true`).
* `helper/blespam-runner` — NDJSON runner. Emits `ready`, `state`,
  `packet`, `error`, `recovery`.
* `helper/blespam-recover` — serdev unbind/bind. Waits up to 12 s
  for hci0 to reach `UP RUNNING` after rebind, then restarts
  bluetooth.service.
* `install.sh` — installs both helpers, removes stand-alone leftovers.
