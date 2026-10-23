"""The REST API against a live service on localhost, running real jobs from a throw-away lab repo."""

import io
import json
import tarfile
import threading
import time

import httpx2 as httpx
import pytest

from .conftest import start, stop


def test_health_is_open_and_says_nothing(svc):
    r = httpx.get(f"{svc.url}/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


# --- auth -------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/api/suites", "/api/jobs", "/api/lab", "/api/bundles", "/api/jobs/aaaaaaaaaaaa"])
def test_no_credentials_is_rejected(svc, path):
    r = httpx.get(svc.url + path)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_wrong_and_malformed_credentials_are_rejected(svc):
    for h in ({"Authorization": "Bearer nope"}, {"Authorization": "Basic Zm9vOmJhcg=="}, {"Authorization": "Bearer"},
              {"Authorization": svc.tokens["owner"]}, {"X-Api-Key": svc.tokens["owner"]},
              {"Cf-Access-Authenticated-User-Email": "owner@example.invalid"}):
        assert httpx.get(f"{svc.url}/api/lab", headers=h).status_code == 401, h


def test_post_without_credentials_is_rejected(svc):
    assert httpx.post(f"{svc.url}/api/jobs", json={"suite": "t-quick"}).status_code == 401
    assert httpx.post(f"{svc.url}/api/pause").status_code == 401


def test_roles(svc):
    with svc.client("eve") as c:  # viewer
        assert c.get("/api/lab").status_code == 200
        assert c.post("/api/jobs", json={"suite": "t-quick"}).status_code == 403
        assert c.post("/api/pause").status_code == 403
    with svc.client("bob") as c:  # runner
        assert c.post("/api/pause").status_code == 403
        assert c.post("/api/resume").status_code == 403


def test_a_removed_user_loses_access_at_once(svc):
    users = svc.cfg.users
    svc.cfg.users = [u for u in users if u.name != "eve"]
    try:
        assert svc.client("eve").get("/api/lab").status_code == 401
    finally:
        svc.cfg.users = users


def test_unknown_host_header_is_refused(svc):
    r = httpx.get(f"{svc.url}/healthz", headers={"Host": "evil.example.com"})
    assert r.status_code == 421


# --- submit and results --------------------------------------------------------------------------
def test_submit_runs_and_leaves_checked_evidence(svc):
    job = svc.submit("t-quick", seed=7)
    assert job["status"] in ("queued", "running", "passed") and job["seed"] == 7
    done = svc.wait(job["id"])
    assert done["status"] == "passed" and done["check_ok"] is True and done["counts"]["passed"] == 1
    head = svc.lab.checkouts and __import__("subprocess").run(
        ["git", "-C", str(svc.cfg.repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert done["commit"] == head  # the run used the commit it says it used
    with svc.client() as c:
        res = c.get(f"/api/jobs/{job['id']}/results").json()
        assert res["counted"] is True and res["checker"]["ok"] is True
        assert [x["status"] for x in res["checker"]["checks"]] == ["pass"] * 6
        assert "t-quick" in res["summary_md"]
        chk = c.get(f"/api/jobs/{job['id']}/check.json").json()
        assert chk["ok"] is True
        r = c.get(f"/api/jobs/{job['id']}/bundle.tar.gz")
        assert r.status_code == 200
    names = tarfile.open(fileobj=io.BytesIO(r.content)).getnames()
    bundle = done["bundle"]
    assert f"{bundle}/manifest.json" in names and f"{bundle}/SHA256SUMS" in names and f"{bundle}.check.json" in names
    man = json.loads(tarfile.open(fileobj=io.BytesIO(r.content)).extractfile(f"{bundle}/manifest.json").read())
    assert man["job"]["id"] == job["id"] and man["job"]["requested_by"] == "owner"
    assert man["lab"]["git_sha"] == head and man["lab"]["git_dirty"] is False and man["seed"] == 7


def test_bundles_are_listed(svc):
    job = svc.submit("t-quick")
    svc.wait(job["id"])
    with svc.client() as c:
        rows = c.get("/api/bundles").json()["bundles"]
    assert any(b["job"] == job["id"] and b["check_ok"] is True for b in rows)


def test_job_env_has_no_service_secrets(svc):
    done = svc.wait(svc.submit("t-env")["id"])
    assert done["status"] == "passed", done


def test_a_real_suite_end_to_end(svc):
    done = svc.wait(svc.submit("l0-lab")["id"], timeout=150)
    assert done["status"] == "passed" and done["check_ok"] is True


# --- validation: only a suite from the list, nothing else ---------------------------------------
@pytest.mark.parametrize("suite", [
    "../../etc/passwd", "..", ".", "/bin/sh", "l0-lab; id", "l0-lab\nid", "$(id)", "`id`", "l0-lab && id", "L0-LAB",
    "l0_lab", "", " l0-lab", "l0-lab ", "_fixture", "t-none", "suites/l0-lab", "l0-lab/../l1-channel", "a" * 500,
    "l0-lab\x00", "-k", "--help", 5, None, ["l0-lab"], {"x": 1}, True,
])
def test_bad_suite_names_are_refused(svc, suite):
    with svc.client() as c:
        r = c.post("/api/jobs", json={"suite": suite})
    assert r.status_code == 400, (suite, r.text)
    assert not [j for j in svc.lab.jobs.jobs.values() if j.suite == suite]


@pytest.mark.parametrize("seed", [-1, 2**31, 1.5, "7", True, None, [1], "--x"])
def test_bad_seeds_are_refused(svc, seed):
    with svc.client() as c:
        assert c.post("/api/jobs", json={"suite": "t-quick", "seed": seed}).status_code == 400


@pytest.mark.parametrize("extra", [{"out": "/tmp/x"}, {"cmd": "id"}, {"args": ["-x"]}, {"env": {"A": "b"}}, {"path": "/"}])
def test_unknown_fields_are_refused(svc, extra):
    with svc.client() as c:
        assert c.post("/api/jobs", json={"suite": "t-quick", **extra}).status_code == 400


@pytest.mark.parametrize("commit", ["HEAD", "main", "abc123", "0" * 40, "--upload-pack=id", "A" * 40, 5])
def test_commit_must_be_a_full_id_on_main(svc, commit):
    with svc.client() as c:
        assert c.post("/api/jobs", json={"suite": "t-quick", "commit": commit}).status_code == 400


def test_commit_pinning_works_for_a_commit_on_main(svc):
    head = svc.wait(svc.submit("t-quick")["id"])["commit"]
    job = svc.submit("t-quick", commit=head)
    assert svc.wait(job["id"])["commit"] == head


def test_bad_json_and_big_bodies(svc):
    with svc.client() as c:
        assert c.post("/api/jobs", content=b"{nope", headers={"Content-Type": "application/json"}).status_code == 400
        assert c.post("/api/jobs", json=["t-quick"]).status_code == 400
        assert c.post("/api/jobs", json={"suite": "t-quick", "pad": "x" * 10000}).status_code == 413


@pytest.mark.parametrize("job_id", ["../../etc", "zzzzzzzzzzzz", "A" * 12, "0" * 12, "1" * 40, "..%2f..", "%00"])
def test_job_ids_are_validated(svc, job_id):
    with svc.client() as c:
        for p in ("", "/log", "/results", "/check.json", "/bundle.tar.gz"):
            assert c.get(f"/api/jobs/{job_id}{p}").status_code == 404
        assert c.post(f"/api/jobs/{job_id}/stop").status_code == 404


# --- queue, live log, stop -----------------------------------------------------------------------
def test_queue_runs_one_job_at_a_time_in_order(svc):
    a = svc.submit("t-slow")
    b = svc.submit("t-quick")
    c_ = svc.submit("t-quick", user="bob")
    with svc.client() as c:
        assert c.get(f"/api/jobs/{b['id']}").json()["queue_position"] in (1, 2)
        end = time.time() + 20
        while c.get(f"/api/jobs/{a['id']}").json()["status"] != "running" and time.time() < end:
            time.sleep(0.1)
        jb, jc = c.get(f"/api/jobs/{b['id']}").json(), c.get(f"/api/jobs/{c_['id']}").json()
        assert (jb["status"], jb["queue_position"]) == ("queued", 1)
        assert (jc["status"], jc["queue_position"]) == ("queued", 2)
        lab = c.get("/api/lab").json()
        assert lab["running"] == a["id"] and lab["queued"] == [b["id"], c_["id"]]
        c.post(f"/api/jobs/{a['id']}/stop")
    done = [svc.wait(x["id"]) for x in (a, b, c_)]
    assert [d["status"] for d in done] == ["stopped", "passed", "passed"]
    assert done[0]["finished"] <= done[1]["started"] <= done[1]["finished"] <= done[2]["started"]  # never two at once


def test_live_log_streams_while_the_job_runs_and_ends_with_it(svc):
    job = svc.submit("t-slow")
    seen, ended = [], {}

    def reader():
        with httpx.Client(base_url=svc.url, headers=svc.headers(), timeout=60) as c:
            with c.stream("GET", f"/api/jobs/{job['id']}/log", params={"follow": "1"}) as r:
                assert r.headers["content-type"].startswith("text/event-stream")
                event = None
                for line in r.iter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        if event == "end":
                            ended.update(json.loads(line[5:]))
                            return
                        seen.append(json.loads(line[5:]))

    t = threading.Thread(target=reader)
    t.start()
    end = time.time() + 30
    while not any(e.get("event") == "test_start" for e in seen) and time.time() < end:
        time.sleep(0.1)
    assert any(e["event"] == "run_start" for e in seen) and any(e["event"] == "test_start" for e in seen)
    assert svc.lab.jobs.get(job["id"]).status == "running"  # we saw lines before the job was over
    with svc.client() as c:
        assert c.post(f"/api/jobs/{job['id']}/stop").status_code == 200
    t.join(30)
    assert not t.is_alive() and ended.get("status") == "stopped"


def test_log_without_follow_gives_lines_and_an_offset(svc):
    job = svc.submit("t-quick")
    svc.wait(job["id"])
    with svc.client() as c:
        first = c.get(f"/api/jobs/{job['id']}/log").json()
        assert first["lines"] and json.loads(first["lines"][0])["event"] == "run_start"
        again = c.get(f"/api/jobs/{job['id']}/log", params={"offset": first["offset"]}).json()
        assert again["lines"] == []
        assert c.get(f"/api/jobs/{job['id']}/log", params={"stream": "../x"}).status_code == 400
        assert "passed" in "\n".join(c.get(f"/api/jobs/{job['id']}/log", params={"stream": "stdout"}).json()["lines"])


def test_stop_a_running_job_with_sigterm(svc):
    job = svc.submit("t-slow")
    while svc.lab.jobs.get(job["id"]).status != "running":
        time.sleep(0.05)
    time.sleep(1)
    t0 = time.time()
    with svc.client() as c:
        r = c.post(f"/api/jobs/{job['id']}/stop", json={"reason": "test"})
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    assert time.time() - t0 < 8
    assert "stopped by owner" in r.json()["stop_reason"]
    assert svc.lab.jobs.running is None
    assert svc.audit()[-1]["kind"] in ("http", "job")


def test_stop_escalates_to_sigkill(svc):
    job = svc.submit("t-stubborn")
    while svc.lab.jobs.get(job["id"]).status != "running":
        time.sleep(0.05)
    time.sleep(1.5)  # let pytest start the test, so SIGTERM is ignored
    t0 = time.time()
    with svc.client() as c:
        assert c.post(f"/api/jobs/{job['id']}/stop").json()["status"] == "stopped"
    assert time.time() - t0 < 10
    done = svc.wait(job["id"])
    assert done["exit_code"] in (-9, -15, 137)
    nxt = svc.wait(svc.submit("t-quick")["id"])  # the lab is free again
    assert nxt["status"] == "passed"


def test_stop_a_queued_job_and_the_rules_for_stopping(svc):
    a = svc.submit("t-slow")
    b = svc.submit("t-quick", user="bob")
    with svc.client("eve") as c:
        assert c.post(f"/api/jobs/{b['id']}/stop").status_code == 403
    with svc.client("bob") as c:
        assert c.post(f"/api/jobs/{a['id']}/stop").status_code == 403  # a runner stops only their own
        assert c.post(f"/api/jobs/{b['id']}/stop").json()["status"] == "cancelled"
        assert c.post(f"/api/jobs/{b['id']}/stop").status_code == 409
    with svc.client() as c:  # an admin stops anyone's
        assert c.post(f"/api/jobs/{a['id']}/stop").json()["status"] == "stopped"


def test_job_timeout_stops_the_run(make_service):
    s = make_service(extra={"service": {"job_timeout_seconds": 3}})
    done = s.wait(s.submit("t-slow")["id"])
    assert done["status"] == "stopped" and "timed out" in done["stop_reason"]


# --- pause: the global kill switch -----------------------------------------------------------------
def test_pause_refuses_new_jobs_cancels_the_queue_and_survives_a_restart(make_service, lab_repo, tmp_path):
    s = make_service()
    running = s.submit("t-slow")
    queued = s.submit("t-quick")
    while s.lab.jobs.get(running["id"]).status != "running":
        time.sleep(0.05)
    with s.client() as c:
        r = c.post("/api/pause", json={"reason": "testing", "stop_running": True})
        assert r.status_code == 200 and r.json()["cancelled"] == [queued["id"]] and r.json()["stopped"] == running["id"]
        r = c.post("/api/jobs", json={"suite": "t-quick"})
        assert r.status_code == 423 and "testing" in r.json()["error"]
        assert c.get("/api/lab").json()["paused"]["by"] == "owner"
    assert s.lab.jobs.get(running["id"]).status == "stopped" and s.lab.jobs.get(queued["id"]).status == "cancelled"
    state = s.cfg.state_dir
    stop(s)
    s2 = start(lab_repo, state)  # same state dir, new process-like start
    try:
        assert s2.lab.jobs.paused["by"] == "owner"  # still paused after the restart
        with httpx.Client(base_url=s2.url, headers=s2.headers()) as c:
            r = c.post("/api/jobs", json={"suite": "t-quick"})
            assert r.status_code == 423
    finally:
        stop(s2)


def test_pause_by_file_from_a_shell_and_resume(svc):
    (svc.cfg.state_dir / "PAUSE").write_text("maintenance")
    try:
        with svc.client() as c:
            r = c.post("/api/jobs", json={"suite": "t-quick"})
            assert r.status_code == 423 and "maintenance" in r.json()["error"]
    finally:
        with svc.client() as c:
            assert c.post("/api/resume").json() == {"resumed": True}
    assert svc.wait(svc.submit("t-quick")["id"])["status"] == "passed"


# --- the transmit interlock --------------------------------------------------------------------------
@pytest.mark.parametrize("rf", ["unset", "open"])
def test_transmitting_suites_are_refused_unless_cabled_or_shielded(make_service, rf):
    s = make_service(extra={"lab": {"rf_setup": rf}})
    with s.client() as c:
        r = c.post("/api/jobs", json={"suite": "t-tx"})
        assert r.status_code == 403 and "transmits" in r.json()["error"]
        suites = {x["name"]: x for x in c.get("/api/suites").json()["suites"]}
        assert suites["t-tx"]["transmits"] is True
        assert c.get("/api/lab").json()["transmit_allowed"] is False
    assert s.wait(s.submit("t-quick")["id"])["status"] == "passed"  # others are fine


@pytest.mark.parametrize("rf", ["cabled", "shielded"])
def test_transmitting_suites_run_when_cabled_or_shielded(make_service, rf):
    s = make_service(extra={"lab": {"rf_setup": rf}})
    done = s.wait(s.submit("t-tx")["id"])
    assert done["status"] == "passed" and done["transmits"] is True
    with s.client() as c:
        b = c.get(f"/api/jobs/{done['id']}/bundle.tar.gz").content
    man = json.loads(tarfile.open(fileobj=io.BytesIO(b)).extractfile(f"{done['bundle']}/manifest.json").read())
    assert man["job"]["rf_setup"] == rf  # the evidence says how the radios were connected


# --- devices ------------------------------------------------------------------------------------------
def test_missing_device_blocks_a_profile_or_a_suite(make_service):
    s = make_service(present={"pluto": False})
    with s.client() as c:
        lab = c.get("/api/lab").json()
        assert {d["id"]: d["present"] for d in lab["devices"]} == {"pluto": False, "rtlsdr": True}
        assert lab["profiles"]["bench"]["ready"] is False
        r = c.post("/api/jobs", json={"suite": "t-quick", "profile": "bench"})
        assert r.status_code == 409 and "pluto" in r.json()["error"]
        assert c.post("/api/jobs", json={"suite": "t-needs"}).status_code == 409
        assert c.post("/api/jobs", json={"suite": "t-quick", "profile": "../bench"}).status_code == 400
    s.inv.overrides["pluto"] = True  # plugged in
    assert s.wait(s.submit("t-needs")["id"])["status"] == "passed"
    done = s.wait(s.submit("t-quick", profile="bench")["id"])
    assert done["status"] == "passed" and done["profile"] == "bench"


def test_device_that_vanishes_while_queued_fails_the_job_safely(make_service):
    s = make_service()
    a = s.submit("t-slow")
    b = s.submit("t-quick", profile="bench")
    s.inv.overrides["pluto"] = False  # unplugged while waiting
    s.lab.jobs  # noqa: B018
    with s.client() as c:
        c.post(f"/api/jobs/{a['id']}/stop")
    done = s.wait(b["id"])
    assert done["status"] == "error" and "pluto" in done["stop_reason"]


# --- audit ----------------------------------------------------------------------------------------------
def test_audit_log_records_who_what_when_and_never_a_secret(svc):
    httpx.get(f"{svc.url}/api/lab")  # no credentials
    httpx.get(f"{svc.url}/api/lab", headers={"Authorization": "Bearer oasa_wrong_guess_1234"})
    job = svc.submit("t-quick", user="bob")
    svc.wait(job["id"])
    with svc.client("eve") as c:
        c.post("/api/jobs", json={"suite": "t-quick"})  # forbidden
    rows = svc.audit()
    sub = [r for r in rows if r.get("action") == "submit" and r.get("job") == job["id"]]
    assert sub and sub[0]["actor"] == "bob" and sub[0]["via"] == "api-token" and sub[0]["status"] == 202
    assert sub[0]["suite"] == "t-quick" and sub[0]["method"] == "POST" and sub[0]["ts"].endswith("Z")
    rejected = [r for r in rows if r.get("status") == 401 and r["path"] == "/api/lab"]
    assert len(rejected) >= 2 and all(r["actor"] is None for r in rejected)
    assert any(r.get("status") == 403 and r["actor"] == "eve" for r in rows)
    assert any(r["kind"] == "job" and r["job"] == job["id"] and r["status"] == "passed" for r in rows)
    raw = svc.cfg.state_dir.joinpath("audit.jsonl").read_text()
    for secret in [*svc.tokens.values(), "oasa_wrong_guess_1234", "gh-secret"]:
        assert secret not in raw
    for line in raw.splitlines():
        json.loads(line)  # every line is JSON


# --- restart ---------------------------------------------------------------------------------------------
def test_jobs_running_when_the_service_stopped_are_marked_interrupted(make_service, lab_repo):
    s = make_service()
    a = s.submit("t-slow")
    b = s.submit("t-quick")
    while s.lab.jobs.get(a["id"]).status != "running":
        time.sleep(0.05)
    state = s.cfg.state_dir
    stop(s)  # shutdown kills the run
    s2 = start(lab_repo, state)
    try:
        ja, jb = s2.lab.jobs.get(a["id"]), s2.lab.jobs.get(b["id"])
        assert ja.status in ("stopped", "interrupted") and jb.status == "cancelled"
    finally:
        stop(s2)
