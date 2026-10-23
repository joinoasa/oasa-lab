# Hardware suites (L2)

The first suites that use real radios on cables. They are written and tested on a **simulated bench** now, so they are ready the day the gear arrives.

| Suite | What it proves | Needs |
|---|---|---|
| `l2-pluto-acceptance` | A Pluto+ clone is what it claims: identity, tuning range, crystal error, all four RF ports | Pluto+, RTL-SDR |
| `l2-lora-link` | The encrypted link works over two LoRa modems on coax, matches the airtime model, and the air shows only ciphertext | 2 Heltec modems, RTL-SDR, `oasa-sec`, `oasa-radio` |
| `l2-rf-control` | Negative control: a plaintext LoRa packet **is** found by the same air check | 2 Heltec modems, RTL-SDR |

> **Safety. Never connect two transmitters, or a transmitter and a receiver, by coax without at least 60 dB of attenuation between them.** Receive-only gear (the RTL-SDR) needs 40 dB. Fixed attenuators first, then cables. Check the wiring against the diagrams below before every transmitting run. The lab refuses to transmit on real hardware until you type the wiring name: `--confirm-wiring NAME`. The checker fails any bundle whose declared paths are below the limits. Radiating over the air is not part of L2.

## The simulated bench

`OASA_LAB_SIM=1` runs every device on its simulated backend, with no hardware and no `lab.toml`:

```
OASA_LAB_SIM=1 oasa-lab devices
OASA_LAB_SIM=1 oasa-lab run l2-pluto-acceptance
OASA_LAB_SIM=1 oasa-lab run l2-rf-control
OASA_LAB_SIM=1 OASA_SEC_BIN_DIR=... OASA_RADIO_BIN_DIR=... oasa-lab run l2-lora-link
```

The simulation is real-time: a LoRa packet takes its real airtime on a shared half-duplex medium, the Pluto and the RTL-SDR agree on tones through the cable losses, and the RTL-SDR "records" noise with a burst wherever a packet was on the air. So the checks have something real to check. CI runs these suites this way.

What the simulation **cannot** show: the real tools and libraries, USB and serial behavior, collisions between real radios, real RF levels, the clone's real faults. Every manifest records `backend: sim` or `hardware` per device. The checker prints **SIMULATED** for a bundle with simulated devices, and `oasa-lab check --require-hardware BUNDLE` fails it. A simulated pass is not an acceptance.

## Devices

| Kind | Hardware | Driver | How it is reached |
|---|---|---|---|
| `pluto` | Pluto+ (AD9363, 2 TX, 2 RX, TCXO) | `devices/pluto.py` | pyadi-iio (optional extra), over Ethernet or USB |
| `rtlsdr` | RTL-SDR Blog V4 (receive only) | `devices/rtlsdr.py` | `rtl_sdr` and `rtl_test` as separate processes; records raw IQ (`cu8`) |
| `serial-radio` | Heltec WiFi LoRa 32 V3 with the OASA LoRa modem firmware | `devices/modem.py` | pyserial; oasa-radio serial framing |
| hub | YKUSH3 (or any uhubctl hub) | `devices/hub.py` | `ykushcmd` or `uhubctl` as separate processes |

Every driver has `open`, `info`, `close` and `power_cycle` (through the hub), and a simulated twin. `info` always carries `model`, `serial`, `firmware` and `backend`; the manifest records it.

### Set up the lab PC

```
pip install -e '.[hw]'             # pyserial, pyadi-iio (and its libiio binding)
# install the RTL-SDR Blog driver fork (rtl_sdr, rtl_test) and ykushcmd (or uhubctl)
cp lab.example.toml lab.toml       # then edit: serial numbers, hub ports, ports, cable losses
oasa-lab devices                   # opens every device, prints what it reports
```

`lab.toml` is not committed. It maps each device to its serial number and USB port. If the serial a device reports is not the one in the file, the suite refuses to use it (so a swapped board cannot slip in). The file also holds the **wiring**: for every cable path, the nominal loss, summed from the parts. Reach lab hardware only over WireGuard; `iiod` and SDR network tools have no authentication.

### What the LoRa modem firmware must do

The firmware is written in oasa-radio. The lab assumes:

1. It speaks the framing of `oasa-radio bridge --radio serial` ([serial-framing.md](https://github.com/joinoasa/oasa-radio/blob/main/docs/serial-framing.md)) on its USB serial port: a packet in goes on the air; every packet heard on the air with a good LoRa CRC comes out. It does **not** decrypt, filter or add to what it hears. The lab's "air check" scans exactly these frames.
2. Its radio settings are fixed in the firmware and equal the `[devices.ID.radio]` table of `lab.toml` (profile, frequency, power). There is no way to ask the modem yet, so the file is the record; the SDR recording checks the airtime independently, so a wrong spreading factor shows up as burst lengths that do not match.
3. It keeps a gap of at least 2 ms between two packets it sends (the SX1262 needs about that to be re-armed anyway, est.). Packets that touch look like one long burst to the checker.
4. It either queues at least 28 packets or stops reading the serial line when full (USB flow control then holds the bridge). Dropping on overflow breaks "delivered exactly once".
5. A heard packet reaches the host within about 50 ms of the end of the burst.

These are open questions for the firmware; where the first real run disagrees, change this list or the firmware, not the check.

## Wiring

Write the nominal loss of each path into `lab.toml` (`loss_db`), summed from the parts. Use fixed 50 ohm SMA attenuators and a resistive splitter; terminate every unused port with 50 ohm. Part losses in the diagrams are typical; use the datasheet or your NanoVNA.

### Pluto+ acceptance (`wiring.pluto`)

```
                    30 dB            30 dB
 Pluto+ TX1 ------[ATT]----+----[ATT]------ Pluto+ RX1        TX1 to RX1: 30 + 3.5 + 30 = 63.5 dB
                           |
                    2-way splitter (3.5 dB per leg)
                           |   30 dB
                           +--[ATT]-------- RTL-SDR           TX1 to RTL-SDR: 63.5 dB

                    30 dB            30 dB
 Pluto+ TX2 ------[ATT]---------[ATT]------ Pluto+ RX2        TX2 to RX2: 30 + 30 = 60 dB
```

The Pluto+ puts out about +7 dBm at most. Sixty dB leaves -53 dBm at a receiver even at full power. The suite uses about -10 dBm and ramps up from -50.

### LoRa link (`wiring.lora`)

```
 Heltec A ---[30 dB]---+                  +---[30 dB]--- Heltec B
                       |                  |
                    4-way splitter (6 dB per leg)
                       |                  |
                       +---[30 dB]---RTL-SDR     50 ohm terminator on the 4th leg

 A to B: 30 + 6 + 6 + 30 = 72 dB        A to RTL-SDR: 30 + 6 + 30 = 66 dB
```

Set the modems to 10 dBm. At 72 dB B hears A at about -62 dBm (LoRa SF7 needs -123). The RTL-SDR sees about -56 dBm, 20 dB or more over its noise at a low gain.

Change the cabling between suites; `--confirm-wiring` is how you say the right one is in place.

## Running the suites

```
oasa-lab devices                                      # first: does everything open?
oasa-lab run l2-pluto-acceptance --confirm-wiring pluto
OASA_SEC_BIN_DIR=.../oasa-sec/target/debug OASA_RADIO_BIN_DIR=.../oasa-radio/target/debug \
  oasa-lab run l2-rf-control  --confirm-wiring lora
  oasa-lab run l2-lora-link   --confirm-wiring lora
oasa-lab check --require-hardware runs/<bundle>       # a real run must have no simulated device
```

A suite whose hardware is not in `lab.toml` is skipped with the reason. A skipped suite does not pass the checker (its known-bad cases never ran). A transmitting suite on real hardware is skipped, with the reason, until `--confirm-wiring` names its wiring.

Run `l2-rf-control` before `l2-lora-link`: it shows the air check works with the real modems and SDR before you rely on its silence.

### First run on real hardware

The drivers follow the library and tool documentation but have **not yet touched real hardware**. Expect small fixes on day one, in this order:

1. `oasa-lab devices`. Fix the serial numbers and ports in `lab.toml`. Check the `ykushcmd` command lines in `devices/hub.py` against `ykushcmd -h`.
2. `l2-pluto-acceptance`, identity and tuning first (`-k "identity or tunes"`). The pyadi-iio channel and gain calls and the full-scale count (2048) are the likely places to adjust.
3. The RTL-SDR tests need `rtl_test` to print `SN:` (the Blog fork does) and a gain that puts the tone 15 dB over the noise without clipping (`RTL_GAIN_DB`).
4. Then the modems. If the serial control lines reset the board on open, set `dtr`/`rts` in `lab.toml`.

### Calibration (absolute RX level)

Without a reference there is no "expected" absolute level for a real Pluto. The suite then judges the two ports against each other (after taking each cable's loss out) and skips `rx_level_vs_expected_db`. After a first run on a unit that passed everything else, copy the logged levels (`loopback` events in `log.jsonl`) into `lab.toml`:

```toml
[devices.pluto0.calibration.rx_dbfs]
"433-rx1" = -23.0
"433-rx2" = -19.5
```

They are valid for the suite's fixed gains (`oasa_lab/plutotest.py`). Change a gain and recalibrate. A second unit is then judged against the first.

For an absolute crystal error better than the RTL-SDR's 1 ppm, measure the RTL-SDR against a reference (a GPSDO) and put the result in `known_ppm` for it.

## What each suite checks

### `l2-pluto-acceptance`

| Test | Catches |
|---|---|
| `test_pluto_boots_and_reports_identity` | No serial, no firmware version, wrong model, fewer than 2 channels |
| `test_tunes_across_70mhz_to_6ghz` | A clone that is really a 325 MHz to 3.8 GHz part |
| `test_frequency_error_within_spec` | A plain 20 to 25 ppm crystal instead of the promised 0.5 ppm TCXO; a drifting synthesizer |
| `test_every_port_loopback[433, 915, 2400]` | A dead, weak, noisy or leaking port; a PLL that does not lock; a wrong attenuator or loose connector |
| `test_power_cycle_recovers` | The hub cannot reboot the radio (skipped without a hub) |

The Pluto's TX and RX share one crystal, so a loopback cannot show the crystal's error. The RTL-SDR (a second, independent TCXO) measures it, at 433 and 915 MHz. The RTL-SDR does not reach 2400 MHz; the crystal is the same at every band, so the 2400 MHz loopback checks the PLL, the level and the port.

Each loopback sends a tone at LO + 100 kHz, starting at TX gain -50 dB and stepping up while the receiver is read. A probe reading over -10 dBFS means the cable is wrong: the suite stops, with the transmitter off, long before the power could hurt.

Every capture (tone on and tone muted, per port and band) is saved as SigMF.

### `l2-lora-link`

`oasa-link` on each side, each through `oasa-radio bridge --radio serial --device PTY`, where the PTY is the lab's tap on the modem's serial port:

```
app -> oasa-link -> bridge -> pty -> TAP -> modem A ~~ coax ~~ modem B -> TAP -> pty -> bridge -> oasa-link -> app
```

- 28 marked packets each way (one burst per direction; every seventh is a MAVLink heartbeat), each delivered **exactly once**.
- **Handshake and goodput** against the airtime of the frames the modems actually carried (the `lora-sf7-bw125` model, see [profiles](profiles.md)); limits below.
- **The air check.** The tap keeps every frame each modem delivered to its host: what anyone with a receiver would get. They are saved as `captures/NN-air-DEVICE.pcap` (frames in synthetic IP and UDP headers) and the checker scans them as it scans simulated captures: no marker, no MAVLink, random-looking bytes.
- **The SDR recording.** The RTL-SDR records the whole exchange as SigMF (`cu8`, 250 kS/s, centered on the channel). The checker finds bursts of energy and lines them up with the frames: each frame needs a burst of the right length (airtime) at the right time. No demodulation.
- `test_modems_pass_packets_both_ways` runs first and tells a radio fault from a link fault.

### `l2-rf-control`

The negative control. No encryption: plaintext packets with a marker and MAVLink heartbeats go both ways. The suite declares `expect_plaintext` and `expect_mavlink`, so the checker must **find** them in both air captures and see that the bytes are not random. If the air check went blind, this suite would fail. The SDR recording is checked the same way.

## Thresholds

The limits are in each `suite.toml` under `[limits.*]`, with the reason next to the number. **The checker re-applies them from the suite file**, so a test cannot loosen them. *est.* = a first guess to tune on the real units; tune them in a pull request with the evidence.

### `l2-pluto-acceptance`

| Metric | Limit | Why |
|---|---|---|
| `tune_error_hz` | within 100 Hz | The synthesizer rounds to a few Hz. A 325 MHz to 3.8 GHz part misses at 70 MHz and 6 GHz |
| `freq_error_ppm_vs_rtl` | within 2.0 ppm | 0.5 (Pluto+ TCXO) + 1.0 (RTL-SDR Blog V4 TCXO) + 0.5 margin. A plain crystal is 10 times over |
| `freq_error_ppm_vs_spec` | within 1.0 ppm (optional) | Only with `known_ppm` for the RTL-SDR: 0.5 spec + 0.5 for the reference |
| `freq_error_ppm_band_spread` | at most 0.6 ppm | One crystal gives the same ppm at 433 and 915 MHz (est.) |
| `rtl_tone_snr_db` | at least 15 dB | The ppm number is only good with a clear tone |
| `loopback_offset_error_ppm` | within 0.25 ppm | One crystal drives both LOs; the PLLs must lock and round (est.) |
| `rx_level_dbfs` | -50 to -10 dBFS | Below: weak, dead or loose. Above: near clipping (est.) |
| `rx_level_vs_expected_db` | within 3 dB (optional) | Against the model (simulation) or the calibration (real). A good port repeats within 1 dB |
| `port_level_spread_db` | at most 3 dB | RX1 and RX2 after taking out each cable's loss (est.) |
| `tone_snr_db` | at least 25 dB | A good port gives 35 or more (est.) |
| `spur_snr_db_tx_off` | at most 15 dB | With TX muted, the strongest line may be 15 dB over the noise at most (est.) |
| `power_cycle_s` | at most 60 s | Boot takes about 20 s (est.) |

### `l2-lora-link`

| Metric | Limit | Why |
|---|---|---|
| `handshake_excess_s` | -0.05 to 2.0 s | Handshake time minus the airtime of the handshake frames. Cannot beat the air; 2 s for process start, USB and firmware turnaround (est.). A lost packet costs a 5 s retry and fails on purpose |
| `goodput_ratio` | 0.6 to 1.05 | Model time over measured time for a burst. Above 1.05 beats physics; below 0.6 wastes 40 % (est.) |

### What the checker fixes (check 7, not tunable by a test)

| Rule | Value |
|---|---|
| Cable loss toward anything that can transmit | at least 60 dB |
| Cable loss toward a receive-only device | at least 40 dB |
| RF setup | `cabled` or `shielded` |
| Burst length against airtime | within 2 ms + 3 % |
| A frame reaches the host after its burst ends | within 50 ms, after fitting one fixed clock offset of at most 1.5 s |
| Frames with a matching burst | at least 95 % |
| Bursts that match no frame | at most 10 % (and at least 2 allowed) |
| Matched bursts above the noise | at least 12 dB |

## Evidence

On top of the [usual bundle](../README.md#where-the-evidence-lands):

| Path | What |
|---|---|
| `recordings/NN-name.sigmf-meta`, `.sigmf-data` | Radio recordings ([SigMF](https://sigmf.org)); both files hashed in `SHA256SUMS`; `core:sha512` of the data inside the metadata; `oasa:run_id` binds it to the run |
| `captures/NN-air-DEVICE.pcap` | The frames a modem delivered to its host |
| `manifest.json` `hardware` | Every device: kind, backend, model, serial, firmware (and hash), radio settings. `hub`. `rf`: setup (`cabled` or `shielded`), wiring name, every path with its loss |
| `manifest.json` `recordings`, `transmits`, `requires` | The list of recordings (with the burst check each wants), and what the suite needs |
| `log.jsonl` `measurement` events | Every number judged against a limit: metric, value, min, max, ok |

The checker's [check 7](checker.md) validates all of it.

## Limits

- The air check proves what the **modems delivered**. A radio that adds its own bytes to the air (a LoRa header) is not visible in them. The SDR recording is checked for timing and energy only; nothing is demodulated yet.
- The simulated air has no collisions. Real LoRa radios can collide; `l2-lora-link` sends one direction at a time to keep clear of that, and the first real run shows what the keepalives do.
- The RTL-SDR's own 1 ppm limits the crystal measurement unless `known_ppm` is set.
- L3 (over the air with antennas) and L4 (field) are not here.
