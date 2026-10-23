"""The simulated bench: a shared world that the simulated devices live in.

`SimWorld` holds what devices must agree on: the cable losses from the inventory, tones that a
simulated Pluto is sending, and one simulated LoRa air (`SimAir`) joining the simulated modems.
It is real-time (a LoRa packet takes its real airtime), so timing checks mean something.

The simulated air is a perfect half-duplex medium: one packet at a time, no collisions. Real
LoRa radios can collide; only the hardware run shows that.
"""

from __future__ import annotations

import heapq
import random
import threading
import time
from collections import deque
from typing import Any

from ..airtime import airtime_s
from ..profiles import profile_settings
from .framing import FrameDecoder, encode_frame


class SimWorld:
    def __init__(self, seed: int = 0, wiring: dict[str, Any] | None = None):
        self.seed = seed
        self.rng = random.Random(seed)
        self.paths: list[dict[str, Any]] = [p for w in (wiring or {}).values() for p in w.get("paths", [])]
        self.air: SimAir | None = None
        self.tones: dict[str, dict[str, Any]] = {}      # "pluto0:tx1" -> {"rf_hz", "dbm", "ppm"}
        self.extra_loss_db: dict[str, float] = {}       # fault injection: "pluto0:rx2" -> dB
        self._lock = threading.Lock()

    def path_loss_db(self, src: str, dst: str, default: float = 60.0) -> float:
        for p in self.paths:
            if p["from"] == src and p["to"] == dst:
                return float(p["loss_db"])
        return default

    def get_air(self, radio: dict[str, Any]) -> "SimAir":
        with self._lock:
            if self.air is None:
                self.air = SimAir(radio_airtime_settings(radio), seed=self.seed)
            return self.air

    def close(self) -> None:
        if self.air is not None:
            self.air.stop()


def radio_airtime_settings(radio: dict[str, Any]) -> dict[str, Any]:
    """The LoRa settings of a modem as the airtime model wants them (profile first, then overrides)."""
    s = profile_settings(str(radio.get("profile", "lora-sf7-bw125")))
    for k, v in radio.items():
        if k.startswith("lora_"):
            s[k] = v
    return s


def radio_key(radio: dict[str, Any]) -> tuple:
    """Two modems hear each other only when all of these match."""
    s = radio_airtime_settings(radio)
    return (float(radio.get("freq_mhz", 915.0)), s["lora_sf"], s["lora_bw_hz"], s["lora_cr"],
            s.get("lora_preamble", 8), int(radio.get("sync_word", 0x12)))


class SimModemPort:
    """Looks like a serial port to the lab: write() frames in, read() frames out."""

    def __init__(self, name: str, air: "SimAir", radio: dict[str, Any], mtu: int = 252):
        self.name, self.air, self.radio, self.mtu = name, air, radio, mtu
        self.key = radio_key(radio)
        self._dec = FrameDecoder(mtu)
        self._out = bytearray()
        self._cv = threading.Condition()
        self.closed = False
        self.txq: deque[tuple[float, bytes]] = deque()
        self.queue_drops = 0
        air.attach(self)

    # --- what the lab calls --------------------------------------------------------------
    def write(self, data: bytes) -> None:
        for pkt in self._dec.feed(data):
            self.air.submit(self, pkt)

    def read(self, timeout: float = 0.05) -> bytes:
        with self._cv:
            if not self._out and not self.closed:
                self._cv.wait(timeout)
            data, self._out = bytes(self._out), bytearray()
            return data

    def close(self) -> None:
        with self._cv:
            self.closed = True
            self._cv.notify_all()

    # --- what the air calls --------------------------------------------------------------
    def deliver(self, packet: bytes) -> None:
        with self._cv:
            self._out += encode_frame(packet, self.mtu)
            self._cv.notify_all()


class SimAir:
    def __init__(self, settings: dict[str, Any], seed: int = 0, loss: float = 0.0,
                 latency_s: float = 0.005, queue_max: int = 64, turnaround_s: float = 0.003):
        self.settings, self.loss, self.latency_s, self.queue_max = settings, loss, latency_s, queue_max
        self.turnaround_s = turnaround_s  # a modem needs a moment between two packets; a real one is slower
        self.rng = random.Random(seed ^ 0x10A)
        self.ports: list[SimModemPort] = []
        self.bursts: list[dict[str, Any]] = []   # every transmission: wall-clock start, seconds, bytes, who
        self._busy_until = 0.0
        self._due: list[tuple[float, int, SimModemPort, bytes]] = []
        self._n = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sim-air")
        self._thread.start()

    def attach(self, port: SimModemPort) -> None:
        with self._lock:
            self.ports.append(port)

    def submit(self, port: SimModemPort, packet: bytes) -> None:
        with self._lock:
            if len(port.txq) >= self.queue_max:
                port.queue_drops += 1
                return
            port.txq.append((time.time(), packet))

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                if now >= self._busy_until:
                    waiting = [p for p in self.ports if p.txq]
                    if waiting:
                        src = min(waiting, key=lambda p: p.txq[0][0])
                        _, pkt = src.txq.popleft()
                        dur = airtime_s(self.settings, len(pkt))
                        self._busy_until = now + dur + self.turnaround_s
                        heard = [p for p in self.ports if p is not src and p.key == src.key]
                        lost = self.rng.random() < self.loss
                        self.bursts.append({"t_start": now, "seconds": dur, "bytes": len(pkt),
                                            "src": src.name, "delivered": bool(heard) and not lost})
                        if heard and not lost:
                            for p in heard:
                                self._n += 1
                                heapq.heappush(self._due, (now + dur + self.latency_s, self._n, p, pkt))
                while self._due and self._due[0][0] <= now:
                    _, _, p, pkt = heapq.heappop(self._due)
                    p.deliver(pkt)
            time.sleep(0.0005)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)
        for p in self.ports:
            p.close()
