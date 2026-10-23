"""Negative control for the air check, on real (or simulated) LoRa modems.

No encryption here, on purpose. Plaintext packets with a random marker and MAVLink 2 heartbeats go
both ways between two modems on coax. The frames each modem delivers are saved as air captures, and
the independent checker must FIND the marker and the MAVLink in them (and see that the bytes are not
random). If the air check were blind, this suite would fail the checker instead of passing it.

The RTL-SDR records the exchange as SigMF; the checker lines its bursts up with the airtime of the
frames, so the control also proves the recording path works.
"""

import time

from oasa_lab.hwlink import LoraBench, frame_airtime
from oasa_lab.testing import mavlink2_heartbeat


def packets(marker: str, who: str, n: int) -> list[bytes]:
    out = []
    for i in range(n):
        out.append(f"{who} telemetry seq={i:03d} lat=47.{i:04d} lon=8.5{i:03d} note={marker}".encode().ljust(80, b"."))
        out.append(mavlink2_heartbeat(i, sysid=1 if who == "A" else 2))
    return out


def test_plaintext_lora_packets_are_seen_on_the_air(lab, hw):
    bench = LoraBench(lab, hw)
    marker = lab.marker()
    n = 8
    sent = {"A": packets(marker, "A", n), "B": packets(marker, "B", n)}
    bench.start_recording()
    time.sleep(0.5)                                   # a little quiet before the first packet
    for who, src in (("A", bench.a), ("B", bench.b)):  # one direction at a time: the air is shared
        for pkt in sent[who]:
            src.link.send_packet(pkt)
            time.sleep(1.2 * frame_airtime(bench.model, len(pkt)))
        bench.settle(0.8)
    time.sleep(0.5)
    entry = bench.finish_recording("plaintext-exchange", "rf-control")
    got_b = [f.data for f in bench.b.link.rx_packets()]
    got_a = [f.data for f in bench.a.link.rx_packets()]
    lab.log("control_frames", heard_by_b=len(got_b), heard_by_a=len(got_a), recording=entry["meta"])
    # our own quick look; the checker repeats it independently from the bundle
    assert sorted(got_b) == sorted(sent["A"]), "modem B did not hear exactly what A sent, once each"
    assert sorted(got_a) == sorted(sent["B"]), "modem A did not hear exactly what B sent, once each"
    assert any(marker.encode() in p for p in got_a + got_b)
    assert any(p[:1] == b"\xfd" for p in got_a + got_b)
