"""Fixtures for the job service tests: a throw-away lab repo, a live service on localhost, test users."""

from __future__ import annotations

import concurrent.futures
import json
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
TOMORROW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 86400))
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid"]

QUICK = '''
def test_quick(lab):
    assert 1 + 1 == 2
'''
TX = '''
def test_tx(hw):
    hw.use_wiring()  # records the cable plan; refuses on real hardware unless --confirm-wiring names it
    for dev in ("heltec_a", "heltec_b", "rtl0"):
        hw.open(dev)  # the checker wants every device on the cable plan in the manifest
'''
SIM_BENCH = '''[lab]
backend = "sim"
[devices.heltec_a]
kind = "serial-radio"
port = "sim:a"
[devices.heltec_b]
kind = "serial-radio"
port = "sim:b"
[devices.rtl0]
kind = "rtlsdr"
[wiring.lora]
rf_setup = "cabled"
paths = [
  { name = "a-to-b", from = "heltec_a", to = "heltec_b", loss_db = 72.0 },
  { name = "b-to-a", from = "heltec_b", to = "heltec_a", loss_db = 72.0 },
  { name = "a-to-rtl", from = "heltec_a", to = "rtl0", loss_db = 66.0, receiver_transmits = false },
]
'''
NEEDS = '''
def test_needs(hw):
    hw.open(hw.inv.ids_of("pluto")[0])  # the checker wants the required device in the manifest
'''
DIRTY = '''
import os
def test_dirty():
    assert not os.path.exists("leftover.txt"), "a file from an earlier job is still here"
    open("leftover.txt", "w").write("x")
'''
FLOOD = '''
def test_flood(capsys):
    with capsys.disabled():  # past pytest's capture, onto the job's stdout
        for i in range(30000):
            print("x" * 100)
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
    # in the process-group sandbox HOME is the job folder; under job-run it is a private folder in the job's own temp dir
    assert os.environ["HOME"].endswith(os.environ["OASA_LAB_JOB_ID"]) or "/job-" in os.environ["HOME"]
    assert not any(k.startswith("FAKE_") for k in os.environ)
'''
SUITES = {
    "t-quick": ('description = "passes fast"\n', QUICK),
    "t-slow": ('description = "runs until stopped"\n', SLOW),
    "t-stubborn": ('description = "ignores SIGTERM"\n', STUBBORN),
    "t-env": ('description = "checks the job environment"\n', ENV),
    "t-tx": ('description = "pretends to transmit"\ntransmits = true\nwiring = "lora"\n', TX),
    "t-dirty": ('description = "leaves a file behind"\n', DIRTY),
    "t-flood": ('description = "writes 3 MB to stdout"\n', FLOOD),
    "t-needs2": ('description = "needs two plutos"\nrequires = ["pluto:2"]\n', QUICK),
    "t-needs": ('description = "needs a pluto"\nrequires = ["pluto"]\n', NEEDS),
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
    shutil.copy(ROOT / "lab.example.toml", repo / "lab.example.toml")  # l0-lab reads it
    shutil.copy(ROOT / "calibration.example.toml", repo / "calibration.example.toml")  # and this
    shutil.copytree(ROOT / "tools", repo / "tools", ignore=shutil.ignore_patterns("__pycache__"))  # l0-lab tests them
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
    data: dict | None = None        # the config as written to config_path
    config_path: Path | None = None

    def reload(self, data: dict | None = None, text: str | None = None) -> bool:
        """Write a new config file and reload it on the service's own event loop, as SIGHUP does."""
        self.config_path.write_text(text if text is not None else toml_dump(data))
        done: concurrent.futures.Future = concurrent.futures.Future()
        loop = self.lab.jobs._worker.get_loop()
        loop.call_soon_threadsafe(lambda: done.set_result(self.lab.reload()))
        return done.result(10)

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

    def audit_where(self, pred, timeout: float = 5.0) -> list[dict]:
        """Audit rows matching `pred`. The line for a request is written just after its response goes out,
        so wait for it instead of reading at once."""
        end = time.time() + timeout
        while True:
            rows = [r for r in self.audit() if pred(r)]
            if rows or time.time() > end:
                return rows
            time.sleep(0.05)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)   # a JSON string is a valid TOML basic string for what the tests write
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(type(v))


def toml_dump(data: dict) -> str:
    """Just enough TOML for the service config: top-level arrays first, then one table per section."""
    out = [f"{k} = {toml_value(v)}" for k, v in data.items() if not isinstance(v, dict)]
    for k, v in data.items():
        if isinstance(v, dict):
            out.append(f"\n[{k}]")
            out += [f"{json.dumps(kk)} = {toml_value(vv)}" for kk, vv in v.items()]
    return "\n".join(out) + "\n"


def service_data(repo: Path, state: Path, port: int, extra: dict | None = None) -> tuple[dict, dict[str, str]]:
    """The config of a test service (as parsed TOML) and its users' API tokens."""
    tokens, token_rows = {}, []
    users = [{"name": "owner", "role": "admin", "github_id": 1001, "github_login": "owner-gh", "email": "owner@example.invalid"},
             {"name": "bob", "role": "runner", "github_id": 1002},
             {"name": "eve", "role": "viewer", "github_id": 1003}]
    for u in users:
        t, h = new_api_token()
        tokens[u["name"]] = t
        token_rows.append({"name": f"{u['name']}-token", "user": u["name"], "sha256": h, "expires": TOMORROW})
    data = {
        "service": {"port": port, "public_url": f"http://127.0.0.1:{port}", "state_dir": str(state), "repo": str(repo),
                    "stop_grace_seconds": 1, "job_timeout_seconds": 300, "min_free_mb": 100},
        "sandbox": {"mode": "none"},  # the unit tests run jobs as the test user; test_sandbox.py covers the real sandbox's plumbing
        "lab": {"rf_setup": "unset"},
        "inventory": {"backend": "simulated", "devices": [
            {"id": "pluto", "kind": "pluto"}, {"id": "rtlsdr", "kind": "rtlsdr"}]},
        "profiles": {"bench": {"requires": ["pluto"], "description": "pluto on the bench"}},
        "users": users, "tokens": token_rows,
        "github": {"client_id": "gh-client"},
    }
    for k, v in (extra or {}).items():
        data.setdefault(k, {}).update(v) if isinstance(v, dict) else data.__setitem__(k, v)
    if data.get("sandbox", {}).get("mode") == "systemd":   # these live in the policy file there (the config refuses them)
        for k in ("stop_grace_seconds", "job_timeout_seconds", "devices_file", "min_commit", "branch"):
            data["service"].pop(k, None)
        data.pop("lab", None)
        data.pop("confirm_wiring", None)
    return data, tokens


def start(repo: Path, state: Path, *, extra: dict | None = None, env: dict | None = None,
          present: dict | None = None, http=None, policy_uid: int = 0) -> Running:
    port = free_port()
    data, tokens = service_data(repo, state, port, extra)
    config_path = state.parent / f"{state.name}.toml"   # what a reload (SIGHUP) reads
    config_path.write_text(toml_dump(data))
    cfg = parse_config(data, state.parent, {"OASA_LAB_GITHUB_CLIENT_SECRET": "gh-secret", **(env or {})}, policy_uid=policy_uid)
    inv = SimulatedInventory(cfg.devices, present or {})
    lab = Lab(cfg, inventory=inv, http=http, config_path=config_path)
    server = uvicorn.Server(uvicorn.Config(lab.app(), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "service did not start"
    return Running(lab, cfg, f"http://127.0.0.1:{port}", tokens, server, th, inv, data, config_path)


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
