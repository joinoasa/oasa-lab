# oasa-lab

The **OASA radio and hardware test lab**: test suites, instrument drivers and the evidence checker that let people and AI agents test OASA systems at every level, from pure simulation to real radios on a bench.

Tech tree node **T-BENCH1** (test bench and test software). First user: **R-COM2**, OASA's encrypted link for every radio.

Tier 0a and 1 need no hardware: any laptop can run them. The `l2-` suites use real radios on cables; they run on a **simulated bench** until the gear is on the table ([docs/hardware-suites.md](docs/hardware-suites.md)).

## Quick start

Needs Python 3.11+ and git.

```
git clone https://github.com/joinoasa/oasa-lab && cd oasa-lab
python -m venv .venv && . .venv/bin/activate && pip install -e .
oasa-lab run l1-channel
```

`oasa-lab list` shows the suites. Every run ends with a short result, the path of its evidence bundle, and exit code 0 only if all tests passed **and** the independent checker passed.

| Suite | What it proves |
|---|---|
| `l0-lab` | The lab itself: channel is repeatable, pcap files are valid, the checker catches tampering |
| `l1-channel` | The simulated radio channel does what its settings say, from a seed |
| `l1-plaintext-control` | Negative control: the checker finds a marker and MAVLink in plaintext |
| `l1-profiles` | Radio profiles (LoRa, SiK, mLRS, WFB-ng): measured airtime and throughput match the model, half-duplex shares one medium, queue drops are counted |
| `l1-oasa-link` | The encrypted oasa-sec link under loss, reordering, replay and attack (needs `OASA_SEC_BIN_DIR`) |
| `l2-pluto-acceptance` | A Pluto+ clone is genuine: tuning range, crystal error, all four ports on cable (Pluto+ and RTL-SDR) |
| `l2-lora-link` | The encrypted link over two LoRa modems on coax; the air shows only ciphertext; an SDR recording matches the airtime (needs `OASA_SEC_BIN_DIR`, `OASA_RADIO_BIN_DIR`) |
| `l2-rf-control` | Negative control: the air check finds a plaintext LoRa packet |

### Hardware suites

`OASA_LAB_SIM=1` runs the `l2-` suites on a simulated bench, no hardware. For the real bench, copy `lab.example.toml` to `lab.toml` (not committed; serial numbers, USB ports, cable losses), then:

```
pip install -e '.[hw]'
oasa-lab devices                                          # does everything open?
oasa-lab run l2-pluto-acceptance --confirm-wiring pluto   # transmits: you confirm the wiring
oasa-lab check --require-hardware runs/<bundle>           # rejects a simulated run
```

A suite whose hardware is missing skips with the reason. A transmitting suite skips until `--confirm-wiring` names its wiring. **Never connect transmitters without at least 60 dB between them.** Wiring, safety and thresholds: [docs/hardware-suites.md](docs/hardware-suites.md).

## How an agent watches a run

`log.jsonl` in the bundle is written and flushed line by line. Start the run, then:

```
tail -f runs/*/log.jsonl
```

Every line is one JSON object with `ts` (RFC 3339 UTC), `event` and fields. Events: `run_start`, `test_start`, `test_end` (with `outcome`), `channel_stats` (every second, then a `final` one) and `run_end`. Tests may add their own, such as `link_metrics` (handshake time and goodput of the oasa-sec link over a radio profile). A test that stalls shows as a `test_start` with no `test_end`; stop the run with Ctrl-C or SIGTERM.

## Where the evidence lands

`runs/<UTC time>-<suite>-<id>/` (`runs/` is git-ignored):

| File | What |
|---|---|
| `results.ctrf.json`, `junit.xml` | Every test, pass or fail ([CTRF](https://ctrf.io) and JUnit) |
| `log.jsonl` | The live log |
| `manifest.json` | Suite, seed, times, git SHA, versions, OS, channel settings, binaries with hashes, markers, expectations; for hardware runs the devices (serials, firmware), RF setup and recordings |
| `captures/*.pcap` | Every packet the simulated channel forwarded, one file per channel (open in Wireshark); `*-air-*.pcap` is what a radio modem delivered |
| `recordings/*.sigmf-meta`, `*.sigmf-data` | Radio recordings ([SigMF](https://sigmf.org)) from the SDRs |
| `logs/` | Logs of programs the tests ran |
| `summary.md` | Short human summary, linking each file |
| `SHA256SUMS` | Hashes of all of the above (`sha256sum -c SHA256SUMS` works) |

Beside the folder: `runs/<same name>.check.json`, the checker's verdict. See [docs/checker.md](docs/checker.md).

## The simulated channel

`oasa-lab channel` is a UDP proxy that adds loss, delay, jitter, reordering, duplication, corruption, a rate limit, an MTU and replayed packets, all repeatable from `--seed`. Point endpoint A at the A port and endpoint B at the B port:

```
oasa-lab channel --a 127.0.0.1:5001 --b 127.0.0.1:5002 --loss 0.1 --reorder 0.1 --seed 7 --pcap out.pcap
```

### Radio profiles

A profile makes the channel behave like a real radio: packets take their airtime, a half-duplex radio shares one medium, a transmit queue can overflow (`--max-queue`), and the radio's packet size limit applies. `oasa-lab profiles` lists them. Settings you pass override the profile's:

```
oasa-lab profiles
oasa-lab channel --profile lora-sf9-bw125 --loss 0.05 --pcap out.pcap
```

From a test: `lab.channel(a=a.addr, b=b.addr, profile="lora-sf9-bw125", loss=0.05)`. The manifest records the profile and the resolved settings, and the checker recomputes airtime from them. Numbers and sources: [docs/profiles.md](docs/profiles.md).

From Python: `from oasa_lab.channel import Channel, ChannelSettings`. Other repos can use either. Details are in the module docstring of [oasa_lab/channel.py](oasa_lab/channel.py). Captures are raw-IPv4 pcap files (link type 101), so Wireshark shows addresses, ports and UDP payload.

## Run it remotely, from a Claude chat

`oasa-lab serve` is a small job service for a lab PC with radios on it. It runs the lab's suites **one job at a time**, from a clean checkout of `main`, and every run leaves the usual evidence bundle and checker verdict. Claude (a custom connector), scripts and a terminal use the same queue.

```
pip install -e '.[service]'
oasa-lab token owner-laptop --user citruscybersec   # an API token; paste the printed block in service.toml
oasa-lab serve --config service.toml                    # localhost only
```

- Only suite names from the repo run. No code, shell or paths.
- Suites that transmit (`transmits = true` in `suite.toml`) are refused unless the lab policy (`/etc/oasa-lab/lab-policy.toml`, root-owned) says `rf_setup = "cabled"` or `"shielded"`. On real hardware you also confirm each suite's wiring there (`[confirm_wiring]`); `oasa-lab job-run` checks the same file before any suite code runs and then passes `--confirm-wiring`.
- Stop a job, or pause the whole lab, any time. Every request is in an audit log.
- Sign-in for Claude is GitHub, owner only. The lab PC dials out through a Cloudflare Tunnel and opens no inbound port.

How it fits together, threat model, using it from a chat, stopping everything: [docs/remote-lab.md](docs/remote-lab.md). One-time lab PC setup (scripts for Ubuntu 24.04): [deploy/README.md](deploy/README.md).

## More

- Plan: [docs/r-com2-plan.md](docs/r-com2-plan.md)
- Hardware suites: [docs/hardware-suites.md](docs/hardware-suites.md)
- Checker: [docs/checker.md](docs/checker.md)
- Remote lab (job service, Claude connector): [docs/remote-lab.md](docs/remote-lab.md), setup in [deploy/](deploy/README.md)
- Radio profiles: [docs/profiles.md](docs/profiles.md)
- Outside software we use: [docs/software-used.md](docs/software-used.md)
- Radios, adapters and waveforms live in [oasa-radio](https://github.com/joinoasa/oasa-radio). Encryption lives in [oasa-sec](https://github.com/joinoasa/oasa-sec).

Status: M1 (lab skeleton) plus the first hardware suites, tested on the simulated bench only. Licensed under [Apache-2.0](LICENSE).
