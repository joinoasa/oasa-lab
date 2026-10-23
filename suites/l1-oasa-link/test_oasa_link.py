"""The oasa-sec link, run for real through the simulated channel.

Needs the binaries `oasa-pair` and `oasa-link` in $OASA_SEC_BIN_DIR. Without them every test is
SKIPPED with the reason (recorded in the results). A skipped suite does not pass the checker:
its known-bad cases never ran.
"""

import time

import pytest

from oasa_lab.channel import ChannelSettings
from oasa_lab.linkharness import HOST, LinkPair, LinkProc, MissingBinaries, find_bins, make_identity, trust
from oasa_lab.pcap import read_pcap
from oasa_lab.testing import Endpoint, free_udp_port, wait_for


@pytest.fixture
def bins(lab):
    try:
        return find_bins(lab)
    except MissingBinaries as exc:
        pytest.skip(str(exc))


def messages(marker, n, size=80):
    return [f"{marker}|{i:05d}|".encode().ljust(size, b".") for i in range(n)]


def capture_of(lab):
    return read_pcap(lab.ctx.run_dir / lab.ctx.captures[-1]["file"])


def test_delivery_under_loss_reorder_duplicate_replay(lab, bins, tmp_path):
    n = 1000
    marker = lab.marker()
    sent = messages(marker, n)
    with LinkPair(lab, bins, tmp_path, loss=0.10, reorder=0.10, duplicate=0.05, replay=0.05) as lp:
        lp.wait_established()
        lp.send_a_to_b(sent, batch=20, gap=0.01)
        lp.finish()
    got = lp.app_b.payloads()
    assert set(got) <= set(sent), "something was delivered that nobody sent"
    assert len(got) == len(set(got)), "a packet was delivered twice"
    assert len(got) >= 0.85 * n, f"only {len(got)} of {n} delivered"
    assert lp.stats["bob"]["delivered"] == len(got)


def test_captures_hold_no_plaintext(lab, bins, tmp_path):
    n = 300
    marker = lab.marker()
    with LinkPair(lab, bins, tmp_path, loss=0.05) as lp:
        lp.wait_established()
        lp.send_a_to_b(messages(marker, n), batch=20, gap=0.01)
        lp.finish()
    pk = capture_of(lab)
    assert len(pk) > n * 0.8, "the capture should hold the encrypted traffic"
    assert not any(marker.encode() in p.payload for p in pk)
    assert not any(marker.encode()[:12] in p.payload for p in pk)


def test_corrupted_packets_are_dropped_never_delivered(lab, bins, tmp_path):
    n = 800
    sent = messages(lab.marker(), n)
    with LinkPair(lab, bins, tmp_path, corrupt=0.05) as lp:
        lp.wait_established()
        lp.send_a_to_b(sent, batch=20, gap=0.01)
        lp.finish()
    got = lp.app_b.payloads()
    assert lp.link_b.drops().get("bad_tag", 0) > 0, "corrupted packets should be dropped as bad_tag"
    assert set(got) <= set(sent), "a corrupted packet was delivered"
    assert len(got) == len(set(got))
    assert len(got) < n


def test_untrusted_identity_gets_nothing(lab, bins, tmp_path):
    """Attacker test: a stranger's handshakes, sent straight at a link, get no reply at all."""
    alice = make_identity(bins, tmp_path, "alice")
    bob = make_identity(bins, tmp_path, "bob")
    mallory = make_identity(bins, tmp_path, "mallory")
    trust(bins, bob, alice)  # Bob trusts Alice only
    trust(bins, mallory, bob)  # Mallory knows Bob's key; Bob does not know hers
    b_bind, m_bind = (HOST, free_udp_port()), (HOST, free_udp_port())
    with Endpoint() as app_b, Endpoint() as app_m, Endpoint() as raw:
        b_plain, m_plain = (HOST, free_udp_port()), (HOST, free_udp_port())
        link_b = LinkProc(bins, bob, "alice", bind=b_bind, remote=None, initiate=False, plain_listen=b_plain,
                          plain_send=app_b.addr, log_path=lab.file("link-bob.jsonl"))
        link_m = LinkProc(bins, mallory, "bob", bind=m_bind, remote=b_bind, initiate=True, plain_listen=m_plain,
                          plain_send=app_m.addr, log_path=lab.file("link-mallory.jsonl"), duration=4)
        for i in range(10):  # plus raw junk from a third socket
            raw.send(bytes([i]) * 100, b_bind)
        time.sleep(4.5)
        m_stats = link_m.stop()
        b_stats = link_b.stop()
        raw.wait_idle(0.3)
        assert raw.received == [], "Bob answered a stranger's junk"
    assert not link_m.seen("session_established"), "Mallory got a session with Bob"
    assert not link_b.seen("session_established"), "Bob accepted an untrusted identity"
    assert m_stats is not None and m_stats["received"] == 0, "Bob sent packets back to Mallory"
    assert app_b.received == [] and app_m.received == []
    assert b_stats is not None and b_stats["delivered"] == 0


def test_rekey_keeps_delivering(lab, bins, tmp_path):
    secs, gap = 8, 0.05
    sent = messages(lab.marker(), int(secs / gap))
    with LinkPair(lab, bins, tmp_path, rekey_secs=3, duration=10, loss=0.02) as lp:
        t0 = lp.wait_established()
        for pkt in sent:
            lp.app_a.send(pkt, lp.a_plain)
            time.sleep(gap)
        lp.finish()
    got = lp.app_b.received
    assert set(d for _, d in got) <= set(sent)
    assert len(got) >= 0.8 * len(sent), f"{len(got)} of {len(sent)} delivered across rekeys"
    rekeys = len(lp.link_a.seen("rekey")) + len(lp.link_b.seen("rekey"))
    assert rekeys >= 2, f"expected rekeys over 10 s with --rekey-secs 3, saw {rekeys}"
    for w in range(4):  # every 2 s window of the sending period saw deliveries
        lo, hi = t0 + 2 * w, t0 + 2 * w + 2
        assert any(lo <= t < hi for t, _ in got), f"nothing delivered between {2 * w} s and {2 * w + 2} s"


@pytest.mark.parametrize("profile", ["lora-sf7-bw125", "sik-64k"])
def test_link_over_radio_profiles(lab, bins, tmp_path, profile):
    """Informational: the link over the airtime of a real radio. It asserts only that the link comes up
    and delivers. Handshake time and goodput go to the log as `link_metrics` events, to read and compare."""
    n, size = 20, 80
    s = ChannelSettings.for_profile(profile)
    mtu = min(s.mtu, 255)  # the link's largest encrypted packet: the radio's own limit (oasa-link default is 128)
    sent = messages(lab.marker(), n, size)
    with LinkPair(lab, bins, tmp_path, profile=profile, mtu=mtu) as lp:
        up = lp.wait_established()
        handshake_s = up - lp.t_start
        t0 = time.monotonic()
        lp.send_a_to_b(sent, batch=n)  # one burst: the radio is the bottleneck
        wait_for(lambda: len(lp.app_b.received) >= n, timeout=30)  # the radio is slow; don't stop it early
        lp.finish()
    got = lp.app_b.received
    assert set(d for _, d in got) <= set(sent), "something was delivered that nobody sent"
    assert got, "the link came up but delivered nothing"
    span = got[-1][0] - t0
    goodput_bps = sum(len(d) for _, d in got) * 8 / span
    wire = [p for p in capture_of(lab) if p.dst == lp.b_bind]
    lab.log("link_metrics", profile=profile, link_mtu=mtu, handshake_s=round(handshake_s, 3), sent=n,
            delivered=len(got), payload_bytes=size, burst_s=round(span, 3), goodput_bps=round(goodput_bps),
            wire_packets_a_to_b=len(wire), wire_bytes_a_to_b=sum(len(p.payload) for p in wire),
            airtime_64_byte_s=round(s.airtime_s(64), 4))
