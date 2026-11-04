"""Suites that radiate (transmit into antennas): the owner's [antenna] TX caps in the lab policy, the interlock,
job-run's environment, the hw gate per device, and the checker's rules for rf.setup = "antenna"."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from oasa_lab.checker.core import _rf_errors
from oasa_lab.hw import Hardware, antenna_caps_from_env
from oasa_lab.jobrunner import run_env
from oasa_lab.policy import Policy, PolicyError, interlock, parse_policy
from oasa_lab.suites import parse_suite, suite_facts

CAPS = {"b2000": 10.0, "e3100": 8.0}


def facts(text: str, name: str = "l2-air") -> dict:
    return suite_facts(parse_suite(name, text))


AIR = facts('description = "x"\ntransmits = true\nradiates = true\nrequires = ["b200"]\nwiring = "air"\n')
CABLE = facts('description = "x"\ntransmits = true\nrequires = ["b200"]\nwiring = "usrp"\n', "l2-cable")


# --- suite.toml -------------------------------------------------------------------------------------------------
def test_radiates_must_be_said_as_true():
    assert AIR["radiates"] is True
    assert CABLE["radiates"] is False
    assert facts('transmits = true\nradiates = "yes"\n')["radiates"] is False


# --- the policy file --------------------------------------------------------------------------------------------
def test_policy_reads_antenna_caps():
    p = parse_policy({"antenna": {"max_tx_gain_db": {"b2000": 10, "e3100": 8.5}}})
    assert p.antenna_caps == {"b2000": 10.0, "e3100": 8.5}
    assert parse_policy({}).antenna_caps == {}


@pytest.mark.parametrize("bad", [
    {"antenna": {"max_tx_gain_db": {"b2000": -1}}},
    {"antenna": {"max_tx_gain_db": {"b2000": 91}}},
    {"antenna": {"max_tx_gain_db": {"b2000": True}}},
    {"antenna": {"max_tx_gain_db": {"b2000": "10"}}},
    {"antenna": {"max_tx_gain_db": {"../x": 10}}},
    {"antenna": {"max_tx_gain_db": 10}},
    {"antenna": {"max_power": {"b2000": 10}}},
])
def test_policy_refuses_bad_antenna_caps(bad):
    with pytest.raises(PolicyError):
        parse_policy(bad)


# --- the interlock ----------------------------------------------------------------------------------------------
def test_radiating_suite_needs_the_owners_caps_and_wiring():
    confirm = {"l2-air": "air"}
    assert "no TX caps" in interlock(AIR, "cabled", False, confirm, {})
    assert "no TX caps" in interlock(AIR, "cabled", False, confirm, None)
    assert interlock(AIR, "cabled", False, confirm, CAPS) is None
    # rf_setup describes the cabled bench; it does not open or close the antennas
    assert interlock(AIR, "unset", False, confirm, CAPS) is None
    # the wiring must still be confirmed, per suite
    assert "Connect wiring 'air'" in interlock(AIR, "cabled", False, {}, CAPS)
    # on the simulated bench nothing radiates: no caps needed
    assert interlock(AIR, "unset", True, {}, {}) is None


def test_caps_do_not_open_cable_suites_and_radiates_needs_transmits():
    # caps do not loosen the cable rule
    assert "rf_setup" in interlock(CABLE, "unset", False, {"l2-cable": "usrp"}, CAPS)
    assert interlock(CABLE, "cabled", False, {"l2-cable": "usrp"}, CAPS) is None
    odd = facts('transmits = false\nradiates = true\nrequires = ["b200"]\n', "l2-odd")
    assert "not transmits = true" in interlock(odd, "cabled", False, {}, CAPS)


# --- job-run's environment --------------------------------------------------------------------------------------
def test_job_env_carries_the_caps_only_when_set(tmp_path):
    env = run_env(Policy(antenna_caps=CAPS), "0" * 32, "1" * 40, None, str(tmp_path), "/usr/bin/python3")
    assert json.loads(env["OASA_LAB_ANTENNA_CAPS"]) == CAPS
    env = run_env(Policy(), "0" * 32, "1" * 40, None, str(tmp_path), "/usr/bin/python3")
    assert "OASA_LAB_ANTENNA_CAPS" not in env


def test_caps_from_env_ignore_garbage(monkeypatch):
    monkeypatch.setenv("OASA_LAB_ANTENNA_CAPS", json.dumps(CAPS))
    assert antenna_caps_from_env() == CAPS
    for junk in ("", "not json", "[1, 2]", '{"b2000": "loud"}'):
        monkeypatch.setenv("OASA_LAB_ANTENNA_CAPS", junk)
        assert antenna_caps_from_env() == {}


# --- the hw gate ------------------------------------------------------------------------------------------------
def _hw(suite_text: str, wiring: dict, caps: dict, confirm: str | None, real: bool = True) -> Hardware:
    suite = parse_suite("l2-air", suite_text)
    ctx = SimpleNamespace(suite=suite, confirm_wiring=confirm, hardware={"devices": {}, "hub": None, "rf": None})
    devices = {"b2000": {"kind": "b200"}, "e3100": {"kind": "e310"}, "pluto0": {"kind": "pluto"}}
    inv = SimpleNamespace(devices=devices, wiring=wiring, backend_of=lambda i: "hardware" if real else "sim")
    h = object.__new__(Hardware)
    h.ctx, h.inv, h.antenna_caps = ctx, inv, dict(caps)
    return h


AIR_TOML = 'transmits = true\nradiates = true\nrequires = ["b200"]\nwiring = "air"\n'
AIR_WIRING = {"air": {"rf_setup": "antenna", "antennas": ["b2000:tx/rx", "e3100:rx2-a"]},
              "usrp": {"rf_setup": "cabled", "paths": [{"name": "p", "from": "b2000:tx/rx", "to": "e3100:rx2-b", "loss_db": 60}]}}


def test_hw_gate_per_device_and_records_the_caps():
    h = _hw(AIR_TOML, AIR_WIRING, {"b2000": 10.0}, "air")
    rf = h.use_wiring()
    assert rf["setup"] == "antenna" and rf["antenna_caps"] == {"b2000": 10.0}
    assert rf["antennas"] == ["b2000:tx/rx", "e3100:rx2-a"]
    assert h.tx_allowed("b2000") is None
    assert "no antenna TX cap" in h.tx_allowed("e3100")
    h.antenna_caps["pluto0"] = 5.0
    assert "cannot radiate" in h.tx_allowed("pluto0")


def test_hw_gate_refuses_mixed_up_wiring_and_missing_caps():
    from oasa_lab.hw import WiringRefused
    with pytest.raises(WiringRefused, match="not rf_setup"):
        _hw(AIR_TOML, AIR_WIRING, CAPS, "usrp").use_wiring("usrp")
    cable = 'transmits = true\nrequires = ["b200"]\nwiring = "air"\n'
    with pytest.raises(WiringRefused, match="does not say radiates"):
        _hw(cable, AIR_WIRING, CAPS, "air").use_wiring()
    with pytest.raises(WiringRefused, match="no antenna TX caps"):
        _hw(AIR_TOML, AIR_WIRING, {}, "air").use_wiring()
    with pytest.raises(WiringRefused, match="--confirm-wiring air"):
        _hw(AIR_TOML, AIR_WIRING, CAPS, None).use_wiring()


def test_hw_cap_never_raises_the_device_limit():
    class Dev:
        def __init__(self, cfg):
            self.cfg = dict(cfg)
        @property
        def max_tx_gain(self):
            return float(self.cfg.get("max_tx_gain", 40.0))
    for own, cap, want in ((None, 10.0, 10.0), (None, 60.0, 40.0), (30.0, 20.0, 20.0), (15.0, 20.0, 15.0)):
        d = Dev({} if own is None else {"max_tx_gain": own})
        d.cfg["max_tx_gain"] = min(float(d.max_tx_gain), cap)    # what Hardware.open does
        assert d.max_tx_gain == want


# --- the checker ------------------------------------------------------------------------------------------------
def _dev(kind: str, gain: float, backend: str = "hardware") -> dict:
    return {"kind": kind, "backend": backend, "max_tx_gain": gain}


ANT_RF = {"setup": "antenna", "wiring": "air", "paths": [], "antennas": ["b2000:tx/rx"], "antenna_caps": {"b2000": 10.0}}


def test_checker_accepts_a_capped_antenna_run():
    assert _rf_errors(ANT_RF, {"b2000": _dev("b200", 10.0)}, True, True) == []


@pytest.mark.parametrize("rf, by_id, radiates, why", [
    (ANT_RF, {"b2000": _dev("b200", 10.0)}, False, "does not declare radiates"),
    ({**ANT_RF, "antenna_caps": {}}, {"b2000": _dev("b200", 10.0)}, True, "antenna_caps"),
    ({**ANT_RF, "antennas": []}, {"b2000": _dev("b200", 10.0)}, True, "antennas"),
    (ANT_RF, {"b2000": _dev("b200", 12.0)}, True, "above its antenna cap"),
    (ANT_RF, {"b2000": _dev("b200", 10.0), "e3100": _dev("e310", 5.0)}, True, "without an antenna TX cap"),
    ({**ANT_RF, "antenna_caps": {"b2000": 120}}, {"b2000": _dev("b200", 10.0)}, True, "0 to 90"),
    ({**ANT_RF, "antenna_caps": {"p0": 5}}, {"p0": _dev("pluto", 5.0)}, True, "cannot radiate"),
    ({"setup": "cabled", "paths": []}, {}, True, "not antenna"),
])
def test_checker_refuses_bad_antenna_runs(rf, by_id, radiates, why):
    errs = _rf_errors(rf, by_id, True, radiates)
    assert errs and any(why in e for e in errs), errs
