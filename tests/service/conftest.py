"""Fixtures for the job service tests: a throw-away lab repo, a live service on localhost, test users."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx2 as httpx
import pytest

pytest.importorskip("mcp")
import uvicorn  # noqa: E402

from oasa_lab.service.app import Lab  # noqa: E402
from oasa_lab.service.auth import new_api_token  # noqa: E402
from oasa_lab.service.config import Config, parse_config  # noqa: E402
from oasa_lab.service.inventory import SimulatedInventory  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid"]

QUICK = '''
def test_quick(lab):
    assert 1 + 1 == 2
'''
SLOW = '''
import time
def test_slow():
    for _ in range(600):
        time.sleep(0.1)
'''
STUBBORN = '''
import signal, time
def test_stubborn():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for _ in range(600):
        time.sleep(0.1)
'''
ENV = '''
import os
def test_env():
    assert "OASA_LAB_GITHUB_CLIENT_SECRET" not in os.environ
    assert "SECRET_FOR_TEST" not in os.environ
    assert os.environ["OASA_LAB_JOB_ID"]
    assert os.environ["HOME"].endswith(os.environ["OASA_LAB_JOB_ID"])
'''
SUITES = {
    "t-quick": ('description = "passes fast"\n', QUICK),
    "t-slow": ('description = "runs until stopped"\n', SLOW),
    "t-stubborn": ('description = "ignores SIGTERM"\n', STUBBORN),
    "t-env": ('description = "checks the job environment"\n', ENV),
    "t-tx": ('description = "pretends to transmit"\ntransmits = true\n', QUICK),
    "t-needs": ('description = "needs a pluto"\nrequires = ["pluto"]\n', QUICK),
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="session")
def lab_repo(tmp_path_factory) -> Path:
    """A git repo with branch main: the real oasa_lab code and suites, plus tiny test suites (t-*)."""
    repo = tmp_path_factory.mktemp("lab-repo")
    shutil.copytree(ROOT / "oasa_lab", repo / "oasa_lab", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "pyproject.toml", repo / "pyproject.toml")
    shutil.copy(ROOT / "README.md", repo / "README.md")
    shutil.copytree(ROOT / "suites", repo / "suites", ignore=shutil.ignore_patterns("__pycache__"))
    for name, (toml, body) in SUITES.items():
        d = repo / "suites" / name
        d.mkdir(parents=True)
        (d / "suite.toml").write_text(toml + "\n[captures]\nencrypted = false\n")
        (d / f"test_{name.replace('-', '_')}.py").write_text(body)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "test lab")
    return repo


@dataclass
class Running:
    lab: Lab
    cfg: Config
    url: str
    tokens: dict[str, str]  # user name -> API token
    server: uvicorn.Server
    thread: threading.Thread
    inv: SimulatedInventory

    def headers(self, user: str = "owner") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[user]}"}

    def client(self, user: str | None = "owner") -> httpx.Client:
        return httpx.Client(base_url=self.url, headers=self.headers(user) if user else {}, timeout=30)

    def wait(self, job_id: str, want=("passed", "failed", "stopped", "error", "cancelled"), timeout: float = 90) -> dict:
        end = time.time() + timeout
        with self.client() as c:
            while time.time() < end:
                j = c.get(f"/api/jobs/{job_id}").json()
                if j["status"] in want:
                    return j
                time.sleep(0.2)
        raise AssertionError(f"job {job_id} did not reach {want}")

    def submit(self, suite: str, user: str = "owner", **extra) -> dict:
        with self.client(user) as c:
            r = c.post("/api/jobs", json={"suite": suite, **extra})
            assert r.status_code == 202, r.text
            return r.json()

    def audit(self) -> list[dict]:
        return self.lab.audit.read()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(repo: Path, state: Path, *, extra: dict | None = None, env: dict | None = None,
          present: dict | None = None, http=None) -> Running:
    port = free_port()
    tokens, token_rows = {}, []
    users = [{"name": "owner", "role": "admin", "github_id": 1001, "github_login": "owner-gh", "email": "owner@example.invalid"},
             {"name": "bob", "role": "runner", "github_id": 1002},
             {"name": "eve", "role": "viewer", "github_id": 1003}]
    for u in users:
        t, h = new_api_token()
        tokens[u["name"]] = t
        token_rows.append({"name": f"{u['name']}-token", "user": u["name"], "sha256": h})
    data = {
        "service": {"port": port, "public_url": f"http://127.0.0.1:{port}", "state_dir": str(state), "repo": str(repo),
                    "stop_grace_seconds": 1, "job_timeout_seconds": 120},
        "lab": {"rf_setup": "unset"},
        "inventory": {"backend": "simulated", "devices": [
            {"id": "pluto", "kind": "pluto"}, {"id": "rtlsdr", "kind": "rtl-sdr"}]},
        "profiles": {"bench": {"requires": ["pluto"], "description": "pluto on the bench"}},
        "users": users, "tokens": token_rows,
        "github": {"client_id": "gh-client"},
    }
    for k, v in (extra or {}).items():
        data.setdefault(k, {}).update(v) if isinstance(v, dict) else data.__setitem__(k, v)
    cfg = parse_config(data, state.parent, {"OASA_LAB_GITHUB_CLIENT_SECRET": "gh-secret", **(env or {})})
    inv = SimulatedInventory(cfg.devices, present or {})
    lab = Lab(cfg, inventory=inv, http=http)
    server = uvicorn.Server(uvicorn.Config(lab.app(), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "service did not start"
    return Running(lab, cfg, f"http://127.0.0.1:{port}", tokens, server, th, inv)


def stop(r: Running) -> None:
    r.server.should_exit = True
    r.thread.join(15)


@pytest.fixture(scope="module")
def svc(lab_repo, tmp_path_factory):
    os.environ["SECRET_FOR_TEST"] = "must-not-reach-jobs"
    r = start(lab_repo, tmp_path_factory.mktemp("state") / "s")
    yield r
    stop(r)
    os.environ.pop("SECRET_FOR_TEST", None)


@pytest.fixture
def make_service(lab_repo, tmp_path):
    made = []

    def make(**kw) -> Running:
        r = start(lab_repo, tmp_path / f"s{len(made)}", **kw)
        made.append(r)
        return r

    yield make
    for r in made:
        stop(r)
