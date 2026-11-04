"""Ettus B200mini and USRP E310: receive, and a test tone to transmit over cable.

UHD is GPL-3.0, so it is never imported or shipped: `uhd_usrp_probe`, `uhd_config_info` and the UHD
examples run as separate processes. To receive: `rx_samples_to_file` for the E310, which needs a
subdevice and channel, and `rx_samples_c` for the B200mini, because in UHD 4.6 `rx_samples_to_file
--gain` fails on that radio with an "RX channel out of range" error. To transmit: `tx_waveforms` for a
single sine tone, and `tx_samples_from_file` to send a block of IQ samples on repeat (a test waveform
the suite made, never anything from a caller).

A capture with an overflow, a dropped packet or the wrong sample count is an error, not a short recording.

Transmitting goes through `require_tx` first (the suite must declare `transmits = true` and the
operator must have confirmed the wiring). The gain is capped by `max_tx_gain` (default 40 dB, the level
first tested on a cabled bench; raise it in `lab.toml` for a bench that has measured its path losses,
never above the radio's own maximum). The transmitter also stops by itself after `max_tx_seconds` (the tone
tool by its sample count, the file tool by a watchdog), so a crashed suite cannot leave a transmitter
running for long. `close()` always stops it.

UHD 4.6 on the E310 sometimes fails to claim the radio right after another UHD process used it ("Someone
tried to claim this device again"); the old claim expires on the E310 a few seconds later. Both the
transmit and the receive call wait and try again.

`max_rx_rate` and `max_tx_rate` are the highest rates a driver will ask for. The defaults are low on
purpose. Raise them in `lab.toml` for a host once that host has shown the rate stays clean.
"""

from __future__ import annotations

import cmath
import math
import random
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from array import array
from collections import deque
from pathlib import Path
from typing import Any

from ..iq import DATATYPES, IQ
from .base import Device, DeviceError, Unavailable

EXAMPLE_DIRS = ("/usr/libexec/uhd/examples", "/usr/lib/uhd/examples")
PROBE_TIMEOUT_S = 60.0
START_TIMEOUT_S = 30.0  # first start of a B200 loads its firmware and FPGA image
SIM_NOISE_DBFS_AT_0DB = -107.0  # simulated receive chain: noise in dBFS = this + gain
SIM_RX_K_DB = 0.0               # simulated receive chain: dBFS = dBm + gain + this
TX_DBM_AT_0DB = -75.0           # full-scale tone at TX gain 0, 915 MHz (est.; with 60 dB of cable, TX 40 and
                                # amplitude 0.3 a real B200mini gave -66 dBFS on an E310 at RX gain 40)
DEFAULT_MAX_TX_GAIN = 40.0      # dB. The level first tested on a cabled bench (2 x 60 dB). lab.toml may raise it
HARD_MAX_TX_GAIN = 89.75        # the AD936x maximum in UHD; nothing above it is accepted
MAX_TX_AMPL = 0.7               # tx_waveforms refuses more
DEFAULT_MAX_TX_RATE = 1e6
DEFAULT_MAX_TX_SECONDS = 60.0   # the tone process stops by itself after this
TX_START_TIMEOUT_S = 45.0       # a B200 loads its firmware and FPGA image on first use
CLAIM_RETRIES = 2
CLAIM_WAIT_S = 15.0             # the E310 drops a stale claim after a few seconds
CLAIM_CLASH = "claim this device again"


class Usrp(Device):
    model_name = "USRP"
    default_max_rx_rate = 1e6
    default_antenna = "RX2"
    rx_tool_name = "rx_samples_to_file"
    tx_tool_name = "tx_waveforms"
    tx_file_tool_name = "tx_samples_from_file"
    datatype = "ci16_le"
    default_tx_port = "tx/rx"     # names used in [wiring] paths: "<device id>:<port>"
    default_tx_subdev: str | None = None

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._facts: dict[str, Any] = {}
        self._rx_tool: str | None = None
        self._tx_tool: str | None = None
        self._tx_file_tool: str | None = None
        self._tx_watchdog: threading.Timer | None = None
        self._tmp: Path | None = None
        self._tx_proc: subprocess.Popen | None = None
        self._tx_log: deque[str] = deque(maxlen=200)

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

    @property
    def max_tx_gain(self) -> float:
        return float(self.cfg.get("max_tx_gain", DEFAULT_MAX_TX_GAIN))

    @property
    def max_tx_rate(self) -> float:
        return float(self.cfg.get("max_tx_rate", DEFAULT_MAX_TX_RATE))

    @property
    def max_tx_seconds(self) -> float:
        return float(self.cfg.get("max_tx_seconds", DEFAULT_MAX_TX_SECONDS))

    @property
    def tx_port(self) -> str:
        return str(self.cfg.get("tx_port", self.default_tx_port))

    @property
    def rx_port(self) -> str:
        return str(self.cfg.get("rx_port", "rx2"))

    @property
    def transmitting(self) -> bool:
        if self.backend == "sim":
            return self.world is not None and f"{self.id}:{self.tx_port}" in self.world.tones
        return self._tx_proc is not None and self._tx_proc.poll() is None

    def _find_tool(self, name: str) -> str | None:
        return shutil.which(name) or next(
            (str(p) for p in (Path(d) / name for d in EXAMPLE_DIRS) if p.is_file()), None)

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
        if not 0 <= self.max_tx_gain <= HARD_MAX_TX_GAIN:
            raise DeviceError(f"{self.id}: max_tx_gain must be 0 to {HARD_MAX_TX_GAIN:g} dB")
        if not 0 < self.max_tx_rate or not 0 < self.max_tx_seconds:
            raise DeviceError(f"{self.id}: max_tx_rate and max_tx_seconds must be above 0")
        self._tmp = Path(tempfile.mkdtemp(prefix="oasa-usrp-"))
        if self.backend == "sim":
            self._facts = {"serial": self.cfg.get("serial", "SIM-USRP"), "firmware": "sim", "uhd": "simulated"}
            return
        try:
            probe, version = shutil.which("uhd_usrp_probe"), shutil.which("uhd_config_info")
            self._rx_tool = self._find_tool(self.rx_tool_name)
            self._tx_tool = self._find_tool(self.tx_tool_name)   # optional: only a transmitting suite needs it
            self._tx_file_tool = self._find_tool(self.tx_file_tool_name)
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
        try:
            self.tx_off()
        finally:
            if self._tmp is not None:
                shutil.rmtree(self._tmp, ignore_errors=True)

    def _info(self) -> dict[str, Any]:
        return {"model": self.model_name + (" (simulated)" if self.backend == "sim" else ""), **self._facts,
                "max_rx_rate": self.max_rx_rate, "max_tx_gain": self.max_tx_gain, "max_tx_rate": self.max_tx_rate,
                "tx_port": self.tx_port, "rx_port": self.rx_port}

    # --- transmit --------------------------------------------------------------------------------
    def tx_tone(self, rf_hz: float, offset_hz: float, gain_db: float, scale: float, sample_rate: float) -> None:
        """Send one sine tone at rf_hz + offset_hz until tx_off() (or max_tx_seconds). Returns once it is on air."""
        self.require_tx("tx_tone")
        rf_hz, offset_hz, gain_db, scale, fs = float(rf_hz), float(offset_hz), float(gain_db), float(scale), float(sample_rate)
        if not 0 <= gain_db <= self.max_tx_gain:
            raise DeviceError(f"{self.id}: refused TX gain {gain_db:g} dB, max_tx_gain is {self.max_tx_gain:g}")
        if not 0 < scale <= MAX_TX_AMPL:
            raise DeviceError(f"{self.id}: refused amplitude {scale:g}, the range is 0 to {MAX_TX_AMPL:g}")
        if not 0 < fs <= self.max_tx_rate:
            raise DeviceError(f"{self.id}: refused {fs:g} S/s, max_tx_rate is {self.max_tx_rate:g}")
        if not abs(offset_hz) < fs / 2:
            raise DeviceError(f"{self.id}: tone offset {offset_hz:g} Hz is outside +-{fs / 2:g} Hz")
        self.tx_off()
        if self.backend == "sim":
            self.world.tones[f"{self.id}:{self.tx_port}"] = {
                "rf_hz": rf_hz + offset_hz, "dbm": TX_DBM_AT_0DB + gain_db + 20 * math.log10(scale),
                "ppm": float((self.cfg.get("sim") or {}).get("xo_ppm", 0.0))}
            return
        if not self._tx_tool:
            raise Unavailable(f"{self.id}: UHD tool {self.tx_tool_name} not found")
        cmd = self.tx_command(rf_hz, offset_hz, gain_db, scale, fs, int(fs * self.max_tx_seconds))
        for attempt in range(CLAIM_RETRIES + 1):
            try:
                self._start_tx(cmd)
                return
            except DeviceError as exc:
                if CLAIM_CLASH not in str(exc) or attempt == CLAIM_RETRIES:
                    raise
                time.sleep(CLAIM_WAIT_S)

    def tx_command(self, rf_hz: float, offset_hz: float, gain_db: float, scale: float, fs: float, nsamps: int) -> list[str]:
        cmd = [str(self._tx_tool), "--args", self.device_args(), "--rate", f"{fs:.0f}", "--freq", f"{rf_hz:.0f}",
               "--gain", f"{gain_db:g}", "--ampl", f"{scale:g}", "--wave-type", "SINE", "--wave-freq", f"{offset_hz:.0f}",
               "--nsamps", str(nsamps), "--ant", str(self.cfg.get("tx_antenna", "TX/RX")),
               "--channels", str(self.cfg.get("tx_channel", 0))]
        subdev = self.cfg.get("tx_subdev", self.default_tx_subdev)
        return cmd + (["--subdev", str(subdev)] if subdev else [])

    def tx_file(self, samples: list[complex], rf_hz: float, gain_db: float, sample_rate: float) -> None:
        """Send `samples` (complex, magnitude 1.0 = full scale) on repeat until tx_off() or max_tx_seconds."""
        self.require_tx("tx_file")
        rf_hz, gain_db, fs = float(rf_hz), float(gain_db), float(sample_rate)
        if not 0 <= gain_db <= self.max_tx_gain:
            raise DeviceError(f"{self.id}: refused TX gain {gain_db:g} dB, max_tx_gain is {self.max_tx_gain:g}")
        if not 0 < fs <= self.max_tx_rate:
            raise DeviceError(f"{self.id}: refused {fs:g} S/s, max_tx_rate is {self.max_tx_rate:g}")
        if not samples:
            raise DeviceError(f"{self.id}: nothing to send")
        peak = max(abs(v) for v in samples)
        if not 0 < peak <= MAX_TX_AMPL:
            raise DeviceError(f"{self.id}: refused samples with peak {peak:.3g}, the range is 0 to {MAX_TX_AMPL:g}")
        self.tx_off()
        if self.backend == "sim":
            self.world.tones[f"{self.id}:{self.tx_port}"] = {
                "rf_hz": rf_hz, "dbm": TX_DBM_AT_0DB + gain_db, "iq": list(samples), "fs": fs,
                "ppm": float((self.cfg.get("sim") or {}).get("xo_ppm", 0.0))}
            return
        if not self._tx_file_tool:
            raise Unavailable(f"{self.id}: UHD tool {self.tx_file_tool_name} not found")
        assert self._tmp is not None
        path = self._tmp / "tx.cf32"
        path.write_bytes(array("f", (c for v in samples for c in (v.real, v.imag))).tobytes())
        cmd = [str(self._tx_file_tool), "--args", self.device_args(), "--file", str(path), "--type", "float",
               "--rate", f"{fs:.0f}", "--freq", f"{rf_hz:.0f}", "--gain", f"{gain_db:g}",
               "--ant", str(self.cfg.get("tx_antenna", "TX/RX")), "--channel", str(self.cfg.get("tx_channel", 0)),
               "--repeat"]
        subdev = self.cfg.get("tx_subdev", self.default_tx_subdev)
        cmd += ["--subdev", str(subdev)] if subdev else []
        for attempt in range(CLAIM_RETRIES + 1):
            try:
                self._start_tx(cmd, ready="Actual TX Gain", settle_s=2.0)
                break
            except DeviceError as exc:
                if CLAIM_CLASH not in str(exc) or attempt == CLAIM_RETRIES:
                    raise
                time.sleep(CLAIM_WAIT_S)
        self._tx_watchdog = threading.Timer(self.max_tx_seconds, self.tx_off)
        self._tx_watchdog.daemon = True
        self._tx_watchdog.start()

    def _start_tx(self, cmd: list[str], ready: str = "Ctrl + C", settle_s: float = 0.5) -> None:
        self._tx_log.clear()
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
        self._tx_proc = proc
        def read() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._tx_log.append(line.rstrip("\n"))
                if marker in line:
                    started.set()
            started.set()   # the tool ended: wake the waiter, which then sees it is no longer running
        marker, started = ready, threading.Event()
        threading.Thread(target=read, daemon=True, name=f"{self.id}-tx-log").start()
        started.wait(TX_START_TIMEOUT_S)
        if proc.poll() is not None or not any(marker in l for l in self._tx_log):
            text = " | ".join(self._tx_log)[-400:]
            self.tx_off()
            raise DeviceError(f"{self.id}: the transmitter did not start: {text}")
        time.sleep(settle_s)   # let the stream settle before anyone measures it
        if proc.poll() is not None:
            text = " | ".join(self._tx_log)[-400:]
            self.tx_off()
            raise DeviceError(f"{self.id}: the transmitter stopped at once: {text}")

    def tx_off(self) -> dict[str, Any]:
        """Stop the tone. Safe to call at any time. Returns what the tool reported (underflows)."""
        if self.backend == "sim":
            if self.world is not None:
                self.world.tones.pop(f"{self.id}:{self.tx_port}", None)
            return {"underflows": 0}
        dog, self._tx_watchdog = self._tx_watchdog, None
        if dog is not None and dog is not threading.current_thread():
            dog.cancel()
        proc, self._tx_proc = self._tx_proc, None
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGINT)   # the tool's own clean stop: it sends end-of-burst
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        return {"underflows": sum(l.count("U") for l in self._tx_log if l.strip() and set(l.strip()) <= {"U"})}

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
            data, ended = self._sim_capture(center_hz, sample_rate, samples, gain_db), time.time()
        else:
            for attempt in range(CLAIM_RETRIES + 1):
                try:
                    data, ended = self._receive(center_hz, sample_rate, samples, gain_db, seconds)
                    break
                except DeviceError as exc:
                    if CLAIM_CLASH not in str(exc) or attempt == CLAIM_RETRIES:
                        raise
                    time.sleep(CLAIM_WAIT_S)
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

    def _sim_capture(self, center_hz: float, fs: float, n: int, gain_db: float) -> bytes:
        """Noise, plus every simulated tone that reaches this receiver through a [wiring] path."""
        rng = self.world.rng if self.world is not None else random.Random(1)
        sim = self.cfg.get("sim") or {}
        dbfs = float(sim.get("noise_dbfs", SIM_NOISE_DBFS_AT_0DB + gain_db))
        sigma = 10 ** (dbfs / 20) / math.sqrt(2)
        my_ppm = float(sim.get("xo_ppm", 0.0))
        tones = []
        if self.world is not None:
            for src, t in list(self.world.tones.items()):
                loss = self.world.path_loss_db(src, f"{self.id}:{self.rx_port}", default=math.inf)
                if math.isinf(loss):
                    continue
                amp = 10 ** ((t["dbm"] - loss + gain_db + SIM_RX_K_DB) / 20)
                f = t["rf_hz"] * (1 + t["ppm"] * 1e-6) - center_hz * (1 + my_ppm * 1e-6)
                if abs(f) < fs / 2:
                    iq = t.get("iq")
                    if iq is not None and float(t["fs"]) != fs:
                        continue   # a different sample rate: not modelled, the receiver sees noise only
                    tones.append((amp, 2 * math.pi * f / fs, rng.uniform(0, 2 * math.pi), iq,
                                  rng.randrange(len(iq)) if iq else 0))
        out = []
        for k in range(n):
            v = complex(rng.gauss(0, sigma), rng.gauss(0, sigma))
            for amp, w, ph, iq, start in tones:
                rot = cmath.exp(1j * (w * k + ph))
                v += amp * rot * (iq[(start + k) % len(iq)] if iq is not None else 1.0)
            out.append(v)
        if self.datatype == "cf32_le":
            return array("f", (c for v in out for c in (v.real, v.imag))).tobytes()
        return array("h", (max(-32768, min(32767, round(c * 32767))) for v in out for c in (v.real, v.imag))).tobytes()


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
    default_tx_port = "trx-a"
    default_tx_subdev = "A:0"     # TX on side A, port TRX-A

    @property
    def rx_port(self) -> str:
        return str(self.cfg.get("rx_port", "rx2-b" if int(self.cfg.get("channel", 0)) == 1 else "rx2-a"))

    def device_args(self) -> str:
        addr = self.cfg.get("addr")
        if not addr:
            raise DeviceError(f"{self.id}: lab.toml needs addr, the E310's network address")
        return f"type=e3xx,addr={addr}"

    def _parse_probe(self, text: str) -> dict[str, Any]:
        mpm, fpga = self._field(text, "MPM Version"), self._field(text, "FPGA Version")
        return {"serial": self._field(text, "serial"), "firmware": f"mpm {mpm}, fpga {fpga}",
                "image": self._field(text, "mender_artifact")}
