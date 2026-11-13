# Hardware suites (L2)

The first suites that use real radios on cables. They are written and tested on a **simulated bench** now, so they are ready the day the gear arrives.

| Suite | What it proves | Needs |
|---|---|---|
| `l2-pluto-acceptance` | A Pluto+ clone is what it claims: identity, tuning range, crystal error, all four RF ports | Pluto+, RTL-SDR |
| `l2-lora-link` | The encrypted link works over two LoRa modems on coax, matches the airtime model, and the air shows only ciphertext | 2 Heltec modems, RTL-SDR, `oasa-sec`, `oasa-radio` |
| `l2-rf-control` | Negative control: a plaintext LoRa packet **is** found by the same air check | 2 Heltec modems, RTL-SDR |
| `l2-sik-link` | R-COM1 on an off-the-shelf pair: both radios report the declared settings, bytes cross both bridges both ways unchanged, delivery and latency match the `sik-64k` model, the MTU holds; the bridge refuses a radio whose settings differ, also after the other radio changed them with `RT` (H1) | 2 SiK radios, `oasa-radio` with `--sik-expect` |
| `l2-sik-oasa-link` | R-COM2 over the R-COM1 radio: the encrypted link over the SiK pair, both ways, handshake and goodput against `sik-64k`, ciphertext only in what the radios delivered | 2 SiK radios, `oasa-sec`, `oasa-radio` with `--sik-expect` |
| `l2-rx-noise` | Receive only: each radio is alive and its input is quiet, and the E310 draws normal power. Transmits nothing and only reads the supply | B200mini, E310, KORAD |
| `l2-waveform-w1` | OASA's W0 modem between a B200mini and an E310 over cable, both ways, BPSK and QPSK: BER at a measured Eb/N0 within 1 dB of theory; noise gives no frames, the wrong demodulator passes no CRC; frame v1 found without the length; light-code packet loss within 1 dB of the simulation | B200mini, E310, `oasa-radio` |
| `l2-e310-two-channel` | Both E310 receive channels in one stream (direction finding, step 1): no lost samples, independent noise and matching floors with the transmitter off, channel isolation, and `oasa-radio waveform df` agrees with the lab's own phase sum | B200mini, E310, `oasa-radio` |
| `l2-sdr-link` | The encrypted oasa-sec link over the bench: every packet a frame v1 on the cables (RF relay); both ways, ciphertext only on the air, replay and changed packet dropped, untrusted identity gets no session ([sdr-link.md](sdr-link.md)) | B200mini, E310, `oasa-sec`, `oasa-radio` |
| `l2-sdr-control` | Negative control: plaintext frames over the same relay **are** found by the air check | B200mini, E310, `oasa-radio` |
| `l2-sdr-live-link` | The encrypted link live: both radios streaming, 32-byte messages at 100/s each way, delay of each direction on one clock; loss, one-way median/p99/p99.9/worst against the control-link targets, radio reopens ([sdr-live-link.md](sdr-live-link.md)) | B200mini, E310, the installed radio helper, `oasa-sec`, `oasa-radio` |
| `l2-sdr-live-soak` | F11: the same live link for 24 hours (`[sdr_live] soak_duration_s`), the same limits over the whole day, plus a per-hour report so a drift shows; the only suite the policy may give a job longer than an hour (`[lab] long_jobs`) ([sdr-live-link.md](sdr-live-link.md) section 6) | as `l2-sdr-live-link` |
| `l2-usrp-loopback` | A tone each way between a B200mini and an E310 over cable: the level follows the TX gain, the crystal offset is in spec and mirrors between directions, and the tone stops when the transmitter does | B200mini, E310 |

> **Safety. Never connect two transmitters, or a transmitter and a receiver, by coax without at least 60 dB of attenuation between them.** Receive-only gear (the RTL-SDR) needs 40 dB. Fixed attenuators first, then cables. Check the wiring against the diagrams below before every transmitting run. The lab refuses to transmit on real hardware until you type the wiring name: `--confirm-wiring NAME`. The checker fails any bundle whose declared paths are below the limits. Radiating over the air only in suites with `radiates = true` (see *Over the air* below).

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
| `sik` | SiK telemetry radio (Holybro SiK V3 915 MHz) | `devices/sik.py`, `devices/sikat.py` | pyserial; reads the settings with AT commands (`+++`, `ATI`, `ATI5`, `ATO`) when opened and records firmware and settings; never changes them. Then a transparent byte stream with oasa-radio's framing, through the same tap as the LoRa modem |
| `korad` | KORAD KA3005P bench supply | `devices/korad.py` | pyserial. Never above 6 V, 3 A; `lab.toml` can only lower that. `hold_output = true` for a supply that feeds a radio |
| `b200` | Ettus B200mini | `devices/usrp.py` | UHD tools as separate processes; records raw IQ (`cf32_le`); transmits only a test tone (`tx_waveforms`) or a test waveform the suite made (`tx_samples_from_file`), TX gain capped by `max_tx_gain` (default 40 dB) |
| `e310` | Ettus USRP E310 | `devices/usrp.py` | UHD tools as separate processes, `type=e3xx,addr=...` over the network; records raw IQ (`ci16_le`); test tone from TRX-A, same cap |
| `tinysa` | tinySA Ultra / Ultra+ (the level reference for now, about 2 dB) | `devices/tinysa.py` | pyserial, its USB serial console. Reads only: scan, peak level, marker. Adds the fitted input pad in software. Console commands are to be confirmed. See [calibration.md](calibration.md) |
| `power_sensor` | RF power sensor that speaks SCPI, for a later 0.5 dB calibration (first model: Keysight U2004A) | `devices/powersensor.py` | usbtmc (`/dev/usbtmc*`, standard library), a serial port (pyserial) or a raw SCPI socket. Reads only: identify, zero, set frequency, average power in dBm. Commands are in one table per model; all but `*IDN?`, `*CLS`, `*OPC?` are to be confirmed. See [calibration.md](calibration.md) |
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

### SiK pair (`wiring.sik`)

```
              RP-SMA/SMA   30 dB    30 dB   SMA/RP-SMA
 SiK A (USB) ----[ADP]----[ATT]----[ATT]----[ADP]---- SiK B (USB)        A to B and B to A: 60 dB
```

The Holybro SiK V3 puts out up to 20 dBm (100 mW). At 60 dB the other radio hears about -40 dBm, far above its
sensitivity (about -117 dBm at 64 kbps, Holybro's figure, est.) and below what can hurt it. Both radios transmit,
so both directions need 60 dB. Power both from the lab PC's USB. A SiK radio transmits whenever it is powered
(its TDM link keeps sending, est.), so connect the attenuators **before** you plug the radios in; the lab's
transmit gate covers what the lab sends, not what the radio's firmware sends by itself. No RTL-SDR: SiK hops
over 50 channels from 915 to 928 MHz by default, wider than an RTL-SDR sees at once.

### USRP loopback (`wiring.usrp`)

```
              30 dB   20 dB   10 dB
 B200mini TX/RX --[ATT]--[ATT]--[ATT]-- E310 RX2-B        B200 to E310: 60 dB

              30 dB   20 dB   10 dB
 E310 TRX-A   --[ATT]--[ATT]--[ATT]-- B200mini RX2        E310 to B200: 60 dB
```

Both radios can transmit, so each path needs 60 dB. Ettus limits: TX +20 dBm, RX input -15 dBm. At full TX gain the
receiver would see about -40 dBm; the suite stops at TX gain 40 dB (about -110 dBm at the receiver, est.). Tighten
every SMA join by hand and check it: on the first bench a loose join on the B200 side added about 21 dB.

The two radios read about 17 dB apart for the same gains (the E310 sends more, or the B200 hears better, at the
same gain index; measured by swapping the two attenuator stacks). So compare a path with itself over time, not
the two paths with each other.

Change the cabling between suites; `--confirm-wiring` is how you say the right one is in place.

**The transmit calls check too.** `Pluto.tx_tone`, `Usrp.tx_tone` and `ModemLink.send_packet` (and the pty a bridge writes through) ask the run first: the suite must say `transmits = true` in `suite.toml`, and a wiring must be on record (`hw.use_wiring()`, which on real hardware needs `--confirm-wiring`). Otherwise they raise `TxRefused` before anything goes out. A device built by hand on real hardware, with no `hw` fixture behind it, never transmits. A suite with `requires` must say `transmits = true` or `transmits = false`; on real hardware it is refused if it says neither. Muting (`tx_off`) and receiving are always allowed.

**Over the air.** A suite that transmits into antennas says `radiates = true` (with `transmits = true`) and names a wiring with `rf_setup = "antenna"` and an `antennas` list (see `lab.example.toml`). It needs a test plan. It runs on real hardware only when the owner has capped each transmitting radio in `[antenna] max_tx_gain_db` of the root-owned lab policy (job-run passes the caps to the suite as `OASA_LAB_ANTENNA_CAPS`), and confirmed its wiring as for any other suite. The hardware layer lowers each radio's `max_tx_gain` to its cap (never raises it) and refuses TX on any radio without one. Only USRPs (`b200`, `e310`) can radiate. A radiating suite cannot use cabled wiring, and a cabled suite cannot use antenna wiring. The checker requires the antennas, the caps and every recorded `max_tx_gain` at or below its cap.

## Running the suites

```
oasa-lab devices                                      # first: does everything open?
oasa-lab run l2-pluto-acceptance --confirm-wiring pluto
OASA_SEC_BIN_DIR=.../oasa-sec/target/debug OASA_RADIO_BIN_DIR=.../oasa-radio/target/debug \
  oasa-lab run l2-rf-control  --confirm-wiring lora
  oasa-lab run l2-lora-link   --confirm-wiring lora
  oasa-lab run l2-sik-link     --confirm-wiring sik
  oasa-lab run l2-sik-oasa-link --confirm-wiring sik
oasa-lab check --require-hardware runs/<bundle>       # a real run must have no simulated device
```

A suite whose hardware is not in `lab.toml` is skipped with the reason. A skipped suite does not pass the checker (its known-bad cases never ran). A transmitting suite on real hardware is skipped, with the reason, until `--confirm-wiring` names its wiring.

Run `l2-rf-control` before `l2-lora-link`: it shows the air check works with the real modems and SDR before you rely on its silence.

### The SiK pair on day one

1. Plug in one radio at a time, find its port (`ls -l /dev/serial/by-id/`) and USB serial (`udevadm info -q property -n PORT | grep SERIAL_SHORT`). Read it: `oasa-radio sik show --device PORT`.
2. Pick a NETID for the pair (`shuf -i 1-65535 -n 1`) and set both radios, one at a time: `oasa-radio sik set --device PORT --netid N --mavlink 0 --air-speed 64 --ecc 0 --txpower 20`. Each prints before and after.
3. Write both into `lab.toml` (`[devices.sik_a]`, `[devices.sik_b]` from `lab.example.toml`, with your ports, serials and NETID).
4. Wire `wiring.sik` (60 dB, attenuators on **before** power), then `oasa-lab devices`: each radio must show its firmware and settings.
5. `oasa-lab run l2-sik-link --confirm-wiring sik`, then `oasa-lab run l2-sik-oasa-link --confirm-wiring sik`, and `oasa-lab check --require-hardware` on each bundle.

Both suites need an `oasa-radio` with `sik` and `--sik-expect` in `OASA_RADIO_BIN_DIR` (they skip otherwise, and a skipped suite fails the checker). CI builds oasa-radio `main`, so the oasa-radio change that adds `--sik-expect` merges before the suites do.

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

### `l2-sik-link` (R-COM1)

```
app -> oasa-radio bridge --sik-expect -> pty -> TAP -> SiK A ~~ coax ~~ SiK B -> TAP -> pty -> bridge -> app
```

| Test | Shows |
|---|---|
| `test_radios_report_their_settings` | Both radios report (ATI5, read at open) what `[devices.ID.sik]` in `lab.toml` declares, `MAVLINK=0`, a NETID that is not the default 25, AIR_SPEED and ECC of the profile, the same link settings and firmware |
| `test_bytes_both_ways` | Each bridge checks its radio (`--sik-expect`) and starts. 30 packets each way, one at a time, random sizes up to the MTU: delivered unchanged, once, in order; `delivery_ratio`, latency against the model. One packet of exactly the MTU arrives, one byte more is dropped by the bridge (`too_large`). A burst of 40 MTU packets: `burst_ratio` |
| `test_bridge_refuses_mismatched_settings` | Known-bad: a bridge told to expect another NETID exits 1 with a failed `sik_check` naming NETID, and never starts |
| `test_remote_change_is_caught` | Known-bad (H1): radio B lowers radio A's TX power one step over the air (`RTS4=...`, never up; at the lowest step DUTY_CYCLE instead), not saved. A's bridge refuses, naming the setting. Then A is put back through its own port: `+++`, `ATZ` (a reboot drops the unsaved change), read again; if that fails and A has a `hub_port`, a USB power cycle. The result is logged (`sik_restored`); a failed restore fails the test and says to power-cycle the radio |

**The model** (`oasa_lab/siklink.py`): a packet of n bytes is `wire_len(n)` bytes on the serial line (oasa-radio's framing, the longest case). Alone, it takes the serial port in (10 bits a byte at SERIAL_SPEED), its airtime (`sik-64k`, [profiles](profiles.md)) and the serial port out. In a burst each packet costs the slower of the serial port and the air (at 57600 baud the serial port). Bridge MTU 242: framed it is at most 249 bytes, one SiK packet (250 bytes with ECC off).

The simulated SiK radios start with the settings `lab.toml` expects (so the settings checks pass in CI); a `[devices.ID.sim]` table overrides them, and an l0-lab test starts one mismatched and sees the suite's check and the checker fail. They (`devices/simsik.py`) speak the AT protocol (guards, echo, `RT` run by the linked radio without authentication) and carry bytes on the simulated air with the profile's airtime, half-duplex, through 57600-baud serial ports, hearing each other only when their link settings match. They have no TDM windows and no hopping, so real latency will be higher: the latency limits leave room for a TDM window (est.).

### `l2-sik-oasa-link` (R-COM2 over the R-COM1 radio)

`l2-lora-link` on the SiK pair: `oasa-link` on each side, each through a bridge with `--sik-expect`. 28 marked packets each way (every seventh a MAVLink heartbeat), each delivered exactly once; the handshake against the floor (serial in, air, serial out for each handshake frame) and goodput against the model; the frames each radio delivered are saved as air captures and the checker scans them (no marker, no MAVLink, random). No SDR recording (hopping, above).

### `l2-usrp-loopback`

For each direction the suite sends a 100 kHz-offset tone at 915 MHz, amplitude 0.3, at TX gain 20, 30 and 40 dB,
and records it at RX gain 40 dB. It stops if a probe tone reads above -20 dBFS (a wrong cable), before raising the
gain. At the last gain it measures the level, the SNR and where the tone landed; then it stops the transmitter and
records again: the tone must be gone (the known-bad case). The two directions' crystal offsets must add up to
about zero. The transmitter is a `tx_waveforms` process; it also stops by itself after `max_tx_seconds` (60 s), and
`close()` stops it in any case. Limits and their reasons: the suite's `suite.toml`.

### `l2-e310-two-channel`

Both E310 receive channels in one UHD stream (`E310.capture_pair`: `rx_samples_to_file --channels 0,1 --subdev
"A:0 A:1"`, which writes `ch0_` and `ch1_` files), the first step of direction finding (oasa-radio#7). On the
`usrp` cables the B200mini's tone reaches RX2-B only, so the suite measures what can be measured without a splitter:
the tone on RX2-B, how far below it RX2-A hears the same frequency (channel isolation, or a lower bound set by the
noise), that the two channels are independent noise with the transmitter off (coherence about 1/sqrt(N); a driver
writing one channel twice gives 1), that their noise floors match, and that `oasa-radio waveform df` gives the
same phase and coherence as the lab's own `r = mean(b conj(a))` on the same recording. All four recordings are
saved. With a splitter feeding both channels (a later wiring) the same capture measures the channels' phase
offset, the calibration for direction finding. The simulated bench gives each port its own noise and a
configurable channel offset (`sim.port_phase_deg`).

### `l2-waveform-w1`

OASA's waveform milestone W1 (oasa-radio issue 6): the W0 modem from simulation, on two real radios over the
`usrp` cables. `oasa-radio waveform iq` makes 24 frames with known random 64-byte payloads; the suite joins them
with gaps, shifts them up by a **digital IF of 250 kHz** and sends the block on repeat at **1 MS/s**, 125 kbaud, 8
samples per symbol. The other radio records four repeats; the suite shifts the recording back down and `oasa-radio
waveform decode` finds the frames. **Eb/N0 is measured from the recording** in the modem's own convention: Es from
the power on the frames minus the power in the gaps, N0 from the noise spectrum of the gaps inside the signal band.
The receiver runs at RX gain 60 dB and the decoder is given the payload length (`--payload-len 64`), so a bit
error in the unprotected W0 header does not lose a frame. For each direction and modulation the TX gain is raised
until the frames are clear, then set for about 1, 3, 5 and 7 dB. A point counts with 20 or more bit errors; its loss is the measured Eb/N0 minus the Eb/N0 at which theory gives
the measured BER, and it must be within 1 dB. Code: `oasa_lab/w1test.py`.

**Outside signals.** The cable bench is not sealed from the air, and on 2026-11-11 two runs each lost one point to
a narrowband signal that came on part way through a recording: +13 dB in 150 to 200 kHz and +7 dB in 200 to 250 kHz
above the centre for the last 160 ms of a 1.09 s recording, nothing else changed, and the 18 frames under it were
lost. So every recording is checked before it is decoded (`find_interference`): power in 2 ms blocks, in sub-bands
16 to 203 kHz either side of the IF, against each sub-band's normal level in that recording. Our own signal and
the noise are symmetric about the IF, so anything we change (TX gain, the transmitter starting, frames and gaps)
raises a sub-band and its mirror together; three blocks in a row where one side rises 5 dB or more beyond twice
its mirror's rise flag the recording. A flagged recording is logged as a `capture_flagged` event (where, how much,
when, and whether it was taken again), the first of the run is saved as `w1-flagged` with the span annotated,
and the recording is taken again, at most 2 extra per point (`MAX_RECAPTURES`, the only place this is set; a
light point's 4 recordings share them). When the retakes run out, the last recording is used and the point
passes or fails on its merits. The decision rests on the samples alone, before anything is decoded, so a point
cannot be taken again until it passes: a clean recording that measures badly is kept. Each test logs its count
as `captures_flagged` (for the record, no pass mark), the `w1_point`, `w1_v1_point` and `w1_light_point` lines carry
`captures_flagged`, and the run's `summary.md` gives the total. Not seen, by design: a signal centred on the IF
with a symmetric spectrum, one on for most of the recording, one shorter than about 3 blocks, or a rise across the
whole band. On synthetic recordings (real oasa-radio blocks, Es/N0 -1 to 20 dB, offsets to 12 kHz, gain steps,
noise-floor rises, a ramping or late transmitter) none of 1600 clean ones was flagged; a 10 dB rise for 8 ms or
more was found in 1200 of 1200, a 6 dB rise for 20 ms or more in 314 of 320, a 3 dB rise never.

**Recordings.** Each test saves one recording (`w1-DIRECTION-MOD`, `w1-v1-...`, `w1-light-...`: its strongest
or first point), which is rarely the one that failed. So a point outside a limit (`ber_loss_db`, `light_loss_db`,
`light_delivered_fraction`, `v1_found_fraction`, `rx_peak_fs`, or a loss outside the simulated curve) also saves
its own recording as `w1-fail-TAG-DIRECTION-MOD-txGAIN` (for example `w1-fail-ber-e310-b200-bpsk-tx23p5`). Its
description and one annotation over the whole recording give the direction, modulation, TX gain, measured Eb/N0,
the metric, its value and limit (`oasa:metric`, `oasa:value`, `oasa:min`, `oasa:max`, ...); a light point saves
its first of 4 captures. When a test has too few points in its window (`points_measured`,
`light_points_measured`), the points inside and the nearest one on each side are saved (tag `window`,
`light-window`). At most 4 a run, first come first kept (`MAX_KEPT`, `w1test.KeepFailing`); one past that is
logged as `w1_recording_dropped`. They are ordinary recordings: hashed, in the manifest and `summary.md`, and
checked. A run that passes saves none. Nothing here changes a limit or a verdict.

What the first bench showed (2026-11-04): at 200 kS/s nothing decoded although the frames were there (the E310
has no DSP for low rates); at zero IF the BER was about 5 dB off theory (the receiver's DC offset sits on the
signal); with the IF at 1 MS/s every point was within 0.9 dB, both ways, BPSK and QPSK. Frames missed below about
4 dB are not counted, so the BER there is for the frames found. 2-FSK is not in the suite yet.

Follow-up (2026-11-04, outside the service): those missed frames were header bit errors, a share of (1-BER)^16.
With the length known (oasa-radio #13) 189 to 191 of 192 frames are found from 0 to 8 dB. At RX gain 40 the E310's
noise was about one ADC step, so it was quantized and BPSK read 0.65x theory at 7 dB; at RX gain 60 it read 1.03x.
QPSK lost up to 1.4 dB at 0 to 2 dB from the coarse frequency search; oasa-radio #15 fits the frame's phase slope
and brings 1 and 2 dB within 0.1 dB (0 dB still about +1.2 dB, oasa-radio #14, so the lowest target is 1 dB).
The suite needs oasa-radio 34be038 or later (`--payload-len` on decode).

**Frame format v1** (oasa-radio `docs/frame-v1.md`, its bench check plan): `test_v1_header_finds_frames` sends
24 v1 frames (header with a rate-1/4 code and a CRC-8) at the TX gains of the W0 points near 1 and 3 dB and
decodes them *without* the length, as a real link must. At 0.5 dB Eb/N0 and up, at least 90 % of the frames in
the recording must be found (`v1_found_fraction`, est.). The W0 recordings of the same points are decoded again
by reading their unprotected header (`w0_header_found_fraction`, logged for comparison, no pass mark), and every
`w1_point`/`w1_v1_point` line carries the decoder's counts (`preambles`, `sync_rejected`, `header_rejected`,
`too_short`, `inside_frame`), so a result says which stage lost frames. Simulated bench: v1 93 to 95 of 96
found at 0.7 to 3.3 dB; W0 by its header 38 to 73 of 96.

**Light error correction** (oasa-radio `docs/fec-light.md`, its bench check plan; needs an oasa-radio with
`--code`): `test_light_code_packet_loss` sends 24 frame-v1 frames with the light code (K=7, rate 1/2, interleaver;
`waveform iq --frame v1 --code light`) at TX gains set from the W0 probe: the symbol energy at a gain is the same,
so a coded frame's Eb/N0 is the W0 probe's plus 3.0 dB, and gain = probe gain + (target - 3.0 - probe Eb/N0) for
2.5, 3.5, 4.5 and 5.5 dB (BPSK) or 2.0, 2.5, 3.0 and 5.5 dB (QPSK: its simulated loss falls from 50 % to
2 % between about 1.8 and 3.5 dB, so 3.5 and 4.5 would give one judged point). **Eb counts information bits**: the measured Es/N0 is divided by bits per symbol x code
rate (rate 1 for W0 and uncoded v1, so their numbers do not change). A point is 4 recordings of 4 1/8 repeats, at
least 384 frames, decoded *without* the length (default sync check; again with `--sync-max-errors 5`, logged
only). Delivered = a good CRC-16 and a payload that was sent. Frames are counted per slot: where the block sits in
each recording follows from the frames found, so frames cut by the recording's edges are neither sent nor lost.
The reference is `oasa-radio waveform per --frame v1 --code light` at 1.5 to 6.5 dB, 400 frames, `--cfo-search 0.1
--cfo-random 0.018 --random-frac-delay` (the same binary, once per modulation; both start with the first light test and run while the bench measures).
For each point with a loss of 2 to 50 %, `light_loss_db` = measured Eb/N0 minus the Eb/N0 at which the
simulation gives that loss (interpolated in log loss) must be within 1 dB; at 5 dB and up at least 97 % must be
delivered (`light_delivered_fraction`, est.). Known bad: `test_light_as_uncoded_fails_the_crc` decodes a light
recording with `--payload-len 64 --code none`, which must give no good CRC; with `--code light` it must give
some. Each `w1_light_point` line has the slots, delivered, the loss with sync 5, frames with a bad CRC-16 and
the decoder's counts; `w1_light_reference` has the simulated curve.
Simulated bench (2026-11-05): 392 to 394 slots a point, `light_loss_db` -0.39 to +0.33 dB over 12 judged
points, 99.7 to 100 % delivered at 5.3 to 5.8 dB, 0 good CRCs read as uncoded (98 read as light). The suite then
takes about 14 minutes instead of 5; the W0 and v1 numbers are unchanged.

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

### `l2-sik-link` and `l2-sik-oasa-link`

| Metric | Limit | Why |
|---|---|---|
| `delivery_ratio` | at least 0.98 | On cable SiK should lose nothing (est.). Any changed, doubled or reordered packet fails the test anyway |
| `latency_excess_p50_s` | -0.05 to 0.15 s | Median latency minus the model. Below: beats the model by more than sending part of a frame early can save (est.). Above: a TDM window (MAX_WINDOW 131 ms) plus USB latency (est.) |
| `latency_excess_max_s` | -0.05 to 0.4 s | The worst packet: a window on each side plus the silence between, with room (est.) |
| `burst_ratio` | 0.6 to 1.05 | Bottleneck time over measured time for 40 MTU packets. Above 1.05 beats the serial port; ArduPilot quotes about 90 % of the air rate one way (est.) |
| `handshake_excess_s` | -0.1 to 2.0 s | Handshake time minus the floor of the frames carried; 2 s for processes, USB and TDM (est.). A lost handshake packet (5 s retry) fails on purpose |
| `goodput_ratio` | 0.5 to 1.05 | Model time over measured time for the link's burst, both directions' frames (est.) |

Simulated bench, seeds 1 to 3 (2026-11-05): delivery 1.0, median latency excess -5 to +3 ms, worst +6 to +28 ms, burst ratio 0.97 to 0.98, handshake excess 0.03 to 0.06 s, goodput 0.94 to 0.97. The simulation has no TDM windows, so expect the real latency well above these.

**Checker (check 7) for `sik` devices:** each must record its ATI5 settings and the settings `lab.toml` expected, and they must agree; `MAVLINK` must be recorded and 0; a real radio's expectation must name at least NETID and MAVLINK; all SiK radios in a run must share the settings that let them link (NETID, AIR_SPEED, ECC, band, LBT_RSSI, MAX_WINDOW); a real one must record its firmware (the `ATI` banner).

### What the checker fixes (check 7, not tunable by a test)

| Rule | Value |
|---|---|
| Cable loss toward anything that can transmit | at least 60 dB |
| Cable loss toward a receive-only device | at least 40 dB |
| RF setup | `cabled` or `shielded`; `antenna` only for a suite with `radiates = true`, with every transmitting radio capped |
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
| `manifest.json` `hardware` | Every device: kind, backend, model, serial, firmware (and hash), radio settings. `hub`. `rf`: setup (`cabled`, `shielded` or `antenna`), wiring name, every path with its loss; for `antenna` the antenna ports and TX caps |
| `manifest.json` `recordings`, `transmits`, `radiates`, `requires` | The list of recordings (with the burst check each wants), and what the suite needs |
| `log.jsonl` `measurement` events | Every number judged against a limit: metric, value, min, max, ok |

The checker's [check 7](checker.md) validates all of it.

## Limits

- The air check proves what the **modems delivered**. A radio that adds its own bytes to the air (a LoRa header) is not visible in them. The SDR recording is checked for timing and energy only; nothing is demodulated yet.
- The simulated air has no collisions. Real LoRa radios can collide; `l2-lora-link` sends one direction at a time to keep clear of that, and the first real run shows what the keepalives do.
- The RTL-SDR's own 1 ppm limits the crystal measurement unless `known_ppm` is set.
- L3 (over the air with antennas) and L4 (field) are not here.
