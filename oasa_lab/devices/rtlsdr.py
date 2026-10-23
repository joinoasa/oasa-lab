"""RTL-SDR Blog V4 (receive only). Raw IQ goes to disk as 8-bit unsigned pairs (SigMF `cu8`).

Real backend: the `rtl_sdr` and `rtl_test` programs from the RTL-SDR Blog driver fork, run as
separate processes (GPL-2.0, so never linked or shipped; the V4 needs the Blog fork). We chose
the command line over a Python wrapper: pyrtlsdr is GPL-3.0 and a wrapper adds nothing here.
"""

from __future__ import annotations

import cmath
import math
import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..iq import IQ
from .base import Device, DeviceError, Unavailable
from .sim import SimWorld

SIM_RTL_K_DB = 20.0  # simulated receive chain: dBFS = dBm + gain + K (arbitrary but fixed)


@dataclass
class Recording:
    path: Path
    sample_rate: float
    center_hz: float
    start_time: float     # wall-clock seconds of the first sample (to a few tens of ms)
    seconds: float
    datatype: str = "cu8"

    @property
    def data(self) -> bytes:
        return self.path.read_bytes()


class RtlSdr(Device):
    kind = "rtlsdr"

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._exe: str | None = None
        self._proc: subprocess.Popen | None = None
        self._rec: dict[str, Any] | None = None
        self._tmp = Path(tempfile.mkdtemp(prefix="oasa-rtl-"))
        self._facts: dict[str, Any] = {}

    @property
    def selector(self) -> str:
        return str(self.cfg.get("serial") or self.cfg.get("index", 0))

    # --- real ---------------------------------------------------------------------------------
    def _open(self) -> None:
        if self.backend == "sim":
            return
        self._exe = shutil.which("rtl_sdr")
        test = shutil.which("rtl_test")
        if not self._exe or not test:
            raise Unavailable("rtl_sdr / rtl_test not found (install the RTL-SDR Blog driver fork)")
        text = self._probe(test)
        m = re.search(r"\d+:\s*([^,\n]+),\s*([^,\n]+),\s*SN:\s*(\S+)", text)
        if not m:
            raise DeviceError(f"{self.id}: no RTL-SDR found for {self.selector!r}: {text.strip()[:200]}")
        want = self.cfg.get("serial")
        if want and m.group(3) != str(want):
            raise DeviceError(f"{self.id}: found serial {m.group(3)}, lab.toml says {want}")
        tuner = re.search(r"Found (.+?) tuner", text)
        self._facts = {"vendor": m.group(1).strip(), "product": m.group(2).strip(), "serial": m.group(3),
                       "tuner": tuner.group(1) if tuner else None, "blog_v4": "Blog V4" in text}

    def _probe(self, test_exe: str, timeout: float = 8.0) -> str:
        """Start rtl_test, read its start-up lines (device list, tuner), then stop it: it never ends by itself."""
        proc = subprocess.Popen([test_exe, "-d", self.selector, "-t"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        text = ""
        end = time.time() + timeout
        try:
            while time.time() < end:
                ready, _, _ = select.select([proc.stdout], [], [], 0.1)
                if ready:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    text += chunk.decode("utf-8", "replace")
                    if re.search(r"Found .+ tuner|No supported devices|usb_claim_interface error|Failed to open", text):
                        end = min(end, time.time() + 0.5)      # a moment more: the Blog driver prints its V4 line nearby
                elif proc.poll() is not None:
                    break
        finally:
            proc.kill()
            proc.wait(5)
            proc.stdout.close()
        return text

    def _info(self) -> dict[str, Any]:
        if self.backend == "sim":
            return {"model": "RTL-SDR Blog V4 (simulated)", "serial": self.cfg.get("serial", "SIM-RTL"),
                    "firmware": "n/a", "tuner": "Rafael Micro R828D", "tcxo_ppm_spec": 1.0,
                    "known_ppm": self.cfg.get("known_ppm")}
        f = self._facts
        return {"model": f"{f['vendor']} {f['product']}".strip(), "serial": f["serial"], "firmware": "n/a",
                "tuner": f["tuner"], "blog_v4": f["blog_v4"], "tcxo_ppm_spec": 1.0,
                "known_ppm": self.cfg.get("known_ppm")}

    def _close(self) -> None:
        if self._proc is not None:
            self.stop_recording()
        shutil.rmtree(self._tmp, ignore_errors=True)

    # --- recording ---------------------------------------------------------------------------------
    def capture(self, center_hz: float, sample_rate: float, samples: int, gain_db: float = 20.0) -> IQ:
        """A short capture, returned in memory."""
        if self.backend == "sim":
            return self._sim_tone_capture(center_hz, sample_rate, samples, gain_db)
        out = self._tmp / "short.cu8"
        cmd = [self._exe, "-d", self.selector, "-f", str(int(center_hz)), "-s", str(int(sample_rate)),
               "-g", str(gain_db), "-n", str(int(samples)), str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30 + samples / sample_rate,
                           stdin=subprocess.DEVNULL)
        if r.returncode != 0 or not out.is_file():
            raise DeviceError(f"{self.id}: rtl_sdr failed: {(r.stderr or r.stdout).strip()[-200:]}")
        data = out.read_bytes()
        out.unlink()
        return IQ(data, "cu8", float(sample_rate), float(center_hz))

    def start_recording(self, name: str, center_hz: float, sample_rate: float, gain_db: float = 20.0) -> None:
        if self._rec is not None:
            raise DeviceError(f"{self.id}: already recording")
        path = self._tmp / f"{name}.cu8"
        if self.backend == "sim":
            self._rec = {"path": path, "fs": float(sample_rate), "center": float(center_hz), "t0": time.time()}
            return
        cmd = [self._exe, "-d", self.selector, "-f", str(int(center_hz)), "-s", str(int(sample_rate)),
               "-g", str(gain_db), str(path)]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                      stdin=subprocess.DEVNULL)
        t_seen = None
        end = time.time() + 15
        while time.time() < end:  # wait for the first block; its age tells when sampling began
            if self._proc.poll() is not None:
                raise DeviceError(f"{self.id}: rtl_sdr stopped at once: {(self._proc.stderr.read() or '')[-200:]}")
            if path.exists() and path.stat().st_size > 0:
                t_seen, first = time.time(), path.stat().st_size
                break
            time.sleep(0.005)
        if t_seen is None:
            self._proc.kill()
            raise DeviceError(f"{self.id}: rtl_sdr wrote nothing in 15 s")
        t0 = t_seen - (first / 2) / sample_rate
        self._rec = {"path": path, "fs": float(sample_rate), "center": float(center_hz), "t0": t0}

    def stop_recording(self) -> Recording:
        rec, self._rec = self._rec, None
        if rec is None:
            raise DeviceError(f"{self.id}: not recording")
        if self.backend == "sim":
            return self._sim_finish(rec)
        proc, self._proc = self._proc, None
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
        size = rec["path"].stat().st_size
        size -= size % 2
        with open(rec["path"], "r+b") as fh:
            fh.truncate(size)
        return Recording(rec["path"], rec["fs"], rec["center"], rec["t0"], size / 2 / rec["fs"])

    # --- simulation ---------------------------------------------------------------------------------
    @property
    def _ppm(self) -> float:
        return float((self.cfg.get("sim") or {}).get("xo_ppm", -0.4))

    def _sim_tone_capture(self, center_hz: float, fs: float, n: int, gain_db: float) -> IQ:
        world = self.world
        if not isinstance(world, SimWorld):
            raise Unavailable(f"{self.id}: the simulated backend needs a SimWorld")
        rng = world.rng
        lo = center_hz * (1 + self._ppm * 1e-6)
        sigma = 10 ** (-47 / 20) / math.sqrt(2)
        tones = []
        for src, t in list(world.tones.items()):
            rf = t["rf_hz"] * (1 + t["ppm"] * 1e-6)
            if abs(rf - lo) < fs / 2 * 0.9:
                dbm = t["dbm"] - world.path_loss_db(src, self.id) - world.extra_loss_db.get(self.id, 0.0)
                tones.append((rf - lo, 10 ** ((dbm + gain_db + SIM_RTL_K_DB) / 20), rng.uniform(0, 2 * math.pi)))
        out = bytearray()
        for k in range(n):
            z = complex(rng.gauss(0, sigma), rng.gauss(0, sigma))
            for off, amp, ph in tones:
                z += amp * cmath.exp(1j * (2 * math.pi * off * k / fs + ph))
            out.append(max(0, min(255, round(127.5 + 127.5 * z.real))))
            out.append(max(0, min(255, round(127.5 + 127.5 * z.imag))))
        return IQ(bytes(out), "cu8", float(fs), float(center_hz))

    def _sim_finish(self, rec: dict[str, Any]) -> Recording:
        """Noise, plus a burst of stronger noise wherever the simulated air carried a packet."""
        world = self.world
        if not isinstance(world, SimWorld):
            raise Unavailable(f"{self.id}: the simulated backend needs a SimWorld")
        fs, t0 = rec["fs"], rec["t0"]
        seconds = time.time() - t0
        n = int(seconds * fs)
        rng = world.rng
        quiet = bytes([125 + i % 6 for i in range(256)])                    # |2b-255| is 1 to 5
        loud = bytes([max(0, min(255, 67 + round(i * 121 / 255))) for i in range(256)])  # about +-60 counts
        buf = bytearray(rng.randbytes(2 * n).translate(quiet))
        bursts = world.air.bursts if world.air is not None else []
        for b in bursts:
            i0 = int((b["t_start"] - t0) * fs)
            i1 = i0 + int(b["seconds"] * fs)
            i0, i1 = max(0, i0), min(n, i1)
            if i1 > i0:
                buf[2 * i0:2 * i1] = rng.randbytes(2 * (i1 - i0)).translate(loud)
        rec["path"].write_bytes(bytes(buf))
        # The claimed start time is off by a few ms, like a real one, so the checker's alignment has work to do.
        return Recording(rec["path"], fs, rec["center"], t0 + rng.uniform(-0.015, 0.015), n / fs)
