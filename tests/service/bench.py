"""A stand-in lab PC for the systemd mode, without systemd: a git mirror, a policy file, spool and results
folders, and a fake `systemctl` that starts `job-run` the way the unit would (and refuses what polkit refuses).

The real isolation (another user, a cgroup, hidden folders, polkit) is systemd's and can only be shown on the
lab PC (deploy/selftest.sh does). Here we test what our own code decides and what it writes."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

FAKE_SYSTEMCTL = r'''#!__PYTHON__
import json, os, re, signal, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
conf = json.load(open(os.path.join(here, "fake.json")))
args = sys.argv[1:]
with open(os.path.join(here, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(args) + "\n")
words = [a for a in args if not a.startswith("--")]
verb, unit = (words + [None, None])[:2]
JOB = re.compile(r"^oasa-lab-job@[0-9a-f]{32}\.service$")


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def pidfile(u):
    return os.path.join(here, u + ".pid")


def pid_of(u):
    try:
        return int(open(pidfile(u)).read())
    except (OSError, ValueError):
        return None


if os.path.exists(os.path.join(here, "deny")):
    print("Interactive authentication required.")
    sys.exit(1)
if verb == "start" and unit == "oasa-lab-mirror.service":
    r = subprocess.run(["git", "--git-dir=" + conf["mirror"], "fetch", "-q", "--force", conf["repo"],
                        "+refs/heads/main:refs/heads/main"], capture_output=True, text=True)
    sys.exit(r.returncode)
if verb in ("start", "stop") and unit and JOB.match(unit):
    job_id = unit.split("@")[1].split(".")[0]
    pid = pid_of(unit)
    if verb == "start":
        if pid and alive(pid):
            sys.exit(0)
        code = ("import os, sys; from oasa_lab.jobrunner import run_job; "
                "sys.exit(run_job(sys.argv[1], %r, policy_uid=os.getuid()))" % conf["policy"])
        p = subprocess.Popen([conf["python"], "-c", code, job_id], cwd="/", start_new_session=True,
                             env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"},
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=open(os.path.join(here, job_id + ".err"), "w"))
        open(pidfile(unit), "w").write(str(p.pid))
        sys.exit(0)
    if pid and alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(600):
            if not alive(pid):
                break
            time.sleep(0.1)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
    sys.exit(0)
if verb == "is-active" and unit and JOB.match(unit):
    pid = pid_of(unit)
    if pid and alive(pid):
        print("active")
        sys.exit(0)
    print("inactive")
    sys.exit(3)
print("Access denied")
sys.exit(1)
'''

POLICY = """[lab]
rf_setup = "{rf_setup}"
simulated = {simulated}
devices_file = "{devices_file}"
job_timeout_seconds = {job_timeout}
stop_grace_seconds = {grace}
min_commit = "{min_commit}"
[bins]
{bins}
[confirm_wiring]
{confirm}
[paths]
spool = "{spool}"
results = "{results}"
mirror = "{mirror}"
git_config = ""
pause = "{pause}"
"""


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


class Bench:
    """Folders, a mirror of `repo`, a policy and a fake systemctl under `root`."""

    def __init__(self, root: Path, repo: Path):
        self.root, self.repo = root, repo
        self.spool, self.results, self.mirror = root / "spool", root / "results", root / "mirror.git"
        self.policy_path, self.fake = root / "lab-policy.toml", root / "fake"
        self.spool.mkdir(parents=True)
        self.results.mkdir()
        self.fake.mkdir()
        git("clone", "-q", "--bare", str(repo), str(self.mirror))
        self.write_policy()
        ctl = self.fake / "systemctl"
        ctl.write_text(FAKE_SYSTEMCTL.replace("__PYTHON__", sys.executable))
        ctl.chmod(ctl.stat().st_mode | stat.S_IXUSR)
        self.ctl = ctl
        (self.fake / "fake.json").write_text(json.dumps({"mirror": str(self.mirror), "repo": str(repo),
                                                         "policy": str(self.policy_path), "python": sys.executable}))

    def write_policy(self, rf_setup="cabled", simulated=True, devices_file="", job_timeout=120, grace=1, min_commit="",
                     bins=None, confirm=None) -> Path:
        self.policy_path.write_text(POLICY.format(
            rf_setup=rf_setup, simulated=str(simulated).lower(), devices_file=devices_file, job_timeout=job_timeout,
            grace=grace, min_commit=min_commit, spool=self.spool, results=self.results, mirror=self.mirror, pause=self.root / "PAUSE",
            bins="\n".join(f'{k} = "{v}"' for k, v in (bins or {}).items()),
            confirm="\n".join(f'{k} = "{v}"' for k, v in (confirm or {}).items())))
        self.policy_path.chmod(0o644)
        return self.policy_path

    def calls(self) -> list[list[str]]:
        p = self.fake / "calls.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def head(self) -> str:
        return git("--git-dir", str(self.mirror), "rev-parse", "refs/heads/main")

    def request(self, job_id: str, suite="t-quick", seed=0, commit: str | None = None, **over) -> Path:
        body = {"id": job_id, "suite": suite, "seed": seed, "commit": commit or self.head(), **over}
        path = self.spool / f"{job_id}.json"
        path.write_text(json.dumps(body))
        return path

    def exit_info(self, job_id: str) -> dict:
        return json.loads((self.results / job_id / "exit.json").read_text())


def wait_for(pred, timeout=30.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False
