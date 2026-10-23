"""The checker must catch tampering with radio evidence: SigMF recordings, hashes, the wiring,
the limits and the burst timing. Each test starts from a real, passing bundle made on the simulated
bench (`l2-rf-control` and `l2-pluto-acceptance` through `oasa-lab run`)."""

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys

import pytest

from oasa_lab.bundle import write_sums
from oasa_lab.checker import check_bundle
from oasa_lab.pcap import PcapWriter, read_pcap
from oasa_lab.suites import suites_dir


def make_bundle(tmp_path_factory, suite):
    out = tmp_path_factory.mktemp(f"bundle-{suite}")
    r = subprocess.run([sys.executable, "-m", "oasa_lab", "run", suite, "--out", str(out)], capture_output=True,
                       text=True, timeout=180, env={**os.environ, "OASA_LAB_SIM": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    return next(p for p in out.iterdir() if p.is_dir())


@pytest.fixture(scope="module")
def good_control(tmp_path_factory):
    return make_bundle(tmp_path_factory, "l2-rf-control")


@pytest.fixture(scope="module")
def good_pluto(tmp_path_factory):
    return make_bundle(tmp_path_factory, "l2-pluto-acceptance")


def copy_of(src, tmp_path):
    dst = tmp_path / src.name
    shutil.copytree(src, dst)
    return dst


def verdict(bundle, **kw):
    res = check_bundle(bundle, suites_dir(), **kw)
    return {c["id"]: c for c in res["checks"]}, res


def failed(bundle, **kw):
    return sorted(i for i, c in verdict(bundle, **kw)[0].items() if c["status"] == "fail")


def reason7(bundle, **kw):
    return verdict(bundle, **kw)[0][7]["reason"]


def edit_json(path, fn):
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data, indent=2) + "\n")


def recording(b):
    meta = next((b / "recordings").glob("*.sigmf-meta"))
    return meta, meta.with_suffix(".sigmf-data")


def set_data(b, data):
    """Replace the dataset and keep every hash consistent, so only the content itself can give it away."""
    meta, data_path = recording(b)
    data_path.write_bytes(data)
    edit_json(meta, lambda m: m["global"].update({"core:sha512": hashlib.sha512(data).hexdigest()}))
    write_sums(b)


def test_untouched_radio_bundles_pass(good_control, good_pluto):
    for b in (good_control, good_pluto):
        checks, res = verdict(b)
        assert res["ok"], {i: c["reason"] for i, c in checks.items() if c["status"] == "fail"}
        assert len(checks) == 7
    assert "SIMULATED" in verdict(good_control)[0][7]["reason"]
    assert "bursts line up" in verdict(good_control)[0][7]["reason"]


def test_simulated_bundle_is_refused_when_hardware_is_required(good_control):
    assert 7 in failed(good_control, require_hardware=True)
    assert "simulated" in reason7(good_control, require_hardware=True)


# --- recordings: listing, hashes, metadata -------------------------------------------------------------
def test_flipped_sigmf_byte_is_caught_by_the_hashes(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    meta, data = recording(b)
    raw = bytearray(data.read_bytes())
    raw[1000] ^= 1
    data.write_bytes(raw)
    f = failed(b)
    assert 1 in f and 7 in f
    assert "mismatch" in verdict(b)[0][1]["reason"]


def test_changed_sigmf_data_with_fixed_sums_is_caught_by_core_sha512(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    meta, data = recording(b)
    raw = bytearray(data.read_bytes())
    raw[1000] ^= 1
    data.write_bytes(raw)
    write_sums(b)
    assert failed(b) == [7]
    assert "core:sha512" in reason7(b)


def test_missing_or_wrong_metadata_is_caught(good_control, tmp_path):
    cases = {
        "core:sha512": lambda m: m["global"].pop("core:sha512"),
        "core:sample_rate": lambda m: m["global"].update({"core:sample_rate": -1}),
        "core:datatype": lambda m: m["global"].update({"core:datatype": "cf32"}),   # needs _le or _be
        "core:version": lambda m: m["global"].update({"core:version": "2.0.0"}),
        "captures": lambda m: m.update({"captures": []}),
        "core:frequency": lambda m: m["captures"][0].pop("core:frequency"),
        "core:datetime": lambda m: m["captures"][0].update({"core:datetime": "yesterday"}),
        "annotations": lambda m: m.update({"annotations": [{"core:sample_start": 10**9}] if False else [{"core:sample_start": 0, "core:sample_count": 10**9}]}),
    }
    for name, edit in cases.items():
        b = copy_of(good_control, tmp_path / name.replace(":", "_"))
        meta, _ = recording(b)
        edit_json(meta, edit)
        write_sums(b)
        assert failed(b) == [7], name


def test_manifest_and_metadata_must_agree(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    meta, _ = recording(b)
    edit_json(meta, lambda m: m["captures"][0].update({"core:frequency": 433e6}))
    write_sums(b)
    assert failed(b) == [7] and "center_hz" in reason7(b)


def test_data_not_a_whole_number_of_samples_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    meta, data = recording(b)
    set_data(b, data.read_bytes() + b"\x80")
    assert failed(b) == [7] and "whole number" in reason7(b)


def test_recording_missing_from_the_manifest_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    edit_json(b / "manifest.json", lambda m: m.update({"recordings": []}))
    write_sums(b)
    assert failed(b) == [7] and "not listed in the manifest" in reason7(b)


def test_unlisted_extra_recording_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    meta, data = recording(b)
    shutil.copy(meta, b / "recordings" / "99-extra.sigmf-meta")
    shutil.copy(data, b / "recordings" / "99-extra.sigmf-data")
    write_sums(b)
    assert failed(b) == [7] and "99-extra" in reason7(b)


def test_deleted_recording_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    meta, data = recording(b)
    data.unlink()
    write_sums(b)
    assert 7 in failed(b)


def test_recording_not_hashed_in_sha256sums_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    lines = [ln for ln in (b / "SHA256SUMS").read_text().splitlines() if ".sigmf-meta" not in ln]
    (b / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    f = failed(b)
    assert 1 in f and 7 in f and "not hashed in SHA256SUMS" in reason7(b)


def test_recording_from_another_run_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    meta, _ = recording(b)
    edit_json(meta, lambda m: m["global"].update({"oasa:run_id": "0" * 32}))
    write_sums(b)
    assert failed(b) == [7] and "another run" in reason7(b)


def test_recording_dated_outside_the_run_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    meta, _ = recording(b)
    edit_json(meta, lambda m: m["captures"][0].update({"core:datetime": "2020-01-01T00:00:00.000000Z"}))
    write_sums(b)
    assert failed(b) == [7] and "outside the run" in reason7(b)


# --- bursts against airtime ---------------------------------------------------------------------------
def test_recording_with_no_bursts_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    meta, data = recording(b)
    quiet = bytes([125 + i % 6 for i in range(256)])
    set_data(b, random.Random(1).randbytes(len(data.read_bytes())).translate(quiet))
    assert failed(b) == [7] and "line up" in reason7(b)


def test_recording_shifted_in_time_is_caught(good_control, tmp_path):
    """A real recording, but 1.9 s off from the frames (more than the 1.5 s the clock fit allows)."""
    b = copy_of(good_control, tmp_path)
    meta, _ = recording(b)
    from datetime import datetime, timedelta

    def shift(m):
        t = datetime.fromisoformat(m["captures"][0]["core:datetime"].replace("Z", "+00:00")) + timedelta(seconds=1.9)
        m["captures"][0]["core:datetime"] = t.isoformat(timespec="microseconds").replace("+00:00", "Z")
    edit_json(meta, shift)
    write_sums(b)
    assert failed(b) == [7]


def test_recording_of_the_wrong_length_bursts_is_caught(good_control, tmp_path):
    """The lie is made consistently: the modems and the burst check both claim SF9. The real bursts are SF7."""
    b = copy_of(good_control, tmp_path)

    def lie(m):
        for d in m["hardware"]["devices"]:
            if d["kind"] == "serial-radio":
                d["radio"]["airtime"]["lora_sf"] = 9
        for r in m["recordings"]:
            r["burst_check"]["model"]["lora_sf"] = 9
    edit_json(b / "manifest.json", lie)
    write_sums(b)
    assert failed(b) == [7] and "line up" in reason7(b)


def test_burst_model_that_differs_from_the_declared_radio_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    edit_json(b / "manifest.json", lambda m: [r["burst_check"]["model"].update({"lora_sf": 9}) for r in m["recordings"]])
    write_sums(b)
    assert failed(b) == [7] and "was declared with" in reason7(b)


def test_burst_check_naming_a_missing_air_capture_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    edit_json(b / "manifest.json", lambda m: [r["burst_check"].update({"captures": ["captures/nope.pcap"]}) for r in m["recordings"]])
    write_sums(b)
    assert failed(b) == [7]


# --- the air capture is a live scan -------------------------------------------------------------------
def test_air_capture_without_the_marker_fails_the_negative_control(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    pcap = next((b / "captures").glob("*.pcap"))
    rng = random.Random(4)
    w = PcapWriter(pcap)
    for p in read_pcap(good_control / "captures" / pcap.name):
        w.write(p.src, p.dst, rng.randbytes(len(p.payload)), ts=p.ts)
    w.close()
    write_sums(b)
    assert 4 in failed(b)


def test_air_capture_of_an_unlisted_device_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    edit_json(b / "manifest.json", lambda m: m["captures"][0].update({"device": "heltec_z"}))
    write_sums(b)
    assert 2 in failed(b)


# --- RF safety ----------------------------------------------------------------------------------------
def test_too_little_loss_between_transmitters_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    edit_json(b / "manifest.json", lambda m: m["hardware"]["rf"]["paths"][0].update({"loss_db": 45.0}))
    write_sums(b)
    assert failed(b) == [7] and "below the 60 dB" in reason7(b)


def test_receive_only_path_needs_40_db(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)
    edit_json(b / "manifest.json", lambda m: m["hardware"]["rf"]["paths"][2].update({"loss_db": 30.0}))
    write_sums(b)
    assert failed(b) == [7] and "below the 40 dB" in reason7(b)


def test_over_the_air_is_refused_and_missing_rf_data_is_caught(good_control, tmp_path):
    b = copy_of(good_control, tmp_path / "air")
    edit_json(b / "manifest.json", lambda m: m["hardware"]["rf"].update({"setup": "open-air"}))
    write_sums(b)
    assert failed(b) == [7] and "cabled" in reason7(b)
    b = copy_of(good_control, tmp_path / "none")
    edit_json(b / "manifest.json", lambda m: m["hardware"].update({"rf": None}))
    write_sums(b)
    assert failed(b) == [7] and "no hardware.rf" in reason7(b)


def test_a_run_cannot_drop_the_transmit_flag_or_the_hardware_it_requires(good_control, tmp_path):
    b = copy_of(good_control, tmp_path / "flag")
    edit_json(b / "manifest.json", lambda m: m.update({"transmits": False}))
    write_sums(b)
    assert failed(b) == [7] and "transmits" in reason7(b)
    b = copy_of(good_control, tmp_path / "req")
    edit_json(b / "manifest.json", lambda m: m.update({"requires": []}))
    write_sums(b)
    assert failed(b) == [7] and "dropped hardware" in reason7(b)


def test_a_real_device_needs_a_serial_and_firmware_on_record(good_control, tmp_path):
    b = copy_of(good_control, tmp_path)

    def claim_real(m):
        for d in m["hardware"]["devices"]:
            d["backend"] = "hardware"
            d["serial"] = "n/a"
    edit_json(b / "manifest.json", claim_real)
    write_sums(b)
    assert failed(b) == [7] and "serial number on record" in reason7(b)


# --- measurements and limits --------------------------------------------------------------------------
def edit_log(b, fn):
    rows = [json.loads(ln) for ln in (b / "log.jsonl").read_text().splitlines()]
    fn(rows)
    (b / "log.jsonl").write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    write_sums(b)


def test_loosened_limits_are_caught(good_pluto, tmp_path):
    b = copy_of(good_pluto, tmp_path)

    def loosen(rows):
        for r in rows:
            if r["event"] == "measurement" and r["metric"] == "freq_error_ppm_vs_rtl":
                r["min"], r["max"] = -50, 50
    edit_log(b, loosen)
    assert failed(b) == [7] and "differ from the suite file" in reason7(b)


def test_a_failing_measurement_marked_ok_is_caught(good_pluto, tmp_path):
    b = copy_of(good_pluto, tmp_path)

    def lie(rows):
        for r in rows:
            if r["event"] == "measurement" and r["metric"] == "freq_error_ppm_vs_rtl":
                r["value"] = 22.0       # a plain crystal; "ok" left true
    edit_log(b, lie)
    assert failed(b) == [7] and "the log says ok=True" in reason7(b)
    b2 = copy_of(good_pluto, tmp_path / "b2")

    def honest_but_failing(rows):
        for r in rows:
            if r["event"] == "measurement" and r["metric"] == "freq_error_ppm_vs_rtl":
                r["value"], r["ok"] = 22.0, False
    edit_log(b2, honest_but_failing)
    assert failed(b2) == [7] and "outside its limits" in reason7(b2)


def test_a_limit_nobody_measured_against_is_caught(good_pluto, tmp_path):
    b = copy_of(good_pluto, tmp_path)
    edit_log(b, lambda rows: rows.__setitem__(slice(None), [r for r in rows
                                                            if not (r["event"] == "measurement" and r["metric"] == "port_level_spread_db")]))
    assert failed(b) == [7] and "nothing was measured" in reason7(b)


def test_an_optional_limit_may_stay_unmeasured(good_pluto, tmp_path):
    """Without a calibration the absolute level is not judged; that must not fail the run."""
    b = copy_of(good_pluto, tmp_path)
    edit_log(b, lambda rows: rows.__setitem__(slice(None), [r for r in rows
                                                            if not (r["event"] == "measurement" and r["metric"] == "rx_level_vs_expected_db")]))
    assert failed(b) == []
