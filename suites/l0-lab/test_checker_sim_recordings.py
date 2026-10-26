"""Recordings from a pure simulation (`lab.sigmf(..., device="sim:NAME")`): no device in the hardware
manifest, marked simulated, checked like any other recording, refused when real hardware is required.
Each test starts from a real bundle made by `oasa-lab run` on a tiny suite written here."""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys

import pytest

from oasa_lab.bundle import write_sums
from oasa_lab.checker import check_bundle

SUITE = '''
import struct
import time


def test_saves_simulated_iq(lab):
    iq = struct.pack("<8f", *range(8))
    entry = lab.sigmf("tone", iq, datatype="cf32_le", sample_rate=1e6, center_hz=915e6,
                      start_time=time.time(), device="sim:waveform", purpose="unit test tone")
    assert entry["simulated"] is True
'''


@pytest.fixture(scope="module")
def suites(tmp_path_factory):
    d = tmp_path_factory.mktemp("suites")
    (d / "sim-iq").mkdir()
    (d / "sim-iq" / "suite.toml").write_text('description = "one simulated recording"\n[captures]\nencrypted = false\n')
    (d / "sim-iq" / "test_sim_iq.py").write_text(SUITE)
    return d


@pytest.fixture(scope="module")
def good_sim(tmp_path_factory, suites):
    out = tmp_path_factory.mktemp("bundle-sim")
    env = {**os.environ, "OASA_LAB_SUITES": str(suites)}
    env.pop("OASA_LAB_SIM", None)
    r = subprocess.run([sys.executable, "-m", "oasa_lab", "run", "sim-iq", "--out", str(out)], capture_output=True,
                       text=True, timeout=180, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return next(p for p in out.iterdir() if p.is_dir())


def copy_of(src, tmp_path):
    dst = tmp_path / src.name
    shutil.copytree(src, dst)
    return dst


def result(bundle, suites, **kw):
    res = check_bundle(bundle, suites, **kw)
    return {c["id"]: c for c in res["checks"]}, res


def edit_json(path, fn):
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data, indent=2) + "\n")


def parts(b):
    meta = next((b / "recordings").glob("*.sigmf-meta"))
    return meta, meta.with_suffix(".sigmf-data")


def test_simulated_recording_passes_and_says_simulated(good_sim, suites):
    checks, res = result(good_sim, suites)
    assert res["ok"], {i: c["reason"] for i, c in checks.items() if c["status"] == "fail"}
    assert checks[7]["status"] == "pass"
    assert "1 SigMF recording(s) listed, hashed and valid (1 SIMULATED)" in checks[7]["reason"]
    meta, _ = parts(good_sim)
    g = json.loads(meta.read_text())["global"]
    assert g["oasa:device"] == "sim:waveform" and g["oasa:simulated"] is True
    assert "simulated" in g["core:hw"]
    m = json.loads((good_sim / "manifest.json").read_text())
    assert m["recordings"][0]["simulated"] is True and m["recordings"][0]["device"] == "sim:waveform"


def test_require_hardware_rejects_a_simulated_recording(good_sim, suites):
    checks, res = result(good_sim, suites, require_hardware=True)
    assert not res["ok"] and checks[7]["status"] == "fail"
    assert "recordings were simulated" in checks[7]["reason"]


def test_tampered_simulated_data_is_caught_by_the_hashes(good_sim, suites, tmp_path):
    b = copy_of(good_sim, tmp_path)
    _, data = parts(b)
    raw = bytearray(data.read_bytes())
    raw[0] ^= 1
    data.write_bytes(bytes(raw))
    checks, res = result(b, suites)
    assert not res["ok"] and checks[1]["status"] == "fail"


def test_tampered_simulated_data_with_fixed_sums_is_caught_by_core_sha512(good_sim, suites, tmp_path):
    b = copy_of(good_sim, tmp_path)
    _, data = parts(b)
    data.write_bytes(struct.pack("<8f", *range(8, 16)))
    write_sums(b)
    checks, res = result(b, suites)
    assert checks[7]["status"] == "fail" and "sha512" in checks[7]["reason"]


def test_simulated_recording_cannot_be_relabelled_as_real(good_sim, suites, tmp_path):
    """Dropping the simulated mark, in the manifest or in the SigMF file, must not pass."""
    for where in ("manifest", "meta"):
        b = copy_of(good_sim, tmp_path / where)
        meta, data = parts(b)
        if where == "manifest":
            edit_json(b / "manifest.json", lambda m: m["recordings"][0].update({"simulated": False}))
        else:
            edit_json(meta, lambda m: m["global"].update({"oasa:simulated": False}))
            raw = hashlib.sha512(data.read_bytes()).hexdigest()
            assert json.loads(meta.read_text())["global"]["core:sha512"] == raw
        write_sums(b)
        checks, res = result(b, suites, require_hardware=True)
        assert checks[7]["status"] == "fail" and "simulated" in checks[7]["reason"], where


def test_real_device_name_is_not_a_free_pass(good_sim, suites, tmp_path):
    """A recording that names a device the manifest does not list, without the sim: prefix, still fails."""
    b = copy_of(good_sim, tmp_path)
    meta, _ = parts(b)
    edit_json(meta, lambda m: m["global"].update({"oasa:device": "pluto-1", "oasa:simulated": False}))
    edit_json(b / "manifest.json", lambda m: m["recordings"][0].update({"device": "pluto-1", "simulated": False}))
    write_sums(b)
    checks, _ = result(b, suites)
    assert checks[7]["status"] == "fail" and "is not a device in the manifest" in checks[7]["reason"]
