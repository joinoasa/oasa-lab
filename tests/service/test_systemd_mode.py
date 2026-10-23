"""The service in `sandbox.mode = "systemd"`: a request in the spool folder, a unit started through systemctl, the result
read back from the results folder. systemd is a stand-in (bench.py); the real one is checked by deploy/selftest.sh."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
import uuid
from pathlib import Path

import pytest

from oasa_lab.service.config import ConfigError, parse_config
from oasa_lab.service.jobs import Job
from oasa_lab.service.sandbox import Launch

from .bench import Bench, git, wait_for
from .conftest import SIM_BENCH, start, stop

JOB_UNIT = re.compile(r"^oasa-lab-job@[0-9a-f]{32}\.service$")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def lab(lab_repo, tmp_path):
    made = []

    def make(rf_setup="cabled", simulated=True, extra=None, devices=None, **policy_kw):
        up = tmp_path / f"upstream{len(made)}"
        git("clone", "-q", str(lab_repo), str(up))
        root = tmp_path / f"lab{len(made)}"
        bench = Bench(root, up)
        if devices:
            (root / "devices.toml").write_text(devices)
            policy_kw["devices_file"] = str(root / "devices.toml")
        bench.write_policy(rf_setup=rf_setup, simulated=simulated, **policy_kw)
        svc = start(up, root / "state", policy_uid=os.getuid(), extra={
            "sandbox": {"mode": "systemd", "policy": str(bench.policy_path), "systemctl": str(bench.ctl)},
            **({} if simulated else {"inventory": {"backend": "usb"}}), **(extra or {})})
        svc.lab.checkouts.refresh_every = 0          # ask the (fake) mirror unit every time, so tests see new commits at once
        made.append((svc, bench))
        svc.bench, svc.upstream = bench, up
        return svc

    yield make
    for svc, bench in made:
        stop(svc)
        for pidfile in bench.fake.glob("*.pid"):     # a job-run that outlived its test
            try:
                os.killpg(int(pidfile.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass


def running(svc, job_id):
    return wait_for(lambda: svc.lab.jobs.get(job_id).status == "running" and any((svc.bench.results / job_id).glob("*/log.jsonl")), 60)


def test_a_job_runs_in_a_unit_and_its_evidence_is_copied_out_and_judged_by_the_service(lab):
    svc = lab()
    done = svc.wait(svc.submit("t-env")["id"])
    assert done["status"] == "passed" and done["check_ok"] is True and re.fullmatch(r"[0-9a-f]{32}", done["id"]), done
    unit = f"oasa-lab-job@{done['id']}.service"
    calls = svc.bench.calls()
    assert ["--no-ask-password", "start", "--no-block", unit] in calls
    assert ["--no-ask-password", "start", "oasa-lab-mirror.service"] in calls, "the mirror is refreshed before a commit is picked"
    # the service asked for nothing but: start/stop of a job unit, its state, and the mirror refresh
    for c in calls:
        words = [a for a in c if not a.startswith("--")]
        assert words[0] in ("start", "stop", "is-active"), c
        assert JOB_UNIT.match(words[-1]) or words == ["start", "oasa-lab-mirror.service"], c
    jd = svc.cfg.state_dir / "jobs" / done["id"]
    assert (jd / "verified" / done["bundle"]).is_dir()
    assert not (jd / "src").exists() and not (jd / "judge").exists() and not (jd / "runs").exists()
    assert not (svc.bench.spool / f"{done['id']}.json").exists(), "the request is removed afterwards"
    assert svc.bench.exit_info(done["id"])["code"] == 0
    assert "tests:" in (jd / "stdout.log").read_text(), "the job's output was copied into the service's log"
    with svc.client() as c:
        res = c.get(f"/api/jobs/{done['id']}/results").json()
        assert res["counted"] is True and res["checker"]["ok"] is True
        assert c.get(f"/api/jobs/{done['id']}/log").status_code == 200


def test_the_request_holds_only_what_the_job_side_needs(lab, monkeypatch):
    svc = lab()
    seen = {}
    orig = svc.lab.jobs.sandbox._write_request

    def spy(launch):
        orig(launch)
        seen.update(json_load(svc.bench.spool / f"{launch.job_id}.json"))

    def json_load(p):
        import json

        return json.loads(p.read_text())

    monkeypatch.setattr(svc.lab.jobs.sandbox, "_write_request", spy)
    done = svc.wait(svc.submit("t-quick", seed=5)["id"])
    assert set(seen) == {"id", "suite", "seed", "commit"} and seen["seed"] == 5 and seen["commit"] == done["commit"] == svc.bench.head()


def test_a_live_log_is_readable_while_the_job_runs_and_stop_asks_for_the_unit_to_stop(lab):
    svc = lab()
    job = svc.submit("t-slow")
    assert running(svc, job["id"])
    j = svc.lab.jobs.get(job["id"])
    assert svc.lab.jobs.read_log(j)[0], "the live log.jsonl comes from the job's results folder"
    with svc.client() as c:
        r = c.post(f"/api/jobs/{job['id']}/stop", json={})
    assert r.status_code == 200 and r.json()["status"] == "stopped", r.text
    assert ["--no-ask-password", "stop", f"oasa-lab-job@{job['id']}.service"] in svc.bench.calls()


def test_a_suite_that_ignores_the_stop_is_killed_after_the_grace_period(lab):
    svc = lab()
    job = svc.submit("t-stubborn")
    assert running(svc, job["id"])
    time.sleep(1.5)
    t0 = time.time()
    with svc.client() as c:
        assert c.post(f"/api/jobs/{job['id']}/stop", json={}).json()["status"] == "stopped"
    assert time.time() - t0 < 30
    assert svc.bench.exit_info(job["id"])["code"] == -signal.SIGKILL


def test_a_job_that_runs_too_long_is_stopped(lab):
    svc = lab(job_timeout=3)
    done = svc.wait(svc.submit("t-slow")["id"])
    assert done["status"] == "stopped" and "timed out" in done["stop_reason"], done


def test_if_systemd_refuses_to_start_the_unit_the_job_fails_cleanly(lab):
    svc = lab()
    (svc.bench.fake / "deny").write_text("")
    done = svc.wait(svc.submit("t-quick")["id"])
    assert done["status"] == "error" and "cannot start the job unit" in done["stop_reason"], done
    assert not list(svc.bench.spool.iterdir()), "the request is not left behind"
    assert "authentication" in (svc.cfg.state_dir / "jobs" / done["id"] / "stdout.log").read_text().lower()


def test_when_job_run_refuses_the_reason_reaches_the_job(lab):
    """The service believed the old policy; job-run read the current one and refused the transmitting suite."""
    svc = lab(rf_setup="cabled")
    svc.bench.write_policy(rf_setup="unset")           # the owner changed it after the service started
    done = svc.wait(svc.submit("t-tx")["id"])
    assert done["status"] == "error" and "rf_setup" in done["stop_reason"], done
    assert done["bundle"] is None


def test_job_run_applies_the_lock_even_if_the_service_were_lying(lab):
    """A service that writes its own request (the compromised case) still meets the lock."""
    svc = lab(rf_setup="unset", simulated=False)
    job_id = uuid.uuid4().hex
    svc.bench.request(job_id, suite="t-tx")
    from oasa_lab import jobrunner

    assert jobrunner.run_job(job_id, str(svc.bench.policy_path), policy_uid=os.getuid()) == jobrunner.EX_REFUSED
    assert "rf_setup" in svc.bench.exit_info(job_id)["error"]


def test_real_hardware_runs_only_with_the_wiring_the_policy_confirms(lab):
    svc = lab(rf_setup="shielded", simulated=False, confirm={"t-tx": "lora"}, devices=SIM_BENCH)
    done = svc.wait(svc.submit("t-tx")["id"])
    assert done["status"] == "passed" and done["wiring_confirmed"] == "lora", done


def test_a_new_commit_on_main_is_picked_up_through_the_mirror_refresh(lab):
    svc = lab()
    d = svc.upstream / "suites" / "t-fresh"
    d.mkdir()
    (d / "suite.toml").write_text('description = "added after the service started"\n[captures]\nencrypted = false\n')
    (d / "test_t_fresh.py").write_text("def test_fresh(lab):\n    assert True\n")
    git("add", "-A", cwd=svc.upstream)
    git("commit", "-q", "-m", "fresh suite", cwd=svc.upstream)
    done = svc.wait(svc.submit("t-fresh")["id"])
    assert done["status"] == "passed" and done["commit"] == git("rev-parse", "HEAD", cwd=svc.upstream)


def test_commits_off_main_or_below_the_floor_are_refused_by_the_service_too(lab):
    svc = lab()
    with svc.client() as c:
        assert c.post("/api/jobs", json={"suite": "t-quick", "commit": "0" * 40}).status_code == 400
    head = svc.bench.head()
    svc2 = lab(min_commit=head)
    with svc2.client() as c:
        assert c.post("/api/jobs", json={"suite": "t-quick", "commit": head}).status_code == 202


# --- what the service reads back is never trusted ------------------------------------------------------------
def fake_job(svc, job_id=None):
    return Job(id=job_id or uuid.uuid4().hex, suite="t-quick", seed=0, profile=None, requested_by="owner", via="api",
               commit="a" * 40, created="2026-01-01T00:00:00Z", status="running")


def test_a_results_folder_swapped_for_a_link_is_not_followed(lab, tmp_path):
    svc = lab()
    jobs = svc.lab.jobs
    secret = tmp_path / "secret"
    (secret / "20260101T000000Z-t-quick-deadbeef").mkdir(parents=True)
    (secret / "20260101T000000Z-t-quick-deadbeef" / "token.txt").write_text("hunter2")
    (secret / "20260101T000000Z-t-quick-deadbeef" / "log.jsonl").write_text('{"leak": true}\n')
    (secret / "stdout.log").write_text("hunter2\n")
    job = fake_job(svc)
    jobs.jobs[job.id] = job
    (svc.bench.results / job.id).symlink_to(secret)          # swapped for a link to somewhere else
    jd = jobs.job_dir(job)
    jd.mkdir(parents=True)
    assert run(jobs._collect(job, jd, jd)) is None and job.bundle is None
    assert not (jd / "verified").exists(), "nothing was copied from behind the link"
    assert jobs.read_log(job) == ([], 0) and jobs.tail_log(job) == []
    sb = jobs.sandbox
    assert sb._read_exit(job.id) is None
    out = open(jd / "copy.log", "wb")
    assert sb._copy_log(svc.bench.results / job.id, 0, out) == 0 and (jd / "copy.log").read_bytes() == b""


def test_a_bundle_folder_swapped_for_a_link_inside_results_is_not_followed(lab, tmp_path):
    svc = lab()
    jobs = svc.lab.jobs
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("x")
    job = fake_job(svc)
    jobs.jobs[job.id] = job
    res = svc.bench.results / job.id
    res.mkdir()
    (res / "20260101T000000Z-t-quick-deadbeef").symlink_to(outside)
    (res / "stdout.log").symlink_to(outside / "f")
    jd = jobs.job_dir(job)
    jd.mkdir(parents=True)
    assert run(jobs._collect(job, jd, jd)) is None and job.bundle is None and not (jd / "verified").exists()
    assert jobs.sandbox._copy_log(res, 0, open(jd / "c.log", "wb")) == 0


def test_the_request_is_never_written_through_a_link_or_over_a_file(lab, tmp_path):
    svc = lab()
    sb = svc.lab.jobs.sandbox
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    job_id = uuid.uuid4().hex
    (svc.bench.spool / f"{job_id}.json").symlink_to(victim)
    launch = Launch(job_id, tmp_path, "t-quick", 0, "a" * 40, None)
    with pytest.raises(OSError):
        sb._write_request(launch)
    assert victim.read_text() == "keep"
    with pytest.raises(ValueError):
        sb._write_request(Launch("../" + job_id, tmp_path, "t-quick", 0, "a" * 40, None))


def test_an_exit_file_that_lies_is_read_as_data(lab):
    svc = lab()
    sb = svc.lab.jobs.sandbox
    job_id = uuid.uuid4().hex
    res = svc.bench.results / job_id
    res.mkdir()
    for text, want in [('{"code": true}', None), ('{"code": "0"}', None), ('{"code": 0}', 0), ("[]", None), ("nope", None)]:
        (res / "exit.json").write_text(text)
        info = sb._read_exit(job_id)
        got = info.get("code") if isinstance(info, dict) else None
        got = got if isinstance(got, int) and not isinstance(got, bool) else None
        assert got == want, text


def test_a_job_unit_that_vanishes_without_a_result_is_an_error_not_a_pass(lab, monkeypatch):
    svc = lab()
    sb = svc.lab.jobs.sandbox
    monkeypatch.setattr("oasa_lab.service.sandbox.START_GRACE", 0.2)

    async def go():
        job_id = uuid.uuid4().hex
        (svc.bench.results / job_id).mkdir()
        proc = __import__("oasa_lab.service.sandbox", fromlist=["UnitProc"]).UnitProc()
        launch = Launch(job_id, svc.bench.root, "t-quick", 0, "a" * 40, None)
        with open(svc.bench.root / "o.log", "wb") as out:
            await asyncio.wait_for(sb._watch(launch, out, proc), 20)
        return proc

    proc = run(go())
    assert proc.returncode == -1 and "without a result" in proc.detail


# --- config in systemd mode -----------------------------------------------------------------------------------
BASE = {"service": {"port": 8765, "public_url": "http://127.0.0.1:8765"}, "users": [{"name": "o", "role": "admin", "github_id": 1}]}


def test_systemd_mode_reads_the_lab_rules_from_the_policy_and_nowhere_else(lab, tmp_path):
    svc = lab(rf_setup="shielded", confirm={"t-tx": "lora"}, min_commit="a" * 40, job_timeout=77, grace=3)
    c = svc.cfg
    assert (c.rf_setup, c.confirm_wiring, c.min_commit, c.job_timeout_seconds, c.stop_grace_seconds) == ("shielded", {"t-tx": "lora"}, "a" * 40, 77, 3)
    pol = svc.bench.policy_path
    for extra in ({"lab": {"rf_setup": "cabled"}}, {"confirm_wiring": {"t-tx": "lora"}}, {"service": {"min_commit": "a" * 40}},
                  {"service": {"devices_file": "/x"}}, {"service": {"job_timeout_seconds": 5}}, {"service": {"branch": "x"}}):
        data = {**BASE, "sandbox": {"mode": "systemd", "policy": str(pol)}}
        for k, v in extra.items():
            data[k] = {**data.get(k, {}), **v}
        with pytest.raises(ConfigError, match="policy"):
            parse_config(data, tmp_path, {}, policy_uid=os.getuid())


def test_systemd_mode_needs_a_good_policy_and_a_matching_inventory(lab, tmp_path):
    pol = lab().bench.policy_path
    ok = {**BASE, "sandbox": {"mode": "systemd", "policy": str(pol)}}
    assert parse_config(ok, tmp_path, {}, policy_uid=os.getuid()).policy is not None
    with pytest.raises(ConfigError, match="owned by"):
        parse_config(ok, tmp_path, {}, policy_uid=os.getuid() + 1)
    with pytest.raises(ConfigError, match="cannot read"):
        parse_config({**BASE, "sandbox": {"mode": "systemd", "policy": str(tmp_path / "none.toml")}}, tmp_path, {}, policy_uid=os.getuid())
    with pytest.raises(ConfigError, match="simulated"):
        parse_config({**ok, "inventory": {"backend": "usb"}}, tmp_path, {}, policy_uid=os.getuid())
    with pytest.raises(ConfigError, match="unknown"):
        parse_config({**BASE, "sandbox": {"mode": "systemd", "policy": str(pol), "user": "root"}}, tmp_path, {}, policy_uid=os.getuid())
    with pytest.raises(ConfigError, match="sandbox.mode"):
        parse_config({**BASE, "sandbox": {"mode": "docker"}}, tmp_path, {})
    with pytest.raises(ConfigError):    # the default is the real sandbox, which needs the root-owned policy
        parse_config(BASE, tmp_path, {})
    assert parse_config({**BASE, "sandbox": {"mode": "none"}}, tmp_path, {}).policy is None


def test_the_service_has_no_root_helper_code_left():
    root = Path(__file__).resolve().parents[2]
    assert not (root / "oasa_lab" / "service" / "jobrun.py").exists()
    assert not list((root / "deploy" / "systemd").glob("oasa-lab-jobrun*"))
    for p in (root / "oasa_lab").rglob("*.py"):
        text = p.read_text()
        assert "SO_PEERCRED" not in text and "os.chown" not in text and "systemd-run" not in text, p


def test_the_owners_pause_file_comes_from_the_policy_and_stops_new_jobs(lab):
    svc = lab()
    assert svc.cfg.local_pause_file == str(svc.bench.root / "PAUSE")
    (svc.bench.root / "PAUSE").write_text("")
    with svc.client() as c:
        assert c.post("/api/jobs", json={"suite": "t-quick"}).status_code == 423
