# R-COM2 plan: one encrypted link for every OASA radio

**Status:** Draft v0.1 · **Owner:** citruscybersec · **Last reviewed:** 2026-10-23

R-COM2 gives every OASA platform (drones, rovers, boats, buoys, sensor nodes, relays, ground stations) one encrypted, authenticated link that runs over any radio. It builds on R-SEC1 (device identity and pairing, done in [oasa-sec](https://github.com/joinoasa/oasa-sec)).

This plan covers three tech tree nodes together:

| Node | What we build | Repo |
|---|---|---|
| **R-COM1** Basic radio link | Off-the-shelf radios carrying bytes, with adapters | [oasa-radio](https://github.com/joinoasa/oasa-radio) |
| **T-BENCH1** Test bench and software | The lab: test levels, drivers, evidence and checker | oasa-lab (this repo) |
| **R-COM2** Encrypted link | The link layer, on every radio above | [oasa-sec](https://github.com/joinoasa/oasa-sec) (crypto), oasa-radio (adapters) |

Later tracks build on it: OASA's own waveforms, and **T-RF1** (RF scanning) for radios that adapt to their surroundings.

---

## 1. The link (in oasa-sec)

Modeled on WireGuard, which is proven on lossy networks, using the Noise handshake we already have.

- **Who may connect:** devices paired in R-SEC1. The trusted-peers list becomes the gate: no entry, no session.
- **Session setup:** a Noise handshake between known identities (about 96 bytes, then 48 back), split into pieces on small radios. Random packets are dropped cheaply before any expensive math.
- **Every packet stands alone,** so loss and reordering don't break the link:
  - a 64-bit counter per direction makes each packet's nonce; it never repeats under a key
  - the counter is sent shortened (2–4 bytes) and rebuilt by the receiver, as DTLS 1.3 does
  - **the 16-byte authentication tag is never shortened** (RFC 8439 forbids it)
  - a 1024-packet replay window drops copies, checked only after the tag verifies
  - overhead: about 21–23 bytes per packet
- **Fresh keys:** a new handshake after a message count or a time limit, set per radio from its airtime.
- **One-to-many:** a group key per epoch for broadcasts (a ground station to its fleet), renewed when membership changes. Commands that must prove their sender also carry a signature.
- **Less revealing traffic:** optional fixed packet sizes and steady send rates per message type.
- **Room to grow:** a version and cipher-suite field, so new algorithms (for example post-quantum) can be added later.
- **Radio-agnostic API:** the link takes bytes in and out. Each radio is a small adapter with its MTU, speed and loss profile.
- **First user:** an encrypted MAVLink proxy between the ground station and the companion computer, with MAVLink signing kept inside it as a second layer.

The detailed spec becomes **OASA-SEC link v0** in oasa-sec, through the normal standards stages.

## 2. Radios (adapters in oasa-radio)

| Size or job | First radios | Why |
|---|---|---|
| Tiny node | LoRa boards (Heltec V3, RAK WisBlock) with RNode or our own firmware | Cheap, low power |
| Small drone, fixed-wing | SiK and mLRS telemetry radios (915 MHz) | Carry any bytes; MAVLink-friendly |
| High rate (video, relay) | WFB-ng on RTL8812AU Wi-Fi adapters | Open and proven on drones |
| SDR test node | ADALM-Pluto | Cheap, runs Linux on the device, open firmware |
| Airborne SDR | bladeRF 2.0 micro (24 g) | Small, open software |
| Bench SDR (later) | LimeSDR Mini 2.0, USRP B210 | More channels, wider bandwidth |
| Listening | RTL-SDR V4L (receive only) | Record what's on the air and check it's unreadable |
| Our own radio (future) | openwifi on Zynq + AD9361 as the reference | The one fully open radio design we found |

Adapter order: UDP (simulation and Wi-Fi) → serial (SiK, mLRS) → LoRa → SDR.

No off-the-shelf link we checked encrypts well enough on its own (for example ExpressLRS can be hijacked, and Meshtastic channel messages can't prove the sender). So OASA's link always runs on top, and a radio's own encryption never counts.

## 3. OASA waveforms (in oasa-radio)

A do-it-ourselves track to learn what's possible: our own physical layer on SDRs, published openly like everything else. To R-COM2 it is just one more radio.

| Stage | What | Done when |
|---|---|---|
| W0 | Simple modem in simulation: FSK, then BPSK/QPSK, sync and preamble | Error rate matches theory across signal levels |
| W1 | The same modem on two Plutos over cable | Same curves on real hardware |
| W2 | Error correction (convolutional, then LDPC) and OFDM | Measured gain over W1 |
| W3 | **Adaptive:** change modulation, coding, bandwidth or channel on the fly when interference shows up | Keeps the link up in a lab interference test |
| W4 | **Fleet-aware:** our own radios share channels or time slots as more of them come into range | No self-interference with several nodes in the lab |

W3 and W4 build on **T-RF1** (radios scanning when idle). Tools: FutureSDR (Rust SDR framework), liquid-dsp (MIT), GNU Radio for quick prototypes, SoapySDR to drive any SDR, openwifi as a full reference.

## 4. The lab (this repo)

### Test levels

| Level | What runs | Hardware |
|---|---|---|
| **L0** | Unit tests and known-answer crypto vectors | None |
| **L1** | Simulated radio channel: loss, delay, reordering, duplication, bandwidth limits, noise. Repeatable from a seed | None |
| **L2** | Real radios joined by coax through attenuators, or inside a shielded box | Bench |
| **L3** | Over the air in the lab space | Bench + antennas |
| **L4** | Field and flight tests | Vehicles |

Every feature passes L0 and L1 in CI before it touches hardware.

### How an agent runs it

- **labgrid** runs the hardware: a coordinator knows every radio, board, power switch and serial console; exporters on the lab hosts share them. We write drivers for the SDRs, attenuators and power control.
- **pytest** runs the tests. One command for people and agents, for example `oasa-lab run l1-link-basic`.
- **Live view:** JSON-lines logs stream during the run, so an agent can watch, spot a stall and stop it.
- **Remote access only over WireGuard.** SDR network tools (SoapyRemote, rtl_tcp, iiod) have no authentication of their own.
- **Hardware jobs run on a private runner.** GitHub warns that self-hosted runners on public repos can be taken over by outside pull requests, so lab hardware jobs use a separate private repo with one-job runners.

### Evidence every run leaves

| File | What |
|---|---|
| `results.ctrf.json` | Every test, pass or fail, with timings (CTRF); also JUnit XML for CI |
| `log.jsonl` | Structured live log |
| `manifest.json` | Git SHAs, firmware hashes, hardware serials, attenuator and radio settings, test seed |
| `*.sigmf-meta` / `*.sigmf-data` | Raw radio recordings (SigMF), with hashes |
| `summary.md` | Short human summary, generated from the files above |
| signature | The whole bundle signed (cosign, in-toto format) |

### The checker (deep verification)

A separate program re-checks every bundle without trusting the run that made it:

1. Signature and hashes match; nothing was changed after the run.
2. The manifest names real, expected versions and settings.
3. Every pass and fail in the results is backed by the log.
4. Recordings contain **no readable payload**: no MAVLink start bytes, no test plaintext, and randomness consistent with ciphertext.
5. Numbers make physical sense: error rates follow signal level, latency is within the radio's limits.
6. Known-bad cases really failed: the attacker test, wrong keys and replayed packets.

A run counts only when the checker passes it. Agents and people review the same bundle, and the summary links to every file.

## 5. Lab tiers

Prices checked October 2026 where marked; others are estimates.

| Tier | What's in it | About |
|---|---|---|
| **0a. Free** | Any laptop. L0 and L1 only: the simulated channel, simulated radios and the full evidence and checker pipeline | $0 |
| **0b. First real radios** | 2× Heltec LoRa V3 ($18–20), RTL-SDR V4L ($37.95), a few SMA cables and fixed attenuators (estimate $30–50), any old laptop or Raspberry Pi | **~$110–130** |
| **1. Bench start** | Adds 2× ADALM-Pluto ($268.10 each), SiK telemetry pair ($58.99), 2× ESP32-S3 boards (about $18), NanoVNA-H4 (about $50), splitters and terminations, Raspberry Pi 5 8 GB lab host | **~$1,300** |
| **2. Automated lab** | Adds a programmable attenuator (Mini-Circuits RCDAT-6000-60, $917), 10 MHz GPS reference (Leo Bodnar Mini GPSDO, $196), bladeRF 2.0 micro ($540), 2× WFB-ng Wi-Fi adapters ($79.99), mLRS kit ($41.99+), shielded box, switchable USB power, managed switch and WireGuard router | **+~$2,400** |
| **3. Full lab** | Adds a spectrum analyzer (Siglent SSA3021X, about $1,400), LimeSDR Mini 2.0 ($630), USRP B210 ($2,387), RFD900x pair ($473.81) | **+~$4,900** |

Itemized list: [lab-kit.md](lab-kit.md).

Tier 0a lets anyone in the world help from day one. Tier 0b is enough for the first real encrypted packets over LoRa.

## 6. Milestones

| # | Milestone | Done when |
|---|---|---|
| M1 | Lab skeleton (Tier 0a) | `oasa-lab run` works; L1 simulated channel; evidence bundle and checker pass in CI |
| M2 | Link v0 in simulation | OASA-SEC link v0 spec drafted; link passes L1 under loss, reordering and replay attacks; checker confirms no plaintext |
| M3 | First real radios (Tier 0b) | Encrypted packets over two LoRa boards on cable; RTL-SDR recording checked as unreadable |
| M4 | Encrypted MAVLink (Tier 1) | PX4/ArduPilot SITL commanded through the encrypted link over SiK radios and over Pluto; R-COM1 and R-COM2 meet the release standard |
| M5 | Automated bench (Tier 2) | Attenuator sweeps run unattended; an agent runs, watches and reports a full suite remotely |
| M6 | OASA waveform W1–W2 | Our own modem on Plutos over cable, measured against theory |

W3–W4 (adaptive and fleet-aware) follow once T-RF1 starts.

## 7. Open questions

1. Claim R-COM1 and T-BENCH1 under the OASA-SEC project (techtree #166), or register a radio project?
2. Default rekey limits per radio type, once we measure real airtime.
3. Which LoRa firmware first: RNode, or our own minimal firmware on RadioLib?
4. Where the lab lives physically, and which hosts are always on.
