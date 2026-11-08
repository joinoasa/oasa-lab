# Bench calibration

The calibration file holds what was measured on the bench: the loss of each cable route, each radio's TX
power at each gain, and each receiver's dBFS-to-dBm scale. Suites read it to turn raw numbers into absolute
levels. Each run's manifest records which file was in place (its sha256 and date), so a result says which
calibration it used. How the measurements are taken is the lab's own procedure; this page covers the file,
the safety minimums it enforces, and how suites read it.

## Where it lives

`lab.toml` points at it. Without the table nothing changes: suites run as before and the manifest says
`"calibration": null`.

```toml
[calibration]
path = "calibration.toml"     # relative to lab.toml
recabled = "2026-01-20"       # optional: the last re-cabling. A calibration dated before it is refused
```

After any re-cabling, measure the changed routes again, bump `date`, and set `recabled`.

## The file (version 1)

A full example with placeholder numbers: [`calibration.example.toml`](../calibration.example.toml).

```toml
version = 1
date = "2026-01-15"            # the day the measurements were taken
operator = "example"
vna = { model = "...", cal_kit = "...", cal_date = "2026-01-15" }
power_sensor = { model = "...", serial_hash = "sha256 prefix only", cal_cert_date = "2025-09-01" }

[paths.b200_to_e310]           # one table per route the tests use
route = ["b200.tx", "cable.c1", "att.30a", "att.20a", "att.10a", "cable.c2", "e310.rx2b"]
freq_mhz  = [915, 920, 2400, 2450, 5800]
loss_db   = [60.4, 60.4, 61.0, 61.1, 62.3]    # positive dB, one per frequency
phase_deg = [10.0, 8.5, -80.0, -95.0, 30.0]
# receive_only = true          # a route toward a device that cannot transmit (a sensor, an RTL-SDR)

[radios.b200.tx]
freq_mhz = [915, 920]
gain_db  = [0, 10, 20, 30, 40, 50, 60, 70, 80, 89.75]
dbm_out  = [[...], [...]]      # one row per frequency, one value per gain step
spread_db_max = 0.15           # spread of the repeated readings

[radios.b200.rx]
freq_mhz = [915, 920]
gain_db  = [0, 20, 40, 60, 76]
dbfs_to_dbm = [[...], [...]]   # dBm_in = dBFS + this value
floor_dbfs  = [[...], [...]]   # terminated noise floor
```

## Rules (the loader refuses a file that breaks one)

- `version = 1`; `date` and `power_sensor.cal_cert_date` are `YYYY-MM-DD`.
- **Safety minimums:** every route has 60 dB or more at every listed frequency (between transmitters), or
  40 dB or more if it says `receive_only = true`. Losses are positive.
- Every list has the length of its axis; tables are rows (frequencies) by columns (gains). Axes rise.
- TX: `spread_db_max` at most 0.2 dB, and `dbm_out` does not fall (by more than 0.5 dB) as gain rises.
- The power sensor's certificate is at most 12 months old on `date`.
- **No serial numbers in clear.** Use `serial_hash` (a sha256 prefix). The file is copied into evidence
  bundles, which may be public.
- Not older than `recabled` in lab.toml.

## How suites read it

```python
def test_absolute_level(lab):
    cal = lab.require_calibration()            # skips the test when none is configured
    loss = cal.path_loss_db("b200_to_e310", 915.0)
    tx = cal.tx_dbm("b200", gain_db=40, freq_mhz=915.0)
    rx = cal.rx_dbm("e310", dbfs=-23.4, gain_db=40, freq_mhz=915.0)
```

| Call | Returns |
|---|---|
| `lab.calibration` | The `Calibration`, or `None` when lab.toml configures none. Raises `CalibrationError` for a bad or stale file |
| `lab.require_calibration()` | The `Calibration`; skips the test without one; fails it for a bad one |
| `cal.path_loss_db(path, freq_mhz)` | Route loss (dB) |
| `cal.tx_dbm(radio, gain_db, freq_mhz)` | Power out of the radio's TX port (dBm) |
| `cal.rx_dbm(radio, dbfs, gain_db, freq_mhz)` | Level at the RX port (dBm) from a measured dBFS; `cal.rx_offset_db(...)` is the offset alone |
| `cal.rx_floor_dbfs(radio, gain_db, freq_mhz)` | Measured noise floor (dBFS) |
| `cal.date`, `cal.sha256` | What the manifest records |

Values between measured points are interpolated linearly (dB against MHz, and against gain dB). Nothing is
extrapolated: asking outside the measured range raises `CalibrationError`. Prefer the measured frequencies.

A suite that cannot run without it says so in `suite.toml`:

```toml
needs_calibration = true      # every test is skipped without a calibration, and fails on a bad one
```

## In the evidence

The manifest's `calibration` is `null`, or `{sha256, date, version, valid, used}`. `used` is true once a
test read the file; the bundle then keeps a copy as `calibration.toml`. A file that is configured but not
read is recorded by hash and date only (`valid` says whether it would load). The checker checks the field
and that the copy matches (see [checker.md](checker.md)). Older bundles without the field still pass.

## The power sensor

`kind = "power_sensor"` in lab.toml (see `lab.example.toml`): SCPI over usbtmc (`/dev/usbtmc*`), a serial port
or a raw socket. It only reads: identify, zero (with no signal at its input), set the frequency correction,
read average power in dBm. A reading within 3 dB of `max_input_dbm` stops with an error. The commands are in
one table per model in `oasa_lab/devices/powersensor.py`; only `*IDN?`, `*CLS` and `*OPC?` (IEEE 488.2) are
fixed by a standard, and the manifest records `commands_confirmed: false` until a model's table has been
checked against its programming guide. `keysight-u2004a` is the first model. `rs-nrp-z11` is a reserved slot
with no backend (it uses the vendor's toolkit, not plain SCPI). The simulated backend returns a configured
level plus noise.
