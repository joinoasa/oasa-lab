"""Send thousands of packets through the proxy and measure what comes out."""

import math
import struct
import time

import pytest

from oasa_lab.channel import ChannelSettings, Direction
from oasa_lab.pcap import read_pcap
from oasa_lab.testing import Endpoint, identify, make_packet


def bound(n, p, sigmas=5.0):
    sd = math.sqrt(n * p * (1 - p))
    return n * p - sigmas * sd - 0.5, n * p + sigmas * sd + 0.5


def run(lab, n, size=64, **settings):
    """Send n packets A to B through a channel. Returns (sent, endpoint B, channel stats a_to_b, trace)."""
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, **settings) as ch:
            sent = {i: make_packet(i, lab.rng, size) for i in range(n)}
            a.send_paced(sent.values(), ch.addr_a)
            b.wait_idle(0.4)
        stats = ch.stats()["a_to_b"]
        assert stats["sent"] == n, "the proxy must see every packet we sent"
        assert len(b.received) == stats["forwarded"], "everything forwarded must arrive"
        return sent, b, stats, ch.trace("a_to_b")


def test_transparent_by_default(lab):
    sent, b, st, _ = run(lab, 500)
    assert [d for d in b.payloads()] == list(sent.values())
    assert st["dropped"] == st["duplicated"] == st["corrupted"] == st["reordered"] == 0


@pytest.mark.parametrize("p", [0.05, 0.2, 0.5])
def test_loss_matches_setting(lab, p):
    n = 3000
    sent, b, st, _ = run(lab, n, loss=p)
    lo, hi = bound(n, p)
    assert lo <= st["dropped"] <= hi, f"{st['dropped']} dropped of {n}, expected {lo:.0f}..{hi:.0f}"
    got = {identify(d, sent)[0] for d in b.payloads()}
    assert len(got) == n - st["dropped"]


def test_duplicate_rate(lab):
    n, p = 3000, 0.1
    sent, b, st, _ = run(lab, n, duplicate=p)
    lo, hi = bound(n, p)
    assert lo <= st["duplicated"] <= hi
    ids = [identify(d, sent)[0] for d in b.payloads()]
    assert len(ids) == n + st["duplicated"]
    assert len(set(ids)) == n


def test_corrupt_flips_exactly_one_byte(lab):
    n, p = 3000, 0.1
    sent, b, st, _ = run(lab, n, corrupt=p)
    lo, hi = bound(n, p)
    assert lo <= st["corrupted"] <= hi
    diffs = [identify(d, sent)[1] for d in b.payloads()]
    assert all(d in (0, 1) for d in diffs), "a packet changed in more than one byte, or was unknown"
    assert sum(d == 1 for d in diffs) == st["corrupted"]


def test_reorder_delivers_late(lab):
    n, p = 1500, 0.2
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, reorder=p, reorder_delay_ms=20) as ch:
            sent = {i: make_packet(i, lab.rng) for i in range(n)}
            for pkt in sent.values():
                a.send(pkt, ch.addr_a)
                time.sleep(0.001)
            b.wait_idle(0.4)
        st = ch.stats()["a_to_b"]
    lo, hi = bound(n, p)
    assert lo <= st["reordered"] <= hi
    order = [identify(d, sent)[0] for d in b.payloads()]
    assert sorted(order) == list(range(n)), "reordering must not lose or add packets"
    highest, late = -1, 0
    for s in order:
        if s < highest:
            late += 1
        highest = max(highest, s)
    assert 0.8 * st["reordered"] <= late <= st["reordered"], f"{late} arrived late, {st['reordered']} were delayed"


def test_no_reorder_keeps_order_with_delay(lab):
    sent, b, _, _ = run(lab, 400, delay_ms=5)
    assert b.payloads() == list(sent.values())


def test_delay_and_jitter(lab):
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, delay_ms=40, jitter_ms=20) as ch:
            t0 = []
            for i in range(60):
                t0.append(time.monotonic())
                a.send(struct.pack("!I", i), ch.addr_a)
                time.sleep(0.01)
            b.wait_idle(0.3)
    lat = {}
    for t, d in b.received:
        lat[struct.unpack("!I", d)[0]] = (t - t0[struct.unpack("!I", d)[0]]) * 1000
    assert len(lat) == 60
    assert min(lat.values()) >= 19.0, "delay must be at least delay_ms - jitter_ms"
    assert max(lat.values()) < 60 + 150, "delay should stay near delay_ms + jitter_ms"


def test_rate_limit(lab):
    n, size, kbps = 20, 200, 64  # 1600 bits at 64 kbit/s = 25 ms each, 0.5 s in all
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, rate_kbps=kbps) as ch:
            t0 = time.monotonic()
            for i in range(n):
                a.send(make_packet(i, lab.rng, size), ch.addr_a)
            b.wait_idle(0.3)
    elapsed = b.received[-1][0] - t0
    assert len(b.received) == n
    assert elapsed >= n * size * 8 / (kbps * 1000) * 0.95, f"{n} packets took only {elapsed:.3f} s"
    assert elapsed < 3.0


def test_mtu_drops_oversize(lab):
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, mtu=100) as ch:
            small = [make_packet(i, lab.rng, 60) for i in range(100)]
            big = [make_packet(1000 + i, lab.rng, 101) for i in range(100)]
            a.send_paced([p for pair in zip(small, big) for p in pair], ch.addr_a)
            b.wait_idle()
        st = ch.stats()["a_to_b"]
    assert st["too_big"] == 100
    assert sorted(b.payloads()) == sorted(small)
    assert all(len(p.payload) <= 100 for p in read_pcap(lab.ctx.run_dir / lab.ctx.captures[-1]["file"]))


def test_replay_resends_old_packets(lab):
    n, p = 2000, 0.1
    sent, b, st, _ = run(lab, n, replay=p, delay_ms=1)
    lo, hi = bound(n, p)
    assert lo - 1 <= st["replayed"] <= hi
    ids = [identify(d, sent)[0] for d in b.payloads()]
    assert len(ids) == n + st["replayed"]
    assert set(ids) == set(range(n))


def test_same_seed_same_pattern(lab):
    """Same seed, same decisions. Run through real sockets twice, then compare."""
    settings = dict(loss=0.2, duplicate=0.1, corrupt=0.1, reorder=0.1, replay=0.1, seed=777)
    _, _, st1, t1 = run(lab, 800, **settings)
    _, _, st2, t2 = run(lab, 800, **settings)
    assert t1 == t2 and st1 == st2
    assert "L" in t1 and "c" in t1 and "d" in t1 and "p" in t1 and "r" in t1
    # ...and the same without sockets: the pattern depends only on seed and packet sequence.
    s = ChannelSettings(**settings)
    for pattern in (t1,):
        d = Direction("a_to_b", s)
        for _ in range(800):
            d.process(b"x" * 64, now=time.monotonic())  # clock changes; decisions must not
        assert " ".join(d.trace) == pattern


def test_different_seed_differs(lab):
    base = dict(loss=0.2, duplicate=0.1, corrupt=0.1, reorder=0.1, replay=0.1)
    _, _, _, t1 = run(lab, 800, seed=1, **base)
    _, _, _, t2 = run(lab, 800, seed=2, **base)
    assert t1 != t2


def test_directions_are_independent(lab):
    """B to A works, with its own random stream."""
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, loss=0.3, seed=5) as ch:
            pkts = [make_packet(i, lab.rng) for i in range(600)]
            a.send_paced(pkts, ch.addr_a)
            b.send_paced(pkts, ch.addr_b)
            a.wait_idle(0.4)
            b.wait_idle(0.4)
        st = ch.stats()
    assert st["a_to_b"]["sent"] == st["b_to_a"]["sent"] == 600
    assert len(a.received) == st["b_to_a"]["forwarded"] > 0
    assert len(b.received) == st["a_to_b"]["forwarded"] > 0
    assert ch.trace("a_to_b") != ch.trace("b_to_a")


def test_pcap_holds_exactly_what_was_forwarded(lab):
    sent, b, st, _ = run(lab, 500, loss=0.1, duplicate=0.1, replay=0.1, delay_ms=1)
    pk = read_pcap(lab.ctx.run_dir / lab.ctx.captures[-1]["file"])
    assert len(pk) == st["forwarded"]
    assert sorted(p.payload for p in pk) == sorted(b.payloads())
    assert all(p.dst == b.addr for p in pk)
