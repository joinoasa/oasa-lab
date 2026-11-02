"""Ettus B200mini and USRP E310, receive only.

UHD is GPL-3.0, so it is never imported or shipped: `uhd_usrp_probe`, `uhd_config_info` and
the receive examples run as separate processes. These drivers have no transmit path. The only radio
tools they start are receive examples: `rx_samples_to_file` for the E310, which needs a subdevice and
channel, and `rx_samples_c` for the B200mini, because in UHD 4.6 `rx_samples_to_file --gain` fails on
that radio with an "RX channel out of range" error.

A capture with an overflow, a dropped packet or the wrong sample count is an error, not a short recording.

`max_rx_rate` is the highest rate a driver will ask for. The defaults are low on purpose. Raise it in
`lab.toml` for a host once that host has shown the rate stays clean.
"""

from __future__ import annotations

import math
import random
import re
import shutil
import subprocess
import tempfile
import time
from array import array
from pathlib import Path
from typing import Any

from ..iq import DATATYPES, IQ
from .base import Device, DeviceError, Unavailable

EXAMPLE_DIRS = ("/usr/libexec/uhd/examples", "/usr/lib/uhd/examples")
PROBE_TIMEOUT_S = 60.0
START_TIMEOUT_S = 30.0  # first start of a B200 loads its firmware and FPGA image
SIM_NOISE_DBFS_AT_0DB = -107.0  # simulated receive chain: noise in dBFS = this + gain


class Usrp(Device):
    model_name = "USRP"
    default_max_rx_rate = 1e6
    default_antenna = "RX2"
    rx_tool_name = "rx_samples_to_file"
    datatype = "ci16_le"

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._facts: dict[str, Any] = {}
        self._rx_tool: str | None = None
        self._tmp: Path | None = None

    # --- to override -------------------------------------------------------------------------
    def device_args(self) -> str:
        raise NotImplementedError

    def _parse_probe(self, text: str) -> dict[str, Any]:
        raise NotImplementedError

    # --- helpers ---------------------------------------------------------------------------------
    @property
    def max_rx_rate(self) -> float:
        return float(self.cfg.get("max_rx_rate", self.default_max_rx_rate))

    @property
    def max_rx_gain(self) -> float:
        return float(self.cfg.get("max_rx_gain", 60.0))

    def _run(self, cmd: list[str], timeout: float) -> str:
        try:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DeviceError(f"{self.id}: {Path(cmd[0]).name} did not finish in {timeout:.0f} s") from None
        text = r.stdout.decode("utf-8", "replace")
        if r.returncode != 0:
            raise DeviceError(f"{self.id}: {Path(cmd[0]).name} failed: {text.strip()[-300:]}")
        return text

    @staticmethod
    def _field(text: str, name: str) -> str | None:
        m = re.search(rf"\b{re.escape(name)}:\s*([A-Za-z0-9._+-]+)", text)  # the probe pads some lines with other characters
        return m.group(1) if m else None

    # --- real and simulated ----------------------------------------------------------------------
    def _open(self) -> None:
        if not 0 < self.max_rx_rate:
            raise DeviceError(f"{self.id}: max_rx_rate must be above 0")
        self._tmp = Path(tempfile.mkdtemp(prefix="oasa-usrp-"))
        if self.backend == "sim":
            self._facts = {"serial": self.cfg.get("serial", "SIM-USRP"), "firmware": "sim", "uhd": "simulated"}
            return
        try:
            probe, version = shutil.which("uhd_usrp_probe"), shutil.which("uhd_config_info")
            self._rx_tool = shutil.which(self.rx_tool_name) or next(
                (str(p) for p in (Path(d) / self.rx_tool_name for d in EXAMPLE_DIRS) if p.is_file()), None)
            if not probe or not version or not self._rx_tool:
                raise Unavailable(f"UHD tools not found (uhd_usrp_probe, uhd_config_info, {self.rx_tool_name})")
            facts = self._parse_probe(self._run([probe, "--args", self.device_args()], PROBE_TIMEOUT_S))
            want = str(self.cfg.get("serial", ""))
            if not facts.get("serial"):
                raise DeviceError(f"{self.id}: the probe reported no serial number")
            if want and facts["serial"] != want:
                raise DeviceError(f"{self.id}: serial is {facts['serial']}, lab.toml expects {want}")
            m = re.search(r"UHD\s+(\S+)", self._run([version, "--version"], 15.0))
            facts["uhd"] = m.group(1) if m else "unknown"
            self._facts = facts
        except BaseException:
            shutil.rmtree(self._tmp, ignore_errors=True)
            raise

    def _close(self) -> None:
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)

    def _info(self) -> dict[str, Any]:
        return {"model": self.model_name + (" (simulated)" if self.backend == "sim" else ""), **self._facts,
                "max_rx_rate": self.max_rx_rate, "receive_only": True}

    # --- what suites call ------------------------------------------------------------------------
    def capture(self, center_hz: float, sample_rate: float, samples: int, gain_db: float = 40.0) -> IQ:
        """Receive `samples` IQ pairs and return them in memory. `iq.datatype` and `iq.start_time` say how and when."""
        center_hz, sample_rate, samples, gain_db = float(center_hz), float(sample_rate), int(samples), float(gain_db)
        if not 0 < sample_rate <= self.max_rx_rate:
            raise DeviceError(f"{self.id}: refused {sample_rate:g} S/s, max_rx_rate is {self.max_rx_rate:g}")
        if not 0 <= gain_db <= self.max_rx_gain:
            raise DeviceError(f"{self.id}: refused gain {gain_db:g} dB, max_rx_gain is {self.max_rx_gain:g}")
        if samples <= 0:
            raise DeviceError(f"{self.id}: samples must be above 0")
        seconds = samples / sample_rate
        if self.backend == "sim":
            data, ended = self._sim_noise(samples, gain_db), time.time()
        else:
            data, ended = self._receive(center_hz, sample_rate, samples, gain_db, seconds)
        iq = IQ(data, self.datatype, sample_rate, center_hz)
        iq.start_time = ended - seconds  # type: ignore[attr-defined]
        return iq

    def rx_command(self, out: Path, center_hz: float, sample_rate: float, samples: int, gain_db: float) -> list[str]:
        cmd = [str(self._rx_tool), "--args", self.device_args(), "--file", str(out), "--type", "short",
               "--nsamps", str(samples), "--rate", f"{sample_rate:.0f}", "--freq", f"{center_hz:.0f}",
               "--gain", f"{gain_db:g}", "--ant", str(self.cfg.get("antenna", self.default_antenna)),
               "--channels", str(self.cfg.get("channel", 0))]
        subdev = self.cfg.get("subdev")
        return cmd + (["--subdev", str(subdev)] if subdev else [])

    def _receive(self, center_hz: float, fs: float, n: int, gain: float, seconds: float) -> tuple[bytes, float]:
        assert self._tmp is not None
        out = self._tmp / "rx.iq"
        text = self._run(self.rx_command(out, center_hz, fs, n, gain), START_TIMEOUT_S + seconds * 2)
        ended = time.time()
        if re.search(r"overflow|^[ODS]+\s*$|\bLate\b|ERROR_CODE", text, re.IGNORECASE | re.MULTILINE):
            raise DeviceError(f"{self.id}: samples were lost at {fs:g} S/s: {text.strip()[-200:]}")
        per_sample = DATATYPES[self.datatype]
        if not out.is_file() or out.stat().st_size != per_sample * n:
            size = out.stat().st_size if out.is_file() else 0
            raise DeviceError(f"{self.id}: asked for {n} samples, got {size / per_sample:g}")
        data = out.read_bytes()
        out.unlink()
        return data, ended

    def _sim_noise(self, n: int, gain_db: float) -> bytes:
        rng = self.world.rng if self.world is not None else random.Random(1)
        sim = self.cfg.get("sim") or {}
        dbfs = float(sim.get("noise_dbfs", SIM_NOISE_DBFS_AT_0DB + gain_db))
        sigma = 32767 * 10 ** (dbfs / 20) / math.sqrt(2)
        if self.datatype == "cf32_le":
            return array("f", (rng.gauss(0, sigma) / 32768 for _ in range(2 * n))).tobytes()
        return array("h", (max(-32768, min(32767, round(rng.gauss(0, sigma)))) for _ in range(2 * n))).tobytes()


class B200Mini(Usrp):
    kind = "b200"
    model_name = "Ettus B200mini"
    default_max_rx_rate = 10e6
    rx_tool_name = "rx_samples_c"
    datatype = "cf32_le"

    def rx_command(self, out: Path, center_hz: float, sample_rate: float, samples: int, gain_db: float) -> list[str]:
        """Always the RX2 port: rx_samples_c has no antenna option, and RX2 is the B200mini's receive default."""
        return [str(self._rx_tool), "-a", self.device_args(), "-f", f"{center_hz:.0f}", "-r", f"{sample_rate:.0f}",
                "-g", f"{gain_db:g}", "-n", str(samples), "-o", str(out)]

    def device_args(self) -> str:
        serial = self.cfg.get("serial")
        return f"type=b200,serial={serial}" if serial else "type=b200"

    def _parse_probe(self, text: str) -> dict[str, Any]:
        fw, fpga = self._field(text, "FW Version"), self._field(text, "FPGA Version")
        return {"serial": self._field(text, "serial"), "firmware": f"fw {fw}, fpga {fpga}"}


class E310(Usrp):
    kind = "e310"
    model_name = "Ettus USRP E310"
    default_max_rx_rate = 1e6

    def device_args(self) -> str:
        addr = self.cfg.get("addr")
        if not addr:
            raise DeviceError(f"{self.id}: lab.toml needs addr, the E310's network address")
        return f"type=e3xx,addr={addr}"

    def _parse_probe(self, text: str) -> dict[str, Any]:
        mpm, fpga = self._field(text, "MPM Version"), self._field(text, "FPGA Version")
        return {"serial": self._field(text, "serial"), "firmware": f"mpm {mpm}, fpga {fpga}",
                "image": self._field(text, "mender_artifact")}
