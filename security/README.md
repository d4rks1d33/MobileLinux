# Security tools layer

These are **optional, distro-level** security tools, deliberately kept separate
from device (hardware) support. Nothing here is required to boot, place a call,
or bring up the radio — that is all in `devices/<vendor>/<codename>/`.

A build can pull this layer in with a profile:

```bash
mobilelinux build rhodep --distro kali --profile security
```

Each tool ships a `layer.yaml` manifest (what it is, how it installs, its
dependencies) plus its own idempotent `install.sh`, migrated verbatim from the
original `nethunter-rhodep-repo/extra-tools/`.

| Tool | What it is |
|------|------------|
| `nethunter-pro` | GTK4/libadwaita NetHunter Pro control panel (53 modules, IoT engine, phishkin3, BLE spam, Bjorn adapter). |
| `pwnagotchi` | WPA-handshake capture (pinned to external USB WiFi `wlan1`). |
| `modem-at` | AT console to the modem over glink (radio diagnostics). |
| `terminal-keyboard` | Esc/Tab/Ctrl/Alt/arrows for the on-screen keyboard. |
| `terminal-clipboard` | Copy/paste in the terminal as zsh widgets. |
| `claude-free` | Claude Code over any opencode-configured model provider. |
| `cleanup` | Weekly disk cleanup + journal cap. |

Portability note: these are written for Kali/Phosh/Plasma but are **not** tied
to a specific device. The `wlan0`/`wlan1` pinning in `pwnagotchi` reflects the
generic "internal WiFi can't do monitor mode, use a USB adapter" pattern, not
rhodep specifically.
