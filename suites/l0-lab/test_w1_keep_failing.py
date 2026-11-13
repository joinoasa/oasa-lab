"""oasa_lab/w1test.py: KeepFailing saves the recording of a W1 point outside its limit, at most MAX_KEPT a run, and
around_window picks the points to keep when a test has too few in its window. The end-to-end tests run a tiny suite
with `oasa-lab run` and check its bundle; no radio and no oasa-radio needed."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from oasa_lab.checker import check_bundle
from oasa_lab.iq import IQ, cf32_bytes
from oasa_lab.w1test import MAX_KEPT, KeepFailing, Point, around_window

LIMITS = {"ber_loss_db": {"min": -1.0, "max": 1.0}, "points_measured": {"min": 2, "max": 100}}


class FakeLab:
    def __init__(self):
        self.ctx = SimpleNamespace(suite=SimpleNamespace(limits=LIMITS))
        self.saved, self.events = [], []

    def measure(self, metric, name, value, unit=""):
        lim = LIMITS[metric]
        return lim["min"] <= value <= lim["max"]

    def sigmf(self, name, data, **kw):
        self.saved.append({"name": name, "data": data, **kw})

    def log(self, event, **fields):
        self.events.append((event, fields))


def point(gain: float, rec: bool = True, **kw) -> Point:
    pt = Point("e310-b200", "bpsk", gain, rx_id="b200", ebn0_db=6.6321, errors=40, bits=12288, frames=96, **kw)
    if rec:
        pt.recording = IQ(cf32_bytes([0.01j] * 1000), "cf32_le", 1e6, 915e6)
    return pt


def test_an_out_of_limit_point_is_saved_with_its_numbers():
    lab, keep = FakeLab(), KeepFailing(915e6)
    pt = point(23.5)
    assert keep.measure(lab, pt, "ber", "ber_loss_db", "loss", 1.57, "dB") is False
    (s,) = lab.saved
    assert s["name"] == "w1-fail-ber-e310-b200-bpsk-tx23p5" and s["device"] == "b200" and s["center_hz"] == 915e6
    assert s["data"] == pt.recording.data and s["datatype"] == "cf32_le" and s["sample_rate"] == 1e6
    (a,) = s["annotations"]
    assert a["core:sample_start"] == 0 and a["core:sample_count"] == 1000
    assert (a["oasa:metric"], a["oasa:value"], a["oasa:min"], a["oasa:max"]) == ("ber_loss_db", 1.57, -1.0, 1.0)
    assert (a["oasa:direction"], a["oasa:mod"], a["oasa:tx_gain_db"], a["oasa:ebn0_db"]) == ("e310-b200", "bpsk", 23.5, 6.63)
    for text in (a["core:description"], s["description"]):
        assert "e310-b200 bpsk TX 23.5 dB, Eb/N0 6.63 dB: ber_loss_db 1.57 dB, limit -1..1" in text
    assert "ber_loss_db 1.57 dB (-1..1)" in s["purpose"]
    assert lab.events == []


def test_a_point_inside_its_limit_saves_nothing():
    lab, keep = FakeLab(), KeepFailing(915e6)
    for g, v in ((10.0, 0.2), (16.0, -0.9), (22.0, 1.0)):
        assert keep.measure(lab, point(g), "ber", "ber_loss_db", "loss", v, "dB") is True
    assert lab.saved == [] and lab.events == [] and keep.kept == 0


def test_the_cap_keeps_the_first_and_logs_the_rest():
    lab, keep = FakeLab(), KeepFailing(915e6)
    pts = [point(10.0 + i) for i in range(MAX_KEPT + 2)]
    for pt in pts:
        keep.measure(lab, pt, "ber", "ber_loss_db", "loss", 2.0, "dB")
    assert [s["name"] for s in lab.saved] == [f"w1-fail-ber-e310-b200-bpsk-tx{10 + i}" for i in range(MAX_KEPT)]
    assert [e for e, _ in lab.events] == ["w1_recording_dropped"] * 2
    assert lab.events[0][1]["tx_gain_db"] == 10.0 + MAX_KEPT and lab.events[0][1]["cap"] == MAX_KEPT


def test_a_point_is_saved_once_and_one_without_a_recording_not_at_all():
    lab, keep = FakeLab(), KeepFailing(915e6)
    pt = point(12.0)
    assert keep.keep(lab, pt, "ber", "ber_loss_db", 1.5) is True
    assert keep.keep(lab, pt, "window", "points_measured", 1) is False
    assert keep.keep(lab, point(13.0, rec=False), "ber", "ber_loss_db", 1.5) is False
    assert len(lab.saved) == 1 and lab.events == []


def test_around_window_keeps_the_points_inside_and_the_nearest_each_side():
    p = {g: point(g) for g in (4.0, 10.0, 16.0, 22.0, 28.0, 34.0)}
    got = around_window([(p[4.0], -1), (p[10.0], -1), (p[16.0], 0), (p[22.0], 1), (p[28.0], 1), (p[34.0], None)])
    assert [x.tx_gain_db for x in got] == [16.0, 10.0, 22.0]
    assert [x.tx_gain_db for x in around_window([(p[22.0], 1), (p[28.0], 1)])] == [22.0]
    assert around_window([]) == []


# --- through `oasa-lab run`: the evidence path and the checker ---------------------------------------------

SUITE = '''
import os
import time

from oasa_lab.iq import IQ, cf32_bytes
from oasa_lab.w1test import KeepFailing, Point

LOSS = [float(v) for v in os.environ["W1KEEP_LOSS"].split(",")]


def test_points(lab):
    keep = KeepFailing(915e6)
    ok = True
    for i, loss in enumerate(LOSS):
        pt = Point("e310-b200", "bpsk", 20.5 + i, rx_id="sim:w1keep", ebn0_db=6.6 + i, errors=40, bits=12288)
        pt.recording = IQ(cf32_bytes([complex(0.01 * (k % 7), 0.0) for k in range(4000)]), "cf32_le", 1e6, 915e6)
        pt.recording.start_time = time.time()
        ok &= keep.measure(lab, pt, "ber", "ber_loss_db", f"loss at {pt.ebn0_db:g} dB", loss, "dB")
    assert ok
'''


@pytest.fixture(scope="module")
def suites(tmp_path_factory):
    d = tmp_path_factory.mktemp("suites")
    (d / "w1keep").mkdir()
    (d / "w1keep" / "suite.toml").write_text('description = "W1 failing recordings"\n[captures]\nencrypted = false\n'
                                             '[limits.ber_loss_db]\nmin = -1.0\nmax = 1.0\n')
    (d / "w1keep" / "test_w1keep.py").write_text(SUITE)
    return d


def run(tmp_path, suites, loss: str):
    env = {**os.environ, "OASA_LAB_SUITES": str(suites), "W1KEEP_LOSS": loss}
    env.pop("OASA_LAB_SIM", None)
    r = subprocess.run([sys.executable, "-m", "oasa_lab", "run", "w1keep", "--out", str(tmp_path)], capture_output=True,
                       text=True, timeout=180, env=env)
    bundle = next(p for p in tmp_path.iterdir() if p.is_dir())
    res = check_bundle(bundle, suites)
    return r, bundle, {c["name"]: c for c in res["checks"]}, res


def test_a_clean_run_adds_nothing_and_passes(tmp_path, suites):
    r, bundle, checks, res = run(tmp_path, suites, "0.1,-0.3,0.9")
    assert r.returncode == 0, r.stdout + r.stderr
    assert res["ok"], {n: c["reason"] for n, c in checks.items() if c["status"] == "fail"}
    assert json.loads((bundle / "manifest.json").read_text())["recordings"] == []
    assert not (bundle / "recordings").exists()


def test_failing_points_are_ordinary_recordings_up_to_the_cap(tmp_path, suites):
    r, bundle, checks, res = run(tmp_path, suites, ",".join(["1.57", "0.2"] + ["1.2"] * MAX_KEPT))
    assert r.returncode == 1
    recs = json.loads((bundle / "manifest.json").read_text())["recordings"]
    assert [x["base"] for x in recs] == [f"recordings/{i + 1:02d}-w1-fail-ber-e310-b200-bpsk-tx{g}"
                                         for i, g in enumerate(("20p5", "22p5", "23p5", "24p5"))]
    meta = json.loads((bundle / f"{recs[0]['meta']}").read_text())
    (a,) = meta["annotations"]
    assert a["oasa:value"] == 1.57 and a["oasa:tx_gain_db"] == 20.5 and "ber_loss_db 1.57 dB, limit -1..1" in a["core:description"]
    sums = (bundle / "SHA256SUMS").read_text()
    assert all(x["meta"] in sums and x["data"] in sums for x in recs)
    summary = (bundle / "summary.md").read_text()
    assert "W1 failing point: ber_loss_db 1.57 dB (-1..1), e310-b200 bpsk TX 20.5 dB" in summary
    log = [json.loads(l) for l in (bundle / "log.jsonl").read_text().splitlines()]
    assert [x["tx_gain_db"] for x in log if x["event"] == "w1_recording_dropped"] == [25.5]
    # the checker fails the run for the measurements only; the recordings pass like any other
    assert not res["ok"]
    assert checks["radio_evidence"]["status"] == "fail"
    errs = checks["radio_evidence"]["details"]["errors"]
    assert errs and all("is outside its limits" in e for e in errs), errs
    assert checks["no_readable_payload"]["status"] == "pass" and checks["manifest"]["status"] == "pass"
