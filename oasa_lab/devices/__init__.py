"""Lab devices. `open_device` builds the right driver and backend from lab.toml."""

from __future__ import annotations

from typing import Any

from .base import Device, DeviceError, TxRefused, Unavailable
from .hub import SimHub, UsbHub, make_hub
from .inventory import Inventory, InventoryError
from .korad import Korad
from .modem import ModemLink, SerialRadio
from .pluto import Pluto
from .powersensor import PowerSensor
from .rtlsdr import RtlSdr
from .sik import SikRadio
from .sim import SimWorld
from .tinysa import TinySA
from .usrp import B200Mini, E310

DRIVERS: dict[str, type[Device]] = {"pluto": Pluto, "rtlsdr": RtlSdr, "serial-radio": SerialRadio, "korad": Korad, "b200": B200Mini,
                                 "e310": E310, "sik": SikRadio, "power_sensor": PowerSensor, "tinysa": TinySA}


def make_device(dev_id: str, inv: Inventory, hub: UsbHub | None, world: SimWorld | None) -> Device:
    cfg: dict[str, Any] = inv.devices[dev_id]
    backend = inv.backend_of(dev_id)
    return DRIVERS[cfg["kind"]](dev_id, cfg, backend, hub, world)


__all__ = ["Device", "DeviceError", "TxRefused", "Unavailable", "Inventory", "InventoryError", "UsbHub", "SimHub", "SimWorld",
           "Pluto", "RtlSdr", "SerialRadio", "SikRadio", "Korad", "B200Mini", "E310", "PowerSensor", "TinySA", "ModemLink",
           "make_device", "make_hub"]
