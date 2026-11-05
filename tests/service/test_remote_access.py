"""Remote access hardening (T-BENCH2 security addendum): token expiry and roles (RH1 a, b), reload without a
restart (RH1 c), only the pauser resumes (RM1 b), a per-user limit on active jobs (RM2 a)."""

import asyncio
import copy
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx2 as httpx
import pytest

from oasa_lab.service.auth import new_api_token
from oasa_lab.service.config import ConfigError, hash_token, lower_role, parse_config, parse_expires

from .conftest import TOMORROW, free_port, service_data, toml_dump
from .test_mcp import Session, run

USER = {"name": "owner", "role": "admin", "github_id": 1}
YESTERDAY = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))


def cfg(**kw):
    return parse_config({"users": [USER], "sandbox": {"mode": "none"}, **kw}, Path("/tmp"), {})


def row(name="t", user="owner", token="oasa_x", **kw):
    return {"name": name, "user": user, "sha256": hash_token(token), "expires": TOMORROW, **kw}


def token_row(svc, name, user, expires=TOMORROW, role=None):
    tok, h = new_api_token()
    r = {"name": name, "user": user, "sha256": h, "expires": expires}
    if role:
        r["role"] = role
    return tok, r


def with_tokens(svc, *rows):
    data = copy.deepcopy(svc.data)
    data["tokens"] = data["tokens"] + list(rows)
    return data


def wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


# --- RH1 a: every token expires -----------------------------------------------------------------------------------
def test_a_token_without_expiry_is_refused_at_startup_naming_it():
    with pytest.raises(ConfigError, match=r"without `expires`: old-laptop, ci\b.*oasa-lab token"):
        cfg(tokens=[{"name": "old-laptop", "user": "owner", "sha256": "0" * 64},
                    {"name": "ci", "user": "owner", "sha256": "1" * 64}])


@pytest.mark.parametrize("bad", ["soon", "", 12, "2026-13-01", True])
def test_a_bad_expiry_is_refused(bad):
    with pytest.raises(ConfigError, match="expires"):
        cfg(tokens=[row(expires=bad)])


def test_expiry_formats():
    import datetime as dt

    day = dt.datetime(2027, 1, 2, tzinfo=dt.timezone.utc).timestamp()
    assert parse_expires("2027-01-02") == parse_expires(dt.date(2027, 1, 2)) == day
    assert parse_expires("2027-01-02T00:00:00Z") == parse_expires(dt.datetime(2027, 1, 2)) == day
    assert parse_expires("2027-01-02T01:00:00+01:00") == day


def test_an_expired_token_matches_nothing():
    c = cfg(tokens=[row(token="old", expires=YESTERDAY), row("t2", token="new")])
    seen = []
    assert c.api_token_user("old", seen.append) is None and [t.name for t in seen] == ["t"]
    assert c.api_token_user("new")[1].name == "owner"


@pytest.mark.parametrize("args,err", [(["--days", "0"], "--days"), (["--days", "-3"], "--days"),
                                      (["--days", "91"], "--days"), (["--role", "root"], "--role")])
def test_token_cli_refuses_bad_lifetimes_and_roles(args, err):
    r = subprocess.run([sys.executable, "-c", "import sys; from oasa_lab.cli import main; sys.exit(main(sys.argv[1:]))",
                        "token", "agent", "--user", "owner", *args], capture_output=True, text=True)
    assert r.returncode != 0 and err in r.stderr and "oasa_" not in r.stdout


def test_token_cli_writes_an_entry_the_config_accepts():
    r = subprocess.run([sys.executable, "-c", "import sys; from oasa_lab.cli import main; sys.exit(main(sys.argv[1:]))",
                        "token", "agent-1", "--user", "owner", "--days", "7", "--role", "runner"],
                       capture_output=True, text=True, check=True)
    tok = r.stdout.split()[r.stdout.split().index("secret):") + 1]
    block = r.stdout[r.stdout.index("[[tokens]]"):]
    import tomllib

    entry = tomllib.loads(block)["tokens"][0]
    c = cfg(tokens=[entry])
    t, u = c.api_token_user(tok)
    assert t.role == "runner" and u.role == "runner" and 6.9 * 86400 < t.expires - time.time() <= 7 * 86400


def test_an_expired_token_is_refused_on_rest_and_mcp_and_audited(make_service):
    svc = make_service()
    old, old_row = token_row(svc, "old-script", "owner", expires=YESTERDAY)
    assert svc.reload(with_tokens(svc, old_row))
    unknown = httpx.get(f"{svc.url}/api/lab", headers={"Authorization": "Bearer oasa_never_issued"})
    r = httpx.get(f"{svc.url}/api/lab", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == unknown.status_code == 401 and r.json() == unknown.json()     # no oracle
    assert r.headers.get("www-authenticate") == unknown.headers.get("www-authenticate")
    mcp = {"Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    m_old = httpx.post(f"{svc.url}/mcp", json=body, headers={**mcp, "Authorization": f"Bearer {old}"})
    m_unk = httpx.post(f"{svc.url}/mcp", json=body, headers={**mcp, "Authorization": "Bearer oasa_never_issued"})
    assert m_old.status_code == m_unk.status_code == 401 and m_old.text == m_unk.text
    rows = svc.audit_where(lambda r: r.get("event") == "api_token_expired")
    assert len(rows) >= 2 and {r["credential"] for r in rows} == {"old-script"} and rows[0]["actor"] == "owner"
    assert old not in svc.lab.audit.path.read_text() and old_row["sha256"] not in svc.lab.audit.path.read_text()


# --- RH1 b: a token's role only lowers ----------------------------------------------------------------------------
def test_lower_role():
    assert lower_role("admin", "runner") == "runner"
    assert lower_role("viewer", "admin") == "viewer"    # never raises
    assert lower_role("runner", None) == "runner"


def test_a_token_role_above_the_user_does_not_raise_it():
    c = parse_config({"users": [USER, {"name": "v", "role": "viewer", "github_id": 2}], "sandbox": {"mode": "none"},
                      "tokens": [row(user="v", token="tv", role="admin")]}, Path("/tmp"), {})
    assert c.api_token_user("tv")[1].role == "viewer"
    with pytest.raises(ConfigError, match="role"):
        cfg(tokens=[row(role="root")])


def test_a_runner_token_of_an_admin_cannot_pause_on_either_surface(make_service):
    svc = make_service()
    agent, agent_row = token_row(svc, "owner-agent", "owner", role="runner")
    assert svc.reload(with_tokens(svc, agent_row))
    h = {"Authorization": f"Bearer {agent}"}
    with httpx.Client(base_url=svc.url, headers=h, timeout=30) as c:
        assert c.post("/api/pause").status_code == 403
        assert c.post("/api/resume").status_code == 403
        assert c.post("/api/jobs", json={"suite": "t-quick", "commit": "0" * 40}).status_code == 403  # pick_commit
        job = c.post("/api/jobs", json={"suite": "t-slow"}).json()                                 # run: yes
    bob = svc.submit("t-quick", user="bob")

    async def go():
        async with Session(svc, token=agent) as s:
            err, msg = await s.call("pause_lab")
            assert err and "role" in json.dumps(msg)
            err, msg = await s.call("resume_lab")
            assert err and "role" in json.dumps(msg)
            err, msg = await s.call("stop_job", job_id=bob["id"])       # someone else's job: stop_any is admin's
            assert err
            assert not (await s.call("stop_job", job_id=job["id"]))[0]  # its own: yes
    run(go())
    assert not svc.lab.jobs.paused
    assert any(r.get("tool") == "pause_lab" and r.get("status") == "forbidden" and r.get("credential") == "api-token:owner-agent"
               for r in svc.audit())


# --- RH1 c: reload users and tokens without a restart -------------------------------------------------------------
def test_a_bad_reload_keeps_the_old_config(make_service):
    svc = make_service()
    before = (list(svc.cfg.users), list(svc.cfg.tokens))
    assert svc.reload(text="this is [not toml") is False
    broken = copy.deepcopy(svc.data)
    broken["tokens"].append({"name": "no-expiry", "user": "owner", "sha256": "2" * 64})
    assert svc.reload(broken) is False
    assert (svc.cfg.users, svc.cfg.tokens) == before
    assert svc.client("owner").get("/api/lab").status_code == 200
    fails = svc.audit_where(lambda r: r.get("event") == "reload_failed")
    assert len(fails) == 2 and "no-expiry" in fails[1]["error"]


def test_reload_drops_a_removed_users_sessions_and_keeps_the_rest(make_service):
    svc = make_service()
    p = svc.lab.provider
    keep = p._issue("c1", "owner")
    gone = p._issue("c2", "eve")
    data = copy.deepcopy(svc.data)
    data["users"] = [u for u in data["users"] if u["name"] != "eve"]
    data["tokens"] = [t for t in data["tokens"] if t["user"] != "eve"]
    assert svc.reload(data)
    assert p.lookup(keep.access_token) and not p.lookup(gone.access_token)
    assert httpx.get(f"{svc.url}/api/lab", headers={"Authorization": f"Bearer {keep.access_token}"}).status_code == 200
    assert svc.client("eve").get("/api/lab").status_code == 401
    ev = svc.audit_where(lambda r: r.get("event") == "reload")[-1]
    assert ev["removed_users"] == ["eve"] and ev["oauth_revoked"] == 2


def _serve(config: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import sys; from oasa_lab.cli import main; sys.exit(main(sys.argv[1:]))",
                             "serve", "--config", str(config)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="needs SIGHUP")
def test_sighup_adds_and_removes_a_token_while_a_job_runs(lab_repo, tmp_path):
    port = free_port()
    data, tokens = service_data(lab_repo, tmp_path / "state", port)
    config = tmp_path / "service.toml"
    config.write_text(toml_dump(data))
    proc = _serve(config)
    url = f"http://127.0.0.1:{port}"
    old = {"Authorization": f"Bearer {tokens['owner']}"}
    try:
        assert wait_for(lambda: _ok(f"{url}/healthz"), 30), "service did not start"
        job = httpx.post(f"{url}/api/jobs", json={"suite": "t-slow"}, headers=old).json()
        assert wait_for(lambda: httpx.get(f"{url}/api/jobs/{job['id']}", headers=old).json()["status"] == "running", 30)
        new_tok, new_row = new_api_token()
        data["tokens"] = [t for t in data["tokens"] if t["user"] != "owner"] + [
            {"name": "owner-new", "user": "owner", "sha256": new_row, "expires": TOMORROW}]
        config.write_text(toml_dump(data))
        os.kill(proc.pid, signal.SIGHUP)
        new = {"Authorization": f"Bearer {new_tok}"}
        assert wait_for(lambda: httpx.get(f"{url}/api/lab", headers=new).status_code == 200), "new token not picked up"
        assert httpx.get(f"{url}/api/lab", headers=old).status_code == 401                  # revoked, no restart
        assert proc.poll() is None
        j = httpx.get(f"{url}/api/jobs/{job['id']}", headers=new).json()
        assert j["status"] == "running" and not j.get("stop_reason")                     # the run went on
        # a bad file on the next SIGHUP changes nothing
        config.write_text("[service\n")
        os.kill(proc.pid, signal.SIGHUP)
        time.sleep(0.5)
        assert proc.poll() is None and httpx.get(f"{url}/api/lab", headers=new).status_code == 200
        r = httpx.post(f"{url}/api/jobs/{job['id']}/stop", headers=new, json={"reason": "test over"})
        assert r.json()["status"] == "stopped" and "owner" in r.json()["stop_reason"]
        audit = [json.loads(x) for x in (tmp_path / "state" / "audit.jsonl").read_text().splitlines()]
        assert any(a.get("event") == "reload" and a.get("credentials") == ["bob-token", "eve-token", "owner-new"] for a in audit)
        assert any(a.get("event") == "reload_failed" for a in audit)
        assert not any(a.get("event") == "start" for a in audit[1:])                      # one start: no restart
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()


def _ok(u):
    try:
        return httpx.get(u, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


# --- RM1 b: only the pauser resumes; an admin needs force, which is audited ---------------------------------------
def test_resume_belongs_to_whoever_paused(make_service):
    svc = make_service(extra={"users": [
        {"name": "owner", "role": "admin", "github_id": 1001, "email": "owner@example.invalid"},
        {"name": "bob", "role": "runner", "github_id": 1002},
        {"name": "eve", "role": "admin", "github_id": 1003}]})   # a second admin
    with svc.client("owner") as c:
        assert c.post("/api/pause", json={"reason": "changing cables"}).status_code == 200
    with svc.client("eve") as c:
        r = c.post("/api/resume")
        assert r.status_code == 403 and "paused by owner" in r.json()["error"]
        assert c.post("/api/resume", json={"force": "yes"}).status_code == 400
        assert svc.lab.jobs.paused

    async def mcp_try():
        async with Session(svc, "eve") as s:
            err, msg = await s.call("resume_lab")
            assert err and "paused by owner" in json.dumps(msg)
    run(mcp_try())
    assert svc.lab.jobs.paused and not svc.audit_where(lambda r: r.get("event") == "resume_forced", 0.2)
    with svc.client("owner") as c:            # the one who paused needs no force
        assert c.post("/api/resume").json() == {"resumed": True}
        assert c.post("/api/pause").status_code == 200

    with svc.client("eve") as c:
        out = c.post("/api/resume", json={"force": True}).json()
        assert out == {"resumed": True, "overrode": "owner"}
    forced = svc.audit_where(lambda r: r.get("event") == "resume_forced")
    assert forced[-1]["actor"] == "eve" and forced[-1]["paused_by"] == "owner"
    http = svc.audit_where(lambda r: r.get("kind") == "http" and r.get("action") == "resume" and r.get("force") is True)
    assert http and http[-1]["overrode"] == "owner"

    async def mcp_force():
        async with Session(svc, "owner") as s:
            await s.call("pause_lab", reason="again")
        async with Session(svc, "eve") as s:
            err, r = await s.call("resume_lab", force=True)
            assert not err and r["resumed"] is True and r["overrode"] == "owner"
    run(mcp_force())
    assert not svc.lab.jobs.paused
    assert any(r.get("tool") == "resume_lab" and r.get("args") == {"force": True} and r.get("actor") == "eve"
               for r in svc.audit())


def test_force_does_not_clear_the_owners_local_pause(make_service, tmp_path):
    local = tmp_path / "owner-pause"
    svc = make_service(extra={"service": {"local_pause_file": str(local)}})
    local.write_text("on the bench")
    with svc.client() as c:
        out = c.post("/api/resume", json={"force": True}).json()
        assert out["resumed"] is False and out["still_paused"]["local"] is True
    assert local.exists()


# --- RM2 a: a per-user limit on active jobs -----------------------------------------------------------------------
def test_one_user_cannot_fill_the_queue(make_service):
    svc = make_service(extra={"service": {"max_active_per_user": 2, "max_queued": 3}})
    a = svc.submit("t-slow", user="bob")
    svc.submit("t-slow", user="bob")
    with svc.client("bob") as c:
        r = c.post("/api/jobs", json={"suite": "t-quick"})
        assert r.status_code == 429 and "per-user limit is 2" in r.json()["error"]
    svc.submit("t-quick", user="owner")                     # another user still gets in
    with svc.client("owner") as c:
        r = c.post("/api/jobs", json={"suite": "t-quick"})
        assert r.status_code == 429 and "lab-wide limit is 3" in r.json()["error"]
        c.post(f"/api/jobs/{a['id']}/stop")
    assert svc.wait(a["id"], want=("stopped", "cancelled"))


def test_the_per_user_limit_has_a_safe_default_and_checks():
    assert cfg().max_active_per_user == 3
    with pytest.raises(ConfigError, match="max_active_per_user"):
        cfg(service={"max_active_per_user": 0})
