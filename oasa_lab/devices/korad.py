"""KORAD KA3005P bench supply on a USB serial port.

The limits here are a hard stop, not a default. `lab.toml` can lower them and never raise them, and a
value over the limit is refused before a byte reaches the port. The supply also keeps settings made
on its front panel, so turning the output on checks what is set first.

By default the output is off at open and at close: a supply left on is a risk. A supply that feeds a
radio is different, because cutting it is a hard power loss and the radio may need a button press to
come back. Set `hold_output = true` for that unit: the driver then never changes the output on its
own, and only an explicit `output()` call does. The limits apply either way.

`close` turns the output off only if it runs. A killed process cannot do that, so the next `open` does.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .base import Device, DeviceError
from .modem import PortLike, SerialPort

HARD_MAX_V = 6.0
HARD_MAX_A = 3.0
BAUD = 9600
TOLERANCE_V = 0.005
TOLERANCE_A = 0.0005
STATUS_OUTPUT = 0x40
STATUS_CV = 0x01


class SimKoradPort:
    """Speaks the KA3005P command set, so the simulated bench runs the same driver code as the real one."""

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        self.vset, self.iset = float(cfg.get("vset", 0.0)), float(cfg.get("iset", 1.0))
        self.on = bool(cfg.get("on", False))
        self.load_a = float(cfg.get("load_a", 0.385))
        self.serial = str(cfg.get("serial", "SIM-KORAD-0001"))
        self.ignore_set = bool(cfg.get("ignore_set", False))
        self.reply = b""
        self.written: list[str] = []

    def write(self, data: bytes) -> None:
        cmd = data.decode()
        self.written.append(cmd)
        if cmd == "*IDN?":
            self.reply = f"KORAD KA3005P V1.5 SN:{self.serial}".encode()
        elif cmd == "VSET1?":
            self.reply = f"{self.vset:05.2f}".encode()
        elif cmd == "ISET1?":
            self.reply = f"{self.iset:05.3f}".encode()
        elif cmd == "VOUT1?":
            self.reply = f"{self.vset if self.on else 0.0:05.2f}".encode()
        elif cmd == "IOUT1?":
            self.reply = f"{min(self.load_a, self.iset) if self.on else 0.0:05.3f}".encode()
        elif cmd == "STATUS?":
            self.reply = bytes([(STATUS_OUTPUT if self.on else 0) | STATUS_CV])
        elif cmd.startswith("VSET1:") and not self.ignore_set:
            self.vset = float(cmd[6:])
        elif cmd.startswith("ISET1:") and not self.ignore_set:
            self.iset = float(cmd[6:])
        elif cmd in ("OUT1", "OUT0"):
            self.on = cmd == "OUT1"

    def read(self, timeout: float = 0.05) -> bytes:
        out, self.reply = self.reply, b""
        return out

    def close(self) -> None:
        pass


class Korad(Device):
    kind = "korad"

    def _limit(self, key: str, hard: float) -> float:
        value = float(self.cfg.get(key, hard))
        if not 0 < value <= hard:
            raise DeviceError(f"{self.id}: {key} = {value:g} must be above 0 and at most {hard:g}")
        return value

    def _open(self) -> None:
        self.max_v = self._limit("max_v", HARD_MAX_V)
        self.max_a = self._limit("max_a", HARD_MAX_A)
        self.hold_output = bool(self.cfg.get("hold_output", False))
        self.port: PortLike = self._make_port()
        try:
            self.idn = self._ask("*IDN?")
            mm = re.search(r"\bKA3005P\b\s+(\S+)\s+SN:(\S+)", self.idn)
            if not mm:
                raise DeviceError(f"{self.id}: this is not a KA3005P (it said {self.idn!r})")
            self.firmware, self.serial = mm.group(1), mm.group(2)
            want = str(self.cfg.get("serial", ""))
            if want and want != self.serial:
                raise DeviceError(f"{self.id}: serial is {self.serial}, lab.toml expects {want}")
            self._check_settings()
            if not self.hold_output:
                self._switch(False)
        except BaseException:
            self.port.close()
            raise

    def _make_port(self) -> PortLike:
        if self.backend == "sim":
            return SimKoradPort({"serial": self.cfg.get("serial", "SIM-KORAD-0001"), **self.cfg.get("sim", {})})
        return SerialPort(str(self.cfg["port"]), BAUD)

    def _close(self) -> None:
        try:
            if not self.hold_output:
                self._switch(False)
        finally:
            self.port.close()

    def _info(self) -> dict[str, Any]:
        return {"model": "KORAD KA3005P", "serial": self.serial, "firmware": self.firmware, "max_v": self.max_v,
                "max_a": self.max_a, "hold_output": self.hold_output, **self.read()}

    # --- the serial conversation --------------------------------------------------------------
    def _ask(self, cmd: str, settle_s: float = 0.25) -> str:
        """The KA3005P ends its replies with nothing at all, so a reply is over when the line goes quiet."""
        self.port.read()
        self.port.write(cmd.encode())
        return self._reply(settle_s).decode(errors="replace").strip("\x00 \r\n")

    def _reply(self, settle_s: float) -> bytes:
        end = time.monotonic() + settle_s
        got = b""
        while time.monotonic() < end:
            data = self.port.read()
            got += data
            if self.backend == "sim" and got:
                break
            if data:
                end = max(end, time.monotonic() + 0.05)
            elif not got:
                time.sleep(0.01)
        if not got:
            raise DeviceError(f"{self.id}: no reply from the supply")
        return got

    def _number(self, cmd: str) -> float:
        text = self._ask(cmd)
        try:
            return float(re.match(r"\d+\.\d+", text).group(0))  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            raise DeviceError(f"{self.id}: {cmd} gave {text!r}") from None

    def _status(self) -> int:
        self.port.read()
        self.port.write(b"STATUS?")
        return self._reply(0.25)[0]

    def _write(self, cmd: str) -> None:
        self.port.write(cmd.encode())
        time.sleep(0 if self.backend == "sim" else 0.1)  # the supply drops commands sent too close together

    def _check_settings(self) -> None:
        """Settings can come from the front panel, so they are never trusted."""
        vset, iset = self._number("VSET1?"), self._number("ISET1?")
        if vset > self.max_v + TOLERANCE_V or iset > self.max_a + TOLERANCE_A:
            raise DeviceError(f"{self.id}: the supply is set to {vset:g} V, {iset:g} A, over the limit "
                              f"{self.max_v:g} V, {self.max_a:g} A. Fix it on the front panel")

    def _switch(self, on: bool) -> None:
        self._write("OUT1" if on else "OUT0")
        if bool(self._status() & STATUS_OUTPUT) != on:
            raise DeviceError(f"{self.id}: output did not turn {'on' if on else 'off'}")

    # --- what suites call ------------------------------------------------------------------------
    def set(self, volts: float, amps: float) -> None:
        """Set voltage and current limit, then read them back."""
        volts, amps = float(volts), float(amps)
        if not 0 <= volts <= self.max_v:
            raise DeviceError(f"{self.id}: refused {volts:g} V, the limit is {self.max_v:g} V")
        if not 0 <= amps <= self.max_a:
            raise DeviceError(f"{self.id}: refused {amps:g} A, the limit is {self.max_a:g} A")
        self._write(f"VSET1:{volts:05.2f}")
        self._write(f"ISET1:{amps:05.3f}")
        vset, iset = self._number("VSET1?"), self._number("ISET1?")
        if abs(vset - volts) > TOLERANCE_V or abs(iset - amps) > TOLERANCE_A:
            raise DeviceError(f"{self.id}: asked for {volts:g} V, {amps:g} A, the supply says {vset:g} V, {iset:g} A")

    def output(self, on: bool) -> None:
        """Turn the output on or off and check that it happened."""
        if on:
            self._check_settings()
        self._switch(bool(on))

    def read(self) -> dict[str, Any]:
        status = self._status()
        return {"vset": self._number("VSET1?"), "iset": self._number("ISET1?"), "vout": self._number("VOUT1?"),
                "iout": self._number("IOUT1?"), "output": bool(status & STATUS_OUTPUT),
                "mode": "cv" if status & STATUS_CV else "cc"}
