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
"""

from __future__ import annotations

import asyncio
import collections
import json
import random
import socket
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable, NamedTuple

from .pcap import PcapWriter

DIRECTIONS = ("a_to_b", "b_to_a")
STAT_KEYS = (
    "sent", "dropped", "duplicated", "reordered", "corrupted", "replayed", "too_big",
    "unrouted", "abandoned", "forwarded",
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

    def __post_init__(self) -> None:
        for name in ("loss", "reorder", "duplicate", "corrupt", "replay"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        for name in ("delay_ms", "jitter_ms", "reorder_delay_ms", "rate_kbps", "replay_delay_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.mtu < 0 or self.mtu > 65507:
            raise ValueError("mtu must be 0 (no limit) to 65507")
        if not (isinstance(self.seed, int) and 0 <= self.seed < 2**63):
            raise ValueError("seed must be an integer from 0 to 2**63-1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChannelSettings":
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown channel setting(s): {sorted(unknown)}")
        return cls(**d)


class Emission(NamedTuple):
    delay: float  # seconds from now
    data: bytes
    kind: str  # "primary", "duplicate" or "replay"


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

    def process(self, data: bytes, now: float = 0.0) -> list[Emission]:
        """Decide what happens to one packet.

        Trace code per packet: `L` lost, `B` too big, `.` forwarded, then flags
        `c` corrupted, `r` reordered, `d` duplicated, `p` replay attached.
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
        if u_loss < s.loss:
            st["dropped"] += 1
            self.trace.append("L")
            return []
        code = "."
        if u_cor < s.corrupt and data:
            buf = bytearray(data)
            buf[int(f_idx * len(buf))] ^= 1 + int(f_xor * 255)
            data = bytes(buf)
            st["corrupted"] += 1
            code += "c"
        delay = max(0.0, s.delay_ms + (f_jit * 2 - 1) * s.jitter_ms) / 1000.0
        if u_reo < s.reorder:
            delay += s.reorder_delay_ms * (0.5 + f_reo) / 1000.0
            st["reordered"] += 1
            code += "r"
        out = [Emission(delay, data, "primary")]
        if u_dup < s.duplicate:
            out.append(Emission(delay + (1 + 4 * f_gap) / 1000.0, data, "duplicate"))
            st["duplicated"] += 1
            code += "d"
        if u_rep < s.replay and self._history:
            old = self._history[int(f_pick * len(self._history))]
            out.append(Emission(delay + s.replay_delay_ms * (0.5 + f_rdel) / 1000.0, old, "replay"))
            st["replayed"] += 1
            code += "p"
        self._history.append(data)
        self.trace.append(code)
        if s.rate_kbps:
            out = [e._replace(delay=e.delay + self._serialize(len(e.data), now)) for e in out]
        return out

    def _serialize(self, size: int, now: float) -> float:
        tx = size * 8 / (self.s.rate_kbps * 1000.0)
        start = max(now, self._busy_until)
        self._busy_until = start + tx
        return (start - now) + tx


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
        while self._pending and time.monotonic() < deadline:
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
        for handle, name in list(self._pending.values()):
            handle.cancel()
            self.dirs[name].stats["abandoned"] += 1
        self._pending.clear()
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
        for e in d.process(data, self._loop.time()):
            if e.delay <= 0:
                self._send(name, side, src, dst, e)
            else:
                self._seq += 1
                key = self._seq
                handle = self._loop.call_later(e.delay, self._send_later, key, name, side, src, dst, e)
                self._pending[key] = (handle, name)

    def _send_later(self, key: int, name: str, side: str, src: Addr, dst: Addr, e: Emission) -> None:
        self._pending.pop(key, None)
        self._send(name, side, src, dst, e)

    def _send(self, name: str, side: str, src: Addr, dst: Addr, e: Emission) -> None:
        out = self._tr["b" if side == "a" else "a"]
        out.sendto(e.data, dst)
        self.dirs[name].stats["forwarded"] += 1
        if self._pcap:
            self._pcap.write(src, dst, e.data)

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
    for f in fields(ChannelSettings):
        p.add_argument("--" + f.name.replace("_", "-"), type=type(f.default), default=f.default)
    args = p.parse_args(argv)
    settings = ChannelSettings(**{f.name: getattr(args, f.name) for f in fields(ChannelSettings)})

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
