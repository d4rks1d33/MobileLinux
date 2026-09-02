# NetHunter Pro App (Phosh)

A native **GTK4 / libadwaita** control panel for **Kali NetHunter Pro** running
on a mainline Linux kernel with Phosh, built for the Motorola Moto G82 5G
(rhodep) port but not tied to it.

It is a **reimplementation**, not a port, of the Android
[kali-nethunter-app](https://github.com/kimocoder/kali-nethunter-app). That app
is Java/Android and most of it exists to manage the Kali *chroot* inside
Android. On NetHunter Pro the tools are native and there is no chroot, so the
UI is rewritten in GTK and only the useful logic — which command each screen
runs — is carried over. Privilege comes from `pkexec` (the Phosh polkit
prompt), not a persistent root shell.

## Why a reimplementation

The original is ~29 Android `Fragment`s over Activities, XML layouts and
Android APIs that do not exist on Phosh. What is reusable is the shell logic:
`new ShellExecuter().RunAsRootOutput("setprop ...")` becomes an async `pkexec`
call here. The Android-only, chroot-heavy screens (ChrootManager, the
Bluetooth/VNC/Wifipumpkin service wrappers) are dropped; native systemd
services replace them.

## Screens

| Screen | What it does | Source it maps to |
|---|---|---|
| Kali Services | start/stop ssh, postgresql, bettercap, ... via systemd | KaliServicesFragment |
| MAC Changer | randomise or reset an interface MAC | MacchangerFragment |
| Nmap | build a scan from options and run it | NmapFragment |
| USB and Radio | OTG charge/host switch, monitor mode (rhodep) | new, port-specific |
| Pwnagotchi | start/stop pwnagotchi on wlan1, pick manual/auto, open the web UI, download wpa-sec cracked keys and export a wordlist | new, port-specific |
| Wifipumpkin3 | rogue AP, captive-portal template picker, generate a portal from a login-page git repo | WifipumpkinFragment (extended) |
| Phishkin3 (evilginx) | wp3 + evilginx2 attack: phishlet, homoglyph domain, interface. Ships 95 community phishlets. Orchestrator in `helper/rhodep-phishkin3-launch` | new |
| Docker | engine on/off (`docker.service` + `.socket` together), downloaded images with per-image Run/Stop, Docker Hub search, wipe-everything | new |
| CARsenal | CAN bus tools (can-utils), symbolic car icon | CARsenalFragment |
| HID / BadUSB | type keystrokes into a connected host | HidFragment |
| Run Command | one-off shell command, optionally as root | TerminalFragment |

A module greys itself out and says which tool is missing if the command it
needs is not on `PATH`, so screens degrade cleanly rather than failing at
click time.

## Wifipumpkin3 screen notes

The rogue-AP screen builds a wifipumpkin3 startup script from the chosen options
and opens it in a terminal. What each field maps to:

  * **AP interface** — `set interface` (the adapter that becomes the AP, e.g.
    `wlan1`, the external dongle).
  * **Internet interface** — `set interface_net` (wifipumpkin3's `-iNet`): the
    adapter with a working connection, shared to the AP's clients, so they get
    real internet through the phone. `wlan0` by default (the internal Wi-Fi).
    Leave empty for a captive portal that does not pass traffic on.
  * **Proxy** — `captiveflask` is the captive portal; `pumpkinproxy` is the
    transparent MITM proxy; `sniffkin3` captures credentials. (Not
    `pumpkinproxy` for the portal — that was a bug once.)
  * **Captive-portal template** — enabled with `set captiveflask.<Template> true`,
    which is what writes e.g. `DarkLogin=true` into `captive-portal.ini`. This is
    not the `templates.custom` install command, which installs *new* templates
    rather than selecting one.

**Captive-portal templates live in** `/usr/share/wifipumpkin3/config/templates`
(also mirrored under the dist-packages path). Drop a new template folder in
there and press **Refresh templates** to pick it up. The stock ones are
`DarkLogin`, `FlaskDemo`, `Login_v4`, `evilqr3` and `loginPage`.

**Build a portal from a login-page repo.** The screen has a "Build a portal from
a login page repo" box: paste a git URL of a static login page (e.g.
`github.com/trananhtuat/instagram-login`) and it produces a wifipumpkin3 captive
portal from it. `helper/rhodep-make-captiveportal` does the work: clone the repo,
fold its `index.html` into `templates/login.html`, move css/js/images under
`static/` and rewrite their paths to `url_for('static', ...)`, write the plugin
`.py`, install both into place (the captiveflask plugins dir and the templates
dir, directly -- not by starting wifipumpkin3, which would reconfigure the
network), and delete the clone so the source does not sit around.

The form rewrite is best-effort and honest about it. It forces the first `<form>`
to `method="POST"`, renames the username-ish field to `name="login"` and the
password field to `name="password"` (the two fields captiveflask captures),
handling quoted and unquoted attributes. If the page has no `<form>` -- it
submits through JavaScript, like the Instagram example above -- it says so
plainly, because a capture that silently captures nothing is worse than a
warning. Pick a repo with a real HTML login form for capture to work.

Note the NAT/forwarding wifipumpkin3 sets up needs iptables working in the
kernel; on a kernel without the `ip_tables` module, sharing internet fails with
"table does not exist".

## Phishkin3 (evilginx) screen notes

Automates the wifipumpkin3 + evilginx2 attack described at
https://docs.wifipumpkin3.com/blog/tutorials/phishkin3. Three inputs on the
screen: **Phishlet**, **Look-alike domain**, **AP interface** (plus SSID,
Internet interface). The orchestrator is `helper/rhodep-phishkin3-launch`.

### The domain is a look-alike, not the real one

HSTS preload refuses a DNS spoof of `instagram.com` before the browser will
even talk. The domain has to be different from the target's. The screen
suggests two kinds of look-alike, and the launcher accepts either:

  * **Plain**, like `instagram-login.com` -- obvious to a reader, but the
    address bar shows exactly what was typed.
  * **Homoglyph**, like `instagrаm.com` with a Cyrillic а (U+0430). Reads as
    the original at a glance, but is a different domain to the browser. The
    launcher converts these to punycode (`xn--instgram-46g.com`) since that
    is what evilginx, the DNS spoof and `/etc/hosts` all use downstream.

Homoglyph caveat: modern browsers show the punycode in the address bar as an
anti-spoofing measure, so on an up-to-date Android the reader may see
`xn--instgram-...` rather than the Cyrillic form. Still a different domain,
still evades HSTS, but not perfectly invisible.

### 95 phishlets pre-installed

Under `/usr/share/evilginx2/phishlets/`, from three community repos:
  * https://github.com/Whispergate/ose_evilginx_phishlets
  * https://github.com/jeanlucndato/Evilginx2-Phishlets
  * https://github.com/hash3liZer/phishlets

Populars covered: `instagram`, `facebook`, `google`, `github`, `linkedin`,
`microsoft`, `netflix`, `amazon`, `paypal`. They advertise `min_ver: 2.x` but
evilginx v3 loads them anyway. A **fixed Instagram phishlet** ships in the
repo under `phishlets/instagram.yaml` -- the stock one's `sub_filters` are a
no-op, see below.

### The bugs that had to be worked around, and where they live in the code

Each is worth reading before touching this because they are traps the next
attacker will hit:

1. **`data.db` residue**. evilginx v3 stores phishlets and lures in a BuntDB
   (not a config file), so reusing the config dir accumulates old lures.
   `get-url 0` then returns the wrong lure. Fix: `run_evilginx_setup` clears
   `data.db`, `config.json` and `blacklist.txt` each run, keeping `crt/` so
   the developer CA is stable.

2. **DNS spoof wildcards do not match**. `add *.<domain>` writes a wildcard
   into the zone file, but wp3's pydns does not match it against a real query
   for `www.<domain>` -- the log shows `no local zone found, proxying
   www.<domain>` and the browser gets `ERR_NAME_NOT_RESOLVED`. Fix: the pulp
   lists every landing hostname explicitly (`add www.<domain>`, `add
   m.<domain>`, ...), read from the phishlet's `proxy_hosts`.

3. **The phishkin3 gate blocks 443**. It opens only DNS and port 8080 to
   the AP; the browser can reach the portal but not the lure that `/login`
   302s to on `https://<domain>/`. Fix: after phishkin3 comes up, the launcher
   does `iptables -I FORWARD 1 -i <iface> -p tcp --dport 443 -d 172.16.0.1
   -j ACCEPT`.

4. **evilginx's blacklist locks out the AP IP**. Blacklist runs `unauth` by
   default: a probe or curl to the phishlet domain without a valid lure adds
   the source IP, and after that every request from that IP gets the
   `unauth_url` (a Rickroll). All victim traffic transits through the AP, so
   its IP is what ends up blacklisted, and every real visitor is Rickrolled.
   Fix: the launcher sends `blacklist off` on startup.

5. **The stock Instagram phishlet's `sub_filters` are a no-op**. `search` and
   `replace` are both `https://{hostname}/`, so the served HTML keeps
   hardcoded `https://www.instagram.com/` links and the browser fetches
   static assets from the real Instagram through the DNS spoof, giving the
   "logo only" symptom. The fixed phishlet in `phishlets/instagram.yaml`
   rewrites `www.instagram.com` -> `www.{domain}` and `m.instagram.com` ->
   `m.{domain}` in HTML and JS.

6. **NetworkManager fights hostapd for `wlan1`**. NM keeps issuing
   disconnects because from its point of view the interface has no
   connection; the SSID flashes on and off, the victim cannot associate.
   `dmesg` shows repeated `entered promiscuous mode` / `left promiscuous
   mode`. Fix: `nmcli device set <iface> managed no` before Launch;
   `managed yes` on Stop.

7. **wp3's `-iNM` is a standalone action**, not a flag that composes with
   `-p`. Passing `-iNM wlan1 -p attack.pulp` makes wp3 ignore the interface
   and exit before touching the pulp -- log shows `The interface wlan1 has
   been ignored successfully` and nothing else, tmux dies with `no server
   running`. Fix: `-iNM` is not in the wp3 invocation; nmcli does the same
   job without killing wp3.

8. **`pkill -f phishkin3` would kill the launcher**. The launcher is called
   `nethunter-pro-phishkin3-launch`, and `-f` matches full argv. Fix: the
   reset uses narrow patterns -- `pgrep -f 'wifipumpkin3 -p'`, `pgrep -f
   'plugins/bin/phishkin3'`, `pkill -x` for `evilginx2`, `hostapd`,
   `dnsmasq`. The `pgrep` variants also filter the launcher's own pid.

9. **Half-cleaned state stops phishkin3 from starting**. Duplicate FORWARD
   ACCEPT rules from several Launches, an already-open tmux session, or a
   half-alive process makes wp3 bail during phishkin3 setup: hostapd runs,
   DHCP answers, but there is no captive portal and no NAT. Fix:
   `full_reset` runs at the start of every Launch regardless of whether
   Stop was pressed.

10. **The developer-mode certificate is called "Super-Evil Root CA"**.
    Android never trusts it. In a lab the CA can be installed on the
    victim (Settings → Security → Install certificate) to get browser
    traffic working, but system apps ignore user-installed CAs since
    Android 7 -- Google's mitigation for exactly this attack. For real
    use the path is a look-alike domain with a Let's Encrypt cert via
    `autocert` (without `-developer`), which the launcher does not do
    today. `docs/phishkin3-cert-notes.md` is the place to write this up
    when it is built.

### Building a captive portal from a login-page git repo

`helper/rhodep-make-captiveportal` (invoked from the wifipumpkin3 screen)
clones a repo, folds `index.html` into `templates/login.html`, moves assets
under `static/` with paths rewritten to `url_for('static', ...)`, forces the
first `<form>` to `method="POST"` and renames the username-ish field to
`name="login"` and the password field to `name="password"` (the two fields
captiveflask captures), writes the plugin `.py`, installs both pieces into
place directly (writing where `install` puts them, without starting
wifipumpkin3 which would reconfigure the network and drop the ssh session
running the install), and deletes the clone.

The rewrite is best-effort and honest about it: a page that submits via
JavaScript rather than a POST form is reported as uncapturable rather than
silently producing a portal that catches nothing. Tested with a real HTML
form (rewritten to POST + login/password fields, quoted or unquoted) and
with the Instagram repo which uses JS submit (flagged correctly).

## Architecture

- `executor.py` — async command runner. Everything runs off the GTK thread and
  delivers results back on it; `root=True` wraps in `pkexec`.
- `module.py` — `NHModule` base class and a registry. Each screen is one
  subclass that declares a title, an icon and its required tools.
- `widgets.py` — shared pieces (`CommandButton`, `OutputView`).
- `window.py` — adaptive `NavigationSplitView` that collapses to one pane on a
  phone-width screen, so the same UI works on the desktop and in the hand.
- `modules/` — one file per screen. Adding a screen is one `@register` class.

## Install

```
pip install --break-system-packages .
install -Dm644 data/org.kali.NetHunterPro.desktop \
    /usr/share/applications/org.kali.NetHunterPro.desktop
install -Dm644 data/org.kali.NetHunterPro.svg \
    /usr/share/icons/hicolor/scalable/apps/org.kali.NetHunterPro.svg
install -Dm644 data/org.kali.NetHunterPro.policy \
    /usr/share/polkit-1/actions/org.kali.NetHunterPro.policy
```

Then it appears in the Phosh app drawer as **NetHunter Pro**, or run
`nethunter-pro` from a terminal.

## Requirements

GTK4 and libadwaita (`gir1.2-gtk-4.0`, `gir1.2-adw-1`), Python 3.10+, and
whatever tool a given screen drives (nmap, macchanger, aircrack-ng, ...).

## License

GPL-2.0-or-later, matching the app it reimplements.
