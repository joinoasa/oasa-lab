"""Many radios sharing one half-duplex LoRa channel, in simulated time (T-MSG1, issue 65).

Two models, both event-driven on a simulated clock (no sockets, no sleeps), repeatable from a seed:

- `run_text(TextCase)`: N radios on the long-range text setting. Each sends text (acknowledged,
  retried) to a random other radio and broadcasts its position. With or without listen-before-send.
  0 to 4 voice calls run at the same time: each call is set up on this channel, and while it lasts
  its two radios are on the voice setting and (with one receiver) hear nothing here.
- `run_voice(VoiceCase)`: 1 to 4 push-to-talk conversations sharing one fast voice setting, direct
  or through one relay each, with the traffic model of `voicesim.VoiceStream`.

Collision rule: any overlap of two transmissions destroys both, at every receiver (one collision
domain). Optional: a simple capture rule. Every number and its status: docs/crowd-sim.md.
"""

from __future__ import annotations

import heapq
import random
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from .airtime import lora_symbol_s
from .channel import ChannelSettings
from .voicesim import MAX_QUEUE as VOICE_QUEUE
from .voicesim import OASA_OVERHEAD_BYTES as OH
from .voicesim import PLAN_DELAY_MS, PLAN_DELIVERY, VoiceStream

# Long-range settings for text, location and call setup (the plan has not fixed one). SF11 at
# 250 kHz is Meshtastic's LongFast (which uses a 16-symbol preamble; ours has 8).
TEXT_SETTINGS = ("lora-sf9-bw125", "lora-sf10-bw250", "lora-sf11-bw250")
# Fast settings for voice (the plan: SF7 or SF8 at 250 or 500 kHz).
VOICE_SETTINGS = ("lora-sf7-bw250", "lora-sf8-bw250", "lora-sf7-bw500", "lora-sf8-bw500")
CROWD_N = (10, 30, 100, 300)  # the card's crowd sizes
FINE_N = (10, 20, 30, 50, 75, 100, 150, 200, 300)  # for the largest N above the mark
CALLS = (0, 1, 2, 4)

# Packet sizes on the air: content + the OASA link's 22 bytes (voicesim.OASA_OVERHEAD_BYTES).
TEXT_BYTES = 60 + OH  # a short text with its sender, recipient and message id (est.)
POS_BYTES = 20 + OH  # latitude, longitude, altitude, time (est.)
ACK_BYTES = 4 + OH  # the message id it acknowledges (est.)
SETUP_BYTES = 40 + OH  # call setup request (est.)

TEXT_ATTEMPTS = 3  # a text or call setup is sent at most 3 times (2 retries)
# No ACK by 2 ACK airtimes + 1 packet airtime + uniform 0 to 5 s after a send: send again.
RETRY_JITTER_S = 5.0
QUEUE = 16  # packets waiting per radio on the text setting; a new packet that finds it full is dropped
PRIO = {"ack": 0, "setup": 1, "text": 2, "pos": 3}  # the plan's order (voice is on its own setting)

CALL_S = 90.0  # a call lasts 90 s after it is accepted
CALL_GAP_S = 10.0  # next call in the same slot starts 0 to 10 s later

WINDOW_S = 1800.0  # traffic is made for 30 min (longer under 200 radios, see default_window_s), then 300 s
DRAIN_S = 300.0

# Listen-before-send: channel activity detection over 2 symbols plus half a symbol of processing.
# It sees a transmission that is on the air when it starts; one that starts during the CAD is missed.
CAD_SYMBOLS = 2.5
CW_MIN_EXP, CW_MAX_EXP = 3, 7  # backoff: uniform 0 to 2^k CAD times, k = 3 + busy count, up to 7
# Before its first CAD a packet waits uniform 0 to 2^3 CAD times (an ACK goes at once), so radios
# that queued during one packet do not all check the channel the moment it ends.

CAPTURE_DB = 6.0  # capture rule: a packet survives an overlap if 6 dB above each overlapping one
POWER_SPREAD_DB = 20.0  # received power per sender-receiver pair: uniform over 20 dB, fixed per run

PLAN_TEXT_DELIVERY = 0.95  # the card's mark: text delivery above 95 %

VOICE_RATE_BPS, VOICE_INTERVAL_MS = 700, 160  # plan baseline 700 bit/s; 4 frames per packet
VOICE_WINDOW_S = 300.0
SPURT_S = (2.0, 10.0)  # one talk turn
TURN_GAP_S = (0.5, 2.0)  # silence before the other side answers


@dataclass(frozen=True)
class Traffic:
    name: str
    texts_per_h: float  # per radio
    pos_interval_s: float


TRAFFIC = {t.name: t for t in (Traffic("pos60", 6, 60), Traffic("quiet", 6, 900), Traffic("chatty", 30, 900))}


def lbt_name(lbt: bool) -> str:
    return "lbt" if lbt else "blind"


@dataclass(frozen=True)
class TextCase:
    profile: str
    n: int
    lbt: bool
    traffic: str = "quiet"
    calls: int = 0
    listen: str = "one"  # "one": a radio in a call hears nothing here; "two": it has a second receiver
    capture: bool = False
    hidden: float = 0.0  # share of radio pairs that cannot hear each other's carrier (LBT only)

    def __post_init__(self) -> None:
        if self.traffic not in TRAFFIC or self.listen not in ("one", "two") or not 0 <= self.hidden < 1:
            raise ValueError(f"bad case {self}")
        if self.n < 2 or self.n < 2 * self.calls + 1:
            raise ValueError("too few radios for the calls")

    @property
    def name(self) -> str:
        extra = "".join([f"/listen-{self.listen}" if self.calls else "", "/capture" if self.capture else "",
                         f"/hidden{self.hidden:g}" if self.hidden else ""])
        return f"{self.profile}/n{self.n}/{lbt_name(self.lbt)}/{self.traffic}/calls{self.calls}{extra}"


@dataclass(frozen=True)
class VoiceCase:
    profile: str
    calls: int
    lbt: bool
    relayed: bool

    @property
    def name(self) -> str:
        return f"{self.profile}/calls{self.calls}/{lbt_name(self.lbt)}/{'relayed' if self.relayed else 'direct'}"


class Sim:
    """A simulated clock: events run in time order; at equal times, ends (order 0) before the rest."""

    def __init__(self) -> None:
        self.q: list[tuple[float, int, int, Callable, tuple]] = []
        self.k = 0
        self.now = 0.0

    def at(self, t: float, fn: Callable, *a: Any, order: int = 1) -> None:
        self.k += 1
        heapq.heappush(self.q, (t, order, self.k, fn, a))

    def run(self, until: float) -> None:
        while self.q and self.q[0][0] <= until:
            t, _, _, fn, a = heapq.heappop(self.q)
            self.now = t
            fn(*a)


class Tx:
    __slots__ = ("src", "kind", "start", "end", "item", "hits")

    def __init__(self, src: int, kind: str, start: float, end: float, item: Any):
        self.src, self.kind, self.start, self.end, self.item = src, kind, start, end, item
        self.hits: list[Tx] = []  # transmissions that overlapped this one


class Medium:
    """One channel, one collision domain: every receiver hears every transmitter."""

    def __init__(self, power: list[list[float]] | None = None, deaf_to: list[set[int]] | None = None):
        self.active: list[Tx] = []
        self.power = power  # capture rule on when set
        self.deaf_to = deaf_to  # deaf_to[r]: senders whose carrier r's CAD cannot see (hidden nodes)
        self.on_air = 0.0
        self.sent = 0
        self.collided = 0
        self.clean_air = 0.0

    def start(self, tx: Tx) -> None:
        for o in self.active:
            o.hits.append(tx)
            tx.hits.append(o)
        self.active.append(tx)
        self.sent += 1

    def end(self, tx: Tx) -> None:
        self.active.remove(tx)
        self.on_air += tx.end - tx.start
        if tx.hits:
            self.collided += 1
        else:
            self.clean_air += tx.end - tx.start

    def busy(self, r: int) -> bool:
        if self.deaf_to is None:
            return bool(self.active)
        return any(o.src not in self.deaf_to[r] for o in self.active)

    def got(self, tx: Tx, r: int) -> bool:
        """Did radio r receive tx? Not if r was sending (half-duplex) or another packet overlapped it,
        unless the capture rule is on and tx is CAPTURE_DB above each overlapping one at r."""
        if r == tx.src:
            return False
        if not tx.hits:
            return True
        if self.power is None or any(o.src == r for o in tx.hits):
            return False
        p = self.power
        return all(p[tx.src][r] >= p[o.src][r] + CAPTURE_DB for o in tx.hits)


def _pct(xs: list[float], q: float) -> float:
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def _power(rng: random.Random, n: int) -> list[list[float]]:
    return [[rng.random() * POWER_SPREAD_DB for _ in range(n)] for _ in range(n)]


def _backoff(sim: Sim, rng: random.Random, t_cad: float, k: int) -> float:
    return sim.now + t_cad + rng.random() * (1 << min(CW_MIN_EXP + k, CW_MAX_EXP)) * t_cad


def cad_s(profile: str) -> float:
    s = ChannelSettings.for_profile(profile)
    return CAD_SYMBOLS * lora_symbol_s(s.lora_sf, s.lora_bw_hz)


# ---------------------------------------------------------------- text, location and call setup


class Msg:
    __slots__ = ("kind", "src", "dst", "created", "attempts", "acked", "got_at", "gave_up", "slot", "accepted")

    def __init__(self, kind: str, src: int, dst: int, created: float, slot: int = -1):
        self.kind, self.src, self.dst, self.created, self.slot = kind, src, dst, created, slot
        self.attempts = 0
        self.acked = False
        self.got_at: float | None = None
        self.gave_up = False
        self.accepted = False  # call setup: the callee took the call


class Radio:
    __slots__ = ("id", "queue", "sending", "call", "pending", "pos", "hold")

    def __init__(self, i: int):
        self.id = i
        self.queue: list[tuple[int, int, str, Any]] = []
        self.sending = False
        self.call = (0.0, 0.0)  # last call: [start, end)
        self.pending = False  # picked for a call that is being set up
        self.pos: list | None = None  # the queued position, if any: [created]
        self.hold = 0.0  # after sending a text or setup, listen for its ACK until then


class TextSim:
    def __init__(self, case: TextCase, seed: int, window_s: float | None = None):
        self.c, self.window = case, window_s or default_window_s(case.n)
        self.rng = random.Random(f"crowd-text/{seed}/{case.name}")
        self.sim = Sim()
        s = ChannelSettings.for_profile(case.profile)
        self.air = {k: s.airtime_s(b) for k, b in
                    (("text", TEXT_BYTES), ("pos", POS_BYTES), ("ack", ACK_BYTES), ("setup", SETUP_BYTES))}
        self.size = {"text": TEXT_BYTES, "pos": POS_BYTES, "ack": ACK_BYTES, "setup": SETUP_BYTES}
        self.t_cad = cad_s(case.profile)
        deaf_to = None
        if case.hidden:
            deaf_to = [set() for _ in range(case.n)]
            for a in range(case.n):
                for b in range(a + 1, case.n):
                    if self.rng.random() < case.hidden:
                        deaf_to[a].add(b)
                        deaf_to[b].add(a)
        self.m = Medium(_power(self.rng, case.n) if case.capture else None, deaf_to)
        self.radios = [Radio(i) for i in range(case.n)]
        self.msgs: list[Msg] = []  # texts and call setups
        self.seq = 0
        self.dropped = {k: 0 for k in PRIO}
        self.pos_made = self.pos_replaced = self.pos_heard = 0
        self.cad_busy = 0
        self.setup_tries = 0
        self.calls_up = 0
        self.call_s = 0.0  # radio-seconds spent in calls (deaf when listen is "one")

    # -- helpers
    def deaf(self, r: Radio, t0: float, t1: float | None = None) -> bool:
        if self.c.listen == "two":
            return False
        a, b = r.call
        return a < b and a <= (t0 if t1 is None else t1) and t0 < b

    def enqueue(self, r: Radio, kind: str, item: Any) -> bool:
        if len(r.queue) >= QUEUE:
            self.dropped[kind] += 1
            return False
        self.seq += 1
        heapq.heappush(r.queue, (PRIO[kind], self.seq, kind, item))
        self.kick(r)
        return True

    def kick(self, r: Radio) -> None:
        now = self.sim.now
        if r.sending or not r.queue or self.deaf(r, now):
            return
        if now < r.hold and r.queue[0][2] != "ack":
            self.sim.at(r.hold, self.kick, r)
            return
        r.sending = True
        if not self.c.lbt:
            self.transmit(r)
        elif r.queue[0][2] == "ack":
            self.cad(r, 0)
        else:
            self.sim.at(now + self.rng.random() * (1 << CW_MIN_EXP) * self.t_cad, self.cad, r, 0)

    def cad(self, r: Radio, k: int) -> None:
        if self.deaf(r, self.sim.now):
            r.sending = False
            return
        if self.m.busy(r.id):
            self.cad_busy += 1
            self.sim.at(_backoff(self.sim, self.rng, self.t_cad, k), self.cad, r, k + 1)
        else:
            self.sim.at(self.sim.now + self.t_cad, self.transmit, r)

    def transmit(self, r: Radio) -> None:
        now = self.sim.now
        if not r.queue or self.deaf(r, now):
            r.sending = False
            return
        _, _, kind, item = heapq.heappop(r.queue)
        if kind == "pos":
            r.pos = None
        elif kind in ("text", "setup"):
            item.attempts += 1
        tx = Tx(r.id, kind, now, now + self.air[kind], item)
        self.m.start(tx)
        self.sim.at(tx.end, self.tx_end, r, tx, order=0)

    def tx_end(self, r: Radio, tx: Tx) -> None:
        self.m.end(tx)
        r.sending = False
        now, kind, item = self.sim.now, tx.kind, tx.item
        if kind == "pos":
            if not tx.hits and self.m.power is None:
                if self.c.calls and self.c.listen == "one":
                    self.pos_heard += sum(1 for o in self.radios if o is not r and not self.deaf(o, tx.start, now))
                else:
                    self.pos_heard += self.c.n - 1
            else:
                self.pos_heard += sum(1 for o in self.radios if not self.deaf(o, tx.start, now) and self.m.got(tx, o.id))
        elif kind in ("text", "setup"):
            dst = self.radios[item.dst]
            if not self.deaf(dst, tx.start, now) and self.m.got(tx, dst.id):
                if item.got_at is None:
                    item.got_at = now
                self.enqueue(dst, "ack", item)
            r.hold = now + self.air["ack"] + 2 * self.t_cad
            timeout = 2 * self.air["ack"] + self.air[kind] + self.rng.random() * RETRY_JITTER_S
            self.sim.at(now + timeout, self.ack_timeout, item, item.attempts)
        elif kind == "ack":  # r is the receiver of the text or setup, item the message
            if item.kind == "setup" and not item.accepted and not item.gave_up:
                self.start_call(item, now)  # the callee accepts: in the call from now
            src = self.radios[item.src]
            if not self.deaf(src, tx.start, now) and self.m.got(tx, src.id) and not item.acked:
                item.acked = True
                if item.kind == "setup" and r.call[1] > now and not self.deaf(src, now):
                    self.join_call(src, r.call[1], now)
        self.kick(r)

    def ack_timeout(self, m: Msg, attempt: int) -> None:
        if m.acked or m.attempts != attempt or m.gave_up:
            return
        if m.attempts < TEXT_ATTEMPTS:
            if m.kind == "setup":
                self.setup_tries += 1
            self.enqueue(self.radios[m.src], m.kind, m)
            return
        m.gave_up = True
        if m.kind == "setup" and not m.accepted:
            self.end_slot(m)

    # -- traffic
    def make_text(self, r: Radio) -> None:
        now = self.sim.now
        if now >= self.window:
            return
        dst = self.rng.randrange(self.c.n - 1)
        m = Msg("text", r.id, dst + (dst >= r.id), now)
        self.msgs.append(m)
        if not self.enqueue(r, "text", m):
            m.gave_up = True
        self.sim.at(now + self.rng.expovariate(TRAFFIC[self.c.traffic].texts_per_h / 3600.0), self.make_text, r)

    def make_pos(self, r: Radio) -> None:
        now = self.sim.now
        if now >= self.window:
            return
        self.pos_made += 1
        if r.pos is not None:  # a newer position replaces one still waiting
            self.pos_replaced += 1
            r.pos[0] = now
        else:
            r.pos = [now]
            if not self.enqueue(r, "pos", r.pos):
                r.pos = None
        iv = TRAFFIC[self.c.traffic].pos_interval_s
        self.sim.at(now + iv * (0.95 + 0.1 * self.rng.random()), self.make_pos, r)

    # -- calls: `calls` slots, each running one call after another
    def start_slot(self, slot: int) -> None:
        now = self.sim.now
        if now >= self.window:
            return
        free = [r for r in self.radios if not r.pending and r.call[1] <= now]
        if len(free) < 2:
            self.sim.at(now + CALL_GAP_S, self.start_slot, slot)
            return
        a, b = self.rng.sample(free, 2)
        a.pending = b.pending = True
        m = Msg("setup", a.id, b.id, now, slot)
        self.msgs.append(m)
        self.setup_tries += 1
        if not self.enqueue(a, "setup", m):
            m.gave_up = True
            self.end_slot(m)

    def start_call(self, m: Msg, now: float) -> None:
        m.accepted = True
        self.radios[m.dst].call = (now, now + CALL_S)
        self.calls_up += 1
        self.call_s += CALL_S
        self.sim.at(now + CALL_S, self.end_slot, m, order=0)

    def join_call(self, caller: Radio, end: float, now: float) -> None:
        caller.call = (now, end)
        self.call_s += end - now

    def end_slot(self, m: Msg) -> None:
        """The call ended, or its setup failed: free both radios; the slot's next call follows."""
        now = self.sim.now
        for i in (m.src, m.dst):
            r = self.radios[i]
            r.pending = False
            self.kick(r)
        self.sim.at(now + self.rng.random() * CALL_GAP_S, self.start_slot, m.slot)

    def run(self) -> dict[str, Any]:
        tr = TRAFFIC[self.c.traffic]
        for r in self.radios:
            self.sim.at(self.rng.expovariate(tr.texts_per_h / 3600.0), self.make_text, r)
            self.sim.at(self.rng.random() * tr.pos_interval_s, self.make_pos, r)
        for slot in range(self.c.calls):
            self.sim.at(self.rng.random() * CALL_GAP_S, self.start_slot, slot)
        self.sim.run(self.window + DRAIN_S)
        return self.results()

    def results(self) -> dict[str, Any]:
        c, tr = self.c, TRAFFIC[self.c.traffic]
        texts = [m for m in self.msgs if m.kind == "text"]
        setups = [m for m in self.msgs if m.kind == "setup"]
        got = [m for m in texts if m.got_at is not None]
        delays = sorted(m.got_at - m.created for m in got)
        span = self.window + DRAIN_S
        offered = c.n * (tr.texts_per_h / 3600.0 * (self.air["text"] + self.air["ack"])
                         + self.air["pos"] / tr.pos_interval_s)
        delivery = len(got) / len(texts) if texts else 1.0
        return {
            "case": c.name, "profile": c.profile, "n": c.n, "window_s": self.window, "lbt": c.lbt, "traffic": c.traffic,
            "texts_per_h": tr.texts_per_h, "pos_interval_s": tr.pos_interval_s, "calls": c.calls,
            "listen": c.listen, "capture": c.capture, "hidden": c.hidden,
            "airtime_ms": {k: round(v * 1000, 1) for k, v in self.air.items()},
            "offered": round(offered, 4), "busy_share": round(self.m.on_air / span, 4),
            "tx": self.m.sent, "collided_share": round(self.m.collided / max(1, self.m.sent), 4),
            "clean_air_share": round(self.m.clean_air / span, 4), "cad_busy": self.cad_busy,
            "texts": len(texts), "text_delivered": len(got), "text_delivery": round(delivery, 4),
            "text_acked": sum(m.acked for m in texts),
            "text_attempts_mean": round(sum(m.attempts for m in texts) / max(1, len(texts)), 3),
            "text_delay_mean_s": round(sum(delays) / len(delays), 3) if delays else 0.0,
            "text_delay_p95_s": round(_pct(delays, 0.95), 3),
            "dropped": dict(self.dropped),
            "pos_made": self.pos_made, "pos_replaced": self.pos_replaced,
            "pos_delivery": round(self.pos_heard / max(1, self.pos_made * (c.n - 1)), 4),
            "setups": len(setups), "setup_delivered": sum(m.got_at is not None for m in setups),
            "setup_acked": sum(m.acked for m in setups), "calls_up": self.calls_up,
            "in_call_share": round(self.call_s / (c.n * span), 4),
            "meets_mark": delivery > PLAN_TEXT_DELIVERY,
        }


def default_window_s(n: int) -> float:
    """30 min, stretched under 200 radios so that a small crowd still sends enough texts to count
    (10 radios: 10 h)."""
    return WINDOW_S * max(1.0, 200 / n)


def run_text(case: TextCase, seed: int, window_s: float | None = None) -> dict[str, Any]:
    return TextSim(case, seed, window_s).run()


def largest_n(results: dict[int, dict[str, Any]]) -> int:
    """The largest N whose text delivery is above 95 % (0: none). Smaller crowds can still fail
    (with calls, a small crowd has a large share of its radios in a call)."""
    return max((n for n, r in results.items() if r["meets_mark"]), default=0)


def sizes_to_try(grid: dict[int, dict[str, Any]]) -> list[int]:
    """The FINE_N sizes not yet run that could change the answer: those below the first grid size
    above the largest passing one. Assumes no size passes above a failing grid size that is itself
    above every passing one."""
    passing = [n for n in sorted(grid) if grid[n]["meets_mark"]]
    if not passing:
        return []
    above = [n for n in sorted(grid) if n > passing[-1]]
    top = above[0] if above else max(FINE_N) + 1
    return [n for n in FINE_N if n not in grid and n < top]


# ---------------------------------------------------------------- voice on the fast setting


def run_voice(case: VoiceCase, seed: int, window_s: float = VOICE_WINDOW_S) -> dict[str, Any]:
    """`calls` conversations on one fast setting. Conversation k: radios 3k and 3k+1 take turns to
    talk; radio 3k+2 relays every packet once when `relayed` (the listener hears only the relay)."""
    rng = random.Random(f"crowd-voice/{seed}/{case.name}")
    sim, m = Sim(), Medium()
    stream = VoiceStream(VOICE_RATE_BPS, VOICE_INTERVAL_MS)
    s = ChannelSettings.for_profile(case.profile)
    air, t_cad = s.airtime_s(stream.packet_bytes), cad_s(case.profile)
    queues = [deque() for _ in range(3 * case.calls)]
    sending = [False] * len(queues)
    made: list[Any] = []
    delivered: dict[int, float] = {}
    drops = {"queue": 0}
    busy = [0]

    def offer(r: int, item: tuple) -> None:
        if len(queues[r]) >= VOICE_QUEUE:
            drops["queue"] += 1
            return
        queues[r].append(item)
        kick(r)

    def kick(r: int) -> None:
        if sending[r] or not queues[r]:
            return
        sending[r] = True
        if case.lbt:
            sim.at(sim.now + rng.random() * (1 << CW_MIN_EXP) * t_cad, cad, r, 0)
        else:
            transmit(r)

    def cad(r: int, k: int) -> None:
        if m.busy(r):
            busy[0] += 1
            sim.at(_backoff(sim, rng, t_cad, k), cad, r, k + 1)
        else:
            sim.at(sim.now + t_cad, transmit, r)

    def transmit(r: int) -> None:
        tx = Tx(r, "voice", sim.now, sim.now + air, queues[r].popleft())
        m.start(tx)
        sim.at(tx.end, tx_end, r, tx, order=0)

    def tx_end(r: int, tx: Tx) -> None:
        m.end(tx)
        sending[r] = False
        k, hop, listener, relay = tx.item
        if hop == 1 and case.relayed:
            if m.got(tx, relay):
                offer(relay, (k, 2, listener, relay))
        elif m.got(tx, listener) and k not in delivered:
            delivered[k] = sim.now
        kick(r)

    for c in range(case.calls):
        a, b, relay = 3 * c, 3 * c + 1, 3 * c + 2
        t, talker = rng.random() * 5.0, 0
        while t < window_s:
            spurt = rng.uniform(*SPURT_S)
            src, dst = (a, b) if talker == 0 else (b, a)
            for p in stream.packets(spurt, t):
                made.append(p)
                sim.at(p.ready_s, offer, src, (len(made) - 1, 1, dst, relay))
            t += spurt + rng.uniform(*TURN_GAP_S)
            talker ^= 1
    sim.run(window_s + 60.0)
    delays = sorted(d for k, t in delivered.items() for d in VoiceStream.frame_delays(made[k], t))
    talk_s = len(made) * VOICE_INTERVAL_MS / 1000.0
    hops = 2 if case.relayed else 1
    delivery = len(delivered) / len(made) if made else 1.0
    p95 = _pct(delays, 0.95) * 1000
    return {
        "case": case.name, "profile": case.profile, "calls": case.calls, "lbt": case.lbt,
        "mode": "relayed" if case.relayed else "direct", "rate_bps": VOICE_RATE_BPS,
        "interval_ms": VOICE_INTERVAL_MS, "packet_bytes": stream.packet_bytes,
        "airtime_ms": round(air * 1000, 2),
        "occupancy_per_talker": round(air * hops / (VOICE_INTERVAL_MS / 1000.0), 4),
        "offered": round(len(made) * air * hops / window_s, 4),
        "busy_share": round(m.on_air / window_s, 4), "talk_s": round(talk_s, 1),
        "packets": len(made), "delivered": len(delivered), "delivery": round(delivery, 4),
        "queue_dropped": drops["queue"], "collided_share": round(m.collided / max(1, m.sent), 4),
        "cad_busy": busy[0],
        "delay_mean_ms": round(sum(delays) / len(delays) * 1000, 1) if delays else 0.0,
        "delay_p95_ms": round(p95, 1),
        "meets_plan": delivery >= PLAN_DELIVERY and bool(delays) and p95 < PLAN_DELAY_MS,
    }
