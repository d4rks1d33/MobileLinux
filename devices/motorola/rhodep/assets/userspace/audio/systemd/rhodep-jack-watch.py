#!/usr/bin/python3
"""Switch the audio output when the headset jack is plugged in or out.

PipeWire cannot do this itself here. ACP is disabled for this card, because its
PCMs are DPCM front ends that cannot be opened until a route exists, so the sink
is a statically declared node rather than an ACP profile and there is no UCM
driven jack handling to hook into. Speaker and Headphones are both MultiMedia1
and share hw:0,0, so switching is a UCM device change that the sink never sees.

Detection itself comes from the codec's MBHC block, which reports through an
input device. It only works while the codec is kept awake; see
udev/90-rhodep-wcd937x-jack.rules.

Known limitation: unplugging while something is already playing switches the
route but leaves the speaker silent until that stream is reopened. See
rhodep-audio-out for why, it is an ASoC ordering problem and not something this
script can work around.
"""

import os
import re
import struct
import subprocess
import sys

JACK_NAME = "Headset Jack"
AUDIO_OUT = "/usr/local/bin/rhodep-audio-out"

# linux/input-event-codes.h
EV_SW = 0x05
SW_HEADPHONE_INSERT = 0x02
SW_LINEOUT_INSERT = 0x06

# struct input_event: struct timeval (2 * long) + __u16 + __u16 + __s32
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)


def find_jack_device():
    """Return /dev/input/eventN for the headset jack, by name.

    The event number is not stable across boots, so the name is the only sane
    way to find it.
    """
    try:
        with open("/proc/bus/input/devices") as f:
            blocks = f.read().split("\n\n")
    except OSError as exc:
        sys.exit("cannot read /proc/bus/input/devices: %s" % exc)

    for block in blocks:
        if JACK_NAME not in block:
            continue
        match = re.search(r"\b(event\d+)\b", block)
        if match:
            return "/dev/input/" + match.group(1)
    return None


def set_output(output):
    try:
        subprocess.run([AUDIO_OUT, output], check=True,
                       stdout=subprocess.DEVNULL)
        print("jack: switched to %s" % output, flush=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        print("jack: failed to switch to %s: %s" % (output, exc), flush=True)


def jack_is_plugged():
    """Read the current state from ALSA rather than assuming one at startup.

    Without this a headset already plugged in when the service starts would be
    ignored until the next physical change.
    """
    try:
        out = subprocess.run(
            ["amixer", "-c", "0", "cget", "iface=CARD,name=Headphone Jack"],
            capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(": values="):
            return line.endswith("on")
    return None


def main():
    path = find_jack_device()
    if not path:
        sys.exit("no input device named %r; is the sound card up?" % JACK_NAME)

    plugged = jack_is_plugged()
    if plugged is not None:
        set_output("headphones" if plugged else "speaker")

    with open(path, "rb") as dev:
        while True:
            data = dev.read(EVENT_SIZE)
            if not data or len(data) < EVENT_SIZE:
                break
            _, _, etype, code, value = struct.unpack(EVENT_FORMAT, data)
            if etype != EV_SW:
                continue
            if code in (SW_HEADPHONE_INSERT, SW_LINEOUT_INSERT):
                set_output("headphones" if value else "speaker")

    sys.exit("jack input device disappeared")


if __name__ == "__main__":
    main()
