"""`oasa-lab job-run`: the installed program that treats the service's request as untrusted data."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import uuid

import pytest

from oasa_lab import jobrunner
from oasa_lab.policy import PolicyError, parse_policy

from .bench import Bench, git, wait_for
from .conftest import SIM_BENCH

ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def bench(lab_repo, tmp_path):
    return Bench(tmp_path / "lab", lab_repo)


def job(bench, job_id=ID, **kw):
    return jobrunner.run_job(job_id, str(bench.policy_path), policy_uid=os.getuid(), **kw)


@pytest.fixture
def spy(monkeypatch):
    """Replace the suite run: record what would have been started."""
    calls = []

    def fake_execute(cmd, env, cwd, log_path, timeout, grace):
        calls.append({"cmd": cmd, "env": env, "cwd": cwd, "timeout": timeout, "grace": grace})
        open(log_path, "w").write("fake run\n")
        return 0, None

    monkeypatch.setattr(jobrunner, "execute", fake_execute)
    return calls


# --- the id and the request ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "abc", "0" * 31, "0" * 33, "A" * 32, "g" * 32, "../../etc/passwd", ID + "\n", ID[:-1] + "\0",
                                 "oasa-lab-job@" + ID, ID + ".json", " " + ID])
def test_a_job_id_must_be_exactly_32_lowercase_hex(bench, spy, bad):
    assert job(bench, bad) == jobrunner.EX_USAGE
    assert list(bench.results.iterdir()) == [] and not spy, "nothing is created and nothing runs"


@pytest.mark.parametrize("raw, why", [
    (b"not json", "not JSON"), (b"[]", "not an object"), (b"\xff\xfe", "not JSON"),
    (json.dumps({"id": ID, "suite": "t-quick", "seed": 0}).encode(), "exactly"),                               # missing commit
    (json.dumps({"id": ID, "suite": "t-quick", "seed": 0, "commit": "a" * 40, "env": {}}).encode(), "exactly"),  # unknown field
    (json.dumps({"id": "f" * 32, "suite": "t-quick", "seed": 0, "commit": "a" * 40}).encode(), "another job"),
    (json.dumps({"id": ID, "suite": "../x", "seed": 0, "commit": "a" * 40}).encode(), "suite"),
    (json.dumps({"id": ID, "suite": "T-QUICK", "seed": 0, "commit": "a" * 40}).encode(), "suite"),
    (json.dumps({"id": ID, "suite": "t-quick\n", "seed": 0, "commit": "a" * 40}).encode(), "suite"),
    (json.dumps({"id": ID, "suite": "x" * 65, "seed": 0, "commit": "a" * 40}).encode(), "suite"),
    (json.dumps({"id": ID, "suite": 5, "seed": 0, "commit": "a" * 40}).encode(), "suite"),
    (json.dumps({"id": ID, "suite": "t-quick", "seed": True, "commit": "a" * 40}).encode(), "seed"),
    (json.dumps({"id": ID, "suite": "t-quick", "seed": -1, "commit": "a" * 40}).encode(), "seed"),
    (json.dumps({"id": ID, "suite": "t-quick", "seed": 2**31, "commit": "a" * 40}).encode(), "seed"),
    (json.dumps({"id": ID, "suite": "t-quick", "seed": "1", "commit": "a" * 40}).encode(), "seed"),
    (json.dumps({"id": ID, "suite": "t-quick", "seed": 1.5, "commit": "a" * 40}).encode(), "seed"),
    (json.dumps({"id": ID, "suite": "t-quick", "seed": 0, "commit": "HEAD"}).encode(), "commit"),
    (json.dumps({"id": ID, "suite": "t-quick", "seed": 0, "commit": "A" * 40}).encode(), "commit"),
    (json.dumps({"id": ID, "suite": "t-quick", "seed": 0, "commit": "a" * 39}).encode(), "commit"),
    (json.dumps({"id": ID, "suite": "t-quick", "seed": 0, "commit": ("a" * 40) + "\n"}).encode(), "commit"),
])
def test_a_bad_request_is_refused_before_anything_is_cloned_or_run(bench, spy, raw, why):
    (bench.spool / f"{ID}.json").write_bytes(raw)
    assert job(bench) == jobrunner.EX_REFUSED
    info = bench.exit_info(ID)
    assert info["code"] is None and why in info["error"], info
    assert not spy


def test_a_request_that_is_too_big_is_refused(bench, spy):
    (bench.spool / f"{ID}.json").write_text(json.dumps({"id": ID, "suite": "t-quick", "seed": 0, "commit": "a" * 40, "pad": "x" * 5000}))
    assert job(bench) == jobrunner.EX_REFUSED and "too big" in bench.exit_info(ID)["error"]


def test_a_missing_request_is_refused(bench, spy):
    assert job(bench) == jobrunner.EX_REFUSED and "no usable request" in bench.exit_info(ID)["error"]


def test_a_request_that_is_a_link_or_a_pipe_is_refused(bench, spy, tmp_path):
    good = tmp_path / "elsewhere.json"
    good.write_text(json.dumps({"id": ID, "suite": "t-quick", "seed": 0, "commit": bench.head()}))
    (bench.spool / f"{ID}.json").symlink_to(good)               # a valid request, but behind a link
    assert job(bench) == jobrunner.EX_REFUSED and "no usable request" in bench.exit_info(ID)["error"]
    other = "1" * 32
    os.mkfifo(bench.spool / f"{other}.json")                    # would block a naive reader forever
    assert job(bench, other) == jobrunner.EX_REFUSED
    assert not spy


def test_a_spool_folder_that_is_a_link_is_refused(bench, spy, tmp_path):
    real = tmp_path / "real-spool"
    bench.spool.rename(real)
    bench.spool.symlink_to(real)
    (real / f"{ID}.json").write_text(json.dumps({"id": ID, "suite": "t-quick", "seed": 0, "commit": bench.head()}))
    assert job(bench) == jobrunner.EX_REFUSED and not spy


# --- the commit ---------------------------------------------------------------------------------------
def test_a_commit_that_is_not_on_main_is_refused(bench, spy, lab_repo, tmp_path):
    side = tmp_path / "side"
    git("clone", "-q", str(lab_repo), str(side))
    git("checkout", "-q", "-b", "side", cwd=side)
    (side / "x.txt").write_text("x")
    git("add", "-A", cwd=side)
    git("commit", "-q", "-m", "side", cwd=side)
    sha = git("rev-parse", "HEAD", cwd=side)
    git("--git-dir", str(bench.mirror), "fetch", "-q", str(side), "refs/heads/side:refs/heads/side")   # in the mirror, not on main
    bench.request(ID, commit=sha)
    assert job(bench) == jobrunner.EX_REFUSED and "not on main" in bench.exit_info(ID)["error"]
    bench.request("2" * 32, commit="0" * 40)                     # not in the mirror at all
    assert job(bench, "2" * 32) == jobrunner.EX_REFUSED and "not on main" in bench.exit_info("2" * 32)["error"]
    assert not spy


def test_a_commit_below_the_floor_is_refused_and_the_floor_itself_is_allowed(bench, spy, lab_repo, tmp_path):
    up = tmp_path / "up"
    git("clone", "-q", str(lab_repo), str(up))
    old = git("rev-parse", "HEAD", cwd=up)
    (up / "y.txt").write_text("y")
    git("add", "-A", cwd=up)
    git("commit", "-q", "-m", "second", cwd=up)
    new = git("rev-parse", "HEAD", cwd=up)
    git("--git-dir", str(bench.mirror), "fetch", "-q", "--force", str(up), "refs/heads/main:refs/heads/main")
    bench.write_policy(min_commit=new)
    bench.request(ID, commit=old)
    assert job(bench) == jobrunner.EX_REFUSED and "older than the lowest" in bench.exit_info(ID)["error"]
    assert not spy
    other = "3" * 32
    bench.request(other, commit=new)
    assert job(bench, other) == 0 and len(spy) == 1


def test_a_mirror_that_others_can_write_is_refused(bench, spy):
    bench.mirror.chmod(0o777)
    bench.request(ID)
    assert job(bench) == jobrunner.EX_REFUSED and "writable" in bench.exit_info(ID)["error"]
    assert not spy


def test_the_job_runs_from_a_private_clone_with_no_hooks_and_it_is_removed(bench, spy):
    bench.request(ID)
    assert job(bench) == 0
    (call,) = spy
    assert call["cwd"].endswith("/src") and "/job-" in call["cwd"]
    assert not os.path.exists(call["cwd"]), "the private temp dir is gone"


# --- the suite and the transmit lock --------------------------------------------------------------------
def test_an_unknown_or_linked_suite_is_refused(bench, spy):
    bench.request(ID, suite="no-such-suite")
    assert job(bench) == jobrunner.EX_REFUSED and "no suite named" in bench.exit_info(ID)["error"]
    assert not spy


@pytest.mark.parametrize("policy, suite, refused", [
    (dict(rf_setup="unset", simulated=False), "t-tx", "rf_setup"),
    (dict(rf_setup="open", simulated=False, confirm={"t-tx": "lora"}), "t-tx", "rf_setup"),
    (dict(rf_setup="unset", simulated=True), "t-tx", "rf_setup"),
    (dict(rf_setup="cabled", simulated=False), "t-tx", "transmits on real hardware"),
    (dict(rf_setup="shielded", simulated=False, confirm={"t-tx": "pluto"}), "t-tx", "transmits on real hardware"),
    (dict(rf_setup="cabled", simulated=False, confirm={"t-quick": "lora"}), "t-tx", "transmits on real hardware"),
    (dict(rf_setup="cabled", simulated=False), "t-needs", "does not say transmits"),
])
def test_the_transmit_lock_is_applied_before_any_suite_code_runs(bench, spy, policy, suite, refused):
    bench.write_policy(**policy)
    bench.request(ID, suite=suite)
    assert job(bench) == jobrunner.EX_REFUSED
    assert refused in bench.exit_info(ID)["error"]
    assert not spy, "the suite must not have been started"
    assert refused in (bench.results / ID / "stdout.log").read_text(), "the refusal is in the job's log too"


@pytest.mark.parametrize("policy, suite, confirm", [
    (dict(rf_setup="cabled", simulated=False, confirm={"t-tx": "lora"}), "t-tx", "lora"),
    (dict(rf_setup="shielded", simulated=False, confirm={"t-tx": "lora"}), "t-tx", "lora"),
    (dict(rf_setup="cabled", simulated=True), "t-tx", None),                                   # simulated bench: nothing to confirm
    (dict(rf_setup="unset", simulated=False, confirm={"t-tx": "lora"}), "t-quick", None),      # does not transmit
    (dict(rf_setup="cabled", simulated=False, confirm={"t-tx": "lora", "t-quick": "lora"}), "t-quick", None),
])
def test_the_wiring_is_confirmed_only_when_the_policy_allows_it(bench, spy, policy, suite, confirm):
    bench.write_policy(**policy)
    bench.request(ID, suite=suite)
    assert job(bench) == 0
    (call,) = spy
    assert ("--confirm-wiring" in call["cmd"]) == bool(confirm)
    if confirm:
        assert call["cmd"][-2:] == ["--confirm-wiring", confirm] and call["env"]["OASA_LAB_WIRING_CONFIRMED"] == confirm
    else:
        assert "OASA_LAB_WIRING_CONFIRMED" not in call["env"]


def test_the_suite_gets_a_minimal_environment_from_the_policy(bench, spy, monkeypatch):
    monkeypatch.setenv("SECRET_FOR_TEST", "hunter2")
    monkeypatch.setenv("OASA_SEC_BIN_DIR", "/evil/bin")        # the caller's environment is never inherited
    dev = bench.root / "lab.toml"
    dev.write_text(SIM_BENCH)
    bench.write_policy(rf_setup="shielded", simulated=False, devices_file=str(dev), bins={"sec": "/opt/oasa-sec/bin", "radio": "/opt/oasa-radio/bin"})
    bench.request(ID)
    assert job(bench) == 0
    (call,) = spy
    env = call["env"]
    assert set(env) == {"PATH", "HOME", "LANG", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "OASA_LAB_JOB_ID", "OASA_LAB_RF_SETUP",
                        "OASA_LAB_GIT_SHA", "OASA_LAB_INVENTORY", "OASA_SEC_BIN_DIR", "OASA_RADIO_BIN_DIR"}, sorted(env)
    assert env["OASA_LAB_RF_SETUP"] == "shielded" and env["OASA_LAB_INVENTORY"] == str(dev) and "OASA_LAB_SIM" not in env
    assert env["OASA_SEC_BIN_DIR"] == "/opt/oasa-sec/bin" and env["OASA_RADIO_BIN_DIR"] == "/opt/oasa-radio/bin"
    assert env["OASA_LAB_JOB_ID"] == ID and env["OASA_LAB_GIT_SHA"] == bench.head()
    assert "hunter2" not in json.dumps(env)
    assert call["cmd"][1:] == ["-m", "oasa_lab", "run", "t-quick", "--seed", "0", "--out", str(bench.results / ID)]


def test_the_simulated_bench_is_the_policys_word(bench, spy):
    bench.request(ID)
    assert job(bench) == 0
    assert spy[0]["env"]["OASA_LAB_SIM"] == "1" and "OASA_LAB_INVENTORY" not in spy[0]["env"]


# --- one job at a time, one run per id --------------------------------------------------------------------
def test_a_second_job_is_refused_while_one_runs(bench, spy):
    bench.request(ID)
    fd = os.open(bench.results / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)                              # "the first job"
    try:
        assert job(bench) == jobrunner.EX_BUSY
        assert not (bench.results / ID).exists() and not spy
    finally:
        os.close(fd)
    assert job(bench) == 0 and len(spy) == 1


def test_the_owners_pause_file_stops_a_job_even_if_the_service_asks(bench, spy):
    bench.request(ID)
    (bench.root / "PAUSE").write_text("")
    assert job(bench) == jobrunner.EX_REFUSED and "paused" in bench.exit_info(ID)["error"] and not spy
    (bench.root / "PAUSE").unlink()
    (bench.root / "PAUSE").mkdir()                              # a folder, a dangling link: anything at that path pauses
    other = "4" * 32
    bench.request(other)
    assert job(bench, other) == jobrunner.EX_REFUSED and "paused" in bench.exit_info(other)["error"]


def test_an_unexpected_failure_still_leaves_a_result(bench, monkeypatch):
    bench.request(ID)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(jobrunner, "execute", boom)
    assert job(bench) == jobrunner.EX_REFUSED
    info = bench.exit_info(ID)
    assert info["code"] is None and "No space left" in info["error"] and info["reason"] == "error"


def test_an_id_runs_once(bench, spy):
    bench.request(ID)
    assert job(bench) == 0
    assert job(bench) == jobrunner.EX_REFUSED and len(spy) == 1


# --- folders swapped for links ------------------------------------------------------------------------------
def test_a_results_folder_for_this_id_that_is_a_link_is_not_followed(bench, spy, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (bench.results / ID).symlink_to(outside)
    bench.request(ID)
    assert job(bench) == jobrunner.EX_REFUSED
    assert list(outside.iterdir()) == [], "nothing was written through the link"
    assert not spy


def test_a_results_folder_that_is_a_link_is_refused(bench, spy, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    bench.results.rmdir()
    bench.results.symlink_to(outside)
    bench.request(ID)
    assert job(bench) == jobrunner.EX_REFUSED
    assert list(outside.iterdir()) == [] and not spy


def test_old_results_are_pruned_without_following_links(bench, spy, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (bench.results / "old1").mkdir()
    (bench.results / "old1" / "f").write_text("x")
    (bench.results / "old2").symlink_to(outside)
    (bench.results / "stray.txt").write_text("x")
    bench.request(ID)
    assert job(bench) == 0
    assert sorted(p.name for p in bench.results.iterdir()) == [".lock", ID]
    assert (outside / "keep.txt").read_text() == "keep", "a link in results/ is removed, its target is not touched"


def test_the_result_is_readable_by_the_group_and_exit_json_comes_last(bench, spy, monkeypatch):
    bench.request(ID)

    def fake_execute(cmd, env, cwd, log_path, timeout, grace):
        d = bench.results / ID / "bundle" / "sub"
        d.mkdir(parents=True)
        (d / "f.txt").write_text("x")
        os.chmod(d / "f.txt", 0o600)
        os.symlink("/etc/hostname", bench.results / ID / "bundle" / "leak")
        open(log_path, "w").write("x")
        return 0, None

    monkeypatch.setattr(jobrunner, "execute", fake_execute)
    assert job(bench) == 0
    mode = lambda p: (bench.results / ID / p).stat().st_mode & 0o777  # noqa: E731
    assert mode("bundle/sub/f.txt") & 0o040 and mode("bundle/sub") & 0o050 == 0o050 and mode(".") & 0o050 == 0o050
    assert bench.exit_info(ID) == {"code": 0, "error": None, "reason": None}
    assert not (bench.results / ID / ".exit.json.tmp").exists()


# --- the policy file ---------------------------------------------------------------------------------------
def test_the_policy_must_be_a_plain_file_owned_by_root_and_closed(bench, spy, tmp_path):
    bench.request(ID)
    assert jobrunner.run_job(ID, str(bench.policy_path), policy_uid=os.getuid() + 1) == jobrunner.EX_REFUSED   # wrong owner
    bench.policy_path.chmod(0o666)
    assert job(bench) == jobrunner.EX_REFUSED                                                                    # others can write
    bench.policy_path.chmod(0o664)
    assert job(bench) == jobrunner.EX_REFUSED                                                                    # group can write
    bench.policy_path.chmod(0o644)
    link = tmp_path / "link.toml"
    link.symlink_to(bench.policy_path)
    assert jobrunner.run_job(ID, str(link), policy_uid=os.getuid()) == jobrunner.EX_REFUSED
    assert jobrunner.run_job(ID, str(tmp_path / "missing.toml"), policy_uid=os.getuid()) == jobrunner.EX_REFUSED
    assert not spy and not list(bench.results.iterdir())


@pytest.mark.parametrize("text, why", [
    ('[lab]\nrf_setup = "anechoic"\n', "rf_setup"),
    ('[lab]\nrf_setup = "cabled"\nsurprise = 1\n', "unknown key"),
    ('[labs]\n', "unknown section"),
    ('top = 1\n', "unknown section"),
    ('[lab]\nmin_commit = "abc"\n', "min_commit"),
    ('[lab]\nbranch = "-x"\n', "branch"),
    ('[lab]\njob_timeout_seconds = 99999\n', "job_timeout_seconds"),
    ('[lab]\nsimulated = "yes"\n', "simulated"),
    ('[bins]\nsec = "relative/bin"\n', "absolute"),
    ('[bins]\nsec = "/opt/../etc"\n', "absolute"),
    ('[confirm_wiring]\n"../x" = "w"\n', "confirm_wiring"),
    ('[confirm_wiring]\nt-tx = "w; rm"\n', "confirm_wiring"),
    ('[paths]\nspool = ""\n', "spool"),
])
def test_a_policy_with_a_typo_or_a_bad_value_is_an_error_not_a_default(text, why):
    import tomllib

    with pytest.raises(PolicyError, match=why):
        parse_policy(tomllib.loads(text))


def test_the_policy_example_in_deploy_is_valid():
    from pathlib import Path

    import tomllib

    text = (Path(__file__).resolve().parents[2] / "deploy" / "lab-policy.toml.example").read_text()
    pol = parse_policy(tomllib.loads(text))
    assert pol.rf_setup == "unset" and not pol.simulated and pol.confirm_wiring == {}


def test_there_is_no_way_for_a_caller_to_pick_the_policy_or_paths():
    # the command line takes one id; --policy is accepted only as the second and third word, which only root's unit file writes
    assert jobrunner.main([]) == jobrunner.EX_USAGE
    assert jobrunner.main([ID, "extra"]) == jobrunner.EX_USAGE
    assert jobrunner.main([ID, "--results", "/tmp"]) == jobrunner.EX_USAGE
    assert jobrunner.main([ID, "--policy", "/nonexistent/policy.toml"]) == jobrunner.EX_REFUSED
    assert "OASA_LAB" not in open(jobrunner.__file__).read().split("def run_job")[1].split("load_policy")[0], "no environment switch for the policy"


# --- stopping and timing out, with the real suites -----------------------------------------------------------
def spawn(bench, job_id, grace=1):
    code = ("import os, sys; from oasa_lab.jobrunner import run_job; "
            f"sys.exit(run_job(sys.argv[1], {str(bench.policy_path)!r}, policy_uid=os.getuid()))")
    return subprocess.Popen([sys.executable, "-c", code, job_id], cwd="/", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def started(bench, job_id):
    return wait_for(lambda: any((bench.results / job_id).glob("*/log.jsonl")), 60)


@pytest.mark.parametrize("suite, code", [("t-slow", -signal.SIGTERM), ("t-stubborn", -signal.SIGKILL)])
def test_sigterm_stops_the_suite_and_the_result_says_so(bench, suite, code):
    job_id = uuid.uuid4().hex
    bench.request(job_id, suite=suite)
    proc = spawn(bench, job_id)
    assert started(bench, job_id), "the suite never started"
    time.sleep(1.5)                                            # let the stubborn suite install its SIGTERM handler
    t0 = time.time()
    proc.send_signal(signal.SIGTERM)
    proc.wait(30)
    assert time.time() - t0 < 20
    info = bench.exit_info(job_id)
    assert info["reason"] == "stopped" and info["code"] in (code, 1, 0), info
    if suite == "t-stubborn":
        assert info["code"] == -signal.SIGKILL, "a suite that ignores SIGTERM is killed after the grace period"
    assert not list(bench.results.glob("*/.exit.json.tmp"))


def test_a_suite_that_runs_too_long_is_cut_off(bench, monkeypatch):
    monkeypatch.setattr(jobrunner, "BACKSTOP", 0.0)
    bench.write_policy(job_timeout=3)
    bench.request(ID, suite="t-slow")
    assert job(bench) in (0, 1, 3)
    info = bench.exit_info(ID)
    assert info["reason"].startswith("timed out"), info


def test_a_full_run_writes_the_bundle_the_log_and_the_exit_file(bench):
    bench.request(ID, suite="t-quick", seed=7)
    assert job(bench) == 0
    res = bench.results / ID
    bundles = [p for p in res.iterdir() if p.is_dir()]
    assert len(bundles) == 1 and (bundles[0] / "results.ctrf.json").is_file()
    assert "tests:" in (res / "stdout.log").read_text()
    assert bench.exit_info(ID)["code"] == 0
    assert (res.stat().st_mode & 0o750) == 0o750
