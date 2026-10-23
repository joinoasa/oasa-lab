"""Lab devices. `open_device` builds the right driver and backend from lab.toml."""

from __future__ import annotations

from typing import Any

from .base import Device, DeviceError, Unavailable
from .hub import SimHub, UsbHub, make_hub
from .inventory import Inventory, InventoryError
from .modem import ModemLink, SerialRadio
from .pluto import Pluto
from .rtlsdr import RtlSdr
from .sim import SimWorld

DRIVERS: dict[str, type[Device]] = {"pluto": Pluto, "rtlsdr": RtlSdr, "serial-radio": SerialRadio}


def make_device(dev_id: str, inv: Inventory, hub: UsbHub | None, world: SimWorld | None) -> Device:
    cfg: dict[str, Any] = inv.devices[dev_id]
    backend = inv.backend_of(dev_id)
    return DRIVERS[cfg["kind"]](dev_id, cfg, backend, hub, world)


__all__ = ["Device", "DeviceError", "Unavailable", "Inventory", "InventoryError", "UsbHub", "SimHub", "SimWorld",
           "Pluto", "RtlSdr", "SerialRadio", "ModemLink", "make_device", "make_hub"]
