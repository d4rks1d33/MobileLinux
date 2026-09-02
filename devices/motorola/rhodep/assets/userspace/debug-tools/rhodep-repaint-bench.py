#!/usr/bin/env python3
"""Deterministic repaint load for the rhodep display investigation.

Every measurement of the glitched lines so far was a cumulative dmesg count
taken over whatever the phone happened to be doing, so two runs were never
comparable and an improvement could not be told apart from a quiet minute.
This scrolls an identical surface for a fixed time and reports how many frames
landed, so a change can be judged.

The content is uploaded once as a texture and then scrolled, rather than drawn
per frame. A first version painted every frame with cairo and reached 18 fps
with 135 late frames out of 150, which measured Python's raster speed and
nothing about the display -- it did not raise a single DSI error. Scrolling a
ready texture is also what the application list actually does.

GTK4 rather than QML: Kali ships Qt6 without the standalone qml runtime, and
PySide6 here has only QtCore, QtGui and QtWidgets. GTK4's frame clock exposes
add_tick_callback(), which fires once per presented frame and carries the
frame time, which is the measurement wanted.

Prints one line:
    RESULT frames=N secs=S fps=F janks=J worst_ms=W
"""

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

TEX_W = 1080
TEX_H = 4048          # comfortably taller than the panel, 44 rows of 92
ROW_H = 92
SCROLL_PX_S = 900.0   # fixed speed, so pixels moved per run do not vary
REFRESH_HZ = 60.0
# A frame is late once it overruns one and a half refresh periods; below that
# the compositor is merely early or late within its own budget.
JANK_MS = (1000.0 / REFRESH_HZ) * 1.5
WARMUP_FRAMES = 15


def build_texture() -> Gdk.Texture:
    """Stripes, built with byte operations so pycairo is not required."""
    # GDK memory textures here are byte-ordered BGRA.
    def row(b, g, r):
        return bytes((b, g, r, 0xFF)) * TEX_W

    dark = row(0x23, 0x1B, 0x13)
    light = row(0x30, 0x25, 0x1B)
    accent = row(0x8A, 0x5A, 0x2E)

    band = accent * 4 + light * (ROW_H - 4)
    band2 = accent * 4 + dark * (ROW_H - 4)
    pair = band + band2

    data = pair * (TEX_H // (ROW_H * 2))
    data += pair[: TEX_W * 4 * (TEX_H - (TEX_H // (ROW_H * 2)) * ROW_H * 2)]

    return Gdk.MemoryTexture.new(
        TEX_W,
        TEX_H,
        Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
        GLib.Bytes.new(data),
        TEX_W * 4,
    )


class Bench(Gtk.ApplicationWindow):
    def __init__(self, app, duration):
        super().__init__(application=app)
        self.duration = duration
        self.frames = 0
        self.janks = 0
        self.worst = 0.0
        self.t_start = None
        self.t_prev = None

        self.set_title("rhodep repaint bench")
        self.fullscreen()

        picture = Gtk.Picture.new_for_paintable(build_texture())
        picture.set_can_shrink(False)
        picture.set_halign(Gtk.Align.CENTER)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        self.scroller.set_child(picture)

        # A full screen of damage every frame is not what a dragged slider or a
        # single moving row produces, and a command mode panel updates only the
        # region that changed. The small variant keeps the moving area to a
        # strip so the two damage patterns can be compared.
        if os.environ.get("RHODEP_BENCH_SMALL"):
            self.scroller.set_size_request(-1, 320)
            self.scroller.set_valign(Gtk.Align.CENTER)
            self.scroller.set_vexpand(False)
            holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            holder.append(self.scroller)
            holder.set_valign(Gtk.Align.CENTER)
            self.set_child(holder)
        else:
            self.set_child(self.scroller)

        self.add_tick_callback(self.on_tick)

    def on_tick(self, _widget, clock):
        now = clock.get_frame_time() / 1e6  # microseconds -> seconds
        if self.t_start is None:
            self.t_start = now
            self.t_prev = now
            return GLib.SOURCE_CONTINUE

        self.frames += 1
        dt_ms = (now - self.t_prev) * 1000.0
        self.t_prev = now

        # Startup allocates buffers and uploads the texture, which is real work
        # but not what is being measured.
        if self.frames > WARMUP_FRAMES:
            if dt_ms > JANK_MS:
                self.janks += 1
            if dt_ms > self.worst:
                self.worst = dt_ms

        elapsed = now - self.t_start
        if elapsed >= self.duration:
            fps = self.frames / elapsed if elapsed else 0.0
            print(
                "RESULT frames=%d secs=%.2f fps=%.1f janks=%d worst_ms=%.1f"
                % (self.frames, elapsed, fps, self.janks, self.worst),
                flush=True,
            )
            self.get_application().quit()
            return GLib.SOURCE_REMOVE

        adj = self.scroller.get_vadjustment()
        span = max(1.0, adj.get_upper() - adj.get_page_size())
        # Ping-pong rather than wrap, so the direction reverses the way it does
        # when a list is flicked up and then down.
        pos = (elapsed * SCROLL_PX_S) % (2 * span)
        adj.set_value(pos if pos <= span else 2 * span - pos)
        return GLib.SOURCE_CONTINUE


class App(Gtk.Application):
    def __init__(self, duration):
        super().__init__(application_id="net.rhodep.RepaintBench")
        self.duration = duration

    def do_activate(self):
        Bench(self, self.duration).present()


def main() -> int:
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    return App(duration).run([])


if __name__ == "__main__":
    sys.exit(main())
