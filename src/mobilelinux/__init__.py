"""MobileLinux: automated device porting platform for mobile Linux.

The package is organized in layers that mirror the project's core idea of
separating hardware support from the distribution:

- ``core``      : device schema, models, loader/registry, tool detection, run helpers.
- ``installer`` : install/flash strategy abstraction (fastboot, rescue-dd, ...).
- ``distros``   : distribution backends (kali, ...) that build a rootfs.
- ``desktops``  : desktop environment layers (phosh, plasma, lomiri).
- ``security``  : optional security-tool layers (nethunter-pro, pwnagotchi).
- ``testing``   : modular hardware test suite.
- ``ota``       : release/OTA/security-update machinery.
- ``importers`` : adapters that import device support from other ecosystems (pmOS).
- ``cli``       : the ``mobilelinux`` command-line interface.
"""

__version__ = "0.1.0"
