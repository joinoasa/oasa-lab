# The live link over the SDR bench (continuous radios, low delay)

**Status:** Draft v0.1, 2026-11-06 · **Owner:** citruscybersec · Builds on [sdr-link.md](sdr-link.md)

`l2-sdr-link` proved the encrypted link over real RF with a half-duplex relay: one UHD tool at a time, cycles of 14 to 20 s, a handshake of minutes. Since then oasa-radio has a live `sdr` adapter (`oasa-radio bridge --radio sdr`, [docs/sdr-adapter.md](https://github.com/joinoasa/oasa-radio/blob/main/docs/sdr-adapter.md)): both radios transmit and receive all the time, each frame goes out as soon as its packet arrives, and on the bench the round trip is about 12 ms. This plan makes that a counted suite, `l2-sdr-live-link`.

## 1. What it proves

| Claim | How the suite shows it |
|---|---|
| The encrypted link runs live over real RF | Two `oasa-link` processes, Alice (B200mini side) and Bob (E310 side); the handshake and all data cross the cables as frame v1, full duplex, one frequency per direction |
| It is fast | Handshake time; one-way delay p50, p99 and max over a fixed packet load (32-byte messages at 100/s each way) |
| It loses little | Delivery each way over the same load |
| The air carries only ciphertext | Each bridge records what it received (`--rx-record`); the frames decoded from it go into the air capture (`[captures] encrypted = true`), as in `l2-sdr-link` |

Replay, changed-packet and stranger cases stay in `l2-sdr-link`: they test the link, which is the same program here.

## 2. How the bench runs

```
 app A ─UDP─ oasa-link Alice ─UDP─ bridge A ─FIFOs─ helper B200 ═60 dB═ helper E310 ─FIFOs─ bridge B ─UDP─ oasa-link Bob ─UDP─ app B
```

- **Radio helper** (one per radio): moves samples between the radio and two FIFOs. TX: each frame gets a radio timestamp a few ms ahead of the radio's clock, the transmitter streams zeros in between (the B200mini's output takes tens of ms to come up after idle). RX: continuous. It enforces the radio's TX gain cap and stops by itself after `--max-seconds`. It is a separate program in `radio-helper/` (C++ on UHD). The lab never links UHD into its own code; it starts the helper as a process, as it starts UHD's example tools today.
- **Bridge**: `oasa-radio bridge --radio sdr --tx-mode burst`, 250 kbaud QPSK at 1 MS/s, IF 175 kHz, `--coarse-step 4`.
- **FIFOs on a RAM filesystem.** On ext4 every FIFO write goes through the journal and stalls every 5 s (10 to 70 ms; found 2026-11-06).
- **Real-time priority** if the host allows it (`ulimit -r`); the suite records whether it had it.

**Simulated bench.** The same bridges joined by `oasa-radio waveform cable` (gain, clock offset, noise) instead of helpers and radios. CI runs it; it shows the software alone (no airtime, no lead).

## 3. Interlocks

The helper is a transmitter the lab starts, so it gets the same rules as the tone and file tools:

| Rule | How |
|---|---|
| Wiring confirmed | `transmits = true`, `wiring = "usrp"`, the TX gate (`confirm_wiring`) as for every transmitting suite |
| TX gain cap | `Usrp.max_tx_gain` from `lab.toml`; the helper refuses a higher gain and checks the gain the radio reports |
| TX time cap | `--max-seconds` = the radio's `max_tx_seconds` (60 s by default): the run carries 50 s of traffic. A longer soak needs the owner to raise `max_tx_seconds` for these radios |
| One owner of the hardware | The job service opens the radios; the helpers run inside the job and end with it (they exit when the bridge closes its FIFO) |
| Pinned binary | The helper is built from this repo at `LAB_REF` and installed by the owner, like oasa-radio |

## 4. Measures and first limits

| Measure | First limit | Why |
|---|---|---|
| `handshake_s` | ≤ 1 s | Bench: 0.04 to 0.05 s |
| `delivery_a2b`, `delivery_b2a` | ≥ 0.99 | Bench, 3-minute runs: 0.999 and up |
| `one_way_p50_ms` | ≤ 10 | Bench: about 6 |
| `one_way_p99_ms` | ≤ 15 | Bench: about 7.5 |
| `one_way_max_ms` | ≤ 100 | Bench: up to ~80 with FIFOs on disk; tighten after runs with FIFOs in RAM |

The targets OASA aims for (p50 ≤ 5 ms, p99 ≤ 10 ms, max ≤ 20 ms, loss < 0.1 %) are not the pass limits yet: the limits record what this bench does, the targets what a radio must do.

## 5. Known issue: the E310 over the network

In 10-minute runs on 2026-11-06 the E310 stopped transmitting after 3 to 9 minutes in about half the runs, sometimes followed by `RfnocError: OpTimeout` on control calls; once it dropped off the network until power-cycled. The helper notices and reopens the radio (back in about 13 s); a 50 s run usually sees none. A run with a reopen is reported (`reinits`) and judged by its limits like any other.

## 6. Open

- Where the helper lives and its license: proposed `radio-helper/` here, as its own program (owner to confirm).
- `max_tx_seconds` for a 10-minute soak (owner).
- A second USB radio would remove the E310 issue from the control link's numbers.
