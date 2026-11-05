"""Both E310 receive channels in one stream. Limits and reasons: suite.toml.

Wiring "usrp" as in l2-usrp-loopback: B200mini TX/RX through 60 dB to E310 RX2-B; RX2-A has no cable. So RX2-B
hears the tone and RX2-A only what leaks across. With a splitter feeding both channels (a later wiring), the
same capture measures the channels' phase offset, the calibration for direction finding.
"""

import json
import math
import os
import subprocess
import time
from pathlib import Path

import pytest

from oasa_lab.devices import Unavailable
from oasa_lab.hw import Hardware, WiringRefused
from oasa_lab.iq import tone_report
from oasa_lab.usrptest import (OFFSET_HZ, RX_GAIN_DB, SAMPLE_RATE, TX_SCALE, TooStrong, cross, level_at_dbfs,
                               tone_pair)

RF_HZ = 915e6
STATE: dict = {}


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
def pair(lab, hw: Hardware, radio):
    """One tone run, shared by the tests: (PairCapture, B200, E310)."""
    if "pc" not in STATE:
        try:
            hw.use_wiring()
            b200, e310 = hw.first("b200"), hw.first("e310")
        except (WiringRefused, Unavailable) as exc:
            pytest.skip(str(exc))
        lab.binary(radio)
        try:
            pc = tone_pair(b200, e310, RF_HZ)
        except TooStrong as exc:
            pytest.fail(str(exc))
        STATE.update(pc=pc, b200=b200, e310=e310)
        for tag, (a, b) in (("on", pc.on), ("off", pc.off)):
            for port, iq in (("rx2-a", a), ("rx2-b", b)):
                lab.sigmf(f"two-channel-{tag}-{port}", iq.data, datatype=iq.datatype, sample_rate=SAMPLE_RATE,
                          center_hz=RF_HZ, start_time=getattr(iq, "start_time", time.time()), device=e310.id,
                          purpose=f"two-channel {tag} {port}",
                          description=f"{e310.id} {port}, channel {0 if port == 'rx2-a' else 1} of one two-channel "
                                      f"stream; {b200.id} {b200.tx_port} tone at centre + {OFFSET_HZ:g} Hz into RX2-B, "
                                      f"TX gain {pc.final_gain_db:g} dB, amplitude {TX_SCALE:g}, RX gain {RX_GAIN_DB:g} dB, "
                                      f"{'on' if tag == 'on' else 'transmitter off'}")
        x = cross(*pc.on)
        lab.log("two_channel", final_tx_gain_db=pc.final_gain_db,
                probe_levels_dbfs={f"{g:g}": round(v, 2) for g, v in pc.probe_levels.items()},
                tone_on_phase_deg=round(x["phase_deg"], 3), tone_on_coherence=round(x["coherence"], 5),
                power_a_dbfs=round(x["power_a_dbfs"], 2), power_b_dbfs=round(x["power_b_dbfs"], 2))
    return STATE["pc"], STATE["b200"], STATE["e310"]


def test_wired_channel_hears_the_tone(lab, pair):
    pc, _, _ = pair
    rep = tone_report(pc.on[1])
    STATE["tone_hz"] = rep["freq_hz"]
    ok = [lab.measure("rx_level_dbfs", "level on RX2-B", round(rep["tone_dbfs"], 2), "dBFS"),
          lab.measure("tone_snr_db", "tone SNR on RX2-B", round(rep["snr_db"], 1), "dB"),
          lab.measure("tx_underflows", "underflows", pc.underflows, "")]
    assert all(ok), rep


def test_channel_isolation(lab, pair):
    pc, _, _ = pair
    f = STATE.get("tone_hz") or tone_report(pc.on[1])["freq_hz"]
    on_b, on_a = level_at_dbfs(pc.on[1], f), level_at_dbfs(pc.on[0], f)
    lab.log("isolation", tone_hz=round(f, 1), rx2_b_dbfs=round(on_b, 2), rx2_a_dbfs=round(on_a, 2),
            rx2_a_off_dbfs=round(level_at_dbfs(pc.off[0], f), 2))
    iso = round(on_b - on_a, 2)
    assert lab.measure("channel_isolation_db", "RX2-B over RX2-A at the tone", iso, "dB"), f"{iso} dB"


def test_channels_are_independent_without_signal(lab, pair):
    pc, _, _ = pair
    x = cross(*pc.off)
    c = round(x["coherence"], 5)
    assert lab.measure("noise_coherence", "coherence, transmitter off", c, ""), \
        f"coherence {c} with no signal: the two channels are not independent"


def test_noise_floors_match(lab, pair):
    pc, _, _ = pair
    x = cross(*pc.off)
    d = round(x["power_b_dbfs"] - x["power_a_dbfs"], 2)
    assert lab.measure("noise_balance_db", "RX2-B minus RX2-A noise", d, "dB"), f"{d} dB"


def test_waveform_df_agrees(lab, pair, radio, tmp_path):
    pc, _, _ = pair
    a, b = pc.on
    assert a.datatype == "ci16_le", a.datatype
    fa, fb = tmp_path / "a.iq", tmp_path / "b.iq"
    fa.write_bytes(a.data)
    fb.write_bytes(b.data)
    n = min(a.n, b.n)
    r = subprocess.run([str(radio), "waveform", "df", "--in1", str(fa), "--in2", str(fb), "--format", "ci16",
                        "--block", str(n)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-300:]
    lines = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
    seg = [x for x in lines if x["event"] == "segment"]
    assert len(seg) == 1, lines
    ours = cross(a, b)
    dp = abs((seg[0]["phase_deg"] - ours["phase_deg"] + 180.0) % 360.0 - 180.0)
    dc = abs(seg[0]["coherence"] - ours["coherence"])
    lab.log("df_compare", radio_phase_deg=seg[0]["phase_deg"], lab_phase_deg=round(ours["phase_deg"], 6),
            radio_coherence=seg[0]["coherence"], lab_coherence=round(ours["coherence"], 8))
    ok = [lab.measure("df_phase_agreement_deg", "waveform df phase vs the lab", round(dp, 6), "deg"),
          lab.measure("df_coherence_agreement", "waveform df coherence vs the lab", round(dc, 8), "")]
    assert all(ok) and math.isfinite(dp)
