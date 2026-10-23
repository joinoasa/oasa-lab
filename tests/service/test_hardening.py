"""Findings from the security review: files a job planted, pause files, limits, commits, git settings."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx2 as httpx
import pytest

from oasa_lab.service import files
from oasa_lab.service.checkout import Checkout, CheckoutError, Checkouts, _git, git_env, read_suites

ROOT = Path(__file__).resolve().parents[2]
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def run(coro):
    return asyncio.run(coro)


# --- files a job planted (M1) ----------------------------------------------------------------------------
def test_open_inside_refuses_symlinks_in_every_position(tmp_path):
    root = tmp_path / "runs"
    (root / "b").mkdir(parents=True)
    (root / "b" / "log.jsonl").write_text("ok\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    assert files.read_inside(root, "b", "log.jsonl") == b"ok\n"
    (root / "b" / "leak").symlink_to(secret)                 # a file link
    (root / "linked").symlink_to(tmp_path)                   # a folder link
    (root / "b" / "up").symlink_to("..")
    for parts in (("b", "leak"), ("linked", "secret.txt"), ("b", "up", "secret.txt"), ("b", "..", "..", "secret.txt"),
                  ("", ), ("b/log.jsonl",), ("b", "nope")):
        with pytest.raises(files.UnsafePath):
            files.open_inside(root, *parts)
    os.mkfifo(root / "b" / "pipe")
    with pytest.raises(files.UnsafePath):                    # a pipe would block the service forever
        files.open_inside(root, "b", "pipe")
    assert files.list_dirs(root) == ["b"]                    # the linked folder is not listed


def test_copy_tree_takes_plain_files_only_and_obeys_the_limits(tmp_path):
    root = tmp_path / "runs"
    (root / "b" / "sub").mkdir(parents=True)
    (root / "b" / "a.txt").write_text("a")
    (root / "b" / "sub" / "c.txt").write_text("c")
    (root / "b" / "leak").symlink_to(tmp_path / "x")
    os.mkfifo(root / "b" / "pipe")
    out = tmp_path / "copy"
    stats = files.copy_tree(root, "b", out, 1000)
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == ["a.txt", "sub/c.txt"]
    assert sorted(stats["skipped"]) == ["leak", "pipe"] and stats["files"] == 2
    (root / "b" / "big").write_bytes(b"x" * 5000)
    with pytest.raises(files.TooBig):
        files.copy_tree(root, "b", tmp_path / "copy2", 1000)
    assert not (tmp_path / "copy2").exists()                 # nothing half-copied is left
    with pytest.raises(files.TooBig):
        files.copy_tree(root, "b", tmp_path / "copy3", 10**9, max_files=2)
    with pytest.raises(files.UnsafePath):
        files.copy_tree(root, "../runs", tmp_path / "copy4", 1000)


def _finished_job(svc):
    done = svc.wait(svc.submit("t-quick")["id"])
    assert done["status"] == "passed"
    return done


def test_a_forged_verdict_a_planted_link_and_a_swapped_result_do_not_count(make_service, tmp_path):
    """What a job can leave behind is not trusted: the service copies it out and runs its own checker."""
    svc = make_service()
    good = _finished_job(svc)
    jobs = svc.lab.jobs
    job = jobs.get(good["id"])
    # a second 'job' whose folder looks like a hostile suite wrote it
    fake = type(job)(**{**job.to_dict(), "id": "abcdef012345", "status": "running", "bundle": None, "check_ok": None})
    jobs.jobs[fake.id] = fake
    jd = jobs.job_dir(fake)
    shutil.copytree(jobs.bundle_dir(job), jd / "runs" / job.bundle)
    shutil.copy(jobs.bundle_dir(job).parent / (job.bundle + ".check.json"), jd / "runs" / (job.bundle + ".check.json"))
    bundle = jd / "runs" / job.bundle
    ctrf = bundle / "results.ctrf.json"
    ctrf.write_text(ctrf.read_text().replace('"passed": 1', '"passed": 9'))          # swapped result
    (bundle / "leak.txt").symlink_to("/etc/hostname")                                 # planted link
    forged = jd / "runs" / (job.bundle + ".check.json")
    forged.write_text(json.dumps({"ok": True, "checker": {"name": "oasa-lab-checker", "version": "forged"}, "checks": []}))
    suites = Path(jobs.checkouts.dirs) / job.commit / "suites"
    problem = run(jobs._collect(fake, jd, suites.parent))
    assert problem is None and fake.check_ok is False, "the service's own checker must fail the swapped evidence"
    assert fake.checker != "forged"
    assert not (jobs.bundle_dir(fake) / "leak.txt").exists()
    assert (jobs.bundle_dir(fake) / "results.ctrf.json").read_text() == ctrf.read_text()


def test_a_bundle_folder_that_is_a_link_is_not_a_bundle(make_service, tmp_path):
    svc = make_service()
    good = _finished_job(svc)
    jobs = svc.lab.jobs
    job = jobs.get(good["id"])
    fake = type(job)(**{**job.to_dict(), "id": "abcdef012346", "status": "running", "bundle": None, "check_ok": None})
    jobs.jobs[fake.id] = fake
    jd = jobs.job_dir(fake)
    (jd / "runs").mkdir(parents=True)
    (jd / "runs" / "20260101T000000Z-t-quick-deadbeef").symlink_to(jobs.bundle_dir(job))
    assert run(jobs._collect(fake, jd, jd)) is None and fake.bundle is None


def test_oversize_evidence_is_rejected(make_service):
    svc = make_service(extra={"service": {"max_bundle_mb": 1}})
    good = _finished_job(svc)
    jobs = svc.lab.jobs
    job = jobs.get(good["id"])
    fake = type(job)(**{**job.to_dict(), "id": "abcdef012347", "status": "running", "bundle": None, "check_ok": None})
    jobs.jobs[fake.id] = fake
    jd = jobs.job_dir(fake)
    shutil.copytree(jobs.bundle_dir(job), jd / "runs" / job.bundle)
    (jd / "runs" / job.bundle / "huge.bin").write_bytes(b"\0" * 2_000_000)
    assert "evidence rejected" in run(jobs._collect(fake, jd, jd))


def test_results_name_the_checker_that_judged_them(make_service):
    svc = make_service()
    done = _finished_job(svc)
    assert done["checker"]
    with svc.client() as c:
        res = c.get(f"/api/jobs/{done['id']}/results").json()
    assert res["checker"]["version"] == done["checker"] and f"checker {done['checker']}" in res["judged_by"]


# --- one suite parser, from the checkout (M3) ------------------------------------------------------------
def _checkout_with(tmp_path, suites: dict[str, str], parser_edit=None) -> Checkout:
    co = tmp_path / "co"
    (co / "oasa_lab").mkdir(parents=True)
    src = (ROOT / "oasa_lab" / "suites.py").read_text()
    (co / "oasa_lab" / "suites.py").write_text(parser_edit(src) if parser_edit else src)
    for name, toml in suites.items():
        (co / "suites" / name).mkdir(parents=True)
        (co / "suites" / name / "suite.toml").write_text(toml)
    return Checkout("a" * 40, co)


def test_suites_come_from_the_checkouts_own_parser(tmp_path, monkeypatch):
    from oasa_lab.service import checkout as ck

    monkeypatch.setattr(ck, "_SUITES", {})
    co = _checkout_with(tmp_path, {"l2-x": 'description = "x"\ntransmits = true\nrequires = ["pluto", "serial-radio:2"]\nwiring = "w"\n'},
                        parser_edit=lambda s: s.replace('"wiring": s.wiring', '"wiring": "from-the-checkout"'))
    found = run(read_suites(co))
    assert found["l2-x"] == {"name": "l2-x", "description": "x", "transmits": True, "transmits_declared": True,
                             "wiring": "from-the-checkout", "requires": ["pluto", "serial-radio:2"]}


def test_suite_list_edge_cases(tmp_path, monkeypatch):
    from oasa_lab.service import checkout as ck

    monkeypatch.setattr(ck, "_SUITES", {})
    co = _checkout_with(tmp_path, {
        "ok": 'description = "fine"\n',
        "broken": "this is = not [toml",
        "stringy": 'description = "s"\ntransmits = "yes"\n',
        "_hidden": 'description = "h"\n',
        "bad-req": 'description = "b"\nrequires = ["Pluto; rm -rf /"]\n',
    })
    (co.path / "suites" / "linked").symlink_to(co.path / "suites" / "ok")
    found = run(read_suites(co))
    assert set(found) == {"ok", "stringy"}
    # the runner's reading of a non-boolean is "yes, it transmits" (the cautious one); it is not a declaration
    assert found["stringy"]["transmits"] is True and found["stringy"]["transmits_declared"] is False


def test_a_suite_list_that_hangs_is_cut_off(tmp_path, monkeypatch):
    from oasa_lab.service import checkout as ck

    monkeypatch.setattr(ck, "_SUITES", {})
    co = _checkout_with(tmp_path, {}, parser_edit=lambda s: s.replace("from __future__ import annotations", "from __future__ import annotations\nimport time\ntime.sleep(60)", 1))
    with pytest.raises(CheckoutError, match="timed out"):
        run(read_suites(co, timeout=1))


def test_the_service_does_not_keep_a_second_suite_parser():
    src = (ROOT / "oasa_lab" / "service" / "checkout.py").read_text()
    assert "tomllib" not in src


# --- git settings (M1) and a fresh copy per job (L2) --------------------------------------------------------
def test_git_ignores_a_planted_config_and_hooks(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[core]\n  fsmonitor = /tmp/evil\n[alias]\n  rev-parse = !echo pwned\n")
    monkeypatch.setenv("HOME", str(home))
    env = git_env()
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_CONFIG_NOSYSTEM"] == "1" and env["HOME"] != str(home)
    out = run(_git("config", "--list", "--show-origin"))
    assert ".gitconfig" not in out and "evil" not in out


def _repo(tmp_path, n=3):
    repo = tmp_path / "up"
    repo.mkdir()
    subprocess.run([*GIT, "init", "-q", "-b", "main", str(repo)], check=True)
    shas = []
    for i in range(n):
        (repo / "f.txt").write_text(str(i))
        subprocess.run([*GIT, "-C", str(repo), "add", "-A"], check=True)
        subprocess.run([*GIT, "-C", str(repo), "commit", "-q", "-m", f"c{i}"], check=True)
        shas.append(subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip())
    return repo, shas


def test_each_job_gets_a_fresh_copy_with_no_hooks(tmp_path):
    repo, shas = _repo(tmp_path)
    cks = Checkouts(str(repo), "main", tmp_path / "git")
    run(cks.latest())
    a = run(cks.export(shas[2], tmp_path / "a"))
    (a / "leftover").write_text("x")
    (a / "f.txt").write_text("changed")
    b = run(cks.export(shas[2], tmp_path / "a"))              # same place, again
    assert not (b / "leftover").exists() and (b / "f.txt").read_text() == "2"
    assert not (b / ".git" / "hooks").exists()
    old = os.umask(0o077)                                     # a strict service umask must not hide the copy from the job user
    try:
        c = run(cks.export(shas[2], tmp_path / "c"))
    finally:
        os.umask(old)
    assert (c / "f.txt").stat().st_mode & 0o004 and (c.stat().st_mode & 0o005) == 0o005
    assert (run(cks.export(shas[0], tmp_path / "old")) / "f.txt").read_text() == "0"
    with pytest.raises(CheckoutError):
        run(cks.export("not-a-sha", tmp_path / "bad"))


def test_two_jobs_of_the_same_commit_do_not_share_files(make_service):
    svc = make_service()
    a = svc.wait(svc.submit("t-dirty")["id"])
    b = svc.wait(svc.submit("t-dirty")["id"])
    assert a["status"] == "passed" and b["status"] == "passed", (a, b)
    assert not list((svc.cfg.state_dir / "jobs").glob("*/src")), "the job's copy is removed when it ends"


# --- commits below the floor (L3) ----------------------------------------------------------------------------
def test_a_commit_before_the_floor_is_refused(tmp_path):
    repo, shas = _repo(tmp_path)
    cks = Checkouts(str(repo), "main", tmp_path / "git", floor=shas[1])
    with pytest.raises(CheckoutError, match="older than the lowest"):
        run(cks.at(shas[0]))
    assert run(cks.at(shas[1])).sha == shas[1] and run(cks.at(shas[2])).sha == shas[2]
    assert run(cks.at(None)).sha == shas[2]
    # a floor that main does not contain stops everything rather than silently allowing it
    other = Checkouts(str(repo), "main", tmp_path / "git2", floor="f" * 40)
    with pytest.raises(CheckoutError):
        run(other.latest())


def test_the_rest_api_refuses_a_commit_below_the_floor(make_service, lab_repo):
    head = subprocess.run(["git", "-C", str(lab_repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    svc = make_service(extra={"service": {"min_commit": head}})
    with svc.client() as c:
        assert c.post("/api/jobs", json={"suite": "t-quick", "commit": head}).status_code == 202
        assert c.post("/api/jobs", json={"suite": "t-quick", "commit": "0" * 40}).status_code == 400


# --- pause files (L5) -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["empty", "text", "dangling_link", "folder", "unreadable"])
def test_any_pause_file_pauses_the_lab(make_service, kind):
    svc = make_service()
    pause = svc.cfg.state_dir / "PAUSE"
    if kind == "empty":
        pause.write_text("")
    elif kind == "text":
        pause.write_text("maintenance")
    elif kind == "dangling_link":
        pause.symlink_to(svc.cfg.state_dir / "nowhere")
    elif kind == "folder":
        pause.mkdir()
    else:
        pause.write_text("x")
        pause.chmod(0)
    with svc.client() as c:
        r = c.post("/api/jobs", json={"suite": "t-quick"})
        assert r.status_code == 423, kind
        assert c.get("/api/lab").json()["paused"] is not None
        assert c.post("/api/resume").json()["resumed"] is True      # the API can still clear its own kind
        assert c.post("/api/jobs", json={"suite": "t-quick"}).status_code == 202


def test_a_local_pause_file_cannot_be_cleared_through_the_api(make_service, tmp_path):
    local = tmp_path / "owner-pause"
    svc = make_service(extra={"service": {"local_pause_file": str(local)}})
    local.write_text("rigging the antenna")
    with svc.client() as c:
        assert c.post("/api/jobs", json={"suite": "t-quick"}).status_code == 423
        out = c.post("/api/resume").json()
        assert out["resumed"] is False and out["still_paused"]["local"] is True
        assert c.post("/api/jobs", json={"suite": "t-quick"}).status_code == 423
        assert c.post("/api/pause", json={"reason": "also"}).status_code == 200
        assert c.post("/api/resume").json()["resumed"] is True        # clears only the API's file
        assert c.post("/api/jobs", json={"suite": "t-quick"}).status_code == 423
    assert local.exists()
    local.unlink()                                                      # only the owner, from a shell
    assert svc.submit("t-quick")["status"] in ("queued", "running")


def test_pause_via_mcp_resume_also_keeps_the_local_pause(make_service, tmp_path):
    local = tmp_path / "owner-pause2"
    svc = make_service(extra={"service": {"local_pause_file": str(local)}})
    local.write_text("x")
    assert svc.lab.jobs.resume() is False and svc.lab.jobs.paused["local"] is True


# --- disk, retention, read caps (L6) ----------------------------------------------------------------------------
def test_a_job_does_not_start_without_free_disk(make_service):
    svc = make_service(extra={"service": {"min_free_mb": 10**9}})
    with svc.client() as c:
        r = c.post("/api/jobs", json={"suite": "t-quick"})
    assert r.status_code == 507 and "free disk" in r.json()["error"]


def test_old_evidence_is_deleted_beyond_the_retention_setting(make_service):
    svc = make_service(extra={"service": {"keep_jobs": 2}})
    ids = [svc.wait(svc.submit("t-quick")["id"])["id"] for _ in range(4)]
    deadline = time.time() + 10
    while time.time() < deadline and len(svc.lab.jobs.jobs) > 2:
        time.sleep(0.1)
    left = set(svc.lab.jobs.jobs)
    assert left == set(ids[2:]), (left, ids)
    for gone in ids[:2]:
        assert not (svc.cfg.state_dir / "jobs" / gone).exists()
    with svc.client() as c:
        assert c.get(f"/api/jobs/{ids[0]}").status_code == 404
        assert c.get(f"/api/jobs/{ids[3]}").status_code == 200


def test_log_reads_are_capped_however_much_is_asked(make_service):
    svc = make_service()
    done = svc.wait(svc.submit("t-flood")["id"])
    jobs = svc.lab.jobs
    job = jobs.get(done["id"])
    size = (svc.cfg.state_dir / "jobs" / job.id / "stdout.log").stat().st_size
    assert size > 2_000_000
    lines, off = jobs.read_log(job, 0, "stdout", max_bytes=50_000_000)
    assert off <= 1_000_000 and sum(len(x) + 1 for x in lines) <= 1_000_000
    assert 0 < jobs.log_end(job, "stdout") <= size
    assert len(jobs.tail_log(job, 10_000, "stdout")) <= 500


def test_a_long_stream_ends_with_a_marker_the_client_can_resume_from(make_service, monkeypatch):
    monkeypatch.setattr("oasa_lab.service.api.MAX_STREAM_BYTES", 5_000)
    svc = make_service()
    done = svc.wait(svc.submit("t-flood")["id"])
    with svc.client() as c:
        body = c.get(f"/api/jobs/{done['id']}/log?stream=stdout&follow=1").text
    assert "event: end" in body and '"truncated": true' in body
    assert len(body) < 500_000


def test_watch_job_asks_for_the_end_of_the_log_cheaply(make_service):
    svc = make_service()
    done = svc.wait(svc.submit("t-flood")["id"])
    from .test_mcp import Session

    async def go():
        async with Session(svc, mode="legacy") as s:
            err, w = await s.call("watch_job", job_id=done["id"], lines=2)
            assert not err and len(w["lines"]) == 2 and w["offset"] > 0
    run(go())
