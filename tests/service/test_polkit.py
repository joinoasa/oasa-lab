"""The polkit rule that lets the service user start and stop job units, and nothing else.

The rule is JavaScript. polkit reads it with its own engine on the lab PC; here node runs the same file with a
stand-in `polkit` object. Without node the id pattern is still compared with the one the Python side uses."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from oasa_lab.policy import JOB_ID_PATTERN, JOB_ID_RE

RULE = Path(__file__).resolve().parents[2] / "deploy" / "polkit" / "50-oasa-lab.rules"
GOOD = "0123456789abcdef0123456789abcdef"
MANAGE = "org.freedesktop.systemd1.manage-units"

HARNESS = r"""
const fs = require('fs');
let rule = null;
global.polkit = { addRule: function (f) { if (rule) throw new Error('two rules'); rule = f; },
                  Result: { NO: 'no', YES: 'yes', AUTH_SELF: 'auth_self', AUTH_SELF_KEEP: 'auth_self_keep',
                            AUTH_ADMIN: 'auth_admin', AUTH_ADMIN_KEEP: 'auth_admin_keep', NOT_HANDLED: null } };
require('vm').runInThisContext(fs.readFileSync(process.argv[2], 'utf8'), { filename: process.argv[2] });
const cases = JSON.parse(process.argv[3]);
console.log(JSON.stringify(cases.map(function (c) {
  const r = rule({ id: c.action, lookup: function (k) { return c.details[k]; } }, { user: c.user });
  return r === undefined ? null : r;
})));
"""


def decide(cases, tmp_path):
    h = tmp_path / "harness.js"
    h.write_text(HARNESS)
    out = subprocess.run(["node", str(h), str(RULE), json.dumps(cases)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def case(unit=None, verb=None, user="oasalab", action=MANAGE):
    details = {}
    if unit is not None:
        details["unit"] = unit
    if verb is not None:
        details["verb"] = verb
    return {"action": action, "user": user, "details": details}


def test_the_rule_uses_the_same_id_pattern_as_the_python_side():
    text = RULE.read_text()
    assert f"oasa-lab-job@{JOB_ID_PATTERN}\\.service" in text, "the rule and oasa_lab/policy.py must name ids the same way"
    assert JOB_ID_RE.match(GOOD) and not JOB_ID_RE.match(GOOD + "\n"), "python's $ would allow a trailing newline; ours does not"


def test_the_rule_is_ecmascript_5_for_polkits_engine():
    text = RULE.read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))
    for banned in ("=>", "`", "let ", "const ", "class ", "startsWith", "includes(", "endsWith"):
        assert banned not in code, f"{banned!r} is not ES5; polkit(8) says rules must conform to ECMA-262 edition 5"
    assert "polkit.addRule" in code and "oasalab" in code


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_rule_allows_exactly_start_and_stop_of_job_units_and_the_mirror_refresh(tmp_path):
    unit = f"oasa-lab-job@{GOOD}.service"
    cases = [
        case(unit, "start"), case(unit, "stop"), case("oasa-lab-mirror.service", "start"),
        # the same unit, other verbs
        *[case(unit, v) for v in ("restart", "try-restart", "reload", "reload-or-restart", "kill", "reset-failed", "clean",
                                  "set-property", "freeze", "")],
        # other units
        case("ssh.service", "start"), case("oasa-lab.service", "stop"), case("oasa-lab.service", "restart"),
        case("oasa-lab-mirror.service", "stop"), case("oasa-lab-mirror.service", "restart"),
        case("oasa-lab-mirror.timer", "start"), case("cloudflared.service", "stop"),
        # near misses of the job unit name
        case(f"oasa-lab-job@{GOOD[:-1]}.service", "start"), case(f"oasa-lab-job@{GOOD}0.service", "start"),
        case(f"oasa-lab-job@{GOOD.upper()}.service", "start"), case(f"oasa-lab-job@{GOOD[:-1]}g.service", "start"),
        case(f"oasa-lab-job@{GOOD}.service\n", "start"), case(f" oasa-lab-job@{GOOD}.service", "start"),
        case(f"x-oasa-lab-job@{GOOD}.service", "start"), case(f"oasa-lab-job@{GOOD}.service.d", "start"),
        case(f"oasa-lab-job@{GOOD}.socket", "start"), case(f"oasa-lab-job@{GOOD}.scope", "start"),
        case(f"oasa-lab-job@{GOOD}@x.service", "start"), case("oasa-lab-job@.service", "start"), case("oasa-lab-job.service", "start"),
        case(f"oasa-lab-job@../{GOOD}.service", "start"), case(f"oasa-lab-job@{GOOD}.service/../ssh.service", "start"),
        case(f"oasa-lab-job-evil@{GOOD}.service", "start"),
        # no details (StartTransientUnit and the like), or half of them
        case(), case(unit), case(verb="start"),
        {"action": MANAGE, "user": "oasalab", "details": {"unit": 5, "verb": "start"}},
    ]
    want = ["yes", "yes", "yes"] + ["no"] * (len(cases) - 3)
    assert decide(cases, tmp_path) == want


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_other_users_and_other_actions_are_not_touched(tmp_path):
    unit = f"oasa-lab-job@{GOOD}.service"
    cases = [case(unit, "start", user=u) for u in ("root", "oasalab-job", "oasalab-mirror", "alice", "oasalab2", "OASALAB", "")]
    cases += [case(unit, "start", action=a) for a in ("org.freedesktop.systemd1.manage-unit-files", "org.freedesktop.systemd1.reload-daemon",
                                                     "org.freedesktop.login1.reboot", "org.freedesktop.policykit.exec", "")]
    assert decide(cases, tmp_path) == [None] * len(cases)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_service_user_gets_a_plain_no_for_everything_else_it_asks_systemd(tmp_path):
    # NO, not a password prompt: an unattended service must never wait for an agent
    assert decide([case("ssh.service", "start")], tmp_path) == ["no"]
