"""What a hardware suite gets from the `hw` fixture: the inventory, the devices, the wiring.

Devices open on request. Their `info()` goes into the manifest as it is opened. A suite whose
hardware is not in lab.toml is skipped with the reason. A suite that transmits on real hardware
runs only after the operator names the wiring (`oasa-lab run SUITE --confirm-wiring NAME`).
"""

from __future__ import annotations

import json
import os
from typing import Any

from .context import RunContext, derive_seed
from .devices import Device, Inventory, SimWorld, make_device, make_hub


class WiringRefused(Exception):
    """The suite may not transmit yet; the message says why."""


class UndeclaredTransmit(WiringRefused):
    """A suite that needs hardware must say, in suite.toml, whether it transmits."""


# Kinds whose TX gain the lab can cap; only these may radiate.
RADIATING_KINDS = ("b200", "e310")


def antenna_caps_from_env() -> dict[str, float]:
    """The owner's TX caps into antennas: job-run passes them from the root-owned policy. Bad input means none."""
    try:
        raw = json.loads(os.environ.get("OASA_LAB_ANTENNA_CAPS", "") or "{}")
        return {str(k): float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except (ValueError, TypeError):
        return {}


class Hardware:
    def __init__(self, ctx: RunContext, inv: Inventory, nodeid: str, antenna_caps: dict[str, float] | None = None):
        self.ctx, self.inv = ctx, inv
        self.antenna_caps = antenna_caps_from_env() if antenna_caps is None else dict(antenna_caps)
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
        on_air = w["rf_setup"] == "antenna"
        if on_air and not suite.radiates:
            raise WiringRefused(f"wiring '{name}' is on antennas, and suite {suite.name!r} does not say radiates = true")
        if suite.radiates and not on_air:
            raise WiringRefused(f"suite {suite.name!r} radiates, and wiring '{name}' is not rf_setup = \"antenna\"")
        if suite.transmits and real and self.ctx.confirm_wiring != name:
            raise WiringRefused(
                f"this suite transmits. Connect wiring '{name}' as in docs/hardware-suites.md (at least 60 dB "
                f"between transmitters), then run again with --confirm-wiring {name}")
        if suite.radiates and real and not self.antenna_caps:
            raise WiringRefused("this suite radiates and no antenna TX caps are set (the owner's [antenna] "
                                "max_tx_gain_db in the lab policy; job-run passes them)")
        paths = []
        for p in w.get("paths", []):
            dest = str(p["to"]).split(":")[0]
            kind = self.inv.devices.get(dest, {}).get("kind")
            paths.append({"name": p["name"], "from": p["from"], "to": p["to"], "loss_db": float(p["loss_db"]),
                          "receiver_transmits": bool(p.get("receiver_transmits", kind != "rtlsdr"))})
        rf = {"setup": w["rf_setup"], "wiring": name, "description": w.get("description", ""), "paths": paths}
        if on_air:
            rf["antennas"] = [str(a) for a in w.get("antennas", [])]
            rf["antenna_caps"] = dict(sorted(self.antenna_caps.items()))
        self.ctx.hardware["rf"] = rf
        return rf

    # --- the transmit gate -----------------------------------------------------------------------
    def tx_allowed(self, dev_id: str | None = None) -> str | None:
        """None when this run may transmit (from device `dev_id`); else why not. Every device's transmit call asks
        this. A radiating suite on real hardware may transmit only from a device the owner capped."""
        suite, rf = self.ctx.suite, self.ctx.hardware.get("rf")
        if not suite.transmits:
            return f"suite {suite.name!r} does not declare transmits = true"
        if not rf:
            return "no wiring is on record; call hw.use_wiring() first"
        real = any(self.inv.backend_of(i) != "sim" for i in self.inv.devices)
        if real and self.ctx.confirm_wiring != rf.get("wiring"):
            return f"wiring {rf.get('wiring')!r} was not confirmed with --confirm-wiring"
        if suite.radiates and real:
            kind = self.inv.devices.get(dev_id or "", {}).get("kind")
            if dev_id not in self.antenna_caps:
                return f"device {dev_id!r} has no antenna TX cap in the lab policy"
            if kind not in RADIATING_KINDS:
                return f"device {dev_id!r} ({kind}) cannot radiate here: only {', '.join(RADIATING_KINDS)} have a TX gain cap"
        return None

    # --- devices ------------------------------------------------------------------------------
    def open(self, dev_id: str) -> Device:
        if dev_id in self.devices:
            return self.devices[dev_id]
        if dev_id not in self.inv.devices:
            raise KeyError(f"lab.toml has no device {dev_id!r}")
        suite = self.ctx.suite
        if self.inv.backend_of(dev_id) != "sim" and suite.requires and not suite.transmits_declared:
            raise UndeclaredTransmit(f"suite {suite.name!r} needs hardware but suite.toml does not say transmits = true "
                                     "or transmits = false. Say which, so the lab knows whether it may radiate")
        dev = make_device(dev_id, self.inv, self.hub, self.world)
        dev.tx_gate = lambda: self.tx_allowed(dev_id)
        if suite.radiates and dev_id in self.antenna_caps and hasattr(dev, "max_tx_gain"):
            # into an antenna the owner's cap applies when it is lower than the device's own limit (never raises it)
            dev.cfg["max_tx_gain"] = min(float(dev.max_tx_gain), self.antenna_caps[dev_id])
        dev = dev.open()
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
