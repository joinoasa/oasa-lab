"""Transmit over cable between a B200mini and an E310, both ways. Limits and reasons: suite.toml.

Wiring (docs/hardware-suites.md, "USRP loopback"): B200mini TX/RX to E310 RX2-B and E310 TRX-A to
B200mini RX2, each through 60 dB or more of fixed attenuators. The suite transmits only after
`--confirm-wiring usrp`. Every direction starts at a low TX gain and steps up while watching the
receiver, so a wrong cable is seen at the first step.
"""

import time

import pytest

from oasa_lab.devices import Unavailable
from oasa_lab.hw import Hardware, WiringRefused
from oasa_lab.usrptest import OFFSET_HZ, RX_GAIN_DB, SAMPLE_RATE, TX_SCALE, TooStrong, loopback

RF_HZ = 915e6
OFFSETS: dict[str, float] = {}


@pytest.fixture
def radios(hw: Hardware):
    try:
        rf = hw.use_wiring()
        b200, e310 = hw.first("b200"), hw.first("e310")
    except (WiringRefused, Unavailable) as exc:   # not confirmed yet, or a tool is not installed
        pytest.skip(str(exc))
    return b200, e310, {p["name"]: p["loss_db"] for p in rf["paths"]}


def run_direction(lab, tx, rx, name: str, loss_db: float) -> None:
    try:
        lb = loopback(tx, rx, RF_HZ)
    except TooStrong as exc:
        pytest.fail(str(exc))
    for tag, iq in (("on", lb.on), ("off", lb.off)):
        lab.sigmf(f"{name}-tone-{tag}", iq.data, datatype=iq.datatype, sample_rate=SAMPLE_RATE, center_hz=RF_HZ,
                  start_time=getattr(iq, "start_time", time.time()), device=rx.id, purpose=f"loopback {name} tone {tag}",
                  description=f"{tx.id} {tx.tx_port} to {rx.id} {rx.rx_port}, tone at centre + {OFFSET_HZ:g} Hz, "
                              f"TX gain {lb.final_gain_db:g} dB, amplitude {TX_SCALE:g}, RX gain {RX_GAIN_DB:g} dB, "
                              f"{'on' if tag == 'on' else 'transmitter off'}")
    lab.log("loopback", direction=name, loss_db=loss_db, final_tx_gain_db=lb.final_gain_db,
            levels_dbfs={f"{g:g}": round(v, 2) for g, v in lb.levels.items()},
            tone_hz=round(lb.tone["freq_hz"], 1), snr_db=round(lb.tone["snr_db"], 1))
    OFFSETS[name] = lb.offset_ppm
    failures = []
    for metric, label, value, unit in (
            ("rx_level_dbfs", f"level {name}", round(lb.tone["tone_dbfs"], 2), "dBFS"),
            ("tone_snr_db", f"tone SNR {name}", round(lb.tone["snr_db"], 1), "dB"),
            ("tx_gain_slope_db_per_db", f"gain slope {name}", round(lb.slope_db_per_db, 3), "dB/dB"),
            ("radio_offset_ppm", f"offset {name}", round(lb.offset_ppm, 4), "ppm"),
            ("tx_off_snr_db", f"after TX off {name}", round(lb.spur["snr_db"], 1), "dB"),
            ("tx_underflows", f"underflows {name}", lb.underflows, "")):
        if not lab.measure(metric, label, value, unit):
            failures.append(f"{label} = {value} {unit} is outside [limits.{metric}]")
    assert not failures, "; ".join(failures)


def test_b200_to_e310(lab, radios):
    b200, e310, loss = radios
    run_direction(lab, b200, e310, "b200-e310", loss["b200-e310"])


def test_e310_to_b200(lab, radios):
    b200, e310, loss = radios
    run_direction(lab, e310, b200, "e310-b200", loss["e310-b200"])


def test_offsets_mirror(lab, radios):
    if len(OFFSETS) < 2:
        pytest.fail(f"needs both directions measured, have {sorted(OFFSETS)}")
    total = OFFSETS["b200-e310"] + OFFSETS["e310-b200"]
    assert lab.measure("offset_mirror_ppm", "forward + reverse offset", round(total, 4), "ppm"), f"{total:+.3f} ppm"
