"""Radio profiles through real sockets: does the channel behave like the radio it claims to be?

Timing is read from the capture. A packet's time stamp there is when it started to go on the air,
so the spacing of the stamps is the model's schedule and is exact. Arrival times at the receiving
endpoint are checked too, with slack for a busy machine: a packet can arrive a little late, never early.
"""

import dataclasses

import pytest

from oasa_lab.channel import ChannelSettings, Direction
from oasa_lab.checker.air import airtime as checker_airtime
from oasa_lab.checker.air import check_air
from oasa_lab.checker.pcapread import Packet
from oasa_lab.pcap import read_pcap
from oasa_lab.profiles import profile_names
from oasa_lab.testing import Endpoint, make_packet

TIMED = [n for n in profile_names() if n != "ideal"]
ABS = 0.0005  # seconds of slack on a stamp comparison (stamps are whole microseconds)


def capture(lab, ch):
    """The packets of channel `ch` (stopped), split by direction: (a_to_b, b_to_a)."""
    pk = read_pcap(lab.ctx.run_dir / lab.ctx.captures[-1]["file"])
    dst_b = ch.endpoints()["b"]
    to_b = sorted((p for p in pk if f"{p.dst[0]}:{p.dst[1]}" == dst_b), key=lambda p: p.ts)
    to_a = sorted((p for p in pk if f"{p.dst[0]}:{p.dst[1]}" != dst_b), key=lambda p: p.ts)
    return to_b, to_a


def spacing(packets):
    return [b.ts - a.ts for a, b in zip(packets, packets[1:])]


def pick_size(s: ChannelSettings) -> int:
    """The smallest size (20, 64, 250, 1400 bytes) that stays on the air 5 ms or more, so Python's own
    speed cannot hide the model; the largest the profile allows if none does."""
    cap = s.mtu or 1400
    for size in (20, 64, 250, 1400):
        if size <= cap and s.airtime_s(size) >= 0.005:
            return size
    return min(cap, 1400)


def per_packet(s: ChannelSettings, size: int) -> float:
    """Seconds between starts in a back-to-back burst: the slower of the air and the serial port."""
    t = s.airtime_s(size)
    return max(t, size * 10 / s.serial_bps) if s.serial_bps else t


@pytest.mark.parametrize("name", TIMED)
def test_airtime_and_throughput_match_the_model(lab, name):
    s = ChannelSettings.for_profile(name)
    size = pick_size(s)
    step = per_packet(s, size)
    n = max(3, min(300, round(1.0 / step)))
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, profile=name) as ch:
            pkts = [make_packet(i, lab.rng, size) for i in range(n)]
            a.send_paced(pkts, ch.addr_a, batch=n)  # one burst: the whole queue is full from the start
            b.wait_idle(0.5, timeout=30)
        st = ch.stats()["a_to_b"]
    to_b, _ = capture(lab, ch)
    assert st["sent"] == n and st["forwarded"] == n and st["queue_dropped"] == 0
    assert b.payloads() == pkts, "a profile alone must not lose, reorder or change packets"
    # The schedule in the capture: each packet starts one step after the one before.
    gaps = spacing(to_b)
    assert min(gaps) >= s.airtime_s(size) - ABS, "packets overlap on the air"
    assert gaps == pytest.approx([step] * (n - 1), abs=ABS + 0.002 * step), f"{name}: spacing is not {step * 1000:.3f} ms"
    # The model and the checker's own copy of it agree for this very packet.
    assert checker_airtime(s.to_dict(), size, "a_to_b") == pytest.approx(s.airtime_s(size))
    if s.airtime == "slots":  # starts sit on the frame grid
        slot = s.slot_ms / 1000
        offs = [((p.ts - to_b[0].ts) / slot) for p in to_b]
        assert offs == pytest.approx([round(o) for o in offs], abs=0.01)
    # What the receiver saw. Its clock is read by a thread that a busy machine can hold back by some
    # milliseconds, so this is a sanity bound; the exact check is the capture above.
    arrivals = [t for t, _ in b.received]
    span, want = arrivals[-1] - arrivals[0], step * (n - 1)
    assert span >= 0.9 * want - 0.03, f"{name}: {n} packets arrived in {span:.3f} s, faster than the model's {want:.3f} s"
    assert span <= want * 1.10 + 0.08, f"{name}: {n} packets took {span:.3f} s, the model says {want:.3f} s"
    goodput, model = (n - 1) * size * 8 / span, size * 8 / step
    lab.log("profile_throughput", profile=name, size=size, packets=n, airtime_ms=round(s.airtime_s(size) * 1000, 3),
            step_ms=round(step * 1000, 3), goodput_bps=round(goodput), model_bps=round(model))
    assert 0.75 * model <= goodput <= 1.2 * model


@pytest.mark.parametrize("name", ["lora-sf7-bw125", "sik-64k"])
def test_half_duplex_serializes_both_directions(lab, name):
    s = ChannelSettings.for_profile(name)
    assert s.half_duplex
    size, n = pick_size(s), 8

    def burst(**over):
        with Endpoint() as a, Endpoint() as b:
            with lab.channel(a=a.addr, b=b.addr, profile=name, **over) as ch:
                a.send_paced([make_packet(i, lab.rng, size) for i in range(n)], ch.addr_a, batch=n)
                b.send_paced([make_packet(100 + i, lab.rng, size) for i in range(n)], ch.addr_b, batch=n)
                a.wait_idle(0.5, timeout=30)
                b.wait_idle(0.5, timeout=30)
        assert len(a.received) == len(b.received) == n
        to_b, to_a = capture(lab, ch)
        return sorted(to_b + to_a, key=lambda p: p.ts), to_b, to_a

    both, to_b, to_a = burst()
    air = s.airtime_s(size)
    # Never two packets on the air together, across both directions.
    for prev, nxt in zip(both, both[1:]):
        assert nxt.ts - prev.ts >= air - ABS, "two packets overlapped on a half-duplex medium"
    # They take turns instead of one side waiting for the other's whole queue.
    dirs = ["b" if p in to_b else "a" for p in both]
    assert dirs[:6].count("a") == dirs[:6].count("b") == 3, dirs
    span_half = both[-1].ts - both[0].ts
    assert span_half >= (2 * n - 1) * air - 2 * ABS, "half-duplex span is shorter than the air needed"
    # Full duplex on the same radio: both directions at once, about half the time.
    both_fd, _, _ = burst(half_duplex=False)
    span_full = both_fd[-1].ts - both_fd[0].ts
    assert span_half >= 1.7 * span_full, f"half-duplex {span_half:.3f} s vs full-duplex {span_full:.3f} s"
    lab.log("half_duplex_span", profile=name, half_s=round(span_half, 4), full_s=round(span_full, 4))


def test_queue_overflow_is_counted(lab):
    name, size, n, depth = "lora-sf7-bw125", 64, 30, 4
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, profile=name, max_queue=depth) as ch:
            pkts = [make_packet(i, lab.rng, size) for i in range(n)]
            a.send_paced(pkts, ch.addr_a, batch=n)
            b.wait_idle(0.5, timeout=30)
        st = ch.stats()["a_to_b"]
    # One packet goes on the air at once, `depth` wait, the rest find the queue full.
    assert st["sent"] == n
    assert st["queue_dropped"] == n - (depth + 1), st
    assert st["forwarded"] == depth + 1 and st["dropped"] == 0 and st["abandoned"] == 0
    assert "Q" in ch.trace("a_to_b").split() and ch.trace("a_to_b").split().count("Q") == st["queue_dropped"]
    assert b.payloads() == pkts[:depth + 1], "the packets that were kept must be the first ones, in order"
    assert len(read_pcap(lab.ctx.run_dir / lab.ctx.captures[-1]["file"])) == depth + 1
    # No limit set: nothing is dropped, however long the queue.
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, profile="sik-64k") as ch2:
            a.send_paced([make_packet(i, lab.rng, 60) for i in range(60)], ch2.addr_a, batch=60)
            b.wait_idle(0.5, timeout=30)
    assert ch2.stats()["a_to_b"]["queue_dropped"] == 0 and len(b.received) == 60


def test_a_queue_that_keeps_up_drops_nothing(lab):
    name, size, n = "lora-sf7-bw125", 20, 10
    s = ChannelSettings.for_profile(name)
    gap = 1.5 * s.airtime_s(size)
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, profile=name, max_queue=1) as ch:
            import time

            for i in range(n):
                a.send(make_packet(i, lab.rng, size), ch.addr_a)
                time.sleep(gap)
            b.wait_idle(0.5, timeout=30)
        st = ch.stats()["a_to_b"]
    assert st["queue_dropped"] == 0 and st["forwarded"] == n


def test_duty_cycle_limits_transmit_time(lab):
    s = ChannelSettings.for_profile("sik-64k", serial_bps=0, duty_cycle=0.25)
    size, n = 100, 20
    air = s.airtime_s(size)
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, profile="sik-64k", serial_bps=0, duty_cycle=0.25) as ch:
            a.send_paced([make_packet(i, lab.rng, size) for i in range(n)], ch.addr_a, batch=n)
            b.wait_idle(0.5, timeout=30)
    to_b, _ = capture(lab, ch)
    assert spacing(to_b) == pytest.approx([air / 0.25] * (n - 1), abs=ABS + 0.002 * air / 0.25)
    share = (n - 1) * air / (to_b[-1].ts - to_b[0].ts)
    assert share == pytest.approx(0.25, rel=0.02)


def test_settings_passed_override_the_profile(lab):
    size, n = 100, 20
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, profile="sik-64k", serial_bps=0, air_kbps=128.0) as ch:
            a.send_paced([make_packet(i, lab.rng, size) for i in range(n)], ch.addr_a, batch=n)
            b.wait_idle(0.5, timeout=30)
    recorded = lab.ctx.channels[-1]
    assert recorded["profile"] == "sik-64k"
    assert recorded["settings"]["air_kbps"] == 128.0 and recorded["settings"]["serial_bps"] == 0
    assert recorded["settings"]["mtu"] == 250 and recorded["settings"]["half_duplex"] is True
    want = 1625e-6 + 102 * 8 / 128_000  # fixed part as in the profile, 102 bytes at 128 kbps
    to_b, _ = capture(lab, ch)
    assert spacing(to_b) == pytest.approx([want] * (n - 1), abs=ABS)


def test_profile_combines_with_impairments(lab):
    """WFB-ng profile plus loss, duplicates and corruption: rates follow the settings and the air rule holds."""
    n, size, p = 1500, 200, 0.1
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, profile="wifi-wfb", loss=p, duplicate=0.05, corrupt=0.05) as ch:
            a.send_paced([make_packet(i, lab.rng, size) for i in range(n)], ch.addr_a, batch=100, gap=0.02)
            b.wait_idle(0.5, timeout=30)
        st = ch.stats()["a_to_b"]
    import math

    sd = math.sqrt(n * p * (1 - p))
    assert abs(st["dropped"] - n * p) <= 5 * sd
    assert st["duplicated"] > 0 and st["corrupted"] > 0
    assert st["forwarded"] == n - st["dropped"] + st["duplicated"] == len(b.received)
    to_b, to_a = capture(lab, ch)
    errs, info = check_air("t", lab.ctx.channels[-1]["settings"], {"a_to_b": to_b, "b_to_a": to_a})
    assert errs == [] and info["pairs_checked"] == len(to_b) - 1


def test_profile_mtu_and_override(lab):
    """The LoRa profile refuses a 256-byte packet; with mtu=100 as well, anything over 100."""
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, profile="lora-sf7-bw125", mtu=100) as ch:
            a.send_paced([make_packet(0, lab.rng, 100), make_packet(1, lab.rng, 101), make_packet(2, lab.rng, 255)],
                         ch.addr_a)
            b.wait_idle(0.5, timeout=10)
        st = ch.stats()["a_to_b"]
    assert st["too_big"] == 2 and len(b.received) == 1
    assert ChannelSettings.for_profile("lora-sf7-bw125").mtu == 255


def test_same_seed_same_decisions(lab):
    """Loss, duplicates, corruption, reordering and replay depend on the seed and the packet order only:
    the same through real sockets on a profile, with the airtime model switched off, and with no sockets."""
    kw = dict(loss=0.2, duplicate=0.1, corrupt=0.1, reorder=0.1, replay=0.1, seed=777)
    n, size = 500, 200

    def run(profile):
        with Endpoint() as a, Endpoint() as b:
            with lab.channel(a=a.addr, b=b.addr, profile=profile, **kw) as ch:
                a.send_paced([make_packet(i, lab.rng, size) for i in range(n)], ch.addr_a, batch=50, gap=0.01)
                b.wait_idle(0.5, timeout=30)
            return ch.trace("a_to_b"), ch.stats()["a_to_b"]

    t1, s1 = run("wifi-wfb")
    t2, s2 = run("wifi-wfb")
    assert t1 == t2 and s1 == s2
    t3, _ = run(None)  # no profile, no airtime model
    assert t3 == t1, "the airtime model must not change which packets are lost, duplicated or corrupted"
    pure = Direction("a_to_b", ChannelSettings.for_profile("wifi-wfb", **kw))
    for i in range(n):
        pure.process(make_packet(i, lab.rng, size), now=1e6 * i)  # any clock
    assert " ".join(pure.trace) == t1
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, profile="wifi-wfb", **{**kw, "seed": 778}) as ch:
            a.send_paced([make_packet(i, lab.rng, size) for i in range(n)], ch.addr_a, batch=50, gap=0.01)
            b.wait_idle(0.5, timeout=30)
    assert ch.trace("a_to_b") != t1


def test_air_rule_rejects_overlapping_packets(lab):
    """The checker's air rule passes a real half-duplex capture and fails the same capture when two
    packets are moved to overlap, in one direction and across directions."""
    name, size, n = "sik-64k", 100, 6
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, profile=name) as ch:
            a.send_paced([make_packet(i, lab.rng, size) for i in range(n)], ch.addr_a, batch=n)
            b.send_paced([make_packet(50 + i, lab.rng, size) for i in range(n)], ch.addr_b, batch=n)
            a.wait_idle(0.5, timeout=10)
            b.wait_idle(0.5, timeout=10)
    settings = lab.ctx.channels[-1]["settings"]
    to_b, to_a = capture(lab, ch)
    errs, info = check_air("t", settings, {"a_to_b": to_b, "b_to_a": to_a})
    assert errs == [] and info["pairs_checked"] == 2 * n - 1
    air = checker_airtime(settings, size, "a_to_b")

    def shifted(packets, idx, ts):
        out = list(packets)
        out[idx] = dataclasses.replace(out[idx], ts=ts)
        return out

    same = shifted(to_b, 2, to_b[1].ts + 0.3 * air)
    errs, _ = check_air("t", settings, {"a_to_b": same, "b_to_a": to_a})
    assert errs and "overlap" in errs[0]
    cross = shifted(to_a, 1, to_b[2].ts + 0.5 * air)
    errs, _ = check_air("t", settings, {"a_to_b": to_b, "b_to_a": cross})
    assert errs and "both directions" in errs[0]
    # The same crossing is allowed on a full-duplex channel: the rule follows the settings.
    up = [Packet(0.0, ("127.0.0.1", 1), ("127.0.0.1", 2), bytes(size))]
    down = [Packet(0.5 * air, ("127.0.0.1", 2), ("127.0.0.1", 1), bytes(size))]
    errs, _ = check_air("t", settings, {"a_to_b": up, "b_to_a": down})
    assert errs and "both directions" in errs[0]
    assert check_air("t", {**settings, "half_duplex": False}, {"a_to_b": up, "b_to_a": down})[0] == []
