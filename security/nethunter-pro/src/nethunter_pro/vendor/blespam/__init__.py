"""Vendored Bluetooth-LE-Spam Linux backend.

Original: https://github.com/dsdanielpark/Bluetooth-LE-Spam (Android port),
Linux/GTK3 port by the Bluetooth-LE-Spam-linux project. Only the backend
files were vendored in -- the GTK3 UI (app.py) was replaced by the GTK4
libadwaita module at ``nethunter_pro.modules.blespam``.

The four modules kept intact:

  * :mod:`hci`      raw HCI user-channel via ctypes.
  * :mod:`payloads` all 747 packets across 14 categories, byte-for-byte
                    from the Android sources.
  * :mod:`engine`   the advertising loop (thread that cycles the picked
                    packets on the radio).
  * :mod:`radio`    prepare/restore of hci0 around the session.
"""
