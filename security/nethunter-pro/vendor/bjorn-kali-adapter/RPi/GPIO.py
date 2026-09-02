"""Headless stub of RPi.GPIO for Bjorn on Kali/pmOS.

Bjorn's e-paper display code imports RPi.GPIO transitively even when
the ``config/shared_config.json`` sets a display type that we're not
using. We ship this stub instead of installing the real RPi.GPIO
(which is a C extension that only builds on Raspberry Pi hardware).

Every function is a no-op that returns None; every attribute answers
truthily. Bjorn's display module catches its own exceptions when a
draw call goes nowhere, so silently accepting every op keeps the
web UI + orchestrator running.
"""

# Pin numbering modes -- Bjorn only reads them, never uses them.
BOARD = 10
BCM = 11
BOTH = 33
FALLING = 32
RISING = 31
PUD_UP = 22
PUD_DOWN = 21
HIGH = 1
LOW = 0
IN = 1
OUT = 0


def setwarnings(_):
    return None


def setmode(_):
    return None


def setup(*_a, **_kw):
    return None


def output(*_a, **_kw):
    return None


def input(_pin):
    return LOW


def cleanup(*_a, **_kw):
    return None


def add_event_detect(*_a, **_kw):
    return None


def add_event_callback(*_a, **_kw):
    return None


def wait_for_edge(*_a, **_kw):
    return None


def event_detected(*_a, **_kw):
    return False


def gpio_function(_pin):
    return -1


class PWM:
    """Software PWM stub. No-op, keeps interface calls alive."""
    def __init__(self, *_a, **_kw):
        pass

    def start(self, _duty):
        return None

    def stop(self):
        return None

    def ChangeDutyCycle(self, _duty):
        return None

    def ChangeFrequency(self, _hz):
        return None
