"""USB hub with per-port power switching (YKUSH3 or any uhubctl hub).

Real backends run a tool as a separate process, so nothing is linked in:
  ykushcmd (Yepkit, Apache-2.0)  or  uhubctl (GPL-2.0, test use only).
The command lines are in `COMMANDS`; check them against the tool's own help when the hub arrives.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from typing import Any

from .base import DeviceError, Unavailable

# {bin} {serial} {port} {location} are filled in from the [hub] table of lab.toml.
COMMANDS: dict[str, dict[str, list[str]]] = {
    "ykush3": {
        "bin": ["ykushcmd"],
        "on": ["{bin}", "ykush3", "-s", "{serial}", "-u", "{port}"],
        "off": ["{bin}", "ykush3", "-s", "{serial}", "-d", "{port}"],
        "get": ["{bin}", "ykush3", "-s", "{serial}", "-g", "{port}"],
    },
    "uhubctl": {
        "bin": ["uhubctl"],
        "on": ["{bin}", "-l", "{location}", "-p", "{port}", "-a", "on"],
        "off": ["{bin}", "-l", "{location}", "-p", "{port}", "-a", "off"],
        "get": ["{bin}", "-l", "{location}", "-p", "{port}"],
    },
}


class UsbHub:
    """Switch the power of hub ports. `backend` is "sim" or "hardware"."""

    backend = "hardware"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = dict(cfg)
        self.kind = str(cfg.get("kind", "ykush3"))
        if self.kind not in COMMANDS:
            raise DeviceError(f"hub kind must be one of {list(COMMANDS)}")
        self._lock = threading.Lock()

    def _run(self, action: str, port: int) -> str:
        spec = COMMANDS[self.kind]
        exe = shutil.which(self.cfg.get("bin", spec["bin"][0]))
        if not exe:
            raise Unavailable(f"{spec['bin'][0]} is not installed (needed for the {self.kind} hub)")
        fill = {"bin": exe, "serial": str(self.cfg.get("serial", "")), "port": str(port),
                "location": str(self.cfg.get("location", ""))}
        cmd = [a.format(**fill) for a in spec[action]]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise DeviceError(f"{self.kind} {action} port {port} failed: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout

    def info(self) -> dict[str, Any]:
        return {"id": "hub", "kind": "hub", "backend": self.backend, "model": self.kind,
                "serial": str(self.cfg.get("serial") or self.cfg.get("location") or "n/a"), "firmware": "n/a"}

    def is_on(self, port: int) -> bool:
        out = self._run("get", port).lower()
        return re.search(r"\boff\b|\bdown\b", out) is None

    def set_power(self, port: int, on: bool) -> None:
        with self._lock:
            self._run("on" if on else "off", port)

    def power_cycle(self, port: int, off_s: float = 2.0) -> None:
        self.set_power(port, False)
        time.sleep(off_s)
        self.set_power(port, True)


class SimHub(UsbHub):
    """Remembers port states. Devices on a port that is off refuse to open."""

    backend = "sim"

    def __init__(self, cfg: dict[str, Any]):
        super().__init__({"kind": "ykush3", **cfg})
        self.state: dict[int, bool] = {}
        self.cycles: list[int] = []

    def is_on(self, port: int) -> bool:
        return self.state.get(port, True)

    def set_power(self, port: int, on: bool) -> None:
        with self._lock:
            self.state[port] = on

    def power_cycle(self, port: int, off_s: float = 2.0) -> None:
        self.set_power(port, False)
        time.sleep(min(off_s, 0.05))  # the simulation does not wait
        self.set_power(port, True)
        self.cycles.append(port)


def make_hub(cfg: dict[str, Any] | None, sim: bool) -> UsbHub | None:
    if not cfg:
        return None
    return SimHub(cfg) if sim or cfg.get("backend") == "sim" else UsbHub(cfg)
