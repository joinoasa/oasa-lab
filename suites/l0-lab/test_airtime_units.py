"""Unit tests for the airtime formulas, the profiles and the transmit scheduler (no sockets, no clock)."""

import itertools
import subprocess
import sys

import pytest

from oasa_lab import airtime as lab_air
from oasa_lab.channel import STAT_KEYS, Air, ChannelSettings, Direction
from oasa_lab.checker import air as checker_air
from oasa_lab.profiles import PROFILES, describe, profile_names, profile_settings


def semtech_driver_ms_numerator(pld, sf, bw, cr, pre, explicit, crc, ldro):
    """A line-by-line port of sx126x_get_lora_time_on_air_numerator (Semtech SX126x driver, C integers).
    Returns seconds."""
    cr_denom = cr + 4
    num = (pld << 3) + (16 if crc else 0) - 4 * sf + (20 if explicit else 0)
    if sf <= 6:
        den = 4 * sf
    else:
        num += 8
        den = 4 * (sf - 2) if ldro else 4 * sf
    if num < 0:
        num = 0
    intermed = ((num + den - 1) // den) * cr_denom + pre + 12
    if sf <= 6:
        intermed += 2
    return (4 * intermed + 1) * (1 << (sf - 2)) / bw


def test_lora_known_answers():
    """SF7/SF12, 125 kHz, CR 4/5, 8 preamble symbols, explicit header, CRC. Worked by hand from the Semtech
    formula. The SF12 value for 13 bytes also agrees with the 1.16 s quoted on the TTN forum (docs/profiles.md)."""
    assert lab_air.lora_airtime_s(10, 7, 125_000) == pytest.approx(0.041216, abs=1e-6)  # 40.25 symbols
    assert lab_air.lora_airtime_s(10, 12, 125_000) == pytest.approx(0.991232, abs=1e-6)  # 30.25 symbols
    assert lab_air.lora_airtime_s(64, 7, 125_000) == pytest.approx(0.118016, abs=1e-6)
    assert lab_air.lora_airtime_s(13, 12, 125_000) == pytest.approx(1.16, abs=0.01)


def test_lora_matches_the_semtech_driver_over_a_grid():
    n = 0
    for sf, bw, cr, pre, explicit, crc in itertools.product(
            range(5, 13), (125_000, 250_000, 500_000), (1, 2, 3, 4), (6, 8, 12), (True, False), (True, False)):
        for size in (0, 1, 10, 51, 64, 100, 222, 255):
            for ldro in (0, 1):
                want = semtech_driver_ms_numerator(size, sf, bw, cr, pre, explicit, crc, ldro)
                got = lab_air.lora_airtime_s(size, sf, bw, cr, pre, explicit, crc, ldro)
                assert got == pytest.approx(want, rel=1e-12), (sf, bw, cr, pre, explicit, crc, size, ldro)
                n += 1
    assert n > 10_000


def test_low_data_rate_optimize_switches_on_above_16_ms_symbols():
    assert [lab_air.lora_ldro(sf, 125_000) for sf in range(7, 13)] == [0, 0, 0, 0, 1, 1]
    assert lab_air.lora_ldro(12, 500_000) == 0  # 8.2 ms
    assert lab_air.lora_ldro(12, 250_000) == 1  # 16.4 ms
    assert lab_air.lora_ldro(12, 125_000, 0) == 0  # explicit setting wins


GRID = [
    dict(airtime="lora", lora_sf=sf, lora_bw_hz=bw, lora_cr=cr, lora_preamble=pre, lora_explicit_header=eh,
         lora_crc=crc, lora_ldro=ld)
    for sf, bw, cr, pre, eh, crc, ld in itertools.product(
        (5, 6, 7, 9, 11, 12), (125_000, 500_000), (1, 4), (8, 10), (True, False), (True, False), (-1, 0, 1))
] + [
    dict(airtime="bitrate", air_kbps=k, fixed_us=f, overhead_bytes=o, fec_ratio=r, symbol_us=sy, extra_bits=eb)
    for k, f, o, r, sy, eb in itertools.product((9.6, 64.0, 13_000.0), (0.0, 1625.0), (0, 2, 56),
                                                (1.0, 2.0), (0.0, 4.0), (0, 22))
] + [
    dict(airtime="slots", slot_ms=ms, slot_payload=p, slot_payload_rev=r)
    for ms, p, r in itertools.product((20.0, 53.0), (64, 100), (0, 82))
]


def test_checker_and_lab_compute_the_same_airtime():
    """The checker repeats the formulas without importing the lab. They must agree."""
    for s in GRID:
        full = ChannelSettings(**s).to_dict()
        for size in (0, 1, 13, 64, 100, 250, 255, 1000):
            for d in ("a_to_b", "b_to_a"):
                a, b = lab_air.airtime_s(full, size, d), checker_air.airtime(full, size, d)
                assert a == pytest.approx(b, rel=1e-9, abs=1e-12), (s, size, d, a, b)


def test_airtime_grows_with_size_and_is_positive():
    for name in profile_names():
        s = ChannelSettings.for_profile(name)
        if not s.airtime:
            continue
        sizes = range(1, (s.mtu or 300) + 1)
        times = [s.airtime_s(n) for n in sizes]
        assert all(t > 0 for t in times), name
        assert all(b >= a - 1e-15 for a, b in zip(times, times[1:])), name


def test_wifi_profile_known_answer():
    """64 bytes: (64 + 56) * 8 + 22 = 982 bits; 52 bits a symbol -> 19 symbols * 4 us + 40 us = 116 us."""
    assert ChannelSettings.for_profile("wifi-wfb").airtime_s(64) == pytest.approx(116e-6)


def test_sik_profile_known_answer():
    """64 kbps = 125 us a byte. 13 byte-times of preamble and header, 2 byte trailer."""
    s = ChannelSettings.for_profile("sik-64k")
    assert s.airtime_s(100) == pytest.approx((13 + 102) * 125e-6)
    e = ChannelSettings.for_profile("sik-64k-ecc")
    assert e.airtime_s(100) == pytest.approx(13 * 125e-6 + (100 + 6) * 2 * 125e-6)


def test_mlrs_uses_whole_slots_and_different_payloads_each_way():
    s = ChannelSettings.for_profile("mlrs-19hz")
    assert s.airtime_s(1, "a_to_b") == s.airtime_s(64, "a_to_b") == pytest.approx(0.053)
    assert s.airtime_s(65, "a_to_b") == pytest.approx(0.106)
    assert s.airtime_s(82, "b_to_a") == pytest.approx(0.053)
    assert s.airtime_s(83, "b_to_a") == pytest.approx(0.106)
    assert s.airtime_s(0, "a_to_b") == pytest.approx(0.053)


# --- profiles ----------------------------------------------------------------------------------
def test_every_profile_is_valid_and_named():
    assert {"ideal", "lora-sf7-bw125", "lora-sf9-bw125", "lora-sf12-bw125", "sik-64k", "mlrs-19hz",
            "mlrs-50hz", "wifi-wfb"} <= set(PROFILES)
    for name in PROFILES:
        s = ChannelSettings.for_profile(name)
        assert s.profile == name
        assert ChannelSettings.from_dict(s.to_dict()) == s  # the manifest copy rebuilds exactly


def test_lora_profiles_follow_the_brief():
    for name, sf in (("lora-sf7-bw125", 7), ("lora-sf9-bw125", 9), ("lora-sf12-bw125", 12)):
        s = ChannelSettings.for_profile(name)
        assert (s.airtime, s.lora_sf, s.lora_bw_hz, s.lora_cr, s.lora_preamble) == ("lora", sf, 125_000, 1, 8)
        assert s.lora_explicit_header and s.lora_crc and s.mtu == 255 and s.half_duplex
    assert lab_air.lora_ldro(12, 125_000) == 1 and lab_air.lora_ldro(9, 125_000) == 0


def test_settings_passed_override_the_profile():
    s = ChannelSettings.for_profile("lora-sf9-bw125", loss=0.05, mtu=100, half_duplex=False, seed=4)
    assert (s.loss, s.mtu, s.half_duplex, s.seed) == (0.05, 100, False, 4)
    assert (s.lora_sf, s.lora_bw_hz, s.profile) == (9, 125_000, "lora-sf9-bw125")
    # a different spreading factor changes the airtime, but the label stays
    assert ChannelSettings.for_profile("lora-sf9-bw125", lora_sf=7).airtime_s(64) == \
        ChannelSettings.for_profile("lora-sf7-bw125").airtime_s(64)


def test_unknown_profile_names_the_choices():
    with pytest.raises(ValueError, match="lora-sf7-bw125"):
        ChannelSettings.for_profile("no-such-radio")


@pytest.mark.parametrize("bad", [
    dict(half_duplex=True), dict(max_queue=3), dict(duty_cycle=0.1), dict(serial_bps=9600),
    dict(airtime="bogus"), dict(airtime="lora"), dict(airtime="lora", lora_sf=13, lora_bw_hz=125000),
    dict(airtime="bitrate"), dict(airtime="bitrate", air_kbps=64, fec_ratio=0.5),
    dict(airtime="slots", slot_ms=20), dict(airtime="bitrate", air_kbps=64, rate_kbps=64),
    dict(airtime="bitrate", air_kbps=64, duty_cycle=1.5),
])
def test_bad_airtime_settings_rejected(bad):
    with pytest.raises(ValueError):
        ChannelSettings(**bad)


def test_describe_numbers():
    d = describe("lora-sf7-bw125")
    assert d["bitrate_bps"] == pytest.approx(5468.75) and d["max_payload"] == 255
    assert d["airtime_s"] == pytest.approx(0.118016, abs=1e-6)
    assert describe("ideal")["airtime_s"] == 0.0
    assert describe("wifi-wfb")["bitrate_bps"] == 13_000_000


def test_profiles_command_lists_every_profile():
    out = subprocess.run([sys.executable, "-m", "oasa_lab", "profiles"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    for name in PROFILES:
        assert name in out.stdout
    assert "64-byte airtime" in out.stdout and "118.02 ms" in out.stdout


def test_channel_command_takes_a_profile_and_overrides():
    out = subprocess.run([sys.executable, "-m", "oasa_lab", "channel", "--profile", "lora-sf9-bw125", "--loss", "0.05",
                          "--no-half-duplex", "--duration", "0.3", "--stats-interval", "0"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    import json
    started = json.loads(out.stdout.splitlines()[0])
    s = started["settings"]
    assert (started["event"], s["profile"], s["lora_sf"], s["loss"], s["half_duplex"]) == \
        ("channel_started", "lora-sf9-bw125", 9, 0.05, False)
    bad = subprocess.run([sys.executable, "-m", "oasa_lab", "channel", "--profile", "nope"], capture_output=True, text=True)
    assert bad.returncode == 2 and "unknown profile" in bad.stderr


# --- the transmit scheduler --------------------------------------------------------------------
def run_air(settings, offers, until=1e6, t0=0.0):
    """offers: (time, direction, size, label). Returns the transmissions as (direction, start, end, label)."""
    air = Air(settings, t0)
    out = []
    for t, d, size, label in sorted(offers, key=lambda o: o[0]):
        out += air.advance(t)
        air.offer(d, t, size, label)
    out += air.advance(until)
    return [(x.direction, x.start, x.end, x.item) for x in out], air


def test_one_direction_goes_out_one_after_another():
    s = ChannelSettings.for_profile("lora-sf7-bw125", half_duplex=False)
    t = s.airtime_s(64)
    tx, _ = run_air(s, [(0.0, "a_to_b", 64, i) for i in range(5)])
    assert [x[1] for x in tx] == pytest.approx([i * t for i in range(5)])
    assert [x[3] for x in tx] == list(range(5))


def test_full_duplex_directions_do_not_wait_for_each_other():
    s = ChannelSettings.for_profile("lora-sf7-bw125", half_duplex=False)
    tx, _ = run_air(s, [(0.0, "a_to_b", 64, "a"), (0.0, "b_to_a", 64, "b")])
    assert [x[1] for x in tx] == [0.0, 0.0]


def test_half_duplex_never_has_two_packets_on_air_and_directions_take_turns():
    s = ChannelSettings.for_profile("lora-sf7-bw125")
    offers = [(0.0, "a_to_b", 64, f"a{i}") for i in range(6)] + [(0.0, "b_to_a", 30, f"b{i}") for i in range(6)]
    tx, _ = run_air(s, offers)
    assert len(tx) == 12
    tx.sort(key=lambda x: x[1])
    for prev, nxt in zip(tx, tx[1:]):
        assert nxt[1] >= prev[2] - 1e-12, "overlap on a half-duplex medium"
    assert [x[0] for x in tx[:4]] == ["a_to_b", "b_to_a", "a_to_b", "b_to_a"]  # taking turns
    full = ChannelSettings.for_profile("lora-sf7-bw125", half_duplex=False)
    tx_full, _ = run_air(full, offers)
    assert max(x[2] for x in tx) > 1.5 * max(x[2] for x in tx_full)  # sharing costs time


def test_idle_medium_starts_at_once_and_late_packets_wait():
    s = ChannelSettings.for_profile("sik-64k", serial_bps=0)
    t = s.airtime_s(50)
    tx, _ = run_air(s, [(1.0, "a_to_b", 50, "x"), (1.0 + t / 2, "b_to_a", 50, "y")])
    assert tx[0][1] == pytest.approx(1.0) and tx[1][1] == pytest.approx(1.0 + t)


def test_lost_packets_still_use_the_air():
    """Direction hands a lost packet to the scheduler as a 'lost' emission of the same size."""
    s = ChannelSettings.for_profile("lora-sf7-bw125", loss=1.0, seed=1)
    d = Direction("a_to_b", s)
    (e,) = d.process(b"x" * 64)
    assert e.kind == "lost" and len(e.data) == 64 and d.stats["dropped"] == 1
    assert Direction("a_to_b", ChannelSettings(loss=1.0)).process(b"x") == []  # no airtime model: nothing


def test_serial_port_paces_packets_before_the_air():
    s = ChannelSettings.for_profile("sik-64k", half_duplex=False)
    t_uart = 100 * 10 / 57_600
    tx, _ = run_air(s, [(0.0, "a_to_b", 100, i) for i in range(4)])
    starts = [x[1] for x in tx]
    assert starts[0] == pytest.approx(t_uart)
    assert all(b - a >= t_uart - 1e-12 for a, b in zip(starts, starts[1:]))  # the serial port is the slower stage
    assert s.airtime_s(100) < t_uart


def test_duty_cycle_spaces_packets():
    s = ChannelSettings.for_profile("lora-sf7-bw125", duty_cycle=0.1)
    t = s.airtime_s(64)
    tx, _ = run_air(s, [(0.0, "a_to_b", 64, i) for i in range(4)])
    assert [x[1] for x in tx] == pytest.approx([i * 10 * t for i in range(4)])
    assert sum(x[2] - x[1] for x in tx) / (tx[-1][2] - tx[0][1]) == pytest.approx(0.4 / (30 + 1) * 10, rel=0.02)


def test_duty_cycle_waiting_direction_does_not_block_the_other():
    s = ChannelSettings.for_profile("lora-sf7-bw125", duty_cycle=0.1)
    tx, _ = run_air(s, [(0.0, "a_to_b", 64, "a1"), (0.0, "a_to_b", 64, "a2"), (0.0, "b_to_a", 64, "b1")])
    by = {x[3]: x for x in tx}
    assert by["b1"][1] == pytest.approx(s.airtime_s(64))  # B goes while A rests
    assert by["a2"][1] > by["b1"][2]


def test_slots_start_on_the_grid():
    s = ChannelSettings.for_profile("mlrs-19hz")
    tx, _ = run_air(s, [(100.010, "a_to_b", 64, "x"), (100.011, "a_to_b", 130, "y"), (100.500, "b_to_a", 20, "z")],
                    t0=100.0)
    for _, start, _, _ in tx:
        k = (start - 100.0) / 0.053
        assert k == pytest.approx(round(k), abs=1e-6), "not on the slot grid"
    by = {x[3]: x for x in tx}
    assert [round((by[k][2] - by[k][1]) / 0.053) for k in "xyz"] == [1, 3, 1]  # whole slots
    assert by["x"][1] == pytest.approx(100.053)  # waits for the next slot
    assert by["y"][1] == pytest.approx(100.106)


def queue_run(max_queue, n, spacing=0.0):
    """Feed n packets through Direction + Air like the channel does. Returns (direction, air, forwarded)."""
    s = ChannelSettings.for_profile("lora-sf7-bw125", max_queue=max_queue, seed=5)
    d, air, fwd = Direction("a_to_b", s), Air(s), 0
    for i in range(n):
        now = i * spacing
        air.advance(now)
        full = bool(s.max_queue) and air.depth("a_to_b") >= s.max_queue
        for e in d.process(b"p" * 64, now, queue_full=full):
            air.offer("a_to_b", now, len(e.data), e)
    air.advance(1e6)
    return d, air


def test_queue_overflow_is_counted():
    d, air = queue_run(max_queue=4, n=40)
    st = d.stats
    assert st["queue_dropped"] == 35  # one on the air, four waiting, the rest dropped
    assert st["sent"] == 40 and d.trace.count("Q") == 35
    assert air.on_air_s["a_to_b"] == pytest.approx(5 * ChannelSettings.for_profile("lora-sf7-bw125").airtime_s(64))
    d0, _ = queue_run(max_queue=0, n=40)
    assert d0.stats["queue_dropped"] == 0


def test_queue_that_keeps_up_drops_nothing():
    t = ChannelSettings.for_profile("lora-sf7-bw125").airtime_s(64)
    d, _ = queue_run(max_queue=1, n=30, spacing=1.01 * t)
    assert d.stats["queue_dropped"] == 0


def test_queue_drop_does_not_move_the_other_random_decisions():
    """A queue drop changes only that packet. Loss, duplicates and corruption at later positions are the same."""
    kw = dict(loss=0.2, duplicate=0.1, corrupt=0.1, reorder=0.1, replay=0.1, seed=11)
    s = ChannelSettings.for_profile("lora-sf7-bw125", **kw)
    plain, dropped = Direction("a_to_b", s), Direction("a_to_b", s)
    for i in range(300):
        plain.process(bytes([i % 256]) * 30)
        dropped.process(bytes([i % 256]) * 30, queue_full=(i % 7 == 3))
    a, b = plain.trace, dropped.trace
    assert [b[i] for i in range(300) if i % 7 != 3] == [a[i] for i in range(300) if i % 7 != 3]
    assert all(b[i] == "Q" for i in range(300) if i % 7 == 3)
    st = dropped.stats
    assert st["sent"] == 300 and set(st) == set(STAT_KEYS)
    survivors = st["sent"] - st["dropped"] - st["queue_dropped"]
    assert survivors > 0 and st["forwarded"] == 0  # forwarded is counted by the channel when it sends


def test_decisions_do_not_depend_on_the_airtime_model_or_the_clock():
    kw = dict(loss=0.2, duplicate=0.1, corrupt=0.1, reorder=0.1, replay=0.1, seed=21)
    base = Direction("a_to_b", ChannelSettings(**kw))
    air = Direction("a_to_b", ChannelSettings.for_profile("sik-64k", **kw))
    for i in range(500):
        base.process(bytes([i % 256]) * 40, now=float(i))
        air.process(bytes([i % 256]) * 40, now=1000.0 * i)
    assert base.trace == air.trace
    assert {k: v for k, v in base.stats.items()} == {k: v for k, v in air.stats.items()}
