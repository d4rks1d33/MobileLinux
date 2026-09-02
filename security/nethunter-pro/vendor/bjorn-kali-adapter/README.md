# Bjorn Kali adapter

Bjorn is upstreamed at https://github.com/infinition/Bjorn as a
Raspberry Pi Zero project with a 2.13" e-paper HAT. This directory
contains the pieces that let it run headless on the rhodep phone
(Kali arm64) so the NetHunter Pro ``bjorn`` module can start/stop
it from a switch.

## What it replaces

The upstream install script (`install_bjorn.sh` in the Bjorn repo)
assumes:

  * Username + hostname = `bjorn`. We run as `kali` on this phone.
  * Path `/home/bjorn/Bjorn`. We keep the code at `/opt/Bjorn`.
  * `raspi-config nonint do_spi 0` + `do_i2c 0` to enable the
    HAT pins. Kali doesn't ship `raspi-config` and the phone has
    no GPIO header anyway.
  * `pip install RPi.GPIO spidev` -- both C extensions that only
    build on real Pi hardware.
  * `/boot/firmware/cmdline.txt` + `config.txt` for USB gadget
    mode. pmOS doesn't have a `/boot/firmware` at all.

## What we ship instead

  * `install_bjorn_kali.sh` -- apt block minus `libatlas-base-dev`
    (not in Kali arm64), pip block driven by
    `/opt/Bjorn/requirements_kali.txt` (RPi.GPIO / spidev removed,
    version pins that broke on Python 3.14 loosened), installs the
    stubs, replaces the waveshare display backend, writes the
    systemd unit at `/etc/systemd/system/bjorn.service`. The unit
    is left disabled -- the NetHunter Pro module runs Bjorn as a
    subprocess owned by the app, so systemd stays out of the way.
  * `RPi/GPIO.py` + `RPi/__init__.py` -- headless no-op stubs.
    Every function returns None; every constant is present. Bjorn's
    display code catches its own exceptions per draw, so silently
    accepting every pin operation keeps the web UI + orchestrator
    running.
  * `spidev.py` -- headless SpiDev stub. Same rationale: the
    e-paper drivers open one at import time on a `/dev/spidev*`
    that doesn't exist; without this the import raises and takes
    the whole Bjorn.py process down before the web server binds.
  * `epdconfig.py` -- replacement for
    `resources/waveshare_epd/epdconfig.py`. Upstream grep-detects
    Raspberry Pi / SunriseX3 / JetsonNano at import time; on the
    phone none match and it falls through to loading a Jetson-only
    `sysfs_software_spi.so`. Our version is a headless
    implementation of the same API. The original is preserved as
    `epdconfig.py.upstream` on install.

## How to use it

The installer is idempotent, run once:

```
sudo /opt/Bjorn/kali-adapter/install_bjorn_kali.sh
```

That's what the NetHunter Pro module tells you if it can't find
`/opt/Bjorn/Bjorn.py`. After that the module's switch starts /
stops Bjorn cleanly on demand; systemd is not enabled and never
gets in the way.

## Restore upstream files (if you attach a real e-paper HAT)

  * `mv resources/waveshare_epd/epdconfig.py.upstream
        resources/waveshare_epd/epdconfig.py`
  * `pip install --break-system-packages RPi.GPIO spidev`
  * remove `/opt/Bjorn/RPi/` and `/opt/Bjorn/spidev.py`

Then run Bjorn's own install script.
