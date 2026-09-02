"""Headless e-paper backend for Bjorn on Kali / pmOS.

The upstream ``resources/waveshare_epd/epdconfig.py`` picks between
a RaspberryPi / SunriseX3 / JetsonNano implementation at import time
by grepping ``/proc/cpuinfo`` and probing a sysfs path. On our phone
none of those match: the JetsonNano branch wins by fall-through,
which then blows up loading ``sysfs_software_spi.so`` -- a library
that only exists on the Jetson SBCs.

We replace the module with this headless backend that satisfies the
API the ``waveshare_epd/epd2in13*.py`` drivers expect (RST/DC/CS/PWR/
BUSY pin control, SPI transfers, delay_ms) with harmless no-ops.
Bjorn's display module already wraps every draw call in try/except
and logs+continues on failure, so the web UI + orchestrator run
normally and the e-paper output just doesn't go anywhere.

If you ever attach a real e-paper HAT, revert this file to the
upstream one and install RPi.GPIO + spidev natively.
"""

import time
import logging

logger = logging.getLogger(__name__)


class _HeadlessImpl:
    """Fake pin definitions + fake SPI operations."""

    # Pin numbers -- values are meaningless, only the attribute names
    # are consumed by the waveshare_epd/*.py drivers.
    RST_PIN = 17
    DC_PIN = 25
    CS_PIN = 8
    BUSY_PIN = 24
    PWR_PIN = 18

    def __init__(self):
        self.Flag = 0

    # -- lifecycle ---------------------------------------------------
    def module_init(self, *_a, **_kw):
        self.Flag = 1
        return 0

    def module_exit(self, *_a, **_kw):
        self.Flag = 0
        return 0

    # -- pin control -------------------------------------------------
    def digital_write(self, _pin, _value):
        return None

    def digital_read(self, _pin):
        # BUSY_PIN reads must eventually return 0 (idle) or the driver
        # sits forever in `while digital_read(BUSY_PIN): ...` loops.
        return 0

    def delay_ms(self, ms):
        # Real timing matters here even without hardware -- the driver
        # uses these delays as flow control between commands. A short
        # sleep keeps behaviour close to the real display.
        time.sleep(max(0.001, ms / 1000.0))

    # -- SPI ---------------------------------------------------------
    def spi_writebyte(self, _data):
        return None

    def spi_writebyte2(self, _data):
        return None


implementation = _HeadlessImpl()

# Publish the implementation's public callables at module level so
# ``from . import epdconfig; epdconfig.digital_write(...)`` works.
import sys as _sys
for _name in dir(implementation):
    if _name.startswith("_"):
        continue
    setattr(_sys.modules[__name__], _name, getattr(implementation, _name))
