# Radio profiles

A profile makes the simulated channel behave like a real radio: each packet takes the time it would take on the air, a half-duplex radio shares one medium, and the radio's packet size limit applies. A suite run then says something real about LoRa, SiK, mLRS or WFB-ng performance before we own the hardware.

**Checked October 2026.** Every number below has a source. A value marked *est.* could not be confirmed, with the reason. Profiles give airtime and packet limits only. They do not model range, signal strength or interference.

## Use

```
oasa-lab profiles                                  # list them
oasa-lab channel --profile lora-sf9-bw125 --loss 0.05 --pcap out.pcap
```

```python
ch = lab.channel(a=a.addr, b=b.addr, profile="lora-sf9-bw125", loss=0.05)
```

Settings you pass override the profile's. The manifest records the profile name and the fully resolved settings of each channel. The checker recomputes airtime from those settings and fails the run if two packets overlap on the air ([checker, check 5](checker.md)).

## The profiles

`oasa-lab profiles` prints this table (64-byte packet, A to B).

| Profile | Bit rate | Max payload | 64-byte airtime | Duplex |
|---|---|---|---|---|
| `ideal` | unlimited | none | 0 | full |
| `lora-sf7-bw125` | 5.5 kbps | 255 | 118.0 ms | half |
| `lora-sf9-bw125` | 1.8 kbps | 255 | 390.1 ms | half |
| `lora-sf12-bw125` | 0.3 kbps | 255 | 2.79 s | half |
| `sik-64k` | 64 kbps | 250 | 9.9 ms | half |
| `sik-64k-ecc` | 64 kbps | 118 | 19.1 ms | half |
| `mlrs-19hz` | 9.7 kbps | 280 | 53 ms | fixed slots |
| `mlrs-31hz` | 16 kbps | 280 | 32 ms | fixed slots |
| `mlrs-50hz` | 25.6 kbps | 280 | 20 ms | fixed slots |
| `wifi-wfb` | 13 Mbps | 1445 | 0.12 ms | full (approximation) |

"Bit rate" is the number people quote (LoRa: raw rate for SF and CR; SiK and Wi-Fi: air rate; mLRS: uplink serial rate). Real goodput is lower. Direction A is the ground side, B the vehicle or node. For mLRS, A to B is the uplink.

## How a packet is timed

Packets of one direction go out one after another. With `half_duplex` both directions share one medium: one packet on the air at a time, and when both sides are ready they take turns. A packet that arrives while the medium is busy waits in the queue. With `max_queue` set (packets, per direction), a packet that finds the queue full is dropped and counted as `queue_dropped`. Without it the queue has no limit. `duty_cycle` (0 to 1) keeps each direction's average transmit time under that share; it is an engineering parameter, not a statement about any regulation.

A lost packet still uses the air; nobody receives it. In the pcap, a packet's time stamp is when it **started** to go on the air. It arrives one airtime (plus any `delay_ms`, jitter or reorder delay) later.

Loss, duplicates, corruption, reordering and replay still depend only on the seed and the packet's place in the sequence. The clock decides when, and (with `max_queue`) whether the queue had room. A queue drop is the one decision that depends on timing; it does not change what happens to any other packet.

Three models, set by `airtime`:

| Model | Time on the air | Used by |
|---|---|---|
| `lora` | Semtech SX126x formula: symbols from SF, BW, CR, preamble, header, CRC, low data rate optimize | LoRa |
| `bitrate` | `fixed_us` + `(bytes + overhead_bytes) * 8 * fec_ratio / air rate`, plus `extra_bits`; with `symbol_us` the data part rounds up to whole symbols | SiK, WFB-ng |
| `slots` | `ceil(bytes / slot_payload)` slots of `slot_ms`, starting on the slot grid | mLRS |

`serial_bps` (SiK) adds a serial port in front of the radio: each packet is clocked in at that rate (10 bits per byte) before it can go on the air. At 57600 baud the serial port, not the 64 kbps air rate, limits a SiK link without ECC. Only the sending side's port is modeled.

## Numbers and sources

### LoRa (`lora-sf7-bw125`, `lora-sf9-bw125`, `lora-sf12-bw125`)

| Number | Value | Source | Status |
|---|---|---|---|
| Time-on-air formula | integer steps of the SX126x driver: `ceil((8*PL + 16*CRC - 4*SF + 8 + 20*explicit) / (4*(SF - 2*LDRO))) * (CR + 4) + preamble + 12`, then `(4*symbols + 1) * 2^(SF-2) / BW` | [Semtech SX126x driver, `sx126x_get_lora_time_on_air_numerator`](https://github.com/Lora-net/sx126x_driver/blob/master/src/sx126x.c). The datasheet formula (SX1261/2 datasheet, time on air, page 41 per [Semtech's forum](https://forum.lora-developers.semtech.com/t/sx126x-time-on-air-calculation/127)) | Confirmed from the driver code. The datasheet PDF itself was not re-read. A test compares our code with a line-by-line port of the driver over more than 10,000 settings |
| Cross-check | 13 bytes at SF12, 125 kHz: 1.155 s here, "1.16 seconds" quoted | [TTN forum](https://www.thethingsnetwork.org/forum/t/spreadsheet-for-lora-airtime-calculation/1190) | Agrees |
| Bandwidth, SF | 125 kHz; 7, 9, 12 | The brief | Choice |
| Coding rate | 4/5 | The brief; the usual LoRaWAN setting | *est.* Not looked up |
| Preamble | 8 symbols | [RadioLib `SX1262::begin`](https://github.com/jgromes/RadioLib/blob/master/src/modules/SX126x/SX1262.h) default | Confirmed |
| Header, CRC | explicit header, CRC on | The brief | Choice |
| Low data rate optimize | on when a symbol is over 16 ms: SF11 and SF12 at 125 kHz (SF12: 32.8 ms) | [Semtech SX1262 application note, "Recommendations for Best Performance"](https://cdn.sparkfun.com/assets/f/f/b/4/2/SX1262_AN-Recommendations_for_Best_Performance.pdf) ("when the symbol time exceeds 16.38 ms"); [TTN forum](https://www.thethingsnetwork.org/forum/t/a-point-to-note-lora-low-data-rate-optimisation-flag/12007) | Confirmed. The channel uses "over 16 ms" |
| Max payload | 255 | Payload length is an 8-bit field (`pld_len_in_bytes` is `uint8_t` in [`sx126x.h`](https://github.com/Lora-net/sx126x_driver/blob/master/src/sx126x.h)) | Confirmed |
| Half-duplex | one transmitter or receiver at a time | How LoRa transceivers work | Confirmed in principle |
| Bit rate in the table | `SF * BW / 2^SF * 4 / (4 + CR)` | Textbook definition of the LoRa raw rate | *est.* Derived, not looked up |

Semtech AN1200.13 (the LoRa modem designer's guide) was not opened; the formula comes from the driver and datasheet.

### SiK (`sik-64k`, `sik-64k-ecc`)

| Number | Value | Source | Status |
|---|---|---|---|
| Air rate | 64 kbps default | [ArduPilot SiK advanced configuration](https://ardupilot.org/copter/docs/common-3dr-radio-advanced-configuration-and-technical-information.html) ("The default is 64"); [`parameters.c`](https://github.com/ArduPilot/SiK/blob/master/Firmware/radio/parameters.c) `AIR_SPEED 64` | Confirmed |
| Serial speed | 57600 baud | `parameters.c` `SERIAL_SPEED 57` | Confirmed |
| Max radio packet | 252 bytes | ArduPilot page ("The highest radio packet size is 252 bytes"); [`radio.h`](https://github.com/ArduPilot/SiK/blob/master/Firmware/radio/radio.h) `MAX_PACKET_LENGTH 252` | Confirmed |
| Data per packet | 250 (ECC off), 118 (ECC on) | [`tdm.c`](https://github.com/ArduPilot/SiK/blob/master/Firmware/radio/tdm.c): `MAX_PACKET_LENGTH - sizeof(trailer)` (2-byte trailer); with Golay `MAX_PACKET_LENGTH/2 - (6 + sizeof(trailer))` | Confirmed |
| Trailer | 2 bytes per packet | `tdm.c` `struct tdm_trailer` (13-bit window and 3 flag bits; no AES) | Confirmed |
| Fixed time per packet | 13 byte-times (1.625 ms at 64 kbps) | `tdm.c` `packet_latency = (8 + 10/2) * ticks_per_byte + 13`: a 40-bit preamble (10 nibbles) plus 8 bytes. The extra 13 ticks of 16 µs (0.2 ms) are radio turnaround and left out | *est.* The code does not say what the 8 bytes hold (sync word, header, length, CRC) |
| ECC (Golay) | twice the bits on air, 4 extra coded bytes | ArduPilot page ("the data rate you can support is halved"); `tdm.c` ("golay encoding doubles the cost per byte ... and adds 4 bytes") | Confirmed. ArduPilot says ECC is no longer recommended, so it is off by default |
| Throughput check | 250-byte packets: 60.4 kbps air-limited, 46 kbps with the serial port | ArduPilot: "about 90%" of the air rate one way, with ECC off | Consistent. The serial port is the limit at 57600 baud |
| Half-duplex | both radios share one frequency | SiK TDM: transmit window, silence, receive window | Confirmed |
| TDM windows, silence periods, `MAX_WINDOW`, frequency hopping | not modeled | | The shared medium with turns is a simplification: real SiK waits for its window, so latency is higher |
| MAVLink framing (`MAVLINK` 0, 1, 2) | not modeled | ArduPilot page | The radio aligns packets to MAVLink frames; our packets are whatever the test sends |

### mLRS (`mlrs-19hz`, `mlrs-31hz`, `mlrs-50hz`)

| Number | Value | Source | Status |
|---|---|---|---|
| Modes | 19 Hz, 31 Hz, 50 Hz | [mLRS README](https://github.com/olliw42/mLRS/blob/main/README.md) (operation modes table) | Confirmed |
| Frame period | 53, 32, 20 ms | [`mLRS/Common/setup.h`](https://github.com/olliw42/mLRS/blob/main/mLRS/Common/setup.h) (`frame_rate_ms` 53, 32, 20) | Confirmed |
| Serial payload per frame | 64 bytes up (A to B), 82 bytes down (B to A) | [`mLRS/Common/frame_types.h`](https://github.com/olliw42/mLRS/blob/main/mLRS/Common/frame_types.h) (`FRAME_TX_PAYLOAD_LEN 64`, `FRAME_RX_PAYLOAD_LEN 82`). README serial rates agree: 50 Hz gives 3200 up and 4100 down bytes/s | Confirmed |
| Model | a fixed slot per frame, each direction on its own; a packet over the payload takes more slots | Frame-based design of mLRS | Simplification: real mLRS alternates transmit and receive inside one frame |
| Max packet | 280 | MAVLink 2's largest frame is 280 bytes. mLRS itself is a byte stream with no packet limit | *est.* From memory of the MAVLink spec; not looked up |
| Band notes | 50 Hz is 2.4 GHz only; 31 Hz and 19 Hz run on 2.4 GHz, 915/868 MHz and 433 MHz. "19 Hz 7x" (SX1276 radios) has the same serial rates and is not a separate profile | README (operation modes table) | Confirmed. The profile models timing only, so it applies to any band |

### WFB-ng on Wi-Fi (`wifi-wfb`)

| Number | Value | Source | Status |
|---|---|---|---|
| Radio settings | 802.11n, 20 MHz, MCS 1, long guard interval (STBC 1, LDPC 1) | [`wfb_ng/conf/master.cfg`](https://github.com/svpcom/wfb-ng/blob/master/wfb_ng/conf/master.cfg) (`bandwidth = 20`, `mcs_index = 1`, `short_gi = False`) | Confirmed |
| Rate | 13 Mbps: 52 data bits per 4 µs symbol | [Keysight HT payload structure](https://helpfiles.keysight.com/csg/n7617/Content/Main/Payload_Structure.htm) (MCS 1, 20 MHz: 52 bits per symbol); symbol of 3.2 µs + 0.8 µs guard | Confirmed, derived |
| Preamble | 40 µs: L-STF 8 + L-LTF 8 + L-SIG 4 + HT-SIG 8 + HT-STF 4 + 2 × HT-LTF 4 | [MathWorks, non-HT PPDU](https://www.mathworks.com/help/wlan/gs/non-ht-ppdu-structure.html) (8, 8, 4 µs); [HT PPDU](https://www.mathworks.com/help/wlan/gs/ht-ppdu-structure.html) (HT-STF 4, HT-LTF 4, HT-SIG two symbols) | Partly *est.* The count of two HT-LTFs for STBC is from the 802.11n rules, not looked up |
| Service and tail bits | 16 + 6 | IEEE 802.11n symbol-count formula | *est.* Not re-fetched; Keysight's page does not state it |
| Per-packet overhead | 56 bytes: 24 (802.11 header) + 9 (`wblock_hdr_t`) + 16 (ChaCha20-Poly1305 tag) + 3 (`wpacket_hdr_t`) + 4 (frame check sequence) | [`src/wifibroadcast.hpp`](https://github.com/svpcom/wfb-ng/blob/master/src/wifibroadcast.hpp) (`ieee80211_header`, `wblock_hdr_t`, `wpacket_hdr_t`, `crypto_aead_chacha20poly1305_ABYTES`) | Confirmed, except the 4-byte checksum, which the hardware adds (*est.*) |
| Max payload | 1445 | `master.cfg` `radio_mtu = 1445`. The source advises against UDP packets over 1448 bytes in production | Confirmed. The brief said about 1400 |
| Full duplex | the two directions are independent | The brief | Approximation: real WFB-ng links share one channel |
| Not modeled | FEC (default 1/2 for MAVLink and tunnel, 8/12 for video), 802.11 DIFS and backoff (about 0.1 ms a frame), retries | `master.cfg` | With FEC the usable rate is lower; apply it by lowering the rate |

The profiles read numbers and equations from the projects above. No code was copied.

## What this does not tell you

- Range, link budget, interference, fading. A profile with `loss=0.05` is a guess about the channel, not a measurement.
- Firmware behavior: retries, acknowledgements, adaptive data rate, listen-before-talk, hopping and sync time.
- Anything about regulations. `duty_cycle` is a plain parameter.

The first real radios (Tier 0b, [R-COM2 plan](r-com2-plan.md)) will show how far these models are from the hardware. Until then, results through a profile say how a link behaves with that airtime, not that it will fly.
