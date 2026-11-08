"""The manifest's calibration field, end to end: real runs with and without a calibration, and the checker on old
and new manifests and on tampered copies."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from oasa_lab import calibration as cal
from oasa_lab.bundle import write_sums
from oasa_lab.checker import check_bundle
from oasa_lab.suites import repo_root, suites_dir

EXAMPLE = repo_root() / "calibration.example.toml"


def run(tmp_path, suite, lab_toml=None):
    out = tmp_path / "runs"
    env = {k: v for k, v in os.environ.items() if k not in ("OASA_LAB_INVENTORY", "OASA_LAB_SIM")}
    if lab_toml is not None:
        (tmp_path / "lab.toml").write_text(lab_toml)
        env["OASA_LAB_INVENTORY"] = str(tmp_path / "lab.toml")
    r = subprocess.run([sys.executable, "-m", "oasa_lab", "run", suite, "--out", str(out)],
                       capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)
    return r, next(p for p in out.iterdir() if p.is_dir())


def manifest(b):
    return json.loads((b / "manifest.json").read_text())


def checks(b):
    return {c["id"]: c for c in check_bundle(b, suites_dir())["checks"]}


def edit(b, fn):
    m = manifest(b)
    fn(m)
    (b / "manifest.json").write_text(json.dumps(m, indent=2))
    write_sums(b)


@pytest.fixture(scope="module")
def used(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cal-used")
    shutil.copy(EXAMPLE, tmp / "calibration.toml")
    r, b = run(tmp, "_fixture-calibration", '[calibration]\npath = "calibration.toml"\n')
    assert r.returncode == 0, r.stdout + r.stderr
    return b


@pytest.fixture(scope="module")
def plain(tmp_path_factory):
    r, b = run(tmp_path_factory.mktemp("cal-none"), "_fixture")
    assert r.returncode == 0, r.stdout + r.stderr
    return b


def copy_of(src, tmp_path):
    dst = tmp_path / src.name
    shutil.copytree(src, dst)
    return dst


def test_a_run_without_calibration_records_null(plain):
    assert manifest(plain)["calibration"] is None
    assert not (plain / "calibration.toml").exists()
    assert checks(plain)[2]["status"] == "pass"


def test_an_old_manifest_without_the_field_still_passes(plain, tmp_path):
    b = copy_of(plain, tmp_path)
    edit(b, lambda m: m.pop("calibration"))
    assert check_bundle(b, suites_dir())["ok"]


def test_a_run_that_used_the_calibration_records_it_and_keeps_a_copy(used):
    c = manifest(used)["calibration"]
    assert c == {"sha256": cal.load(EXAMPLE).sha256, "date": "2026-01-15", "version": 1, "valid": True, "used": True}
    assert (used / "calibration.toml").read_bytes() == EXAMPLE.read_bytes()
    assert check_bundle(used, suites_dir())["ok"]
    assert "Bench calibration of 2026-01-15" in (used / "summary.md").read_text()


def test_a_configured_calibration_is_recorded_even_when_not_used(tmp_path):
    shutil.copy(EXAMPLE, tmp_path / "calibration.toml")
    r, b = run(tmp_path, "_fixture", '[calibration]\npath = "calibration.toml"\n')
    assert r.returncode == 0, r.stdout + r.stderr
    c = manifest(b)["calibration"]
    assert c["used"] is False and c["valid"] is True and c["date"] == "2026-01-15"
    assert not (b / "calibration.toml").exists()


def test_a_suite_that_needs_calibration_is_skipped_without_one(tmp_path):
    r, b = run(tmp_path, "_fixture-calibration")
    assert r.returncode == 0, r.stdout + r.stderr
    s = json.loads((b / "results.ctrf.json").read_text())["results"]["summary"]
    assert (s["skipped"], s["passed"]) == (1, 0)
    assert manifest(b)["calibration"] is None


def test_an_invalid_calibration_is_refused_not_skipped(tmp_path):
    (tmp_path / "calibration.toml").write_text(EXAMPLE.read_text().replace("loss_db   = [60.5", "loss_db   = [50.5"))
    r, b = run(tmp_path, "_fixture-calibration", '[calibration]\npath = "calibration.toml"\n')
    assert r.returncode != 0
    s = json.loads((b / "results.ctrf.json").read_text())["results"]["summary"]
    assert s["failed"] == 1
    c = manifest(b)["calibration"]
    assert c["valid"] is False and c["used"] is False


def test_tampered_calibration_copy_is_caught(used, tmp_path):
    b = copy_of(used, tmp_path)
    (b / "calibration.toml").write_text(EXAMPLE.read_text().replace("60.4", "70.4"))
    write_sums(b)   # the sums alone do not save it: the manifest's hash disagrees
    assert checks(b)[2]["status"] == "fail" and "does not match" in checks(b)[2]["reason"]


@pytest.mark.parametrize("name,fn,expect", [
    ("bad hash", lambda m: m["calibration"].update(sha256="abc"), "sha256 must be 64"),
    ("bad date", lambda m: m["calibration"].update(date="15 Jan"), "date must be YYYY-MM-DD"),
    ("impossible date", lambda m: m["calibration"].update(date="2026-02-30"), "date must be YYYY-MM-DD"),
    ("not bool", lambda m: m["calibration"].update(used="yes"), "used must be true or false"),
    ("used invalid", lambda m: m["calibration"].update(valid=False), "only use a valid"),
    ("not object", lambda m: m.update(calibration="calibration.toml"), "must be null or an object"),
    ("hidden copy", lambda m: m.update(calibration=None), "manifest records no calibration"),
    ("copy but unused", lambda m: m["calibration"].update(used=False), "used is false"),
])
def test_bad_calibration_fields_are_caught(used, tmp_path, name, fn, expect):
    b = copy_of(used, tmp_path)
    edit(b, fn)
    c = checks(b)[2]
    assert c["status"] == "fail" and expect in c["reason"], c["reason"]


def test_a_used_calibration_with_its_copy_removed_is_caught(used, tmp_path):
    b = copy_of(used, tmp_path)
    (b / "calibration.toml").unlink()
    write_sums(b)
    assert "not in the bundle" in checks(b)[2]["reason"]
