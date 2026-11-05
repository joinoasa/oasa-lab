"""Helpers for the SiK suites: two SiK radios on a cable, each through `oasa-radio bridge --radio serial
--sik-expect ...`, and the timing model of a framed packet through them.

    app -> bridge A -> pty -> tap -> SiK A ~~ cable ~~ SiK B -> tap -> pty -> bridge B -> app

Both run unchanged on the simulated bench and on the real one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .airtime import airtime_s
from .devices import DeviceError, SikRadio, Unavailable
from .devices.sikat import LINK_KEYS, SERIAL_BAUD, expect_arg
from .hw import Hardware, WiringRefused
from .hwlink import BridgeProc


def wire_len(n: int) -> int:
    """Bytes on the serial line for a packet of n bytes (oasa-radio docs/serial-framing.md, the longest case)."""
    return n + 4 + 1 + (n + 4) // 254 + 2


class SikBench:
    """Two SiK radios wired per the suite's wiring. Their settings were read (ATI5) when they were opened."""

    def __init__(self, lab, hw: Hardware, capture_air: bool = False):
        try:
            hw.use_wiring()
            ids = hw.inv.ids_of("sik")
            self.a: SikRadio = hw.open(ids[0])
            self.b: SikRadio = hw.open(ids[1])
        except (WiringRefused, Unavailable) as exc:
            pytest.skip(str(exc))
        self.lab, self.hw = lab, hw
        self.model = {k: v for k, v in self.a.model().items() if k != "profile"}
        if self.b.model() != self.a.model():
            raise DeviceError("the two SiK radios are declared with different profiles in lab.toml")
        self.serial_bps = SERIAL_BAUD.get(self.a.settings.get("SERIAL_SPEED", 57), 57_600)
        self.caps: list[str] = []
        if capture_air:
            self.caps = [lab.air_capture(self.a.link, self.a.id), lab.air_capture(self.b.link, self.b.id)]

    def radios(self) -> tuple[SikRadio, SikRadio]:
        return self.a, self.b

    # --- the model ---------------------------------------------------------------------------
    def serial_s(self, n: int) -> float:
        """Time to clock one framed packet of n bytes through a serial port (10 bits a byte)."""
        return wire_len(n) * 10 / self.serial_bps

    def air_s(self, n: int) -> float:
        return airtime_s(self.model, wire_len(n))

    def latency_s(self, n: int) -> float:
        """One packet alone: into radio A's serial port, over the air, out of radio B's."""
        return 2 * self.serial_s(n) + self.air_s(n)

    def busy_s(self, n: int) -> float:
        """What one packet costs in a back-to-back burst: the slower of the serial port and the air."""
        return max(self.serial_s(n), self.air_s(n))

    # --- bridges -----------------------------------------------------------------------------
    def expect(self, radio: SikRadio) -> dict[str, int]:
        """What the bridge must find: lab.toml's [devices.ID.sik] table."""
        if not radio.expect:
            raise DeviceError(f"{radio.id}: lab.toml has no [devices.{radio.id}.sik] table of expected settings")
        return dict(radio.expect)

    def bridge(self, exe: Path, radio: SikRadio, *, listen: tuple[str, int], send: tuple[str, int], log_name: str,
               expect: dict[str, int] | None = None) -> BridgeProc:
        return BridgeProc(exe, radio.link.attach_pty(), baud=radio.baud, mtu=radio.mtu, listen=listen, send=send,
                          log_path=self.lab.file(log_name),
                          extra=["--sik-expect", expect_arg(self.expect(radio) if expect is None else expect)])

    def bridge_extra(self) -> dict[str, list[str]]:
        return {"a": ["--sik-expect", expect_arg(self.expect(self.a))],
                "b": ["--sik-expect", expect_arg(self.expect(self.b))]}

    def settle(self, quiet: float = 1.0) -> None:
        for r in (self.a, self.b):
            r.link.wait_idle(quiet, 60.0)


def link_differences(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    return [k for k in LINK_KEYS if a.get(k) != b.get(k)]
