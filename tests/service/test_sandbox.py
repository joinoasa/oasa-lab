"""The systemd sandbox's plumbing, with stand-ins for systemd-run and systemctl and the helper on a real Unix socket.

The real isolation (another user, a cgroup, hidden folders) is systemd's and can only be shown on the lab PC
(deploy/check.sh does). Here we test what the service and the root helper decide: what is validated, what
command line is built, what environment a job gets, how stop works, and that the helper trusts nothing."""

import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import pytest

from oasa_lab.service import jobrun
from oasa_lab.service.config import ConfigError, parse_config

from .conftest import SIM_BENCH, start, stop

FAKE_SYSTEMD_RUN = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
opts, props, env, cmd, i = [], [], {}, [], 0
while i < len(args):
    a = args[i]
    if a == "--":
        cmd = args[i + 1:]
        break
    if a == "-p":
        props.append(args[i + 1]); i += 2; continue
    if a.startswith("--setenv="):
        k, v = a[len("--setenv="):].split("=", 1); env[k] = v
    else:
        opts.append(a)
    i += 1
unit = next(o.split("=", 1)[1] for o in opts if o.startswith("--unit="))
with open(os.environ["FAKE_SD_LOG"], "a") as fh:
    fh.write(json.dumps({"opts": opts, "props": props, "env": env, "cmd": cmd}) + "\\n")
with open(os.path.join(os.environ["FAKE_SD_DIR"], unit + ".pid"), "w") as fh:
    fh.write(str(os.getpid()))
os.chdir(next(p.split("=", 1)[1] for p in props if p.startswith("WorkingDirectory=")))
os.execvpe(cmd[0], cmd, env)
'''
FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
import json, os, signal, sys
unit = sys.argv[-1]
with open(os.environ["FAKE_SD_LOG"], "a") as fh:
    fh.write(json.dumps({"systemctl": sys.argv[1:]}) + "\\n")
pidfile = os.path.join(os.environ["FAKE_SD_DIR"], unit.removesuffix(".service") + ".pid")
try:
    pid = int(open(pidfile).read())
    os.kill(pid, signal.SIGTERM)
except (OSError, ValueError):
    sys.exit(5)
import time
time.sleep(1.5)          # systemd's TimeoutStopSec: then SIGKILL to what is left
try:
    os.kill(pid, signal.SIGKILL)
except OSError:
    pass
'''


def conf_text(state: Path, backend="simulated", confirm=None, devices_file=None, rf="cabled") -> str:
    t = textwrap.dedent(f'''
        [service]
        state_dir = "{state}"
        job_timeout_seconds = 120
        stop_grace_seconds = 1
        {f'devices_file = "{devices_file}"' if devices_file else ""}
        [lab]
        rf_setup = "{rf}"
        [sandbox]
        mode = "systemd"
        user = "oasalab-job"
        [inventory]
        backend = "{backend}"
        [[users]]
        name = "owner"
        role = "admin"
        github_id = 1
    ''')
    if confirm:
        t += "[confirm_wiring]\n" + "".join(f'{k} = "{v}"\n' for k, v in confirm.items())
    return t


class HelperSocket:
    """What systemd does for oasa-lab-jobrun.socket: accept a connection, run the helper on it."""

    def __init__(self, path: str):
        self.path = path
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    @staticmethod
    def _one(conn):
        with conn:
            jobrun.serve(conn)

    def close(self):
        self.srv.close()


def ask(line: bytes, timeout=20):
    """Talk to the helper over a socket pair. Returns (what it sent back, its return code)."""
    a, b = socket.socketpair()
    rc = []
    t = threading.Thread(target=lambda: rc.append(jobrun.serve(b)))
    t.start()
    a.sendall(line)
    t.join(timeout)
    b.close()
    a.settimeout(5)
    out = b""
    try:
        while chunk := a.recv(65536):
            out += chunk
    except OSError:
        pass
    a.close()
    return out.decode(errors="replace"), (rc[0] if rc else None)


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """Stand-in systemd tools, a root-owned-style helper config, the helper on a socket, and a service that uses them."""
    d = tmp_path / "fake"
    d.mkdir()
    sd, ctl, log = d / "systemd-run", d / "systemctl", d / "log.jsonl"
    sd.write_text(FAKE_SYSTEMD_RUN)
    ctl.write_text(FAKE_SYSTEMCTL)
    for f in (sd, ctl):
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    log.write_text("")
    state = tmp_path / "svc"
    conf = tmp_path / "service.toml"
    for k, v in {"OASA_LAB_JOBRUN_TEST": "1", "OASA_LAB_RUNNER_CONFIG": str(conf), "OASA_LAB_SYSTEMD_RUN": str(sd),
                 "OASA_LAB_SYSTEMCTL": str(ctl), "FAKE_SD_LOG": str(log), "FAKE_SD_DIR": str(d)}.items():
        monkeypatch.setenv(k, v)
    sockdir = tempfile.mkdtemp(prefix="jr")
    sock = HelperSocket(os.path.join(sockdir, "s"))
    started = []

    class B:
        pass

    b = B()
    b.dir, b.log, b.state, b.conf, b.socket = d, log, state, conf, sock.path
    b.records = lambda: [json.loads(x) for x in log.read_text().splitlines() if x]

    def make(lab_repo, extra=None, **conf_kw):
        conf.write_text(conf_text(state / "state", **conf_kw))
        s = start(lab_repo, state / "state", extra={"sandbox": {"mode": "systemd", "socket": sock.path}, **(extra or {})})
        started.append(s)
        return s

    b.make = make
    yield b
    for s in started:
        stop(s)
    sock.close()
    shutil.rmtree(sockdir, ignore_errors=True)


def test_a_job_runs_through_the_helper_with_a_clean_environment_and_the_sandbox_properties(bench, lab_repo, monkeypatch):
    monkeypatch.setenv("SECRET_FOR_TEST", "hunter2")
    monkeypatch.setenv("OASA_SEC_BIN_DIR", "/opt/oasa-sec/bin")
    svc = bench.make(lab_repo)
    done = svc.wait(svc.submit("t-env")["id"])
    assert done["status"] == "passed" and done["check_ok"] is True, done
    (rec,) = [r for r in bench.records() if "cmd" in r]
    assert f"--unit=oasa-job-{done['id']}" in rec["opts"] and "--uid=oasalab-job" in rec["opts"]
    assert {"--wait", "--pipe", "--collect"} <= set(rec["opts"])
    props = set(rec["props"])
    for want in ("NoNewPrivileges=yes", "DevicePolicy=closed", "ProtectSystem=strict", "PrivateTmp=yes", "ProtectProc=invisible",
                 "SystemCallFilter=@system-service", "KillMode=control-group", "MemoryMax=4G", "TasksMax=512",
                 "DeviceAllow=char-usb_device rw", "SupplementaryGroups=plugdev dialout",
                 f"TemporaryFileSystem={svc.cfg.state_dir}:ro"):
        assert want in props, want
    jd = svc.cfg.state_dir / "jobs" / done["id"]
    assert f"BindReadOnlyPaths={jd / 'src'}" in props and f"BindPaths={jd / 'runs'}" in props
    assert any(p.startswith("RuntimeMaxSec=") for p in props)
    # the job's environment: exactly what the helper allows, nothing of the service's
    assert set(rec["env"]) <= {"OASA_LAB_JOB_ID", "OASA_LAB_REQUESTED_BY", "OASA_LAB_RF_SETUP", "OASA_LAB_SIM", "OASA_SEC_BIN_DIR",
                               "PATH", "HOME", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "LANG", "TZ"}, rec["env"]
    assert rec["env"]["OASA_SEC_BIN_DIR"] == "/opt/oasa-sec/bin" and rec["env"]["OASA_LAB_RF_SETUP"] == "cabled"
    assert rec["env"]["HOME"] == "/tmp" and rec["env"]["OASA_LAB_SIM"] == "1"
    assert "hunter2" not in json.dumps(rec) and "OASA_LAB_GITHUB_CLIENT_SECRET" not in rec["env"]
    assert rec["cmd"][1:] == ["-m", "oasa_lab", "run", "t-env", "--seed", "0", "--out", str(jd / "runs")]
    # afterwards: evidence copied out and judged by the service, the job's own folders removed
    assert (jd / "verified" / done["bundle"]).is_dir() and not (jd / "src").exists() and not (jd / "runs").exists()
    assert json.loads((jd / "exit.json").read_text()) == {"code": 0}
    assert "oasa-lab" in (jd / "stdout.log").read_text() or "checker" in (jd / "stdout.log").read_text()


def test_the_rf_setup_and_device_file_come_from_the_root_owned_config_not_from_the_service(bench, lab_repo):
    """The service config says one thing; the root-owned one says another. The job gets the root-owned value."""
    dev = bench.dir / "lab.toml"
    dev.write_text(SIM_BENCH)
    svc = bench.make(lab_repo, rf="shielded", backend="usb", devices_file=dev, confirm={"t-tx": "lora"},
                     extra={"inventory": {"backend": "usb"}, "service": {"devices_file": str(dev)},
                            "confirm_wiring": {"t-tx": "lora"}, "lab": {"rf_setup": "cabled"}})
    done = svc.wait(svc.submit("t-tx")["id"])
    assert done["status"] == "passed", done
    (rec,) = [r for r in bench.records() if "cmd" in r]
    assert rec["env"]["OASA_LAB_RF_SETUP"] == "shielded" and rec["env"]["OASA_LAB_INVENTORY"] == str(dev)
    assert "OASA_LAB_SIM" not in rec["env"] and rec["env"]["OASA_LAB_WIRING_CONFIRMED"] == "lora"
    assert rec["cmd"][-2:] == ["--confirm-wiring", "lora"]
    assert done["wiring_confirmed"] == "lora"


def test_the_helper_refuses_a_wiring_confirmation_the_owner_did_not_write(bench, lab_repo):
    """A service that was talked into a confirmation still cannot make the root helper pass it."""
    dev = bench.dir / "lab.toml"
    dev.write_text(SIM_BENCH)
    svc = bench.make(lab_repo, backend="usb", devices_file=dev, confirm={},   # the root-owned config confirms nothing
                     extra={"inventory": {"backend": "usb"}, "service": {"devices_file": str(dev)},
                            "confirm_wiring": {"t-tx": "lora"}, "lab": {"rf_setup": "cabled"}})
    done = svc.wait(svc.submit("t-tx")["id"])
    assert done["status"] == "error", done
    assert not [r for r in bench.records() if "cmd" in r], "systemd-run must not have been started"
    assert "wiring confirmation does not match" in (svc.cfg.state_dir / "jobs" / done["id"] / "stdout.log").read_text()


def test_stop_asks_for_the_unit_to_be_stopped(bench, lab_repo):
    svc = bench.make(lab_repo)
    job = svc.submit("t-slow")
    svc.wait(job["id"], want=("running",))
    deadline = time.time() + 20
    while time.time() < deadline and not any("cmd" in r for r in bench.records()):
        time.sleep(0.1)
    with svc.client() as c:
        r = c.post(f"/api/jobs/{job['id']}/stop", json={})
    assert r.status_code == 200 and r.json()["status"] == "stopped", r.text
    assert {"systemctl": ["stop", "--quiet", f"oasa-job-{job['id']}.service"]} in bench.records()


def test_a_job_that_ignores_the_stop_is_cut_loose_after_the_grace_period(bench, lab_repo):
    svc = bench.make(lab_repo)
    job = svc.submit("t-stubborn")                           # ignores SIGTERM, so the stand-in systemctl does not end it
    svc.wait(job["id"], want=("running",))
    time.sleep(1.5)
    t0 = time.time()
    with svc.client() as c:
        assert c.post(f"/api/jobs/{job['id']}/stop", json={}).json()["status"] == "stopped"
    assert time.time() - t0 < 30


def test_without_the_helper_a_job_fails_cleanly(bench, lab_repo):
    svc = bench.make(lab_repo)
    svc.cfg.sandbox.__class__  # the config object is frozen; point the sandbox at a socket nobody listens on
    svc.lab.jobs.sandbox.socket = "/nonexistent/jobrun.sock"
    done = svc.wait(svc.submit("t-quick")["id"])
    assert done["status"] == "error"
    assert "cannot reach the job helper" in (svc.cfg.state_dir / "jobs" / done["id"] / "stdout.log").read_text()


# --- the helper on its own --------------------------------------------------------------------------------
def make_job(state: Path, job_id="0123456789ab", launch=None, src=True):
    jd = state / "jobs" / job_id
    jd.mkdir(parents=True)
    if src:
        (jd / "src").mkdir()
    (jd / "launch.json").write_text(json.dumps(launch if launch is not None else launch_with()))
    return jd


def launch_with(**kw):
    base = {"suite": "t-quick", "seed": 1, "sha": "a" * 40, "confirm_wiring": None,
            "env": {"OASA_LAB_JOB_ID": "0123456789ab", "OASA_LAB_REQUESTED_BY": "owner"}}
    base.update(kw)
    return base


@pytest.fixture
def helper(tmp_path, monkeypatch):
    state = tmp_path / "state"
    conf = tmp_path / "service.toml"
    conf.write_text(conf_text(state))
    monkeypatch.setenv("OASA_LAB_JOBRUN_TEST", "1")
    monkeypatch.setenv("OASA_LAB_RUNNER_CONFIG", str(conf))
    h = type("H", (), {})()
    h.state, h.conf = state, conf
    (state / "jobs").mkdir(parents=True)
    return h


def test_helper_builds_the_command_for_a_good_request(helper):
    jd = make_job(helper.state, launch=launch_with(env={"OASA_LAB_JOB_ID": "0123456789ab", "OASA_LAB_REQUESTED_BY": "owner",
                                                          "OASA_SEC_BIN_DIR": "/opt/oasa-sec/bin"}))
    argv, folder = jobrun.prepare(jobrun._config(), os.getuid(), "0123456789ab")
    assert folder == jd and (jd / "runs").is_dir()
    assert argv[-9:] == [sys.executable, "-m", "oasa_lab", "run", "t-quick", "--seed", "1", "--out", str(jd / "runs")]
    assert "--uid=oasalab-job" in argv and "--gid=oasalab-job" in argv
    assert "--setenv=OASA_LAB_RF_SETUP=cabled" in argv and "--setenv=OASA_LAB_SIM=1" in argv
    assert "--setenv=OASA_SEC_BIN_DIR=/opt/oasa-sec/bin" in argv


BAD_LAUNCHES = [
    launch_with(suite="../etc"), launch_with(suite="T-Quick"), launch_with(suite=""), launch_with(suite=None),
    launch_with(sha="abc"), launch_with(sha="G" * 40), launch_with(seed=-1), launch_with(seed=True), launch_with(seed="3"),
    launch_with(seed=2**31),
    launch_with(env={"LD_PRELOAD": "/tmp/x.so"}), launch_with(env={"PATH": "/tmp/evil"}), launch_with(env={"HOME": "/root"}),
    launch_with(env={"OASA_LAB_RF_SETUP": "cabled"}),            # decided by the owner's config only
    launch_with(env={"OASA_LAB_SIM": "1"}), launch_with(env={"OASA_LAB_INVENTORY": "/etc/shadow"}),
    launch_with(env={"OASA_SEC_BIN_DIR": "relative/dir"}), launch_with(env={"OASA_SEC_BIN_DIR": "/a/../etc"}),
    launch_with(env={"OASA_SEC_BIN_DIR": "/a b"}), launch_with(env={"OASA_LAB_DEVICES": "a;rm -rf /"}),
    launch_with(env={"OASA_LAB_JOB_ID": "ffffffffffff"}), launch_with(env=["x"]), launch_with(env={"LANG": 5}),
    launch_with(confirm_wiring="lora"),                           # simulated config: nothing to confirm
]


@pytest.mark.parametrize("launch", BAD_LAUNCHES)
def test_helper_refuses_bad_requests(helper, launch):
    make_job(helper.state, launch=launch)
    out, rc = ask(b"run 0123456789ab\n")
    assert rc == 3 and "refused" in out, out
    assert not (helper.state / "jobs" / "0123456789ab" / "runs").exists()


def test_helper_refuses_a_launch_file_that_is_a_link_or_not_json(helper, tmp_path):
    jd = make_job(helper.state)
    (jd / "launch.json").unlink()
    (tmp_path / "elsewhere.json").write_text("{}")
    (jd / "launch.json").symlink_to(tmp_path / "elsewhere.json")
    assert ask(b"run 0123456789ab\n")[1] == 3
    (jd / "launch.json").unlink()
    (jd / "launch.json").write_text("[1,2")
    assert ask(b"run 0123456789ab\n")[1] == 3


def test_helper_refuses_odd_requests_and_job_folders(helper, tmp_path):
    for line in (b"run ../../etc\n", b"run 0123456789AB\n", b"fly 0123456789ab\n", b"run\n", b"run a b\n", b"\n", b"x" * 200):
        assert ask(line)[1] == 3, line
    real = tmp_path / "real"
    real.mkdir()
    (helper.state / "jobs" / "0123456789ab").symlink_to(real)      # a job folder that is a link
    assert ask(b"run 0123456789ab\n")[1] == 3
    jd = make_job(helper.state, job_id="0123456789ac")
    (jd / "runs").mkdir()                                          # already used
    assert ask(b"run 0123456789ac\n")[1] == 3
    make_job(helper.state, job_id="0123456789ad", src=False)       # no source copy
    assert ask(b"run 0123456789ad\n")[1] == 3


def test_helper_serves_only_the_user_that_owns_the_job_folders(helper):
    make_job(helper.state)
    a, b = socket.socketpair()
    a.sendall(b"stop 0123456789ab\n")
    assert jobrun.serve(b, caller=os.getuid() + 1) == 3, "a caller that does not own the folder is refused"
    assert b"not owned" in a.recv(1000)
    a.close()
    b.close()
    assert jobrun.peer_uid(socket.socketpair()[0]) == os.getuid()


def test_helper_passes_only_the_confirmation_the_config_holds(helper):
    helper.conf.write_text(conf_text(helper.state, backend="usb", confirm={"t-tx": "lora"}))
    make_job(helper.state, launch=launch_with(suite="t-tx", confirm_wiring="lora"))
    argv, _ = jobrun.prepare(jobrun._config(), os.getuid(), "0123456789ab")
    assert argv[-2:] == ["--confirm-wiring", "lora"] and "--setenv=OASA_LAB_WIRING_CONFIRMED=lora" in argv
    assert not any(a.startswith("--setenv=OASA_LAB_SIM") for a in argv)
    make_job(helper.state, job_id="0123456789ac", launch=launch_with(suite="t-tx", confirm_wiring="other",
                                                                     env={"OASA_LAB_JOB_ID": "0123456789ac"}))
    assert ask(b"run 0123456789ac\n")[1] == 3
    make_job(helper.state, job_id="0123456789ae", launch=launch_with(suite="t-other", confirm_wiring="lora",
                                                                     env={"OASA_LAB_JOB_ID": "0123456789ae"}))
    assert ask(b"run 0123456789ae\n")[1] == 3                     # a confirmation for another suite does not carry over


def test_helper_stop_and_clean(helper, tmp_path, monkeypatch):
    d = tmp_path / "fake"
    d.mkdir()
    ctl, log = d / "systemctl", d / "log"
    ctl.write_text("#!/bin/sh\necho \"$@\" >> " + str(log) + "\n")
    ctl.chmod(0o755)
    monkeypatch.setenv("OASA_LAB_SYSTEMCTL", str(ctl))
    jd = make_job(helper.state)
    assert ask(b"stop 0123456789ab\n") == ("ok\n", 0)
    assert log.read_text().strip() == "stop --quiet oasa-job-0123456789ab.service"
    (jd / "runs").mkdir()
    (jd / "runs" / "x").write_text("x")
    assert ask(b"clean 0123456789ab\n") == ("ok\n", 0) and not (jd / "runs").exists()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("keep")
    (jd / "runs").symlink_to(victim)                               # clean must not follow a planted link
    assert ask(b"clean 0123456789ab\n")[1] == 0 and (victim / "keep").exists()


def test_the_exit_file_is_written_without_following_a_planted_link(helper, tmp_path):
    jd = make_job(helper.state)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (jd / "exit.json").symlink_to(victim)
    jobrun._write_exit(jd, 7)
    assert victim.read_text() == "keep" and json.loads((jd / "exit.json").read_text()) == {"code": 7}
    (tmp_path / "other").mkdir()
    swapped = helper.state / "jobs" / "0123456789ac"
    swapped.symlink_to(tmp_path / "other")
    with pytest.raises(OSError):
        jobrun._write_exit(swapped, 0)                              # a job folder swapped for a link


def test_hanging_up_stops_the_job(bench, lab_repo):
    """If the service dies, its connection closes, and the helper stops the job."""
    svc = bench.make(lab_repo)
    job = svc.submit("t-slow")
    svc.wait(job["id"], want=("running",))
    deadline = time.time() + 20
    while time.time() < deadline and not any("cmd" in r for r in bench.records()):
        time.sleep(0.1)
    jobs = svc.lab.jobs
    jobs._proc.writer.close()                                        # what a crashed service leaves behind
    svc.wait(job["id"], timeout=30)
    unit = f"oasa-job-{job['id']}.service"
    deadline = time.time() + 15
    while time.time() < deadline and not any(r.get("systemctl", [""])[-1] == unit for r in bench.records()):
        time.sleep(0.1)
    assert {"systemctl": ["stop", "--quiet", unit]} in bench.records(), "the helper must stop the unit when the service hangs up"


@pytest.mark.parametrize("says,want", [("sandbox-ok", 0), ("sandbox-bad: can read /etc/oasa-lab/env", 1)])
def test_the_self_test_reports_what_the_sandbox_said(helper, tmp_path, monkeypatch, says, want):
    fake = tmp_path / "sd"
    fake.write_text(f"#!/bin/sh\necho '{says}'\necho \"$@\" > {tmp_path}/argv\n[ \"{want}\" = 0 ]\n")
    fake.chmod(0o755)
    monkeypatch.setenv("OASA_LAB_SYSTEMD_RUN", str(fake))
    assert jobrun.main(["selftest"]) == want
    argv = (tmp_path / "argv").read_text()
    assert "--uid=oasalab-job" in argv and "NoNewPrivileges=yes" in argv and "-I -c" in argv
    assert not (helper.state / "jobs" / "5e1f7e57e57e").exists(), "the self-test cleans up after itself"


def test_the_self_test_script_itself_passes_where_it_should(tmp_path):
    """Run the script the self-test sends into the sandbox, here without one: it must flag the missing isolation."""
    import subprocess

    state = tmp_path / "state"
    (state / "jobs" / "x").mkdir(parents=True)
    (state / "oauth.json").write_text("{}")
    src, runs = tmp_path / "src", tmp_path / "runs"
    src.mkdir()
    runs.mkdir()
    r = subprocess.run([sys.executable, "-I", "-c", jobrun.SELFTEST, "nobody-such-user", str(state), "other", str(src), str(runs)],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "sandbox-bad" in r.stdout
    assert "can read" in r.stdout and "sees" in r.stdout and "src" in r.stdout


def test_the_helper_is_not_a_general_launcher_through_its_own_socket(helper):
    out, rc = ask(b"run 0123456789ab; id\n")
    assert rc == 3 and "usage" in out


def test_the_job_user_must_be_unprivileged():
    base = {"service": {"port": 8765, "public_url": "http://127.0.0.1:8765"}, "users": [{"name": "o", "role": "admin", "github_id": 1}]}
    for bad in ("root", "", "Bad User", "x;y"):
        with pytest.raises(ConfigError):
            parse_config({**base, "sandbox": {"user": bad}}, Path("."), {})
    with pytest.raises(ConfigError):
        parse_config({**base, "sandbox": {"mode": "docker"}}, Path("."), {})
    assert parse_config(base, Path("."), {}).sandbox.mode == "systemd", "the default is the real sandbox"
