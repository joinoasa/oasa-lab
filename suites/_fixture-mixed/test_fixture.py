"""One encrypted-looking channel and one plaintext MAVLink control channel in the same run."""

from oasa_lab.testing import Endpoint, make_packet, mavlink2_heartbeat


def test_random_traffic_through_channel(lab):
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, loss=0.1) as ch:
            a.send_paced([make_packet(i, lab.rng, 64) for i in range(400)], ch.addr_a)
            b.wait_idle()
    assert len(b.received) > 300


def test_control_carries_mavlink(lab):
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, control=True) as ch:
            a.send_paced([mavlink2_heartbeat(i) for i in range(300)], ch.addr_a)
            b.wait_idle()
    assert len(b.received) == 300
