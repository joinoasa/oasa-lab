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
| `l1-mavlink-sitl` | Real PX4 and ArduPilot simulators flown through the encrypted link, plus the drone-side MAVLink guard. Slow, not in CI (see below) |

### Hardware suites

`OASA_LAB_SIM=1` runs the `l2-` suites on a simulated bench, no hardware. For the real bench, copy `lab.example.toml` to `lab.toml` (not committed; serial numbers, USB ports, cable losses), then:

```
pip install -e '.[hw]'
oasa-lab devices                                          # does everything open?
oasa-lab run l2-pluto-acceptance --confirm-wiring pluto   # transmits: you confirm the wiring
oasa-lab check --require-hardware runs/<bundle>           # rejects a simulated run
```

A suite whose hardware is missing skips with the reason. A transmitting suite skips until `--confirm-wiring` names its wiring. **Never connect transmitters without at least 60 dB between them.** Wiring, safety and thresholds: [docs/hardware-suites.md](docs/hardware-suites.md).

## Flying a real autopilot through the link (`l1-mavlink-sitl`)

Autopilot SITL, then the drone-side `oasa-link`, the simulated channel (5% loss, captured), the ground-side `oasa-link`, and a pymavlink ground station. The signing key is paired with `oasa-pair`, so signing runs inside the link. For each of PX4 and ArduPilot it checks telemetry rates, signed commands (and that unsigned and wrong-key ones are ignored), a flight to 10 m and back (all commanded through the link), the guard (a signed `SETUP_SIGNING`, FTP and a signing `PARAM_SET` are dropped; the key is unchanged; an ordinary `PARAM_SET` works), that the captures hold no plaintext, and a **negative control**: the same autopilot over a channel without the link, in which the checker must find MAVLink.

It is too slow for CI (it needs the simulators built, about 20 minutes each, see oasa-sec's `docs/sitl.md`), so the CI matrix does not run it. Run it by hand, about 8 minutes:

```
pip install pymavlink                                  # LGPL, used only by this suite
cargo build --bins                                     # in oasa-sec, a build with the MAVLink guard
export OASA_SEC_BIN_DIR=~/oasa-sec/target/debug
export PX4_DIR=~/PX4-Autopilot                         # built with: make px4_sitl_sih
export ARDUPILOT_DIR=~/ardupilot                       # built with: ./waf configure --board sitl && ./waf copter
oasa-lab run l1-mavlink-sitl
```

It uses fixed local ports (PX4 14540, 14580 and 18570; ArduPilot 5780 to 5783), so run one at a time and stop other simulators first. Without a binary, a simulator or pymavlink every test is skipped with the reason, and the checker fails the run because the guard cases never ran. The manifest records each simulator's git commit and binary hash. Keys and pairing codes stay in a temporary folder, never in the bundle.

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

## More

- Plan: [docs/r-com2-plan.md](docs/r-com2-plan.md)
- Hardware suites: [docs/hardware-suites.md](docs/hardware-suites.md)
- Checker: [docs/checker.md](docs/checker.md)
- Radio profiles: [docs/profiles.md](docs/profiles.md)
- Outside software we use: [docs/software-used.md](docs/software-used.md)
- Radios, adapters and waveforms live in [oasa-radio](https://github.com/joinoasa/oasa-radio). Encryption lives in [oasa-sec](https://github.com/joinoasa/oasa-sec).

Status: M1 (lab skeleton) plus the first hardware suites, tested on the simulated bench only. Licensed under [Apache-2.0](LICENSE).
