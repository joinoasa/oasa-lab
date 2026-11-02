"""Receive only: is each radio alive, and is its input quiet? Nothing here transmits, and the supply is only read.

A capture that loses samples fails on its own, in the driver. Limits and reasons: suite.toml.
"""

import math

import pytest

from oasa_lab.devices import Unavailable
from oasa_lab.hw import Hardware

CENTER_HZ = 915e6
SAMPLE_RATE = 1e6
SAMPLES = 200_000
GAIN_DB = 40.0
MEASURE_SAMPLES = 16_384


def mean_dbfs(iq) -> float:
    """From the second half, so the radio's start-up settling is left out."""
    samples = iq.samples(MEASURE_SAMPLES, start=iq.n // 2)
    return 10 * math.log10(sum(abs(s) ** 2 for s in samples) / len(samples) + 1e-20)


def noise_floor(lab, hw: Hardware, kind: str) -> None:
    try:
        radio = hw.first(kind)
    except Unavailable as exc:
        pytest.skip(str(exc))
    iq = radio.capture(CENTER_HZ, SAMPLE_RATE, SAMPLES, gain_db=GAIN_DB)
    assert iq.n == SAMPLES
    dbfs = mean_dbfs(iq)
    lab.sigmf(f"{kind}-noise", iq.data, datatype=iq.datatype, sample_rate=iq.sample_rate, center_hz=iq.center_hz,
              start_time=iq.start_time, device=radio.id, purpose="noise floor",
              description=f"{kind} noise floor, gain {GAIN_DB:g} dB")
    assert lab.measure("rx_noise_dbfs", f"{kind} noise floor", round(dbfs, 2), "dBFS"), f"{kind}: {dbfs:.1f} dBFS"


def test_b200_noise_floor(lab, hw: Hardware):
    noise_floor(lab, hw, "b200")


def test_e310_noise_floor(lab, hw: Hardware):
    noise_floor(lab, hw, "e310")


def test_e310_power_draw(lab, hw: Hardware):
    supply = hw.first("korad")
    reading = supply.read()
    assert reading["output"], "the supply's output is off, so the E310 is off"
    watts = round(reading["vout"] * reading["iout"], 3)
    ok_v = lab.measure("e310_supply_v", "E310 supply voltage", reading["vout"], "V")
    ok_w = lab.measure("e310_power_w", "E310 power draw", watts, "W")
    assert ok_v and ok_w, f"{reading['vout']} V, {reading['iout']} A"
