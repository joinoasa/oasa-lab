"""L1 simulated radio channel: a UDP impairment proxy.

Endpoint A sends to the proxy's A port (`addr_a`); the proxy forwards to B. Endpoint B sends to
the proxy's B port (`addr_b`); the proxy forwards to A. So each endpoint is configured with the
proxy port as its "remote" and sees replies coming from that same port.

Repeatable from the seed: each direction has its own seeded RNG and takes the same number of
draws for every packet. Whether a packet is lost, duplicated, corrupted, delayed or replayed
depends only on the seed and the packet's place in the sequence, never on timing. Timing only
decides *when* the result is sent. Every forwarded packet is written to a pcap (see pcap.py).

Impairments, applied per packet in this order:
  mtu        packet larger than mtu is dropped (too_big)
  loss       dropped with probability `loss`
  corrupt    one random byte XORed with a random non-zero value
  delay      delay_ms, plus or minus up to jitter_ms
  reorder    with probability `reorder` the packet gets extra delay (0.5 to 1.5 x reorder_delay_ms)
  rate       rate_kbps: packets wait for the link to be free, then take size/rate to send
  duplicate  a second copy follows shortly after
  replay     with probability `replay` a packet seen earlier (last 32) is sent again, later

The rate limiter uses real time, so it changes delays but never which packets are dropped.

Airtime model (optional, see airtime.py and profiles.py). With `airtime` set, every packet takes
its on-air time, worked out from its size. Packets of one direction go out one after another;
with `half_duplex` both directions share one medium, so only one packet is on the air at a time.
A packet that arrives while the medium is busy waits in a queue of at most `max_queue` packets
(0 = no limit); a packet that finds the queue full is dropped and counted as `queue_dropped`.
`duty_cycle` (0 to 1) keeps each direction's average transmit time under that share. A lost
packet still uses the air; it is just never delivered. In the pcap, a packet's time stamp is the
moment it started to go on the air; it arrives one airtime (plus any delay) later.

Which packets are lost, duplicated, corrupted or replayed still depends only on the seed and
the packet's place in the sequence. The clock decides only *when*, and (with `max_queue`) whether
the queue had room: a queue drop happens before those decisions are counted, so the other
decisions for the same packet positions stay the same.

Profiles (`ChannelSettings.for_profile("lora-sf7-bw125", loss=0.05)`) fill in the settings of a
real radio. Settings you pass override the profile's.
"""

from __future__ import annotations

import asyncio
import collections
import json
import math
import random
import socket
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable, NamedTuple

from .airtime import MODELS, airtime_s
from .pcap import PcapWriter

DIRECTIONS = ("a_to_b", "b_to_a")
STAT_KEYS = (
    "sent", "dropped", "duplicated", "reordered", "corrupted", "replayed", "too_big",
    "unrouted", "abandoned", "queue_dropped", "forwarded",
)
HISTORY = 32
Addr = tuple[str, int]


@dataclass(frozen=True)
class ChannelSettings:
    loss: float = 0.0
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    reorder: float = 0.0
    reorder_delay_ms: float = 20.0
    duplicate: float = 0.0
    corrupt: float = 0.0
    rate_kbps: float = 0.0  # 0 = unlimited
    mtu: int = 0  # 0 = no limit
    replay: float = 0.0
    replay_delay_ms: float = 200.0
    seed: int = 0
    # --- airtime model (all off by default) ---
    profile: str = ""  # name of the profile these settings came from (a label; the numbers below rule)
    airtime: str = ""  # "", "lora", "bitrate" or "slots"; see airtime.py
    half_duplex: bool = False  # both directions share one medium
    max_queue: int = 0  # packets waiting to transmit, per direction; 0 = no limit
    duty_cycle: float = 0.0  # 0 = off; else each direction's average transmit time share (0 to 1)
    serial_bps: int = 0  # 0 = off; else each packet first crosses a serial port at this rate (8N1)
    # lora
    lora_sf: int = 0
    lora_bw_hz: int = 0
    lora_cr: int = 1  # 1..4 = coding rate 4/5..4/8
    lora_preamble: int = 8
    lora_explicit_header: bool = True
    lora_crc: bool = True
    lora_ldro: int = -1  # -1 = automatic
    # bitrate
    air_kbps: float = 0.0
    fixed_us: float = 0.0
    overhead_bytes: int = 0
    fec_ratio: float = 1.0
    symbol_us: float = 0.0
    extra_bits: int = 0
    # slots
    slot_ms: float = 0.0
    slot_payload: int = 0
    slot_payload_rev: int = 0  # B to A; 0 = same as slot_payload

    def __post_init__(self) -> None:
        for name in ("loss", "reorder", "duplicate", "corrupt", "replay", "duty_cycle"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        for name in ("delay_ms", "jitter_ms", "reorder_delay_ms", "rate_kbps", "replay_delay_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.mtu < 0 or self.mtu > 65507:
            raise ValueError("mtu must be 0 (no limit) to 65507")
        if not (isinstance(self.seed, int) and 0 <= self.seed < 2**63):
            raise ValueError("seed must be an integer from 0 to 2**63-1")
        if self.max_queue < 0 or self.serial_bps < 0:
            raise ValueError("max_queue and serial_bps must not be negative")
        if self.airtime and self.airtime not in MODELS:
            raise ValueError(f"airtime must be empty or one of {', '.join(MODELS)}")
        if not self.airtime:
            if self.half_duplex or self.max_queue or self.duty_cycle or self.serial_bps:
                raise ValueError("half_duplex, max_queue, duty_cycle and serial_bps need an airtime model")
            return
        if self.rate_kbps:
            raise ValueError("rate_kbps and an airtime model cannot be combined (use one or the other)")
        if self.airtime == "lora":
            if not 5 <= self.lora_sf <= 12:
                raise ValueError("lora_sf must be 5 to 12")
            if self.lora_bw_hz <= 0:
                raise ValueError("lora_bw_hz must be positive (for example 125000)")
            if not 1 <= self.lora_cr <= 4:
                raise ValueError("lora_cr must be 1 to 4 (coding rate 4/5 to 4/8)")
            if self.lora_preamble < 0 or self.lora_ldro not in (-1, 0, 1):
                raise ValueError("lora_preamble must not be negative; lora_ldro must be -1, 0 or 1")
        elif self.airtime == "bitrate":
            if self.air_kbps <= 0:
                raise ValueError("air_kbps must be positive")
            if min(self.fixed_us, self.symbol_us) < 0 or min(self.overhead_bytes, self.extra_bits) < 0:
                raise ValueError("fixed_us, symbol_us, overhead_bytes and extra_bits must not be negative")
            if self.fec_ratio < 1.0:
                raise ValueError("fec_ratio must be 1 or more")
        else:  # slots
            if self.slot_ms <= 0 or self.slot_payload < 1 or self.slot_payload_rev < 0:
                raise ValueError("slot_ms and slot_payload must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChannelSettings":
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown channel setting(s): {sorted(unknown)}")
        return cls(**d)

    @classmethod
    def for_profile(cls, profile: str | None, **overrides: Any) -> "ChannelSettings":
        """The settings of a named radio profile (see profiles.py), with `overrides` on top."""
        from .profiles import profile_settings

        base = profile_settings(profile) if profile else {}
        return cls(**{**base, **overrides})

    def airtime_s(self, nbytes: int, direction: str = "a_to_b") -> float:
        return airtime_s(self.to_dict(), nbytes, direction)


class Emission(NamedTuple):
    delay: float  # seconds from now until delivery (without an airtime model)
    data: bytes
    kind: str  # "primary", "duplicate", "replay", or "lost" (airtime model only: used the air, never arrives)
    offer: float = 0.0  # airtime model: seconds from now until it asks for the air (a replay waits)


class Direction:
    """The decisions for one direction. Pure logic: no sockets, no clock unless `now` is given."""

    def __init__(self, name: str, settings: ChannelSettings):
        self.name = name
        self.s = settings
        self.rng = random.Random(settings.seed * 2 + DIRECTIONS.index(name))
        self.stats = dict.fromkeys(STAT_KEYS, 0)
        self.trace: list[str] = []  # one short code per packet, see process()
        self._history: collections.deque[bytes] = collections.deque(maxlen=HISTORY)
        self._busy_until = 0.0

    def unrouted(self) -> None:
        self.stats["sent"] += 1
        self.stats["unrouted"] += 1
        self.trace.append("U")

    def process(self, data: bytes, now: float = 0.0, queue_full: bool = False) -> list[Emission]:
        """Decide what happens to one packet.

        Trace code per packet: `L` lost, `B` too big, `Q` queue full, `.` forwarded, then flags
        `c` corrupted, `r` reordered, `d` duplicated, `p` replay attached.

        `queue_full` (airtime model) is told by the caller from the clock: the packet found the
        transmit queue full. Every other decision is drawn as usual, so the random stream stays
        in step with the packet count whether or not the queue had room.
        """
        s, st, rnd = self.s, self.stats, self.rng.random
        # Fixed number of draws per packet, whatever happens, so streams stay aligned.
        u_loss, u_dup, u_cor, f_idx, f_xor = rnd(), rnd(), rnd(), rnd(), rnd()
        u_reo, f_jit, f_reo, f_gap = rnd(), rnd(), rnd(), rnd()
        u_rep, f_pick, f_rdel = rnd(), rnd(), rnd()
        st["sent"] += 1
        if s.mtu and len(data) > s.mtu:
            st["too_big"] += 1
            self.trace.append("B")
            return []
        lost = u_loss < s.loss
        code, out = ".", []
        corrupted = reordered = duplicated = replayed = False
        if not lost:
            if u_cor < s.corrupt and data:
                buf = bytearray(data)
                buf[int(f_idx * len(buf))] ^= 1 + int(f_xor * 255)
                data = bytes(buf)
                corrupted = True
                code += "c"
            delay = max(0.0, s.delay_ms + (f_jit * 2 - 1) * s.jitter_ms) / 1000.0
            if u_reo < s.reorder:
                delay += s.reorder_delay_ms * (0.5 + f_reo) / 1000.0
                reordered = True
                code += "r"
            out = [Emission(delay, data, "primary")]
            if u_dup < s.duplicate:
                out.append(Emission(delay + (1 + 4 * f_gap) / 1000.0, data, "duplicate"))
                duplicated = True
                code += "d"
            if u_rep < s.replay and self._history:
                old = self._history[int(f_pick * len(self._history))]
                gap = s.replay_delay_ms * (0.5 + f_rdel) / 1000.0
                out.append(Emission(delay + gap, old, "replay", offer=gap))
                replayed = True
                code += "p"
            self._history.append(data)
        if queue_full:  # never transmitted: none of the decisions above count
            st["queue_dropped"] += 1
            self.trace.append("Q")
            return []
        if lost:
            st["dropped"] += 1
            self.trace.append("L")
            return [Emission(0.0, data, "lost")] if s.airtime else []
        st["corrupted"] += corrupted
        st["reordered"] += reordered
        st["duplicated"] += duplicated
        st["replayed"] += replayed
        self.trace.append(code)
        if s.rate_kbps:
            out = [e._replace(delay=e.delay + self._serialize(len(e.data), now)) for e in out]
        return out

    def _serialize(self, size: int, now: float) -> float:
        tx = size * 8 / (self.s.rate_kbps * 1000.0)
        start = max(now, self._busy_until)
        self._busy_until = start + tx
        return (start - now) + tx


class Started(NamedTuple):
    direction: str
    start: float
    end: float
    item: Any


class _Waiting(NamedTuple):
    ready: float  # earliest start: the time it was offered, or when the serial port finished with it
    size: int
    item: Any


class Air:
    """The transmit scheduler of an airtime model. Pure logic: you give it the time.

    `offer()` puts a packet in a direction's queue. `advance(now)` starts every transmission whose
    start time is not after `now` and returns them. A start is the latest of: when the packet was
    ready, when the medium is free, and when the duty cycle lets this direction transmit again.
    The medium is shared by both directions when `half_duplex`, else each direction has its own.
    Two directions ready at the same moment on a shared medium take turns. With nothing queued,
    `next_start()` is None.
    """

    def __init__(self, settings: ChannelSettings, t0: float = 0.0):
        self.s = settings
        self.d = settings.to_dict()
        self.t0 = t0  # slot grid origin
        self.queue: dict[str, collections.deque[_Waiting]] = {n: collections.deque() for n in DIRECTIONS}
        self.busy = dict.fromkeys(("shared",) + DIRECTIONS, 0.0)
        self.next_ok = dict.fromkeys(DIRECTIONS, 0.0)
        self.uart = dict.fromkeys(DIRECTIONS, 0.0)
        self.on_air_s = dict.fromkeys(DIRECTIONS, 0.0)
        self._last = ""  # direction that transmitted last

    def depth(self, direction: str) -> int:
        """Packets waiting (not counting the one on the air)."""
        return len(self.queue[direction])

    def waiting(self) -> int:
        return sum(len(q) for q in self.queue.values())

    def offer(self, direction: str, now: float, size: int, item: Any) -> None:
        ready = now
        if self.s.serial_bps:  # the serial port clocks bytes in one at a time (10 bits each: 8N1)
            ready = max(now, self.uart[direction]) + size * 10 / self.s.serial_bps
            self.uart[direction] = ready
        self.queue[direction].append(_Waiting(ready, size, item))

    def _align(self, t: float) -> float:
        if self.s.airtime != "slots":
            return t
        slot = self.s.slot_ms / 1000.0
        return self.t0 + max(0, math.ceil((t - self.t0) / slot - 1e-9)) * slot

    def _candidate(self, name: str) -> tuple[float, float] | None:
        q = self.queue[name]
        if not q:
            return None
        head = q[0]
        medium = "shared" if self.s.half_duplex else name
        start = self._align(max(head.ready, self.next_ok[name], self.busy[medium]))
        return start, head.ready

    def next_start(self) -> float | None:
        c = [x for x in (self._candidate(n) for n in DIRECTIONS) if x]
        return min(c)[0] if c else None

    def advance(self, now: float) -> list[Started]:
        out: list[Started] = []
        while True:
            cands = [(n, *c) for n in DIRECTIONS if (c := self._candidate(n))]
            if not cands:
                return out
            first = min(c[1] for c in cands)
            if first > now + 1e-9:
                return out
            tied = [c for c in cands if c[1] - first < 1e-12]
            # Two radios ready at the same moment on one medium take turns, so a long burst from
            # one side cannot shut the other out.
            pick = next((c for c in tied if c[0] != self._last), tied[0]) if self.s.half_duplex else tied[0]
            name, start = pick[0], pick[1]
            self._last = name
            w = self.queue[name].popleft()
            t = airtime_s(self.d, w.size, name)
            end = start + t
            self.busy["shared" if self.s.half_duplex else name] = end
            self.next_ok[name] = start + (t / self.s.duty_cycle if self.s.duty_cycle else t)
            self.on_air_s[name] += t
            out.append(Started(name, start, end, w.item))

    def drain(self) -> list[Any]:
        """Take everything still waiting (at shutdown)."""
        items = [w.item for q in self.queue.values() for w in q]
        for q in self.queue.values():
            q.clear()
        return items


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, channel: "Channel", side: str):
        self.channel, self.side = channel, side
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):  # type: ignore[override]
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        self.channel._on_packet(self.side, data, (addr[0], addr[1]))

    def error_received(self, exc: Exception) -> None:
        self.channel.socket_errors += 1


class Channel:
    """Run the proxy in a background thread. Use `with Channel(...) as ch:` or start()/stop().

    a, b: the real addresses of endpoints A and B. A missing address is learned from the first
    packet that arrives on that side's port (needed for B before A can send to it).
    on_event(event, **fields) is called with `channel_stats` (periodic and final).
    """

    def __init__(
        self,
        settings: ChannelSettings | None = None,
        *,
        a: Addr | None = None,
        b: Addr | None = None,
        host: str = "127.0.0.1",
        port_a: int = 0,
        port_b: int = 0,
        pcap_path: Path | str | None = None,
        name: str = "channel",
        on_event: Callable[..., None] | None = None,
        stats_interval: float = 0.0,
    ):
        self.settings = settings or ChannelSettings()
        self.a, self.b = a, b
        self.host, self._port_a, self._port_b = host, port_a, port_b
        self.name = name
        self.pcap_path = Path(pcap_path) if pcap_path else None
        self.on_event = on_event
        self.stats_interval = stats_interval
        self.dirs = {d: Direction(d, self.settings) for d in DIRECTIONS}
        self.socket_errors = 0
        self.addr_a: Addr = (host, 0)  # where A sends
        self.addr_b: Addr = (host, 0)  # where B sends
        self._pcap: PcapWriter | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tr: dict[str, asyncio.DatagramTransport] = {}
        self._pending: dict[int, tuple[asyncio.TimerHandle, str]] = {}
        self._seq = 0
        self._stopped = False
        self._ticker_task: asyncio.Task | None = None
        self.air: Air | None = Air(self.settings) if self.settings.airtime else None
        self._air_timer: asyncio.TimerHandle | None = None
        self._wall0 = 0.0  # wall clock minus loop clock: turns a loop time into a pcap time stamp

    # --- lifecycle -------------------------------------------------------------------------
    def start(self) -> "Channel":
        if self.pcap_path:
            self._pcap = PcapWriter(self.pcap_path)
        ready = threading.Event()
        err: list[BaseException] = []
        self._thread = threading.Thread(target=self._run, args=(ready, err), daemon=True,
                                        name=f"oasa-channel-{self.name}")
        self._thread.start()
        ready.wait()
        if err:
            raise err[0]
        return self

    def _run(self, ready: threading.Event, err: list[BaseException]) -> None:
        loop = self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._setup())
        except BaseException as exc:  # report bind errors to start()
            err.append(exc)
            ready.set()
            return
        ready.set()
        loop.run_forever()
        loop.close()

    async def _setup(self) -> None:
        loop = asyncio.get_running_loop()
        for side, port in (("a", self._port_a), ("b", self._port_b)):
            tr, _ = await loop.create_datagram_endpoint(
                lambda s=side: _Proto(self, s), local_addr=(self.host, port))
            self._tr[side] = tr
            sock = tr.get_extra_info("socket")
            try:  # big buffers, so a burst from a test is not lost inside the kernel
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
            except OSError:
                pass
        self._wall0 = time.time() - loop.time()
        if self.air:
            self.air.t0 = loop.time()
        self.addr_a = self._tr["a"].get_extra_info("sockname")[:2]
        self.addr_b = self._tr["b"].get_extra_info("sockname")[:2]
        if self.stats_interval > 0:
            self._ticker_task = loop.create_task(self._ticker())

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.stats_interval)
            self._emit_stats(final=False)

    def stop(self, drain_timeout: float = 30.0) -> dict[str, dict[str, int]]:
        """Wait for packets still in flight, close everything, emit the final stats."""
        if self._stopped or not self._loop:
            return self.stats()
        self._stopped = True
        deadline = time.monotonic() + drain_timeout
        while (self._pending or (self.air and self.air.waiting())) and time.monotonic() < deadline:
            time.sleep(0.005)
        asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        assert self._thread
        self._thread.join(10)
        if self._pcap:
            self._pcap.close()
        self._emit_stats(final=True)
        return self.stats()

    async def _shutdown(self) -> None:
        if self._ticker_task:
            self._ticker_task.cancel()
        if self._air_timer:
            self._air_timer.cancel()
        for handle, name in list(self._pending.values()):
            handle.cancel()
            self.dirs[name].stats["abandoned"] += 1
        self._pending.clear()
        if self.air:  # still waiting for the air: never sent
            for name, _side, _src, _dst, e in self.air.drain():
                if e.kind != "lost":
                    self.dirs[name].stats["abandoned"] += 1
        for tr in self._tr.values():
            tr.close()

    def __enter__(self) -> "Channel":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # --- packets ---------------------------------------------------------------------------
    def _on_packet(self, side: str, data: bytes, src: Addr) -> None:
        name = "a_to_b" if side == "a" else "b_to_a"
        d = self.dirs[name]
        if side == "a" and self.a is None:
            self.a = src
        if side == "b" and self.b is None:
            self.b = src
        dst = self.b if side == "a" else self.a
        if dst is None:
            d.unrouted()
            return
        assert self._loop
        now = self._loop.time()
        if self.air:
            self._air_advance(now)  # so the queue depth below is the true one
            full = bool(self.settings.max_queue) and self.air.depth(name) >= self.settings.max_queue
            for e in d.process(data, now, queue_full=full):
                if e.offer > 0:
                    self._seq += 1
                    key = self._seq
                    handle = self._loop.call_later(e.offer, self._offer_later, key, name, side, src, dst, e)
                    self._pending[key] = (handle, name)
                else:
                    self._offer(name, side, src, dst, e, now)
            return
        for e in d.process(data, now):
            if e.delay <= 0:
                self._send(name, side, src, dst, e)
            else:
                self._seq += 1
                key = self._seq
                handle = self._loop.call_later(e.delay, self._send_later, key, name, side, src, dst, e)
                self._pending[key] = (handle, name)

    def _send_later(self, key: int, name: str, side: str, src: Addr, dst: Addr, e: Emission,
                    ts: float | None = None) -> None:
        self._pending.pop(key, None)
        self._send(name, side, src, dst, e, ts)

    def _send(self, name: str, side: str, src: Addr, dst: Addr, e: Emission, ts: float | None = None) -> None:
        out = self._tr["b" if side == "a" else "a"]
        out.sendto(e.data, dst)
        self.dirs[name].stats["forwarded"] += 1
        if self._pcap:
            self._pcap.write(src, dst, e.data, ts=ts)

    # --- the air (airtime model) -------------------------------------------------------------
    def _offer_later(self, key: int, name: str, side: str, src: Addr, dst: Addr, e: Emission) -> None:
        self._pending.pop(key, None)
        assert self._loop
        self._offer(name, side, src, dst, e, self._loop.time())

    def _offer(self, name: str, side: str, src: Addr, dst: Addr, e: Emission, now: float) -> None:
        assert self.air
        self.air.offer(name, now, len(e.data), (name, side, src, dst, e))
        self._air_advance(now)

    def _air_advance(self, now: float) -> None:
        """Start every transmission that is due; deliver each one when its airtime is over."""
        assert self.air and self._loop
        for tx in self.air.advance(now):
            name, side, src, dst, e = tx.item
            if e.kind == "lost":
                continue  # it used the air; nobody hears it
            self._seq += 1
            key = self._seq
            deliver = max(0.0, tx.end + (e.delay - e.offer) - now)
            handle = self._loop.call_later(deliver, self._send_later, key, name, side, src, dst, e,
                                           self._wall0 + tx.start)
            self._pending[key] = (handle, name)
        nxt = self.air.next_start()
        if self._air_timer:
            self._air_timer.cancel()
            self._air_timer = None
        if nxt is not None:
            self._air_timer = self._loop.call_at(max(nxt, now), self._air_tick)

    def _air_tick(self) -> None:
        assert self._loop
        self._air_timer = None
        self._air_advance(self._loop.time())

    # --- stats -----------------------------------------------------------------------------
    def stats(self) -> dict[str, dict[str, int]]:
        return {n: dict(d.stats) for n, d in self.dirs.items()}

    def trace(self, direction: str) -> str:
        return " ".join(self.dirs[direction].trace)

    def endpoints(self) -> dict[str, str]:
        fmt = lambda a: f"{a[0]}:{a[1]}" if a else None  # noqa: E731
        return {"a": fmt(self.a), "b": fmt(self.b),
                "proxy_a": fmt(self.addr_a), "proxy_b": fmt(self.addr_b)}

    def _emit_stats(self, final: bool) -> None:
        if self.on_event:
            self.on_event("channel_stats", channel=self.name, final=final, **self.stats(),
                          endpoints=self.endpoints(), socket_errors=self.socket_errors)


def parse_addr(text: str) -> Addr:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"expected HOST:PORT, got {text!r}")
    return host, int(port)


def main(argv: list[str] | None = None) -> int:
    """`oasa-lab channel`: run the proxy standalone. Prints JSON lines. Stop with Ctrl-C/SIGTERM."""
    import argparse
    import signal

    from .jsonl import now_rfc3339

    p = argparse.ArgumentParser(prog="oasa-lab channel", description="Simulated radio channel (UDP proxy).")
    p.add_argument("--a", type=parse_addr, help="HOST:PORT of endpoint A (learned from first packet if omitted)")
    p.add_argument("--b", type=parse_addr, help="HOST:PORT of endpoint B (learned if omitted)")
    p.add_argument("--host", default="127.0.0.1", help="address the proxy listens on")
    p.add_argument("--port-a", type=int, default=0, help="proxy port A sends to (0 = pick one)")
    p.add_argument("--port-b", type=int, default=0, help="proxy port B sends to (0 = pick one)")
    p.add_argument("--pcap", help="write a capture of every forwarded packet here")
    p.add_argument("--duration", type=float, default=0, help="stop after this many seconds (0 = until signal)")
    p.add_argument("--stats-interval", type=float, default=5.0, help="seconds between stats lines (0 = off)")
    p.add_argument("--profile", help="start from a radio profile (see `oasa-lab profiles`); other options override it")
    for f in fields(ChannelSettings):
        if f.name == "profile":
            continue
        flag = "--" + f.name.replace("_", "-")
        if isinstance(f.default, bool):
            p.add_argument(flag, action=argparse.BooleanOptionalAction, default=None)
        else:  # default None = "not given", so a profile's value is kept
            p.add_argument(flag, type=type(f.default), default=None)
    args = p.parse_args(argv)
    given = {f.name: getattr(args, f.name) for f in fields(ChannelSettings)
             if f.name != "profile" and getattr(args, f.name) is not None}
    try:
        settings = ChannelSettings.for_profile(args.profile, **given)
    except ValueError as exc:
        p.error(str(exc))

    def emit(event: str, **fields_: Any) -> None:
        print(json.dumps({"ts": now_rfc3339(), "event": event, **fields_}, separators=(",", ":")), flush=True)

    ch = Channel(settings, a=args.a, b=args.b, host=args.host, port_a=args.port_a, port_b=args.port_b,
                 pcap_path=args.pcap, on_event=emit, stats_interval=args.stats_interval)
    ch.start()
    emit("channel_started", port_a=ch.addr_a[1], port_b=ch.addr_b[1], host=args.host,
         settings=settings.to_dict(), pcap=args.pcap)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait(args.duration or None)
    ch.stop()
    return 0
