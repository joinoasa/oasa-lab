"""lab.toml: which device is which (serial numbers, USB ports) and how the cables run.

Where it is read from: `$OASA_LAB_INVENTORY`, else `./lab.toml`. The real file is not committed;
`lab.example.toml` is. `OASA_LAB_SIM=1` runs every device on its simulated backend, with a built-in
inventory when there is no file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

KINDS = ("pluto", "rtlsdr", "serial-radio", "sik", "korad", "b200", "e310", "power_sensor", "tinysa", "hub")
BACKENDS = ("sim", "hardware")
RF_SETUPS = ("cabled", "shielded", "antenna")   # "antenna": radios on antennas, for suites that radiate

# The settings both simulated SiK radios start with and are expected to report (lab.toml [devices.ID.sik]).
SIM_SIK = {"netid": 4242, "air_speed": 64, "ecc": 0, "mavlink": 0, "serial_speed": 57, "txpower": 20,
           "min_freq": 915000, "max_freq": 928000, "num_channels": 50, "max_window": 131}

# What `OASA_LAB_SIM=1` uses when there is no lab.toml. Same shape as lab.example.toml.
DEFAULT_SIM: dict[str, Any] = {
    "lab": {"name": "simulated bench", "backend": "sim"},
    "hub": {"kind": "ykush3", "serial": "SIM-YKUSH3"},
    "devices": {
        "pluto0": {"kind": "pluto", "uri": "ip:192.168.2.1", "serial": "SIM-PLUTO-0001", "hub_port": 1,
                   "channels": 2},
        "rtl0": {"kind": "rtlsdr", "serial": "SIM-RTL-0001", "hub_port": 2, "known_ppm": -0.4},
        "heltec_a": {"kind": "serial-radio", "model": "Heltec WiFi LoRa 32 V3", "port": "sim:a",
                     "serial": "SIM-HELTEC-A", "hub_port": 3, "firmware": "sim-modem 0.1",
                     "radio": {"profile": "lora-sf7-bw125", "freq_mhz": 915.0, "tx_power_dbm": 10}},
        "heltec_b": {"kind": "serial-radio", "model": "Heltec WiFi LoRa 32 V3", "port": "sim:b",
                     "serial": "SIM-HELTEC-B", "hub_port": 4, "firmware": "sim-modem 0.1",
                     "radio": {"profile": "lora-sf7-bw125", "freq_mhz": 915.0, "tx_power_dbm": 10}},
        "sik_a": {"kind": "sik", "model": "Holybro SiK Telemetry Radio V3 915 MHz", "port": "sim:sik-a",
                  "serial": "SIM-SIK-A", "baud": 57600, "mtu": 242, "radio": {"profile": "sik-64k"},
                  "sik": SIM_SIK},
        "sik_b": {"kind": "sik", "model": "Holybro SiK Telemetry Radio V3 915 MHz", "port": "sim:sik-b",
                  "serial": "SIM-SIK-B", "baud": 57600, "mtu": 242, "radio": {"profile": "sik-64k"},
                  "sik": SIM_SIK},
        "korad0": {"kind": "korad", "port": "sim:korad", "serial": "SIM-KORAD-0001", "hold_output": True,
                   "sim": {"on": True, "vset": 12.0, "iset": 3.0}},
        "psens0": {"kind": "power_sensor", "model": "keysight-u2004a", "transport": "usbtmc", "port": "sim:psens",
                   "serial": "SIM-PSENS-0001", "sim": {"level_dbm": -30.0, "noise_db": 0.02}},
        "tinysa0": {"kind": "tinysa", "model": "tinySA Ultra+", "port": "sim:tinysa", "serial": "SIM-TINYSA-0001",
                    "input_pad_db": 30.0, "sim": {"tone_hz": 915e6, "tone_dbm": -40.0}},
        "b2000": {"kind": "b200", "serial": "SIM-B200-0001"},
        "e3100": {"kind": "e310", "addr": "sim", "serial": "SIM-E310-0001", "channel": 1, "subdev": "A:0 A:1",
                  "sim": {"xo_ppm": 2.4}},
    },
    "wiring": {
        "pluto": {
            "rf_setup": "cabled",
            "description": "Pluto+ TX1 to RX1 and TX2 to RX2 through 2 x 30 dB; TX1 also to the RTL-SDR",
            "paths": [
                {"name": "tx1-rx1", "from": "pluto0:tx1", "to": "pluto0:rx1", "loss_db": 63.5},
                {"name": "tx2-rx2", "from": "pluto0:tx2", "to": "pluto0:rx2", "loss_db": 60.0},
                {"name": "tx1-rtl", "from": "pluto0:tx1", "to": "rtl0", "loss_db": 63.5, "receiver_transmits": False},
            ],
        },
        "usrp": {
            "rf_setup": "cabled",
            "description": "B200mini TX/RX to E310 RX2-B and E310 TRX-A to B200mini RX2, each through 30 + 20 + 10 dB",
            "paths": [
                {"name": "b200-e310", "from": "b2000:tx/rx", "to": "e3100:rx2-b", "loss_db": 60.0},
                {"name": "e310-b200", "from": "e3100:trx-a", "to": "b2000:rx2", "loss_db": 60.0},
            ],
        },
        "sik": {
            "rf_setup": "cabled",
            "description": "SiK A -30 dB- -30 dB- SiK B (two fixed attenuators in line)",
            "paths": [
                {"name": "a-to-b", "from": "sik_a", "to": "sik_b", "loss_db": 60.0},
                {"name": "b-to-a", "from": "sik_b", "to": "sik_a", "loss_db": 60.0},
            ],
        },
        "lora": {
            "rf_setup": "cabled",
            "description": "Heltec A and B through 30 dB each and a 4-way splitter; RTL-SDR on a third leg through 30 dB",
            "paths": [
                {"name": "a-to-b", "from": "heltec_a", "to": "heltec_b", "loss_db": 72.0},
                {"name": "b-to-a", "from": "heltec_b", "to": "heltec_a", "loss_db": 72.0},
                {"name": "a-to-rtl", "from": "heltec_a", "to": "rtl0", "loss_db": 66.0, "receiver_transmits": False},
                {"name": "b-to-rtl", "from": "heltec_b", "to": "rtl0", "loss_db": 66.0, "receiver_transmits": False},
            ],
        },
    },
}


class InventoryError(Exception):
    pass


def _calibration_cfg(cal: Any, source: str, base_dir: str | Path | None) -> dict[str, Any]:
    """[calibration] path = "..." (relative to lab.toml), recabled = "YYYY-MM-DD" (optional). Absent: {}."""
    if cal is None:
        return {}
    if not isinstance(cal, dict):
        raise InventoryError(f"{source}: [calibration] must be a table")
    unknown = set(cal) - {"path", "recabled"}
    if unknown:
        raise InventoryError(f"{source}: [calibration] has unknown keys {sorted(unknown)} (known: path, recabled)")
    path = cal.get("path")
    if not (isinstance(path, str) and path):
        raise InventoryError(f"{source}: [calibration] path must be a file name")
    full = Path(path)
    if not full.is_absolute() and base_dir is not None:
        full = Path(base_dir) / full
    out: dict[str, Any] = {"path": str(full)}
    if "recabled" in cal:
        try:
            out["recabled"] = date.fromisoformat(str(cal["recabled"])).isoformat()
        except ValueError:
            raise InventoryError(f"{source}: [calibration] recabled must be YYYY-MM-DD") from None
    return out


@dataclass
class Inventory:
    name: str = "lab"
    backend: str = "hardware"
    hub: dict[str, Any] | None = None
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    wiring: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: str = "built-in"
    sdr_live: dict[str, Any] = field(default_factory=dict)   # [sdr_live]: the live link's helper and settings
    calibration: dict[str, Any] = field(default_factory=dict)  # [calibration]: path (resolved), recabled date

    def backend_of(self, dev_id: str) -> str:
        return str(self.devices[dev_id].get("backend", self.backend))

    def ids_of(self, kind: str) -> list[str]:
        return [i for i, d in self.devices.items() if d["kind"] == kind]

    def has(self, requirement: str) -> bool:
        """`pluto`, `rtlsdr`, `hub`, or `serial-radio:2` (at least 2)."""
        kind, _, n = requirement.partition(":")
        need = int(n) if n else 1
        have = len(self.ids_of(kind))
        if kind == "hub":
            have = 1 if self.hub else 0
        return have >= need

    def missing(self, requires: list[str]) -> list[str]:
        return [r for r in requires if not self.has(r)]

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str, force_sim: bool = False,
                  base_dir: str | Path | None = None) -> "Inventory":
        lab = data.get("lab", {})
        backend = "sim" if force_sim else str(lab.get("backend", "hardware"))
        if backend not in BACKENDS:
            raise InventoryError(f"{source}: [lab] backend must be one of {BACKENDS}")
        devices = {}
        for dev_id, cfg in (data.get("devices") or {}).items():
            if not isinstance(cfg, dict) or cfg.get("kind") not in KINDS or cfg.get("kind") == "hub":
                raise InventoryError(f"{source}: devices.{dev_id}.kind must be one of {KINDS[:-1]}")
            cfg = dict(cfg)
            if force_sim:
                cfg["backend"] = "sim"
            devices[dev_id] = cfg
        wiring = data.get("wiring") or {}
        for wname, w in wiring.items():
            if w.get("rf_setup") not in RF_SETUPS:
                raise InventoryError(f"{source}: wiring.{wname}.rf_setup must be one of {RF_SETUPS}")
            for p in w.get("paths", []):
                if not all(k in p for k in ("name", "from", "to", "loss_db")):
                    raise InventoryError(f"{source}: wiring.{wname} paths need name, from, to, loss_db")
            ants = w.get("antennas", [])
            if not (isinstance(ants, list) and all(isinstance(a, str) and a for a in ants)):
                raise InventoryError(f"{source}: wiring.{wname}.antennas must be a list of DEVICE:PORT names")
            if w["rf_setup"] == "antenna" and not ants:
                raise InventoryError(f"{source}: wiring.{wname} is rf_setup = \"antenna\" but lists no antennas")
        sdr_live = data.get("sdr_live") or {}
        if not isinstance(sdr_live, dict):
            raise InventoryError(f"{source}: [sdr_live] must be a table")
        calibration = _calibration_cfg(data.get("calibration"), source, base_dir)
        hub = data.get("hub")
        if hub and force_sim:
            hub = {**hub, "backend": "sim"}
        return cls(str(lab.get("name", "lab")), backend, hub, devices, wiring, source, dict(sdr_live), calibration)

    @classmethod
    def load(cls, path: str | Path | None = None, sim: bool | None = None) -> "Inventory":
        if sim is None:
            sim = os.environ.get("OASA_LAB_SIM", "").lower() in ("1", "true", "yes")
        env = os.environ.get("OASA_LAB_INVENTORY")
        file = Path(path) if path else Path(env) if env else Path("lab.toml")
        if file.is_file():
            try:
                return cls.from_dict(tomllib.loads(file.read_text(encoding="utf-8")), str(file), sim, file.parent)
            except tomllib.TOMLDecodeError as exc:
                raise InventoryError(f"{file}: {exc}") from None
        if sim:
            return cls.from_dict(DEFAULT_SIM, "built-in simulated bench", True)
        return cls("none", "hardware", None, {}, {}, "no lab.toml")
