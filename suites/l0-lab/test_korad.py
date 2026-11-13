"""The KORAD driver: the 12 V, 3 A limit, who may touch the output, and the real serial path against a pty."""

import os
import threading
import time

import pytest

from oasa_lab.devices import DeviceError, Inventory, Korad, make_device
from oasa_lab.devices import korad as korad_module
from oasa_lab.devices.inventory import DEFAULT_SIM


def supply(**cfg):
    sim = cfg.pop("sim", {})
    inv = Inventory.from_dict({"lab": {"backend": "sim"},
                               "devices": {"k": {"kind": "korad", "port": "sim:k", "serial": "SIM-KORAD-0001",
                                                 **cfg, "sim": sim}}}, "test")
    return make_device("k", inv, None, None)


def changes(dev):
    return [c for c in dev.port.written if not c.endswith("?")]


def test_it_opens_on_the_built_in_sim_bench():
    inv = Inventory.from_dict(DEFAULT_SIM, "test", force_sim=True)
    assert inv.has("korad")
    with make_device("korad0", inv, None, None) as dev:
        info = dev.info()
    assert isinstance(dev, Korad)
    assert (info["kind"], info["backend"], info["serial"]) == ("korad", "sim", "SIM-KORAD-0001")
    assert info["model"] == "KORAD KA3005P" and info["firmware"] == "V1.5"


@pytest.mark.parametrize("volts,amps", [(12.01, 1), (1, 3.001), (100, 1), (1, 100), (-1, 1), (1, -1)])
def test_over_the_limit_never_reaches_the_port(volts, amps):
    with supply() as dev:
        before = list(dev.port.written)
        with pytest.raises(DeviceError, match="refused"):
            dev.set(volts, amps)
        assert dev.port.written == before


def test_the_limit_itself_is_allowed():
    with supply() as dev:
        dev.set(6.0, 3.0)
        assert changes(dev)[-2:] == ["VSET1:06.00", "ISET1:3.000"]


def test_the_config_can_lower_the_limit():
    with supply(max_v=5.0, max_a=2.0) as dev:
        dev.set(5.0, 2.0)
        with pytest.raises(DeviceError, match="refused"):
            dev.set(5.01, 1)
        with pytest.raises(DeviceError, match="refused"):
            dev.set(1, 2.01)


@pytest.mark.parametrize("cfg", [{"max_v": 12.01}, {"max_a": 3.01}, {"max_v": 48}, {"max_v": 0}, {"max_a": -1}])
def test_the_config_cannot_raise_the_limit(cfg):
    with pytest.raises(DeviceError, match="must be above 0"):
        supply(**cfg).open()


def test_output_is_off_at_open_and_at_close_by_default():
    dev = supply(sim={"on": True, "vset": 5.0})
    dev.open()
    port = dev.port
    assert changes(dev) == ["OUT0"] and port.on is False
    dev.output(True)
    assert port.on is True
    dev.close()
    assert port.on is False


def test_a_killed_run_is_cleaned_up_by_the_next_open():
    dev = supply(sim={"on": True, "vset": 5.0})  # as a killed run left it: close never ran
    with dev:
        assert dev.read()["output"] is False


def test_hold_output_never_changes_the_output_by_itself():
    dev = supply(hold_output=True, sim={"on": True, "vset": 6.0, "iset": 3.0})
    dev.open()
    port = dev.port
    reading = dev.info()
    dev.close()
    assert changes(dev) == [] and port.on is True
    assert reading["output"] is True and reading["hold_output"] is True
    assert reading["vout"] == 6.0 and reading["iout"] == 0.385 and reading["mode"] == "cv"


def test_hold_output_still_obeys_the_limit_and_an_explicit_call():
    with supply(hold_output=True, sim={"on": True, "vset": 6.0}) as dev:
        with pytest.raises(DeviceError, match="refused"):
            dev.set(12.5, 1)
        dev.output(False)
        assert dev.port.on is False


def test_settings_from_the_front_panel_are_not_trusted():
    with pytest.raises(DeviceError, match="front panel"):
        supply(sim={"vset": 12.5}).open()
    with pytest.raises(DeviceError, match="front panel"):
        supply(hold_output=True, sim={"vset": 5.0, "iset": 5.0}).open()


def test_output_on_checks_the_settings_first():
    with supply(max_v=5.0, sim={"vset": 4.0}) as dev:
        dev.port.vset = 6.0  # someone turned the knob while it was open
        with pytest.raises(DeviceError, match="front panel"):
            dev.output(True)
        assert dev.port.on is False


def test_a_different_unit_is_refused_before_anything_changes(monkeypatch):
    port = korad_module.SimKoradPort({"serial": "OTHER", "on": True, "vset": 5.0})
    dev = supply()
    monkeypatch.setattr(dev, "_make_port", lambda: port)
    with pytest.raises(DeviceError, match="lab.toml expects"):
        dev.open()
    assert port.on is True and not [c for c in port.written if not c.endswith("?")]


def test_a_setting_that_did_not_stick_is_an_error():
    with supply(sim={"ignore_set": True}) as dev:
        with pytest.raises(DeviceError, match="the supply says"):
            dev.set(5.0, 1.0)


def test_something_else_on_the_port_is_refused(monkeypatch):
    port = korad_module.SimKoradPort()
    port.write = lambda data: setattr(port, "reply", b"Hello from a modem")
    dev = supply()
    monkeypatch.setattr(dev, "_make_port", lambda: port)
    with pytest.raises(DeviceError, match="not a KA3005P"):
        dev.open()


def test_real_serial_path_against_a_pseudo_terminal():
    """pyserial and the quiet-line reply logic. A thread plays the supply, and answers without a line ending."""
    pytest.importorskip("serial")
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    fake = korad_module.SimKoradPort({"serial": "T-1", "on": True, "vset": 6.0})
    stop = threading.Event()

    def play():
        os.set_blocking(master, False)
        while not stop.is_set():
            try:
                data = os.read(master, 64)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            fake.write(data)
            reply = fake.read()
            if reply:
                os.write(master, reply)

    thread = threading.Thread(target=play, daemon=True)
    thread.start()
    inv = Inventory.from_dict({"devices": {"k": {"kind": "korad", "port": os.ttyname(slave), "serial": "T-1",
                                                 "hold_output": True}}}, "t")
    dev = make_device("k", inv, None, None)
    assert dev.backend == "hardware"
    try:
        with dev:
            info = dev.info()
            assert info["vout"] == 6.0 and info["output"] is True and info["serial"] == "T-1"
            with pytest.raises(DeviceError, match="refused"):
                dev.set(13, 1)
            dev.set(5.5, 2.0)
            assert (fake.vset, fake.iset) == (5.5, 2.0)
    finally:
        stop.set()
        thread.join(1)
    assert fake.on is True
