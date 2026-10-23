"""Measurements for the Pluto acceptance suite, kept apart from the pytest file so the lab's own tests
can run them against a deliberately bad simulated unit.

The Pluto's TX and RX share one crystal, so a TX-to-RX loopback cannot show the crystal's error: it
cancels. The absolute error is measured by the RTL-SDR (a second, independent TCXO).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .devices import Pluto, RtlSdr
from .iq import IQ, tone_report

OFFSET_HZ = 100_000.0       # the tone sits this far from the LO, clear of the DC spike
TX_GAIN_DB = -10.0          # final TX gain; with 60 dB or more of cable the receiver sees about -70 dBm
TX_SCALE = 0.5              # digital amplitude of the tone (-6 dBFS)
RX_GAIN_DB = 30.0           # Pluto manual RX gain
PLUTO_FS = 2.0e6
PLUTO_N = 16384
RTL_FS = 1.024e6
RTL_N = 65536
RTL_GAIN_DB = 30.0
RAMP_DB = (-50.0, -40.0, -30.0, -20.0)   # TX gain steps before the final one
MAX_PROBE_DBFS = -10.0      # a probe tone this strong means the cable is wrong: stop before raising power
RAMP_SAMPLES = 4096


class TooStrong(Exception):
    """The receiver saw far more than a 60 dB cable should give. TX is off again when this is raised."""


def ramp_tone(pluto: Pluto, port: int, rf_hz: float) -> None:
    """Start the tone at low power and step up, checking the receiver at each step. A wrong cable shows
    at the first step, long before the power could hurt anything."""
    for g in (*RAMP_DB, TX_GAIN_DB):
        pluto.tx_tone(port, rf_hz, OFFSET_HZ, g, TX_SCALE, PLUTO_FS)
        if g == TX_GAIN_DB:
            return
        rep = tone_report(pluto.capture(port, rf_hz, PLUTO_FS, RAMP_SAMPLES, RX_GAIN_DB))
        if rep["tone_dbfs"] > MAX_PROBE_DBFS:
            pluto.tx_off()
            raise TooStrong(f"at TX gain {g:g} dB the probe tone reads {rep['tone_dbfs']:.1f} dBFS on RX{port}; "
                            f"check the cable and attenuators of TX{port} to RX{port}")


@dataclass
class Loopback:
    port: int
    rf_hz: float
    on: IQ
    off: IQ
    tone: dict[str, Any]       # tone_report with the TX on
    spur: dict[str, Any]       # tone_report with the TX off (the strongest line left)
    offset_error_ppm: float    # how far the tone is from where it should be; ~0 with one crystal
    expected_dbfs: float | None


def loopback(pluto: Pluto, port: int, rf_hz: float, loss_db: float) -> Loopback:
    try:
        ramp_tone(pluto, port, rf_hz)
        on = pluto.capture(port, rf_hz, PLUTO_FS, PLUTO_N, RX_GAIN_DB)
    finally:
        pluto.tx_off()
    off = pluto.capture(port, rf_hz, PLUTO_FS, PLUTO_N, RX_GAIN_DB)
    tone, spur = tone_report(on), tone_report(off)
    return Loopback(port, rf_hz, on, off, tone, spur, (tone["freq_hz"] - OFFSET_HZ) / rf_hz * 1e6,
                    pluto.expected_rx_dbfs(port, rf_hz, TX_GAIN_DB, TX_SCALE, RX_GAIN_DB, loss_db))


@dataclass
class VsRtl:
    rf_hz: float
    iq: IQ
    tone: dict[str, Any]
    error_ppm: float           # Pluto minus RTL-SDR: (Pluto error) - (RTL-SDR error)
    pluto_ppm: float | None = None   # the Pluto's own error, when the RTL-SDR's is known (lab.toml known_ppm)


def error_vs_rtl(pluto: Pluto, rtl: RtlSdr, rf_hz: float, port: int = 1) -> VsRtl:
    """The Pluto sends a tone; the RTL-SDR, tuned to the same nominal frequency, measures where it lands."""
    try:
        ramp_tone(pluto, port, rf_hz)
        iq = rtl.capture(rf_hz, RTL_FS, RTL_N, RTL_GAIN_DB)
    finally:
        pluto.tx_off()
    tone = tone_report(iq)
    err = (tone["freq_hz"] - OFFSET_HZ) / rf_hz * 1e6
    known = rtl.cfg.get("known_ppm")
    return VsRtl(rf_hz, iq, tone, err, err + float(known) if known is not None else None)
