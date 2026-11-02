"""The B200mini and E310 drivers: receive only, UHD only as a separate process, and no quiet sample loss.
The real backend runs against fake UHD tools on PATH. That checks our logic, not UHD."""

import ast
import os
import stat
import textwrap
from pathlib import Path

import pytest

from oasa_lab.devices import DeviceError, Inventory, Unavailable, make_device
from oasa_lab.devices import usrp as usrp_module
from oasa_lab.devices.inventory import DEFAULT_SIM
from oasa_lab.iq import IQ

B200_PROBE = """
|   |       Mboard: B200mini
|   |   serial: FAKE200
|   |   name: B200mini
|   |   FW Version: 8.0
|   |   FPGA Version: 7.0
"""
E310_PROBE = """
|   |       Mboard: ni-e31x-FAKE310
|   |   dboard_0_serial: 9999999
|   |   mender_artifact: v4.6.0.0_e310_sg3
|   |   serial: FAKE310
|   |   MPM Version: 5.3
|   |   FPGA Version: 6.1
"""


def tool(folder, name, body):
    path = folder / name
    path.write_text("#!/usr/bin/env python3\nimport os, sys\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def uhd(tmp_path, monkeypatch):
    """Fake UHD tools. FAKE_RX chooses how the receive tool behaves, and every call is logged."""
    log = tmp_path / "calls"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_B200", B200_PROBE)
    monkeypatch.setenv("FAKE_E310", E310_PROBE)
    monkeypatch.setenv("FAKE_RX", "good")
    logline = 'open(os.environ["FAKE_LOG"], "a").write(" ".join(sys.argv) + "\\n")\n'
    tool(tmp_path, "uhd_usrp_probe", logline + 'print(os.environ["FAKE_E310" if "e3xx" in sys.argv[2] else "FAKE_B200"])\n')
    tool(tmp_path, "uhd_config_info", 'print("UHD 4.6.0.0+fake")\n')
    tool(tmp_path, "rx_samples_to_file", logline + textwrap.dedent('''
        a = sys.argv
        n = int(a[a.index("--nsamps") + 1])
        mode = os.environ["FAKE_RX"]
        if mode == "fail":
            print("Error: no device"); sys.exit(1)
        if mode == "short":
            n //= 2
        open(a[a.index("--file") + 1], "wb").write(bytes(4 * n))
        print({"overflow": "Got an overflow indication.", "letters": "OOO"}.get(mode, "Done!"))
    '''))
    tool(tmp_path, "rx_samples_c", logline + textwrap.dedent('''
        a = sys.argv
        n = int(a[a.index("-n") + 1])
        mode = os.environ["FAKE_RX"]
        if mode == "fail":
            print("Error: no device"); sys.exit(1)
        if mode == "short":
            n //= 2
        open(a[a.index("-o") + 1], "wb").write(bytes(8 * n))
        print({"overflow": "Got an overflow indication.", "letters": "OOO"}.get(mode, "Success"))
    '''))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return log


def radio(kind="b200", **cfg):
    cfg = {"serial": "FAKE200" if kind == "b200" else "FAKE310", **cfg}
    if kind == "e310":
        cfg.setdefault("addr", "10.0.0.2")
    inv = Inventory.from_dict({"devices": {"r": {"kind": kind, **cfg}}}, "test")
    return make_device("r", inv, None, None)


def sim(kind="b200", **cfg):
    inv = Inventory.from_dict({"lab": {"backend": "sim"}, "devices": {"r": {"kind": kind, "serial": "S", "addr": "x", **cfg}}}, "t")
    return make_device("r", inv, None, None)


@pytest.mark.parametrize("dev_id,kind", [("b2000", "b200"), ("e3100", "e310")])
def test_they_open_on_the_built_in_sim_bench(dev_id, kind):
    inv = Inventory.from_dict(DEFAULT_SIM, "test", force_sim=True)
    assert inv.has(kind)
    with make_device(dev_id, inv, None, None) as dev:
        info = dev.info()
        iq = dev.capture(915e6, 1e6, 4096, gain_db=40)
    assert info["kind"] == kind and info["backend"] == "sim" and info["receive_only"] is True
    assert isinstance(iq, IQ) and iq.n == 4096 and iq.datatype == {"b200": "cf32_le", "e310": "ci16_le"}[kind]


def test_simulated_noise_follows_the_gain():
    def level(gain):
        with sim() as dev:
            s = dev.capture(915e6, 1e6, 4096, gain_db=gain).samples()
        return sum(abs(x) ** 2 for x in s) / len(s)

    assert level(40) > 50 * level(20)


def test_ci16_scale_is_full_scale_at_one():
    assert IQ(b"\x00\x80\x00\x40", "ci16_le", 1, 1).samples() == [complex(-1.0, 0.5)]


@pytest.mark.parametrize("kind", ["b200", "e310"])
def test_there_is_no_transmit_path(kind):
    names = {n for n in dir(radio(kind)) if any(w in n.lower() for w in ("tx", "transmit", "send"))}
    assert names <= {"require_tx", "tx_gate"}


def test_uhd_is_never_imported_and_only_rx_tools_run():
    source = Path(usrp_module.__file__).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("uhd") for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("uhd")
    for word in ("tx_waveforms", "tx_samples_from_file", "benchmark_rate"):
        assert word not in source


def test_identity_and_versions_are_read(uhd):
    with radio("b200") as dev:
        info = dev.info()
    assert (info["serial"], info["firmware"], info["uhd"]) == ("FAKE200", "fw 8.0, fpga 7.0", "4.6.0.0+fake")
    with radio("e310") as dev:
        info = dev.info()
    assert (info["serial"], info["firmware"], info["image"]) == ("FAKE310", "mpm 5.3, fpga 6.1", "v4.6.0.0_e310_sg3")
    assert "type=e3xx,addr=10.0.0.2" in uhd.read_text() and "type=b200,serial=FAKE200" in uhd.read_text()


@pytest.mark.parametrize("kind", ["b200", "e310"])
def test_a_different_unit_is_refused(uhd, kind):
    with pytest.raises(DeviceError, match="lab.toml expects"):
        radio(kind, serial="OTHER").open()


def test_the_e310_needs_an_address(uhd):
    inv = Inventory.from_dict({"devices": {"r": {"kind": "e310", "serial": "X"}}}, "t")
    with pytest.raises(DeviceError, match="needs addr"):
        make_device("r", inv, None, None).open()


def test_missing_tools_are_unavailable(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(usrp_module, "EXAMPLE_DIRS", ())
    with pytest.raises(Unavailable):
        radio().open()


def test_a_capture_runs_the_receive_tool_with_the_right_settings(uhd):
    with radio("e310", subdev="A:0 A:1", channel=1) as dev:
        iq = dev.capture(915e6, 1e6, 1000, gain_db=40)
    line = uhd.read_text().splitlines()[-1]
    assert iq.n == 1000 and iq.sample_rate == 1e6 and iq.center_hz == 915e6 and iq.start_time > 0
    for part in ("--nsamps 1000", "--rate 1000000", "--freq 915000000", "--gain 40", "--ant RX2", "--channels 1",
                 "--type short", "A:0 A:1"):
        assert part in line


def test_the_b200_uses_the_tool_that_can_set_its_gain(uhd):
    with radio("b200") as dev:
        iq = dev.capture(915e6, 1e6, 1000, gain_db=40)
    line = uhd.read_text().splitlines()[-1]
    assert iq.n == 1000 and iq.datatype == "cf32_le" and "rx_samples_c" in line
    for part in ("-a type=b200,serial=FAKE200", "-f 915000000", "-r 1000000", "-g 40", "-n 1000"):
        assert part in line


def test_stray_characters_after_a_probe_field_are_not_part_of_it(uhd, monkeypatch):
    monkeypatch.setenv("FAKE_E310", E310_PROBE.replace("serial: FAKE310", "serial: FAKE310\u00a0"))
    with radio("e310") as dev:
        assert dev.info()["serial"] == "FAKE310"


@pytest.mark.parametrize("kind", ["b200", "e310"])
@pytest.mark.parametrize("mode,message", [("overflow", "samples were lost"), ("letters", "samples were lost"),
                                          ("short", "asked for 1000 samples, got 500"), ("fail", "no device")])
def test_lost_samples_are_an_error(uhd, monkeypatch, kind, mode, message):
    monkeypatch.setenv("FAKE_RX", mode)
    with radio(kind) as dev:
        with pytest.raises(DeviceError, match=message):
            dev.capture(915e6, 1e6, 1000)


def test_rate_and_gain_limits_never_reach_the_tool(uhd):
    with radio("e310") as dev:
        for fs, gain in ((1.1e6, 40), (0, 40), (1e6, 61), (1e6, -1)):
            with pytest.raises(DeviceError, match="refused"):
                dev.capture(915e6, fs, 1000, gain_db=gain)
    assert "rx_samples" not in uhd.read_text()


def test_lab_toml_can_raise_the_rate_once_proven(uhd):
    with radio("e310", max_rx_rate=4e6) as dev:
        assert dev.capture(915e6, 4e6, 1000).n == 1000
