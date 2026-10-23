"""The real (non-simulated) driver code, run against stand-ins: fake command-line tools on PATH and a fake
`adi` module. This checks our own logic (commands, parsing, start and stop, error paths). It cannot
check the real tools or the real hardware; that is the first run on the bench."""

import math
import os
import stat
import sys
import textwrap
import time

import pytest

from oasa_lab.devices import DeviceError, Inventory, Unavailable, make_device
from oasa_lab.devices.hub import UsbHub

FAKE_RTL_TEST = '''#!/usr/bin/env python3
import sys, time
sys.stderr.write("Found 1 device(s):\\n  0:  RTLSDRBlog, Blog V4, SN: 00000001\\n\\nUsing device 0: Generic RTL2832U OEM\\n"
                 "Found Rafael Micro R828D tuner\\nRTL-SDR Blog V4 Detected\\n")
sys.stderr.flush()
time.sleep(30)
'''

FAKE_RTL_SDR = '''#!/usr/bin/env python3
import signal, sys, time
args = sys.argv[1:]
out = args[-1]
n = int(args[args.index("-n") + 1]) if "-n" in args else None
stop = []
signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
with open(out, "wb") as fh:
    sent = 0
    while not stop:
        block = bytes([127, 128]) * 131072          # one rtl_sdr sized block (262144 bytes)
        if n is not None:
            block = block[: min(len(block), 2 * (n - sent))]
        fh.write(block); fh.flush()
        sent += len(block) // 2
        if n is not None and sent >= n:
            break
        time.sleep(0.2)
'''

FAKE_YKUSHCMD = '''#!/usr/bin/env python3
import os, sys
state = os.environ["FAKE_HUB_STATE"]
args = sys.argv[1:]
port = args[-1]
flag = args[-2]
if flag == "-g":
    print("Downstream port %s is: %s" % (port, "OFF" if os.path.exists(state + port) else "ON"))
elif flag == "-d":
    open(state + port, "w").close()
elif flag == "-u":
    os.path.exists(state + port) and os.remove(state + port)
with open(state + "log", "a") as fh:
    fh.write(" ".join(args) + "\\n")
'''


def install(tmp_path, monkeypatch, **tools):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for name, text in tools.items():
        p = d / name
        p.write_text(text)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")


def test_rtlsdr_real_backend_with_fake_tools(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch, rtl_test=FAKE_RTL_TEST, rtl_sdr=FAKE_RTL_SDR)
    inv = Inventory.from_dict({"devices": {"r": {"kind": "rtlsdr", "serial": "00000001"}}}, "t")
    with make_device("r", inv, None, None) as rtl:
        info = rtl.info()
        assert info["serial"] == "00000001" and info["blog_v4"] and "R828D" in info["tuner"]
        iq = rtl.capture(915e6, 250000, 1000, 20)           # a short capture, exactly 1000 samples
        assert iq.n == 1000 and iq.datatype == "cu8"
        t_before = time.time()
        rtl.start_recording("x", 915e6, 250000, 20)
        time.sleep(0.5)
        rec = rtl.stop_recording()
        assert rec.path.stat().st_size >= 262144 and rec.datatype == "cu8"
        assert rec.seconds * 250000 * 2 == rec.path.stat().st_size
        # the start time is the time of the first block minus its age: before "now", after "before"
        assert t_before - 1.0 < rec.start_time < time.time()
    bad = Inventory.from_dict({"devices": {"r": {"kind": "rtlsdr", "serial": "99999999"}}}, "t")
    with pytest.raises(DeviceError, match="lab.toml says"):
        make_device("r", bad, None, None).open()


def test_hub_real_backend_with_fake_ykushcmd(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HUB_STATE", str(tmp_path / "hub-"))
    install(tmp_path, monkeypatch, ykushcmd=FAKE_YKUSHCMD)
    hub = UsbHub({"kind": "ykush3", "serial": "YK3R0001"})
    assert hub.is_on(2)
    hub.set_power(2, False)
    assert not hub.is_on(2)
    hub.power_cycle(2, off_s=0.01)
    assert hub.is_on(2)
    log = (tmp_path / "hub-log").read_text()
    assert "-s YK3R0001 -d 2" in log and "-s YK3R0001 -u 2" in log


def test_hub_without_its_tool_says_so(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(Unavailable, match="ykushcmd"):
        UsbHub({"kind": "ykush3", "serial": "x"}).is_on(1)


class FakeAd9361:
    """Just enough of pyadi-iio's ad9361 for the driver's calls."""
    last = None

    def __init__(self, uri):
        if "bad" in uri:
            raise OSError("no such device")
        FakeAd9361.last = self
        self.uri = uri
        self.ctx = type("Ctx", (), {"attrs": {"hw_model": "Analog Devices PlutoSDR Rev.C (Z7010-AD9363A)",
                                              "hw_serial": "104400abcdef", "fw_version": "v0.38"}})()
        self.dds = {}
        self.tx_hardwaregain_chan0 = self.tx_hardwaregain_chan1 = -89.0
        self._rx_enabled = [0]
        self.rx_buffer_size = 1024

    @property
    def rx_enabled_channels(self):
        return self._rx_enabled

    @rx_enabled_channels.setter
    def rx_enabled_channels(self, v):
        if len(v) > 1 and getattr(self, "one_channel", False):
            raise ValueError("one channel only")
        self._rx_enabled = v

    def dds_single_tone(self, frequency, scale, channel=0):
        self.dds[channel] = (frequency, scale)

    def rx_destroy_buffer(self):
        pass

    def rx(self):
        n = self.rx_buffer_size
        return [complex(0.1 * math.cos(0.3 * k), 0.1 * math.sin(0.3 * k)) * 2048 for k in range(n)]


def test_pluto_real_backend_with_a_fake_adi(monkeypatch):
    mod = type(sys)("adi")
    mod.ad9361 = FakeAd9361
    monkeypatch.setitem(sys.modules, "adi", mod)
    inv = Inventory.from_dict({"devices": {"p": {"kind": "pluto", "uri": "ip:1.2.3.4", "serial": "104400abcdef"}}}, "t")
    with make_device("p", inv, None, None) as p:
        info = p.info()
        assert info["serial"] == "104400abcdef" and info["firmware"] == "v0.38" and info["channels"] == 2
        assert "Pluto" in info["model"]
        sdr = FakeAd9361.last
        assert sdr.dds[0][1] == 0.0 and sdr.dds[1][1] == 0.0       # opened with both transmitters muted
        p.tx_tone(2, 915e6, 1e5, -10.0, 0.5, 2e6)
        assert sdr.dds[1] == (100000, 0.5) and sdr.dds[0][1] == 0.0 and sdr.tx_hardwaregain_chan1 == -10.0
        assert sdr.tx_hardwaregain_chan0 == -89.0                   # the other port stays muted
        iq = p.capture(2, 915e6, 2e6, 512, 30.0)
        assert iq.n == 512 and abs(iq.samples(1)[0] - 0.1) < 1e-6   # counts scaled to full scale 1.0
    assert sdr.dds[1][1] == 0.0, "closing the device must mute the transmitter"
    # wrong serial, missing device, one-channel unit
    bad_serial = Inventory.from_dict({"devices": {"p": {"kind": "pluto", "uri": "ip:1.2.3.4", "serial": "other"}}}, "t")
    with pytest.raises(DeviceError, match="lab.toml says"):
        make_device("p", bad_serial, None, None).open()
    gone = Inventory.from_dict({"devices": {"p": {"kind": "pluto", "uri": "ip:bad"}}}, "t")
    with pytest.raises(DeviceError, match="cannot open"):
        make_device("p", gone, None, None).open()
    FakeAd9361.one_channel = True
    try:
        with make_device("p", inv, None, None) as p:
            assert p.info()["channels"] == 1
    finally:
        del FakeAd9361.one_channel
