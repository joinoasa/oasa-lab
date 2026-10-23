"""The Claude connector: MCP tool calls through a real MCP client over streamable HTTP."""

import asyncio
import json
import time

import httpx2 as httpx
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

TOOLS = {"list_suites", "run_suite", "job_status", "watch_job", "get_results", "stop_job", "lab_status",
         "pause_lab", "resume_lab"}


def run(coro):
    return asyncio.run(coro)


class Session:
    """One MCP client, signed in with a bearer token (what Claude holds after the OAuth sign-in)."""

    def __init__(self, svc, user="owner", mode="auto", token=None):
        self.svc, self.mode = svc, mode
        self.token = token or svc.tokens[user]

    async def __aenter__(self):
        http = httpx.AsyncClient(headers={"Authorization": f"Bearer {self.token}"}, timeout=60)
        self._cm = Client(streamable_http_client(f"{self.svc.url}/mcp", http_client=http), mode=self.mode)
        self.client = await self._cm.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._cm.__aexit__(*exc)

    async def call(self, name, **args):
        r = await self.client.call_tool(name, args)
        data = r.structured_content
        if data is None and r.content:
            try:
                data = json.loads(r.content[0].text)
            except ValueError:
                data = {"text": r.content[0].text}
        return r.is_error, data

    async def wait(self, job_id, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            err, st = await self.call("job_status", job_id=job_id)
            assert not err, st
            if st["status"] not in ("queued", "running"):
                return st
            await asyncio.sleep(0.3)
        raise AssertionError("job did not finish")


@pytest.mark.parametrize("mode", ["auto", "legacy"])  # legacy = the initialize handshake Claude.ai uses today
def test_tools_are_listed_and_annotated(svc, mode):
    async def go():
        async with Session(svc, mode=mode) as s:
            tools = {t.name: t for t in (await s.client.list_tools()).tools}
            assert set(tools) == TOOLS
            assert tools["list_suites"].annotations.read_only_hint is True
            assert tools["stop_job"].annotations.destructive_hint is True
            props = tools["run_suite"].input_schema["properties"]
            assert set(props) == {"suite", "seed", "profile"}  # nothing else can be passed in
            assert "principal" not in json.dumps(tools["run_suite"].input_schema)
    run(go())


def test_run_watch_and_get_results(svc):
    async def go():
        async with Session(svc, mode="legacy") as s:
            err, suites = await s.call("list_suites")
            assert not err and {x["name"] for x in suites["suites"]} >= {"l0-lab", "l1-channel", "t-quick"}
            tx = next(x for x in suites["suites"] if x["name"] == "t-tx")
            assert tx["runnable"] is False and "transmits" in tx["why_not"]
            err, job = await s.call("run_suite", suite="t-quick", seed=3)
            assert not err and job["seed"] == 3 and job["requested_by"] == "owner"
            st = await s.wait(job["id"])
            assert st["status"] == "passed", st
            err, w = await s.call("watch_job", job_id=job["id"], lines=50)
            events = [json.loads(x)["event"] for x in w["lines"]]
            assert events[0] == "run_start" and "test_end" in events and events[-1] == "run_end"
            err, w2 = await s.call("watch_job", job_id=job["id"], since=w["offset"])
            assert w2["lines"] == []
            err, res = await s.call("get_results", job_id=job["id"])
            assert res["counted"] is True and res["checker"]["ok"] is True and res["counts"]["passed"] == 1
            assert res["job"]["links"]["bundle"].endswith("/bundle.tar.gz")
            err, lab = await s.call("lab_status")
            assert {d["id"] for d in lab["devices"]} == {"pluto", "rtlsdr"} and lab["transmit_allowed"] is False
            err, over = await s.call("job_status")
            assert over["recent"] and over["paused"] is None
    run(go())


def test_stop_job_and_pause_through_the_connector(svc):
    async def go():
        async with Session(svc) as s:
            err, job = await s.call("run_suite", suite="t-slow")
            assert not err
            while (await s.call("job_status", job_id=job["id"]))[1]["status"] != "running":
                await asyncio.sleep(0.1)
            err, stopped = await s.call("stop_job", job_id=job["id"], reason="from chat")
            assert not err and stopped["status"] == "stopped"
            err, p = await s.call("pause_lab", reason="chat test")
            assert not err and p["by"] == "owner"
            err, msg = await s.call("run_suite", suite="t-quick")
            assert err and "paused" in json.dumps(msg)
            err, r = await s.call("resume_lab")
            assert not err and r["resumed"] is True
            err, job = await s.call("run_suite", suite="t-quick")
            assert not err
            assert (await s.wait(job["id"]))["status"] == "passed"
    run(go())


@pytest.mark.parametrize("bad", ["../../etc/passwd", "l0-lab; id", "L0-LAB", "_fixture", "nope", ""])
def test_the_connector_refuses_bad_suite_names(svc, bad):
    async def go():
        async with Session(svc) as s:
            err, msg = await s.call("run_suite", suite=bad)
            assert err
    run(go())


def test_bad_arguments_never_reach_the_queue(svc):
    async def go():
        async with Session(svc) as s:
            before = len(svc.lab.jobs.jobs)
            for args in ({"suite": "t-quick", "seed": -1}, {"suite": "t-quick", "seed": "x"},
                         {"suite": "t-quick", "profile": "nope"}, {}):
                err, _ = await s.call("run_suite", **args)
                assert err, args
            err, _ = await s.call("job_status", job_id="../../etc")
            assert err
            assert len(svc.lab.jobs.jobs) == before
            err, job = await s.call("run_suite", suite="t-quick", cmd="id")  # unknown arguments are dropped, never used
            assert not err and "cmd" not in json.dumps(job)
    run(go())


def test_roles_apply_to_tools(svc):
    async def go():
        async with Session(svc, "eve") as s:  # viewer
            assert not (await s.call("lab_status"))[0]
            assert not (await s.call("list_suites"))[0]
            for name, args in (("run_suite", {"suite": "t-quick"}), ("pause_lab", {}), ("resume_lab", {}),
                               ("stop_job", {"job_id": "0" * 12})):
                err, msg = await s.call(name, **args)
                assert err and "role" in json.dumps(msg), name
        async with Session(svc, "bob") as s:  # runner
            err, _ = await s.call("pause_lab")
            assert err
            err, job = await s.call("run_suite", suite="t-slow")
            assert not err
            assert not (await s.call("stop_job", job_id=job["id"]))[0]
    run(go())


def test_unauthenticated_clients_get_a_401_that_points_at_the_sign_in(svc):
    r = httpx.post(f"{svc.url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert 'resource_metadata="' in r.headers["www-authenticate"]
    prm = httpx.get(f"{svc.url}/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{svc.url}/mcp" and prm["authorization_servers"][0].rstrip("/") == svc.url
    bad = httpx.post(f"{svc.url}/mcp", json={}, headers={"Authorization": "Bearer nope", "Accept": "application/json"})
    assert bad.status_code == 401


def test_tool_calls_are_audited_without_secrets(svc):
    async def go():
        async with Session(svc, "bob") as s:
            await s.call("lab_status")
            await s.call("run_suite", suite="../x")
            err, job = await s.call("run_suite", suite="t-quick")
            await s.wait(job["id"])
            return job
    job = run(go())
    rows = svc.audit()
    tools = [r for r in rows if r["kind"] == "tool"]
    assert any(r["tool"] == "run_suite" and r["status"] == "ok" and r["actor"] == "bob" and r["job"] == job["id"] for r in tools)
    assert any(r["tool"] == "run_suite" and r["status"] == "refused" and r["args"]["suite"] == "../x" for r in tools)
    assert any(r["tool"] == "lab_status" and r["via"] == "api-token" for r in tools)
    assert any(r["kind"] == "http" and r["path"] == "/mcp" and r["actor"] == "bob" for r in rows)
    assert svc.tokens["bob"] not in svc.cfg.state_dir.joinpath("audit.jsonl").read_text()


def test_end_to_end_l1_channel_through_the_connector(svc):
    """The real thing: a Claude-style client runs the l1-channel suite and gets checked evidence."""
    async def go():
        async with Session(svc, mode="legacy") as s:
            err, job = await s.call("run_suite", suite="l1-channel")
            assert not err
            st = await s.wait(job["id"], timeout=180)
            assert st["status"] == "passed", st
            err, res = await s.call("get_results", job_id=job["id"])
            assert res["counted"] is True and res["counts"]["failed"] == 0 and res["counts"]["total"] >= 10
            assert [c["status"] for c in res["checker"]["checks"]] == ["pass"] * len(res["checker"]["checks"])
            return res
    res = run(go())
    with svc.client() as c:  # and the bundle really downloads and re-verifies
        assert c.get(f"/api/jobs/{res['job']['id']}/bundle.tar.gz").status_code == 200
