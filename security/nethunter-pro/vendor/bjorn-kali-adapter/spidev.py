"""Headless stub of spidev for Bjorn on Kali/pmOS.

Same rationale as the RPi.GPIO stub: the e-paper display driver in
resources/waveshare_epd/ opens an SpiDev() at import time; on a
phone without /dev/spidev* that raises FileNotFoundError and takes
the whole Bjorn.py process down before the web UI can bind.

Providing a no-op SpiDev keeps every method call harmless. Bjorn's
display code catches its own exceptions per draw and moves on.
"""


class SpiDev:
    """Fake SPI device. Every method returns something benign."""

    def __init__(self, *_a, **_kw):
        self.max_speed_hz = 0
        self.mode = 0
        self.no_cs = False
        self.threewire = False
        self.lsbfirst = False
        self.bits_per_word = 8
        self.cshigh = False

    def open(self, *_a, **_kw):
        return None

    def close(self):
        return None

    def xfer(self, data, *_a, **_kw):
        return list(data)

    def xfer2(self, data, *_a, **_kw):
        return list(data)

    def xfer3(self, data, *_a, **_kw):
        return list(data)

    def readbytes(self, n):
        return [0] * int(n)

    def writebytes(self, _data):
        return None

    def writebytes2(self, _data):
        return None
