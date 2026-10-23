"""The hardware layer, on its simulated backends: framing, inventory, hub, modems, SDRs, and the
acceptance measurements catching a deliberately bad Pluto. Nothing here needs hardware."""

import os
import random
import time
import tomllib
from pathlib import Path

import pytest

from oasa_lab.devices import (DeviceError, Inventory, InventoryError, Pluto, RtlSdr, SerialRadio, SimHub, SimWorld,
                              Unavailable, TxRefused, make_device)
from oasa_lab.devices.framing import FrameDecoder, cobs_decode, cobs_encode, crc16, encode_frame
from oasa_lab.devices.inventory import DEFAULT_SIM
from oasa_lab.iq import IQ, cf32_bytes, tone_report
from oasa_lab.plutotest import TooStrong, error_vs_rtl, loopback, ramp_tone
from oasa_lab.airtime import airtime_s
from oasa_lab.suites import load_suite, suites_dir

ROOT = Path(__file__).resolve().parents[2]
LIMITS = load_suite(suites_dir() / "l2-pluto-acceptance").limits


def inside(metric, value):
    lim = LIMITS[metric]
    return lim.get("min", float("-inf")) <= value <= lim.get("max", float("inf"))


@pytest.fixture
def bench():
    inv = Inventory.from_dict(DEFAULT_SIM, "test", force_sim=True)
    world = SimWorld(1, inv.wiring)
    hub = SimHub(inv.hub)
    made = []

    def get(dev_id, **sim):
        if sim:
            inv.devices[dev_id] = {**inv.devices[dev_id], "sim": {**inv.devices[dev_id].get("sim", {}), **sim}}
        d = make_device(dev_id, inv, hub, world).open()
        made.append(d)
        return d
    get.inv, get.world, get.hub = inv, world, hub
    yield get
    for d in made:
        d.close()
    world.close()


# --- framing -------------------------------------------------------------------------------------------
def test_framing_matches_the_spec_vectors():
    assert crc16(b"123456789") == 0x29B1
    assert encode_frame(bytes([1, 2, 3])).hex() == "00010703010203ebe100"   # example in oasa-radio serial-framing.md
    assert cobs_decode(cobs_encode(bytes([0, 0, 1, 0]))) == bytes([0, 0, 1, 0])


def test_framing_round_trip_and_bad_frames():
    rng = random.Random(3)
    dec = FrameDecoder(252)
    for n in (1, 2, 100, 252):
        pkt = bytes(rng.choice([0, 1, 255, rng.getrandbits(8)]) for _ in range(n))
        frame = encode_frame(pkt)
        got = []
        for i in range(0, len(frame), 5):          # arrives in small pieces
            got += dec.feed(frame[i:i + 5])
        assert got == [pkt]
    bad = bytearray(encode_frame(b"hello world"))
    bad[5] ^= 0x04
    assert dec.feed(bytes(bad) + encode_frame(b"after")) == [b"after"]   # one bad frame never hurts the next
    assert dec.bad_frames == 1
    with pytest.raises(ValueError):
        encode_frame(b"x" * 253)


# --- inventory -----------------------------------------------------------------------------------------
def test_example_inventory_is_valid_and_matches_the_built_in_sim_bench():
    inv = Inventory.load(ROOT / "lab.example.toml", sim=False)
    assert inv.backend == "hardware" and set(inv.devices) == set(DEFAULT_SIM["devices"])
    assert {"pluto", "lora"} <= set(inv.wiring)
    assert inv.has("serial-radio:2") and inv.has("hub") and not inv.has("serial-radio:3")
    assert Inventory.load(ROOT / "lab.example.toml", sim=True).backend_of("pluto0") == "sim"
    for suite in ("l2-pluto-acceptance", "l2-lora-link", "l2-rf-control"):
        s = load_suite(suites_dir() / suite)
        assert s.transmits and s.wiring in inv.wiring and not inv.missing(s.requires), suite


def test_missing_hardware_is_named(tmp_path):
    f = tmp_path / "lab.toml"
    f.write_text('[devices.rtl0]\nkind = "rtlsdr"\n')
    inv = Inventory.load(f, sim=False)
    assert inv.missing(["pluto", "rtlsdr", "serial-radio:2"]) == ["pluto", "serial-radio:2"]


def test_bad_inventory_is_refused(tmp_path):
    for text in ('[devices.x]\nkind = "toaster"\n', '[wiring.w]\nrf_setup = "over-the-air"\n',
                 '[lab]\nbackend = "maybe"\n'):
        f = tmp_path / "lab.toml"
        f.write_text(text)
        with pytest.raises(InventoryError):
            Inventory.load(f, sim=False)


# --- hub -----------------------------------------------------------------------------------------------
def test_sim_hub_powers_a_device_off_and_back(bench):
    pluto = bench("pluto0")
    bench.hub.set_power(pluto.hub_port, False)
    pluto.close()
    with pytest.raises(Unavailable):
        pluto.open()
    bench.hub.set_power(pluto.hub_port, True)
    secs = pluto.power_cycle()
    assert pluto.is_open and bench.hub.cycles == [pluto.hub_port] and secs < 5


# --- modems and the simulated air ----------------------------------------------------------------------
def test_sim_air_takes_real_airtime_and_is_half_duplex(bench):
    a, b = bench("heltec_a"), bench("heltec_b")
    model = a.airtime_settings()
    pkt = bytes(range(1, 101))
    t0 = time.time()
    a.link.send_packet(pkt)
    b.link.send_packet(pkt[::-1])        # both talk at once; the medium is shared, they go one after the other
    time.sleep(1.2)
    heard_b, heard_a = b.link.rx_packets(), a.link.rx_packets()
    assert [f.data for f in heard_b] == [pkt] and [f.data for f in heard_a] == [pkt[::-1]]
    air = airtime_s(model, 100)
    first, second = sorted([heard_b[0].ts, heard_a[0].ts])
    assert first - t0 >= air                       # nothing arrives before its airtime
    assert second - first >= air * 0.95            # the second waited for the medium


def test_modems_with_different_settings_do_not_hear_each_other(bench):
    a = bench("heltec_a")
    b = make_device("heltec_b", _with_sf(bench.inv, "heltec_b", 9), bench.hub, bench.world).open()
    try:
        a.link.send_packet(b"x" * 20)
        time.sleep(0.4)
        assert b.link.rx_packets() == []
    finally:
        b.close()


def _with_sf(inv, dev_id, sf):
    import copy
    inv = copy.deepcopy(inv)
    inv.devices[dev_id]["radio"] = {**inv.devices[dev_id]["radio"], "profile": f"lora-sf{sf}-bw125"}
    return inv


def test_modem_tap_shows_a_pty_to_a_bridge(bench):
    a, b = bench("heltec_a"), bench("heltec_b")
    path = a.link.attach_pty()
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    try:
        os.write(fd, encode_frame(b"through the pty"))
        time.sleep(0.5)
        assert [f.data for f in b.link.rx_packets()] == [b"through the pty"]
        assert [f.data for f in a.link.tx] == [b"through the pty"]     # the tap saw it go out
    finally:
        os.close(fd)


# --- measuring tones -----------------------------------------------------------------------------------
def test_tone_report_finds_frequency_and_level():
    import cmath
    import math
    fs, f = 1.024e6, 100_123.4
    rng = random.Random(5)
    x = [0.1 * cmath.exp(2j * math.pi * f * k / fs) + complex(rng.gauss(0, 0.001), rng.gauss(0, 0.001)) for k in range(16384)]
    rep = tone_report(IQ(cf32_bytes(x), "cf32_le", fs, 0.0))
    assert abs(rep["freq_hz"] - f) < 3 and abs(rep["tone_dbfs"] + 20) < 0.3 and 33 < rep["snr_db"] < 41


# --- the acceptance catches a bad clone ----------------------------------------------------------------
def test_a_good_pluto_passes_the_limits(bench):
    pluto, rtl = bench("pluto0"), bench("rtl0")
    r = error_vs_rtl(pluto, rtl, 915e6)
    assert inside("freq_error_ppm_vs_rtl", r.error_ppm), r.error_ppm
    assert inside("freq_error_ppm_vs_spec", r.pluto_ppm), r.pluto_ppm
    lb = loopback(pluto, 1, 915e6, 63.5)
    assert inside("rx_level_vs_expected_db", lb.tone["tone_dbfs"] - lb.expected_dbfs)
    assert inside("tone_snr_db", lb.tone["snr_db"]) and inside("loopback_offset_error_ppm", lb.offset_error_ppm)


def test_acceptance_catches_a_plain_crystal(bench):
    """A clone with a 22 ppm crystal instead of the promised 0.5 ppm TCXO: the loopback cannot see it
    (one crystal drives both LOs), the comparison with the RTL-SDR does."""
    pluto, rtl = bench("pluto0", xo_ppm=22.0), bench("rtl0")
    assert inside("loopback_offset_error_ppm", loopback(pluto, 1, 433e6, 63.5).offset_error_ppm)
    for f in (433e6, 915e6):
        r = error_vs_rtl(pluto, rtl, f)
        assert not inside("freq_error_ppm_vs_rtl", r.error_ppm)
        assert not inside("freq_error_ppm_vs_spec", r.pluto_ppm)


def test_acceptance_catches_a_weak_port(bench):
    """RX2 hears 12 dB less than it should: against the expected level, and against the other port."""
    pluto = bench("pluto0")
    bench.world.extra_loss_db["pluto0:rx2"] = 12.0
    good, weak = loopback(pluto, 1, 915e6, 63.5), loopback(pluto, 2, 915e6, 60.0)
    assert inside("rx_level_vs_expected_db", good.tone["tone_dbfs"] - good.expected_dbfs)
    assert not inside("rx_level_vs_expected_db", weak.tone["tone_dbfs"] - weak.expected_dbfs)
    spread = abs((good.tone["tone_dbfs"] + 63.5) - (weak.tone["tone_dbfs"] + 60.0))
    assert not inside("port_level_spread_db", spread)          # caught even with no calibration


def test_acceptance_catches_a_dead_port_and_a_leaky_one(bench):
    pluto = bench("pluto0")
    bench.world.extra_loss_db["pluto0:rx1"] = 60.0
    dead = loopback(pluto, 1, 433e6, 63.5)
    assert not inside("rx_level_dbfs", dead.tone["tone_dbfs"]) and not inside("tone_snr_db", dead.tone["snr_db"])


def test_acceptance_catches_a_limited_tuning_range_and_a_missing_channel(bench):
    pluto = bench("pluto0", min_hz=325e6, max_hz=3.8e9, channels=1)
    with pytest.raises(DeviceError):
        pluto.tune(70e6)
    with pytest.raises(DeviceError):
        pluto.tune(6e9)
    assert pluto.info()["channels"] == 1
    with pytest.raises(DeviceError):
        pluto.tx_tone(2, 915e6, 1e5, -10, 0.5, 2e6)


def test_ramp_stops_before_full_power_when_the_cable_is_wrong(bench):
    """TX wired straight into RX (no attenuator): the very first, weakest step already reads too strong."""
    pluto = bench("pluto0")
    bench.world.extra_loss_db["pluto0:rx1"] = -50.0     # negative loss = the attenuators are missing
    with pytest.raises(TooStrong):
        ramp_tone(pluto, 1, 915e6)
    assert not bench.world.tones, "the transmitter must be off again"
    bench.world.extra_loss_db.clear()
    ramp_tone(pluto, 1, 915e6)
    assert "pluto0:tx1" in bench.world.tones
    pluto.tx_off()


def test_real_backends_report_missing_tools_instead_of_crashing(monkeypatch):
    inv = Inventory.from_dict({"devices": {"p": {"kind": "pluto", "uri": "ip:1.2.3.4"}, "r": {"kind": "rtlsdr"},
                                           "m": {"kind": "serial-radio", "port": "/dev/does-not-exist"}}}, "t")
    monkeypatch.setenv("PATH", "/nonexistent")
    for dev_id in ("p", "r", "m"):
        with pytest.raises((Unavailable, DeviceError)):
            make_device(dev_id, inv, None, None).open()


def test_real_serial_backend_against_a_pseudo_terminal(tmp_path):
    """The real (non-simulated) modem path: pyserial, the tap, the frame decoder. A thread plays the modem on
    the other end of a pty and answers every packet with its reverse."""
    pytest.importorskip("serial")
    import pty
    import threading
    import tty

    master, slave = pty.openpty()
    tty.setraw(master)
    path = os.ttyname(slave)
    stop = threading.Event()

    def fake_modem():
        dec = FrameDecoder(252)
        os.set_blocking(master, False)
        while not stop.is_set():
            try:
                data = os.read(master, 4096)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            for pkt in dec.feed(data):
                os.write(master, encode_frame(pkt[::-1]))
    t = threading.Thread(target=fake_modem, daemon=True)
    t.start()
    inv = Inventory.from_dict({"devices": {"m": {"kind": "serial-radio", "port": path, "serial": "T-1",
                                                 "firmware": "fake 1.0", "baud": 115200}}}, "t")
    modem = make_device("m", inv, None, None)
    assert modem.backend == "hardware"
    with pytest.raises(TxRefused), modem:  # real hardware with no suite behind it: nothing goes out
        modem.link.send_packet(b"hello")
    modem.tx_gate = lambda: None  # a unit test standing in for a suite that declared transmits and confirmed wiring
    with modem:
        info = modem.info()
        assert info["serial"] == "T-1" and info["firmware"] == "fake 1.0" and info["radio"]["freq_mhz"] == 915.0
        modem.link.send_packet(b"hello")
        modem.link.send_packet(bytes(range(200)))
        modem.link.wait_idle(0.3, 5)
        assert [f.data for f in modem.link.rx_packets()] == [b"olleh", bytes(range(200))[::-1]]
        assert modem.link.dec_rx.bad_frames == 0
    stop.set()
    t.join(1)
    os.close(master)
    os.close(slave)
