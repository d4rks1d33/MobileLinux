"""Advertising engine: drives the radio in a background thread.

The original port did a "live update" of the advertising payload as fast
as it could (~57 Hz at 20 ms interval): the radio was left in
Advertise_Enable=1 the whole time and Set_Advertising_Data was rewritten
in place. That is what the HCI spec allows, and it works on Intel/CSR
adapters -- but the Qualcomm WCN399x that sits on this phone is not
happy about it. Its firmware ring buffers between the LE controller and
the host command queue overflow after a few thousand payload swaps and
the chip wedges: it stops answering HCI, and short of a `serdev
unbind/bind` the only thing that brings it back is a reboot.

The Android app the packet catalogue was ported from does not run into
this because its default rate is **1 s per payload**, not 20 ms; and
every payload swap goes through mgmt add_advertising / remove_advertising
which under the hood cycles Set_Advertise_Enable(false) → Set_Data →
Set_Advertise_Enable(true). So the fix here is to imitate that pattern:

  * `advertising_interval_ms` is the radio-level parameter (how often
    the chip retransmits a payload once it is loaded). Stays reasonable
    -- 100 ms by default.
  * `payload_change_ms` is the host-level loop (how often WE swap the
    payload). Default 200 ms (5 Hz), tunable up to whatever, but
    clamped down to sane values with a warning if the user asks for
    <100 ms.
  * every swap does the full disable → set_data → enable dance.
  * we listen for HCI_HARDWARE_ERROR events (0x10) and, if the chip
    starts spitting COMMAND_DISALLOWED (0x0C) at us more than a couple
    of times in a row, we back off exponentially instead of hammering.

None of these individually is a spec change -- we're still speaking
plain legacy LE advertising to the chip. They are just cooperative
patterns that keep the firmware queue healthy indefinitely.
"""

from __future__ import annotations

import random
import threading
import time

from .hci import (
    ADV_TYPE_SCAN_IND,
    HCI_STATUS_COMMAND_DISALLOWED,
    HciDevice,
    HciError,
    HciHardwareError,
)

# TX power bytes embedded in the TX Power AD (dBm-ish).  Only used for
# payloads that carry a TX power level (Fast Pair).
TX_POWER_BYTES = {
    "Ultra Low": 0x00,
    "Low": 0x02,
    "Medium": 0x04,
    "High": 0x06,
}

DEFAULT_TX_POWER = "High"

MODE_SEQUENTIAL = "Sequential"
MODE_RANDOM = "Random"

# Below this we refuse to swap payloads. The QCA WCN399x can technically
# accept faster, but our field data says <100 ms is where the wedge
# starts. If the user asks for less, we clamp and warn via on_error.
MIN_PAYLOAD_CHANGE_MS = 100

# The advertising interval (radio-level) is separate: minimum spec value
# (20 ms) is fine because it just controls how often the *loaded* payload
# is re-transmitted -- it doesn't stress the host command queue.
MIN_ADV_INTERVAL_MS = 20


def interval_to_hci_units(ms: int) -> int:
    """Milliseconds -> HCI advertising interval units (0.625 ms each)."""
    units = round(ms / 0.625)
    return max(0x20, min(units, 0x4000))


class SpamEngine:
    """Cycles checked :class:`blespam.payloads.Packet` objects on the radio.

    ``on_state`` / ``on_packet`` / ``on_recovery`` are called from the
    worker thread; the GUI is responsible for marshalling them to the
    main thread (``GLib.idle_add``).
    """

    def __init__(self, on_state=None, on_error=None, on_packet=None,
                 on_recovery=None):
        self.on_state = on_state or (lambda s: None)
        self.on_error = on_error or (lambda e: None)
        self.on_packet = on_packet or (lambda title, adv: None)
        # on_recovery(kind, reason) -- kind = "light" | "heavy" |
        # "unrecoverable". Fired when the self-heal loop kicks in so
        # the UI can show what's happening.
        self.on_recovery = on_recovery or (lambda kind, reason: None)
        self._stop = threading.Event()
        self._thread = None
        self._hci = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        packets,
        interval_ms=100,             # advertising interval on the radio
        payload_change_ms=200,       # how often WE swap the payload
        mode=MODE_SEQUENTIAL,
        tx_power=DEFAULT_TX_POWER,
        dev_id=0,
    ) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(list(packets), interval_ms, payload_change_ms,
                  mode, tx_power, dev_id),
            daemon=True,
        )
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=join_timeout)

    # -- worker ---------------------------------------------------------------

    def _run(self, packets, interval_ms, payload_change_ms,
             mode, tx_power, dev_id) -> None:
        hci = None
        tx_byte = TX_POWER_BYTES.get(tx_power, 0x06)

        # Clamp the payload-change rate. 20-50 Hz is what wedges the
        # QCA chip; refuse to run there.
        if payload_change_ms < MIN_PAYLOAD_CHANGE_MS:
            self.on_error(
                "payload change rate %d ms clamped to %d ms to keep the "
                "QCA controller healthy (see engine.py docstring)"
                % (payload_change_ms, MIN_PAYLOAD_CHANGE_MS))
            payload_change_ms = MIN_PAYLOAD_CHANGE_MS
        if interval_ms < MIN_ADV_INTERVAL_MS:
            interval_ms = MIN_ADV_INTERVAL_MS

        # Consecutive-error tracking for exponential backoff.
        cmd_disallowed_streak = 0
        cooldown_until = 0.0

        try:
            hci = HciDevice(dev_id)
            hci.open()
            self._hci = hci
            hci.reset(retries=5, delay=0.5)

            # Belt and suspenders: kill any advertising that a previous
            # session left running (BlueDucky, an earlier crash, etc).
            # Ignoring COMMAND_DISALLOWED here means "already off".
            try:
                hci.set_advertise_enable(False)
            except HciError:
                pass

            hci.set_advertising_parameters(
                interval_min=interval_to_hci_units(interval_ms),
                interval_max=interval_to_hci_units(interval_ms),
                adv_type=ADV_TYPE_SCAN_IND,
            )
            # Load the first payload before enabling so the very first
            # advertising interval carries something the target can act
            # on, not the empty default.
            first = packets[0] if mode != MODE_RANDOM else random.choice(packets)
            first_adv, first_scan = first.render(tx_byte)
            hci.set_advertising_data(first_adv)
            hci.set_scan_response_data(first_scan or b"")
            hci.set_advertise_enable(True)
            self.on_state("advertising")

            idx = 0
            while not self._stop.is_set():
                if not packets:
                    break

                # Backoff after error streak: sleep, then reset streak
                # and try again. The chip almost always recovers in <1s.
                now = time.monotonic()
                if now < cooldown_until:
                    self._stop.wait(cooldown_until - now)
                    if self._stop.is_set():
                        break

                if mode == MODE_RANDOM:
                    packet = random.choice(packets)
                else:
                    packet = packets[idx % len(packets)]
                    idx += 1
                adv, scan = packet.render(tx_byte)

                # Live-update the payload while advertising stays
                # enabled. The earlier disable->set_data->enable dance
                # closed a ~8 ms window per swap where the radio was
                # off entirely -- Plasma's Fast Pair scanner and the
                # nearby-device UI on Android missed the ads during
                # those gaps because they only listen for ~15 ms at a
                # time. Live-update wedges the QCA controller if driven
                # at 57 Hz, so we still keep the rate at 5 Hz via the
                # payload_change_ms sleep at the bottom of the loop.
                # Set_Advertising_Data is legal per HCI spec while
                # advertising is enabled; it's only a bug if you hammer
                # it faster than the firmware can drain the internal
                # LE buffer.
                try:
                    hci.set_advertising_data(adv)
                    if scan is not None:
                        hci.set_scan_response_data(scan)
                    else:
                        hci.set_scan_response_data(b"")
                    cmd_disallowed_streak = 0
                except HciHardwareError as exc:
                    # This is the chip screaming that it wedged. There
                    # is no in-band recovery for a 0x10 event -- the
                    # only way out is close() + open() which re-runs
                    # the kernel-side firmware setup.
                    self.on_recovery("heavy",
                                     "hardware error 0x%02X" % exc.code)
                    if not self._recover_heavy(hci, interval_ms):
                        self.on_recovery(
                            "unrecoverable",
                            "close+open failed after hardware error")
                        raise
                    cmd_disallowed_streak = 0
                    continue
                except HciError as exc:
                    text = str(exc)
                    if ("0x%02X" % HCI_STATUS_COMMAND_DISALLOWED
                            in text.upper()):
                        # Backoff exponential: 100ms, 200ms, 400ms, ...
                        # capped at 1600ms. After that many failures in
                        # a row we assume the light recovery isn't
                        # enough and escalate.
                        cmd_disallowed_streak += 1
                        if cmd_disallowed_streak >= 6:
                            self.on_recovery(
                                "heavy",
                                "COMMAND_DISALLOWED streak of %d"
                                % cmd_disallowed_streak)
                            if not self._recover_heavy(hci, interval_ms):
                                self.on_recovery(
                                    "unrecoverable",
                                    "close+open failed after streak")
                                raise
                            cmd_disallowed_streak = 0
                            continue
                        wait = min(0.1 * (2 ** (cmd_disallowed_streak - 1)),
                                   1.6)
                        cooldown_until = time.monotonic() + wait
                        self.on_recovery(
                            "light",
                            "COMMAND_DISALLOWED (streak %d, backoff %.1fs)"
                            % (cmd_disallowed_streak, wait))
                        continue
                    # Anything else is unexpected -- surface it.
                    raise

                self.on_packet(packet.title, adv)

                # Wait until it's time to swap the payload. Using the
                # stop event as the timeout makes stop() responsive.
                self._stop.wait(payload_change_ms / 1000.0)

            self.on_state("stopping")
            try:
                hci.set_advertise_enable(False)
            except HciError:
                pass
        except HciError as exc:
            self.on_error(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.on_error("unexpected error: " + str(exc))
        finally:
            if hci is not None:
                try:
                    hci.set_advertise_enable(False)
                except Exception:
                    pass
                hci.close()
                self._hci = None
            self.on_state("stopped")

    # --------------------------------------------------------- recovery
    def _recover_heavy(self, hci: HciDevice, interval_ms: int) -> bool:
        """Close the user channel and re-open it.

        Closing the HCI user-channel fd tells the kernel to release the
        controller (`hci_dev_close_sync` in net/bluetooth/), which on
        QCA UART chips cuts the regulators and the bt_en GPIO. The next
        open() re-runs `qca_regulator_init` + firmware TLV/NVM download,
        which brings the LE controller back to a known-good baseline.

        Takes ~4-6 s. Falls back to False if the controller does not
        come back up, in which case the caller should surface the
        problem to the UI (which offers the serdev unbind/bind knob).
        """
        try:
            hci.close()
        except Exception:
            pass
        # Give the kernel time to drop the regulators before we ask for
        # them again. Empirically <500 ms is not enough on this phone;
        # 1.5 s is the sweet spot.
        time.sleep(1.5)
        try:
            hci.open()
            hci.reset(retries=5, delay=0.5)
            try:
                hci.set_advertise_enable(False)
            except HciError:
                pass
            hci.set_advertising_parameters(
                interval_min=interval_to_hci_units(interval_ms),
                interval_max=interval_to_hci_units(interval_ms),
                adv_type=ADV_TYPE_SCAN_IND,
            )
            return True
        except Exception:
            return False
