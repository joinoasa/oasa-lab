"""W1: the oasa-radio W0 modem over cable, B200mini <-> E310, BPSK and QPSK. Limits and reasons: suite.toml.

Wiring (docs/hardware-suites.md, "USRP loopback"): the same 60 dB paths as l2-usrp-loopback. Transmits only
after `--confirm-wiring usrp`. How a point is measured: oasa_lab/w1test.py.

For each direction and modulation the TX gain is first raised from a low value until the frames are clear;
the points are then set from there for Eb/N0 of about 1, 3, 5 and 7 dB (the level follows the gain 1 dB per dB).
Every recording is checked for an outside signal stepping into the band (w1test.find_interference); a flagged
one is logged as a `capture_flagged` event and taken again, at most w1test.MAX_RECAPTURES extra per point.
A point outside a limit has its recording saved as `w1-fail-...` (w1test.KeepFailing, at most w1test.MAX_KEPT a
run), and so do the points around the window when a test has too few in it.
Needs `oasa-radio` in $OASA_RADIO_BIN_DIR; without it the suite is SKIPPED. The light-code tests need an
oasa-radio with `--code` (oasa-radio docs/fec-light.md); without it they are SKIPPED and the checker fails the
run (a known-bad case and required limits are missing), so the oasa-radio change must be merged first.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from oasa_lab.devices import Unavailable
from oasa_lab.hw import Hardware, WiringRefused
from oasa_lab.iq import IQ
from oasa_lab.w1test import (CODE_RATE, RX_GAIN_DB, SAMPLE_RATE, Block, KeepFailing, Point, TooStrong, around_window,
                             baseband, decode, decode_report, ebn0_for_ber, ebn0_for_loss, make_block, measure_point,
                             per_reference_result, probe_gain, run_light_point, run_point, start_per_reference,
                             supports_code, theory_ber, write_sigmf)

RF_HZ = 915e6
TARGETS_DB = (1.0, 3.0, 5.0, 7.0)
V1_TARGETS_DB = (1.0, 3.0)        # frame v1 is sent at the TX gains of these W0 points
V1_MIN_EBN0_DB = 0.5              # v1_found_fraction is judged from here up
MIN_ERRORS = 20
# Eb/N0 per information bit. QPSK's simulated loss falls from 50 % to 2 % between about 1.8 and 3.5 dB (BPSK's,
# with its weaker sync, about 2.0 to 4.6 dB), so QPSK is aimed lower to give 2 or more judged points; 5.5 dB is
# for light_delivered_fraction.
LIGHT_TARGETS_DB = {"bpsk": (2.5, 3.5, 4.5, 5.5), "qpsk": (2.0, 2.5, 3.0, 5.5)}
LIGHT_REF_DB = [1.5 + 0.5 * k for k in range(11)]            # simulated reference, 1.5 to 6.5 dB
LIGHT_LOSS_RANGE = (0.02, 0.5)                                # what about 400 frames resolve
LIGHT_FULL_DB = 5.0                                           # light_delivered_fraction is judged from here up
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


@pytest.fixture(scope="module", autouse=True)
def _stop_references():
    yield
    for k in [k for k in STATE if k.startswith("light-ref-proc-")]:
        proc = STATE.pop(k)
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


@pytest.fixture(scope="module")
def light(radio):
    if not supports_code(radio):
        pytest.skip(f"{radio} has no --code (the light error-correction tier, oasa-radio docs/fec-light.md); "
                    "build oasa-radio with it to run the light-code tests")
    return radio


@pytest.fixture
def bench(lab, hw: Hardware, radio, tmp_path):
    try:
        hw.use_wiring()
        b200, e310 = hw.first("b200"), hw.first("e310")
    except (WiringRefused, Unavailable) as exc:
        pytest.skip(str(exc))
    lab.binary(radio)
    return {"b200": b200, "e310": e310, "radio": radio, "work": tmp_path}


def keeper() -> KeepFailing:
    """The run's one KeepFailing: saves the recordings of failing points, at most w1test.MAX_KEPT a run."""
    if "keep" not in STATE:
        STATE["keep"] = KeepFailing(RF_HZ)
    return STATE["keep"]


class Flagged:
    """Receives the captures that w1test.find_interference flags in one test. Each is logged as it comes
    (`capture_flagged`, with the detector's numbers and whether it was taken again); the first of the run is
    also saved as a recording, so the detector's call can be looked at. `measure` records the count."""

    def __init__(self, lab):
        self.lab, self.n = lab, 0

    def __call__(self, event: dict, iq: IQ) -> None:
        self.n += 1
        self.lab.log("capture_flagged", **event)
        if not STATE.get("flagged-saved"):
            STATE["flagged-saved"] = True
            start, end = (round(event[k] * SAMPLE_RATE / 1e3) for k in ("start_ms", "end_ms"))
            self.lab.sigmf("w1-flagged", iq.data, datatype=iq.datatype, sample_rate=SAMPLE_RATE, center_hz=RF_HZ,
                           start_time=getattr(iq, "start_time", 0.0), device=event["rx_id"],
                           purpose="W1 capture flagged for an outside signal",
                           description=f"{event['direction']} {event['mod']} TX gain {event['tx_gain_db']:g} dB: "
                                       f"sub-band {event['band_hz'][0] / 1e3:g} to {event['band_hz'][1] / 1e3:g} kHz "
                                       f"up {event['band_rise_db']:g} dB from {event['start_ms']:g} to "
                                       f"{event['end_ms']:g} ms; {'taken again' if event['retaken'] else 'used'}",
                           annotations=[{"core:sample_start": start, "core:sample_count": min(end, iq.n) - start,
                                         "core:description": "outside signal (find_interference)"}])

    def measure(self, what: str) -> None:
        self.lab.measure("captures_flagged", f"captures flagged {what}", self.n)


def sweep(lab, bench, direction: str, mod: str, flagged: Flagged) -> list[Point]:
    tx, rx = (bench["e310"], bench["b200"]) if direction == "e310-b200" else (bench["b200"], bench["e310"])
    block = STATE.get(f"block-{mod}")
    if block is None:
        block = STATE[f"block-{mod}"] = make_block(bench["radio"], mod, bench["work"], seed=11 if mod == "bpsk" else 12)
    start = float(STATE.get(f"start-{direction}", 20.0))
    try:
        g0, probe = probe_gain(bench["radio"], tx, rx, block, RF_HZ, bench["work"], f"{direction}", start_db=start,
                               on_flag=flagged)
        STATE[f"start-{direction}"] = max(0.0, g0 - 6.0)
        STATE[f"probe-{direction}-{mod}"] = (g0, probe.ebn0_db)
        points = []
        for t in TARGETS_DB:
            g = round(g0 + (t - probe.ebn0_db), 1) if probe.ebn0_db is not None else None
            if g is None or not 0 <= g <= tx.max_tx_gain:
                lab.log("note", text=f"{direction} {mod}: no gain for {t:g} dB within 0..{tx.max_tx_gain:g} dB")
                continue
            points.append(run_point(bench["radio"], tx, rx, block, RF_HZ, g, bench["work"], direction, on_flag=flagged))
    except TooStrong as exc:
        pytest.fail(str(exc))
    STATE[f"points-{direction}-{mod}"] = points
    return points


def judge(lab, direction: str, mod: str, points: list[Point]) -> None:
    failures, measured, sides = [], 0, []
    keep = keeper()
    for pt in points:
        e = pt.ebn0_db
        # frames found away from every sent frame's place are not counted (w1test.place_frames); their number is
        # kept in sight here, per point
        if not keep.measure(lab, pt, "off-layout", "frames_off_layout",
                            f"off layout {direction} {mod} TX {pt.tx_gain_db:g}", pt.off_layout):
            failures.append(f"{pt.off_layout} frames off the layout at TX {pt.tx_gain_db:g}")
        lab.log("w1_point", direction=direction, mod=mod, tx_gain_db=pt.tx_gain_db, frames=pt.frames,
                frames_off_layout=pt.off_layout,
                frames_sent_about=pt.frames_sent, crc_ok=pt.crc_ok, bits=pt.bits, errors=pt.errors,
                ber=pt.ber, ebn0_db=None if e is None else round(e, 2),
                theory=None if e is None else theory_ber(mod, e), peak=round(pt.peak, 5), decoder=pt.report,
                captures_flagged=pt.flagged)
        if not keep.measure(lab, pt, "peak", "rx_peak_fs", f"peak {direction} {mod} TX {pt.tx_gain_db:g}",
                            round(pt.peak, 5)):
            failures.append(f"peak {pt.peak:.3f} at TX {pt.tx_gain_db:g}")
        if e is None or pt.errors < MIN_ERRORS:
            sides.append((pt, -1 if e is None else 1))   # no Eb/N0: below the window; too few errors: above
            continue
        sides.append((pt, 0))
        loss = e - ebn0_for_ber(mod, pt.ber)
        measured += 1
        if not keep.measure(lab, pt, "ber", "ber_loss_db", f"loss {direction} {mod} at {e:.2f} dB", round(loss, 3), "dB"):
            failures.append(f"{e:.2f} dB: BER {pt.ber:.2e} is {loss:+.2f} dB from theory")
    if not lab.measure("points_measured", f"points {direction} {mod}", measured):
        failures.append(f"only {measured} point(s) with {MIN_ERRORS}+ errors")
        for pt in around_window(sides):
            keep.keep(lab, pt, "window", "points_measured", measured, note=f"{measured} point(s) with {MIN_ERRORS}+ errors")
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
    flagged = Flagged(lab)
    try:
        points = sweep(lab, bench, direction, mod, flagged)
    finally:
        flagged.measure(f"{direction} {mod}")
    judge(lab, direction, mod, points)


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
    flagged, keep = Flagged(lab), keeper()
    for target in V1_TARGETS_DB:
        w0 = min(w0_points, key=lambda p: abs((p.ebn0_db if p.ebn0_db is not None else 99) - target))
        try:
            v1 = run_point(bench["radio"], tx, rx, block, RF_HZ, w0.tx_gain_db, bench["work"], direction,
                           known_len=False, on_flag=flagged)
        except TooStrong as exc:
            flagged.measure(f"v1 {direction} {mod}")
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
                    "frames_off_layout": v1.off_layout,
                    "frames_sent_about": v1.frames_sent, "crc_ok": v1.crc_ok, "bits": v1.bits, "errors": v1.errors,
                    "ber": v1.ber, "decoder": v1.report},
                w0_header={"ebn0_db": None if w0.ebn0_db is None else round(w0.ebn0_db, 2), "frames": w0h.frames,
                           "frames_off_layout": w0h.off_layout,
                           "frames_sent_about": w0h.frames_sent, "crc_ok": w0h.crc_ok, "decoder": w0h.report},
                peak=round(v1.peak, 5), captures_flagged=v1.flagged)
        if not keep.measure(lab, v1, "v1-peak", "rx_peak_fs", f"peak v1 {direction} {mod} TX {v1.tx_gain_db:g}",
                            round(v1.peak, 5)):
            failures.append(f"peak {v1.peak:.3f} at TX {v1.tx_gain_db:g}")
        lab.measure("w0_header_found_fraction", f"W0 read by header {direction} {mod} at {w0.ebn0_db:.2f} dB",
                    round(w0_frac, 4))
        if v1.ebn0_db is None or v1.ebn0_db < V1_MIN_EBN0_DB:
            continue
        judged += 1
        if not keep.measure(lab, v1, "v1-found", "v1_found_fraction", f"v1 found {direction} {mod} at {v1.ebn0_db:.2f} dB",
                            round(v1_frac, 4)):
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
    flagged.measure(f"v1 {direction} {mod}")
    assert not failures, "; ".join(failures)


@pytest.mark.parametrize("direction", ["e310-b200", "b200-e310"])
@pytest.mark.parametrize("mod", ["bpsk", "qpsk"])
def test_light_code_packet_loss(lab, bench, light, direction, mod):
    """Light error correction (oasa-radio docs/fec-light.md, "Bench check plan"): frame v1 with the K=7 rate-1/2
    code, at gains set from the W0 probe for 2 to 5.5 dB Eb/N0 per information bit (LIGHT_TARGETS_DB). At the same gain the symbol
    energy is the same, so a coded frame's Eb/N0 is the W0 probe's plus 3.0 dB. Decoded without the length, as on
    the air; delivered = good CRC-16 and a payload that was sent. Compared with `oasa-radio waveform per` at the
    bench's offset and timing."""
    probe = STATE.get(f"probe-{direction}-{mod}")
    if probe is None or probe[1] is None:
        pytest.fail(f"needs the W0 probe from test_ber_matches_theory[{mod}-{direction}]")
    g0, e0 = probe
    tx, rx = (bench["e310"], bench["b200"]) if direction == "e310-b200" else (bench["b200"], bench["e310"])
    block = STATE.get(f"block-light-{mod}")
    if block is None:
        block = STATE[f"block-light-{mod}"] = make_block(bench["radio"], mod, bench["work"],
                                                         seed=31 if mod == "bpsk" else 32, frame="v1", code="light")
    for m in ("bpsk", "qpsk"):      # both references start with the first light test and run while it measures
        if f"light-ref-{m}" not in STATE and f"light-ref-proc-{m}" not in STATE:
            STATE[f"light-ref-proc-{m}"] = start_per_reference(bench["radio"], m, LIGHT_REF_DB)
    coded_offset = 10 * math.log10(1 / CODE_RATE["light"])      # 3.0 dB
    failures, measured, points, judged, sides = [], 0, [], [], []
    flagged, keep = Flagged(lab), keeper()
    for t in LIGHT_TARGETS_DB[mod]:
        g = round(g0 + (t - coded_offset - e0), 1)
        if not 0 <= g <= tx.max_tx_gain:
            lab.log("note", text=f"light {direction} {mod}: no gain for {t:g} dB within 0..{tx.max_tx_gain:g} dB")
            continue
        try:
            pt = run_light_point(bench["radio"], tx, rx, block, RF_HZ, g, bench["work"], direction, on_flag=flagged)
        except TooStrong as exc:
            flagged.measure(f"light {direction} {mod}")
            pytest.fail(str(exc))
        points.append(pt)
        e = pt.ebn0_db
        loss = 1 - pt.delivered / pt.frames_sent if pt.frames_sent else None
        loss5 = 1 - pt.delivered_sync5 / pt.frames_sent if pt.frames_sent else None
        lab.log("w1_light_point", direction=direction, mod=mod, target_db=t, tx_gain_db=g, captures=pt.captures,
                ebn0_db=None if e is None else round(e, 2), slots=pt.frames_sent, frames=pt.frames,
                delivered=pt.delivered, loss=loss, crc_bad=pt.frames - sum(bool(f.get("crc_ok")) for f in pt.decoded),
                sync5={"delivered": pt.delivered_sync5, "loss": loss5, "decoder": pt.report_sync5},
                bits=pt.bits, errors=pt.errors, peak=round(pt.peak, 5), decoder=pt.report,
                captures_flagged=pt.flagged)
        if not keep.measure(lab, pt, "light-peak", "rx_peak_fs", f"peak light {direction} {mod} TX {g:g}",
                            round(pt.peak, 5)):
            failures.append(f"peak {pt.peak:.3f} at TX {g:g}")
        if e is None or loss is None:
            continue
        # where the point sits against the loss window: more loss is below it (lower Eb/N0), less is above
        sides.append((pt, -1 if loss > LIGHT_LOSS_RANGE[1] else 1 if loss < LIGHT_LOSS_RANGE[0] else 0))
        if LIGHT_LOSS_RANGE[0] <= loss <= LIGHT_LOSS_RANGE[1]:
            judged.append((pt, loss))
        if e >= LIGHT_FULL_DB:
            frac = pt.delivered / pt.frames_sent
            if not keep.measure(lab, pt, "light-delivered", "light_delivered_fraction",
                                f"light delivered {direction} {mod} at {e:.2f} dB", round(frac, 4)):
                failures.append(f"{e:.2f} dB: {pt.delivered} of {pt.frames_sent} delivered")
    ref = STATE.get(f"light-ref-{mod}")
    if ref is None:
        ref = STATE[f"light-ref-{mod}"] = per_reference_result(STATE.pop(f"light-ref-proc-{mod}"))
        lab.log("w1_light_reference", mod=mod, curve=[{"ebn0_db": e, "loss": l} for e, l in ref])
    for pt, loss in judged:
        e = pt.ebn0_db
        e_sim = ebn0_for_loss(ref, loss)
        lab.log("w1_light_judged", direction=direction, mod=mod, tx_gain_db=pt.tx_gain_db, ebn0_db=round(e, 2),
                loss=loss, sim_ebn0_db_at_loss=None if e_sim is None else round(e_sim, 2))
        if e_sim is None:
            failures.append(f"{e:.2f} dB: loss {loss:.3f} is outside the simulated curve")
            keep.keep(lab, pt, "light-loss", "light_loss_db", None, note=f"loss {loss:.3f} is outside the simulated curve")
            continue
        measured += 1
        if not keep.measure(lab, pt, "light-loss", "light_loss_db", f"light loss {direction} {mod} at {e:.2f} dB",
                            round(e - e_sim, 3), "dB"):
            failures.append(f"{e:.2f} dB: loss {loss:.3f}, which the simulation gives at {e_sim:.2f} dB")
    flagged.measure(f"light {direction} {mod}")
    if not lab.measure("light_points_measured", f"light points {direction} {mod}", measured):
        failures.append(f"only {measured} point(s) with a loss of {LIGHT_LOSS_RANGE[0]:.0%} to {LIGHT_LOSS_RANGE[1]:.0%}")
        for pt in around_window(sides):
            keep.keep(lab, pt, "light-window", "light_points_measured", measured,
                      note=f"loss {1 - pt.delivered / pt.frames_sent:.3f}, window "
                           f"{LIGHT_LOSS_RANGE[0]:.0%} to {LIGHT_LOSS_RANGE[1]:.0%}")
    best = max(points, key=lambda p: p.tx_gain_db, default=None)
    if best is not None and best.recording is not None:
        STATE[f"light-best-{direction}-{mod}"] = best
        if direction == "e310-b200":
            iq: IQ = best.recording
            lab.sigmf(f"w1-light-{direction}-{mod}", iq.data, datatype=iq.datatype, sample_rate=SAMPLE_RATE,
                      center_hz=RF_HZ, start_time=getattr(iq, "start_time", 0.0), device=best.rx_id,
                      purpose=f"W1 light code {mod} {direction}",
                      description=f"Frame v1 with the light code, {mod}, at IF +250 kHz, TX gain {best.tx_gain_db:g} dB, "
                                  f"RX gain {RX_GAIN_DB:g} dB; first of {best.captures} captures; Eb/N0 {best.ebn0_db} dB "
                                  f"(per information bit), {best.delivered} of {best.frames_sent} delivered in all captures")
    assert not failures, "; ".join(failures)


def test_light_as_uncoded_fails_the_crc(lab, bench, light):
    """Known bad: a light-coded recording decoded with the length given and no code (--payload-len 64 --code
    none) reads coded bits as payload bits; no CRC-16 may pass. The same recording with --code light must give
    good frames, or this proves nothing."""
    best = STATE.get("light-best-e310-b200-bpsk")
    if best is None:
        pytest.fail("needs the E310 to B200 BPSK light recording from test_light_code_packet_loss")
    block: Block = STATE["block-light-bpsk"]
    x = baseband(best.recording)
    base = bench["work"] / "light-as-uncoded"
    write_sigmf(block.meta, x, base, "W1 light-coded BPSK recording, decoded as uncoded on purpose")
    wrong = decode_report(bench["radio"], base, "bpsk", "v1", known_len=True, code="none")[0]
    right = decode_report(bench["radio"], base, "bpsk", "v1", known_len=True, code="light")[0]
    ok_wrong = lab.measure("light_as_uncoded_crc_ok", "light decoded as uncoded, good CRCs",
                           sum(bool(f.get("crc_ok")) for f in wrong))
    ok_right = lab.measure("light_as_light_crc_ok", "light decoded as light with the length, good CRCs",
                           sum(bool(f.get("crc_ok")) for f in right))
    assert ok_wrong and ok_right


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
