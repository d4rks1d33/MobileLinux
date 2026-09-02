"""Raw HCI (Bluetooth controller) access for LE advertising.

This module talks directly to a Bluetooth controller through a raw
``AF_BLUETOOTH`` / ``BTPROTO_HCI`` socket in *user channel* mode, which gives
full control over the advertising payload (something BlueZ/DBus cannot do).

Requirements
------------
* Running as root.
* The ``bluetooth`` service must be stopped and ``hci0`` brought down before
  opening the user channel (the kernel refuses the bind while the device is
  busy).  ``run.sh`` does this for you.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
import struct
import time

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
SOCK_RAW = 3

HCI_CHANNEL_USER = 1

HCI_COMMAND_PKT = 0x01
HCI_EVENT_PKT = 0x04

EVT_COMMAND_COMPLETE = 0x0E
EVT_COMMAND_STATUS = 0x0F
# 0x10 is the Hardware Error event -- the chip's way of saying "I have
# lost my mind, please close and reopen me". On the QCA WCN399x this is
# what gets fired when the LE controller queue overflows after too many
# rapid payload swaps. See engine.py:_recover_heavy for what to do when
# we see one of these.
EVT_HARDWARE_ERROR = 0x10
EVT_NUM_COMPLETED_PACKETS = 0x13
EVT_LE_META_EVENT = 0x3E

# HCI status codes we care about. See Bluetooth Core spec Vol 4, Part E
# §1.3 for the full table.
HCI_STATUS_OK = 0x00
HCI_STATUS_COMMAND_DISALLOWED = 0x0C   # radio busy, LE state wrong, ...
HCI_STATUS_INVALID_PARAMETERS = 0x12   # bad length/opcode, or QCA
                                       # "redundant reset" flavour

# OGF (opcode group field) -- see Bluetooth Core spec Vol 4 Part E §5.4.1.
# The OGF sits in the top 6 bits of the 16-bit opcode. The upstream port
# had OGF_LINK_CTL = 0x01<<10 and CMD_RESET = 0x0003 | OGF_LINK_CTL,
# claiming the result was 0x0C03 -- but 0x01<<10 is 0x0400 and 0x0403 is
# *Periodic Inquiry Mode*, not Reset. Under HCI_CHANNEL_USER on this
# QCA chip that command comes back with Invalid HCI Command Parameters
# (0x12), which is exactly the '0x12 on reset' behaviour our reset()
# was accidentally excusing. The correct HCI Reset lives in the Host
# Controller & Baseband group (OGF=0x03), opcode 0x0C03.
OGF_LINK_CTL = 0x01 << 10
OGF_HOST_CTL = 0x03 << 10
OGF_LE_CTL = 0x08 << 10

# Common opcodes
CMD_RESET = 0x0003 | OGF_HOST_CTL                        # 0x0C03
CMD_LE_SET_ADVERTISING_PARAMETERS = 0x0006 | OGF_LE_CTL  # 0x2006
CMD_LE_SET_ADVERTISING_DATA = 0x0008 | OGF_LE_CTL        # 0x2008
CMD_LE_SET_SCAN_RESPONSE_DATA = 0x0009 | OGF_LE_CTL      # 0x2009
CMD_LE_SET_ADVERTISE_ENABLE = 0x000A | OGF_LE_CTL        # 0x200A

ADV_TYPE_IND = 0x00        # connectable + scannable
ADV_TYPE_DIRECT = 0x01
ADV_TYPE_SCAN_IND = 0x02   # scannable, not connectable
ADV_TYPE_NONCONN_IND = 0x03  # not scannable, not connectable

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _pack_command(opcode: int, params: bytes) -> bytes:
    return bytes([HCI_COMMAND_PKT]) + struct.pack("<HB", opcode, len(params)) + params


class HciError(Exception):
    """Raised when a HCI command fails or the controller is unreachable."""


class HciHardwareError(HciError):
    """The controller emitted a Hardware Error event (HCI event 0x10).

    On the QCA WCN399x this is the signal that the firmware's internal
    LE controller state is corrupted -- typically because the host has
    been swapping the advertising payload faster than the firmware can
    reconcile with the radio's TX schedule. The only in-band recovery
    is to close the HCI user channel and reopen it, which makes the
    kernel driver run `qca_regulator_disable` / `qca_regulator_enable`
    and re-download the firmware -- see engine.py:_recover_heavy.

    The ``code`` attribute holds the vendor-specific Hardware_Code byte.
    On the QCA firmware the codes we've seen are 0x0D (LE controller
    queue overflow) and 0x0F (UART sync lost), both meaning the same
    thing from the caller's point of view.
    """
    def __init__(self, code: int):
        super().__init__(
            "HCI Hardware Error event 0x%02X -- chip needs restart" % code)
        self.code = code


class HciDevice:
    """Minimal raw HCI user-channel interface for LE advertising."""

    def __init__(self, dev_id: int = 0):
        self.dev_id = dev_id
        self._fd = None

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def open(self, retries: int = 12, delay: float = 0.5) -> None:
        if self._fd is not None:
            return
        fd = _libc.socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI)
        if fd < 0:
            raise HciError(f"cannot open HCI socket: {os.strerror(ctypes.get_errno())}")
        sockaddr = struct.pack("HHH", AF_BLUETOOTH, self.dev_id, HCI_CHANNEL_USER).ljust(8, b"\x00")
        last = None
        for _ in range(retries):
            ret = _libc.bind(fd, sockaddr, 8)
            if ret == 0:
                self._fd = fd
                return
            last = os.strerror(ctypes.get_errno())
            # EBUSY: the driver is still releasing the radio - try again.
            time.sleep(delay)
        _libc.close(fd)
        raise HciError(
            f"cannot bind HCI user channel on hci{self.dev_id}: {last} "
            "(is bluetooth running / hci0 still up?)"
        )

    def close(self) -> None:
        if self._fd is not None:
            _libc.close(self._fd)
            self._fd = None

    # -- low level ----------------------------------------------------------

    def _send(self, data: bytes) -> None:
        buf = ctypes.create_string_buffer(data, len(data))
        sent = _libc.send(self._fd, buf, len(data), 0)
        if sent < 0:
            raise HciError(f"HCI send failed: {os.strerror(ctypes.get_errno())}")

    def _recv(self, timeout: float) -> bytes:
        readable, _, _ = select.select([self._fd], [], [], timeout)
        if not readable:
            raise HciError("timeout waiting for HCI event (controller unresponsive?)")
        buf = ctypes.create_string_buffer(512)
        n = _libc.recv(self._fd, buf, 512, 0)
        if n < 0:
            raise HciError(f"HCI recv failed: {os.strerror(ctypes.get_errno())}")
        return buf.raw[:n]

    def command(self, opcode: int, params: bytes = b"", timeout: float = 2.0) -> int:
        """Send a HCI command and wait for its Command Complete event.

        Returns the status byte of the command (0 == success).

        Along the way we watch for other events on the socket:

          * Hardware Error (0x10) always raises :class:`HciHardwareError`
            no matter which command we were waiting for. That event is
            unconditional -- the chip is dead, keep going and the next
            command times out too.
          * Num_Completed_Packets (0x13) is drained silently. It is
            informational (broadcast advertising doesn't really need
            back-pressure) but leaving it in the kernel recv queue means
            our next `_recv` picks it up instead of the event we want.
          * LE Meta events and any other unrelated notifications are
            drained silently for the same reason.
        """
        if self._fd is None:
            raise HciError("HCI device is not open")
        self._send(_pack_command(opcode, params))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HciError(f"HCI command 0x{opcode:04X} timed out")
            data = self._recv(min(remaining, 2.0))
            if not data or data[0] != HCI_EVENT_PKT:
                continue
            evt = data[1]
            if evt == EVT_HARDWARE_ERROR:
                # Hardware_Error_Code is the byte right after the event
                # length. Some controllers omit the byte and just fire
                # the event as a two-byte packet -- default to 0x00 in
                # that case.
                code = data[3] if len(data) >= 4 else 0
                raise HciHardwareError(code)
            if evt == EVT_COMMAND_COMPLETE and len(data) >= 6:
                # data[2] = length, data[3] = num packets, data[4:6] = opcode LE
                (cmd_op,) = struct.unpack("<H", data[4:6])
                if cmd_op == opcode:
                    status = data[6] if len(data) > 6 else 0
                    return status
            elif evt == EVT_COMMAND_STATUS and len(data) >= 6:
                status = data[3]
                (cmd_op,) = struct.unpack("<H", data[4:6])
                if cmd_op == opcode:
                    return status
            # else: unrelated event, keep draining

    def _command_ok(self, opcode: int, params: bytes = b"") -> None:
        status = self.command(opcode, params)
        if status != 0:
            raise HciError(f"HCI command 0x{opcode:04X} failed with status 0x{status:02X}")

    # -- controller commands -------------------------------------------------

    def reset(self, retries: int = 5, delay: float = 0.3) -> None:
        """Reset the controller.

        Right after the radio is released the controller can be mid-
        transition and return COMMAND_DISALLOWED (0x0C), so we retry.
        We used to also silently accept 0x12 "Invalid HCI Command
        Parameters" because the upstream port had OGF_LINK_CTL where
        OGF_HOST_CTL should have been, and the resulting opcode 0x0403
        (Periodic Inquiry Mode) predictably came back with 0x12. That
        is fixed now -- opcode is 0x0C03 and a real 0x12 means a real
        problem.
        """
        last = None
        for _ in range(retries):
            try:
                status = self.command(CMD_RESET)
            except HciError as exc:  # noqa: PERF203
                last = exc
                time.sleep(delay)
                continue
            if status == 0:
                return
            last = HciError(
                "HCI Reset returned status 0x%02X" % status)
            time.sleep(delay)
        raise last  # type: ignore[misc]

    def set_advertising_parameters(
        self,
        interval_min: int = 0x0020,   # 20 ms (Android INTERVAL_MIN)
        interval_max: int = 0x0020,
        adv_type: int = ADV_TYPE_NONCONN_IND,
        own_addr_type: int = 0,
        peer_addr_type: int = 0,
        peer_addr: bytes = b"\x00" * 6,
        channel_map: int = 7,
        filter_policy: int = 0,
    ) -> None:
        params = struct.pack(
            "<HHBBB6sBB",
            interval_min & 0xFFFF,
            interval_max & 0xFFFF,
            adv_type & 0xFF,
            own_addr_type & 0xFF,
            peer_addr_type & 0xFF,
            peer_addr[:6].ljust(6, b"\x00"),
            channel_map & 0xFF,
            filter_policy & 0xFF,
        )
        self._command_ok(CMD_LE_SET_ADVERTISING_PARAMETERS, params)

    def set_advertising_data(self, data: bytes) -> None:
        # HCI spec: length byte followed by a *fixed* 31-octet data
        # field. Some chips accept a shorter tail, but the QCA wcn399x
        # returns 0x12 "Invalid HCI Command Parameters" unless the
        # buffer is exactly 32 bytes. Pad with zeros to match the spec.
        data = data[:31]
        payload = struct.pack("<B", len(data)) + data.ljust(31, b"\x00")
        self._command_ok(CMD_LE_SET_ADVERTISING_DATA, payload)

    def set_scan_response_data(self, data: bytes) -> None:
        # Same shape as set_advertising_data: length + fixed 31-octet
        # field. The scan response is optional (data may be empty) but
        # the length + padding are not.
        data = data[:31]
        payload = struct.pack("<B", len(data)) + data.ljust(31, b"\x00")
        self._command_ok(CMD_LE_SET_SCAN_RESPONSE_DATA, payload)

    def set_advertise_enable(self, enabled: bool) -> None:
        self._command_ok(CMD_LE_SET_ADVERTISE_ENABLE, struct.pack("<B", 1 if enabled else 0))
