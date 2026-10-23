"""Helpers for the LoRa hardware suites: the bench (two modems and an RTL-SDR on cables) and the
encrypted link over it (oasa-link + `oasa-radio bridge --radio serial` on each side).

Both run unchanged on the simulated bench and on the real one.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from .airtime import airtime_s
from .devices import DeviceError, SerialRadio, Unavailable
from .hw import Hardware, WiringRefused
from .linkharness import HOST, Bins, Identity, LinkProc, MissingBinaries, find_bins, make_identity, redact, trust
from .testing import Endpoint, free_udp_port, wait_for

RTL_RATE = 250_000.0   # samples per second; LoRa at 125 kHz fits, and a minute of cu8 is 30 MB
RTL_GAIN_DB = 20.0


def find_radio_bin(lab=None) -> Path:
    d = os.environ.get("OASA_RADIO_BIN_DIR")
    if not d:
        raise MissingBinaries("OASA_RADIO_BIN_DIR is not set (folder with oasa-radio)")
    p = Path(d) / "oasa-radio"
    if not (p.is_file() and os.access(p, os.X_OK)):
        raise MissingBinaries(f"oasa-radio not found or not executable in OASA_RADIO_BIN_DIR={d}")
    if lab is not None:
        lab.binary(p)
    return p


def lora_model(modem: SerialRadio) -> dict[str, Any]:
    """The modem's declared LoRa settings, in the form the airtime model and the manifest use."""
    return {k: v for k, v in modem.airtime_settings().items() if k.startswith("lora_") or k == "airtime"}


def frame_airtime(model: dict[str, Any], nbytes: int) -> float:
    return airtime_s(model, nbytes)


class LoraBench:
    """Two modems and an RTL-SDR, wired per the suite's wiring. Air captures are registered for both modems."""

    def __init__(self, lab, hw: Hardware, capture_air: bool = True):
        try:
            hw.use_wiring()
            ids = hw.inv.ids_of("serial-radio")
            self.a, self.b = hw.open(ids[0]), hw.open(ids[1])
            self.rtl = hw.first("rtlsdr")
        except WiringRefused as exc:
            pytest.skip(str(exc))
        except Unavailable as exc:
            pytest.skip(str(exc))
        self.lab, self.hw = lab, hw
        self.model = lora_model(self.a)
        if lora_model(self.b) != self.model:
            raise DeviceError("the two modems are declared with different radio settings; they cannot hear each other")
        self.center_hz = float(self.a.radio.get("freq_mhz", 915.0)) * 1e6
        self.caps: list[str] = []
        if capture_air:
            self.caps = [lab.air_capture(self.a.link, self.a.id), lab.air_capture(self.b.link, self.b.id)]
        self._recording = False

    def start_recording(self) -> None:
        self.rtl.start_recording("lora", self.center_hz, RTL_RATE, RTL_GAIN_DB)
        self._recording = True

    def finish_recording(self, name: str = "exchange", purpose: str = "lora-exchange") -> dict[str, Any]:
        """Stop the SDR and write the recording as SigMF, with a request to line its bursts up with the air captures."""
        rec = self.rtl.stop_recording()
        self._recording = False
        return self.lab.sigmf(
            name, rec.path, datatype=rec.datatype, sample_rate=rec.sample_rate, center_hz=rec.center_hz,
            start_time=rec.start_time, device=self.rtl.id, purpose=purpose,
            description=f"RTL-SDR on the cable tap while the modems exchanged packets ({purpose})",
            burst_check={"captures": list(self.caps), "model": self.model, "overhead_bytes": 0})

    def settle(self, quiet: float = 1.0) -> None:
        for m in (self.a, self.b):
            m.link.wait_idle(quiet, 60.0)


class BridgeProc:
    """One `oasa-radio bridge --radio serial` process. Its JSON-lines output goes to a log in the bundle."""

    def __init__(self, exe: Path, device: str, *, baud: int, mtu: int, listen: tuple[str, int],
                 send: tuple[str, int], log_path: Path):
        self.cmd = [str(exe), "bridge", "--radio", "serial", "--device", device, "--baud", str(baud),
                    "--mtu", str(mtu), "--udp-listen", f"{listen[0]}:{listen[1]}", "--udp-send", f"{send[0]}:{send[1]}"]
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     stdin=subprocess.DEVNULL, bufsize=1)
        self._t = threading.Thread(target=self._read, args=(log_path,), daemon=True)
        self._t.start()

    def _read(self, log_path: Path) -> None:
        assert self.proc.stdout
        with open(log_path, "w", encoding="utf-8") as out:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = redact(json.loads(line))
                    if not isinstance(rec, dict):
                        raise ValueError
                except ValueError:
                    rec = {"event": "raw_output", "line": line[:300]}
                out.write(json.dumps(rec, separators=(",", ":")) + "\n")
                out.flush()
                with self._lock:
                    self.events.append(rec)

    def seen(self, event: str) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e.get("event") == event]

    def stop(self) -> dict | None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(8)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)
        self._t.join(3)
        st = self.seen("stats")
        return st[-1] if st else None


@dataclass
class Apps:
    a: Endpoint
    b: Endpoint


class LoraLinkRig:
    """oasa-link on both sides, each through a bridge to a modem (through the modem's tap).

        app A -> oasa-link A -> bridge A -> pty -> tap -> modem A ~~ air ~~ modem B -> tap -> pty -> bridge B -> oasa-link B -> app B
    """

    def __init__(self, lab, bench: LoraBench, bins: Bins, radio_exe: Path, base: Path, mtu: int = 252, baud: int = 115200):
        self.lab, self.bench, self.mtu = lab, bench, mtu
        self.alice, self.bob = make_identity(bins, base, "alice"), make_identity(bins, base, "bob")
        trust(bins, self.alice, self.bob)
        trust(bins, self.bob, self.alice)
        self.app_a, self.app_b = Endpoint(), Endpoint()
        ports = {k: (HOST, free_udp_port()) for k in ("a_bind", "b_bind", "a_plain", "b_plain", "a_bridge", "b_bridge")}
        self.p = ports
        pty_a, pty_b = bench.a.link.attach_pty(), bench.b.link.attach_pty()
        self.bridge_a = BridgeProc(radio_exe, pty_a, baud=baud, mtu=mtu, listen=ports["a_bridge"], send=ports["a_bind"],
                                   log_path=lab.file("bridge-a.jsonl"))
        self.bridge_b = BridgeProc(radio_exe, pty_b, baud=baud, mtu=mtu, listen=ports["b_bridge"], send=ports["b_bind"],
                                   log_path=lab.file("bridge-b.jsonl"))
        time.sleep(0.5)  # let both bridges open their ports
        self.link_b = LinkProc(bins, self.bob, "alice", bind=ports["b_bind"], remote=ports["b_bridge"], initiate=False,
                               plain_listen=ports["b_plain"], plain_send=self.app_b.addr,
                               log_path=lab.file("link-bob.jsonl"), mtu=mtu)
        self.t_start_mono, self.t_start_wall = time.monotonic(), time.time()
        self.link_a = LinkProc(bins, self.alice, "bob", bind=ports["a_bind"], remote=ports["a_bridge"], initiate=True,
                               plain_listen=ports["a_plain"], plain_send=self.app_a.addr,
                               log_path=lab.file("link-alice.jsonl"), mtu=mtu)
        self.stats: dict[str, Any] = {}

    def wait_established(self, timeout: float = 60.0) -> tuple[float, float]:
        """Wait for the initiator's session. Returns (monotonic, wall-clock) time it was seen."""
        if not self.link_a.wait_event("session_established", timeout):
            raise AssertionError("the link did not establish a session over LoRa in time; see logs/ in the bundle")
        return time.monotonic(), time.time()

    def send(self, direction: str, payloads: list[bytes]) -> tuple[float, float]:
        """Send all payloads in one burst. Returns (monotonic, wall-clock) time just before sending."""
        src, port = (self.app_a, self.p["a_plain"]) if direction == "a_to_b" else (self.app_b, self.p["b_plain"])
        t = (time.monotonic(), time.time())
        src.send_paced(payloads, port, batch=len(payloads))
        return t

    def receiver(self, direction: str) -> Endpoint:
        return self.app_b if direction == "a_to_b" else self.app_a

    def finish(self) -> None:
        self.bench.settle(1.0)
        self.stats["alice"] = self.link_a.stop()
        self.stats["bob"] = self.link_b.stop()
        self.stats["bridge_a"] = self.bridge_a.stop()
        self.stats["bridge_b"] = self.bridge_b.stop()

    def close(self) -> None:
        for p in (self.link_a, self.link_b):
            p.stop()
        for p in (self.bridge_a, self.bridge_b):
            p.stop()
        self.app_a.close()
        self.app_b.close()

    def __enter__(self) -> "LoraLinkRig":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def frames_between(bench: LoraBench, t0: float, t1: float) -> list[tuple[float, int]]:
    """(arrival time, size) of every frame either modem delivered to its host in [t0, t1]."""
    out = []
    for m in (bench.a, bench.b):
        out += [(f.ts, len(f.data)) for f in m.link.rx_packets() if t0 <= f.ts <= t1]
    return sorted(out)
