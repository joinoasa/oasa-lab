"""Acceptance of a Pluto+ clone, to run the day it arrives, inside the return window.

A clone can be wrong in many quiet ways: a plain crystal instead of the promised TCXO, a part that
only tunes 325 MHz to 3.8 GHz, one dead or weak RF port, one channel that does not exist, old or
odd firmware. Each test below is built to catch one of those. Limits and reasons: suite.toml.

Wiring (docs/hardware-suites.md, "Pluto+ acceptance"): TX1 to RX1 and TX2 to RX2 through 60 dB or
more of fixed attenuators, and TX1 also to the RTL-SDR through a splitter. The suite transmits only
after `--confirm-wiring pluto`. Every run starts the tone at very low power and steps up while it
watches the receiver, so a wrong cable is seen at the first step.
"""

import math
import re
import time

import pytest

from oasa_lab.devices import DeviceError, Unavailable
from oasa_lab.hw import Hardware, WiringRefused
from oasa_lab.plutotest import OFFSET_HZ, PLUTO_FS, RTL_FS, TooStrong, error_vs_rtl, loopback

FREQS_MHZ = (433.0, 915.0, 2400.0)
PPM_FREQS_MHZ = (433.0, 915.0)    # the RTL-SDR covers 500 kHz to 1.7 GHz; it cannot see 2400 MHz
TUNE_POINTS_HZ = (70e6, 433e6, 915e6, 2400e6, 6e9)


@pytest.fixture
def bench(lab, hw: Hardware):
    try:
        rf = hw.use_wiring()
        pluto, rtl = hw.first("pluto"), hw.first("rtlsdr")
    except (WiringRefused, Unavailable) as exc:   # not confirmed yet, or a tool/library is not installed
        pytest.skip(str(exc))
    loss = {p["name"]: p["loss_db"] for p in rf["paths"]}

    class Bench:
        def loop_loss(self, port: int) -> float:
            return loss[f"tx{port}-rx{port}"]
    b = Bench()
    b.pluto, b.rtl, b.hw = pluto, rtl, hw
    return b


def measure(lab, failures: list[str], metric: str, name: str, value: float, unit: str = "") -> None:
    if not lab.measure(metric, name, value, unit):
        failures.append(f"{name} = {value:.3g} {unit} is outside its limits (suite.toml, [limits.{metric}])")


def test_pluto_boots_and_reports_identity(lab, bench):
    info = bench.pluto.info()
    lab.log("pluto_identity", **{k: v for k, v in info.items() if k in ("model", "serial", "firmware", "channels", "uri", "backend")})
    assert re.search(r"pluto|ad936", info["model"], re.I), f"unexpected model {info['model']!r}"
    assert re.fullmatch(r"[0-9A-Za-z-]{6,64}", info["serial"]) and info["serial"] != "n/a", "no usable serial number"
    assert re.search(r"\d+\.\d+", info["firmware"]), f"firmware version {info['firmware']!r} does not look like a version"
    assert info["channels"] == 2, "the unit does not give 2 TX / 2 RX channels"


def test_tunes_across_70mhz_to_6ghz(lab, bench):
    failures: list[str] = []
    for lo in TUNE_POINTS_HZ:
        try:
            back = bench.pluto.tune(lo)
        except DeviceError as exc:
            failures.append(f"cannot tune to {lo / 1e6:g} MHz: {exc}")
            continue
        measure(lab, failures, "tune_error_hz", f"tune {lo / 1e6:g} MHz", back - lo, "Hz")
    assert not failures, "; ".join(failures)


def test_frequency_error_within_spec(lab, bench):
    """Pluto against the RTL-SDR: absolute crystal error in ppm, at two bands that both radios cover."""
    failures: list[str] = []
    ppm: dict[float, float] = {}
    for f_mhz in PPM_FREQS_MHZ:
        rf = f_mhz * 1e6
        try:
            r = error_vs_rtl(bench.pluto, bench.rtl, rf)
        except TooStrong as exc:
            pytest.fail(str(exc))
        ppm[f_mhz] = r.error_ppm
        name = f"{f_mhz:g} MHz"
        lab.sigmf(f"rtl-tone-{f_mhz:g}MHz", r.iq.data, datatype="cu8", sample_rate=RTL_FS, center_hz=rf,
                  start_time=time.time(), device=bench.rtl.id, purpose=f"ppm-vs-rtl {name}",
                  description=f"Pluto TX1 tone at LO + {OFFSET_HZ:g} Hz seen by the RTL-SDR; error {r.error_ppm:+.3f} ppm")
        lab.log("ppm_vs_rtl", freq_mhz=f_mhz, error_ppm=round(r.error_ppm, 4), tone_hz=round(r.tone["freq_hz"], 2),
                tone_snr_db=round(r.tone["snr_db"], 1), bin_hz=round(r.tone["bin_hz"], 2))
        measure(lab, failures, "rtl_tone_snr_db", f"RTL tone SNR {name}", r.tone["snr_db"], "dB")
        measure(lab, failures, "freq_error_ppm_vs_rtl", f"Pluto vs RTL-SDR {name}", r.error_ppm, "ppm")
        if r.pluto_ppm is not None:
            measure(lab, failures, "freq_error_ppm_vs_spec", f"Pluto crystal {name}", r.pluto_ppm, "ppm")
    lab.log("ppm_vs_rtl_note", note="2400 MHz is outside the RTL-SDR; the crystal is the same at every band",
            rtl_known_ppm=bench.rtl.cfg.get("known_ppm"))
    measure(lab, failures, "freq_error_ppm_band_spread", "433 vs 915 MHz", abs(ppm[433.0] - ppm[915.0]), "ppm")
    assert not failures, "; ".join(failures)


@pytest.mark.parametrize("f_mhz", FREQS_MHZ)
def test_every_port_loopback(lab, bench, f_mhz):
    """TX1 to RX1 and TX2 to RX2 over cable and attenuators with a tone: all four ports, one band."""
    rf = f_mhz * 1e6
    failures: list[str] = []
    levels: dict[int, float] = {}
    for port in (1, 2):
        name = f"{f_mhz:g} MHz TX{port}->RX{port}"
        try:
            lb = loopback(bench.pluto, port, rf, bench.loop_loss(port))
        except TooStrong as exc:
            pytest.fail(str(exc))
        for tag, iq in (("on", lb.on), ("off", lb.off)):
            lab.sigmf(f"pluto-{f_mhz:g}MHz-tx{port}-rx{port}-tone-{tag}", iq.data, datatype="cf32_le", sample_rate=PLUTO_FS,
                      center_hz=rf, start_time=time.time(), device=bench.pluto.id,
                      purpose=f"loopback {name} tone {tag}",
                      description=f"{name}, tone at LO + {OFFSET_HZ:g} Hz {'on' if tag == 'on' else 'muted'}")
        levels[port] = lb.tone["tone_dbfs"]
        lab.log("loopback", freq_mhz=f_mhz, port=port, level_dbfs=round(lb.tone["tone_dbfs"], 2),
                snr_db=round(lb.tone["snr_db"], 1), offset_error_ppm=round(lb.offset_error_ppm, 5),
                spur_snr_db=round(lb.spur["snr_db"], 1), expected_dbfs=lb.expected_dbfs, loss_db=bench.loop_loss(port))
        measure(lab, failures, "rx_level_dbfs", f"level {name}", lb.tone["tone_dbfs"], "dBFS")
        measure(lab, failures, "tone_snr_db", f"tone SNR {name}", lb.tone["snr_db"], "dB")
        measure(lab, failures, "loopback_offset_error_ppm", f"offset error {name}", lb.offset_error_ppm, "ppm")
        measure(lab, failures, "spur_snr_db_tx_off", f"spur with TX off {name}", lb.spur["snr_db"], "dB")
        if lb.expected_dbfs is not None:
            measure(lab, failures, "rx_level_vs_expected_db", f"level vs expected {name}", lb.tone["tone_dbfs"] - lb.expected_dbfs, "dB")
        else:
            lab.log("note", text=f"no calibration for {name}: the absolute level is not judged, only the ports against each other")
    normalized = {p: levels[p] + bench.loop_loss(p) for p in (1, 2)}  # take the cable's own loss out
    measure(lab, failures, "port_level_spread_db", f"port spread {f_mhz:g} MHz", abs(normalized[1] - normalized[2]), "dB")
    assert not failures, "; ".join(failures)


def test_power_cycle_recovers(lab, bench):
    """The hub can reboot the radio from a script (needed for unattended runs). Skipped without a hub."""
    if bench.pluto.hub is None or bench.pluto.hub_port is None:
        pytest.skip("no hub or no hub_port for the Pluto in lab.toml")
    seconds = bench.pluto.power_cycle()
    failures: list[str] = []
    measure(lab, failures, "power_cycle_s", "Pluto power cycle", seconds, "s")
    assert bench.pluto.info()["serial"] == bench.hw.inv.devices[bench.pluto.id].get("serial", bench.pluto.info()["serial"])
    assert not failures, "; ".join(failures)
