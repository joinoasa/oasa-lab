# Voice over LoRa, simulated (T-MSG1)

Which LoRa settings can carry push-to-talk voice through the OASA link? Suite `l1-voice-lora` answers it in simulation, before any hardware ([issue 64](https://github.com/joinoasa/oasa-lab/issues/64)). **Simulated, not measured.** Nothing here says anything about range.

```
oasa-lab run l1-voice-lora --seed 1
python3 tools/voice_lora_table.py runs/<bundle>    # prints the tables below
```

## What it does

A talker's radio sends one packet every *interval*. A packet holds the codec frames made in that interval plus the OASA link's per-packet overhead. **Direct:** talker to listener. **Relayed:** the talker's packet goes to one relay, which sends it again on the same channel. All radios are half-duplex on one channel, so both hops share the air.

The sweep runs the lab channel's own scheduler (`channel.Air`) and per-packet decisions (`channel.Direction`) on a simulated clock (`oasa_lab/voicesim.py`): 15 settings × 3 rates × 3 intervals × direct/relayed = 270 cases in about a second. One case (SF7, 500 kHz, 2400 bit/s, 80 ms) also runs through the real socket channel, direct and relayed; the checker re-checks its airtime from the capture, and the test checks that it agrees with the simulated clock.

## Numbers and sources

| Number | Value | Source | Status |
|---|---|---|---|
| Codec frames | 700 bit/s (700C): 4 B per 40 ms; 1200: 6 B per 40 ms; 2400: 6 B per 20 ms | Codec 2 [`codec2.c`](https://github.com/drowe67/codec2/blob/main/src/codec2.c): `codec2_bits_per_frame` 28, 48, 48; `codec2_samples_per_frame` 320, 320, 160 at 8 kHz; `codec2_bytes_per_frame` rounds bits up to whole bytes (checked November 2026) | Confirmed. 700C packs 28 bits into 4 bytes, so it sends 800 bit/s |
| OASA overhead | **22 bytes per data packet**: header 1 + receiver index 2 + counter 2 + content type 1 + tag 16 | oasa-sec [`docs/oasa-sec-v0-link.md`](https://github.com/joinoasa/oasa-sec/blob/main/docs/oasa-sec-v0-link.md) §3 ("1 + 2 + 2 + 1 + 16 = 22 bytes (24 with the long counter)"); `src/link/packet.rs` (`TAG_LEN = 16`, `data_header`), oasa-sec `bfc7993`. The [R-COM2 plan](r-com2-plan.md) and the T-MSG1 plan say "about 21–23 bytes", with the 16-byte tag never shortened | Confirmed. The short 2-byte counter fits a slow link (the long one is for fast links, §3) |
| Plan marks | mouth-to-ear delay under 1 s and at least 95 % of voice packets delivered, direct and through one relay. Plan setting: SF7 or SF8 at 250 or 500 kHz; Codec 2 700 bit/s baseline, 1200 and 2400 also tested | The owner's plan for T-MSG1 (issue 64) | Targets, not measurements |
| Packet interval | 80, 160, 320 ms (2, 4, 8 frames at 40 ms; 4, 8, 16 at 20 ms) | Choice: bundling trades delay for airtime | Choice |
| LoRa settings | SF7 to SF11 at 125, 250, 500 kHz; CR 4/5, 8-symbol preamble, explicit header, CRC on | [profiles.md](profiles.md) | As there |
| Talk spurt | 30 s of continuous speech | Choice | Choice |
| Transmit queue | 4 packets per radio; a packet that finds it full is dropped | No firmware yet | *est.* A small firmware queue. It sets how much is lost and how long the delay grows when a setting is overloaded, not whether it carries |
| Channel loss | 0 in the tables | Choice | The tables show capacity only. `test_same_seed_same_results` runs 10 % loss per hop and checks that relayed delivery is about 0.9² |

## What the numbers mean

- **Occupancy:** airtime of one packet × hops ÷ interval. The share of the channel the voice needs. Over 100 %, the setting **fails**: the queue overflows and packets are lost however long the talk.
- **Delivery:** packets that reach the listener ÷ packets sent.
- **Delay:** for each codec frame, from the moment the frame is complete to the moment the packet holding it has fully arrived (both hops when relayed). Mean and 95th percentile. It includes waiting for the rest of the packet's frames, queueing and airtime. It leaves out codec delay, the phone's Bluetooth link, a playout buffer and processing time.
- **Meets the plan:** occupancy at most 100 %, delivery at least 95 %, and the 95th-percentile frame delay under 1 s. The delay here leaves out codec, Bluetooth and playout time, so the real margin to 1 s is smaller than the table shows.

A setting that carries delivers 100 % in every case here. Near 100 % occupancy there is no room left for anything else: handshakes (119 + 55 bytes), keepalives, the other side's voice, other users.

## Results

Run `l1-voice-lora --seed 1`, run id `ec38e188bdde4a919a5ccadacc0a3c4a`, lab commit `6bd5f76`, checker PASS. A second run with the same seed gave the same numbers in every `voice_case` and measurement line.

**The plan's settings (SF7 or SF8, 250 or 500 kHz).** All meet the plan's marks except one: SF8 at 250 kHz cannot carry 2400 bit/s through a relay at any interval (109 % at best).

| Setting | Direct: all three rates | Relayed: 700 and 1200 | Relayed: 2400 |
|---|---|---|---|
| SF7, 500 kHz | 80 ms packets, 22–29 %, mean 38–53 ms | 80 ms, 45–48 %, mean 56–58 ms | 80 ms, 58 %, mean 76 ms |
| SF8, 500 kHz | 80 ms, 39–51 %, mean 51–71 ms | 80 ms, 77–84 %, mean 82–87 ms | 160 ms, 71 %, mean 183 ms |
| SF7, 250 kHz | 80 ms, 45–58 %, mean 56–76 ms | 80 ms, 90–96 %, mean 92–97 ms | 160 ms, 80 %, mean 198 ms |
| SF8, 250 kHz | 700 and 1200 at 80 ms (77–84 %); 2400 at 160 ms (71 %, mean 183 ms) | 700 at 160 ms (90 %); 1200 at 320 ms (71 %, mean 366 ms) | **no** |

Other settings: at 125 kHz only SF7 and SF8 carry anything (SF8 direct only); SF9 to SF11 at 125 kHz and SF11 at 250 kHz carry nothing. Every case that carries is under 1 s at the 95th percentile (worst 618 ms) and delivers 100 %. Cases at 90 % occupancy or more leave almost no air for handshakes, keepalives or a reply.

Cells: occupancy · mean / 95th-percentile frame delay. **fail** cells: occupancy · delivery · 95th-percentile delay of what arrived (30 s spurt, 4-packet queue).

### Meets the plan: lowest delay

| Setting | 700 direct | 700 relayed | 1200 direct | 1200 relayed | 2400 direct | 2400 relayed |
|---|---|---|---|---|---|---|
| `lora-sf7-bw125` | 80 ms: 90 %, 92 / 112 ms | 320 ms: 64 %, 345 / 485 ms | 80 ms: 96 %, 97 / 117 ms | 320 ms: 80 %, 396 / 536 ms | 160 ms: 80 %, 198 / 268 ms | no |
| `lora-sf8-bw125` | 160 ms: 90 %, 204 / 264 ms | no | 320 ms: 71 %, 366 / 506 ms | no | no | no |
| `lora-sf9-bw125` | no | no | no | no | no | no |
| `lora-sf10-bw125` | no | no | no | no | no | no |
| `lora-sf11-bw125` | no | no | no | no | no | no |
| `lora-sf7-bw250` | 80 ms: 45 %, 56 / 76 ms | 80 ms: 90 %, 92 / 112 ms | 80 ms: 48 %, 58 / 78 ms | 80 ms: 96 %, 97 / 117 ms | 80 ms: 58 %, 76 / 106 ms | 160 ms: 80 %, 198 / 268 ms |
| `lora-sf8-bw250` | 80 ms: 77 %, 82 / 102 ms | 160 ms: 90 %, 204 / 264 ms | 80 ms: 84 %, 87 / 107 ms | 320 ms: 71 %, 366 / 506 ms | 160 ms: 71 %, 183 / 253 ms | no |
| `lora-sf9-bw250` | 160 ms: 84 %, 194 / 254 ms | no | 160 ms: 96 %, 214 / 274 ms | no | 320 ms: 99 %, 468 / 618 ms | no |
| `lora-sf10-bw250` | 320 ms: 96 %, 448 / 588 ms | no | no | no | no | no |
| `lora-sf11-bw250` | no | no | no | no | no | no |
| `lora-sf7-bw500` | 80 ms: 22 %, 38 / 58 ms | 80 ms: 45 %, 56 / 76 ms | 80 ms: 24 %, 39 / 59 ms | 80 ms: 48 %, 58 / 78 ms | 80 ms: 29 %, 53 / 83 ms | 80 ms: 58 %, 76 / 106 ms |
| `lora-sf8-bw500` | 80 ms: 39 %, 51 / 71 ms | 80 ms: 77 %, 82 / 102 ms | 80 ms: 42 %, 53 / 73 ms | 80 ms: 84 %, 87 / 107 ms | 80 ms: 51 %, 71 / 101 ms | 160 ms: 71 %, 183 / 253 ms |
| `lora-sf9-bw500` | 80 ms: 71 %, 77 / 97 ms | 160 ms: 84 %, 194 / 254 ms | 80 ms: 77 %, 82 / 102 ms | 160 ms: 96 %, 214 / 274 ms | 80 ms: 96 %, 107 / 137 ms | 320 ms: 99 %, 468 / 618 ms |
| `lora-sf10-bw500` | 160 ms: 77 %, 183 / 243 ms | 320 ms: 96 %, 448 / 588 ms | 160 ms: 90 %, 204 / 264 ms | no | 320 ms: 90 %, 437 / 587 ms | no |
| `lora-sf11-bw500` | 320 ms: 90 %, 428 / 568 ms | no | no | no | no | no |

Over 100 % occupancy but inside the plan's delivery and delay marks over one 30 s spurt (the queue overflows; a longer spurt loses more):

- `lora-sf8-bw125/1200bps/160ms/direct`: 103 %, 99.5 % delivered, p95 880 ms
- `lora-sf8-bw250/2400bps/80ms/direct`: 103 %, 98.4 % delivered, p95 449 ms
- `lora-sf8-bw500/2400bps/80ms/relayed`: 103 %, 98.4 % delivered, p95 530 ms

### All cases

#### Packet every 80 ms (packet sizes 700: 30 B, 1200: 34 B, 2400: 46 B)

| Setting | 700 direct | 700 relayed | 1200 direct | 1200 relayed | 2400 direct | 2400 relayed |
|---|---|---|---|---|---|---|
| `lora-sf7-bw125` | 90 % · 92 / 112 ms | **fail** 180 % · 57 % · 893 ms | 96 % · 97 / 117 ms | **fail** 193 % · 53 % · 956 ms | **fail** 116 % · 87 % · 504 ms | **fail** 231 % · 45 % · 1152 ms |
| `lora-sf8-bw125` | **fail** 154 % · 66 % · 648 ms | **fail** 308 % · 34 % · 1512 ms | **fail** 167 % · 61 % · 698 ms | **fail** 334 % · 31 % · 1633 ms | **fail** 205 % · 50 % · 865 ms | **fail** 411 % · 26 % · 2014 ms |
| `lora-sf9-bw125` | **fail** 283 % · 37 % · 1164 ms | **fail** 566 % · 19 % · 2748 ms | **fail** 308 % · 34 % · 1265 ms | **fail** 617 % · 18 % · 2992 ms | **fail** 385 % · 27 % · 1584 ms | **fail** 771 % · 14 % · 3741 ms |
| `lora-sf10-bw125` | **fail** 566 % · 19 % · 2294 ms | **fail** 1132 % · 10 % · 5464 ms | **fail** 566 % · 19 % · 2294 ms | **fail** 1132 % · 10 % · 5464 ms | **fail** 719 % · 15 % · 2919 ms | **fail** 1439 % · 8 % · 6943 ms |
| `lora-sf11-bw125` | **fail** 1132 % · 10 % · 4557 ms | **fail** 2263 % · 6 % · 10893 ms | **fail** 1234 % · 9 % · 4958 ms | **fail** 2468 % · 5 % · 11867 ms | **fail** 1541 % · 8 % · 6210 ms | **fail** 3082 % · 5 % · 14834 ms |
| `lora-sf7-bw250` | 45 % · 56 / 76 ms | 90 % · 92 / 112 ms | 48 % · 58 / 78 ms | 96 % · 97 / 117 ms | 58 % · 76 / 106 ms | **fail** 116 % · 88 % · 597 ms |
| `lora-sf8-bw250` | 77 % · 82 / 102 ms | **fail** 154 % · 66 % · 772 ms | 84 % · 87 / 107 ms | **fail** 167 % · 61 % · 835 ms | **fail** 103 % · 98 % · 449 ms | **fail** 205 % · 50 % · 1029 ms |
| `lora-sf9-bw250` | **fail** 141 % · 72 % · 598 ms | **fail** 283 % · 37 % · 1389 ms | **fail** 154 % · 66 % · 648 ms | **fail** 308 % · 34 % · 1512 ms | **fail** 193 % · 53 % · 814 ms | **fail** 385 % · 27 % · 1891 ms |
| `lora-sf10-bw250` | **fail** 283 % · 37 % · 1164 ms | **fail** 566 % · 19 % · 2748 ms | **fail** 283 % · 37 % · 1164 ms | **fail** 566 % · 19 % · 2748 ms | **fail** 360 % · 29 % · 1482 ms | **fail** 719 % · 15 % · 3496 ms |
| `lora-sf11-bw250` | **fail** 515 % · 21 % · 2089 ms | **fail** 1029 % · 11 % · 4967 ms | **fail** 566 % · 19 % · 2294 ms | **fail** 1132 % · 10 % · 5464 ms | **fail** 668 % · 16 % · 2714 ms | **fail** 1336 % · 9 % · 6455 ms |
| `lora-sf7-bw500` | 22 % · 38 / 58 ms | 45 % · 56 / 76 ms | 24 % · 39 / 59 ms | 48 % · 58 / 78 ms | 29 % · 53 / 83 ms | 58 % · 76 / 106 ms |
| `lora-sf8-bw500` | 39 % · 51 / 71 ms | 77 % · 82 / 102 ms | 42 % · 53 / 73 ms | 84 % · 87 / 107 ms | 51 % · 71 / 101 ms | **fail** 103 % · 98 % · 530 ms |
| `lora-sf9-bw500` | 71 % · 77 / 97 ms | **fail** 141 % · 72 % · 710 ms | 77 % · 82 / 102 ms | **fail** 154 % · 66 % · 772 ms | 96 % · 107 / 137 ms | **fail** 193 % · 53 % · 968 ms |
| `lora-sf10-bw500` | **fail** 141 % · 72 % · 598 ms | **fail** 283 % · 37 % · 1389 ms | **fail** 141 % · 72 % · 598 ms | **fail** 283 % · 37 % · 1389 ms | **fail** 180 % · 57 % · 763 ms | **fail** 360 % · 29 % · 1769 ms |
| `lora-sf11-bw500` | **fail** 257 % · 40 % · 1060 ms | **fail** 515 % · 21 % · 2501 ms | **fail** 283 % · 37 % · 1164 ms | **fail** 566 % · 19 % · 2748 ms | **fail** 334 % · 31 % · 1378 ms | **fail** 668 % · 16 % · 3247 ms |

#### Packet every 160 ms (packet sizes 700: 38 B, 1200: 46 B, 2400: 70 B)

| Setting | 700 direct | 700 relayed | 1200 direct | 1200 relayed | 2400 direct | 2400 relayed |
|---|---|---|---|---|---|---|
| `lora-sf7-bw125` | 51 % · 142 / 202 ms | **fail** 103 % · 99 % · 1011 ms | 58 % · 152 / 212 ms | **fail** 116 % · 89 % · 1190 ms | 80 % · 198 / 268 ms | **fail** 160 % · 65 % · 1635 ms |
| `lora-sf8-bw125` | 90 % · 204 / 264 ms | **fail** 180 % · 58 % · 1812 ms | **fail** 103 % · 99 % · 880 ms | **fail** 205 % · 51 % · 2055 ms | **fail** 141 % · 73 % · 1227 ms | **fail** 282 % · 38 % · 2807 ms |
| `lora-sf9-bw125` | **fail** 167 % · 62 % · 1416 ms | **fail** 334 % · 32 % · 3285 ms | **fail** 193 % · 54 % · 1625 ms | **fail** 385 % · 29 % · 3778 ms | **fail** 257 % · 41 % · 2151 ms | **fail** 513 % · 22 % · 5021 ms |
| `lora-sf10-bw125` | **fail** 308 % · 35 % · 2553 ms | **fail** 617 % · 19 % · 6011 ms | **fail** 360 % · 30 % · 2961 ms | **fail** 719 % · 16 % · 6987 ms | **fail** 488 % · 23 % · 3991 ms | **fail** 975 % · 13 % · 9464 ms |
| `lora-sf11-bw125` | **fail** 668 % · 17 % · 5424 ms | **fail** 1336 % · 10 % · 12901 ms | **fail** 771 % · 15 % · 6255 ms | **fail** 1541 % · 9 % · 14877 ms | **fail** 1027 % · 12 % · 8307 ms | **fail** 2053 % · 7 % · 19817 ms |
| `lora-sf7-bw250` | 26 % · 101 / 161 ms | 51 % · 142 / 202 ms | 29 % · 106 / 166 ms | 58 % · 152 / 212 ms | 40 % · 134 / 204 ms | 80 % · 198 / 268 ms |
| `lora-sf8-bw250` | 45 % · 132 / 192 ms | 90 % · 204 / 264 ms | 51 % · 142 / 202 ms | **fail** 103 % · 99 % · 1011 ms | 71 % · 183 / 253 ms | **fail** 141 % · 73 % · 1450 ms |
| `lora-sf9-bw250` | 84 % · 194 / 254 ms | **fail** 167 % · 62 % · 1691 ms | 96 % · 214 / 274 ms | **fail** 193 % · 54 % · 1930 ms | **fail** 128 % · 80 % · 1122 ms | **fail** 257 % · 41 % · 2558 ms |
| `lora-sf10-bw250` | **fail** 154 % · 67 % · 1319 ms | **fail** 308 % · 35 % · 3044 ms | **fail** 180 % · 57 % · 1524 ms | **fail** 360 % · 30 % · 3536 ms | **fail** 244 % · 43 % · 2047 ms | **fail** 488 % · 23 % · 4778 ms |
| `lora-sf11-bw250` | **fail** 283 % · 38 % · 2347 ms | **fail** 566 % · 20 % · 5508 ms | **fail** 334 % · 32 % · 2752 ms | **fail** 668 % · 18 % · 6490 ms | **fail** 436 % · 25 % · 3586 ms | **fail** 873 % · 14 % · 8463 ms |
| `lora-sf7-bw500` | 13 % · 80 / 140 ms | 26 % · 101 / 161 ms | 14 % · 83 / 143 ms | 29 % · 106 / 166 ms | 20 % · 102 / 172 ms | 40 % · 134 / 204 ms |
| `lora-sf8-bw500` | 22 % · 96 / 156 ms | 45 % · 132 / 192 ms | 26 % · 101 / 161 ms | 51 % · 142 / 202 ms | 35 % · 126 / 196 ms | 71 % · 183 / 253 ms |
| `lora-sf9-bw500` | 42 % · 127 / 187 ms | 84 % · 194 / 254 ms | 48 % · 137 / 197 ms | 96 % · 214 / 274 ms | 64 % · 173 / 243 ms | **fail** 128 % · 80 % · 1327 ms |
| `lora-sf10-bw500` | 77 % · 183 / 243 ms | **fail** 154 % · 67 % · 1564 ms | 90 % · 204 / 264 ms | **fail** 180 % · 58 % · 1812 ms | **fail** 122 % · 84 % · 1069 ms | **fail** 244 % · 44 % · 2436 ms |
| `lora-sf11-bw500` | **fail** 141 % · 73 % · 1217 ms | **fail** 283 % · 38 % · 2802 ms | **fail** 167 % · 62 % · 1416 ms | **fail** 334 % · 32 % · 3285 ms | **fail** 218 % · 48 % · 1840 ms | **fail** 436 % · 26 % · 4280 ms |

#### Packet every 320 ms (packet sizes 700: 54 B, 1200: 70 B, 2400: 118 B)

| Setting | 700 direct | 700 relayed | 1200 direct | 1200 relayed | 2400 direct | 2400 relayed |
|---|---|---|---|---|---|---|
| `lora-sf7-bw125` | 32 % · 243 / 383 ms | 64 % · 345 / 485 ms | 40 % · 268 / 408 ms | 80 % · 396 / 536 ms | 62 % · 350 / 500 ms | **fail** 125 % · 84 % · 2576 ms |
| `lora-sf8-bw125` | 58 % · 325 / 465 ms | **fail** 116 % · 91 % · 2389 ms | 71 % · 366 / 506 ms | **fail** 141 % · 76 % · 2892 ms | **fail** 109 % · 96 % · 1907 ms | **fail** 218 % · 51 % · 4380 ms |
| `lora-sf9-bw125` | **fail** 109 % · 96 % · 1895 ms | **fail** 218 % · 51 % · 4363 ms | **fail** 128 % · 82 % · 2240 ms | **fail** 257 % · 44 % · 5117 ms | **fail** 199 % · 54 % · 3394 ms | **fail** 397 % · 30 % · 7846 ms |
| `lora-sf10-bw125` | **fail** 193 % · 56 % · 3268 ms | **fail** 385 % · 31 % · 7585 ms | **fail** 244 % · 46 % · 4085 ms | **fail** 488 % · 26 % · 9547 ms | **fail** 359 % · 32 % · 5948 ms | **fail** 718 % · 19 % · 13963 ms |
| `lora-sf11-bw125` | **fail** 411 % · 29 % · 6766 ms | **fail** 822 % · 17 % · 15942 ms | **fail** 513 % · 24 % · 8390 ms | **fail** 1027 % · 15 % · 19902 ms | **fail** 795 % · 17 % · 12929 ms | **fail** 1590 % · 12 % · 30727 ms |
| `lora-sf7-bw250` | 16 % · 191 / 331 ms | 32 % · 243 / 383 ms | 20 % · 204 / 344 ms | 40 % · 268 / 408 ms | 31 % · 250 / 400 ms | 62 % · 350 / 500 ms |
| `lora-sf8-bw250` | 29 % · 232 / 372 ms | 58 % · 325 / 465 ms | 35 % · 253 / 393 ms | 71 % · 366 / 506 ms | 54 % · 324 / 474 ms | **fail** 109 % · 96 % · 2260 ms |
| `lora-sf9-bw250` | 55 % · 315 / 455 ms | **fail** 109 % · 96 % · 2241 ms | 64 % · 345 / 485 ms | **fail** 128 % · 82 % · 2646 ms | 99 % · 468 / 618 ms | **fail** 199 % · 55 % · 4027 ms |
| `lora-sf10-bw250` | 96 % · 448 / 588 ms | **fail** 193 % · 56 % · 3877 ms | **fail** 122 % · 86 % · 2130 ms | **fail** 244 % · 46 % · 4873 ms | **fail** 180 % · 60 % · 3079 ms | **fail** 359 % · 33 % · 7095 ms |
| `lora-sf11-bw250` | **fail** 180 % · 60 % · 3085 ms | **fail** 360 % · 33 % · 7106 ms | **fail** 218 % · 50 % · 3670 ms | **fail** 436 % · 28 % · 8550 ms | **fail** 333 % · 34 % · 5490 ms | **fail** 667 % · 20 % · 12939 ms |
| `lora-sf7-bw500` | 8 % · 166 / 306 ms | 16 % · 191 / 331 ms | 10 % · 172 / 312 ms | 20 % · 204 / 344 ms | 16 % · 200 / 350 ms | 31 % · 250 / 400 ms |
| `lora-sf8-bw500` | 14 % · 186 / 326 ms | 29 % · 232 / 372 ms | 18 % · 196 / 336 ms | 35 % · 253 / 393 ms | 27 % · 237 / 387 ms | 54 % · 324 / 474 ms |
| `lora-sf9-bw500` | 27 % · 227 / 367 ms | 55 % · 315 / 455 ms | 32 % · 243 / 383 ms | 64 % · 345 / 485 ms | 50 % · 309 / 459 ms | 99 % · 468 / 618 ms |
| `lora-sf10-bw500` | 48 % · 294 / 434 ms | 96 % · 448 / 588 ms | 61 % · 335 / 475 ms | **fail** 122 % · 86 % · 2526 ms | 90 % · 437 / 587 ms | **fail** 180 % · 61 % · 3643 ms |
| `lora-sf11-bw500` | 90 % · 428 / 568 ms | **fail** 180 % · 61 % · 3628 ms | **fail** 109 % · 96 % · 1895 ms | **fail** 218 % · 51 % · 4363 ms | **fail** 167 % · 64 % · 2824 ms | **fail** 333 % · 35 % · 6542 ms |

## Assumptions (simulated, not measured)

- Airtime from the Semtech SX126x formula ([profiles.md](profiles.md)); the real radios are not measured yet.
- Perfect channel access: radios never collide, and a radio starts the moment the air is free. No listen-before-talk or channel activity detection time, no TX/RX turnaround, no hop or sync time.
- One relay that forwards every packet the moment it has fully received it, on the same channel and settings. No second relay, no other traffic.
- No packet loss, interference, fading or capture effect (tables). Range and link budget are not modelled at all.
- No acknowledgements or retries; voice is sent once.
- Codec frames come out exactly on time; the phone hands them to the radio with no delay.
- No regulatory limits (dwell time, duty cycle, hopping) are applied.
- The OASA session is already set up: no handshakes or keepalives on the air during the talk spurt.
- Packets are random bytes of the right size; the oasa-sec binaries do not run in this suite (l1-oasa-link covers the link itself).
