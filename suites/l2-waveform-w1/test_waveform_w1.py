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
from oasa_lab.w1test import (RX_GAIN_DB, SAMPLE_RATE, Block, Point, TooStrong, baseband, decode, ebn0_for_ber,
                             make_block, probe_gain, run_point, theory_ber, write_sigmf)

RF_HZ = 915e6
TARGETS_DB = (1.0, 3.0, 5.0, 7.0)
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
