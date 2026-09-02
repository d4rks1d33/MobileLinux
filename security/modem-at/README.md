# modem-at — an AT console for the modem

The Moto G82's modem answers AT commands, and nothing in this port used them
until now. It is worth having on its own terms: it is a third window onto the
radio, independent of the two that already existed.

```
              ┌── AT    modem state, over glink
              │
   Linux ─────┼── QMI   modem services, over qrtr
              │
              └── IPA   the data path
```

That independence is the point. The AT path keeps working when ModemManager is
not running, when the IPA driver is held out of the boot, and when QMI is busy
or wedged. It also does not depend on the CoreSight debug fuses, which on this
handset are **closed** — see `docs/watchdog-ipa-lte-wip/HANDOFF.md`.

## Install

On the phone, as root:

```sh
./install.sh
```

## Use

```sh
sudo rhodep-modem-at              # interactive console
sudo rhodep-modem-at ATI          # one command
sudo rhodep-modem-at -c DATA1 ATI # the other channel that answers
sudo rhodep-modem-at -l           # what the modem advertises
```

In the console, `?` prints a crib sheet and Ctrl-D exits. Everything else is
sent verbatim — the tool is a pipe, not a wrapper, so any command the modem
understands works whether or not this tool has heard of it.

```
at> ATI
Manufacturer: Motorola Mobility
Model: 334
Revision: MPSS.HI.4.3.4-00494-MANNAR_GEN_PACK-1.24452.133  1  [Sep 13 2024]
IMEI: 351287971159510
OK

at> AT$QCSIMSTAT?
$QCSIMSTAT: 0,SIM INIT COMPLETED
OK
```

## How it works, and why that matters

There is no serial port here. The modem's glink edge advertises channels —

```
6080000.remoteproc:glink-edge.DS.-1.-1
6080000.remoteproc:glink-edge.DATA1.-1.-1
6080000.remoteproc:glink-edge.DATA2.-1.-1 … DATA4, DATA11
```

— and no driver binds any of them. `rpmsg_ctrl` can create an endpoint on a
named channel with `RPMSG_CREATE_EPT_IOCTL`, which produces an ordinary
`/dev/rpmsgN` you can read and write. `DS` and `DATA1` turn out to speak AT.

Two things will confuse you if nobody says them:

* **A write can fail with `EBUSY`.** That is glink saying the modem has not
  posted a receive buffer yet, not the port being dead. Open the endpoint
  *blocking* and bound the wait; this tool uses `SIGALRM` so it can never hang.
* **The port echoes.** Send `AT` and the first line back is `AT`. The tool
  drains before each command and strips the echo, so what you see is the
  answer.

`DIAG` is a different story: it is **not** advertised, and creating an endpoint
for it succeeds while `open()` then fails with `EINVAL`, because
`qcom_glink_create_local()` never receives an `OPEN_ACK` from the modem. Nor
will the modem switch an AT port into DIAG mode: `AT$QCDMG` answers `ERROR`.
So the modem's own F3 log is not reachable on this firmware, and this console
is the closest thing to it.

## The command list comes from the modem

`AT+CLAC` makes the modem enumerate what it implements, and it does: **256
commands**, 54 of them Qualcomm or Huawei-style vendor ones. That list is the
authority, not a crib sheet written from memory.

```sh
sudo rhodep-modem-at --commands
```

prints it grouped, and **executes nothing**:

```
the modem implements 256 commands

do not send casually: 12
  $QCACQDBC $QCCLR $QCDGEN $QCDMR $QCPWRDN $QCRMCALL $QCTER $QCVOLT
  +CLCK +CPWD +CRES +CSAS
vendor ($ and ^): 54
standard (+): 150
basic / S-registers: 40
```

## Probing something unknown

Do not send unknown vendor commands in a batch. Ten of them were once sent in
one go, one wedged the application processor hard enough to need the power
button, and because nothing was written down first there was no way afterwards
to tell which. That is what `--probe` is for:

```sh
sudo rhodep-modem-at --probe 'AT$QCSYSMODE?'
```

It writes the command into `/dev/kmsg` **before** sending it, so it lands in
the ramoops console record and survives a reset, then reports whether the
machine is still alive:

```
modem-at: sending AT$QCSYSMODE? (uptime 330.42)
modem-at: survived AT$QCSYSMODE? (uptime 330.57)
```

If the phone goes down, the next boot tells you exactly what was in flight:

```sh
sudo bash -c 'f=$(ls -t /root/pstore-history/*console* | head -1); grep -a modem-at "$f"'
```

Commands on the do-not-send list are refused unless you add `--force`. Three of
them -- `$QCVOLT`, `$QCDMR`, `$QCTER` -- are refused because they are *unknown*
and were in the batch that wedged the phone, not because they are known to be
bad. Someone will have to find out which one it was, one `--probe` at a time.

## What it is good for

Poking at the radio without ModemManager in the way:

* `AT+CESQ` for RSRP and RSRQ, which is the honest signal reading
* `AT+CEREG?` and `AT+COPS?` for registration and access technology
* `AT+CEER` for a cause when something fails
* `AT+CGDCONT?` to see the APNs the modem actually has

It found a use-after-free in mainline's glink, too --
`kernel/patches/0080-rpmsg-glink-take-a-reference-for-the-rcid-entry.patch` --
because using this console and then having the modem crash is exactly the
sequence that trips it.

It was also used to establish something about the LTE reset that nothing else
could: polled continuously across an attach, **the modem answers every command
until 200 ms before the SoC disappears**, reporting itself registered with no
cause to report. Both processors look healthy right up to the instant of the
reset.

## Careful

This is the real modem of a working phone. Reads are harmless; writes are not
necessarily. `AT+CFUN=0` will drop the radio, and some vendor commands persist
across reboots. If the phone is your only way in, remember that WiFi does not
depend on the modem but mobile data does.

## What each command does

    rhodep-modem-at --explain +CEREG     # no modem needed, no root needed
    rhodep-modem-at --explain csq        # bare names resolve to +CSQ
    at> help $QCVOLT                     # same thing inside the console

All 256 commands the modem reports through `AT+CLAC` have an entry. Where the
meaning of a vendor command is not actually known, the entry says UNKNOWN
rather than guessing, because a confident wrong description is worse than
none: `$QCVOLT`, `$QCDMR`, `$QCTER`, `$QCCLR` and `+CCMMD` are all marked that
way. Entries for commands on the do-not-send list repeat the reason they are
refused.

The lookup normalises what you type, so `+CEREG`, `cereg`, `AT+CEREG?` and
`at+cereg=?` all find the same entry, and a query that only partly matches
gets suggestions instead of a wrong answer. It deliberately refuses to guess:
`help zzz` says there is no entry rather than matching `ATZ`, which is a modem
reset.

## Sending an SMS

AT+CMGS does not answer OK. It answers with a "> " prompt and then waits for
the message body, terminated by Ctrl-Z. The console used to sit there waiting
for an OK that was never coming, which made SMS impossible from here.

    sudo rhodep-modem-at --sms "+5491122334455" "Hola"

or inside the console, either the shortcut or by hand:

    at> sms +5491122334455 Hola, esto es una prueba

    at> AT+CMGF=1
    OK
    at> AT+CMGS="+5491122334455"
    >
      the modem is waiting for the message.
      end with a line containing only  .   to send
      or a line containing only        !   to cancel
    sms> Hola, este es un SMS de prueba
    sms> .
    +CMGS: 23
    OK

Ctrl-Z cannot usefully be typed into a line-based prompt, so a line holding
only `.` sends and a line holding only `!` cancels, which is the convention
mail clients have used forever. Cancelling sends ESC, which is what backs the
modem out of text-entry mode; leaving it there would wedge the channel for the
next command.

The wait after Ctrl-Z is ninety seconds, because the modem answers only once
the network has accepted the message.

This is not a list of known commands. The modem reports 256 commands, 54 of
them proprietary, and nothing documents which of those want a body. So the
console does not guess: every command may answer with a prompt, and the modem
saying "> " on a line of its own is what triggers text-entry mode. The standard
cases are AT+CMGS, AT+CMGW, AT+CMGC and AT+CNMA, and any vendor command that
behaves the same way is handled without anyone having to know about it first.

The test is that the last non-empty line is exactly ">", not that the reply
ends with one, so a reply whose text happens to end that way while its OK is
still arriving is not mistaken for a prompt.

In one-shot mode (`rhodep-modem-at 'AT+CMGS="..."'`) there is no terminal to
type a body into, so it reports the prompt and sends ESC rather than leaving
the modem in text-entry mode for whoever opens the channel next.
