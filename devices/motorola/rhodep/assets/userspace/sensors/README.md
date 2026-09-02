# Sensors

	sudo ./build-fastrpc.sh --install     # once, needs network and a compiler
	sudo ./install.sh
	sudo reboot

Accelerometer, gyroscope, magnetometer, compass, proximity and ambient light all
work, through `iio-sensor-proxy` and the `net.hadess.SensorProxy` D-Bus
interface, so screen auto-rotation and anything else that asks for a sensor
works with nothing to run by hand.

	monitor-sensor
	  === Has accelerometer (orientation: normal)
	  === Has ambient light sensor (value: 147.14, unit: lux)
	  === Has proximity sensor (near: 0)
	  === Has compass (heading: 99.69)

## What was actually missing

The sensors are not on any bus Linux owns. `lsm6dso`/`icm4x6xx` (the IMU) sits on
**I3C**, the magnetometers on the sensor core's own I2C, and both hang off the
SSC inside the ADSP. Neither the mainline nor the vendor device tree describes
those buses, because the application processor does not own them: there is no
node to hang an `st_lsm6dsx` off, and writing one is not a partial solution, it
is a non-solution.

Everything on the AP side of the QMI transport had already been worked out in an
earlier session — the message format, the `fixed32` msg_id, the TLV 0x10 that
makes indications arrive — and the sensor core answered every request while
reporting **no sensors at all**. The reason was one missing sensor:

	registry     0 suids
	accel gyro mag proximity ambient_light   0 suids
	timer resampler suid device_mode         1 suid each

Everything present is internal to the framework; everything missing sits
downstream of `registry`, and on SSC every physical sensor driver reads its bus,
address, interrupt and calibration out of the registry at init. The registry
lives on the **application processor**, and the ADSP fetches it over FastRPC.
Nothing on this system answered, so nothing instantiated.

### The previous conclusion was wrong, and that is the useful part

`docs/CAMERA-SENSORS-FEASIBILITY.md` concluded that closing this meant "porting
listener/reverse-RPC support into mainline fastrpc and writing the userspace
daemon", sized as "a substantially bigger job than anything in this file so
far". That was wrong on the expensive half.

**Reverse RPC is not a kernel feature.** The DSP does not push anything at the
AP. The daemon makes an ordinary *forward* invocation on the static handle 3
(`adsp_listener`) which the DSP answers only when it has a call for us; the
daemon services it and invokes again with the result. Every byte of that travels
over `FASTRPC_IOCTL_INVOKE`, which mainline has had all along. Nothing was added
to `drivers/misc/fastrpc.c` for this — no patch in `kernel/patches/` is part of
it, and the running kernel is unchanged.

**And the userspace does not have to be written either.** Qualcomm published
libadsprpc as [`quic/fastrpc`](https://github.com/quic/fastrpc), BSD-3-Clause,
and it has supported the mainline uapi since 2024:

	src/fastrpc_ioctl.c:
	  /* File only compiled when support to upstream kernel is required */
	  #define FASTRPC_IOCTL_INIT_ATTACH_SNS   _IO('R', 8)

`-DENABLE_UPSTREAM_DRIVER_INTERFACE` is on by default in its own build. It
compiled for aarch64 unmodified, first try. The `sensorspd` path maps straight
onto `FASTRPC_IOCTL_INIT_ATTACH_SNS`, which is the ioctl mainline added for
exactly this and which nothing had ever used here.

So the job was a build, a file tree and three systemd units.

## The pieces

	build-fastrpc.sh          clone, patch and build quic/fastrpc
	rhodep-sscrpcd            the listener daemon (single shot, see below)
	rhodep-ssc-populate       copies the registry out of the stock partitions
	rhodep-ssc-links          /vendor and /mnt/vendor/persist symlinks
	rhodep-ssc-wait           blocks until the sensor core really has sensors
	udev/81-rhodep-ssc.rules  iio-sensor-proxy tags + the mount matrix
	systemd/rhodep-adsp-*     the suspend hook (see "Suspend" below)
	patches/0001-*.patch      our two changes to quic/fastrpc

### The file tree, and why the first attempt crashed the ADSP

The very first run took the whole phone down, and the crash message is the best
documentation in this file:

	qcom_q6v5_pas a400000.remoteproc: fatal error received:
	  err_qdi.c:1045:EF:sensor_process:0x1:SNS_REG_TASK:0xa3:
	  sns_registry_sensor.c:154: SNS_RC_SUCCESS == rc

That is the registry task waking up for the first time ever, asking us for its
registry, being told the file does not exist, and asserting. A sensors-PD
assertion is a fatal error for the whole ADSP: it restarted, took audio with it,
and the phone had to be rebooted.

So the tree goes down **before** the daemon starts. The paths are compiled into
the ADSP firmware and cannot be changed:

| path | what | from |
| --- | --- | --- |
| `/vendor/etc/sensors/sns_reg_config` | where the registry is | vendor partition |
| `/vendor/etc/sensors/config/*.json` | 26 per-part descriptions | vendor partition |
| `/mnt/vendor/persist/sensors/registry/registry/` | 226 registry entries | persist partition |

`vendor` is a logical partition inside `super`, mapped with a loop device at the
liblp extent (`dmsetup` is not an option, `dm_mod` will not load on this kernel).
The persist tree is **copied, not bind mounted**, because the DSP writes
calibration back into it and the stock partition should stay untouched — the
same reasoning `userspace/modem/` uses for the modem's RFS tree. Everything
lives in `/var/lib/rhodep-ssc` and is reached through two symlinks, so undoing
it is one `rm -rf`.

One thing had to be rewritten rather than copied. `sns_reg_config` points the
registry sensor at six files under `/sys/devices/soc0`, and mainline's socinfo
has `soc_id`, `revision`, `family`, `machine` and `serial_number` but none of
`hw_platform`, `platform_subtype`, `platform_subtype_id` or `platform_version`.
Rather than patch socinfo, the values are written into
`/vendor/etc/sensors/socinfo/` and `sns_reg_config` — our file, on our
filesystem — is pointed at them.

### Reading what the DSP asks for

The patch to quic/fastrpc adds an opt-in trace of every `apps_std` call. This is
the same lesson the modem taught this port, and it is worth restating because it
paid for itself twice: **the DSP's file requests are the only log there is.** A
missing file is answered with a plain error, the DSP does not complain, and it
asserts several steps later somewhere unrelated.

	systemctl edit rhodep-sscrpcd
	  [Service]
	  Environment=RHODEP_FASTRPC_TRACE=/var/log/rhodep-fastrpc-trace.log

A healthy first boot is about 2700 lines: the whole registry, then the whole
config directory. It only happens once per ADSP boot — the sensor core caches
the parse — so an empty trace on a restart is normal, not a failure.

### rhodep-sscrpcd is not adsprpcd

The stock `adsprpcd` retries every 100 ms forever. Here a failure is an ADSP
crash that takes audio down, so a retry loop turns one crash into a reboot
storm. `rhodep-sscrpcd` runs the listener once and exits; systemd restarts it at
most three times in five minutes.

### The session budget

The ADSP has **five** FastRPC sessions (`compute-cb@3` to `@7`), and each
libadsprpc client costs **two** of them: one for the domain and one for
`rpcmem`, because this kernel has no `/dev/dma_heap/system` and rpcmem falls
back to a second fastrpc fd.

quic/fastrpc ships a udev rule that auto-starts `adsprpcd` and
`adsprpcd_audiopd`; between them that is four sessions and the sensors PD cannot
open one:

	qcom,fastrpc a400000.remoteproc:...: No session available

`build-fastrpc.sh --install` removes that rule and those units. Neither is needed
here — audio on this port goes over APR, not FastRPC. Enabling
`CONFIG_DMABUF_HEAPS_SYSTEM` would halve the cost per client and is worth doing
the next time the kernel is rebuilt.

## iio-sensor-proxy

It already speaks SSC through `libssc` and was already installed and polling
throughout the earlier investigation; it simply had no sensors to find. Two
things still had to be corrected.

**It only probes two of the four SSC drivers it contains.** Its own rule tags
the fastrpc node with `ssc-light ssc-compass`, so `ssc-accel` and
`ssc-proximity` — both present in the binary, both working here — are never
tried. `81-rhodep-ssc.rules` appends them.

**The accelerometer is 180 degrees out**, and the mount matrix that would say so
is not available anywhere the software looks. `libssc` reads the DSP's
`placement` attribute and this device's `icm4x6xx_0_platform.placement` is twelve
zeros, which iio-sensor-proxy reports and then falls back to identity. The
registry *does* carry an orientation, `icm4x6xx_0_platform.orient`
`{x:-x, y:+y, z:-z}`, but that is the sensor-to-device transform and the DSP
already applies it — measured with `scripts/ssc-stream.py`, the raw stream is in
the standard Android device frame:

	flat, screen up      x -0.24   y +0.10   z +9.39
	upright, port down   x -0.10   y +9.82   z -0.17
	rotated left         x +9.74   y +0.15   z +0.18

What is left is that the panel is mounted 180 degrees round relative to that
frame. The display side already compensates — the static UI is the right way up
— and the accelerometer path does not, so held physically normal the proxy
reported `bottom-up`. `ACCEL_MOUNT_MATRIX="-1, 0, 0; 0, -1, 0; 0, 0, 1"` in the
udev rule fixes it; verified afterwards in all four orientations.

Deriving that from the registry alone would have given the wrong answer, which
is why it was measured.

## Two traps when testing this

**Restarting iio-sensor-proxy under a running session leaves the accelerometer
dead.** It refcounts claims, and a client that claims before the SSC device has
been discovered — Phosh re-claims within milliseconds of the proxy appearing,
while SSC discovery takes a QMI round trip — leaves the refcount at 1 with no
driver attached, and the later claims never trigger the enable. The symptom is
`orientation: undefined` forever, with no error anywhere. On a clean boot the
ordering is the other way round and it works. **Reboot before believing a
negative result here.**

**The sensor core's port moves.** It was 5:14, and after an ADSP restart it came
back on 5:13. Anything that hardcodes the port silently tests nothing;
`rhodep-ssc-wait` and `scripts/ssc-stream.py` look it up.

## Ordering

`rhodep-sscrpcd` is started by udev when `/dev/fastrpc-adsp` appears, about ten
seconds into boot, because the ADSP is a remoteproc and a unit merely ordered
early would find `ConditionPathExists` false and be skipped for good.

iio-sensor-proxy asks the sensor core for its sensors **once** and believes the
answer, so starting it before the registry is in gives a phone with no sensors
and nothing in any log. `rhodep-sscrpcd` does not report started until a real
sensor answers (`rhodep-ssc-wait`, about 1.6 s after the listener attaches), and
a drop-in orders iio-sensor-proxy after it. Measured on a clean boot:

	14:36:06.193  Starting rhodep-sscrpcd.service
	14:36:07.809  rhodep-ssc-wait: accel ambient_light proximity present
	14:36:07.856  Started rhodep-sscrpcd.service

## What was verified, and how

Not "the service is running" — the sensors were moved by hand and the numbers
watched:

| | |
| --- | --- |
| ambient light | 147 lux → 17 → **0.00** covered → 114 uncovered |
| proximity | eight clean near/far transitions with a finger |
| compass | 1099 samples spanning 3.6° to 359.2° through a full turn |
| accelerometer | `normal`, `left-up`, `right-up`, `bottom-up` and `tilt: face-up`, and the UI rotates correctly in all four |
| enumeration | accel, gyro, mag, proximity, ambient_light, interrupt, registry, step_detect, gravity, game_rv, rotv, geomag_rv, fmv, amd, rmd, sig_motion, motion_detect, sensor_temperature, gyro_cal, mag_cal, pedometer |
| across reboots | three consecutive clean boots, sensors up with nothing run by hand |

The accelerometer is a TDK-Invensense **icm4x6xx** on I3C, not the `lsm6dso` the
vendor JSON also describes; the light and proximity sensors are one part, a
sensortek **stk3a5x**.

## What this does not fix

- **Gyroscope, step counter and the fusion sensors are enumerated but not
  exposed to userspace**, because iio-sensor-proxy has no driver for them. They
  answer over SSC — `scripts/ssc-stream.py gyro` prints samples — so anything
  that wants them can have them; there is just no D-Bus consumer today.
- **The ADSP sometimes comes up mute.** Once in these tests it booted, said
  `remote processor adsp is now up`, and then published no QMI services at all
  (node 5 empty), no APR services, and therefore no sound card. This is not
  caused by anything here — on that boot `rhodep-sscrpcd` did not start until 69
  seconds in, long after the audio probe had already given up at 21 s — and it
  did not recur on the three boots since. It belongs with the change that made
  Linux boot the DSPs itself rather than inherit them from the bootloader. If
  audio is missing after a boot, check `qrtr-lookup` for node 5 before
  suspecting the audio stack, and reboot.

## Suspend: this listener is why the phone used to never wake up

`systemd/rhodep-adsp-sleep-hook`, installed as
`/usr/lib/systemd/system-sleep/rhodep-adsp`, stops `rhodep-sscrpcd` before a
suspend and starts it again afterwards. It is not an optimisation, and it is
worth knowing why, because the failure it prevents looks like a display bug and
is not one.

A suspend freezes all of userspace, this listener included. The sensors PD
inside the ADSP goes on making FastRPC calls into it, gets no answer, and
asserts:

	qcom_q6v5_pas a400000.remoteproc: fatal error received:
	  err_qdi.c:1045:EX:sensor_process:0x1:frpc_dsp:0xc4:PC=0xb203dab0
	remoteproc remoteproc1: crash detected in adsp: type fatal error

A sensors-PD assertion is fatal to the **whole** ADSP, so it restarts and takes
audio with it — the same failure mode as a missing registry, just triggered by
the freeze instead. And until kernel patch 0059 the restart did worse than that:
re-registering the LPASS clocks oopsed the kernel inside `__clk_register()`,
which runs holding the global clk `prepare_lock`. The dying worker never
released it, so **every `clk_prepare_enable()` on the system blocked forever**
and the display could not be turned back on. SSH kept working, because nothing
in the network path asks for a clock at runtime. That is the "screen stays black
after a long screen-off, but the phone is otherwise alive" bug.

Measured on the device, in this order:

| | |
| --- | --- |
| stop the listener while awake | the ADSP does **not** assert — so the hook is safe |
| suspend without the hook, 40 s | ADSP asserts, restarts; with patch 0059 the system survives but the sound card is gone and `iio-sensor-proxy` segfaults |
| suspend with the hook, 120 s | no assert, no oops, sound card, sensors and display all intact |

Do not try to solve this by making the listener unfreezable. It would still have
to answer the DSP while the AP is asleep, which is exactly what it cannot do.

### Test suspend with `systemctl suspend`, never with `rtcwake`

`rtcwake -m mem` writes to `/sys/power/state` directly and therefore **skips
`/usr/lib/systemd/system-sleep/` entirely**, so the hook above never runs. The
listener stays alive through the freeze, the sensors PD asserts, and the ADSP
takes audio down with it -- the very failure the hook exists to prevent.

Measured the hard way: five `rtcwake -m mem -s 60` cycles produced five ADSP
crashes,

	qcom_q6v5_pas a400000.remoteproc: fatal error received:
	  err_qdi.c:1045:EX:sensor_process:0x1:frpc_dsp:0xc5
	remoteproc remoteproc1: handling crash #5 in adsp

and `/proc/asound/cards` came back "no soundcards" even though the ADSP itself
recovered and reads `running`. A reboot brings the card back; nothing short of
one does, because the card was registered against the ADSP instance that died.

So a suspend test that uses `rtcwake` is not testing the configuration this
port actually ships, and it will quietly cost you audio while it runs.

## Automatic brightness

The ambient light sensor works and reports correct lux -- 6.6 in a dark room,
147 under a lamp, 0.00 covered, 3000+ under a torch. What was missing is
something to read it:

	/usr/libexec/phosh        12 references to net.hadess.SensorProxy
	/usr/libexec/gsd-power     2 references, "Calculated new target
	                             brightness from ambient: %.1f%%"
	org_kde_powerdevil         0

> **Superseded: KWin does this natively now, and this daemon is installed
> disabled.** On Plasma 6.7 `libkwin.so` carries `ClaimLight`,
> `AutomaticBrightness` and `LightLevel`, and the "Automatic brightness" switch
> in the Settings app drives *that*. Verified working on the device. Running
> this daemon at the same time means two things fighting over one control, so
> `install.sh` no longer enables it. Enable it deliberately only if you also
> turn KWin's switch off:
>
>	systemctl --user enable --now rhodep-autobrightness
>
> The rest of this section is kept because the reasoning still applies -- the
> curve, the filter, the panel artefact -- and because the daemon is the
> fallback if KWin's version is ever unusable here.
>
> **The one real difference, measured:** KWin's implementation shows the panel
> artefact described below. Turning its switch on and covering the sensor
> produces a few glitched horizontal lines for a couple of seconds. This daemon
> avoids them by capping how far one command may move the level; KWin does not
> appear to. That is a panel-driver problem rather than a KWin one, and the
> proper fix is in `nt37701_bl_update_status()` -- see "nice to have" in the
> root README.

**PowerDevil, which drives brightness under Plasma Mobile, has no ambient light
support at all** (0 references to SensorProxy in `org_kde_powerdevil`), so its
own dimming is not reading any sensor. That is what this daemon was written
for, before KWin gained the feature.

### It obeys that switch, and the reason it has to is a trap

Because PowerDevil's ambient support does not exist, the switch in Settings is
wired to nothing — while this daemon, which *is* moving the brightness, used to
ignore it completely. The result was the worst kind of bug: a user turns
automatic brightness **off**, sets the brightness to maximum by hand, and about
45 seconds later the screen dims on its own, with the setting still showing
"off" and nothing in any UI to explain it. The 45 seconds are this daemon's own
`--yield-seconds` back-off expiring:

	brightness set elsewhere (9528, we asked 2675), backing off 45s
	settled at 28% (82.1 lux)

The switch is real, though: KWin keeps it per output in
`~/.config/kwinoutputconfig.json` as `automaticBrightness`, and
`kscreen-doctor -o` reports it. So `evaluate()` reads it and stays completely
out of the way while it is off, re-checking on the file's mtime so that
toggling it in Settings takes effect without restarting anything. On the way
back it forgets its previous target, so it resumes from wherever the user left
the brightness rather than snapping to a stale one.

While it is off the daemon also **releases the ambient light sensor** and drops
its own tick from 0.3 s to `--idle-interval` (5 s), instead of holding a stream
open and waking up three times a second to decide it has nothing to do.
Claiming that sensor is not free: iio-sensor-proxy asks the ADSP's sensor core
for a stream and every sample arrives on the AP as a glink interrupt.

Be careful measuring that, though, because it is easy to overstate. The SSC
ambient sensor is **on-change**, so its traffic depends entirely on whether the
light is moving. Measured with the screen off and steady, releasing the claim
took glink+ipcc from 1.6/s to 0.9/s -- real, but small. An earlier figure of
~32/s was taken with the screen *on*, where the panel lights the sensor and
every UI change modulates it; that number says something about the screen, not
about this daemon, and the two must not be compared.

`--ignore-desktop-setting` restores the old behaviour if you want it. If the
file is missing or unparseable the daemon carries on as before, since being
unable to read the switch is not the same as being told to stop.

Note when testing this: **KWin rewrites that file from its own in-memory state**,
so editing it by hand does not stick for long. The daemon does see the edit --
that part is worth knowing, because it means the toggle in Settings works
immediately -- but the authoritative copy is KWin's.

`rhodep-autobrightness` fills the gap. Three things about it are not obvious and
each came out of getting it wrong first.

### It goes through PowerDevil, not /sys/class/backlight

Writing sysfs works and the screen does dim, but PowerDevil keeps its own idea
of the brightness, so the slider in the shell stays where it was and the next
manual nudge jumps. `setBrightnessSilent` keeps PowerDevil the owner of the
backlight and the slider follows. (`Silent` only suppresses the on-screen popup.)

### It must be a user service

`iio-sensor-proxy` authorises `ClaimLight` through polkit with
`allow_active=yes` / `allow_inactive=no`, so only a process inside the active
seat session may claim the sensor. The same binary run from an ssh shell gets
`Not Authorized: Sensor claim not allowed`, which looks like a bug in the daemon
and is not one.

The unit carries `ConditionEnvironment=XDG_CURRENT_DESKTOP=KDE`, so enabling it
cannot break a Phosh session: under Phosh it does not start, and Phosh's own
ambient support takes over.

### The panel draws lines if the brightness moves too far at once

This is the interesting one, and it is a property of the display rather than of
this daemon.

The first version ramped the brightness itself at 10 Hz. It looked smooth and it
covered the screen in horizontal lines while the level was moving, which cleared
as soon as it settled. The cause is that **PowerDevil already animates**:
measured by sampling sysfs during a single `setBrightnessSilent` from 1300 to
9000,

	458  594  927  1342  1843  2430  3107  3163       ~200 ms, 8 writes

so ramping on top of it restarted that animation ten times a second. On this
panel -- a Novatek NT37701 AMOLED in DSI **command mode**, where brightness is a
DCS command and not a PWM duty cycle -- that many brightness commands collide
with frame transmission.

An A/B on the device settled what actually matters, and it is not the rate:

| | |
| --- | --- |
| 6 large jumps, one sysfs write each | **clean** |
| 6 large jumps through PowerDevil (~8 writes each, animated) | **lines** |
| a slow climb in ~6 % steps through PowerDevil | mostly clean, the odd line |

So the artefact scales with **how far a single change moves the panel**, not
with how many changes there are. `AnimationDurationFactor=0` in `kdeglobals`
makes no difference -- PowerDevil's brightness animation is not the Plasma
animation setting.

The daemon therefore caps how far one command may move the level
(`--max-step`, 10 %) and spaces commands out (`--interval`, 0.3 s), so a full
sweep is a handful of small animations rather than one large one. A `--deadband`
of 6 % keeps it from reacting at all to small changes in the light, which is the
common case and costs nothing.

**The proper fix is in the panel driver**, and is not done: the DCS brightness
write should be synchronised to a frame boundary so it cannot land mid-transfer.
Everything above is working around that from userspace.

### The rest of it

The lux is low-pass filtered before any of this, fast when it rises and slow
when it falls (`--tau-up` 0.35 s, `--tau-down` 5 s), so a hand passing over the
phone does not swing the screen. The curve is
`pct = min + (max-min) * (lux/max_lux)^(1/gamma)` with min 5 %, max_lux 3000 and
gamma 2.5. Measured on the device:

	    6.6 lux  ->  13 %      dark room
	  309   lux  ->  43 %
	  765   lux  ->  60 %
	 2708   lux  ->  96 %
	 3985   lux  -> 100 %      torch

It also gets out of the way: if the brightness settles somewhere that is not
what it asked for -- a slider, the power menu, PowerDevil's own idle dimming --
automatic control pauses for 45 seconds. That test cannot be done from the
`brightnessChanged` signal, because PowerDevil emits it for every step of its
animation and any fixed "ignore our own writes for N seconds" window is either
too short for a long animation or long enough to miss the user; instead a value
that has **stopped moving** and is not the one we asked for is treated as
someone else's.

Checked for the obvious failure mode of any ambient loop, the screen lighting
its own sensor: 15 samples over 30 s in a dark room, lux flat at 6.57 and
brightness flat at 465. No runaway.

	systemctl --user status rhodep-autobrightness
	systemctl --user disable --now rhodep-autobrightness    # to turn it off

`python3-gi` is not held even though the daemon needs it: 18 other installed
packages depend on it, so it is not at risk, and pinning it would block security
updates for nothing.

## Protection

Everything here is covered by the port's existing two layers, so an
`apt upgrade`, an `apt purge` or a stray `rm` cannot quietly undo it.

**Three packages are held** (`userspace/apt/apt-holds.txt`, now 50):
`iio-sensor-proxy`, `libssc2` and `libqrtr-glib0`. `libssc2` matters most: it is
the library that actually speaks to the sensor core and it has exactly one
reverse dependency, `iio-sensor-proxy`, so removing that one autoremoves it too.
`iio-sensor-proxy` is held for its behaviour as much as its presence — this port
depends on the `ssc-accel` and `ssc-proximity` drivers inside it and on the
`IIO_SENSOR_PROXY_TYPE` tag names its own udev rule uses.

Not held, on purpose: `libyaml-0-2`, `libbsd0`, `libmd0`, `libprotobuf-c1`.
They are linked by the FastRPC and SSC libraries, but each has three to eighteen
other installed reverse dependencies, so autoremove will not touch them, and
pinning base libraries to protect something that is not at risk only blocks
security updates.

**Everything this installs is registered with `rhodep-protect-files`**, because
none of it belongs to a package and dpkg would never put any of it back:

	sensors        the four /usr/local/sbin programs, 0755
	sensors-lib    libadsprpc.so.1.0.0, libadsp_default_listener.so.1.0.0, 0755
	sensors-conf   the udev rule, the two units and the iio-sensor-proxy drop-in
	adsp-suspend   /usr/lib/systemd/system-sleep/rhodep-adsp, 0755

Each gets the immutable bit and a snapshot that `rhodep-holds-enforce` restores
from, at boot, after every apt transaction, and on a 30-minute timer — and it
compares content, not just existence, so an edited udev rule is repaired too.

Two things are deliberately left out:

- **The soname symlinks** (`libadsprpc.so.1`) are not registered. The store
  keeps a content snapshot, and restoring a symlink from one would replace it
  with a copy of the library. `rhodep-ssc-links` recreates them instead, on
  every start, which is where boot-time repairs belong.
- **The registry tree** under `/var/lib/rhodep-ssc` is not protected. It is 250
  files and the DSP *writes* calibration back into it, so the immutable bit
  would break the thing it is meant to defend. It heals itself: delete it and
  `rhodep-ssc-populate` rebuilds it from the stock partitions on the next boot.

## Undoing it

	sudo systemctl disable --now rhodep-sscrpcd rhodep-ssc-populate
	sudo rm -rf /var/lib/rhodep-ssc /vendor /mnt/vendor
	sudo rm -f /etc/udev/rules.d/81-rhodep-ssc.rules
	sudo rm -f /etc/systemd/system/iio-sensor-proxy.service.d/10-rhodep-ssc.conf
