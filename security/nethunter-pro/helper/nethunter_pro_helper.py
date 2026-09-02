#!/usr/bin/env python3
"""System DBus service that runs commands as root on behalf of the app.

The user authenticates once through polkit; after that every command, including
long streaming ones, runs through this helper. Nothing falls back to pkexec at
runtime, so the password is asked exactly once per session.

Interface org.kali.NetHunterPro.Helper:
  RunCommand(argv, timeout) -> (rc, stdout, stderr)      one-shot
  StartStream(argv) -> handle                             long-running
  StopStream(handle)                                      kill it
  signal StreamLine(handle, line)                         live output
  signal StreamDone(handle, rc)                           finished/killed
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading

from gi.repository import Gio, GLib

BUS_NAME = "org.kali.NetHunterPro.Helper"
OBJECT_PATH = "/org/kali/NetHunterPro/Helper"
IFACE = "org.kali.NetHunterPro.Helper"
POLKIT_ACTION = "org.kali.nethunterpro.run"

INTROSPECTION = f"""
<node>
  <interface name='{IFACE}'>
    <method name='RunCommand'>
      <arg type='as' name='argv' direction='in'/>
      <arg type='i' name='timeout' direction='in'/>
      <arg type='i' name='returncode' direction='out'/>
      <arg type='s' name='stdout' direction='out'/>
      <arg type='s' name='stderr' direction='out'/>
    </method>
    <method name='StartStream'>
      <arg type='as' name='argv' direction='in'/>
      <arg type='s' name='handle' direction='out'/>
    </method>
    <method name='StopStream'>
      <arg type='s' name='handle' direction='in'/>
    </method>
    <method name='Authorize'>
      <arg type='b' name='ok' direction='out'/>
    </method>
    <method name='Ping'>
      <arg type='s' name='reply' direction='out'/>
    </method>
    <signal name='StreamLine'>
      <arg type='s' name='handle'/>
      <arg type='s' name='line'/>
    </signal>
    <signal name='StreamDone'>
      <arg type='s' name='handle'/>
      <arg type='i' name='returncode'/>
    </signal>
  </interface>
</node>
"""


class Helper:
    def __init__(self) -> None:
        self.loop = GLib.MainLoop()
        self.node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        self.connection = None
        self._streams: dict[str, subprocess.Popen] = {}
        self._authorized: set[str] = set()
        self._counter = 0
        self._idle_source = None
        self._arm_idle_exit()

    def _arm_idle_exit(self, seconds: int = 600) -> None:
        if self._idle_source is not None:
            GLib.source_remove(self._idle_source)
        self._idle_source = GLib.timeout_add_seconds(
            seconds, self._maybe_exit
        )

    def _maybe_exit(self) -> bool:
        # Do not exit while a stream is still running.
        if any(p.poll() is None for p in self._streams.values()):
            self._arm_idle_exit()
            return False
        self.loop.quit()
        return False

    def _check_authorization(self, sender: str) -> bool:
        """Ask polkit whether `sender` may run our action, via pkcheck.

        Using the pkcheck binary instead of the DBus API avoids a re-entrant
        call on the system bus from inside our own bus dispatch, which was
        deadlocking. We resolve the caller's pid from the bus name and check
        against that process.
        """
        try:
            dbus_proxy = Gio.DBusProxy.new_sync(
                self.connection, Gio.DBusProxyFlags.NONE, None,
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", None,
            )
            pid_v = dbus_proxy.call_sync(
                "GetConnectionUnixProcessID",
                GLib.Variant("(s)", (sender,)),
                Gio.DBusCallFlags.NONE, 5000, None,
            )
            pid = pid_v.unpack()[0]
        except GLib.Error:
            return False

        try:
            result = subprocess.run(
                ["pkcheck", "--action-id", POLKIT_ACTION,
                 "--process", str(pid), "--allow-user-interaction"],
                capture_output=True, timeout=120,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _emit(self, name: str, params) -> None:
        self.connection.emit_signal(
            None, OBJECT_PATH, IFACE, name, params
        )

    def _stream_worker(self, handle: str, argv: list[str]) -> None:
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True,
            )
        except FileNotFoundError as exc:
            GLib.idle_add(self._emit, "StreamLine",
                          GLib.Variant("(ss)", (handle, f"{exc}\n")))
            GLib.idle_add(self._emit, "StreamDone",
                          GLib.Variant("(si)", (handle, 127)))
            return
        self._streams[handle] = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            GLib.idle_add(self._emit, "StreamLine",
                          GLib.Variant("(ss)", (handle, line)))
        proc.wait()
        GLib.idle_add(self._emit, "StreamDone",
                      GLib.Variant("(si)", (handle, proc.returncode or 0)))

    def handle_call(self, connection, sender, path, iface, method, params, invocation):
        self._arm_idle_exit()

        if method == "Ping":
            invocation.return_value(GLib.Variant("(s)", ("pong",)))
            return

        # The app calls Authorize once at startup: this is the single polkit
        # prompt. After it succeeds the caller is trusted for the life of the
        # helper, so no later command prompts again.
        if method == "Authorize":
            ok = self._check_authorization(sender)
            if ok:
                self._authorized.add(sender)
            invocation.return_value(GLib.Variant("(b)", (ok,)))
            return

        if method in ("RunCommand", "StartStream", "StopStream"):
            # Do the whole thing (authorization + work) in a worker thread, so
            # the polkit round-trip never blocks the DBus main loop, which would
            # deadlock because polkit answers on the same bus.
            threading.Thread(
                target=self._handle_privileged,
                args=(sender, method, params, invocation),
                daemon=True,
            ).start()
            return

        invocation.return_dbus_error(f"{IFACE}.UnknownMethod", method)

    def _handle_privileged(self, sender, method, params, invocation):
        if sender not in self._authorized:
            if not self._check_authorization(sender):
                GLib.idle_add(
                    invocation.return_dbus_error,
                    f"{IFACE}.NotAuthorized", "Not authorized")
                return
            self._authorized.add(sender)

        if method == "RunCommand":
            argv, timeout = params.unpack()
            try:
                p = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=timeout if timeout > 0 else None)
                out = GLib.Variant("(iss)", (p.returncode, p.stdout, p.stderr))
            except subprocess.TimeoutExpired:
                out = GLib.Variant("(iss)", (124, "", f"timed out after {timeout}s"))
            except FileNotFoundError:
                out = GLib.Variant("(iss)", (127, "", f"command not found: {argv[0]}"))
            except Exception as exc:  # pragma: no cover
                out = GLib.Variant("(iss)", (1, "", str(exc)))
            GLib.idle_add(invocation.return_value, out)
            return

        if method == "StartStream":
            (argv,) = params.unpack()
            self._counter += 1
            handle = f"s{self._counter}"
            threading.Thread(
                target=self._stream_worker, args=(handle, argv), daemon=True
            ).start()
            GLib.idle_add(invocation.return_value, GLib.Variant("(s)", (handle,)))
            return

        if method == "StopStream":
            (handle,) = params.unpack()
            proc = self._streams.get(handle)
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            GLib.idle_add(invocation.return_value, None)
            return

    def on_bus_acquired(self, connection, name) -> None:
        self.connection = connection
        connection.register_object(
            OBJECT_PATH, self.node.interfaces[0], self.handle_call, None, None)

    def run(self) -> None:
        Gio.bus_own_name(
            Gio.BusType.SYSTEM, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
            self.on_bus_acquired, None, lambda *_: self.loop.quit())
        self.loop.run()


if __name__ == "__main__":
    Helper().run()
