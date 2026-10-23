"""Nothing transmits unless the running suite says so and the wiring is on record.

The job service checks `transmits` before it starts a run, but a suite could still call a transmit
method it should not. So the transmit calls themselves ask the run (Hardware.tx_allowed)."""

from types import SimpleNamespace

import pytest

from oasa_lab.devices import Inventory, TxRefused, make_device
from oasa_lab.hw import Hardware, UndeclaredTransmit, WiringRefused
from oasa_lab.suites import Suite, load_suite


def ctx_for(suite: Suite, confirm: str | None = None):
    return SimpleNamespace(suite=suite, seed=1, confirm_wiring=confirm, hardware={"devices": {}, "hub": None, "rf": None})


def suite(tmp_path, **kw) -> Suite:
    lines = [f'description = "t"'] + [f"{k} = {v}" for k, v in kw.items()]
    (tmp_path / "suite.toml").write_text("\n".join(lines) + "\n")
    return load_suite(tmp_path)


def bench(tmp_path, backend_sim=True, **kw):
    inv = Inventory.load(sim=True) if backend_sim else Inventory.load(sim=False)
    return Hardware(ctx_for(suite(tmp_path, **kw)), inv, "t"), inv


def test_a_suite_that_does_not_declare_transmits_cannot_transmit(tmp_path):
    hw, _ = bench(tmp_path, transmits="false", requires='["pluto"]', wiring='"pluto"')
    hw.use_wiring()
    with pytest.raises(TxRefused, match="does not declare transmits"):
        hw.open("pluto0").tx_tone(1, 915e6, 1e5, -30, 0.1, 2e6)
    with pytest.raises(TxRefused):
        hw.open("heltec_a").link.send_packet(b"x")
    hw.close()


def test_a_suite_with_no_wiring_on_record_cannot_transmit(tmp_path):
    hw, _ = bench(tmp_path, transmits="true", requires='["pluto"]', wiring='"pluto"')
    with pytest.raises(TxRefused, match="use_wiring"):
        hw.open("pluto0").tx_tone(1, 915e6, 1e5, -30, 0.1, 2e6)
    hw.close()


def test_a_declared_and_wired_suite_may_transmit(tmp_path):
    hw, _ = bench(tmp_path, transmits="true", requires='["pluto", "serial-radio:2"]', wiring='"pluto"')
    hw.use_wiring()
    hw.open("pluto0").tx_tone(1, 915e6, 1e5, -30, 0.1, 2e6)
    hw.open("pluto0").tx_off()  # muting is always allowed
    hw.close()


def test_muting_and_receiving_never_need_the_gate(tmp_path):
    hw, _ = bench(tmp_path, transmits="false", requires='["pluto"]')
    p = hw.open("pluto0")
    p.tx_off()
    p.capture(1, 915e6, 2e6, 64)
    hw.close()


def test_real_hardware_without_a_gate_never_transmits():
    inv = Inventory.from_dict({"devices": {"p": {"kind": "pluto", "uri": "ip:1.2.3.4"}}}, "t")
    p = make_device("p", inv, None, None)
    assert p.backend == "hardware"
    with pytest.raises(TxRefused, match="hw fixture"):
        p.tx_tone(1, 915e6, 1e5, -30, 0.1, 2e6)  # refused before any library is touched


def test_real_hardware_needs_the_confirmed_wiring_even_with_a_gate(tmp_path):
    inv = Inventory.from_dict({"devices": {"p": {"kind": "pluto", "uri": "ip:1.2.3.4"}}, "wiring": {
        "w": {"rf_setup": "cabled", "paths": [{"name": "a", "from": "p:tx1", "to": "p:rx1", "loss_db": 63.5}]}}}, "t")
    hw = Hardware(ctx_for(suite(tmp_path, transmits="true", requires='["pluto"]', wiring='"w"'), confirm="w"), inv, "t")
    hw.use_wiring()
    assert hw.tx_allowed() is None
    hw.ctx.confirm_wiring = None  # the gate looks again at every call
    assert "not confirmed" in hw.tx_allowed()


def test_real_hardware_for_a_suite_that_does_not_say_whether_it_transmits_is_refused(tmp_path):
    inv = Inventory.from_dict({"devices": {"p": {"kind": "pluto", "uri": "ip:1.2.3.4"}}}, "t")
    hw = Hardware(ctx_for(suite(tmp_path, requires='["pluto"]')), inv, "t")
    with pytest.raises(UndeclaredTransmit, match="transmits = true or transmits = false"):
        hw.open("p")
    assert issubclass(UndeclaredTransmit, WiringRefused)


def test_the_suite_list_says_whether_transmits_was_declared(tmp_path):
    assert suite(tmp_path, transmits="false").transmits_declared is True
    assert suite(tmp_path).transmits_declared is False
    assert suite(tmp_path, transmits='"yes"').transmits_declared is False  # not a real boolean
