"""Run commands off the GTK main thread, with live output and cancellation.

User commands run directly. Root commands go through the persistent DBus helper
so the password is asked once; if the helper is missing we fall back to pkexec.

Two ways to run:
  run_async  - collect all output, deliver a Result once (short commands).
  Process    - stream output line by line and allow stopping the command
               (nmap, wifipumpkin, candump, reaver, ... anything long).
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

HELPER_NAME = "org.kali.NetHunterPro.Helper"
HELPER_PATH = "/org/kali/NetHunterPro/Helper"
HELPER_IFACE = "org.kali.NetHunterPro.Helper"


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# ---- short commands, collected -------------------------------------------

def _run_local(argv: list[str], timeout: int | None) -> Result:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return Result(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timed out after {timeout}s")
    except FileNotFoundError:
        return Result(127, "", f"command not found: {argv[0] if argv else '?'}")
    except Exception as exc:  # pragma: no cover
        return Result(1, "", str(exc))


_helper_proxy: Gio.DBusProxy | None = None
_helper_checked = False


def _get_helper() -> Gio.DBusProxy | None:
    global _helper_proxy, _helper_checked
    if _helper_checked:
        return _helper_proxy
    _helper_checked = True
    try:
        proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None,
            HELPER_NAME, HELPER_PATH, HELPER_IFACE, None,
        )
        proxy.call_sync("Ping", None, Gio.DBusCallFlags.NONE, 5000, None)
        _helper_proxy = proxy
    except GLib.Error:
        _helper_proxy = None
    return _helper_proxy


def helper_available() -> bool:
    return _get_helper() is not None


def authorize() -> bool:
    """Ask the helper to authorize this client once, at app startup.

    This is the single polkit prompt. After it succeeds every root command runs
    without prompting again for the life of the app.
    """
    proxy = _get_helper()
    if proxy is None:
        return False
    try:
        res = proxy.call_sync(
            "Authorize", None, Gio.DBusCallFlags.NONE, 120000, None
        )
        return bool(res.unpack()[0])
    except GLib.Error:
        return False


def _run_root(argv: list[str], timeout: int | None) -> Result:
    proxy = _get_helper()
    if proxy is None:
        return _run_local(["pkexec", *argv], timeout)
    try:
        res = proxy.call_sync(
            "RunCommand",
            GLib.Variant("(asi)", (argv, timeout or 0)),
            Gio.DBusCallFlags.NONE,
            (timeout + 5) * 1000 if timeout else -1, None,
        )
        rc, out, err = res.unpack()
        return Result(rc, out, err)
    except GLib.Error as exc:
        return Result(1, "", f"helper error: {exc.message}")


def run_async(
    command: str | list[str],
    callback: Callable[[Result], None],
    *,
    root: bool = False,
    timeout: int | None = 120,
) -> None:
    argv = shlex.split(command) if isinstance(command, str) else list(command)

    def deliver(result: Result) -> bool:
        # An exception raised in a GLib idle callback aborts the whole process
        # with a core dump -- which is how the app was seen to vanish when the
        # Docker engine start/stop callback touched the UI while the daemon was
        # reconfiguring the network. A module callback must never be able to take
        # the app down, so it is contained here.
        try:
            callback(result)
        except Exception:
            import traceback
            traceback.print_exc()
        return False  # one-shot

    def worker() -> None:
        try:
            result = _run_root(argv, timeout) if root else _run_local(argv, timeout)
        except Exception as exc:  # never let the worker thread die silently
            result = Result(1, "", "run_async worker failed: %s" % exc)
        GLib.idle_add(deliver, result)

    threading.Thread(target=worker, daemon=True).start()


def which(tool: str) -> bool:
    return _run_local(["sh", "-c", f"command -v {shlex.quote(tool)}"], 5).ok


# ---- long commands, streamed and stoppable -------------------------------

class Process:
    """A running command with live output and a stop button.

    on_line(text) is called on the GTK thread for each output line.
    on_done(code) is called on the GTK thread when it finishes or is stopped.

    Root processes stream through the DBus helper, which is already authorized,
    so they never invoke pkexec at runtime and never re-prompt. Non-root
    processes run locally. If root is requested but the helper is missing, we
    fall back to a local pkexec run (which will prompt).
    """

    def __init__(
        self,
        command: str | list[str],
        on_line: Callable[[str], None],
        on_done: Callable[[int], None],
        *,
        root: bool = False,
    ) -> None:
        self.on_line = on_line
        self.on_done = on_done
        self.root = root
        self._proc: subprocess.Popen | None = None
        self._stopped = False
        self._handle: str | None = None
        self._sub_id: int | None = None

        if isinstance(command, str):
            self.argv = ["sh", "-c", command]
        else:
            self.argv = list(command)

    def start(self) -> None:
        if self.root and helper_available():
            self._start_via_helper()
        else:
            threading.Thread(target=self._run_local, daemon=True).start()

    # -- helper-backed streaming (root, no re-prompt) ---------------------

    def _start_via_helper(self) -> None:
        proxy = _get_helper()
        connection = proxy.get_connection()
        # Subscribe first so no early lines are missed.
        self._sub_id = connection.signal_subscribe(
            HELPER_NAME, HELPER_IFACE, None, HELPER_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_signal, None,
        )

        # Async so the GTK thread never blocks waiting on the helper/polkit.
        def on_reply(src, res, _user):
            try:
                (self._handle,) = src.call_finish(res).unpack()
            except GLib.Error as exc:
                self.on_line(f"helper error: {exc.message}\n")
                self.on_done(1)
                if self._sub_id is not None:
                    connection.signal_unsubscribe(self._sub_id)
                    self._sub_id = None

        proxy.call(
            "StartStream", GLib.Variant("(as)", (self.argv,)),
            Gio.DBusCallFlags.NONE, 600000, None, on_reply, None,
        )

    def _on_signal(self, _conn, _sender, _path, _iface, signal_name, params, _user):
        if signal_name == "StreamLine":
            handle, line = params.unpack()
            if handle == self._handle:
                self.on_line(line)
        elif signal_name == "StreamDone":
            handle, rc = params.unpack()
            if handle == self._handle:
                code = 130 if self._stopped else rc
                self.on_done(code)
                if self._sub_id is not None:
                    _conn.signal_unsubscribe(self._sub_id)
                    self._sub_id = None

    # -- local streaming (non-root, or root without helper) ---------------

    def _run_local(self) -> None:
        argv = ["pkexec", *self.argv] if self.root else self.argv
        try:
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True,
            )
        except FileNotFoundError as exc:
            GLib.idle_add(self.on_line, f"{exc}\n")
            GLib.idle_add(self.on_done, 127)
            return
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            GLib.idle_add(self.on_line, line)
        self._proc.wait()
        code = 130 if self._stopped else (self._proc.returncode or 0)
        GLib.idle_add(self.on_done, code)

    @property
    def running(self) -> bool:
        if self._sub_id is not None:
            return True
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        self._stopped = True
        if self._handle is not None:
            proxy = _get_helper()
            if proxy is not None:
                try:
                    proxy.call_sync(
                        "StopStream", GLib.Variant("(s)", (self._handle,)),
                        Gio.DBusCallFlags.NONE, 5000, None,
                    )
                except GLib.Error:
                    pass
            return
        if self._proc is not None and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
