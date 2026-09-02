"""rescue-dd strategy: for devices whose bootloader refuses to flash the
rootfs partition (e.g. Motorola `rhodep`, where `fastboot flash userdata`
returns permission denied and there is no fastbootd).

Flow:
  1. flash a rescue boot image to the boot slot (fastboot),
  2. reboot into it — it brings up USB-gadget networking + a root shell
     WITHOUT mounting the root filesystem,
  3. stream the rootfs GPT disk into the target partition with `dd` over that
     transport (telnet/ssh + nc),
  4. flash the distro boot image, set the active slot, reboot.

This same rescue environment is reused for recovery.
"""

from __future__ import annotations

from ...core import ui
from ...core.errors import StrategyError
from .base import PlannedOp, Strategy


class RescueDdStrategy(Strategy):
    name = "rescue-dd"
    tools = ("fastboot",)

    def _transport(self) -> str:
        return self.device.install.get("rescue", {}).get("transport", "")

    def _op_dd_partition(self, step: dict) -> list[PlannedOp]:
        part = step["partition"]
        img = self._image_path(step.get("image", ""))
        target = self._target_device_path(part)
        transport = self._transport()

        # Render the dd command as it runs INSIDE the rescue env, plus how the
        # host streams the image into it. We show both; execution uses whichever
        # transport tooling is present.
        remote_dd = f"dd of={target} bs=4M conv=fsync"

        if transport.startswith("ssh://"):
            userhost = transport[len("ssh://"):]
            cmd = ["sh", "-c", f"ssh -t {userhost} 'sudo {remote_dd}' < {img}"]
            tool = "ssh"
            desc = f"stream {step.get('image')} -> {target} over ssh"
        elif transport.startswith("telnet://"):
            hostport = transport[len("telnet://"):]
            host = hostport.split(":")[0]
            # telnet can't stream binary reliably; use netcat to a listener the
            # rescue shell opens. We model it as a host-side nc pipe.
            cmd = ["sh", "-c",
                   f"# in rescue shell: nc -l -p 5555 | {remote_dd}\n"
                   f"nc {host} 5555 < {img}"]
            tool = "nc"
            desc = f"stream {step.get('image')} -> {target} over netcat (rescue telnet {hostport})"
        else:
            raise StrategyError(
                f"rescue-dd: unknown or missing rescue transport '{transport}'"
            )

        return [PlannedOp(
            description=step.get("description", desc),
            command=cmd, destructive=True, tool=tool, partition=part, remote=True,
        )]

    def _target_device_path(self, part_name: str) -> str:
        for p in self.device.storage.get("partitions", []):
            if p.get("name") == part_name and p.get("device_path"):
                return p["device_path"]
        return f"/dev/disk/by-partlabel/{part_name}"

    def _op_wait_transport(self, step: dict) -> list[PlannedOp]:
        transport = self._transport()
        return [PlannedOp(
            description=f"wait for rescue transport {transport} (boot into rescue image)",
            command=None,
        )]
