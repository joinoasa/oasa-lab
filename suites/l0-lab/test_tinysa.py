"""The tinySA reader: the simulated console, its refusals, and the real serial path against a pseudo-terminal."""

import os
import threading
import time

import pytest

from oasa_lab.devices import DeviceError, Inventory, TinySA, make_device
from oasa_lab.devices import tinysa as tinysa_module
from oasa_lab.devices.inventory import DEFAULT_SIM


def analyzer(**cfg):
    sim = cfg.pop("sim", {})
    dev = {"kind": "tinysa", "port": "sim:t", "serial": "SIM-TINYSA-0001", **cfg, "sim": sim}
    inv = Inventory.from_dict({"lab": {"backend": "sim"}, "devices": {"t": dev}}, "test")
    return make_device("t", inv, None, None)


def test_it_opens_on_the_built_in_sim_bench():
    inv = Inventory.from_dict(DEFAULT_SIM, "test", force_sim=True)
    assert inv.has("tinysa")
    with make_device("tinysa0", inv, None, None) as dev:
        info = dev.info()
    assert isinstance(dev, TinySA)
    assert (info["kind"], info["backend"], info["model"], info["serial"]) == \
        ("tinysa", "sim", "tinySA Ultra+", "SIM-TINYSA-0001")
    assert info["firmware"].startswith("tinySA") and info["commands_confirmed"] is False


def test_peak_reading_adds_the_pad():
    with analyzer(input_pad_db=30.0, sim={"tone_hz": 915e6, "tone_dbm": -42.0, "noise_db": 0.0}) as dev:
        f, dbm = dev.read_peak_dbm(915e6, span_hz=1e6, points=101)
        assert f == pytest.approx(915e6, abs=10e3) and dbm == pytest.approx(-12.0)
        f2, dbm2 = dev.read_marker_dbm()
        assert (f2, dbm2) == (pytest.approx(f), pytest.approx(dbm))
        assert dev.port.written[-1] == "marker 1" and dev.port.written[-3].startswith("scan ")


def test_the_tinysa_never_transmits():
    with analyzer() as dev:
        dev.tx_gate = lambda: "no transmit in this test"
        dev.read_peak_dbm(915e6)
        assert not any(w.startswith(("output", "generate", "mode high output", "mode low output"))
                       for w in dev.port.written)


def test_tinysa_refuses_a_level_near_its_maximum_input():
    with analyzer(max_input_dbm=0.0, sim={"tone_dbm": -2.0, "noise_db": 0.0}) as dev:
        with pytest.raises(DeviceError, match="Remove the signal"):
            dev.read_peak_dbm(915e6)


@pytest.mark.parametrize("start,stop,points", [(0, 1e6, 101), (2e6, 1e6, 101), (1e6, 2e6, 1), (1e6, 2e6, 10_000)])
def test_a_silly_scan_is_refused(start, stop, points):
    with analyzer() as dev:
        with pytest.raises(DeviceError, match="not sane"):
            dev.scan(start, stop, points)


def test_something_that_is_not_a_tinysa_is_refused():
    with pytest.raises(DeviceError, match="not a tinySA"):
        analyzer(sim={"version": "NanoVNA-H 1.2"}).open()


def test_real_serial_path_against_a_pseudo_terminal():
    """pyserial, the echo and the prompt. A thread plays the console."""
    pytest.importorskip("serial")
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    fake = tinysa_module.SimTinySAPort({"tone_hz": 433.92e6, "tone_dbm": -50.0, "noise_db": 0.0})
    stop = threading.Event()

    def play():
        os.set_blocking(master, False)
        buf = b""
        while not stop.is_set():
            try:
                buf += os.read(master, 4096)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            while b"\r" in buf:
                line, _, buf = buf.partition(b"\r")
                fake.write(line)
                os.write(master, fake.read())

    thread = threading.Thread(target=play, daemon=True)
    thread.start()
    inv = Inventory.from_dict({"devices": {"t": {"kind": "tinysa", "port": os.ttyname(slave), "serial": "T-1"}}}, "t")
    dev = make_device("t", inv, None, None)
    try:
        with dev:
            assert dev.backend == "hardware"
            f, dbm = dev.read_peak_dbm(433.92e6, span_hz=200e3, points=51)
            assert dbm == pytest.approx(-20.0) and dev.info()["serial"] == "T-1"
    finally:
        stop.set()
        thread.join(1)
        os.close(slave)
        os.close(master)
