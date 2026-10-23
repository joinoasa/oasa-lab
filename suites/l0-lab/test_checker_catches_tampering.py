"""The checker must catch each kind of tampering. Each test starts from a real, passing bundle
(made by running the hidden `_fixture` suite and `l1-plaintext-control` through `oasa-lab run`)."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from oasa_lab.bundle import write_sums
from oasa_lab.checker.air import airtime
from oasa_lab.checker import check_bundle, write_check
from oasa_lab.pcap import PcapWriter, read_pcap
from oasa_lab.suites import suites_dir


def make_bundle(tmp_path_factory, suite):
    out = tmp_path_factory.mktemp(f"bundle-{suite}")
    r = subprocess.run([sys.executable, "-m", "oasa_lab", "run", suite, "--out", str(out)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return next(p for p in out.iterdir() if p.is_dir())


@pytest.fixture(scope="module")
def good_encrypted(tmp_path_factory):
    return make_bundle(tmp_path_factory, "_fixture")


@pytest.fixture(scope="module")
def good_control(tmp_path_factory):
    return make_bundle(tmp_path_factory, "l1-plaintext-control")


@pytest.fixture(scope="module")
def good_mixed(tmp_path_factory):
    return make_bundle(tmp_path_factory, "_fixture-mixed")


def copy_of(src, tmp_path):
    dst = tmp_path / src.name
    shutil.copytree(src, dst)
    return dst


def verdict(bundle):
    res = check_bundle(bundle, suites_dir())
    return {c["id"]: c for c in res["checks"]}, res


def failed(bundle):
    return sorted(i for i, c in verdict(bundle)[0].items() if c["status"] == "fail")


def edit_json(path, fn):
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data, indent=2) + "\n")


def test_untouched_bundles_pass(good_encrypted, good_control):
    for b in (good_encrypted, good_control):
        checks, res = verdict(b)
        assert res["ok"], {i: c["reason"] for i, c in checks.items() if c["status"] == "fail"}
        assert [c["status"] for c in checks.values()] == ["pass"] * 7


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="sha256sum not installed")
def test_sha256sum_tool_accepts_the_bundle(good_encrypted):
    r = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=good_encrypted, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_flipped_byte_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    pcap = next((b / "captures").glob("*.pcap"))
    raw = bytearray(pcap.read_bytes())
    raw[-1] ^= 0x01
    pcap.write_bytes(raw)
    assert 1 in failed(b)
    assert "mismatch" in verdict(b)[0][1]["reason"]


def test_missing_file_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    (b / "summary.md").unlink()
    assert 1 in failed(b)
    assert "summary.md" in verdict(b)[0][1]["reason"]


def test_extra_file_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    (b / "notes.txt").write_text("added later")
    assert 1 in failed(b)


def test_symlink_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    (b / "link.txt").symlink_to(b / "summary.md")
    assert 1 in failed(b)


def test_missing_sums_file_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    (b / "SHA256SUMS").unlink()
    assert 1 in failed(b)


def test_ctrf_and_log_disagreeing_is_caught(good_encrypted, tmp_path):
    """Hashes re-made, counts kept consistent inside CTRF: only the cross-check can notice."""
    b = copy_of(good_encrypted, tmp_path)

    def flip(c):
        c["results"]["tests"][0]["status"] = "failed"
        c["results"]["summary"]["passed"] -= 1
        c["results"]["summary"]["failed"] += 1

    edit_json(b / "results.ctrf.json", flip)
    write_sums(b)
    checks, _ = verdict(b)
    assert checks[1]["status"] == "pass"
    assert checks[3]["status"] == "fail"
    assert "log says passed, CTRF says failed" in checks[3]["reason"]


def test_ctrf_summary_miscount_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    edit_json(b / "results.ctrf.json", lambda c: c["results"]["summary"].update(passed=99))
    write_sums(b)
    assert failed(b) == [3]


def test_test_missing_from_log_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    lines = (b / "log.jsonl").read_text().splitlines()
    keep = [ln for ln in lines if not ('"test_end"' in ln and "random_traffic" in ln)]
    assert len(keep) == len(lines) - 1
    (b / "log.jsonl").write_text("\n".join(keep) + "\n")
    write_sums(b)
    assert 3 in failed(b)


def test_junit_disagreeing_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    j = (b / "junit.xml").read_text()
    new = re.sub(r"<testcase ([^>]*?)\s*/>", r"<testcase \1><failure message='x'/></testcase>", j, count=1)
    assert new != j
    (b / "junit.xml").write_text(new)
    write_sums(b)
    assert 3 in failed(b)


def rewrite_pcap(path, fn):
    pk = read_pcap(path)
    w = PcapWriter(path)
    for i, p in enumerate(pk):
        w.write(p.src, p.dst, fn(i, p.payload), ts=p.ts)
    w.close()


def retime_pcap(path, fn):
    """Rewrite the time stamps. fn(packets) returns the new list of time stamps."""
    pk = read_pcap(path)
    stamps = fn(pk)
    w = PcapWriter(path)
    for p, ts in zip(pk, stamps):
        w.write(p.src, p.dst, p.payload, ts=ts)
    w.close()


def airtime_capture(b):
    """The half-duplex channel of the fixture run, its manifest entry and the capture's packets."""
    m = json.loads((b / "manifest.json").read_text())
    ch = next(c for c in m["channels"] if c["settings"].get("airtime"))
    assert ch["settings"]["half_duplex"] and ch["settings"]["profile"] == "sik-64k"
    return ch, b / ch["capture"]


def test_untouched_airtime_capture_passes_the_air_rule(good_encrypted):
    checks, _ = verdict(good_encrypted)
    assert checks[5]["status"] == "pass" and "airtime respected on 1 channel" in checks[5]["reason"]
    row = checks[5]["details"]["airtime"][0]
    assert row["pairs_checked"] >= 70 and row["smallest_margin_ms"] >= -0.05


def test_overlap_on_air_is_caught(good_encrypted, tmp_path):
    """Two packets in one direction on the air at once."""
    b = copy_of(good_encrypted, tmp_path)
    ch, cap = airtime_capture(b)
    air = airtime(ch["settings"], 100, "a_to_b")

    def squeeze(pk):
        ts = [p.ts for p in pk]
        first, second = [i for i, p in enumerate(pk) if p.dst == pk[0].dst][:2]
        ts[second] = ts[first] + air * 0.3
        return ts

    retime_pcap(cap, squeeze)
    write_sums(b)
    checks, _ = verdict(b)
    assert checks[1]["status"] == "pass"
    assert checks[5]["status"] == "fail" and "overlap" in checks[5]["reason"], checks[5]["reason"]


def test_overlap_between_directions_on_a_half_duplex_channel_is_caught(good_encrypted, tmp_path):
    """A and B transmit at the same moment on one medium: only a half-duplex channel forbids it."""
    b = copy_of(good_encrypted, tmp_path)
    ch, cap = airtime_capture(b)

    def collide(pk):
        ts = [p.ts for p in pk]
        i = 0
        j = next(k for k, p in enumerate(pk) if p.dst != pk[0].dst)
        ts[j] = ts[i] + 0.004  # B starts 4 ms into A's packet
        return ts

    retime_pcap(cap, collide)
    write_sums(b)
    checks, _ = verdict(b)
    assert checks[5]["status"] == "fail" and "overlap" in checks[5]["reason"], checks[5]["reason"]
    assert "both directions" in checks[5]["reason"]


def test_claiming_a_slower_medium_than_the_capture_shows_is_caught(good_encrypted, tmp_path):
    """The run says 16 kbps, but its packets were spaced for 64 kbps: they would overlap on a 16 kbps radio."""
    b = copy_of(good_encrypted, tmp_path)
    edit_json(b / "manifest.json", lambda m: next(
        c for c in m["channels"] if c["settings"].get("airtime"))["settings"].update(air_kbps=16.0, fixed_us=0.0))
    write_sums(b)
    assert "overlap" in verdict(b)[0][5]["reason"]


def test_queue_drops_with_no_queue_limit_are_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    ch, _ = airtime_capture(b)
    lines = []
    for ln in (b / "log.jsonl").read_text().splitlines():
        rec = json.loads(ln)
        if rec["event"] == "channel_stats" and rec.get("final") and rec["channel"] == ch["id"]:
            rec["a_to_b"]["queue_dropped"] = 2
            rec["a_to_b"]["sent"] += 2
        lines.append(json.dumps(rec))
    (b / "log.jsonl").write_text("\n".join(lines) + "\n")
    write_sums(b)
    assert "max_queue is 0" in verdict(b)[0][5]["reason"]


def test_bad_airtime_settings_are_caught(good_encrypted, tmp_path):
    for change in (dict(airtime="bogus"), dict(air_kbps=-1.0), dict(duty_cycle=1.5), dict(rate_kbps=64.0)):
        b = copy_of(good_encrypted, tmp_path / str(sorted(change)))
        edit_json(b / "manifest.json", lambda m: next(
            c for c in m["channels"] if c["settings"].get("airtime"))["settings"].update(change))
        write_sums(b)
        assert 2 in failed(b), change


def test_marker_in_encrypted_capture_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    marker = json.loads((b / "manifest.json").read_text())["markers"][0]
    cap = sorted((b / "captures").glob("01-*.pcap"))[0]
    rewrite_pcap(cap, lambda i, pl: marker.encode() + pl[len(marker):] if i == 3 else pl)
    write_sums(b)
    checks, _ = verdict(b)
    assert checks[1]["status"] == "pass"
    assert checks[4]["status"] == "fail" and "readable marker" in checks[4]["reason"]


def test_plaintext_in_encrypted_capture_is_caught(good_encrypted, tmp_path):
    """No marker, but the bytes are plain text: the randomness test must object."""
    b = copy_of(good_encrypted, tmp_path)
    cap = sorted((b / "captures").glob("01-*.pcap"))[0]
    rewrite_pcap(cap, lambda i, pl: pl[:8] + (b"altitude=0123 speed=0045 " * 4)[:len(pl) - 8])
    write_sums(b)
    assert "do not look random" in verdict(b)[0][4]["reason"]


def test_mavlink_in_encrypted_capture_is_caught(good_encrypted, tmp_path):
    from oasa_lab.testing import mavlink2_heartbeat

    b = copy_of(good_encrypted, tmp_path)
    cap = sorted((b / "captures").glob("01-*.pcap"))[0]
    rewrite_pcap(cap, lambda i, pl: mavlink2_heartbeat(i) if i % 4 == 0 else pl)
    write_sums(b)
    assert "MAVLink" in verdict(b)[0][4]["reason"]


def test_empty_encrypted_capture_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    cap = sorted((b / "captures").glob("02-*.pcap"))[0]
    rewrite_pcap(cap, lambda i, pl: pl)  # keep format
    PcapWriter(cap).close()  # now empty
    write_sums(b)
    assert 4 in failed(b)


def test_broken_negative_control_is_caught(good_control, tmp_path):
    """If the capture no longer holds the marker, the control must FAIL, not pass."""
    b = copy_of(good_control, tmp_path)
    cap = next((b / "captures").glob("*.pcap"))
    rewrite_pcap(cap, lambda i, pl: bytes(len(pl)))
    write_sums(b)
    checks, _ = verdict(b)
    assert checks[4]["status"] == "fail" and "negative control failed" in checks[4]["reason"]


def test_flipping_encrypted_flag_is_caught(good_control, tmp_path):
    """A run may not relabel a control capture to dodge the scan: the suite file decides."""
    b = copy_of(good_control, tmp_path)

    def relabel(m):
        for c in m["captures"]:
            c.update(encrypted=False, expect_plaintext=False, expect_mavlink=False)

    edit_json(b / "manifest.json", relabel)
    write_sums(b)
    assert "suite file says" in verdict(b)[0][4]["reason"]


def test_mixed_suite_passes_with_a_control_capture(good_mixed):
    checks, res = verdict(good_mixed)
    assert res["ok"], res
    modes = {c["mode"] for c in checks[4]["details"]["captures"]}
    assert modes == {"encrypted", "negative control"}
    m = json.loads((good_mixed / "manifest.json").read_text())
    assert sorted(c.get("role", "") for c in m["captures"]) == ["", "control"]


def test_control_capture_cannot_drop_its_role(good_mixed, tmp_path):
    """Taking `role: control` off a control capture makes it face the encrypted rules."""
    b = copy_of(good_mixed, tmp_path)

    def drop_role(m):
        for c in m["captures"]:
            c.pop("role", None)

    edit_json(b / "manifest.json", drop_role)
    write_sums(b)
    assert "suite file says" in verdict(b)[0][4]["reason"]


def test_encrypted_capture_cannot_pose_as_a_control(good_mixed, tmp_path):
    """Calling the encrypted capture a control does not hide it: the scanner must find MAVLink."""
    b = copy_of(good_mixed, tmp_path)

    def relabel(m):
        for c in m["captures"]:
            if "role" not in c:
                c.update(role="control", encrypted=False, expect_mavlink=True, skip_header_bytes=0)

    edit_json(b / "manifest.json", relabel)
    write_sums(b)
    assert "negative control failed" in verdict(b)[0][4]["reason"]


def test_control_role_needs_a_control_table(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)

    def mark(m):
        m["captures"][0]["role"] = "control"

    edit_json(b / "manifest.json", mark)
    write_sums(b)
    assert "no [captures.control]" in verdict(b)[0][4]["reason"]


def test_stats_that_lie_are_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    lines = []
    for ln in (b / "log.jsonl").read_text().splitlines():
        rec = json.loads(ln)
        if rec["event"] == "channel_stats" and rec.get("final"):
            rec["a_to_b"]["dropped"] += 7
        lines.append(json.dumps(rec))
    (b / "log.jsonl").write_text("\n".join(lines) + "\n")
    write_sums(b)
    assert 5 in failed(b)


def test_loss_far_from_setting_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    edit_json(b / "manifest.json", lambda m: m["channels"][0]["settings"].update(loss=0.6))
    write_sums(b)
    assert "loss" in verdict(b)[0][5]["reason"]


def test_dropped_expectation_is_caught(good_encrypted, tmp_path):
    """The run can't skip a known-bad case by leaving it out of its own manifest."""
    b = copy_of(good_encrypted, tmp_path)
    edit_json(b / "manifest.json", lambda m: m.update(expectations=[]))
    write_sums(b)
    assert "dropped expectation" in verdict(b)[0][6]["reason"]


def test_expected_test_that_did_not_run_is_caught(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    edit_json(b / "manifest.json", lambda m: m["expectations"].append("test_attacker_never_ran"))
    write_sums(b)
    assert "not in the results" in verdict(b)[0][6]["reason"]


def test_bad_manifest_values_are_caught(good_encrypted, tmp_path):
    for field, value in (("lab", {"version": "x", "git_sha": "abc", "git_dirty": False}),
                         ("seed", "zero"), ("end", "2000-01-01T00:00:00Z"), ("markers", ["short"])):
        b = copy_of(good_encrypted, tmp_path / field)
        edit_json(b / "manifest.json", lambda m: m.update({field: value}))
        write_sums(b)
        assert 2 in failed(b), field


def test_garbage_bundle_fails_without_crashing(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "manifest.json").write_text("not json")
    assert verdict(tmp_path / "x")[1]["ok"] is False


def test_verdict_goes_beside_the_bundle_and_is_bound_to_it(good_encrypted, tmp_path):
    b = copy_of(good_encrypted, tmp_path)
    before = sorted(p.name for p in b.iterdir())
    res = check_bundle(b, suites_dir())
    out = write_check(b, res)
    assert out == tmp_path / (b.name + ".check.json")
    assert sorted(p.name for p in b.iterdir()) == before, "the bundle itself must not change"
    assert json.loads(out.read_text())["sha256sums_sha256"]
    assert check_bundle(b, suites_dir())["ok"], "writing the verdict must not break the bundle"
