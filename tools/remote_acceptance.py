#!/usr/bin/env python3
"""T-BENCH2 acceptance: use the remote lab the way an agent does, and check what it proves.

    OASA_LAB_TOKEN=... python3 tools/remote_acceptance.py --url https://lab-mcp.example.com \
        --api-url https://lab-api.example.com --out runs/remote-acceptance

Through the Claude connector (MCP over HTTPS, bearer token), it:

  1. reads lab_status and list_suites;
  2. asks for a transmitting suite whose wiring the owner has not confirmed (default l2-pluto-acceptance)
     and expects a refusal (on real hardware: the missing [confirm_wiring] entry);
  3. runs a simulated-channel suite (default l1-channel) and a hardware suite (default l2-rx-noise on the
     bench-rx profile), follows each with watch_job, and requires get_results to say "counted": true;
  4. pauses the lab, expects run_suite to be refused, and resumes it (admin token);
  5. with OASA_LAB_RUNNER_TOKEN set, checks that a runner may not pause the lab.

Through the REST API it downloads each counted run's check.json and bundle, re-runs the checker on the
bundle here (on real hardware with --require-hardware), and writes a results page per run with
tools/result_summary.py. It prints a Markdown summary and saves it as OUT/summary.md. Exit 0 only if
every check passed.

Tokens come from the environment, never the command line. If the API hostname is behind Cloudflare
Access, set CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET (an Access service token). Needs the
`service` extra (the MCP client); everything else is the standard library.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FINISHED = ("passed", "failed", "stopped", "error", "cancelled", "interrupted")
PAUSE_REASON = "remote acceptance: checking that a paused lab refuses runs"


@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    refusals: list[tuple[str, str]] = field(default_factory=list)
    lab: dict = field(default_factory=dict)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, bool(ok), detail))
        print(f"  [{'ok' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}", file=sys.stderr, flush=True)
        return bool(ok)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)


def cell(x) -> str:
    return str(x).replace("|", "\\|").replace("\n", " ")


class Connector:
    """One MCP session with a bearer token, as Claude.ai holds after its sign-in."""

    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/") + "/mcp", token

    async def __aenter__(self):
        import httpx2 as httpx
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        http = httpx.AsyncClient(headers={"Authorization": f"Bearer {self.token}"}, timeout=60)
        self._cm = Client(streamable_http_client(self.url, http_client=http), mode="legacy")
        self.client = await self._cm.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._cm.__aexit__(*exc)

    async def call(self, name: str, **args) -> tuple[bool, dict]:
        """(is_error, data). An error's data is {"text": message}."""
        r = await self.client.call_tool(name, args)
        data = r.structured_content
        if data is None or r.is_error:
            text = r.content[0].text if r.content else ""
            text = text.split(": ", 1)[1] if text.startswith("Error executing tool ") else text
            try:
                data = json.loads(text)
            except ValueError:
                data = {"text": text}
        return bool(r.is_error), data


def http_get(url: str, token: str, dest: Path | None = None) -> bytes:
    """GET from the REST API with the bearer token, plus a Cloudflare Access service token if one is set."""
    h = {"Authorization": f"Bearer {token}", "User-Agent": "oasa-lab-remote-acceptance"}
    if os.environ.get("CF_ACCESS_CLIENT_ID"):
        h["CF-Access-Client-Id"] = os.environ["CF_ACCESS_CLIENT_ID"]
        h["CF-Access-Client-Secret"] = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=300) as r:
        data = r.read()
    if dest is not None:
        dest.write_bytes(data)
    return data


async def wait_job(c: Connector, job_id: str, timeout: float) -> tuple[dict, int]:
    """Follow the job with watch_job until it ends. Returns (last status, log lines seen)."""
    end, since, seen, last = time.monotonic() + timeout, None, 0, ""
    while time.monotonic() < end:
        err, w = await c.call("watch_job", job_id=job_id, lines=200, **({"since": since} if since is not None else {}))
        if err:
            raise RuntimeError(f"watch_job failed: {w}")
        seen += len(w.get("lines", []))
        since = w.get("offset", since)
        if w["status"] != last:
            print(f"    {job_id[:12]} {w['status']} ({seen} log lines)", file=sys.stderr, flush=True)
            last = w["status"]
        if w["status"] in FINISHED:
            err, st = await c.call("job_status", job_id=job_id)
            return st, seen
        await asyncio.sleep(2)
    raise TimeoutError(f"job {job_id} did not finish in {timeout:.0f} s")


def local_recheck(bundle_tar: Path, out: Path, commit: str, require_hardware: bool) -> tuple[bool, str]:
    """Unpack the downloaded bundle and run the checker on it here: the caller's own verdict."""
    sys.path.insert(0, str(ROOT))
    from oasa_lab.checker import check_bundle

    dest = out / bundle_tar.name.removesuffix(".tar.gz")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle_tar, "r:gz") as tf:
        tf.extractall(dest, filter="data")  # no links out, no absolute paths
    dirs = [p for p in dest.iterdir() if p.is_dir()]
    if len(dirs) != 1:
        return False, f"expected one bundle folder in the download, found {len(dirs)}"
    # The suite files must be the ones that ran: take them from the run's commit, if this clone has it.
    suites: Path | None = out / f"suites-{commit[:12]}"
    if not suites.is_dir():
        git = ["git", "-C", str(ROOT)]
        if subprocess.run([*git, "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True, check=False).returncode == 0:
            arc = subprocess.run([*git, "archive", "--format=tar", commit, "suites"], capture_output=True, check=False)
            if arc.returncode == 0:
                import io
                with tarfile.open(fileobj=io.BytesIO(arc.stdout)) as tf:
                    tf.extractall(suites.parent / f"tree-{commit[:12]}", filter="data")
                (suites.parent / f"tree-{commit[:12]}" / "suites").rename(suites)
    suites = suites if suites.is_dir() else None
    res = check_bundle(dirs[0], suites, require_hardware=require_hardware)
    failed = [c["name"] for c in res["checks"] if c["status"] != "pass"]
    how = (f"with the suite files at {commit[:7]}" if suites else "without the suite file (this clone lacks the commit: git fetch)")
    how += ", hardware required" if require_hardware else ""
    return res["ok"], (f"all {len(res['checks'])} checks pass ({how})" if res["ok"] else f"failed: {', '.join(failed)} ({how})")


async def counted_run(c: Connector, rep: Report, a, suite: str, profile: str | None, real: bool, out: Path) -> None:
    args = {"suite": suite, "seed": a.seed, **({"profile": profile} if profile else {})}
    err, job = await c.call("run_suite", **args)
    if not rep.add(f"run_suite {suite} accepted", not err, job.get("text", "") if err else f"job {job['id']}"):
        return
    try:
        st, lines = await wait_job(c, job["id"], a.timeout)
    except (RuntimeError, TimeoutError) as exc:
        rep.add(f"{suite} finished", False, str(exc))
        return
    rep.add(f"{suite} watched live", lines > 0, f"{lines} log lines through watch_job")
    err, res = await c.call("get_results", job_id=job["id"])
    chk = (res.get("checker") or {}) if not err else {}
    counted = not err and res.get("counted") is True
    rep.add(f"{suite} counted", counted,
            f"status {st['status']}, checker {chk.get('version')} {'PASS' if chk.get('ok') else 'FAIL'}, counts {res.get('counts')}")
    run = {"suite": suite, "job": job["id"], "commit": st.get("commit"), "status": st.get("status"),
           "via": st.get("via"), "requested_by": st.get("requested_by"), "profile": profile, "counted": counted,
           "checker": chk.get("version"), "counts": res.get("counts", {}), "started": st.get("started"),
           "finished": st.get("finished")}
    rep.runs.append(run)
    links = (res.get("job") or {}).get("links", {})
    if not counted or "bundle" not in links:
        return
    base = a.api_url.rstrip("/")

    def rel(u: str) -> str:
        return u if u.startswith("http") else base + u
    try:
        check = json.loads(http_get(rel(links["check"]), a.token))
        rep.add(f"{suite} check.json downloaded", check.get("ok") is True, "service verdict PASS" if check.get("ok") else "service verdict not ok")
        tar = out / f"{suite}-{job['id'][:12]}.tar.gz"
        http_get(rel(links["bundle"]), a.token, tar)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        rep.add(f"{suite} downloads (REST API)", False, f"{type(exc).__name__}: {exc}")
        return
    hw_needed = real and profile is not None
    ok, why = local_recheck(tar, out / "bundles", st.get("commit", ""), hw_needed)
    rep.add(f"{suite} re-checked here", ok, why)
    page = out / f"{suite}-{job['id'][:12]}.md"
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "result_summary.py"), str(tar)], capture_output=True, text=True, check=False)
    if p.returncode == 0:
        page.write_text(p.stdout)
        run["page"] = page.name
    else:
        rep.add(f"{suite} results page", False, p.stderr.strip()[:200])


async def main_async(a) -> Report:
    rep = Report()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    async with Connector(a.url, a.token) as c:
        err, lab = await c.call("lab_status")
        if not rep.add("lab_status", not err, "" if not err else lab.get("text", "")):
            return rep
        real = lab.get("inventory_backend") != "simulated"
        rep.lab = {"inventory": lab.get("inventory_backend"), "rf_setup": lab.get("rf_setup"), "version": lab.get("version")}
        if not rep.add("lab not paused at start", lab.get("paused") is None, json.dumps(lab.get("paused"))):
            return rep

        err, suites = await c.call("list_suites")
        by_name = {s["name"]: s for s in suites.get("suites", [])} if not err else {}
        rep.lab["commit"] = suites.get("commit")
        for s in (a.channel_suite, a.hw_suite, a.tx_suite):
            rep.add(f"list_suites has {s}", s in by_name)
        tx = by_name.get(a.tx_suite, {})
        rep.add(f"{a.tx_suite} transmits and is shown as not runnable", tx.get("transmits") is True and tx.get("runnable") is False,
                cell(tx.get("why_not")))

        # Known-bad 1: a transmitting suite without the owner's wiring confirmation.
        err, msg = await c.call("run_suite", suite=a.tx_suite, seed=a.seed)
        text = msg.get("text", json.dumps(msg))
        want = "confirm_wiring" if real else "refused"
        rep.add(f"run_suite {a.tx_suite} refused (transmit lock)", err and "refused" in text and want in text, text[:300])
        rep.refusals.append((f"run_suite {a.tx_suite}", text))

        # The counted runs.
        await counted_run(c, rep, a, a.channel_suite, None, real, out)
        await counted_run(c, rep, a, a.hw_suite, a.hw_profile, real, out)

        # Roles: a runner may not pause.
        if a.runner_token:
            async with Connector(a.url, a.runner_token) as r:
                err, msg = await r.call("pause_lab", reason="remote acceptance: a runner must not pause")
                text = msg.get("text", json.dumps(msg))
                rep.add("runner may not pause_lab", err and "role" in text, text[:200])
                rep.refusals.append(("pause_lab as runner", text))

        # Known-bad 2: a paused lab refuses runs. Resume only what we paused.
        err, p = await c.call("pause_lab", reason=PAUSE_REASON)
        paused_here = rep.add("pause_lab (admin)", not err, p.get("text", "") if err else f"by {p.get('by')}")
        if paused_here:
            try:
                err, msg = await c.call("run_suite", suite=a.channel_suite, seed=a.seed)
                text = msg.get("text", json.dumps(msg))
                rep.add(f"run_suite {a.channel_suite} refused while paused", err and "paused" in text, text[:300])
                rep.refusals.append((f"run_suite {a.channel_suite} (paused)", text))
            finally:
                err, r = await c.call("resume_lab")
                rep.add("resume_lab", not err and r.get("resumed") is True, json.dumps(r)[:200])
                err, lab = await c.call("lab_status")
                rep.add("lab running again", not err and lab.get("paused") is None, json.dumps(lab.get("paused")))
    return rep


def render(rep: Report, a) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = [f"# Remote acceptance (T-BENCH2), {now}", "",
         f"- **Result:** {'PASS' if rep.ok else 'FAIL'} ({sum(ok for _, ok, _ in rep.checks)} of {len(rep.checks)} checks)",
         f"- **Lab:** inventory `{rep.lab.get('inventory')}`, rf_setup `{rep.lab.get('rf_setup')}`, "
         f"oasa-lab {rep.lab.get('version')} at `{rep.lab.get('commit')}`",
         "- **How:** every job submitted, watched and judged through the Claude connector (MCP, bearer token); "
         "check.json and bundles downloaded from the REST API and re-checked here", ""]
    if rep.runs:
        L += ["## Runs", "", "| Suite | Job | Commit | Status | Tests | Checker | Counted | By |", "|---|---|---|---|---|---|---|---|"]
        for r in rep.runs:
            c = r["counts"] or {}
            tests = f"{c.get('passed', 0)} passed, {c.get('failed', 0)} failed, {c.get('skipped', 0)} skipped"
            job = f"[{r['job'][:12]}]({r['page']})" if r.get("page") else r["job"][:12]
            L.append(f"| `{r['suite']}`{' (' + r['profile'] + ')' if r['profile'] else ''} | {job} | `{(r['commit'] or '')[:7]}` | "
                     f"{r['status']} | {tests} | {r['checker']} | {'yes' if r['counted'] else 'no'} | {r['requested_by']} via {r['via']} |")
        L.append("")
    if rep.refusals:
        L += ["## Refusals (known-bad)", "", "| Call | What the service said |", "|---|---|"]
        L += [f"| {cell(k)} | {cell(v[:300])} |" for k, v in rep.refusals]
        L.append("")
    L += ["## Checks", "", "| Check | Result | Detail |", "|---|---|---|"]
    L += [f"| {cell(n)} | {'pass' if ok else '**FAIL**'} | {cell(d)} |" for n, ok, d in rep.checks]
    L += ["", "Generated by `tools/remote_acceptance.py`. Job ids link to each run's page from `tools/result_summary.py`."]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Environment: OASA_LAB_TOKEN (admin, required), OASA_LAB_RUNNER_TOKEN (optional), "
                                       "CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET (optional)")
    p.add_argument("--url", required=True, help="the connector's base URL (…/mcp is added)")
    p.add_argument("--api-url", help="the REST API's base URL for downloads (default: --url)")
    p.add_argument("--channel-suite", default="l1-channel")
    p.add_argument("--hw-suite", default="l2-rx-noise")
    p.add_argument("--hw-profile", default="bench-rx")
    p.add_argument("--tx-suite", default="l2-pluto-acceptance",
                   help="a transmitting suite whose wiring the policy does NOT confirm")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--timeout", type=float, default=1800, help="seconds to wait for each run")
    p.add_argument("--out", default=f"runs/remote-acceptance-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    a = p.parse_args(argv)
    a.api_url = a.api_url or a.url
    a.token = os.environ.get("OASA_LAB_TOKEN", "")
    a.runner_token = os.environ.get("OASA_LAB_RUNNER_TOKEN", "")
    if not a.token:
        p.error("set OASA_LAB_TOKEN to an admin's API token")
    try:
        rep = asyncio.run(main_async(a))
    except ImportError as exc:
        print(f"remote_acceptance: needs the MCP client: pip install -e '.[service]' ({exc})", file=sys.stderr)
        return 2
    text = render(rep, a)
    Path(a.out, "summary.md").write_text(text)
    print(text)
    print(f"saved: {Path(a.out) / 'summary.md'}", file=sys.stderr)
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
