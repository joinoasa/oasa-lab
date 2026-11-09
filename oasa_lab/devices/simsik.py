"""Simulated SiK telemetry radios on the simulated bench.

Each `SimSikPort` looks like the radio's serial port: bytes in, bytes out. It speaks the SiK AT protocol
as the firmware does (`+++` with 1 s guards, echo, CR LF, `ATI`, `ATI5`, `ATSn=v`, `ATSn?`, `AT&W`, `ATZ`,
`ATO`, `RT...` run by the linked radio without authentication; sources in sikat.py and oasa-radio
docs/sik.md). In data mode it is transparent: bytes in go on the air, bytes heard come out.

`SimSikAir` joins the radios. It is real-time and follows a radio profile (`sik-64k`):

- the serial port clocks bytes in and out at SERIAL_SPEED (10 bits a byte) in both radios;
- one packet on the air at a time (half-duplex), each taking the profile's airtime, at most the profile's
  payload (250 bytes with ECC off);
- two radios hear each other only when their link settings match (NETID, AIR_SPEED, ECC, band, ...),
  and a radio in command mode or booting hears nothing.

Simplifications (the hardware run shows the real thing): no TDM windows or frequency hopping, no
collisions, no loss unless asked. A radio sends what its serial buffer holds when the line has been quiet
for two byte times, when it holds a full packet, or when the oldest byte has waited 20 ms. Real SiK sends
whatever is buffered when its TDM window comes (packet.c, MAVLINK=0).
"""

from __future__ import annotations

import heapq
import random
import threading
import time
from collections import deque
from typing import Any

from ..airtime import airtime_s
from .sikat import DEFAULTS, LINK_KEYS, NAMES, SERIAL_BAUD

SIM_VERSION = "SiK 2.2 on SIM-HM-TRP"
GUARD_S = 1.0
LINE_MAX = 16
BOOT_S = 1.0          # est.
OLDEST_WAIT_S = 0.02


class SimSikPort:
    def __init__(self, name: str, air: "SimSikAir", settings: dict[str, int] | None = None):
        self.name, self.air = name, air
        self.params = dict(DEFAULTS)
        self.params.update(settings or {})
        self.saved = dict(self.params)
        self.at_mode = False
        self.line = bytearray()
        self.last_in: float | None = None
        self.plus = 0
        self.enable_at: float | None = None
        self.booting_until = 0.0
        self.reboots = 0
        self._in: deque[tuple[float, int]] = deque()   # (time the byte has fully arrived, byte)
        self._in_t = 0.0
        self.txbuf = bytearray()
        self.tx_first = self.tx_last = 0.0
        self.cmdq: deque[tuple[str, str]] = deque()      # ("cmd" | "reply", text) waiting for the air
        self._out: deque[tuple[float, bytes]] = deque()  # (time it has left the radio, bytes)
        self._out_t = 0.0
        self._cv = threading.Condition()
        self.closed = False
        air.attach(self)

    # --- the serial port, as the lab sees it ---------------------------------------------------
    def byte_s(self) -> float:
        return 10.0 / SERIAL_BAUD.get(self.params["SERIAL_SPEED"], 57_600)

    def write(self, data: bytes) -> None:
        with self.air.lock:
            t = max(time.time(), self._in_t)
            for c in data:
                t += self.byte_s()
                self._in.append((t, c))
            self._in_t = t

    def read(self, timeout: float = 0.05) -> bytes:
        end = time.time() + timeout
        with self._cv:
            while True:
                now = time.time()
                out = bytearray()
                while self._out and self._out[0][0] <= now:
                    out += self._out.popleft()[1]
                if out or self.closed or now >= end:
                    return bytes(out)
                nxt = self._out[0][0] if self._out else end
                self._cv.wait(max(0.0005, min(end, nxt) - now))

    def close(self) -> None:
        with self._cv:
            self.closed = True
            self._cv.notify_all()

    # --- inside the radio (the air's lock is held) --------------------------------------------
    def emit(self, data: bytes, now: float) -> None:
        """Bytes from the radio to the host, clocked out at the serial speed."""
        t = max(now, self._out_t) + len(data) * self.byte_s()
        self._out_t = t
        with self._cv:
            self._out.append((t, bytes(data)))
            self._cv.notify_all()

    def print(self, text: str, now: float) -> None:
        self.emit(text.replace("\n", "\r\n").encode("ascii"), now)

    def booting(self, now: float) -> bool:
        return now < self.booting_until

    def link_key(self) -> tuple:
        return tuple(self.params[k] for k in LINK_KEYS)

    def listening(self, now: float) -> bool:
        return not self.at_mode and not self.booting(now)

    def step(self, now: float) -> None:
        while self._in and self._in[0][0] <= now:
            t, c = self._in.popleft()
            self._byte(c, t)
        if self.enable_at is not None and now - self.enable_at >= GUARD_S:
            self.enable_at = None
            self.at_mode = True
            self.line.clear()
            self.print("OK\n", now)

    def _byte(self, c: int, t: float) -> None:
        if self.booting(t):
            return
        if self.at_mode:
            self._at_input(c, t)
            return
        # the +++ detector (at_plus_detector)
        if self.enable_at is not None:
            self.enable_at, self.plus = None, 0
        elif c == 0x2B:
            if self.plus or self.last_in is None or t - self.last_in >= GUARD_S:
                self.plus += 1
            if self.plus == 3:
                self.plus, self.enable_at = 0, t
        else:
            self.plus = 0
        self.last_in = t
        # every byte in data mode also goes on the air (serial.c)
        if not self.txbuf:
            self.tx_first = t
        self.tx_last = t
        self.txbuf.append(c)

    def _at_input(self, c: int, t: float) -> None:
        if c == 0x0D:
            self.emit(b"\r\n", t)
            line = self.line.decode("ascii", "replace")
            self.line.clear()
            if line.startswith("RT"):
                self.cmdq.append(("cmd", "AT" + line[2:]))
            elif line.startswith("AT"):
                self.print(self.run(line, t), t)
            return
        if 0x20 <= c < 0x7F:
            if len(self.line) >= LINE_MAX:
                self.at_mode = False
                self.line.clear()
                return
            u = bytes([c]).upper()
            self.line += u
            self.emit(u, t)

    def run(self, cmd: str, now: float) -> str:
        rest = cmd[2:]
        if rest == "":
            return "OK\n"
        if rest in ("I", "I0"):
            return SIM_VERSION + "\n"
        if rest == "I5":
            return "".join(f"S{i}:{n}={self.params[n]}\n" for i, n in enumerate(NAMES))
        if rest == "O":
            self.at_mode = False
            return ""
        if rest == "Z":
            self.params = dict(self.saved)
            self.at_mode = False
            self.booting_until = now + BOOT_S
            self.reboots += 1
            self.txbuf.clear()
            return ""
        if rest == "&W":
            self.saved = dict(self.params)
            return "OK\n"
        if rest.startswith("S"):
            body = rest[1:]
            try:
                if body.endswith("?"):
                    return f"{self.params[NAMES[int(body[:-1])]]}\n"
                reg, val = body.split("=", 1)
                n, v = int(reg), int(val)
                limits = {5: 1, 6: 2, 7: 1, 15: 131}
                if 0 < n < len(NAMES) and 0 <= v <= limits.get(n, 0xFFFFFFFF):
                    self.params[NAMES[n]] = v
                    return "OK\n"
            except (ValueError, IndexError):
                pass
        return "ERROR\n"


class SimSikAir:
    def __init__(self, settings: dict[str, Any], seed: int = 0, loss: float = 0.0,
                 latency_s: float = 0.002, turnaround_s: float = 0.001):
        self.settings, self.loss, self.latency_s, self.turnaround_s = settings, loss, latency_s, turnaround_s
        self.payload = int(settings.get("mtu", 250))
        self.rng = random.Random(seed ^ 0x51C)
        self.ports: list[SimSikPort] = []
        self.bursts: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self._busy_until = 0.0
        self._due: list[tuple[float, int, SimSikPort, str, Any]] = []
        self._n = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sim-sik-air")
        self._thread.start()

    def attach(self, port: SimSikPort) -> None:
        with self.lock:
            self.ports.append(port)

    def _ready(self, p: SimSikPort, now: float) -> bool:
        if p.cmdq:
            return True
        if not p.txbuf:
            return False
        quiet = now - p.tx_last >= 2 * p.byte_s()
        return len(p.txbuf) >= self.payload or quiet or now - p.tx_first >= OLDEST_WAIT_S

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            with self.lock:
                for p in self.ports:
                    p.step(now)
                if now >= self._busy_until:
                    waiting = [p for p in self.ports if not p.booting(now) and self._ready(p, now)]
                    if waiting:
                        src = min(waiting, key=lambda p: (not p.cmdq, p.tx_first))
                        if src.cmdq:
                            kind, text = src.cmdq.popleft()
                            nbytes = len(text)
                            what: Any = (kind, text)
                        else:
                            data = bytes(src.txbuf[:self.payload])
                            del src.txbuf[:self.payload]
                            if src.txbuf:
                                src.tx_first = now
                            kind, nbytes, what = "data", len(data), data
                        dur = airtime_s(self.settings, nbytes)
                        self._busy_until = now + dur + self.turnaround_s
                        lost = self.rng.random() < self.loss
                        heard = [p for p in self.ports if p is not src and p.link_key() == src.link_key()]
                        self.bursts.append({"t_start": now, "seconds": dur, "bytes": nbytes, "src": src.name,
                                            "kind": kind, "delivered": bool(heard) and not lost})
                        if heard and not lost:
                            for p in heard:
                                self._n += 1
                                heapq.heappush(self._due, (now + dur + self.latency_s, self._n, p, kind, (src, what)))
                while self._due and self._due[0][0] <= now:
                    _, _, p, kind, (src, what) = heapq.heappop(self._due)
                    if p.link_key() != src.link_key() or p.booting(now):
                        continue
                    if kind == "data":
                        if p.listening(now):
                            p.emit(what, now)
                    elif what[0] == "cmd":               # a remote AT command: run it, send the answer back
                        reply = p.run(what[1], now)
                        if reply:
                            p.cmdq.append(("reply", reply))
                    else:                                # the answer to our RT command
                        p.print(what[1], now)
            time.sleep(0.0003)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)
        for p in self.ports:
            p.close()
