"""Push-to-talk voice over a LoRa profile, in simulated time (T-MSG1).

A talker's radio sends one packet every `interval_ms`. Each packet holds the codec frames made in
that interval plus the OASA link's per-packet overhead. Direct: talker to listener. Relayed: the
talker's packet goes to a relay, which sends it again on the same channel (one half-duplex medium,
so both hops share the air). The channel's own scheduler (`channel.Air`) and per-packet decisions
(`channel.Direction`) do the work, driven by a simulated clock instead of sockets, so a 30 s talk
spurt takes milliseconds and repeats exactly from the seed.

`VoiceStream` is the traffic model on its own (packet sizes and times, frame delays), for other
simulations to reuse (the crowd simulation, issue 65). `run()` puts one stream on one LoRa profile.

Numbers and their sources: docs/voice-lora-sim.md.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any

from .channel import Air, ChannelSettings, Direction

# Codec 2 (codec2.c: codec2_bits_per_frame, codec2_samples_per_frame at 8 kHz, codec2_bytes_per_frame
# rounds up to whole bytes). bit/s -> (frame length ms, bytes per frame).
CODEC2_FRAMES: dict[int, tuple[int, int]] = {700: (40, 4), 1200: (40, 6), 2400: (20, 6)}

# OASA-SEC link wire v1 data packet, short (2-byte) counter: header 1 + receiver index 2 + counter 2
# + content type 1 + tag 16 (oasa-sec docs/oasa-sec-v0-link.md section 3; src/link/packet.rs).
OASA_OVERHEAD_BYTES = 22

INTERVALS_MS = (80, 160, 320)  # packet interval: codec frames bundled per packet
TALK_S = 30.0  # one talk spurt
MAX_QUEUE = 4  # packets waiting per radio (est.: a small firmware queue)

# The plan's marks for T-MSG1 voice: mouth-to-ear delay under 1 s, at least 95 % of voice packets
# delivered, direct and through one relay.
PLAN_DELAY_MS = 1000.0
PLAN_DELIVERY = 0.95


@dataclass(frozen=True)
class VoicePacket:
    seq: int
    ready_s: float  # when the last frame is complete and the packet can go to the radio
    size: int  # bytes on the air, OASA overhead included
    frame_end_s: tuple[float, ...]  # when each frame in it was complete


@dataclass(frozen=True)
class VoiceStream:
    """One talker: Codec 2 frames at `rate_bps`, bundled into one OASA packet every `interval_ms`."""

    rate_bps: int
    interval_ms: int

    def __post_init__(self) -> None:
        if self.rate_bps not in CODEC2_FRAMES:
            raise ValueError(f"rate_bps must be one of {sorted(CODEC2_FRAMES)}")
        if self.interval_ms <= 0 or self.interval_ms % self.frame_ms:
            raise ValueError(f"interval_ms must be a positive multiple of {self.frame_ms}")

    @property
    def frame_ms(self) -> int:
        return CODEC2_FRAMES[self.rate_bps][0]

    @property
    def frames_per_packet(self) -> int:
        return self.interval_ms // self.frame_ms

    @property
    def packet_bytes(self) -> int:
        return self.frames_per_packet * CODEC2_FRAMES[self.rate_bps][1] + OASA_OVERHEAD_BYTES

    def packets(self, talk_s: float, start_s: float = 0.0) -> list[VoicePacket]:
        """The packets of one talk spurt that starts (first frame begins) at `start_s`."""
        iv, fr = self.interval_ms / 1000.0, self.frame_ms / 1000.0
        return [VoicePacket(k, start_s + (k + 1) * iv, self.packet_bytes,
                            tuple(start_s + k * iv + (i + 1) * fr for i in range(self.frames_per_packet)))
                for k in range(int(round(talk_s / iv)))]

    @staticmethod
    def frame_delays(packet: VoicePacket, delivered_s: float) -> list[float]:
        """Seconds from each frame's completion to the packet's arrival."""
        return [delivered_s - t for t in packet.frame_end_s]


@dataclass(frozen=True)
class Case:
    profile: str
    rate_bps: int
    interval_ms: int
    relayed: bool

    @property
    def stream(self) -> VoiceStream:
        return VoiceStream(self.rate_bps, self.interval_ms)

    @property
    def frame_ms(self) -> int:
        return self.stream.frame_ms

    @property
    def frames_per_packet(self) -> int:
        return self.stream.frames_per_packet

    @property
    def packet_bytes(self) -> int:
        return self.stream.packet_bytes

    @property
    def name(self) -> str:
        return f"{self.profile}/{self.rate_bps}bps/{self.interval_ms}ms/{'relayed' if self.relayed else 'direct'}"


def offered_occupancy(case: Case) -> float:
    """Share of the channel's time the voice needs: airtime of one packet per interval, per hop."""
    air = ChannelSettings.for_profile(case.profile).airtime_s(case.packet_bytes)
    return air * (2 if case.relayed else 1) / (case.interval_ms / 1000.0)


def run(case: Case, seed: int, loss: float = 0.0, talk_s: float = TALK_S,
        max_queue: int = MAX_QUEUE) -> dict[str, Any]:
    """Simulate one talk spurt. Returns the numbers for one table cell."""
    s = ChannelSettings.for_profile(case.profile, loss=loss, max_queue=max_queue, seed=seed)
    assert s.half_duplex and case.packet_bytes <= s.mtu
    air = Air(s)
    hops = {"a_to_b": Direction("a_to_b", s), "b_to_a": Direction("b_to_a", s)}  # hop 1, hop 2
    stream = case.stream
    pkts = stream.packets(talk_s)
    n_pkts = len(pkts)
    # Events: (time, order, kind, packet number). Offers sort before air starts at the same time,
    # so a packet that becomes ready the moment the air frees up gets its turn.
    ev: list[tuple[float, int, str, int]] = [(p.ready_s, 0, "src", p.seq) for p in pkts]
    heapq.heapify(ev)
    delivered: dict[int, float] = {}
    dropped = {"queue": 0, "loss": 0}

    t_end = 0.0

    def started(txs: list) -> None:
        nonlocal t_end
        for tx in txs:
            k, kind = tx.item
            t_end = max(t_end, tx.end)
            if kind == "lost":
                continue  # it used the air; nobody hears it
            if tx.direction == "a_to_b" and case.relayed:
                heapq.heappush(ev, (tx.end, 0, "relay", k))  # the relay has it; send it on
            else:
                delivered.setdefault(k, tx.end)

    def offer(direction: str, now: float, k: int) -> None:
        started(air.advance(now))  # so the queue depth is the true one
        full = air.depth(direction) >= max_queue
        out = hops[direction].process(bytes(case.packet_bytes), now, queue_full=full)
        if full:
            dropped["queue"] += 1
            return
        for e in out:
            if e.kind == "lost":
                dropped["loss"] += 1
            air.offer(direction, now, case.packet_bytes, (k, e.kind))

    while ev or air.waiting():
        nxt = air.next_start()
        if ev and (nxt is None or ev[0][0] <= nxt + 1e-12):
            t, _, kind, k = heapq.heappop(ev)
            if kind == "src":
                offer("a_to_b", t, k)
            elif kind == "relay":
                offer("b_to_a", t, k)
            continue
        started(air.advance(nxt))

    # Delay of each codec frame: from the moment the frame is complete to delivery at the listener.
    delays = sorted(d for k, t in delivered.items() for d in stream.frame_delays(pkts[k], t))
    on_air = sum(air.on_air_s.values())
    span = max(t_end, pkts[-1].ready_s)
    mean = sum(delays) / len(delays) if delays else 0.0
    p95 = delays[min(len(delays) - 1, int(0.95 * len(delays)))] if delays else 0.0
    occ = offered_occupancy(case)
    delivery_ok = len(delivered) / n_pkts >= PLAN_DELIVERY
    delay_ok = bool(delays) and p95 * 1000 < PLAN_DELAY_MS
    return {
        "case": case.name, "profile": case.profile, "rate_bps": case.rate_bps, "interval_ms": case.interval_ms,
        "mode": "relayed" if case.relayed else "direct", "frames_per_packet": stream.frames_per_packet,
        "packet_bytes": case.packet_bytes,
        "airtime_ms": round(s.airtime_s(case.packet_bytes) * 1000, 3),
        "occupancy": round(occ, 4), "busy_share": round(on_air / span, 4),
        "packets": n_pkts, "delivered": len(delivered), "delivery": round(len(delivered) / n_pkts, 4),
        "queue_dropped": dropped["queue"], "lost": dropped["loss"],
        "delay_mean_ms": round(mean * 1000, 1), "delay_p95_ms": round(p95 * 1000, 1),
        "delay_max_ms": round((delays[-1] if delays else 0.0) * 1000, 1),
        "carries": occ <= 1.0,
        # The plan's marks. The delay here leaves out codec, Bluetooth and playout time (see the doc).
        "plan_delivery_ok": delivery_ok, "plan_delay_ok": delay_ok,
        "meets_plan": occ <= 1.0 and delivery_ok and delay_ok,
    }
