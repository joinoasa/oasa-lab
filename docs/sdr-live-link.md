# The live link over the SDR bench (continuous radios, low delay)

**Status:** v1.0, 2026-11-07 · **Owner:** citruscybersec · Builds on [sdr-link.md](sdr-link.md) · Suite: `l2-sdr-live-link`

`l2-sdr-link` proved the encrypted link over real RF with a half-duplex relay: one UHD tool at a time, cycles of 14 to 20 s, a handshake of minutes. oasa-radio's live `sdr` adapter (`oasa-radio bridge --radio sdr`, [docs/sdr-adapter.md](https://github.com/joinoasa/oasa-radio/blob/main/docs/sdr-adapter.md)) keeps both radios transmitting and receiving all the time and sends each frame as soon as its packet arrives. `l2-sdr-live-link` makes that a counted run against the control-link targets.

## 1. What it proves

| Claim | How the suite shows it |
|---|---|
| The encrypted link runs live over real RF | Two `oasa-link` processes, Alice (B200mini side) and Bob (E310 side); the handshake and all data cross the cables as frame v1, full duplex, B200mini to E310 at 915 MHz, E310 to B200mini at 920 MHz |
| It is fast | Handshake time; one-way delay median, p99, p99.9 and worst case **per direction**, for 32-byte messages at 100/s each way |
| It loses little | Loss per direction over the same load |
| The radios stay up | Helper reopens (an E310 that stops transmitting is reopened by its helper; a counted run has none) |

Ciphertext on the air, replay, changed-packet and stranger cases stay in `l2-sdr-link`: they test the link program, which is the same here.

## 2. How a run goes

```
 ping ─UDP─ oasa-link Alice ─UDP─ bridge B200 ─FIFOs─ helper B200 ═60 dB═ helper E310 ─FIFOs─ bridge E310 ─UDP─ oasa-link Bob ─UDP─ echo
```

- **Timing.** `ping` sends numbered 32-byte messages with their send time; `echo` (Bob's app) writes its arrival time into each and sends it back; `ping` notes the answer's arrival. All three times come from one host clock, so each message gives the delay of **each direction**, paired by sequence number (not half the round trip, and a late frame is not lost from the pairing). The tables are fixed size: a growing Python dict rehashes at 2/3 of a power of two (349,525 entries), a 7-40 ms stall that once looked like the link's worst case (2026-11-07).
- **Radio helper** (one per radio): moves samples between the radio and two FIFOs; TX as timed bursts a few ms ahead of the radio clock; restarts a burst after a late packet; reopens the radio if it stops. It links UHD (GPL-3.0). **Owner decision 2026-11-07:** its source stays private to the lab. The owner installs a build on the lab host and pins its path and sha256 in `lab.toml` (`[sdr_live] helper`, `helper_sha256`); the suite refuses any other file and starts it as a separate process, as it starts UHD's example tools ([software-used.md](software-used.md)).
- **Bridge:** `oasa-radio bridge --radio sdr`, 250 kbaud QPSK at 1 MS/s, IF 175 kHz, `--decimate 1 --coarse-step 4`, timed bursts.
- **Settings** (defaults in `oasa_lab/livelink.py`, the ones that met every bench target over an hour on 2026-11-07): E310 lead 2.5 ms with the fast restart after a late packet; B200mini lead 2 ms with 250-sample receive packets (`recv_frame_size=1016`); periodic sensor reads off; each radio's helper and bridge on their own CPU (`b200_cpus`, `e310_cpus`); bridges at SCHED_FIFO 30 when the host allows it (recorded either way).
- **FIFOs on a RAM filesystem** (`/dev/shm`): on ext4 every FIFO write can wait for the 5 s journal commit (10-70 ms stalls).
- **Length:** `duration_s` in `[sdr_live]`, 3000 s by default: a counted job runs at most an hour (`lab.job_timeout_seconds`).

**Simulated bench.** The same bridges, joined by two `oasa-radio waveform cable` processes (gain, clock offset, noise), at a fifth of the bench rates so a debug build keeps up; 10 messages a second for 30 s. CI runs it. It checks the plumbing, loss and the handshake; its delays are a debug build's and are logged, not measured against the limits.

## 3. Interlocks

| Rule | How |
|---|---|
| Wiring confirmed | `transmits = true`, `wiring = "usrp"`, the TX gate (`confirm_wiring`); each radio's `require_tx` before its helper starts |
| TX gain cap | The radio's `max_tx_gain` from `lab.toml`; the helper also refuses more than its own cap (B200mini 46, E310 40 dB). `[sdr_live]` may not pass `--tx-gain`, `--max-seconds` or the device options to the helper |
| TX time cap | The helper's `--max-seconds` = run length + 120 s, refused above the radio's `max_tx_seconds` (owner 2026-11-07: 90000 s for the two cabled radios) |
| Pinned binary | Helper path and sha256 from `lab.toml`; oasa-radio and oasa-sec as for the other suites; all recorded in the manifest |
| One owner of the hardware | The radios are opened through the `hw` fixture; helpers, bridges and links run inside the job and are stopped when the test ends |

## 4. Measures and limits

| Measure | Limit | Bench, 1 h, 2026-11-07 (unencrypted 32-byte frames) |
|---|---|---|
| `handshake_s` | ≤ 2 s | 0.04-0.05 s |
| `loss_a2b_pct`, `loss_b2a_pct` | < 0.1 % | 0.035 % (both ways together) |
| `one_way_p50_{a2b,b2a}_ms` | ≤ 6 (bench gate; 5 ms is for the dev boards) | 5.14 / 4.89 |
| `one_way_p99_*` | ≤ 10 | 6.04 / 5.54 |
| `one_way_p999_*` | ≤ 15 | 6.38 / 7.09 |
| `one_way_max_*` | ≤ 20 (the worst 10-minute window) | round trip ≤ 21.6 |
| `reopens` | 0 | 0 |

The delay limits are `optional` only so the simulated bench can leave them unmeasured; on hardware the test fails if any is missing. With oasa-link each message is 22 bytes longer on the air than on the bench runs above (about 0.35 ms at 250 kbaud QPSK).

## 5. Known issues

- **The E310 over the network.** In 2026-11-06 runs the E310 stopped transmitting after minutes; its internal Ethernet driver was patched (lab notes) and its helper reopens the radio. The patched module and the E310's IRQ placement (internal link on CPU1) are E310 settings the owner applies; they are not part of this repo.
- **Real-time priority.** The job unit sets `RestrictRealtime=yes`; without an owner drop-in (`RestrictRealtime=no`, `LimitRTPRIO`) the bridges and helpers run without SCHED_FIFO, which the bench showed costs late packets. The run records `bridge_rt_prio` and the helpers' `rt` / `rt_refused` events.
- **Memory over a day.** `ping` keeps fixed tables (four 8-byte numbers per message: 276 MB for 24 h at 100/s) and builds its report from them without per-message Python lists; the job unit's `MemoryMax` is 4 GB.

## 6. The 24-hour soak: `l2-sdr-live-soak` (F11)

The same run as `l2-sdr-live-link` (one code path, `oasa_lab/livelink_run.py`), for `[sdr_live] soak_duration_s` (86400 s by default), with the same radios, cables, settings, interlocks and limits. The limits hold over the whole day: loss and percentiles over every message, the worst case as the largest 10-minute maximum. The report adds an event `live_hours`: per hour, messages sent and back, loss, and each direction's median, p99.9 and worst, so a drift over the day (temperature, a slow leak, a clock running away) shows even when the day as a whole passes. On the simulated bench it runs the same 30-second check as `l2-sdr-live-link`, so CI stays fast.

**How a job may run 24 hours (owner, 2026-11-07).** Every suite keeps the one-hour cap (`lab.job_timeout_seconds`, at most 3600 s). A suite may run longer only when both hold:

1. it is in `LONG_JOB_SUITES` in `oasa_lab/policy.py` (code, reviewed in a PR; today only `l2-sdr-live-soak`), and
2. the owner's root-owned lab policy gives it a time in `[lab] long_jobs` (at most 90000 s = 25 h), e.g. `long_jobs = { "l2-sdr-live-soak" = 90000 }`.

The service then stops that job at its own limit, and `job-run` cuts it 30 s after that itself; every other job is still stopped within the hour. The job unit's `RuntimeMaxSec` (the last backstop, if `job-run` itself got stuck) is 90300 s so it can cover the long job; `deploy/selftest.sh` checks it. The radios' own `max_tx_seconds` in lab.toml (90000 for the two cabled USRPs, owner 2026-11-07) still caps the transmit time of every run: a 24-hour soak needs both, and the helpers' `--max-seconds` is the run plus 120 s.

