"""The encrypted oasa-sec link over the SDR bench. Limits and reasons: suite.toml. Design: docs/sdr-link.md.

Alice's link sits on the B200mini side, Bob's on the E310 side. Every datagram between them goes through the RF
relay (oasa_lab/sdrlink.py): a frame v1 on the `usrp` cables, B200 TX/RX to E310 RX2-B one way, E310 TRX-A to
B200 RX2 the other. The tests share one bench session, in file order.
"""

import tempfile
import time
from pathlib import Path

import pytest

from oasa_lab.devices import Unavailable
from oasa_lab.hw import Hardware, WiringRefused
from oasa_lab.linkharness import HOST, LinkProc, MissingBinaries, find_bins, make_identity, trust
from oasa_lab.sdrlink import RfRelay, probe_gains, radio_bin
from oasa_lab.testing import Endpoint, free_udp_port
from oasa_lab.w1test import TooStrong

RF_HZ = 915e6
MTU = 200
LINK_EXTRA = ["--keepalive-secs", "5"]
N_A2B, N_B2A = 12, 6
MSG_LEN = 80
DATA_LEN = MSG_LEN + 22      # one message as a data packet: 22 bytes of link overhead with the 2-byte counter
STATE: dict = {}


def drops(proc: LinkProc, reason: str) -> int:
    return sum(1 for e in proc.seen("drop") if e.get("reason") == reason)


@pytest.fixture
def bench(lab, hw: Hardware):
    if "relay" in STATE:
        # The hw fixture opens the radios for each test and closes them after it (a closed USRP has no temp
        # folder left), so the relay gets this test's radios. Links, identities and the relay stay up.
        try:
            hw.use_wiring()
            b200, e310 = hw.first("b200"), hw.first("e310")
        except (WiringRefused, Unavailable) as exc:
            pytest.skip(str(exc))
        relay = STATE["relay"]
        relay.legs["a2b"].tx, relay.legs["a2b"].rx = b200, e310
        relay.legs["b2a"].tx, relay.legs["b2a"].rx = e310, b200
        STATE.update(b200=b200, e310=e310)
        yield STATE
        return
    try:
        sec = find_bins(lab)
    except MissingBinaries as exc:
        pytest.skip(str(exc))
    radio = radio_bin()
    if radio is None:
        pytest.skip("OASA_RADIO_BIN_DIR is not set or has no oasa-radio")
    try:
        hw.use_wiring()
        b200, e310 = hw.first("b200"), hw.first("e310")
    except (WiringRefused, Unavailable) as exc:
        pytest.skip(str(exc))
    lab.binary(radio)
    work = Path(tempfile.mkdtemp(prefix="sdr-link-"))
    try:
        gains = probe_gains(radio, b200, e310, RF_HZ, work)
    except TooStrong as exc:
        pytest.fail(str(exc))
    for d, (g, ebn0) in gains.items():
        lab.log("probe", direction=d, tx_gain_db=g, ebn0_db=ebn0)
        assert ebn0 is not None and lab.measure("probe_ebn0_db", f"probe {d}", round(ebn0, 2), "dB"), \
            f"{d}: no clear frames at TX gain {g:g} dB"
    alice, bob = make_identity(sec, work, "alice"), make_identity(sec, work, "bob")
    trust(sec, alice, bob)
    trust(sec, bob, alice)
    app_a, app_b = Endpoint(), Endpoint()
    a_bind, b_bind = (HOST, free_udp_port()), (HOST, free_udp_port())
    a_plain, b_plain = (HOST, free_udp_port()), (HOST, free_udp_port())
    relay = RfRelay(radio, work, RF_HZ, {"a2b": (b200, e310, gains["a2b"][0]), "b2a": (e310, b200, gains["b2a"][0])},
                    a_dest=a_bind, b_dest=b_bind, log=lab.log).start()
    lab.air_capture(relay.legs["a2b"].air, e310.id, "b200-to-e310")
    lab.air_capture(relay.legs["b2a"].air, b200.id, "e310-to-b200")
    link_b = LinkProc(sec, bob, "alice", bind=b_bind, remote=relay.addr_b, initiate=False, plain_listen=b_plain,
                      plain_send=app_b.addr, log_path=lab.file("link-bob.jsonl"), mtu=MTU, extra=LINK_EXTRA)
    t0 = time.monotonic()
    link_a = LinkProc(sec, alice, "bob", bind=a_bind, remote=relay.addr_a, initiate=True, plain_listen=a_plain,
                      plain_send=app_a.addr, log_path=lab.file("link-alice.jsonl"), mtu=MTU, extra=LINK_EXTRA)
    STATE.update(relay=relay, link_a=link_a, link_b=link_b, app_a=app_a, app_b=app_b, a_plain=a_plain,
                 b_plain=b_plain, b200=b200, e310=e310, sec=sec, work=work, bob=bob, t0=t0)
    yield STATE


@pytest.fixture(scope="module", autouse=True)
def _teardown():
    yield
    for k in ("link_a", "link_b", "mallory"):
        if k in STATE:
            STATE[k].stop()
    for k in ("app_a", "app_b"):
        if k in STATE:
            STATE[k].close()
    if "relay" in STATE:
        STATE["relay"].close()


def test_encrypted_link_over_the_bench(lab, bench):
    relay, link_a, link_b = bench["relay"], bench["link_a"], bench["link_b"]
    up = relay.run(lambda: bool(link_a.seen("session_established")), timeout=420)
    hs = round(time.monotonic() - bench["t0"], 1)
    assert up, "no session over the relay in 420 s; see logs/link-*.jsonl and the relay_cycle events"
    assert lab.measure("handshake_s", "handshake over the relay", hs, "s"), f"{hs} s"
    marker = lab.marker()
    a2b = [f"{marker}|a2b|{i:03d}|".encode().ljust(MSG_LEN, b".") for i in range(N_A2B)]
    b2a = [f"{marker}|b2a|{i:03d}|".encode().ljust(MSG_LEN, b".") for i in range(N_B2A)]
    # Alice first: per the link spec the responder (Bob) sends on a new session only after a data packet from the
    # initiator arrives on it (anything Bob sends before that is dropped as no_session, by design).
    bench["app_a"].send_paced(a2b, bench["a_plain"], batch=4, gap=0.05)
    relay.run(lambda: len(bench["app_b"].payloads()) >= N_A2B, timeout=180)
    bench["app_b"].send_paced(b2a, bench["b_plain"], batch=4, gap=0.05)
    relay.run(lambda: len(bench["app_a"].payloads()) >= N_B2A, timeout=180)
    got_b, got_a = bench["app_b"].payloads(), bench["app_a"].payloads()
    for got, sent, name in ((got_b, a2b, "a2b"), (got_a, b2a, "b2a")):
        assert set(got) <= set(sent), f"{name}: something was delivered that nobody sent"
        assert len(got) == len(set(got)), f"{name}: a message was delivered twice"
    ok = [lab.measure("delivery_a2b", "Alice to Bob", round(len(got_b) / N_A2B, 3)),
          lab.measure("delivery_b2a", "Bob to Alice", round(len(got_a) / N_B2A, 3))]
    tot = {d: relay.totals(d) for d in ("a2b", "b2a")}
    lab.log("relay_totals", **tot)
    sent = sum(t["sent"] for t in tot.values())
    found = sum(t["found"] for t in tot.values())
    ok.append(lab.measure("frame_delivery", "frames decoded / sent", round(found / sent, 3) if sent else 0.0))
    for d, leg in relay.legs.items():
        assert not any(marker.encode() in f.data for f in leg.air.frames), f"{d}: the marker is readable on the air"
        if leg.recording is not None:
            iq = leg.recording
            lab.sigmf(f"sdr-link-{d}", iq.data, datatype=iq.datatype, sample_rate=iq.sample_rate,
                      center_hz=iq.center_hz, start_time=getattr(iq, "start_time", time.time()), device=leg.rx.id,
                      purpose=f"sdr link {d}",
                      description=f"last relay cycle {d}: {leg.tx.id} {leg.tx.tx_port} to {leg.rx.id} {leg.rx.rx_port}, "
                                  f"frame v1 BPSK at IF +250 kHz, TX gain {leg.gain_db:g} dB; encrypted link packets")
    assert all(ok)


def _carried_data(relay: RfRelay) -> bytes:
    """A data packet Alice sent that the relay carried: the last one exactly the size of a message. (Not just "a big
    one": on the bench Alice also sends 119-byte handshake initiations after the session is up, and replaying one
    of those tests the handshake's replay rule, `stale_handshake`, not the data path's.)"""
    data = [p for p in relay.legs["a2b"].carried if len(p) == DATA_LEN]
    if not data:
        pytest.fail("no data packet was carried a2b to work with")
    return data[-1]


def test_replayed_packet_is_dropped(lab, bench):
    relay, link_b, app_b = bench["relay"], bench["link_b"], bench["app_b"]
    before, n = drops(link_b, "replay"), len(app_b.payloads())
    relay.inject("a2b", _carried_data(relay))
    relay.run(lambda: drops(link_b, "replay") > before, timeout=90)
    got = drops(link_b, "replay") - before
    assert len(app_b.payloads()) == n, "the replayed packet was delivered"
    assert lab.measure("replay_dropped", "replay drops at Bob", got), "Bob did not drop the replay"


def test_changed_packet_is_dropped(lab, bench):
    relay, link_b, app_b = bench["relay"], bench["link_b"], bench["app_b"]
    p = bytearray(_carried_data(relay))
    p[-1] ^= 0x01                       # inside the authentication tag
    before, n = drops(link_b, "bad_tag"), len(app_b.payloads())
    relay.inject("a2b", bytes(p))
    relay.run(lambda: drops(link_b, "bad_tag") > before, timeout=90)
    got = drops(link_b, "bad_tag") - before
    assert len(app_b.payloads()) == n, "the changed packet was delivered"
    assert lab.measure("bad_tag_dropped", "bad_tag drops at Bob", got), "Bob did not drop the changed packet"


def test_untrusted_identity_gets_no_session(lab, bench):
    """Mallory knows Bob's public key, but Bob does not trust Mallory. Alice steps aside; Mallory takes her place
    on the B200 side of the relay."""
    relay, link_b = bench["relay"], bench["link_b"]
    bench["link_a"].stop()
    mallory = make_identity(bench["sec"], bench["work"], "mallory")
    trust(bench["sec"], mallory, bench["bob"])
    m_bind = (HOST, free_udp_port())
    relay.dest["b2a"] = m_bind
    before = drops(link_b, "untrusted_peer")
    proc = LinkProc(bench["sec"], mallory, "bob", bind=m_bind, remote=relay.addr_a, initiate=True,
                    plain_listen=(HOST, free_udp_port()), plain_send=(HOST, free_udp_port()),
                    log_path=lab.file("link-mallory.jsonl"), mtu=MTU, extra=LINK_EXTRA)
    STATE["mallory"] = proc
    relay.run(lambda: drops(link_b, "untrusted_peer") > before, timeout=120)
    relay.run(lambda: False, timeout=20)     # time for any answer to come back over the air
    got = drops(link_b, "untrusted_peer") - before
    assert not proc.seen("session_established"), "Mallory got a session"
    assert lab.measure("untrusted_dropped", "untrusted_peer drops at Bob", got), "Bob never saw Mallory's handshake"
