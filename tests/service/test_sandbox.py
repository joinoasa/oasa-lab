"""The systemd sandbox's plumbing, with stand-ins for systemd-run and systemctl.

The real isolation (another user, a cgroup, hidden folders) is systemd's and can only be shown on the lab PC
(deploy/check.sh does). Here we test what the service and the root helper decide: what is validated, what
command line is built, what environment a job gets, how stop works, and that the helper trusts nothing."""

import json
import os
import stat
import sys
import textwrap
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
    os.kill(int(open(pidfile).read()), signal.SIGTERM)
except (OSError, ValueError):
    sys.exit(5)
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


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """Stand-in systemd tools, a root-owned-style helper config, and a way to start a service that uses them."""
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
    monkeypatch.setenv("OASA_LAB_RUNNER_CONFIG", str(conf))
    monkeypatch.setenv("OASA_LAB_SYSTEMD_RUN", str(sd))
    monkeypatch.setenv("OASA_LAB_SYSTEMCTL", str(ctl))
    started = []

    class B:
        pass

    b = B()
    b.dir, b.log, b.state, b.conf = d, log, state, conf
    b.runner = ["env", f"OASA_LAB_RUNNER_CONFIG={conf}", f"OASA_LAB_SYSTEMD_RUN={sd}", f"OASA_LAB_SYSTEMCTL={ctl}",
                f"FAKE_SD_LOG={log}", f"FAKE_SD_DIR={d}", sys.executable, "-I", "-m", "oasa_lab.service.jobrun"]
    b.records = lambda: [json.loads(x) for x in log.read_text().splitlines() if x]

    def make(lab_repo, extra=None, **conf_kw):
        conf.write_text(conf_text(state / "state", **conf_kw))
        s = start(lab_repo, state / "state", extra={"sandbox": {"mode": "systemd", "runner": b.runner}, **(extra or {})})
        started.append(s)
        return s

    b.make = make
    yield b
    for s in started:
        stop(s)


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


def test_the_rf_setup_and_device_file_come_from_the_root_owned_config_not_from_the_service(bench, lab_repo):
    """The service config says 'unset'; the root-owned one says 'shielded'. The job gets the root-owned value."""
    dev = bench.dir / "lab.toml"
    dev.write_text(SIM_BENCH)
    svc = bench.make(lab_repo, rf="shielded", backend="usb", devices_file=dev, confirm={"t-tx": "lora"},
                     extra={"inventory": {"backend": "usb"}, "service": {"devices_file": str(dev)},
                            "confirm_wiring": {"t-tx": "lora"}, "lab": {"rf_setup": "shielded"}})
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


def test_stop_asks_systemctl_to_stop_the_unit(bench, lab_repo):
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


def test_a_helper_that_cannot_stop_the_job_is_killed_after_the_grace_period(bench, lab_repo):
    svc = bench.make(lab_repo)
    job = svc.submit("t-stubborn")                           # ignores SIGTERM
    svc.wait(job["id"], want=("running",))
    time.sleep(1.5)
    t0 = time.time()
    with svc.client() as c:
        assert c.post(f"/api/jobs/{job['id']}/stop", json={}).json()["status"] == "stopped"
    assert time.time() - t0 < 15


# --- the helper on its own --------------------------------------------------------------------------------
def make_job(state: Path, job_id="0123456789ab", launch=None, src=True):
    jd = state / "jobs" / job_id
    jd.mkdir(parents=True)
    if src:
        (jd / "src").mkdir()
    (jd / "launch.json").write_text(json.dumps(launch if launch is not None else
                                               {"suite": "t-quick", "seed": 1, "sha": "a" * 40, "confirm_wiring": None,
                                                "env": {"OASA_LAB_JOB_ID": job_id, "OASA_LAB_REQUESTED_BY": "owner"}}))
    return jd


@pytest.fixture
def helper(tmp_path, monkeypatch):
    state = tmp_path / "state"
    conf = tmp_path / "service.toml"
    conf.write_text(conf_text(state))
    monkeypatch.setenv("OASA_LAB_RUNNER_CONFIG", str(conf))
    execs = []
    monkeypatch.setattr(os, "execvp", lambda f, a: execs.append((f, a)))
    h = type("H", (), {})()
    h.state, h.conf, h.execs = state, conf, execs
    (state / "jobs").mkdir(parents=True)
    return h


def launch_with(**kw):
    base = {"suite": "t-quick", "seed": 1, "sha": "a" * 40, "confirm_wiring": None,
            "env": {"OASA_LAB_JOB_ID": "0123456789ab", "OASA_LAB_REQUESTED_BY": "owner"}}
    base.update(kw)
    return base


def test_helper_builds_the_command_for_a_good_request(helper, capsys):
    jd = make_job(helper.state, launch=launch_with(env={"OASA_LAB_JOB_ID": "0123456789ab", "OASA_LAB_REQUESTED_BY": "owner",
                                                          "OASA_SEC_BIN_DIR": "/opt/oasa-sec/bin"}))
    assert jobrun.main(["run", "0123456789ab"]) == 0
    (prog, argv), = helper.execs
    assert argv[-9:] == [sys.executable, "-m", "oasa_lab", "run", "t-quick", "--seed", "1", "--out", str(jd / "runs")]
    assert (jd / "runs").is_dir()
    assert f"--uid=oasalab-job" in argv and "--gid=oasalab-job" in argv
    assert "OASA_LAB_RF_SETUP=cabled" in " ".join(argv) and "--setenv=OASA_LAB_SIM=1" in argv


@pytest.mark.parametrize("launch", [
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
])
def test_helper_refuses_bad_requests(helper, launch):
    make_job(helper.state, launch=launch)
    assert jobrun.main(["run", "0123456789ab"]) == 3
    assert helper.execs == []


def test_helper_refuses_a_launch_file_that_is_a_link_or_not_json(helper, tmp_path):
    jd = make_job(helper.state)
    (jd / "launch.json").unlink()
    (tmp_path / "elsewhere.json").write_text("{}")
    (jd / "launch.json").symlink_to(tmp_path / "elsewhere.json")
    assert jobrun.main(["run", "0123456789ab"]) in (3, 4) and helper.execs == []
    (jd / "launch.json").unlink()
    (jd / "launch.json").write_text("[1,2")
    assert jobrun.main(["run", "0123456789ab"]) == 3 and helper.execs == []


def test_helper_refuses_odd_job_folders(helper, tmp_path):
    assert jobrun.main(["run", "../../etc"]) == 3                  # not a job id
    assert jobrun.main(["run", "0123456789AB"]) == 3
    real = tmp_path / "real"
    real.mkdir()
    (helper.state / "jobs" / "0123456789ab").symlink_to(real)      # a job folder that is a link
    assert jobrun.main(["run", "0123456789ab"]) == 3
    jd = make_job(helper.state, job_id="0123456789ac")
    (jd / "runs").mkdir()                                          # already used
    assert jobrun.main(["run", "0123456789ac"]) == 3
    jd2 = make_job(helper.state, job_id="0123456789ad", src=False)  # no source copy
    assert jobrun.main(["run", "0123456789ad"]) in (3, 4)
    assert helper.execs == []


def test_helper_passes_only_the_confirmation_the_config_holds(helper, tmp_path):
    helper.conf.write_text(conf_text(helper.state, backend="usb", confirm={"t-tx": "lora"}))
    make_job(helper.state, launch=launch_with(suite="t-tx", confirm_wiring="lora"))
    assert jobrun.main(["run", "0123456789ab"]) == 0
    argv = helper.execs[-1][1]
    assert argv[-2:] == ["--confirm-wiring", "lora"] and "--setenv=OASA_LAB_WIRING_CONFIRMED=lora" in argv
    assert not any(a.startswith("--setenv=OASA_LAB_SIM") for a in argv)
    make_job(helper.state, job_id="0123456789ac", launch=launch_with(suite="t-tx", confirm_wiring="other",
                                                                     env={"OASA_LAB_JOB_ID": "0123456789ac"}))
    n = len(helper.execs)
    assert jobrun.main(["run", "0123456789ac"]) == 3 and len(helper.execs) == n
    make_job(helper.state, job_id="0123456789ae", launch=launch_with(suite="t-other", confirm_wiring="lora",
                                                                     env={"OASA_LAB_JOB_ID": "0123456789ae"}))
    assert jobrun.main(["run", "0123456789ae"]) == 3              # a confirmation for another suite does not carry over


def test_helper_stop_and_clean(helper, tmp_path):
    jd = make_job(helper.state)
    assert jobrun.main(["stop", "0123456789ab"]) == 0
    assert helper.execs[-1][1][-3:] == ["stop", "--quiet", "oasa-job-0123456789ab.service"]
    (jd / "runs").mkdir()
    (jd / "runs" / "x").write_text("x")
    assert jobrun.main(["clean", "0123456789ab"]) == 0 and not (jd / "runs").exists()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("keep")
    (jd / "runs").symlink_to(victim)                               # clean must not follow a planted link
    assert jobrun.main(["clean", "0123456789ab"]) == 0 and (victim / "keep").exists()
    assert jobrun.main(["bogus", "0123456789ab"]) == 2 and jobrun.main(["run"]) == 2


def test_through_sudo_the_helper_uses_only_the_fixed_config_and_tools(monkeypatch, tmp_path):
    f = tmp_path / "evil.toml"
    f.write_text(conf_text(tmp_path / "s"))
    monkeypatch.setenv("OASA_LAB_RUNNER_CONFIG", str(f))
    monkeypatch.setenv("OASA_LAB_SYSTEMD_RUN", "/tmp/evil")
    monkeypatch.setenv("SUDO_UID", "998")                         # what sudo sets
    seen = []
    import oasa_lab.service.config as config
    monkeypatch.setattr(config, "load_config", lambda path, env=None: seen.append(str(path)) or (_ for _ in ()).throw(OSError("stop")))
    assert jobrun.main(["clean", "0123456789ab"]) == 4
    assert seen == [jobrun.CONFIG], "through sudo the config path is fixed"
    assert jobrun._tool("systemd-run", "OASA_LAB_SYSTEMD_RUN") == "systemd-run"
    assert jobrun._caller() == 998
    monkeypatch.setenv("SUDO_UID", "not-a-number")
    with pytest.raises(jobrun.Refused):
        jobrun._caller()


def test_through_sudo_the_job_folder_must_belong_to_the_caller(monkeypatch, helper):
    make_job(helper.state)
    monkeypatch.setenv("SUDO_UID", str(os.getuid() + 1))           # somebody else
    monkeypatch.delenv("OASA_LAB_RUNNER_CONFIG")
    monkeypatch.setattr(jobrun, "CONFIG", str(helper.conf))
    assert jobrun.main(["run", "0123456789ab"]) == 3 and helper.execs == []
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))               # the owner of the folder
    assert jobrun._job_folder(jobrun._config(), "0123456789ab").name == "0123456789ab"


def test_the_job_user_must_be_unprivileged():
    base = {"service": {"port": 8765, "public_url": "http://127.0.0.1:8765"}, "users": [{"name": "o", "role": "admin", "github_id": 1}]}
    for bad in ("root", "", "Bad User", "x;y"):
        with pytest.raises(ConfigError):
            parse_config({**base, "sandbox": {"user": bad}}, Path("."), {})
    with pytest.raises(ConfigError):
        parse_config({**base, "sandbox": {"mode": "docker"}}, Path("."), {})
    assert parse_config(base, Path("."), {}).sandbox.mode == "systemd", "the default is the real sandbox"
