# Crowds on one channel, simulated (T-MSG1)

How many radios can share one area for text and for voice? Suite `l1-crowd` estimates it in simulation ([issue 65](https://github.com/joinoasa/oasa-lab/issues/65)). **Simulated, not measured.** Nothing here says anything about range.

```
oasa-lab run l1-crowd --seed 1
python3 tools/crowd_table.py runs/<bundle>    # prints the tables below
```

## What it does

Two models, both event-driven on a simulated clock (`oasa_lab/crowdsim.py`), repeatable from the seed. The suite runs over 500 cases in about 30 s.

1. **Text setting.** N radios (10, 30, 100, 300) on one long-range LoRa setting. Each sends texts to a random other radio (acknowledged, retried) and broadcasts its position. Blind (send when ready) or listen-before-send (LBT). 0, 1, 2 or 4 voice calls run at the same time: each call is set up on this setting, and while it lasts its two radios are on the voice setting.
2. **Voice setting.** 1, 2 or 4 push-to-talk conversations share one fast setting, direct or through one relay each, blind or LBT. The traffic is `voicesim.VoiceStream` from [l1-voice-lora](voice-lora-sim.md).

The plan has voice and text on **different settings**, and a radio with one receiver can listen to only one at a time. That is an open design question; this suite does not decide it. It runs both ends:

- **One receiver** (main table): a radio in a call hears nothing on the text setting and sends nothing there until the call ends. Texts and positions sent to it are lost unless a retry falls after the call.
- **Two receivers** (4-call comparison): a radio in a call still hears and sends on the text setting.

Not modelled: a radio that hops back to the text setting between talk turns, voice packets that wait for text, a call setup that stays on the voice setting, or the text setting's traffic slowing the voice down (the two settings are separate channels here and never collide with each other).

**Largest N:** the largest crowd size, of 10, 20, 30, 50, 75, 100, 150, 200 and 300, whose text delivery is above 95 %. The in-between sizes run only up to the first grid size (10, 30, 100, 300) that fails above the largest passing one. With calls, small crowds can fail while larger ones pass (a call takes a larger share of a small crowd's radios); the table lists those.

## Numbers and sources

| Number | Value | Source | Status |
|---|---|---|---|
| Text settings | SF9 125 kHz, SF10 250 kHz, SF11 250 kHz; CR 4/5, 8-symbol preamble, explicit header, CRC on | Plan: text, location and call setup on a long-range setting, not yet fixed. Airtime: [profiles.md](profiles.md) | Choice. SF11 250 kHz is Meshtastic's LongFast setting, which uses a 16-symbol preamble (not re-checked here); ours has 8, so our packets are 8 symbols (66 ms) shorter |
| Voice settings | SF7 or SF8 at 250 or 500 kHz; Codec 2 700 bit/s, a packet every 160 ms (4 frames, 38 bytes); one relay at most | Plan | Choice of the 160 ms interval: from [l1-voice-lora](voice-lora-sim.md), it carries 700 bit/s on all four settings, relayed too |
| Link overhead | 22 bytes per packet | oasa-sec link wire v1 (see [voice-lora-sim.md](voice-lora-sim.md)); plan: 21 to 23 | Confirmed |
| Packet sizes | text 60 + 22 = 82 B; position 20 + 22 = 42 B; ACK 4 + 22 = 26 B; call setup 40 + 22 = 62 B | Text: a short message with sender, recipient and message id. Position: latitude, longitude, altitude, time. ACK: the message id | *est.* No message format yet |
| Traffic | `quiet`: 6 texts per radio per hour, a position every 900 s. `pos60`: 6 texts per hour, a position every 60 s. `chatty`: 30 texts per hour, a position every 900 s | Plan: quiet by default, positions rarely when many radios are near, no background traffic. `quiet` is "quiet in a crowd" | Choice. Texts are random in time (Poisson); positions every interval ±5 %, random start |
| No background traffic | no beacons, keepalives, routing or handshakes | Plan: quiet by default | As the plan |
| Hops | text and positions are not relayed: one hop, everyone in range | Plan: 2 to 3 hops | Not modelled. Each relay hop would send each packet again on the same channel |
| Priority | per radio: ACK, call setup, text, position | Plan: call setup, voice, text, location. Voice is on its own setting here | Choice for the ACK. Priority only orders a radio's own queue; radios do not defer to each other's priority |
| Queue | 16 packets per radio on the text setting; a new packet that finds it full is dropped. A new position replaces one still waiting. Voice: 4 packets (as l1-voice-lora) | No firmware yet | *est.* |
| Text ACK and retries | a text or call setup is acknowledged by its receiver; with no ACK after 2 ACK airtimes + its own airtime + uniform 0 to 5 s, it is sent again, up to 3 sends. After a send the radio listens for the ACK (2 CAD times + an ACK airtime) before sending anything but an ACK | Choice | *est.* Simple; positions and voice are never acknowledged |
| Collision rule | any overlap of two transmissions destroys both, at every receiver. A radio that is sending hears nothing (half-duplex). Packets that only touch both arrive | Card: a stated simple rule | Choice |
| Capture rule (alternative) | at a receiver, a packet survives an overlap if it is at least 6 dB above each overlapping packet. Received power: uniform over 0 to 20 dB per sender-receiver pair, fixed per run | Card: no capture beyond a stated simple rule | *est.* Run on `quiet`, no calls |
| One collision domain | every radio hears every other | Card | Choice. Relayed voice assumes the listener needs the relay but still counts collisions as if everyone hears everyone |
| Hidden carriers (alternative) | 30 % of radio pairs cannot hear each other's carrier, so LBT misses them; every receiver still hears both, so they still collide | Choice: the simplest hidden-node case | *est.* Run on `quiet`, no calls, LBT |
| LBT | channel activity detection (CAD) takes 2.5 symbols (2 symbols plus half a symbol of processing). It sees a transmission on the air when it starts; one that starts during the CAD is missed. Busy: wait uniform 0 to 2^k CAD times (k = 3 + busy count, up to 7), then check again; never gives up. Before the first CAD a packet waits uniform 0 to 8 CAD times (an ACK checks at once). No TX/RX turnaround time | Choice | *est.* Not measured on SX126x |
| Calls | each call slot runs one call after another: two random free radios, set up on the text setting, then 90 s; the next call starts 0 to 10 s after one ends or fails | Choice | *est.* |
| Call setup | the callee takes the call when it sends its ACK; the caller joins when it hears the ACK. A setup with no ACK after 3 sends fails | Choice | *est.* |
| Voice talk | turns of uniform 2 to 10 s, uniform 0.5 to 2 s of silence before the other side talks; 300 s per case | Choice | *est.* |
| Run length | traffic for 30 min (10 h at 10 radios: stretched under 200 radios so small crowds send enough texts), then 300 s to finish | Choice | Choice |
| Mark | text delivery above 95 % | Card | Target |
| Voice mark | 95 % of voice packets delivered, 95th-percentile frame delay under 1 s | Plan (see voice-lora-sim.md) | Target |
| LBT against blind | measured elsewhere at 1.5 to 2.2 times the delivered traffic of sending blind | Plan | Sanity comparison only, not an input |

## What the numbers mean

- **Text delivery:** texts that reach their recipient at least once ÷ texts made (including those dropped from a full queue). A text needs its own send and, for no retry, its ACK to get through.
- **Text delay:** from when the text is made to when the recipient first has it. 95th percentile.
- **Position delivery:** receptions ÷ (positions made × other radios). A replaced or dropped position counts as heard by nobody; a radio in a call misses it.
- **Offered:** the airtime the crowd asks for (texts with one ACK each, positions), before retries, as a share of time. Over 100 % no access rule can carry it.
- **Voice delivery and delay:** as in [voice-lora-sim.md](voice-lora-sim.md): voice packets that reach the listener, and each codec frame's delay from when it is complete to arrival.

## Results

Run `l1-crowd --seed 1`, run id `285d91c8c4ef489196eb3d0fa9bbf19c`, lab commit `748b3dd`, checker PASS. A second run with the same seed (`d0ee3b1c…`) gave the same numbers in every `crowd_*` and measurement line.

**One seed.** Cases near 95 % move with the seed, so read a largest N as ± one size step. Example: `lora-sf9-bw125`, `quiet`, blind, 100 radios delivers 95.0, 93.8 and 92.1 % with seeds 1, 2 and 3.

### In short

- **Listen-before-send matters most.** With no calls and `quiet` traffic, LBT keeps text above 95 % up to 300 radios on SF9 125 kHz and SF10 250 kHz and 200 on SF11 250 kHz; blind, 100, 75 and 30. LBT delivered 2.2 times the collision-free airtime of blind at about 40 % offered load, inside the 1.5 to 2.2 times measured elsewhere; 1.1 to 1.3 times at light load and far more when blind collapses.
- **Text setting.** SF10 250 kHz carries as many radios as SF9 125 kHz (its packets are slightly shorter: 431 against 472 ms for a text). SF11 250 kHz (LongFast-like) takes 1.8 times the airtime and carries about two thirds as many radios with LBT.
- **Positions.** A position every 60 s halves the crowd: LBT 100, 150 and 75 radios. Every 900 s ("quiet in a crowd") costs little. 30 texts per radio per hour is about the same load as positions every 60 s.
- **Calls and the text setting.** With one receiver, any call costs text delivery about in proportion to the share of radios in calls: 10 radios with one call lose 15 to 20 % of texts; 30 radios with one call deliver 93 to 96 %. So a small group misses the mark even with one call, while with `quiet` traffic and LBT, 100 to 300 radios on SF9 or SF10 still pass with up to 2 calls (4 calls: 300 only). With two receivers, LBT delivers 100 % at every size with 4 calls (SF11 at 300 radios: 78 %, the channel's own limit). The losses are missed texts, not airtime: three sends span about 10 to 20 s and a call lasts 90 s.
- **Voice conversations on one fast setting.** One conversation meets the plan on every setting, direct and relayed, except SF8 250 kHz relayed with LBT (75 % occupancy plus the CAD waits: 72 % delivered). Blind, two or more conversations fail everywhere (they collide: 2 conversations on SF7 250 kHz deliver 66 %). With LBT: SF7 500 kHz carries 4 direct or 2 relayed; SF7 250 kHz and SF8 500 kHz carry 2 direct, 1 relayed; SF8 250 kHz carries 1 direct (relayed only blind).
- **Alternatives.** The capture rule helps most where collisions are many (blind, 300 radios on SF10: 53 → 75 %; LBT on SF11: 85 → 96 %). Hidden carriers (30 % of pairs) cut LBT at 300 radios from 100 % to 85 to 87 % on SF9 and SF10, and to 25 % on SF11.

### Evidence for the open questions (not decisions)

- **Which long-range setting for text:** SF10 250 kHz carries the same crowd as SF9 125 kHz; SF11 250 kHz about two thirds of it. Range is not modelled, so this is only the airtime side.
- **Hearing text during a call:** with one receiver and the simple rules here, a call makes its radios miss texts unless retries outlast the call; this decides the mark for groups of 30 or fewer. A second receiver, or retries (or store-and-forward) that span a call, would remove that loss; neither alternative is costed here.
- **Group size limits:** about 200 to 300 radios in one collision domain for `quiet` text with LBT, 75 to 150 with positions every 60 s, and 10 to 100 sending blind. On the voice side, 1 to 4 conversations at once in one area, depending on the fast setting, and only with LBT for more than one.

### Tables

#### Largest N with text delivery above 95 %

| Setting | Traffic | blind, 0 calls | blind, 1 call | blind, 2 calls | blind, 4 calls | LBT, 0 calls | LBT, 1 call | LBT, 2 calls | LBT, 4 calls |
|---|---|---|---|---|---|---|---|---|---|
| `lora-sf9-bw125` | pos60 | 20 | none | none | none | 100 | 150 (not 10, 20, 30) | 100 (not 10, 20, 30, 50) | none |
| `lora-sf9-bw125` | quiet | 100 | none | none | none; two receivers: 75 | 300 | 300 (not 10, 20, 30) | 300 (not 10, 20, 30, 50) | 300 (not 10, 20, 30, 50, 75, 100); two receivers: 300 |
| `lora-sf9-bw125` | chatty | 20 | none | none | none | 100 | 100 (not 10, 20, 30) | none | none |
| `lora-sf10-bw250` | pos60 | 30 | none | none | none | 150 | 150 (not 10, 20, 30) | 150 (not 10, 20, 30, 50) | none |
| `lora-sf10-bw250` | quiet | 75 | none | none | none; two receivers: 75 | 300 | 300 (not 10, 20) | 300 (not 10, 20, 30, 50) | 300 (not 10, 20, 30, 50, 75, 100, 150); two receivers: 300 |
| `lora-sf10-bw250` | chatty | 20 | none | none | none | 100 | 100 (not 10, 20, 30) | 100 (not 10, 20, 30, 50) | none |
| `lora-sf11-bw250` | pos60 | 10 | none | none | none | 75 | none | none | none |
| `lora-sf11-bw250` | quiet | 30 | none | none | none; two receivers: 30 | 200 | 200 (not 10, 20, 30) | 200 (not 10, 20, 30, 50) | none; two receivers: 200 |
| `lora-sf11-bw250` | chatty | 10 | none | none | none | 50 | none | none | none |

#### Offered load (share of airtime asked for, before retries)

| Setting | Traffic | Airtime text / position / ACK | N = 10 | N = 30 | N = 100 | N = 300 |
|---|---|---|---|---|---|---|
| `lora-sf9-bw125` | pos60 | 472 / 288 / 206 ms | 6 % | 18 % | 59 % | 178 % |
| `lora-sf9-bw125` | quiet | 472 / 288 / 206 ms | 1 % | 4 % | 14 % | 43 % |
| `lora-sf9-bw125` | chatty | 472 / 288 / 206 ms | 6 % | 18 % | 60 % | 179 % |
| `lora-sf10-bw250` | pos60 | 431 / 267 / 206 ms | 6 % | 17 % | 55 % | 165 % |
| `lora-sf10-bw250` | quiet | 431 / 267 / 206 ms | 1 % | 4 % | 14 % | 41 % |
| `lora-sf10-bw250` | chatty | 431 / 267 / 206 ms | 6 % | 17 % | 56 % | 168 % |
| `lora-sf11-bw250` | pos60 | 780 / 494 / 371 ms | 10 % | 30 % | 101 % | 304 % |
| `lora-sf11-bw250` | quiet | 780 / 494 / 371 ms | 2 % | 7 % | 25 % | 74 % |
| `lora-sf11-bw250` | chatty | 780 / 494 / 371 ms | 10 % | 30 % | 101 % | 304 % |

#### Text and position, no calls

Cells: text delivery · 95th-percentile text delay · position delivery. **Bold**: text delivery 95 % or less.

| Setting | Traffic | Access | N = 10 | N = 30 | N = 100 | N = 300 |
|---|---|---|---|---|---|---|
| `lora-sf9-bw125` | pos60 | blind | 99.8 % · 5.9 s · pos 89 % | **93.8 % · 8.2 s · pos 71 %** | **47.3 % · 10.2 s · pos 26 %** | **1.3 % · 11.1 s · pos 2 %** |
| `lora-sf9-bw125` | pos60 | LBT | 100.0 % · 0.6 s · pos 100 % | 100.0 % · 1.2 s · pos 98 % | 99.5 % · 6.6 s · pos 89 % | **25.3 % · 97.7 s · pos 9 %** |
| `lora-sf9-bw125` | quiet | blind | 100.0 % · 0.5 s · pos 98 % | 98.3 % · 4.3 s · pos 93 % | 95.0 % · 8.0 s · pos 75 % | **50.6 % · 10.4 s · pos 32 %** |
| `lora-sf9-bw125` | quiet | LBT | 100.0 % · 0.6 s · pos 100 % | 100.0 % · 0.6 s · pos 100 % | 100.0 % · 1.2 s · pos 99 % | 100.0 % · 4.7 s · pos 96 % |
| `lora-sf9-bw125` | chatty | blind | 99.2 % · 5.1 s · pos 88 % | **91.1 % · 7.7 s · pos 71 %** | **22.2 % · 10.7 s · pos 14 %** | **0.2 % · 11.6 s · pos 0 %** |
| `lora-sf9-bw125` | chatty | LBT | 100.0 % · 0.7 s · pos 100 % | 100.0 % · 1.7 s · pos 98 % | 99.2 % · 10.9 s · pos 85 % | **8.6 % · 393.6 s · pos 5 %** |
| `lora-sf10-bw250` | pos60 | blind | 100.0 % · 4.9 s · pos 91 % | 96.8 % · 8.0 s · pos 73 % | **48.0 % · 9.9 s · pos 30 %** | **1.9 % · 11.8 s · pos 2 %** |
| `lora-sf10-bw250` | pos60 | LBT | 100.0 % · 0.7 s · pos 100 % | 100.0 % · 1.0 s · pos 98 % | 99.5 % · 5.9 s · pos 89 % | **28.4 % · 75.9 s · pos 11 %** |
| `lora-sf10-bw250` | quiet | blind | 99.8 % · 0.4 s · pos 99 % | 99.1 % · 3.1 s · pos 94 % | **95.0 % · 6.9 s · pos 82 %** | **52.9 % · 9.0 s · pos 34 %** |
| `lora-sf10-bw250` | quiet | LBT | 100.0 % · 0.5 s · pos 100 % | 100.0 % · 0.5 s · pos 99 % | 100.0 % · 1.3 s · pos 98 % | 100.0 % · 4.9 s · pos 94 % |
| `lora-sf10-bw250` | chatty | blind | 99.4 % · 4.8 s · pos 93 % | **93.5 % · 7.5 s · pos 78 %** | **32.7 % · 10.1 s · pos 18 %** | **0.3 % · 9.4 s · pos 0 %** |
| `lora-sf10-bw250` | chatty | LBT | 100.0 % · 0.7 s · pos 100 % | 100.0 % · 1.4 s · pos 99 % | 99.2 % · 10.9 s · pos 87 % | **9.9 % · 294.4 s · pos 4 %** |
| `lora-sf11-bw250` | pos60 | blind | 98.6 % · 7.5 s · pos 84 % | **83.6 % · 11.6 s · pos 56 %** | **17.6 % · 11.6 s · pos 10 %** | **0.3 % · 14.6 s · pos 0 %** |
| `lora-sf11-bw250` | pos60 | LBT | 100.0 % · 1.6 s · pos 99 % | 100.0 % · 4.5 s · pos 96 % | **83.5 % · 57.7 s · pos 49 %** | **6.3 % · 228.8 s · pos 1 %** |
| `lora-sf11-bw250` | quiet | blind | 99.0 % · 0.8 s · pos 95 % | 98.1 % · 7.6 s · pos 85 % | **82.9 % · 10.3 s · pos 56 %** | **17.8 % · 11.7 s · pos 12 %** |
| `lora-sf11-bw250` | quiet | LBT | 100.0 % · 1.0 s · pos 99 % | 100.0 % · 1.3 s · pos 99 % | 100.0 % · 4.1 s · pos 96 % | **84.8 % · 78.1 s · pos 46 %** |
| `lora-sf11-bw250` | chatty | blind | 96.1 % · 7.6 s · pos 84 % | **69.9 % · 11.5 s · pos 51 %** | **4.4 % · 12.8 s · pos 4 %** | **0.0 % · 14.3 s · pos 0 %** |
| `lora-sf11-bw250` | chatty | LBT | 100.0 % · 2.0 s · pos 99 % | 100.0 % · 5.8 s · pos 97 % | **39.2 % · 496.4 s · pos 17 %** | **1.9 % · 587.1 s · pos 1 %** |

#### Voice calls and the text setting (quiet traffic)

| Setting | Access | Calls | N = 10 | N = 30 | N = 100 | N = 300 |
|---|---|---|---|---|---|---|
| `lora-sf9-bw125` | blind | 0 | 100.0 % | 98.3 % | 95.0 % | **50.6 %** |
| `lora-sf9-bw125` | blind | 1 | **79.6 %, in call 19 %, setup 371/377** | **93.5 %, in call 6 %, setup 121/125** | **91.2 %, in call 2 %, setup 37/38** | **48.1 %, in call 0 %, setup 11/29** |
| `lora-sf9-bw125` | blind | 2 | **70.0 %, in call 37 %, setup 739/753** | **91.5 %, in call 12 %, setup 245/254** | **90.4 %, in call 3 %, setup 73/78** | **50.1 %, in call 1 %, setup 20/62** |
| `lora-sf9-bw125` | blind | 4 | **37.7 %, in call 73 %, setup 1455/1510** | **73.5 %, in call 24 %, setup 480/502** | **84.7 %, in call 7 %, setup 134/158** | **39.1 %, in call 1 %, setup 36/142** |
| `lora-sf9-bw125` | blind | 4 (two receivers) | 99.4 %, in call 74 %, setup 1500/1508 | 99.1 %, in call 24 %, setup 495/510 | **89.5 %, in call 7 %, setup 140/159** | **40.7 %, in call 2 %, setup 49/135** |
| `lora-sf9-bw125` | LBT | 0 | 100.0 % | 100.0 % | 100.0 % | 100.0 % |
| `lora-sf9-bw125` | LBT | 1 | **83.6 %, in call 19 %, setup 377/377** | **94.5 %, in call 6 %, setup 126/126** | 98.5 %, in call 2 %, setup 37/38 | 99.1 %, in call 1 %, setup 17/19 |
| `lora-sf9-bw125` | LBT | 2 | **71.5 %, in call 37 %, setup 753/753** | **89.2 %, in call 12 %, setup 250/251** | 96.7 %, in call 4 %, setup 76/76 | 98.8 %, in call 1 %, setup 36/38 |
| `lora-sf9-bw125` | LBT | 4 | **45.8 %, in call 74 %, setup 1498/1504** | **81.5 %, in call 24 %, setup 500/503** | **93.7 %, in call 7 %, setup 149/152** | 97.1 %, in call 2 %, setup 72/77 |
| `lora-sf9-bw125` | LBT | 4 (two receivers) | 100.0 %, in call 75 %, setup 1506/1506 | 100.0 %, in call 25 %, setup 503/503 | 100.0 %, in call 7 %, setup 152/152 | 100.0 %, in call 2 %, setup 76/76 |
| `lora-sf10-bw250` | blind | 0 | 99.8 % | 99.1 % | **95.0 %** | **52.9 %** |
| `lora-sf10-bw250` | blind | 1 | **82.8 %, in call 19 %, setup 376/377** | **93.9 %, in call 6 %, setup 122/126** | **93.3 %, in call 2 %, setup 32/39** | **52.0 %, in call 0 %, setup 14/30** |
| `lora-sf10-bw250` | blind | 2 | **67.7 %, in call 37 %, setup 742/753** | **88.1 %, in call 12 %, setup 245/251** | **90.3 %, in call 3 %, setup 70/78** | **45.6 %, in call 1 %, setup 23/60** |
| `lora-sf10-bw250` | blind | 4 | **38.3 %, in call 74 %, setup 1468/1504** | **70.6 %, in call 24 %, setup 477/501** | **85.8 %, in call 7 %, setup 135/156** | **45.9 %, in call 2 %, setup 53/122** |
| `lora-sf10-bw250` | blind | 4 (two receivers) | 100.0 %, in call 75 %, setup 1505/1511 | 99.7 %, in call 24 %, setup 500/504 | **94.0 %, in call 7 %, setup 145/155** | **49.1 %, in call 2 %, setup 53/114** |
| `lora-sf10-bw250` | LBT | 0 | 100.0 % | 100.0 % | 100.0 % | 100.0 % |
| `lora-sf10-bw250` | LBT | 1 | **84.7 %, in call 19 %, setup 376/376** | 96.2 %, in call 6 %, setup 126/126 | 97.9 %, in call 2 %, setup 38/38 | 99.3 %, in call 1 %, setup 18/19 |
| `lora-sf10-bw250` | LBT | 2 | **68.9 %, in call 37 %, setup 754/755** | **89.9 %, in call 12 %, setup 250/251** | 96.9 %, in call 4 %, setup 76/76 | 98.2 %, in call 1 %, setup 34/38 |
| `lora-sf10-bw250` | LBT | 4 | **41.9 %, in call 75 %, setup 1504/1508** | **78.1 %, in call 24 %, setup 499/503** | **93.9 %, in call 7 %, setup 150/152** | 97.8 %, in call 2 %, setup 72/76 |
| `lora-sf10-bw250` | LBT | 4 (two receivers) | 100.0 %, in call 75 %, setup 1509/1509 | 100.0 %, in call 25 %, setup 504/504 | 100.0 %, in call 7 %, setup 152/152 | 100.0 %, in call 2 %, setup 75/76 |
| `lora-sf11-bw250` | blind | 0 | 99.0 % | 98.1 % | **82.9 %** | **17.8 %** |
| `lora-sf11-bw250` | blind | 1 | **82.2 %, in call 18 %, setup 368/374** | **93.4 %, in call 6 %, setup 122/126** | **79.9 %, in call 2 %, setup 34/43** | **14.9 %, in call 0 %, setup 5/58** |
| `lora-sf11-bw250` | blind | 2 | **69.1 %, in call 37 %, setup 728/754** | **85.5 %, in call 12 %, setup 237/258** | **74.7 %, in call 3 %, setup 58/86** | **12.6 %, in call 0 %, setup 11/111** |
| `lora-sf11-bw250` | blind | 4 | **37.3 %, in call 72 %, setup 1411/1508** | **74.1 %, in call 23 %, setup 440/512** | **69.9 %, in call 6 %, setup 120/181** | **6.8 %, in call 1 %, setup 11/267** |
| `lora-sf11-bw250` | blind | 4 (two receivers) | 98.8 %, in call 74 %, setup 1486/1516 | 95.8 %, in call 24 %, setup 485/510 | **68.5 %, in call 6 %, setup 115/189** | **8.9 %, in call 1 %, setup 12/263** |
| `lora-sf11-bw250` | LBT | 0 | 100.0 % | 100.0 % | 100.0 % | **84.8 %** |
| `lora-sf11-bw250` | LBT | 1 | **80.8 %, in call 19 %, setup 375/375** | **93.9 %, in call 6 %, setup 125/125** | 98.1 %, in call 2 %, setup 37/38 | **82.3 %, in call 0 %, setup 7/19** |
| `lora-sf11-bw250` | LBT | 2 | **69.9 %, in call 37 %, setup 747/749** | **90.7 %, in call 12 %, setup 248/251** | 97.2 %, in call 3 %, setup 71/76 | **85.5 %, in call 1 %, setup 25/36** |
| `lora-sf11-bw250` | LBT | 4 | **46.8 %, in call 74 %, setup 1484/1497** | **79.5 %, in call 24 %, setup 497/501** | **91.8 %, in call 7 %, setup 141/149** | **84.6 %, in call 2 %, setup 42/72** |
| `lora-sf11-bw250` | LBT | 4 (two receivers) | 100.0 %, in call 74 %, setup 1498/1498 | 100.0 %, in call 24 %, setup 500/500 | 100.0 %, in call 7 %, setup 151/151 | **78.3 %, in call 1 %, setup 46/75** |

#### Voice conversations sharing one fast setting

Cells: offered airtime · voice packets delivered · mean / 95th-percentile frame delay. **Bold**: misses the plan (95 % delivered, under 1 s).

| Setting | Conversations | direct, blind | direct, LBT | relayed, blind | relayed, LBT |
|---|---|---|---|---|---|
| `lora-sf7-bw250` | 1 | 21 % · 100.0 % · 101 / 161 ms | 21 % · 100.0 % · 108 / 170 ms | 43 % · 100.0 % · 142 / 202 ms | 43 % · 100.0 % · 155 / 219 ms |
| `lora-sf7-bw250` | 2 | **43 % · 66.3 % · 101 / 161 ms** | 43 % · 99.6 % · 114 / 177 ms | **85 % · 17.2 % · 142 / 202 ms** | **87 % · 67.7 % · 428 / 1160 ms** |
| `lora-sf7-bw250` | 4 | **85 % · 18.1 % · 101 / 161 ms** | **86 % · 81.0 % · 306 / 861 ms** | **170 % · 3.8 % · 142 / 202 ms** | **169 % · 31.5 % · 1168 / 2904 ms** |
| `lora-sf8-bw250` | 1 | 38 % · 100.0 % · 132 / 192 ms | 37 % · 100.0 % · 145 / 211 ms | 76 % · 100.0 % · 204 / 264 ms | **75 % · 71.5 % · 485 / 1113 ms** |
| `lora-sf8-bw250` | 2 | **77 % · 26.8 % · 132 / 192 ms** | **76 % · 85.0 % · 364 / 1012 ms** | **151 % · 14.0 % · 204 / 264 ms** | **149 % · 32.9 % · 1494 / 3645 ms** |
| `lora-sf8-bw250` | 4 | **149 % · 1.0 % · 132 / 192 ms** | **151 % · 49.3 % · 928 / 2796 ms** | **296 % · 0.4 % · 204 / 264 ms** | **302 % · 15.2 % · 2766 / 6372 ms** |
| `lora-sf7-bw500` | 1 | 11 % · 100.0 % · 80 / 140 ms | 11 % · 100.0 % · 84 / 145 ms | 22 % · 100.0 % · 101 / 161 ms | 21 % · 100.0 % · 108 / 169 ms |
| `lora-sf7-bw500` | 2 | **21 % · 81.5 % · 80 / 140 ms** | 21 % · 99.3 % · 86 / 146 ms | **42 % · 61.6 % · 101 / 161 ms** | 43 % · 96.0 % · 114 / 179 ms |
| `lora-sf7-bw500` | 4 | **43 % · 48.4 % · 80 / 140 ms** | 43 % · 97.3 % · 91 / 160 ms | **86 % · 24.6 % · 101 / 161 ms** | **84 % · 71.6 % · 215 / 602 ms** |
| `lora-sf8-bw500` | 1 | 19 % · 100.0 % · 96 / 156 ms | 19 % · 100.0 % · 102 / 166 ms | 36 % · 100.0 % · 132 / 192 ms | 38 % · 100.0 % · 145 / 208 ms |
| `lora-sf8-bw500` | 2 | **37 % · 69.2 % · 96 / 156 ms** | 38 % · 99.1 % · 108 / 170 ms | **74 % · 31.1 % · 132 / 192 ms** | **77 % · 73.8 % · 310 / 862 ms** |
| `lora-sf8-bw500` | 4 | **74 % · 26.3 % · 96 / 156 ms** | **74 % · 85.2 % · 219 / 619 ms** | **149 % · 7.7 % · 132 / 192 ms** | **149 % · 36.4 % · 941 / 2466 ms** |

#### Alternatives: capture rule and hidden carriers (quiet traffic, no calls)

| Setting | Rule | N = 10 | N = 30 | N = 100 | N = 300 |
|---|---|---|---|---|---|
| `lora-sf9-bw125` | LBT, any overlap (main) | 100.0 % · 0.6 s · pos 100 % | 100.0 % · 0.6 s · pos 100 % | 100.0 % · 1.2 s · pos 99 % | 100.0 % · 4.7 s · pos 96 % |
| `lora-sf9-bw125` | LBT, capture | 100.0 % · 0.6 s · pos 100 % | 100.0 % · 0.6 s · pos 99 % | 100.0 % · 1.3 s · pos 99 % | 100.0 % · 4.7 s · pos 96 % |
| `lora-sf9-bw125` | LBT, hidden carriers 30 % | 100.0 % · 0.6 s · pos 99 % | 100.0 % · 0.9 s · pos 98 % | 99.0 % · 5.3 s · pos 90 % | **85.5 % · 10.7 s · pos 57 %** |
| `lora-sf9-bw125` | blind, any overlap (main) | 100.0 % · 0.5 s · pos 98 % | 98.3 % · 4.3 s · pos 93 % | 95.0 % · 8.0 s · pos 75 % | **50.6 % · 10.4 s · pos 32 %** |
| `lora-sf9-bw125` | blind, capture | 99.9 % · 0.5 s · pos 98 % | 99.8 % · 3.3 s · pos 95 % | 96.8 % · 6.9 s · pos 81 % | **66.3 % · 10.1 s · pos 42 %** |
| `lora-sf10-bw250` | LBT, any overlap (main) | 100.0 % · 0.5 s · pos 100 % | 100.0 % · 0.5 s · pos 99 % | 100.0 % · 1.3 s · pos 98 % | 100.0 % · 4.9 s · pos 94 % |
| `lora-sf10-bw250` | LBT, capture | 100.0 % · 0.5 s · pos 100 % | 100.0 % · 0.5 s · pos 100 % | 100.0 % · 1.0 s · pos 99 % | 100.0 % · 3.8 s · pos 95 % |
| `lora-sf10-bw250` | LBT, hidden carriers 30 % | 100.0 % · 0.5 s · pos 99 % | 100.0 % · 0.9 s · pos 97 % | 98.5 % · 5.4 s · pos 91 % | **86.9 % · 8.9 s · pos 63 %** |
| `lora-sf10-bw250` | blind, any overlap (main) | 99.8 % · 0.4 s · pos 99 % | 99.1 % · 3.1 s · pos 94 % | **95.0 % · 6.9 s · pos 82 %** | **52.9 % · 9.0 s · pos 34 %** |
| `lora-sf10-bw250` | blind, capture | 100.0 % · 0.4 s · pos 98 % | 99.8 % · 0.4 s · pos 95 % | 98.2 % · 6.3 s · pos 83 % | **74.6 % · 9.6 s · pos 45 %** |
| `lora-sf11-bw250` | LBT, any overlap (main) | 100.0 % · 1.0 s · pos 99 % | 100.0 % · 1.3 s · pos 99 % | 100.0 % · 4.1 s · pos 96 % | **84.8 % · 78.1 s · pos 46 %** |
| `lora-sf11-bw250` | LBT, capture | 100.0 % · 1.0 s · pos 100 % | 100.0 % · 1.6 s · pos 99 % | 100.0 % · 3.0 s · pos 99 % | 96.2 % · 53.4 s · pos 70 % |
| `lora-sf11-bw250` | LBT, hidden carriers 30 % | 100.0 % · 1.0 s · pos 98 % | 99.3 % · 4.6 s · pos 95 % | **94.4 % · 10.7 s · pos 80 %** | **24.5 % · 28.0 s · pos 14 %** |
| `lora-sf11-bw250` | blind, any overlap (main) | 99.0 % · 0.8 s · pos 95 % | 98.1 % · 7.6 s · pos 85 % | **82.9 % · 10.3 s · pos 56 %** | **17.8 % · 11.7 s · pos 12 %** |
| `lora-sf11-bw250` | blind, capture | 99.5 % · 0.8 s · pos 97 % | 99.5 % · 5.7 s · pos 93 % | **86.2 % · 10.3 s · pos 64 %** | **33.4 % · 12.6 s · pos 21 %** |

#### Listen-before-send against blind (sanity comparison)

| Setting | Traffic | N | Offered | Clean airtime blind / LBT | LBT ÷ blind |
|---|---|---|---|---|---|
| `lora-sf9-bw125` | pos60 | 100 | 59 % | 16.4 % / 50.6 % | 3.09 |
| `lora-sf9-bw125` | pos60 | 300 | 178 % | 2.4 % / 17.2 % | 7.31 |
| `lora-sf9-bw125` | quiet | 100 | 14 % | 12.1 % / 13.2 % | 1.09 |
| `lora-sf9-bw125` | quiet | 300 | 43 % | 16.6 % / 35.5 % | 2.15 |
| `lora-sf9-bw125` | chatty | 100 | 60 % | 11.1 % / 55.6 % | 5.02 |
| `lora-sf9-bw125` | chatty | 300 | 179 % | 0.3 % / 10.1 % | 39.00 |
| `lora-sf10-bw250` | pos60 | 100 | 55 % | 17.0 % / 46.5 % | 2.74 |
| `lora-sf10-bw250` | pos60 | 300 | 165 % | 3.1 % / 19.5 % | 6.28 |
| `lora-sf10-bw250` | quiet | 100 | 14 % | 11.5 % / 13.1 % | 1.15 |
| `lora-sf10-bw250` | quiet | 300 | 41 % | 16.7 % / 36.1 % | 2.16 |
| `lora-sf10-bw250` | chatty | 100 | 56 % | 14.9 % / 55.7 % | 3.74 |
| `lora-sf10-bw250` | chatty | 300 | 168 % | 0.3 % / 10.9 % | 32.15 |
| `lora-sf11-bw250` | pos60 | 100 | 101 % | 10.4 % / 53.5 % | 5.17 |
| `lora-sf11-bw250` | pos60 | 300 | 304 % | 0.2 % / 5.8 % | 23.20 |
| `lora-sf11-bw250` | quiet | 100 | 25 % | 17.8 % / 23.8 % | 1.33 |
| `lora-sf11-bw250` | quiet | 300 | 74 % | 9.7 % / 52.2 % | 5.38 |
| `lora-sf11-bw250` | chatty | 100 | 101 % | 3.5 % / 30.3 % | 8.71 |
| `lora-sf11-bw250` | chatty | 300 | 304 % | 0.1 % / 3.6 % | 71.80 |

#### All text cases with 1 call (one receiver)

| Setting | Traffic | Access | N = 10 | N = 30 | N = 100 | N = 300 |
|---|---|---|---|---|---|---|
| `lora-sf9-bw125` | pos60 | blind | **80.4 % · 74.0 s · pos 59 %** | **87.9 % · 9.5 s · pos 62 %** | **47.4 % · 10.3 s · pos 25 %** | **1.6 % · 12.7 s · pos 2 %** |
| `lora-sf9-bw125` | pos60 | LBT | **82.6 % · 74.3 s · pos 73 %** | **93.8 % · 18.6 s · pos 90 %** | 99.0 % · 8.5 s · pos 86 % | **26.4 % · 93.6 s · pos 9 %** |
| `lora-sf9-bw125` | quiet | blind | **79.6 % · 75.3 s · pos 75 %** | **93.5 % · 48.5 s · pos 86 %** | **91.2 % · 8.2 s · pos 70 %** | **48.1 % · 9.9 s · pos 29 %** |
| `lora-sf9-bw125` | quiet | LBT | **83.6 % · 65.9 s · pos 82 %** | **94.5 % · 30.0 s · pos 93 %** | 98.5 % · 3.0 s · pos 97 % | 99.1 % · 7.2 s · pos 93 % |
| `lora-sf9-bw125` | chatty | blind | **81.0 % · 73.1 s · pos 72 %** | **82.3 % · 11.0 s · pos 61 %** | **18.5 % · 10.7 s · pos 11 %** | **0.2 % · 8.4 s · pos 0 %** |
| `lora-sf9-bw125` | chatty | LBT | **84.2 % · 74.4 s · pos 81 %** | **94.6 % · 15.9 s · pos 91 %** | 97.0 % · 16.1 s · pos 80 % | **9.0 % · 573.1 s · pos 6 %** |
| `lora-sf10-bw250` | pos60 | blind | **81.1 % · 75.7 s · pos 61 %** | **89.0 % · 10.1 s · pos 63 %** | **51.5 % · 10.4 s · pos 29 %** | **1.6 % · 10.2 s · pos 2 %** |
| `lora-sf10-bw250` | pos60 | LBT | **83.7 % · 75.2 s · pos 74 %** | **93.7 % · 21.2 s · pos 89 %** | 97.7 % · 6.0 s · pos 88 % | **32.0 % · 85.3 s · pos 10 %** |
| `lora-sf10-bw250` | quiet | blind | **82.8 % · 76.3 s · pos 78 %** | **93.9 % · 15.1 s · pos 86 %** | **93.3 % · 8.5 s · pos 71 %** | **52.0 % · 10.2 s · pos 29 %** |
| `lora-sf10-bw250` | quiet | LBT | **84.7 % · 65.9 s · pos 83 %** | 96.2 % · 5.8 s · pos 93 % | 97.9 % · 1.8 s · pos 97 % | 99.3 % · 5.9 s · pos 91 % |
| `lora-sf10-bw250` | chatty | blind | **82.2 % · 70.9 s · pos 71 %** | **82.6 % · 10.8 s · pos 61 %** | **28.4 % · 10.3 s · pos 14 %** | **0.4 % · 12.2 s · pos 0 %** |
| `lora-sf10-bw250` | chatty | LBT | **84.2 % · 75.0 s · pos 81 %** | **93.9 % · 23.5 s · pos 91 %** | 97.7 % · 12.3 s · pos 86 % | **9.3 % · 259.2 s · pos 4 %** |
| `lora-sf11-bw250` | pos60 | blind | **79.1 % · 67.4 s · pos 56 %** | **70.7 % · 13.4 s · pos 46 %** | **15.1 % · 13.4 s · pos 8 %** | **0.1 % · 6.9 s · pos 0 %** |
| `lora-sf11-bw250` | pos60 | LBT | **85.4 % · 68.8 s · pos 73 %** | **94.2 % · 43.5 s · pos 87 %** | **86.3 % · 66.5 s · pos 47 %** | **6.5 % · 317.8 s · pos 2 %** |
| `lora-sf11-bw250` | quiet | blind | **82.2 % · 76.6 s · pos 73 %** | **93.4 % · 11.5 s · pos 79 %** | **79.9 % · 11.6 s · pos 56 %** | **14.9 % · 13.3 s · pos 10 %** |
| `lora-sf11-bw250` | quiet | LBT | **80.8 % · 80.1 s · pos 82 %** | **93.9 % · 39.5 s · pos 93 %** | 98.1 % · 5.8 s · pos 95 % | **82.3 % · 72.2 s · pos 51 %** |
| `lora-sf11-bw250` | chatty | blind | **75.2 % · 73.3 s · pos 64 %** | **61.8 % · 13.4 s · pos 44 %** | **4.7 % · 12.2 s · pos 3 %** | **0.0 % · 0.0 s · pos 0 %** |
| `lora-sf11-bw250` | chatty | LBT | **84.1 % · 80.0 s · pos 79 %** | **94.8 % · 24.0 s · pos 87 %** | **37.9 % · 564.9 s · pos 18 %** | **2.0 % · 493.4 s · pos 1 %** |

#### All text cases with 2 calls (one receiver)

| Setting | Traffic | Access | N = 10 | N = 30 | N = 100 | N = 300 |
|---|---|---|---|---|---|---|
| `lora-sf9-bw125` | pos60 | blind | **59.0 % · 90.9 s · pos 36 %** | **81.3 % · 44.4 s · pos 55 %** | **41.5 % · 9.5 s · pos 24 %** | **1.4 % · 8.6 s · pos 1 %** |
| `lora-sf9-bw125` | pos60 | LBT | **68.4 % · 85.9 s · pos 51 %** | **89.5 % · 59.9 s · pos 81 %** | 95.4 % · 11.5 s · pos 83 % | **23.4 % · 103.1 s · pos 8 %** |
| `lora-sf9-bw125` | quiet | blind | **70.0 % · 86.6 s · pos 60 %** | **91.5 % · 55.8 s · pos 77 %** | **90.4 % · 10.2 s · pos 73 %** | **50.1 % · 10.2 s · pos 27 %** |
| `lora-sf9-bw125` | quiet | LBT | **71.5 % · 86.3 s · pos 64 %** | **89.2 % · 66.6 s · pos 87 %** | 96.7 % · 7.3 s · pos 94 % | 98.8 % · 7.3 s · pos 91 % |
| `lora-sf9-bw125` | chatty | blind | **63.2 % · 88.7 s · pos 49 %** | **74.7 % · 52.2 s · pos 58 %** | **17.7 % · 10.7 s · pos 14 %** | **0.2 % · 8.5 s · pos 0 %** |
| `lora-sf9-bw125` | chatty | LBT | **71.7 % · 88.9 s · pos 64 %** | **88.6 % · 57.9 s · pos 84 %** | **92.0 % · 46.0 s · pos 72 %** | **7.7 % · 437.7 s · pos 5 %** |
| `lora-sf10-bw250` | pos60 | blind | **64.2 % · 86.8 s · pos 36 %** | **84.2 % · 54.3 s · pos 57 %** | **45.9 % · 10.6 s · pos 27 %** | **2.0 % · 9.4 s · pos 2 %** |
| `lora-sf10-bw250` | pos60 | LBT | **70.6 % · 86.2 s · pos 52 %** | **87.4 % · 68.1 s · pos 81 %** | 97.7 % · 7.7 s · pos 85 % | **31.8 % · 79.7 s · pos 12 %** |
| `lora-sf10-bw250` | quiet | blind | **67.7 % · 93.1 s · pos 59 %** | **88.1 % · 45.8 s · pos 78 %** | **90.3 % · 9.4 s · pos 72 %** | **45.6 % · 9.8 s · pos 30 %** |
| `lora-sf10-bw250` | quiet | LBT | **68.9 % · 88.8 s · pos 63 %** | **89.9 % · 66.8 s · pos 87 %** | 96.9 % · 12.6 s · pos 95 % | 98.2 % · 5.3 s · pos 91 % |
| `lora-sf10-bw250` | chatty | blind | **61.9 % · 86.9 s · pos 46 %** | **79.9 % · 51.3 s · pos 55 %** | **26.2 % · 10.6 s · pos 14 %** | **0.2 % · 12.1 s · pos 0 %** |
| `lora-sf10-bw250` | chatty | LBT | **70.4 % · 88.9 s · pos 63 %** | **88.5 % · 60.7 s · pos 85 %** | 95.8 % · 18.4 s · pos 80 % | **9.2 % · 245.4 s · pos 4 %** |
| `lora-sf11-bw250` | pos60 | blind | **60.5 % · 87.4 s · pos 34 %** | **70.1 % · 30.8 s · pos 42 %** | **11.0 % · 14.4 s · pos 8 %** | **0.0 % · 0.0 s · pos 0 %** |
| `lora-sf11-bw250` | pos60 | LBT | **70.3 % · 91.8 s · pos 50 %** | **87.3 % · 68.4 s · pos 78 %** | **84.3 % · 62.2 s · pos 46 %** | **6.2 % · 223.5 s · pos 2 %** |
| `lora-sf11-bw250` | quiet | blind | **69.1 % · 88.3 s · pos 58 %** | **85.5 % · 49.0 s · pos 70 %** | **74.7 % · 12.6 s · pos 54 %** | **12.6 % · 10.9 s · pos 8 %** |
| `lora-sf11-bw250` | quiet | LBT | **69.9 % · 90.0 s · pos 64 %** | **90.7 % · 59.7 s · pos 86 %** | 97.2 % · 6.7 s · pos 93 % | **85.5 % · 56.2 s · pos 53 %** |
| `lora-sf11-bw250` | chatty | blind | **59.7 % · 86.4 s · pos 43 %** | **57.4 % · 32.6 s · pos 36 %** | **3.2 % · 13.7 s · pos 4 %** | **0.0 % · 0.0 s · pos 0 %** |
| `lora-sf11-bw250` | chatty | LBT | **69.8 % · 97.0 s · pos 62 %** | **88.5 % · 69.8 s · pos 83 %** | **36.8 % · 519.6 s · pos 16 %** | **1.4 % · 744.6 s · pos 0 %** |

#### All text cases with 4 calls (one receiver)

| Setting | Traffic | Access | N = 10 | N = 30 | N = 100 | N = 300 |
|---|---|---|---|---|---|---|
| `lora-sf9-bw125` | pos60 | blind | **30.9 % · 134.5 s · pos 7 %** | **70.5 % · 73.4 s · pos 41 %** | **39.8 % · 12.3 s · pos 22 %** | **1.1 % · 11.5 s · pos 1 %** |
| `lora-sf9-bw125` | pos60 | LBT | **48.2 % · 179.4 s · pos 20 %** | **78.9 % · 76.6 s · pos 65 %** | **93.2 % · 44.7 s · pos 79 %** | **23.7 % · 96.3 s · pos 8 %** |
| `lora-sf9-bw125` | quiet | blind | **37.7 % · 161.0 s · pos 24 %** | **73.5 % · 79.6 s · pos 63 %** | **84.7 % · 12.4 s · pos 65 %** | **39.1 % · 10.6 s · pos 26 %** |
| `lora-sf9-bw125` | quiet | LBT | **45.8 % · 175.9 s · pos 33 %** | **81.5 % · 79.3 s · pos 72 %** | **93.7 % · 29.3 s · pos 89 %** | 97.1 % · 9.1 s · pos 89 % |
| `lora-sf9-bw125` | chatty | blind | **33.3 % · 140.1 s · pos 19 %** | **61.2 % · 75.3 s · pos 42 %** | **16.6 % · 11.6 s · pos 12 %** | **0.1 % · 10.6 s · pos 0 %** |
| `lora-sf9-bw125` | chatty | LBT | **47.1 % · 186.6 s · pos 30 %** | **78.5 % · 76.7 s · pos 72 %** | **92.2 % · 47.3 s · pos 71 %** | **7.4 % · 369.2 s · pos 3 %** |
| `lora-sf10-bw250` | pos60 | blind | **31.7 % · 140.4 s · pos 7 %** | **72.7 % · 76.2 s · pos 41 %** | **44.7 % · 11.3 s · pos 23 %** | **1.9 % · 12.5 s · pos 2 %** |
| `lora-sf10-bw250` | pos60 | LBT | **39.1 % · 169.3 s · pos 19 %** | **79.1 % · 82.2 s · pos 65 %** | **92.7 % · 30.6 s · pos 80 %** | **27.0 % · 74.0 s · pos 11 %** |
| `lora-sf10-bw250` | quiet | blind | **38.3 % · 171.6 s · pos 24 %** | **70.6 % · 82.5 s · pos 61 %** | **85.8 % · 25.5 s · pos 65 %** | **45.9 % · 10.5 s · pos 31 %** |
| `lora-sf10-bw250` | quiet | LBT | **41.9 % · 163.9 s · pos 32 %** | **78.1 % · 79.5 s · pos 74 %** | **93.9 % · 33.6 s · pos 91 %** | 97.8 % · 5.9 s · pos 90 % |
| `lora-sf10-bw250` | chatty | blind | **31.7 % · 149.8 s · pos 20 %** | **66.0 % · 73.9 s · pos 43 %** | **22.6 % · 18.7 s · pos 12 %** | **0.3 % · 11.7 s · pos 0 %** |
| `lora-sf10-bw250` | chatty | LBT | **43.5 % · 180.5 s · pos 30 %** | **79.3 % · 74.8 s · pos 72 %** | **92.3 % · 44.5 s · pos 75 %** | **9.6 % · 263.3 s · pos 4 %** |
| `lora-sf11-bw250` | pos60 | blind | **27.2 % · 174.3 s · pos 8 %** | **56.4 % · 77.7 s · pos 32 %** | **8.6 % · 12.5 s · pos 6 %** | **0.3 % · 9.0 s · pos 0 %** |
| `lora-sf11-bw250` | pos60 | LBT | **45.2 % · 187.5 s · pos 19 %** | **79.2 % · 82.0 s · pos 63 %** | **79.2 % · 82.3 s · pos 42 %** | **6.1 % · 242.8 s · pos 2 %** |
| `lora-sf11-bw250` | quiet | blind | **37.3 % · 156.8 s · pos 24 %** | **74.1 % · 88.6 s · pos 58 %** | **69.9 % · 13.0 s · pos 41 %** | **6.8 % · 12.1 s · pos 5 %** |
| `lora-sf11-bw250` | quiet | LBT | **46.8 % · 165.9 s · pos 33 %** | **79.5 % · 85.3 s · pos 73 %** | **91.8 % · 41.1 s · pos 89 %** | **84.6 % · 70.7 s · pos 49 %** |
| `lora-sf11-bw250` | chatty | blind | **26.9 % · 146.8 s · pos 17 %** | **43.3 % · 67.1 s · pos 25 %** | **1.8 % · 12.8 s · pos 1 %** | **0.0 % · 0.0 s · pos 0 %** |
| `lora-sf11-bw250` | chatty | LBT | **45.7 % · 233.1 s · pos 30 %** | **79.5 % · 86.0 s · pos 70 %** | **35.6 % · 655.0 s · pos 14 %** | **1.5 % · 454.3 s · pos 1 %** |

#### All text cases with 4 calls (two receivers)

| Setting | Traffic | Access | N = 10 | N = 30 | N = 100 | N = 300 |
|---|---|---|---|---|---|---|
| `lora-sf9-bw125` | quiet | blind | 99.4 % · 2.5 s · pos 92 % | 99.1 % · 4.7 s · pos 90 % | **89.5 % · 8.3 s · pos 67 %** | **40.7 % · 10.3 s · pos 22 %** |
| `lora-sf9-bw125` | quiet | LBT | 100.0 % · 0.6 s · pos 99 % | 100.0 % · 0.9 s · pos 99 % | 100.0 % · 1.6 s · pos 98 % | 100.0 % · 5.6 s · pos 93 % |
| `lora-sf10-bw250` | quiet | blind | 100.0 % · 2.8 s · pos 96 % | 99.7 % · 5.0 s · pos 91 % | **94.0 % · 6.9 s · pos 77 %** | **49.1 % · 9.0 s · pos 31 %** |
| `lora-sf10-bw250` | quiet | LBT | 100.0 % · 0.5 s · pos 100 % | 100.0 % · 0.5 s · pos 99 % | 100.0 % · 1.4 s · pos 96 % | 100.0 % · 5.8 s · pos 94 % |
| `lora-sf11-bw250` | quiet | blind | 98.8 % · 6.8 s · pos 93 % | 95.8 % · 7.9 s · pos 79 % | **68.5 % · 11.7 s · pos 48 %** | **8.9 % · 12.7 s · pos 6 %** |
| `lora-sf11-bw250` | quiet | LBT | 100.0 % · 1.3 s · pos 99 % | 100.0 % · 2.7 s · pos 98 % | 100.0 % · 4.9 s · pos 96 % | **78.3 % · 89.1 s · pos 48 %** |

## Assumptions (simulated, not measured)

- Airtime from the Semtech SX126x formula ([profiles.md](profiles.md)); the real radios are not measured yet.
- One collision domain: every radio hears every other. No range, path loss, fading or interference beyond the stated capture and hidden-carrier alternatives.
- Collisions: any overlap destroys both packets at every receiver (alternative: the 6 dB capture rule above). Half-duplex: a radio sending hears nothing.
- LBT as stated above (CAD time, what it sees, backoff). No TX/RX turnaround, no frequency hopping, no duty-cycle or dwell-time limits.
- Text and call setup acknowledged with up to 2 retries; positions and voice never. No relaying of text or positions (the plan's 2 to 3 hops are not modelled).
- Message sizes, rates, queue sizes, call lengths and talk turns as in the table: estimates, not from a message format or firmware.
- Voice and text are separate channels that never interfere. A radio in a call either hears nothing on the text setting (one receiver) or everything (two receivers); nothing in between.
- No OASA session handshakes or keepalives on the air. Packets carry no real content; oasa-sec does not run in this suite.
- One seed per case (see above).
