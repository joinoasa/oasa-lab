"""Push-to-talk voice over LoRa through the OASA link, simulated (T-MSG1).

The sweep runs the channel's own scheduler on a simulated clock (oasa_lab/voicesim.py), so all
270 cases take seconds. One case also runs through the real socket channel, so the checker can
re-check its airtime from the capture and the two can be compared.

A case "carries" the voice when its offered occupancy (airtime the voice needs per second, both
hops when relayed) is at most 100 %. Cases over 100 % are logged as failing: that is a result,
and the suite passes when it reports them correctly. Table and assumptions: docs/voice-lora-sim.md.
"""

import math
import time

import pytest

from oasa_lab.channel import ChannelSettings
from oasa_lab.pcap import read_pcap
from oasa_lab.profiles import LORA_BWS_KHZ, LORA_SFS
from oasa_lab.testing import Endpoint
from oasa_lab.voicesim import (CODEC2_FRAMES, INTERVALS_MS, OASA_OVERHEAD_BYTES, PLAN_DELAY_MS, PLAN_DELIVERY,
                                Case, VoiceStream, offered_occupancy, run)

SETTINGS = [f"lora-sf{sf}-bw{bw}" for bw in LORA_BWS_KHZ for sf in LORA_SFS]
RATES = sorted(CODEC2_FRAMES)


def cases():
    return [Case(p, r, i, relayed) for p in SETTINGS for r in RATES for i in INTERVALS_MS for relayed in (False, True)]


def report(lab, res):
    """One voice_case line per case, and the numbers against the suite's sanity limits."""
    lab.log("voice_case", **res)
    name = res["case"]
    ok = [lab.measure("voice_occupancy", name, res["occupancy"]),
          lab.measure("voice_delivery", name, res["delivery"]),
          lab.measure("voice_delay_mean_ms", name, res["delay_mean_ms"], "ms"),
          lab.measure("voice_delay_p95_ms", name, res["delay_p95_ms"], "ms")]
    assert all(ok), res


def test_packet_sizes_fit_and_include_the_link_overhead():
    for c in cases():
        frame_ms, frame_bytes = CODEC2_FRAMES[c.rate_bps]
        assert c.packet_bytes == c.interval_ms // frame_ms * frame_bytes + OASA_OVERHEAD_BYTES
        assert c.packet_bytes <= ChannelSettings.for_profile(c.profile).mtu


def test_voice_stream_model():
    """The traffic model on its own: 700 bit/s, a packet every 160 ms = 4 frames of 4 bytes + 22."""
    v = VoiceStream(700, 160)
    pk = v.packets(talk_s=1.6, start_s=10.0)
    assert len(pk) == 10 and all(p.size == 4 * 4 + 22 for p in pk)
    assert pk[0].ready_s == pytest.approx(10.16) and pk[0].frame_end_s == pytest.approx((10.04, 10.08, 10.12, 10.16))
    assert VoiceStream.frame_delays(pk[0], 10.30) == pytest.approx([0.26, 0.22, 0.18, 0.14])
    assert VoiceStream(2400, 80).packet_bytes == 4 * 6 + 22
    with pytest.raises(ValueError):
        VoiceStream(2400, 30)


def test_voice_sweep(lab):
    """Every setting x rate x interval, direct and relayed. Each verdict follows the occupancy."""
    t0 = time.monotonic()
    results = [run(c, seed=lab.seed) for c in cases()]
    for res in results:
        report(lab, res)
        assert res["meets_plan"] == (res["carries"] and res["delivery"] >= PLAN_DELIVERY
                                     and res["delay_p95_ms"] < PLAN_DELAY_MS), res
        if res["occupancy"] <= 1.0:
            assert res["carries"] and res["delivery"] == 1.0, res
            # The air was busy for the share the voice needs (the spurt plus the last packet's tail).
            assert res["busy_share"] == pytest.approx(res["occupancy"], rel=0.05), res
        else:
            assert not res["carries"], res
            assert res["delivery"] < 1.0 and res["queue_dropped"] > 0, res
            assert res["busy_share"] > 0.95, res
    carried = sum(r["carries"] for r in results)
    lab.log("voice_sweep", cases=len(results), carries=carried, failing=len(results) - carried,
            meets_plan=sum(r["meets_plan"] for r in results),
            seed=lab.seed, wall_s=round(time.monotonic() - t0, 2))
    assert 0 < carried < len(results), "the sweep should hold both outcomes"


def test_relay_doubles_the_air_and_adds_a_hop():
    c = Case("lora-sf7-bw500", 1200, 160, False)
    r = Case(c.profile, c.rate_bps, c.interval_ms, True)
    assert offered_occupancy(r) == pytest.approx(2 * offered_occupancy(c))
    d, rr = run(c, seed=1), run(r, seed=1)
    assert d["delivery"] == rr["delivery"] == 1.0
    assert rr["delay_mean_ms"] == pytest.approx(d["delay_mean_ms"] + d["airtime_ms"], abs=0.2)


def test_overloaded_setting_is_reported_failing(lab):
    """Known bad: SF11 at 125 kHz cannot carry 2400 bit/s, least of all through a relay. It must be
    reported as failing, with the queue overflowing and delivery under 100 %, not crash the run."""
    for relayed in (False, True):
        res = run(Case("lora-sf11-bw125", 2400, 160, relayed), seed=lab.seed)
        report(lab, res)
        assert res["occupancy"] > 1.0 and res["carries"] is False
        assert res["delivery"] < 1.0 / res["occupancy"] + 0.05
        assert res["queue_dropped"] > 0
    # Just over the line: a case at 100 % or less carries, and one a little over does not.
    over = [c for c in cases() if 1.0 < offered_occupancy(c) < 1.3]
    under = [c for c in cases() if 0.8 < offered_occupancy(c) <= 1.0]
    assert over and under
    assert not any(run(c, seed=lab.seed)["carries"] for c in over)
    assert all(run(c, seed=lab.seed)["delivery"] == 1.0 for c in under)


def test_same_seed_same_results(lab):
    """With channel loss on, the same seed gives the same numbers; another seed gives others.
    Cases that carry the voice, so loss is the only reason a packet goes missing."""
    picks = [Case("lora-sf7-bw250", 1200, 160, False), Case("lora-sf7-bw250", 1200, 160, True),
             Case("lora-sf9-bw500", 700, 320, True)]
    p = 0.1
    assert all(offered_occupancy(c) <= 1.0 for c in picks)
    a = [run(c, seed=lab.seed, loss=p, talk_s=120) for c in picks]
    b = [run(c, seed=lab.seed, loss=p, talk_s=120) for c in picks]
    other = [run(c, seed=lab.seed + 1, loss=p, talk_s=120) for c in picks]
    assert a == b
    assert a != other
    for res in a:
        hops = 2 if res["mode"] == "relayed" else 1
        want = (1 - p) ** hops
        sd = math.sqrt(res["packets"] * want * (1 - want))
        assert abs(res["delivered"] - res["packets"] * want) <= 5 * sd, res
        lab.log("voice_repeat", case=res["case"], loss=p, seed=lab.seed, delivery=res["delivery"],
                other_seed_delivery=other[a.index(res)]["delivery"])


class Relay(Endpoint):
    """An endpoint that sends on everything it hears (the relay radio)."""

    forward_to = None

    def _loop(self) -> None:
        import socket

        while not self._stop:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self.received.append((time.monotonic(), data))
            if self.forward_to:
                self.sock.sendto(data, self.forward_to)


@pytest.mark.parametrize("relayed", [False, True], ids=["direct", "relayed"])
def test_socket_channel_agrees(lab, relayed):
    """One case through the real channel (sockets, wall clock): every packet arrives, each one
    hop-airtimes after it was sent, as the simulated clock says. The checker re-checks the airtime
    from the capture. Payloads are random bytes the size of an OASA data packet."""
    case = Case("lora-sf7-bw500", 2400, 80, relayed)
    sim = run(case, seed=lab.seed, talk_s=3.0)
    hops = 2 if relayed else 1
    air = sim["airtime_ms"] / 1000
    n = sim["packets"]
    pkts = [bytes(lab.rng.getrandbits(8) for _ in range(case.packet_bytes)) for _ in range(n)]
    with Endpoint() as src, Endpoint() as dst, Relay() as rly:
        # Relayed: source -> A port -> relay; relay -> B port -> listener (one shared medium).
        # Direct: source -> A port -> listener; nothing goes the other way.
        a, b = (dst.addr, rly.addr) if relayed else (src.addr, dst.addr)
        with lab.channel(a=a, b=b, profile=case.profile) as ch:
            rly.forward_to = ch.addr_b
            sent = []
            t0 = time.monotonic()
            for k, p in enumerate(pkts):
                time.sleep(max(0.0, t0 + k * case.interval_ms / 1000 - time.monotonic()))
                sent.append(time.monotonic())
                src.send(p, ch.addr_a)
            dst.wait_idle(0.5, timeout=10)
        st = ch.stats()
    got = dst.received
    assert [d for _, d in got] == pkts, "every voice packet arrives, in order"
    lat = [t - s for (t, _), s in zip(got, sent)]
    mean = sum(lat) / n
    lab.log("voice_socket_check", case=case.name, packets=n, sim_delivery=sim["delivery"],
            mean_latency_ms=round(mean * 1000, 2), model_latency_ms=round(hops * air * 1000, 2))
    assert sim["delivery"] == 1.0
    assert hops * air - 0.002 <= mean <= hops * air + 0.03
    assert st["a_to_b"]["forwarded"] == n and st["b_to_a"]["forwarded"] == (n if relayed else 0)
    caps = read_pcap(lab.ctx.run_dir / lab.ctx.captures[-1]["file"])
    on_air = len(caps) * air
    span = n * case.interval_ms / 1000
    assert on_air / span == pytest.approx(sim["occupancy"], rel=0.02)
