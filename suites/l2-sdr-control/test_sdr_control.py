"""Negative control for l2-sdr-link's air check: plaintext over the same RF relay must be found by the checker."""

import tempfile
from pathlib import Path

import pytest

from oasa_lab.devices import Unavailable
from oasa_lab.hw import Hardware, WiringRefused
from oasa_lab.linkharness import HOST
from oasa_lab.sdrlink import RfRelay, probe_gains, radio_bin
from oasa_lab.testing import free_udp_port, mavlink2_heartbeat
from oasa_lab.w1test import TooStrong

RF_HZ = 915e6


def test_plaintext_frames_are_seen_on_the_air(lab, hw: Hardware):
    radio = radio_bin()
    if radio is None:
        pytest.skip("OASA_RADIO_BIN_DIR is not set or has no oasa-radio")
    try:
        hw.use_wiring()
        b200, e310 = hw.first("b200"), hw.first("e310")
    except (WiringRefused, Unavailable) as exc:
        pytest.skip(str(exc))
    lab.binary(radio)
    work = Path(tempfile.mkdtemp(prefix="sdr-control-"))
    try:
        gains = probe_gains(radio, b200, e310, RF_HZ, work)
    except TooStrong as exc:
        pytest.fail(str(exc))
    relay = RfRelay(radio, work, RF_HZ, {"a2b": (b200, e310, gains["a2b"][0]), "b2a": (e310, b200, gains["b2a"][0])},
                    a_dest=(HOST, free_udp_port()), b_dest=(HOST, free_udp_port()), log=lab.log).start()
    try:
        lab.air_capture(relay.legs["a2b"].air, e310.id, "b200-to-e310-plaintext")
        marker = lab.marker()
        for i in range(6):
            relay.inject("a2b", f"telemetry seq={i} lat=47.{i:04d} lon=8.5{i:03d} note={marker}".encode())
            relay.inject("a2b", mavlink2_heartbeat(i))
        relay.cycle("a2b")
    finally:
        relay.close()
    seen = [f.data for f in relay.legs["a2b"].air.frames]
    assert any(marker.encode() in p for p in seen), f"no plaintext frame came through ({len(seen)} frames)"
    assert any(p[:1] == b"\xfd" for p in seen), "no MAVLink heartbeat came through"
