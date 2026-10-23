"""The encrypted link over real (or simulated) LoRa modems on coax.

    app -> oasa-link -> oasa-radio bridge -> modem ~~ coax ~~ modem -> oasa-radio bridge -> oasa-link -> app

Needs the oasa-sec binaries in $OASA_SEC_BIN_DIR and `oasa-radio` in $OASA_RADIO_BIN_DIR. Without
them the test is SKIPPED with the reason, and a skipped suite does not pass the checker.

What it shows:
  - marked packets (and MAVLink heartbeats) go A to B and B to A, each delivered exactly once;
  - the handshake time and the goodput match the lora-sf7-bw125 airtime model (limits in suite.toml);
  - the frames each modem heard on the air (what anyone with a receiver gets) hold no marker, no
    MAVLink and look random: the checker scans them, as air captures;
  - the RTL-SDR recorded the exchange, and the checker lines its bursts up with the airtime.
"""

import time

import pytest

from oasa_lab.hwlink import LoraBench, LoraLinkRig, find_radio_bin, frame_airtime, frames_between
from oasa_lab.linkharness import MissingBinaries, find_bins
from oasa_lab.testing import mavlink2_heartbeat, wait_for

N = 28        # packets each way; a burst, so the radio is the bottleneck. Enough bytes per capture for the randomness test (5000)
SIZE = 200    # bytes of marked payload
MTU = 252     # oasa-link and the bridge agree; LoRa carries up to 255


@pytest.fixture
def tools(lab):
    try:
        return find_bins(lab), find_radio_bin(lab)
    except MissingBinaries as exc:
        pytest.skip(str(exc))


def payloads(marker: str, direction: str, n: int) -> list[bytes]:
    """Unique packets. Every seventh one is a MAVLink heartbeat, so the air scan has something to look for."""
    out = []
    tag = "AB" if direction == "a_to_b" else "BA"
    for i in range(n):
        if i % 7 == 6:
            out.append(mavlink2_heartbeat(i, sysid=1 if tag == "AB" else 2))
        else:
            out.append(f"{marker}|{tag}|{i:05d}|".encode().ljust(SIZE, b"."))
    return out


def test_modems_pass_packets_both_ways(lab, hw):
    """The radio alone, no link: a few packets each way arrive once each. Tells a radio fault from a link fault."""
    bench = LoraBench(lab, hw, capture_air=False)   # no capture: these packets are plain random bytes
    sent = {"A": [lab.rng.randbytes(60) for _ in range(5)], "B": [lab.rng.randbytes(60) for _ in range(5)]}
    for who, src in (("A", bench.a), ("B", bench.b)):
        for pkt in sent[who]:
            src.link.send_packet(pkt)
            time.sleep(1.2 * frame_airtime(bench.model, len(pkt)))
    bench.settle(0.8)
    assert sorted(f.data for f in bench.b.link.rx_packets()) == sorted(sent["A"])
    assert sorted(f.data for f in bench.a.link.rx_packets()) == sorted(sent["B"])


def test_encrypted_link_over_lora(lab, hw, tools, tmp_path):
    bins, radio = tools
    bench = LoraBench(lab, hw)
    marker = lab.marker()
    bench.start_recording()
    time.sleep(0.5)
    failures: list[str] = []

    def check(metric: str, name: str, value: float, unit: str = "") -> None:
        if not lab.measure(metric, name, value, unit):
            failures.append(f"{name} = {value:g} {unit} is outside its limits (see suite.toml)")

    with LoraLinkRig(lab, bench, bins, radio, tmp_path, mtu=MTU) as rig:
        up_mono, up_wall = rig.wait_established(60)
        handshake_s = up_mono - rig.t_start_mono
        hs_frames = frames_between(bench, rig.t_start_wall, up_wall + 0.05)
        hs_air = sum(frame_airtime(bench.model, n) for _, n in hs_frames)
        lab.log("link_metrics", phase="handshake", handshake_s=round(handshake_s, 3), frames=len(hs_frames),
                airtime_s=round(hs_air, 3), model="lora-sf7-bw125")
        check("handshake_excess_s", "handshake", handshake_s - hs_air, "s")

        for direction in ("a_to_b", "b_to_a"):          # one direction at a time: the air is half-duplex
            sent = payloads(marker, direction, N)
            t_mono, t_wall = rig.send(direction, sent)
            dst = rig.receiver(direction)
            wait_for(lambda: len(dst.received) >= N, timeout=N * frame_airtime(bench.model, MTU) * 4 + 30)
            got = [d for _, d in dst.received]
            span = (dst.received[-1][0] - t_mono) if dst.received else float("nan")
            bench.settle(1.0)
            frames = frames_between(bench, t_wall, t_wall + span + 0.05)
            model_s = sum(frame_airtime(bench.model, n) for _, n in frames)
            lab.log("link_metrics", phase=direction, sent=N, delivered=len(got), payload_bytes=sum(map(len, got)),
                    span_s=round(span, 3), model_s=round(model_s, 3), frames=len(frames),
                    goodput_bps=round(sum(map(len, got)) * 8 / span) if got else 0,
                    model_goodput_bps=round(sum(map(len, got)) * 8 / model_s) if model_s else 0)
            if sorted(got) != sorted(sent):
                failures.append(f"{direction}: sent {N}, delivered {len(got)} "
                                f"({len(set(got))} different); every packet must arrive exactly once")
            elif model_s and span == span:
                check("goodput_ratio", f"goodput {direction}", model_s / span, "")
        rig.finish()

    time.sleep(0.5)
    entry = bench.finish_recording("link-exchange", "lora-link")
    # our own quick look at the air; the checker repeats it from the bundle
    heard = [f.data for m in (bench.a, bench.b) for f in m.link.rx_packets()]
    lab.log("air_summary", frames=len(heard), recording=entry["meta"], bridge_stats=rig.stats.get("bridge_a"))
    if not heard:
        failures.append("the modems heard nothing")
    if any(marker.encode()[:12] in p for p in heard):
        failures.append("a marker is readable on the air")
    if any(p[:1] == b"\xfd" and len(p) < 40 for p in heard):
        failures.append("a short MAVLink-looking frame is readable on the air")
    assert not failures, "; ".join(failures)
