"""Negative control for checker step 4.

Plaintext UDP carrying a random marker and MAVLink 2 heartbeats goes through the channel. The
capture is declared `encrypted: false, expect_plaintext: true`, so the checker must FIND the
marker and the MAVLink frames, and see that the bytes are not random. If the scanner were
broken, this suite would fail the checker, not pass it.
"""

from oasa_lab.pcap import read_pcap
from oasa_lab.testing import Endpoint, mavlink2_heartbeat


def test_checker_input_is_plaintext(lab):
    marker = lab.marker()
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, loss=0.1, delay_ms=2, jitter_ms=1) as ch:
            pkts = []
            for i in range(300):
                text = f"telemetry seq={i} lat=47.{i:04d} lon=8.5{i:03d} alt={400 + i % 50} note={marker}"
                pkts.append(text.encode())
                pkts.append(mavlink2_heartbeat(i))
            a.send_paced(pkts, ch.addr_a)
            b.wait_idle()
    # Our own quick look at the capture. The checker repeats this independently.
    seen = [p.payload for p in read_pcap(lab.ctx.run_dir / lab.ctx.captures[-1]["file"])]
    assert any(marker.encode() in p for p in seen)
    assert any(p[:1] == b"\xfd" for p in seen)
    assert len(b.received) > 400
