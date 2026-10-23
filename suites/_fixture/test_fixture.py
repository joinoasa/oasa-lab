"""A real, small run: random ("encrypted-looking") traffic through the channel."""

from oasa_lab.testing import Endpoint, make_packet


def test_random_traffic_through_channel(lab):
    lab.marker()  # recorded in the manifest; never sent
    with Endpoint() as a, Endpoint() as b:
        with lab.channel(a=a.addr, b=b.addr, loss=0.1, duplicate=0.05, delay_ms=1) as ch:
            sent = [make_packet(i, lab.rng, 64) for i in range(400)]
            a.send_paced(sent, ch.addr_a)
            b.wait_idle()
    assert 300 < len(b.received) < 450


def test_stranger_gets_nothing(lab):
    """Known-bad case: a third socket never sees traffic between A and B."""
    with Endpoint() as a, Endpoint() as b, Endpoint() as stranger:
        with lab.channel(a=a.addr, b=b.addr) as ch:
            a.send_paced([make_packet(i, lab.rng) for i in range(20)], ch.addr_a)
            b.wait_idle()
        stranger.wait_idle(0.1)
    assert len(b.received) == 20
    assert stranger.received == []
