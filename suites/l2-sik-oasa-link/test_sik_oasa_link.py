"""R-COM2 over the R-COM1 radio: the encrypted link over two SiK radios on cable.

    app -> oasa-link -> oasa-radio bridge --sik-expect -> pty -> tap -> SiK A ~~ cable ~~ SiK B -> ... -> app

Needs the oasa-sec binaries in $OASA_SEC_BIN_DIR and `oasa-radio` (with `--sik-expect`) in $OASA_RADIO_BIN_DIR.
Without them the test is SKIPPED with the reason, and a skipped suite does not pass the checker.

What it shows:
  - both bridges checked their radio's settings before they started (H1);
  - marked packets (and MAVLink heartbeats) go A to B and B to A, each delivered exactly once;
  - the handshake time and the goodput match the sik-64k model (limits in suite.toml);
  - the frames each radio delivered (what anyone with a SiK radio on the same settings gets) hold no marker,
    no MAVLink and look random: the checker scans them, as air captures.

No RTL-SDR recording: SiK hops over 50 channels across 915 to 928 MHz by default, wider than an RTL-SDR
sees at once, so a recording could not be lined up with the frames (docs/hardware-suites.md).
"""

import subprocess
import time

import pytest

from oasa_lab.hwlink import LoraLinkRig, find_radio_bin, frames_between
from oasa_lab.linkharness import MissingBinaries, find_bins
from oasa_lab.siklink import SikBench
from oasa_lab.testing import mavlink2_heartbeat, wait_for

N = 28        # packets each way; enough bytes per capture for the randomness test
SIZE = 200    # bytes of marked payload; with oasa-link's header and tag it fits the bridge's MTU (242)


@pytest.fixture
def tools(lab):
    try:
        bins, radio = find_bins(lab), find_radio_bin(lab)
    except MissingBinaries as exc:
        pytest.skip(str(exc))
    r = subprocess.run([str(radio), "sik", "--help"], capture_output=True, text=True, timeout=10)
    if "--sik-expect" not in r.stdout:
        pytest.skip("this oasa-radio has no SiK settings check (sik, --sik-expect); build oasa-radio with it")
    return bins, radio


def payloads(marker: str, direction: str, n: int) -> list[bytes]:
    out = []
    tag = "AB" if direction == "a_to_b" else "BA"
    for i in range(n):
        if i % 7 == 6:
            out.append(mavlink2_heartbeat(i, sysid=1 if tag == "AB" else 2))
        else:
            out.append(f"{marker}|{tag}|{i:05d}|".encode().ljust(SIZE, b"."))
    return out


def test_encrypted_link_over_sik(lab, hw, tools, tmp_path):
    bins, radio = tools
    bench = SikBench(lab, hw, capture_air=True)
    mtu = bench.a.mtu
    marker = lab.marker()
    failures: list[str] = []

    def check(metric: str, name: str, value: float, unit: str = "") -> None:
        if not lab.measure(metric, name, value, unit):
            failures.append(f"{name} = {value:g} {unit} is outside its limits (see suite.toml)")

    with LoraLinkRig(lab, bench, bins, radio, tmp_path, mtu=mtu, baud=bench.a.baud,
                     bridge_extra=bench.bridge_extra()) as rig:
        for name, br in (("a", rig.bridge_a), ("b", rig.bridge_b)):
            chk = br.seen("sik_check")
            assert chk and chk[-1].get("ok") is True, f"bridge {name}: settings check {chk}"
        up_mono, up_wall = rig.wait_established(60)
        handshake_s = up_mono - rig.t_start_mono
        hs = frames_between(bench, rig.t_start_wall, up_wall + 0.05)
        floor = sum(bench.latency_s(n) for _, n in hs)
        lab.log("link_metrics", phase="handshake", handshake_s=round(handshake_s, 3), frames=len(hs),
                floor_s=round(floor, 3), model="sik-64k")
        check("handshake_excess_s", "handshake", handshake_s - floor, "s")

        for direction in ("a_to_b", "b_to_a"):
            sent = payloads(marker, direction, N)
            t_mono, t_wall = rig.send(direction, sent)
            dst = rig.receiver(direction)
            wait_for(lambda: len(dst.received) >= N, timeout=N * bench.latency_s(mtu) * 4 + 30)
            got = [d for _, d in dst.received]
            span = (dst.received[-1][0] - t_mono) if dst.received else float("nan")
            bench.settle(1.0)
            frames = frames_between(bench, t_wall, t_wall + span + 0.05)
            model_s = sum(bench.busy_s(n) for _, n in frames)
            lab.log("link_metrics", phase=direction, sent=N, delivered=len(got), payload_bytes=sum(map(len, got)),
                    span_s=round(span, 3), model_s=round(model_s, 3), frames=len(frames),
                    goodput_bps=round(sum(map(len, got)) * 8 / span) if got else 0)
            if sorted(got) != sorted(sent):
                failures.append(f"{direction}: sent {N}, delivered {len(got)} "
                                f"({len(set(got))} different); every packet must arrive exactly once")
            elif model_s and span == span:
                check("goodput_ratio", f"goodput {direction}", model_s / span, "")
            dst.received.clear()
        rig.finish()

    heard = [f.data for r in (bench.a, bench.b) for f in r.link.rx_packets()]
    lab.log("air_summary", frames=len(heard), bridge_stats=rig.stats.get("bridge_a"))
    if not heard:
        failures.append("the radios delivered nothing")
    if any(marker.encode()[:12] in p for p in heard):
        failures.append("a marker is readable in what the radios delivered")
    if any(p[:1] == b"\xfd" and len(p) < 40 for p in heard):
        failures.append("a short MAVLink-looking frame is readable in what the radios delivered")
    assert not failures, "; ".join(failures)
