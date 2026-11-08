"""An RF power sensor that speaks SCPI: identify, zero, set frequency, read average power (dBm).

It only reads. It never transmits and has no output, so it needs no TX gate. The one risk is to the
sensor itself: too much power into it. Every hookup keeps 40 dB or more between a transmitter and the
sensor (AGENTS.md), and a reading within `margin_db` of the sensor's `max_input_dbm` stops with an error
so the operator removes the signal.

Transports (`transport` in lab.toml):
    "usbtmc"  the Linux usbtmc driver, `port = "/dev/usbtmc0"` (standard library only)
    "serial"  SCPI over a serial port, `port = "/dev/ttyUSB0"`, `baud = 9600` (pyserial)
    "tcp"     a raw SCPI socket, `host = "192.0.2.10"`, `tcp_port = 5025`

Commands live in one table per model (`COMMANDS`). Only the IEEE 488.2 common commands (*IDN?, *CLS,
*OPC?) are fixed by a standard; every other string is to be confirmed against the model's programming
guide before a real measurement (`confirmed` lists what is). `info()` records which table ran and
whether all of it is confirmed, so a bundle shows it.
"""

from __future__ import annotations

import math
import os
import random
import re
import socket
import time
from typing import Any, Protocol

from .base import Device, DeviceError, Unavailable

IEEE4882 = ("idn", "clear", "opc")   # common commands: IEEE 488.2, the same on every SCPI instrument

# One table per model. A string with {hz} is formatted with the frequency in Hz. None: the model has no such command.
COMMANDS: dict[str, dict[str, Any]] = {
    "generic": {
        "description": "Plain SCPI-99 style power meter commands (to be confirmed per model)",
        "idn": "*IDN?",
        "clear": "*CLS",
        "opc": "*OPC?",
        "unit_dbm": "UNIT:POW DBM",
        "zero": "CAL:ZERO:AUTO ONCE",
        "freq": "SENS:FREQ {hz:.0f}",
        "read": "READ?",
        "confirmed": IEEE4882,
        "freq_range_hz": None,
    },
    # The first concrete model: a USB sensor with USBTMC and SCPI on Linux, 9 kHz to 6 GHz.
    # Command strings below are TO BE CONFIRMED against the Keysight U2000 series programming guide.
    "keysight-u2004a": {
        "description": "Keysight/Agilent U2004A USB power sensor (U2000 series); commands to be confirmed",
        "idn": "*IDN?",
        "clear": "*CLS",
        "opc": "*OPC?",
        "unit_dbm": "UNIT:POW DBM",               # to be confirmed
        "zero": "CAL:ZERO:AUTO ONCE",             # to be confirmed (the guide may name a zero type first)
        "freq": "SENS:FREQ {hz:.0f}",             # to be confirmed
        "read": "READ?",                          # to be confirmed (FETC? after INIT may be the usual form)
        "confirmed": IEEE4882,
        "freq_range_hz": (9e3, 6e9),              # the model's stated range; confirm on the datasheet
    },
}

# Models the lab may get that do not speak plain SCPI. A slot for a later backend, not implemented.
STUB_MODELS: dict[str, str] = {
    "rs-nrp-z11": ("R&S NRP-Z11 uses the legacy NRP-Z protocol through R&S's NRP Toolkit, not plain SCPI. "
                   "No backend yet: one would talk to the toolkit (check its licence first) as a separate process"),
}

TRANSPORTS = ("usbtmc", "serial", "tcp")
SCPI_NAN = 9.9e37          # SCPI's "not a number" is 9.91E37; anything this large is no reading
DEFAULT_MAX_INPUT_DBM = 20.0
DEFAULT_MARGIN_DB = 3.0


class ScpiPort(Protocol):
    def write(self, line: str) -> None: ...
    def read_line(self, timeout_s: float) -> str: ...
    def close(self) -> None: ...


class UsbtmcPort:
    """/dev/usbtmcN from the Linux usbtmc driver. A read returns one whole reply; the kernel's timeout applies."""

    def __init__(self, path: str):
        try:
            # O_NOCTTY: never become the controlling terminal (a session leader, like a job, would get SIGHUP on close)
            self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        except OSError as exc:
            raise DeviceError(f"cannot open {path}: {exc} (is the usbtmc driver loaded, and may this user open it?)") from None

    def write(self, line: str) -> None:
        try:
            os.write(self.fd, (line + "\n").encode())
        except OSError as exc:
            raise DeviceError(f"usbtmc write failed: {exc}") from None

    def read_line(self, timeout_s: float) -> str:
        try:
            return os.read(self.fd, 65536).decode(errors="replace").strip()
        except OSError as exc:
            raise DeviceError(f"usbtmc read failed (no reply?): {exc}") from None

    def close(self) -> None:
        os.close(self.fd)


class _LinePort:
    """Shared by serial and TCP: replies end with a newline."""

    def _chunk(self) -> bytes:
        raise NotImplementedError

    def read_line(self, timeout_s: float) -> str:
        end = time.monotonic() + timeout_s
        buf = getattr(self, "_buf", b"")
        while b"\n" not in buf:
            if time.monotonic() > end:
                self._buf = buf
                raise DeviceError("no reply from the power sensor")
            buf += self._chunk()
        line, _, self._buf = buf.partition(b"\n")
        return line.decode(errors="replace").strip()


class SerialScpiPort(_LinePort):
    def __init__(self, path: str, baud: int):
        from .modem import SerialPort
        self.s = SerialPort(path, baud, dtr=True, rts=False)

    def write(self, line: str) -> None:
        self.s.write((line + "\n").encode())

    def _chunk(self) -> bytes:
        return self.s.read()

    def close(self) -> None:
        self.s.close()


class TcpScpiPort(_LinePort):
    def __init__(self, host: str, port: int, timeout_s: float):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout_s)
        except OSError as exc:
            raise DeviceError(f"cannot reach the power sensor at {host}:{port}: {exc}") from None
        self.sock.settimeout(0.1)

    def write(self, line: str) -> None:
        self.sock.sendall((line + "\n").encode())

    def _chunk(self) -> bytes:
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            return b""
        if not data:
            raise DeviceError("the power sensor closed the connection")
        return data

    def close(self) -> None:
        self.sock.close()


class SimSensorPort:
    """Answers the model's own command table, so the simulated bench runs the same driver code.

    sim settings: level_dbm (what is at the input), noise_db (1-sigma), seed, idn (a different *IDN? reply)."""

    def __init__(self, table: dict[str, Any], cfg: dict[str, Any], serial: str):
        self.t = table
        self.level_dbm = float(cfg.get("level_dbm", -30.0))
        self.noise_db = float(cfg.get("noise_db", 0.02))
        self.rng = random.Random(int(cfg.get("seed", 0)))
        self.idn = str(cfg.get("idn", f"OASA,SIM-POWER-SENSOR,{serial},0.1"))
        self.freq_hz: float | None = None
        self.zeroed = False
        self.dbm = False
        self.written: list[str] = []
        self.replies: list[str] = []
        self.closed = False

    def write(self, line: str) -> None:
        self.written.append(line)
        t = self.t
        freq_prefix = (t.get("freq") or "").split("{")[0]
        if line == t["idn"]:
            self.replies.append(self.idn)
        elif line == t["opc"]:
            self.replies.append("1")
        elif line == t.get("zero"):
            self.zeroed = True
        elif line == t.get("unit_dbm"):
            self.dbm = True
        elif freq_prefix and line.startswith(freq_prefix):
            self.freq_hz = float(line[len(freq_prefix):])
        elif line == t["read"]:
            if not self.dbm:
                self.replies.append(f"{10 ** (self.level_dbm / 10) / 1000:+.6E}")   # watts until told otherwise
            else:
                self.replies.append(f"{self.level_dbm + self.rng.gauss(0, self.noise_db):+.4E}")

    def read_line(self, timeout_s: float) -> str:
        if not self.replies:
            raise DeviceError("no reply from the power sensor")
        return self.replies.pop(0)

    def close(self) -> None:
        self.closed = True


class PowerSensor(Device):
    kind = "power_sensor"

    def _open(self) -> None:
        model = str(self.cfg.get("model", "generic"))
        if model in STUB_MODELS:
            raise Unavailable(f"{self.id}: model {model!r}: {STUB_MODELS[model]}")
        if model not in COMMANDS:
            raise DeviceError(f"{self.id}: model must be one of {sorted(COMMANDS)} (or a stub: {sorted(STUB_MODELS)})")
        self.model_key, self.t = model, COMMANDS[model]
        self.transport = str(self.cfg.get("transport", "usbtmc"))
        if self.transport not in TRANSPORTS:
            raise DeviceError(f"{self.id}: transport must be one of {TRANSPORTS}")
        self.timeout_s = float(self.cfg.get("timeout_s", 5.0))
        self.max_input_dbm = float(self.cfg.get("max_input_dbm", DEFAULT_MAX_INPUT_DBM))
        self.margin_db = float(self.cfg.get("margin_db", DEFAULT_MARGIN_DB))
        lo, hi = self.t.get("freq_range_hz") or (None, None)
        self.min_freq_hz = float(self.cfg.get("min_freq_hz", lo or 1.0))
        self.max_freq_hz = float(self.cfg.get("max_freq_hz", hi or 1e12))
        self.freq_hz: float | None = None
        self.zeroed = False
        self.port: ScpiPort = self._make_port()
        try:
            self.idn = self.query(self.t["idn"])
            parts = [p.strip() for p in self.idn.split(",")]
            if len(parts) != 4:   # IEEE 488.2: maker, model, serial, firmware
                raise DeviceError(f"{self.id}: *IDN? gave {self.idn!r}, not maker,model,serial,firmware")
            self.maker, self.model_name, self.serial, self.firmware = parts
            want = str(self.cfg.get("serial", ""))
            if want and want != self.serial:
                raise DeviceError(f"{self.id}: serial is {self.serial}, lab.toml expects {want}")
            if self.t.get("clear"):
                self.port.write(self.t["clear"])
            if self.t.get("unit_dbm"):
                self.port.write(self.t["unit_dbm"])
        except BaseException:
            self.port.close()
            raise

    def _make_port(self) -> ScpiPort:
        if self.backend == "sim":
            return SimSensorPort(self.t, dict(self.cfg.get("sim", {})), str(self.cfg.get("serial", "SIM-PSENS-0001")))
        if self.transport == "usbtmc":
            return UsbtmcPort(str(self.cfg["port"]))
        if self.transport == "serial":
            return SerialScpiPort(str(self.cfg["port"]), int(self.cfg.get("baud", 9600)))
        return TcpScpiPort(str(self.cfg["host"]), int(self.cfg.get("tcp_port", 5025)), self.timeout_s)

    def _close(self) -> None:
        self.port.close()

    def _info(self) -> dict[str, Any]:
        cmds = [k for k in ("idn", "clear", "opc", "unit_dbm", "zero", "freq", "read") if self.t.get(k)]
        return {"model": f"{self.maker} {self.model_name}", "serial": self.serial, "firmware": self.firmware,
                "command_table": self.model_key, "commands_confirmed": all(k in self.t["confirmed"] for k in cmds),
                "transport": self.transport, "max_input_dbm": self.max_input_dbm, "frequency_hz": self.freq_hz,
                "zeroed": self.zeroed}

    # --- SCPI -------------------------------------------------------------------------------------
    def query(self, cmd: str, timeout_s: float | None = None) -> str:
        self.port.write(cmd)
        return self.port.read_line(self.timeout_s if timeout_s is None else timeout_s)

    def _wait(self, timeout_s: float) -> None:
        if self.query(self.t["opc"], timeout_s).strip() != "1":
            raise DeviceError(f"{self.id}: the sensor did not finish (*OPC? did not answer 1)")

    # --- what suites call ------------------------------------------------------------------------
    def identify(self) -> dict[str, str]:
        return {"maker": self.maker, "model": self.model_name, "serial": self.serial, "firmware": self.firmware}

    def zero(self, timeout_s: float = 30.0) -> None:
        """Zero the sensor. Do it with no signal at its input (the transmitter off or disconnected)."""
        if not self.t.get("zero"):
            raise Unavailable(f"{self.id}: model {self.model_key!r} has no zero command in the table")
        self.port.write(self.t["zero"])
        self._wait(timeout_s)
        self.zeroed = True

    def set_frequency(self, hz: float) -> None:
        """The frequency correction for what is being measured."""
        hz = float(hz)
        if not (math.isfinite(hz) and self.min_freq_hz <= hz <= self.max_freq_hz):
            raise DeviceError(f"{self.id}: {hz:g} Hz is outside {self.min_freq_hz:g} to {self.max_freq_hz:g} Hz")
        self.port.write(self.t["freq"].format(hz=hz))
        self._wait(self.timeout_s)
        self.freq_hz = hz

    def read_dbm(self, count: int = 1) -> float:
        """Average power in dBm (the mean of `count` readings, averaged in mW)."""
        if self.freq_hz is None:
            raise DeviceError(f"{self.id}: set_frequency() first: a reading without frequency correction is wrong")
        if count < 1:
            raise ValueError("count must be 1 or more")
        mw = []
        for _ in range(count):
            text = self.query(self.t["read"])
            mm = re.fullmatch(r"\s*([-+]?\d+(\.\d*)?([eE][-+]?\d+)?)\s*", text)
            if not mm:
                raise DeviceError(f"{self.id}: {self.t['read']} gave {text!r}")
            v = float(mm.group(1))
            if not math.isfinite(v) or abs(v) >= SCPI_NAN:
                raise DeviceError(f"{self.id}: the sensor gave no valid reading ({text!r})")
            if v >= self.max_input_dbm - self.margin_db:
                raise DeviceError(f"{self.id}: {v:.2f} dBm is within {self.margin_db:g} dB of the sensor's maximum "
                                  f"input ({self.max_input_dbm:g} dBm). Remove the signal and add attenuation")
            mw.append(10 ** (v / 10))
        return 10 * math.log10(sum(mw) / len(mw))
