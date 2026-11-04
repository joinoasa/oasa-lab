"""W1: the oasa-radio W0 modem over cable, B200mini <-> E310, BPSK and QPSK. Limits and reasons: suite.toml.

Wiring (docs/hardware-suites.md, "USRP loopback"): the same 60 dB paths as l2-usrp-loopback. Transmits only
after `--confirm-wiring usrp`. How a point is measured: oasa_lab/w1test.py.

For each direction and modulation the TX gain is first raised from a low value until the frames are clear;
the points are then set from there for Eb/N0 of about 1, 3, 5 and 7 dB (the level follows the gain 1 dB per dB).
Needs `oasa-radio` in $OASA_RADIO_BIN_DIR; without it the suite is SKIPPED.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from oasa_lab.devices import Unavailable
from oasa_lab.hw import Hardware, WiringRefused
from oasa_lab.iq import IQ
from oasa_lab.w1test import (RX_GAIN_DB, SAMPLE_RATE, Block, Point, TooStrong, baseband, decode, decode_report,
                             ebn0_for_ber, make_block, measure_point, probe_gain, run_point, theory_ber, write_sigmf)

RF_HZ = 915e6
TARGETS_DB = (1.0, 3.0, 5.0, 7.0)
V1_TARGETS_DB = (1.0, 3.0)        # frame v1 is sent at the TX gains of these W0 points
V1_MIN_EBN0_DB = 0.5              # v1_found_fraction is judged from here up
MIN_ERRORS = 20
STATE: dict[str, object] = {}


@pytest.fixture(scope="module")
def radio():
    d = os.environ.get("OASA_RADIO_BIN_DIR")
    if not d:
        pytest.skip("OASA_RADIO_BIN_DIR is not set (folder with oasa-radio)")
    p = Path(d) / "oasa-radio"
    if not (p.is_file() and os.access(p, os.X_OK)):
        pytest.skip(f"oasa-radio not found or not executable in OASA_RADIO_BIN_DIR={d}")
    return p


@pytest.fixture
def bench(lab, hw: Hardware, radio, tmp_path):
    try:
        hw.use_wiring()
        b200, e310 = hw.first("b200"), hw.first("e310")
    except (WiringRefused, Unavailable) as exc:
        pytest.skip(str(exc))
    lab.binary(radio)
    return {"b200": b200, "e310": e310, "radio": radio, "work": tmp_path}


def sweep(lab, bench, direction: str, mod: str) -> list[Point]:
    tx, rx = (bench["e310"], bench["b200"]) if direction == "e310-b200" else (bench["b200"], bench["e310"])
    block = STATE.get(f"block-{mod}")
    if block is None:
        block = STATE[f"block-{mod}"] = make_block(bench["radio"], mod, bench["work"], seed=11 if mod == "bpsk" else 12)
    start = float(STATE.get(f"start-{direction}", 20.0))
    try:
        g0, probe = probe_gain(bench["radio"], tx, rx, block, RF_HZ, bench["work"], f"{direction}", start_db=start)
        STATE[f"start-{direction}"] = max(0.0, g0 - 6.0)
        points = []
        for t in TARGETS_DB:
            g = round(g0 + (t - probe.ebn0_db), 1) if probe.ebn0_db is not None else None
            if g is None or not 0 <= g <= tx.max_tx_gain:
                lab.log("note", text=f"{direction} {mod}: no gain for {t:g} dB within 0..{tx.max_tx_gain:g} dB")
                continue
            points.append(run_point(bench["radio"], tx, rx, block, RF_HZ, g, bench["work"], direction))
    except TooStrong as exc:
        pytest.fail(str(exc))
    STATE[f"points-{direction}-{mod}"] = points
    return points


def judge(lab, direction: str, mod: str, points: list[Point]) -> None:
    failures, measured = [], 0
    for pt in points:
        e = pt.ebn0_db
        lab.log("w1_point", direction=direction, mod=mod, tx_gain_db=pt.tx_gain_db, frames=pt.frames,
                frames_sent_about=pt.frames_sent, crc_ok=pt.crc_ok, bits=pt.bits, errors=pt.errors,
                ber=pt.ber, ebn0_db=None if e is None else round(e, 2),
                theory=None if e is None else theory_ber(mod, e), peak=round(pt.peak, 5), decoder=pt.report)
        if not lab.measure("rx_peak_fs", f"peak {direction} {mod} TX {pt.tx_gain_db:g}", round(pt.peak, 5)):
            failures.append(f"peak {pt.peak:.3f} at TX {pt.tx_gain_db:g}")
        if e is None or pt.errors < MIN_ERRORS:
            continue
        loss = e - ebn0_for_ber(mod, pt.ber)
        measured += 1
        if not lab.measure("ber_loss_db", f"loss {direction} {mod} at {e:.2f} dB", round(loss, 3), "dB"):
            failures.append(f"{e:.2f} dB: BER {pt.ber:.2e} is {loss:+.2f} dB from theory")
    if not lab.measure("points_measured", f"points {direction} {mod}", measured):
        failures.append(f"only {measured} point(s) with {MIN_ERRORS}+ errors")
    best = max(points, key=lambda p: p.tx_gain_db, default=None)
    if best is not None and best.recording is not None:
        iq: IQ = best.recording
        lab.sigmf(f"w1-{direction}-{mod}", iq.data, datatype=iq.datatype, sample_rate=SAMPLE_RATE, center_hz=RF_HZ,
                  start_time=getattr(iq, "start_time", 0.0), device=best.rx_id,
                  purpose=f"W1 {mod} {direction}",
                  description=f"W0 frames, {mod}, at IF +250 kHz, TX gain {best.tx_gain_db:g} dB, RX gain {RX_GAIN_DB:g} dB; "
                              f"Eb/N0 {best.ebn0_db} dB, {best.errors} errors in {best.bits} bits")
        STATE[f"best-{direction}-{mod}"] = best
    assert not failures, "; ".join(failures)


@pytest.mark.parametrize("direction", ["e310-b200", "b200-e310"])
@pytest.mark.parametrize("mod", ["bpsk", "qpsk"])
def test_ber_matches_theory(lab, bench, direction, mod):
    judge(lab, direction, mod, sweep(lab, bench, direction, mod))


@pytest.mark.parametrize("direction", ["e310-b200", "b200-e310"])
@pytest.mark.parametrize("mod", ["bpsk", "qpsk"])
def test_v1_header_finds_frames(lab, bench, direction, mod):
    """Frame format v1 (oasa-radio docs/frame-v1.md, "Bench check plan"): the header has a code and a CRC-8, so
    the receiver finds frames without being told the length. Sent at the TX gains of the W0 points near 1 and
    3 dB; decoded without --payload-len. The W0 recordings of the same points are decoded again the same way
    (reading the header), so the two formats are compared on the same path and gain."""
    w0_points: list[Point] = STATE.get(f"points-{direction}-{mod}") or []
    if not w0_points:
        pytest.fail(f"needs the W0 points from test_ber_matches_theory[{mod}-{direction}]")
    tx, rx = (bench["e310"], bench["b200"]) if direction == "e310-b200" else (bench["b200"], bench["e310"])
    block = STATE.get(f"block-v1-{mod}")
    if block is None:
        block = STATE[f"block-v1-{mod}"] = make_block(bench["radio"], mod, bench["work"],
                                                      seed=21 if mod == "bpsk" else 22, frame="v1")
    w0_block: Block = STATE[f"block-{mod}"]
    failures, judged = [], 0
    for target in V1_TARGETS_DB:
        w0 = min(w0_points, key=lambda p: abs((p.ebn0_db if p.ebn0_db is not None else 99) - target))
        try:
            v1 = run_point(bench["radio"], tx, rx, block, RF_HZ, w0.tx_gain_db, bench["work"], direction,
                           known_len=False)
        except TooStrong as exc:
            pytest.fail(str(exc))
        # the W0 recording of the same point, now reading its header
        x = baseband(w0.recording)
        base = bench["work"] / f"w0-header-{direction}-{mod}-{w0.tx_gain_db:g}".replace(".", "p")
        write_sigmf(w0_block.meta, x, base, "W1 W0 recording, decoded reading the header")
        frames, report = decode_report(bench["radio"], base, mod, "w0", known_len=False)
        w0h = Point(direction, mod, w0.tx_gain_db)
        measure_point(w0h, w0_block, x, frames)
        w0h.frames_sent, w0h.report = w0.frames_sent, report
        v1_frac = v1.frames / v1.frames_sent if v1.frames_sent else 0.0
        w0_frac = w0h.frames / w0h.frames_sent if w0h.frames_sent else 0.0
        lab.log("w1_v1_point", direction=direction, mod=mod, tx_gain_db=w0.tx_gain_db,
                v1={"ebn0_db": None if v1.ebn0_db is None else round(v1.ebn0_db, 2), "frames": v1.frames,
                    "frames_sent_about": v1.frames_sent, "crc_ok": v1.crc_ok, "bits": v1.bits, "errors": v1.errors,
                    "ber": v1.ber, "decoder": v1.report},
                w0_header={"ebn0_db": None if w0.ebn0_db is None else round(w0.ebn0_db, 2), "frames": w0h.frames,
                           "frames_sent_about": w0h.frames_sent, "crc_ok": w0h.crc_ok, "decoder": w0h.report},
                peak=round(v1.peak, 5))
        if not lab.measure("rx_peak_fs", f"peak v1 {direction} {mod} TX {v1.tx_gain_db:g}", round(v1.peak, 5)):
            failures.append(f"peak {v1.peak:.3f} at TX {v1.tx_gain_db:g}")
        lab.measure("w0_header_found_fraction", f"W0 read by header {direction} {mod} at {w0.ebn0_db:.2f} dB",
                    round(w0_frac, 4))
        if v1.ebn0_db is None or v1.ebn0_db < V1_MIN_EBN0_DB:
            continue
        judged += 1
        if not lab.measure("v1_found_fraction", f"v1 found {direction} {mod} at {v1.ebn0_db:.2f} dB", round(v1_frac, 4)):
            failures.append(f"v1 at {v1.ebn0_db:.2f} dB: {v1.frames} of about {v1.frames_sent} found")
        if target == V1_TARGETS_DB[0] and v1.recording is not None and direction == "e310-b200":
            iq: IQ = v1.recording
            lab.sigmf(f"w1-v1-{direction}-{mod}", iq.data, datatype=iq.datatype, sample_rate=SAMPLE_RATE,
                      center_hz=RF_HZ, start_time=getattr(iq, "start_time", 0.0), device=v1.rx_id,
                      purpose=f"W1 frame v1 {mod} {direction}",
                      description=f"Frame v1, {mod}, at IF +250 kHz, TX gain {v1.tx_gain_db:g} dB, RX gain {RX_GAIN_DB:g} dB; "
                                  f"Eb/N0 {v1.ebn0_db:.2f} dB, {v1.frames} frames found without the length")
    if judged == 0:
        failures.append(f"no v1 point at {V1_MIN_EBN0_DB:g} dB or more")
    assert not failures, "; ".join(failures)


def test_wrong_modulation_fails_the_crc(lab, bench):
    best = STATE.get("best-e310-b200-bpsk")
    if best is None:
        pytest.fail("needs the E310 to B200 BPSK recording from test_ber_matches_theory")
    block: Block = STATE["block-bpsk"]
    x = baseband(best.recording)
    base = bench["work"] / "wrong-mod"
    write_sigmf(block.meta, x, base, "W1 BPSK recording, decoded as QPSK on purpose")
    wrong = decode(bench["radio"], base, "qpsk")
    right = decode(bench["radio"], base, "bpsk")
    ok_wrong = lab.measure("wrong_mod_crc_ok", "BPSK decoded as QPSK, good CRCs", sum(bool(f.get("crc_ok")) for f in wrong))
    ok_right = lab.measure("right_mod_crc_ok", "BPSK decoded as BPSK, good CRCs", sum(bool(f.get("crc_ok")) for f in right))
    assert ok_wrong and ok_right


def test_noise_only_gives_no_frames(lab, bench):
    rx = bench["b200"]
    bench["e310"].tx_off()
    bench["b200"].tx_off()
    block = STATE.get("block-bpsk") or make_block(bench["radio"], "bpsk", bench["work"], seed=11)
    iq = rx.capture(RF_HZ, SAMPLE_RATE, 4 * len(block.samples), gain_db=RX_GAIN_DB)
    base = bench["work"] / "noise"
    write_sigmf(block.meta, baseband(iq), base, "W1 noise only: no transmitter on")
    found = decode(bench["radio"], base, "bpsk") + decode(bench["radio"], base, "qpsk")
    lab.sigmf("w1-noise-only", iq.data, datatype=iq.datatype, sample_rate=SAMPLE_RATE, center_hz=RF_HZ,
              start_time=getattr(iq, "start_time", 0.0), device=rx.id, purpose="W1 noise only",
              description="B200 RX2 with both transmitters off; decoded as BPSK and QPSK")
    assert lab.measure("noise_only_frames", "frames in noise", len(found)), f"{len(found)} frame(s) in noise"
