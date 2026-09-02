"""Importing this package registers every module, in sidebar order.

Order reflects the natural pentester flow:

  Core:      device services + system
  Loot:      central loot browser (up top so it is easy to reach)
  Recon:     passive discovery on the LAN and on the air
  Wi-Fi:     recon (probes, survey) -> capture (PMKID, handshake)
             -> utility (deauth/mdk4/CSA) -> rogue AP variants
             -> post-exploit (fragattacks, kr00k, SSID confusion)
             -> all-in-one (wifite) -> long-lived (pineapple etc)
  Payloads:  actual exploitation
  USB/radio: HID / BLE / SDR-adjacent
  Services:  misc
"""
# Core / device
from . import kali_services    # noqa: F401
from . import net_control      # noqa: F401
from . import docker           # noqa: F401
from . import sysinfo          # noqa: F401
# Loot -- browsed constantly, keep it high on the list
from . import loot             # noqa: F401
# Recon (LAN + IoT)
from . import net_discovery    # noqa: F401
from . import nmap             # noqa: F401
from . import searchsploit     # noqa: F401
from . import routersploit     # noqa: F401
from . import android_rce      # noqa: F401
from . import tv_remote        # noqa: F401
from . import iot_hacking      # noqa: F401
from . import iot_setup        # noqa: F401
from . import iot_recon        # noqa: F401
# Wi-Fi: passive recon
from . import probe_harvester  # noqa: F401
from . import wifi_direct      # noqa: F401
# Wi-Fi: capture attacks
from . import wifi_attacks     # noqa: F401  # WPS
from . import pmkid            # noqa: F401
from . import handshake        # noqa: F401
# Wi-Fi: utility
from . import deauth           # noqa: F401
# Wi-Fi: rogue AP
from . import evil_twin        # noqa: F401
from . import karma            # noqa: F401
from . import eaphammer        # noqa: F401
from . import wifipumpkin      # noqa: F401
from . import phishkin3        # noqa: F401
# Wi-Fi: post-exploit / research
from . import fragattacks      # noqa: F401
from . import ssid_confusion   # noqa: F401
from . import krack            # noqa: F401
from . import kr00k            # noqa: F401
# Wi-Fi: all-in-one + long-lived
from . import wifite          # noqa: F401
from . import pineapple        # noqa: F401
from . import pwnagotchi       # noqa: F401
from . import bjorn            # noqa: F401
from . import driftnet         # noqa: F401
# Payloads / exploitation
from . import payloads         # noqa: F401
from . import set_tool         # noqa: F401
# USB / radio attacks
from . import duckhunter       # noqa: F401
from . import hid              # noqa: F401
from . import usb_arsenal      # noqa: F401
from . import bluetooth        # noqa: F401
from . import bluetoolkit      # noqa: F401
from . import ble_prov         # noqa: F401
from . import ble_track        # noqa: F401
from . import ble_attack       # noqa: F401
from . import blespam          # noqa: F401
from . import blueducky        # noqa: F401
from . import carsenal         # noqa: F401
from . import gps              # noqa: F401
# Services and misc
from . import vnc              # noqa: F401
from . import custom_commands  # noqa: F401
from . import terminal         # noqa: F401
