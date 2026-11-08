"""The live link's lab side (oasa_lab/livelink.py): its settings, the helper's command line and caps, and the
traffic tools that time each direction on one clock. No radios: the helper is never started here."""

import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from oasa_lab import livelink as ll
from oasa_lab.devices import Inventory, InventoryError, make_device


def dev(kind, **cfg):
    cfg = {"serial": "S", **cfg}
    if kind == "e310":
        cfg.setdefault("addr", "10.0.0.2")
    inv = Inventory.from_dict({"lab": {"backend": "sim"}, "devices": {"r": {"kind": kind, **cfg}}}, "t")
    return make_device("r", inv, None, None)


def test_defaults_are_the_bench_settings_that_met_every_target():
    c = ll.LiveConfig.from_dict({})
    assert c["e310_helper_args"] == ["--rt", "--lead-ms", "2.5", "--late-restart", "fast", "--sensor-s", "0"]
    assert c["b200_helper_args"][-2:] == ["--dev-extra", "recv_frame_size=1016"]
    assert ll.HW_BRIDGE == ["--tx-mode", "burst", "--symbol-rate", "250000", "--if-hz", "175000", "--decimate", "1",
                            "--coarse-step", "4"]
    assert c["rate_hz"] == 100 and c["bridge_rt_prio"] == 30 and c["duration_s"] <= 3500
    assert c["soak_duration_s"] == 86400, "the F11 soak is 24 hours"


@pytest.mark.parametrize("bad", [
    {"duraton_s": 60},                                  # a typo is refused, not ignored
    {"duration_s": 5}, {"soak_duration_s": 90001}, {"soak_duration_s": True}, {"rate_hz": 0}, {"b200_tx_gain": "40"}, {"bridge_rt_prio": 99},
    {"e310_cpus": "2; rm"}, {"b200_helper_args": "--rt"}, {"helper_sha256": "abc"},
    {"e310_helper_args": ["--tx-gain", "89"]},          # the lab sets and caps the gain, never the config
    {"b200_helper_args": ["--max-seconds=99999"]},
])
def test_bad_settings_are_refused(bad):
    with pytest.raises(ll.LiveConfigError):
        ll.LiveConfig.from_dict(bad)


def test_the_inventory_keeps_the_table_and_refuses_a_non_table():
    inv = Inventory.from_dict({"sdr_live": {"duration_s": 600}}, "t")
    assert inv.sdr_live == {"duration_s": 600}
    with pytest.raises(InventoryError):
        Inventory.from_dict({"sdr_live": "x"}, "t")


def test_only_the_pinned_helper_runs(tmp_path):
    h = tmp_path / "helper"
    h.write_text("#!/bin/sh\n")
    h.chmod(0o755)
    with pytest.raises(ll.LiveConfigError, match="not set"):
        ll.LiveConfig.from_dict({"helper": str(h)}).check_helper()
    with pytest.raises(ll.LiveConfigError, match="not the pinned"):
        ll.LiveConfig.from_dict({"helper": str(h), "helper_sha256": "0" * 64}).check_helper()
    good = ll.LiveConfig.from_dict({"helper": str(h), "helper_sha256": ll.sha256_file(h)})
    assert good.check_helper() == h
    with pytest.raises(ll.LiveConfigError, match="no \\[sdr_live\\] helper"):
        ll.LiveConfig.from_dict({}).check_helper()


def test_helper_command_takes_its_caps_from_the_device(tmp_path):
    e = dev("e310", max_tx_seconds=90000, subdev="A:0 A:1", channel=1)
    cmd = ll.helper_command(tmp_path / "h", e, tx=tmp_path / "e.tx", rx=tmp_path / "e.rx", tx_freq=920e6,
                            rx_freq=915e6, tx_gain=28, rx_gain=60, max_seconds=3120, extra=["--rt"])
    s = " ".join(cmd)
    assert "--radio e310" in s and "--device-args type=e3xx,addr=10.0.0.2" in s and "--tx-gain 28" in s
    assert cmd[cmd.index("--rx-subdev") + 1] == "A:0 A:1" and cmd[cmd.index("--tx-subdev") + 1] == "A:0"
    assert "--max-seconds 3120" in s and cmd[-1] == "--rt"
    with pytest.raises(ll.LiveConfigError, match="max_tx_gain"):
        ll.helper_command(tmp_path / "h", e, tx=tmp_path / "a", rx=tmp_path / "b", tx_freq=920e6, rx_freq=915e6,
                          tx_gain=41, rx_gain=60, max_seconds=60, extra=[])
    b = dev("b200")                                   # max_tx_seconds stays at its 60 s default
    with pytest.raises(ll.LiveConfigError, match="max_tx_seconds"):
        ll.helper_command(tmp_path / "h", b, tx=tmp_path / "a", rx=tmp_path / "b", tx_freq=915e6, rx_freq=920e6,
                          tx_gain=40, rx_gain=60, max_seconds=3120, extra=[])


def test_wrap_pins_and_prioritises_without_a_shell():
    assert ll.wrap(["x", "a b"], "2", 30) == ["taskset", "-c", "2", "chrt", "-f", "30", "x", "a b"]
    assert ll.wrap(["x"]) == ["x"]


def test_summary_percentiles_and_ten_minute_windows():
    vals = [5.0] * 990 + [9.0] * 9 + [19.0]
    times = [1000.0 + i for i in range(999)] + [1000.0 + 700]
    s = ll.summarize(vals, times, 1000.0)
    assert s["p50"] == 5.0 and s["p99"] == 9.0 and s["max"] == 19.0 and s["n"] == 1000
    assert s["window_max"] == [5.0, 19.0] and sum(s["hist"].values()) == 1000
    assert ll.summarize([])["p50"] is None


def test_the_result_reports_each_hour_from_the_fixed_tables():
    from array import array

    n, gap = 7300, 1.0                         # two full hours and 100 s of a third
    back = bytearray([1]) * n
    for k in (5, 6, 3700):                     # two lost in hour 0, one in hour 1
        back[k] = 0
    a2b = array("d", [5.0] * n)
    b2a = array("d", [4.0] * n)
    a2b[4000] = 19.0                           # hour 1's worst
    rtt = array("d", (x + y for x, y in zip(a2b, b2a)))
    t_rx = array("d", (1000.0 + k * gap for k in range(n)))
    r = ll.result(n, gap, 1000.0, back, a2b, b2a, rtt, t_rx)
    assert r["back"] == n - 3 and r["a2b"]["max"] == 19.0 and r["a2b"]["n"] == n - 3
    assert [h["sent"] for h in r["hours"]] == [3600, 3600, 100]
    assert [h["back"] for h in r["hours"]] == [3598, 3599, 100]
    assert r["hours"][0]["loss_pct"] == round(100 * 2 / 3600, 4) and r["hours"][2]["loss_pct"] == 0
    assert r["hours"][1]["a2b"] == {"p50": 5.0, "p999": 5.0, "max": 19.0} and r["hours"][0]["b2a"]["max"] == 4.0
    assert r["lost_first"] == [5, 6, 3700] and r["outliers"] == []
    json.dumps(r)                              # what ping writes


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_ping_and_echo_time_each_direction_on_one_clock(tmp_path):
    """A loopback 'link' that holds every datagram 3 ms one way and 1 ms the other: the tools must read 3 and 1, not
    2 and 2 (half the round trip). Also: one datagram dropped each way is counted where it was lost."""
    p_ping, p_echo, p_a, p_b = (_free_port() for _ in range(4))
    stop = threading.Event()

    def relay(listen, send, delay, drop_seq):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", listen))
        s.settimeout(0.05)
        while not stop.is_set():
            try:
                d, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            if ll.PKT.unpack_from(d)[0] == drop_seq:
                continue
            threading.Timer(delay, s.sendto, (d, ("127.0.0.1", send))).start()

    threading.Thread(target=relay, args=(p_a, p_echo, 0.003, 7), daemon=True).start()
    threading.Thread(target=relay, args=(p_b, p_ping, 0.001, 11), daemon=True).start()
    n = 60
    eo, po = tmp_path / "echo.json", tmp_path / "ping.json"
    echo = subprocess.Popen(ll.traffic_cmd("echo", f"127.0.0.1:{p_echo}", f"127.0.0.1:{p_b}", str(n), str(eo)))
    time.sleep(0.3)
    subprocess.run(ll.traffic_cmd("ping", f"127.0.0.1:{p_ping}", f"127.0.0.1:{p_a}", str(n), "0.01", str(po)),
                   check=True, timeout=30)
    echo.terminate()
    echo.wait(10)
    stop.set()
    pr, er = json.loads(po.read_text()), json.loads(eo.read_text())
    assert er["received"] == n - 1 and er["missing_first"] == [7]
    assert pr["back"] == n - 2 and pr["lost_first"] == [7, 11] and pr["bad"] == 0 and pr["duplicates"] == 0
    assert 2.9 <= pr["a2b"]["p50"] <= 4.5 and 0.9 <= pr["b2a"]["p50"] <= 2.5
