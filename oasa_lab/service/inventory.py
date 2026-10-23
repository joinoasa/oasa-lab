"""Which lab devices are present. Pluggable: a simulated backend for tests and a Linux one for the lab PC.

The Linux backend only looks (sysfs, one TCP connect). It never opens a device, sends a command
or touches the SDR control ports' data. Radios are driven by the tests, not by the service.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .config import Config, Device


@dataclass
class DeviceStatus:
    id: str
    kind: str
    present: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Inventory(Protocol):
    async def scan(self) -> list[DeviceStatus]: ...


class SimulatedInventory:
    """Reports what the config says. `present = false` on a device simulates a missing radio."""

    def __init__(self, devices: list[Device], overrides: dict[str, bool] | None = None):
        self.devices = devices
        self.overrides = overrides if overrides is not None else {}

    async def scan(self) -> list[DeviceStatus]:
        out = []
        for d in self.devices:
            present = self.overrides.get(d.id, d.simulated_present)
            out.append(DeviceStatus(d.id, d.kind, present, "simulated"))
        return out


class UsbSysfsInventory:
    """USB devices from /sys/bus/usb/devices; network devices by a TCP connect to their port."""

    def __init__(self, devices: list[Device], sysfs: Path = Path("/sys/bus/usb/devices")):
        self.devices = devices
        self.sysfs = sysfs

    def _usb_present(self) -> list[tuple[str, str]]:
        found = []
        if self.sysfs.is_dir():
            for d in sorted(self.sysfs.iterdir()):
                try:
                    vid = (d / "idVendor").read_text().strip().lower()
                    pid = (d / "idProduct").read_text().strip().lower()
                except OSError:
                    continue
                try:
                    serial = (d / "serial").read_text().strip()
                except OSError:
                    serial = ""
                found.append((f"{vid}:{pid}", serial))
        return found

    @staticmethod
    def _tcp_ok(addr: str) -> bool:
        host, _, port = addr.rpartition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=1.0):
                return True
        except (OSError, ValueError):
            return False

    async def scan(self) -> list[DeviceStatus]:
        usb = await asyncio.to_thread(self._usb_present)
        taken: set[int] = set()
        out = []
        for d in self.devices:
            if d.usb:
                # Two boards can share one vendor:product, so each config entry claims its own match.
                idx = next((i for i, (vp, ser) in enumerate(usb)
                            if i not in taken and vp == d.usb and (not d.serial or ser == d.serial)), None)
                if idx is not None:
                    taken.add(idx)
                out.append(DeviceStatus(d.id, d.kind, idx is not None, f"usb {d.usb}"))
            elif d.tcp:
                ok = await asyncio.to_thread(self._tcp_ok, d.tcp)
                out.append(DeviceStatus(d.id, d.kind, ok, f"tcp {d.tcp}"))
            else:
                out.append(DeviceStatus(d.id, d.kind, False, "no usb or tcp address in the config"))
        return out


def make_inventory(cfg: Config) -> Inventory:
    return SimulatedInventory(cfg.devices) if cfg.inventory_backend == "simulated" else UsbSysfsInventory(cfg.devices)
