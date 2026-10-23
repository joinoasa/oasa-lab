"""What a hardware suite gets from the `hw` fixture: the inventory, the devices, the wiring.

Devices open on request. Their `info()` goes into the manifest as it is opened. A suite whose
hardware is not in lab.toml is skipped with the reason. A suite that transmits on real hardware
runs only after the operator names the wiring (`oasa-lab run SUITE --confirm-wiring NAME`).
"""

from __future__ import annotations

from typing import Any

from .context import RunContext, derive_seed
from .devices import Device, Inventory, SimWorld, make_device, make_hub


class WiringRefused(Exception):
    """The suite may not transmit yet; the message says why."""


class Hardware:
    def __init__(self, ctx: RunContext, inv: Inventory, nodeid: str):
        self.ctx, self.inv = ctx, inv
        sim_any = any(inv.backend_of(i) == "sim" for i in inv.devices) or inv.backend == "sim"
        self.world = SimWorld(derive_seed(ctx.seed, nodeid, "world"), inv.wiring) if sim_any else None
        hub_cfg = inv.hub
        self.hub = make_hub(hub_cfg, sim=bool(hub_cfg) and (inv.backend == "sim" or (hub_cfg or {}).get("backend") == "sim"))
        self.devices: dict[str, Device] = {}
        if self.hub is not None:
            ctx.hardware["hub"] = self.hub.info()

    # --- wiring --------------------------------------------------------------------------------
    def use_wiring(self, name: str | None = None) -> dict[str, Any]:
        """Record the cable plan in the manifest. Raises WiringRefused if a transmitting suite on real
        hardware has not been confirmed."""
        suite = self.ctx.suite
        name = name or suite.wiring
        if not name:
            raise WiringRefused("the suite names no wiring")
        w = self.inv.wiring.get(name)
        if w is None:
            raise WiringRefused(f"lab.toml has no [wiring.{name}] (see docs/hardware-suites.md for the cabling)")
        real = any(self.inv.backend_of(i) != "sim" for i in self.inv.devices)
        if suite.transmits and real and self.ctx.confirm_wiring != name:
            raise WiringRefused(
                f"this suite transmits. Connect wiring '{name}' as in docs/hardware-suites.md (at least 60 dB "
                f"between transmitters), then run again with --confirm-wiring {name}")
        paths = []
        for p in w.get("paths", []):
            dest = str(p["to"]).split(":")[0]
            kind = self.inv.devices.get(dest, {}).get("kind")
            paths.append({"name": p["name"], "from": p["from"], "to": p["to"], "loss_db": float(p["loss_db"]),
                          "receiver_transmits": bool(p.get("receiver_transmits", kind != "rtlsdr"))})
        rf = {"setup": w["rf_setup"], "wiring": name, "description": w.get("description", ""), "paths": paths}
        self.ctx.hardware["rf"] = rf
        return rf

    # --- devices ------------------------------------------------------------------------------
    def open(self, dev_id: str) -> Device:
        if dev_id in self.devices:
            return self.devices[dev_id]
        if dev_id not in self.inv.devices:
            raise KeyError(f"lab.toml has no device {dev_id!r}")
        dev = make_device(dev_id, self.inv, self.hub, self.world).open()
        self.devices[dev_id] = dev
        self.ctx.hardware["devices"][dev_id] = dev.info()
        return dev

    def first(self, kind: str) -> Device:
        ids = self.inv.ids_of(kind)
        if not ids:
            raise KeyError(f"lab.toml has no {kind} device")
        return self.open(ids[0])

    def all_of(self, kind: str) -> list[Device]:
        return [self.open(i) for i in self.inv.ids_of(kind)]

    def close(self) -> None:
        for dev in self.devices.values():
            try:
                dev.close()
            except Exception:
                pass
        if self.world is not None:
            self.world.close()
