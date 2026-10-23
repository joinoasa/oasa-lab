"""Pluto+ (AD9363, 2 TX / 2 RX) through pyadi-iio.

pyadi-iio (Analog Devices' own license: BSD-3-style plus "use only with Analog Devices parts")
and its libiio binding (pylibiio, LGPL-2.1+) are an optional extra, `pip install 'oasa-lab[hw]'`,
imported only when a real Pluto is opened. They are test tools, never shipped. Ports are numbered
1 and 2: TX1/RX1 and TX2/RX2.

NOT YET RUN ON REAL HARDWARE. The calls follow the pyadi-iio documentation; the first run on the
bench may need small fixes (see docs/hardware-suites.md, "First run").
"""

from __future__ import annotations

import cmath
import math
import time
from typing import Any

from ..iq import IQ, cf32_bytes
from .base import Device, DeviceError, Unavailable
from .sim import SimWorld

FULL_SCALE_COUNTS = 2048.0   # the AD9361 gives 12-bit samples; 1.0 = +-2048 counts (est.)
TX_FULL_SCALE_DBM = 7.0      # AD9363 TX output at 0 dB gain, low bands (est., datasheet "typ")
SIM_RX_K_DB = 20.0           # simulated receive chain: dBFS = dBm + gain + K (arbitrary but fixed)


def tx_reference_dbm(rf_hz: float) -> float:
    """Output power of a full-scale tone at 0 dB TX gain. A rough fall-off with frequency (est.)."""
    return TX_FULL_SCALE_DBM - 1.0 * (rf_hz / 1e9)


class Pluto(Device):
    kind = "pluto"

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._sdr: Any = None
        self.channels = int(self.cfg.get("channels", 2))
        self._attrs: dict[str, str] = {}
        self._sim = dict(self.cfg.get("sim") or {})
        self._lo = 915e6
        self._tx_scale = 0.0

    # --- common ------------------------------------------------------------------------------------
    def _open(self) -> None:
        if self.backend == "sim":
            if not isinstance(self.world, SimWorld):
                raise Unavailable(f"{self.id}: the simulated backend needs a SimWorld")
            self.channels = int(self._sim.get("channels", self.channels))
            return
        try:
            import adi  # type: ignore
        except ImportError:
            raise Unavailable("pyadi-iio is not installed (pip install 'oasa-lab[hw]')") from None
        try:
            self._sdr = adi.ad9361(uri=str(self.cfg["uri"]))
        except Exception as exc:  # the library raises plain OSError / Exception on a missing device
            raise DeviceError(f"{self.id}: cannot open {self.cfg.get('uri')}: {exc}") from None
        ctx_attrs = getattr(getattr(self._sdr, "ctx", None), "attrs", {}) or {}
        self._attrs = {str(k): str(v) for k, v in dict(ctx_attrs).items()}
        want = self.cfg.get("serial")
        got = self._attrs.get("hw_serial")
        if want and got and str(want) != got:
            self.close_quietly()
            raise DeviceError(f"{self.id}: serial {got}, lab.toml says {want}")
        try:  # a one-channel unit refuses the second channel
            self._sdr.rx_enabled_channels = [0, 1]
            self._sdr.rx_enabled_channels = [0]
            self.channels = 2
        except Exception:
            self.channels = 1
        self.tx_off()

    def close_quietly(self) -> None:
        try:
            self._close()
        except Exception:
            pass

    def _close(self) -> None:
        try:
            self.tx_off()
        finally:
            if self._sdr is not None:
                for fn in ("rx_destroy_buffer", "tx_destroy_buffer"):
                    try:
                        getattr(self._sdr, fn)()
                    except Exception:
                        pass
                self._sdr = None

    def _info(self) -> dict[str, Any]:
        if self.backend == "sim":
            return {"model": "Pluto+ AD9363 (simulated)", "serial": self.cfg.get("serial", "SIM-PLUTO"),
                    "firmware": self._sim.get("firmware", "v0.38"), "channels": self.channels,
                    "uri": self.cfg.get("uri"), "tcxo_ppm_spec": 0.5}
        a = self._attrs
        return {"model": a.get("hw_model") or a.get("hw_carrier") or "Pluto", "serial": a.get("hw_serial") or self.cfg.get("serial"),
                "firmware": a.get("fw_version"), "channels": self.channels, "uri": self.cfg.get("uri"),
                "xo_correction": a.get("ad9361-phy,xo_correction"), "tcxo_ppm_spec": 0.5}

    # --- radio -----------------------------------------------------------------------------------------
    def tune(self, lo_hz: float) -> float:
        """Set both LOs. Returns the frequency the chip reports back (it rounds to a few Hz)."""
        lo = int(lo_hz)
        if self.backend == "sim":
            lo_min, lo_max = float(self._sim.get("min_hz", 70e6)), float(self._sim.get("max_hz", 6e9))
            if not lo_min <= lo <= lo_max:
                raise DeviceError(f"{self.id}: {lo} Hz is outside the tuning range {lo_min:.0f} to {lo_max:.0f} Hz")
            self._lo = float(lo)
            return float(lo)
        try:
            self._sdr.rx_lo = lo
            self._sdr.tx_lo = lo
            return float(self._sdr.rx_lo)
        except Exception as exc:
            raise DeviceError(f"{self.id}: cannot tune to {lo} Hz: {exc}") from None

    def tx_tone(self, port: int, rf_hz: float, offset_hz: float, gain_db: float, scale: float, fs: float) -> None:
        """Send a single tone at rf_hz + offset_hz from TX `port` (1 or 2). The other TX is muted."""
        if port not in (1, 2) or port > self.channels:
            raise DeviceError(f"{self.id}: no TX{port}")
        self.tune(rf_hz)
        if self.backend == "sim":
            self.world.tones[f"{self.id}:tx{port}"] = {
                "rf_hz": rf_hz + offset_hz,
                "dbm": tx_reference_dbm(rf_hz) + gain_db + 20 * math.log10(scale),
                "ppm": float(self._sim.get("xo_ppm", 0.2))}
            return
        sdr = self._sdr
        try:
            sdr.sample_rate = int(fs)
            sdr.tx_rf_bandwidth = int(fs)
            self.tx_off()
            setattr(sdr, f"tx_hardwaregain_chan{port - 1}", float(gain_db))
            sdr.dds_single_tone(int(offset_hz), float(scale), channel=port - 1)
        except Exception as exc:
            self.tx_off()
            raise DeviceError(f"{self.id}: cannot start the tone: {exc}") from None
        time.sleep(0.3)

    def tx_off(self) -> None:
        """Mute both transmitters (DDS scale 0 and the lowest gain)."""
        if self.backend == "sim":
            for k in [k for k in self.world.tones if k.startswith(f"{self.id}:")]:
                del self.world.tones[k]
            return
        if self._sdr is None:
            return
        for ch in range(2):
            try:
                self._sdr.dds_single_tone(0, 0.0, channel=ch)
                setattr(self._sdr, f"tx_hardwaregain_chan{ch}", -89.0)
            except Exception:
                pass

    def capture(self, port: int, rf_hz: float, fs: float, samples: int, gain_db: float = 30.0) -> IQ:
        """Record `samples` IQ samples from RX `port` at LO rf_hz, manual gain."""
        if port not in (1, 2) or port > self.channels:
            raise DeviceError(f"{self.id}: no RX{port}")
        self.tune(rf_hz)
        if self.backend == "sim":
            return self._sim_capture(port, rf_hz, fs, samples, gain_db)
        sdr = self._sdr
        try:
            sdr.rx_destroy_buffer()
            sdr.sample_rate = int(fs)
            sdr.rx_rf_bandwidth = int(fs)
            sdr.rx_enabled_channels = [port - 1]
            setattr(sdr, f"gain_control_mode_chan{port - 1}", "manual")
            setattr(sdr, f"rx_hardwaregain_chan{port - 1}", float(gain_db))
            sdr.rx_buffer_size = int(samples)
            sdr.rx()                       # the first buffer after a retune is not settled; drop it
            x = sdr.rx()
        except Exception as exc:
            raise DeviceError(f"{self.id}: capture failed: {exc}") from None
        return IQ(cf32_bytes([complex(v) / FULL_SCALE_COUNTS for v in x]), "cf32_le", float(fs), float(rf_hz))

    def expected_rx_dbfs(self, port: int, rf_hz: float, tx_gain_db: float, scale: float, rx_gain_db: float,
                         loss_db: float) -> float | None:
        """What a good unit should read for a tone sent from TX `port` into RX `port` over `loss_db` of cable.

        Simulated: the model it runs on. Real: only if lab.toml holds a calibration for this port and band,
        copied from a unit already known good (`[devices.ID.calibration.rx_dbfs]`, key "915-rx1", at the
        suite's fixed gains). Without one there is no absolute reference and this returns None.
        """
        if self.backend == "sim":
            return tx_reference_dbm(rf_hz) + tx_gain_db + 20 * math.log10(scale) - loss_db + rx_gain_db + SIM_RX_K_DB
        cal = (self.cfg.get("calibration") or {}).get("rx_dbfs") or {}
        v = cal.get(f"{rf_hz / 1e6:g}-rx{port}")
        return float(v) if v is not None else None

    # --- simulation ------------------------------------------------------------------------------------
    def _sim_capture(self, port: int, rf_hz: float, fs: float, n: int, gain_db: float) -> IQ:
        world: SimWorld = self.world
        rng = world.rng
        ppm = float(self._sim.get("xo_ppm", 0.2))
        sigma = 10 ** (-57 / 20) / math.sqrt(2)
        tone = world.tones.get(f"{self.id}:tx{port}")
        amp = off = 0.0
        if tone:
            src, dst = f"{self.id}:tx{port}", f"{self.id}:rx{port}"
            loss = world.path_loss_db(src, dst) + world.extra_loss_db.get(dst, 0.0)
            dbfs = tone["dbm"] - loss + gain_db + SIM_RX_K_DB
            amp = 10 ** (dbfs / 20)
            # one crystal drives both LOs, so a crystal error cancels in loopback
            off = (tone["rf_hz"] * (1 + ppm * 1e-6)) - (rf_hz * (1 + ppm * 1e-6))
        ph = rng.uniform(0, 2 * math.pi)
        dc = complex(0.002, -0.001)
        x = [dc + complex(rng.gauss(0, sigma), rng.gauss(0, sigma)) + (amp * cmath.exp(1j * (2 * math.pi * off * k / fs + ph)) if amp else 0)
             for k in range(n)]
        return IQ(cf32_bytes(x), "cf32_le", float(fs), float(rf_hz))
