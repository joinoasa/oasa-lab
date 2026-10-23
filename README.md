# oasa-lab

The **OASA radio and hardware test lab**: test suites, instrument drivers and the evidence checker that let people and AI agents test OASA systems at every level, from pure simulation to real radios on a bench.

Tech tree node **T-BENCH1** (test bench and test software). First user: **R-COM2**, OASA's encrypted link for every radio.

This is **milestone M1**: Tier 0a, no hardware. Any laptop can run it.

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
| `l1-oasa-link` | The encrypted oasa-sec link under loss, reordering, replay and attack (needs `OASA_SEC_BIN_DIR`) |

## How an agent watches a run

`log.jsonl` in the bundle is written and flushed line by line. Start the run, then:

```
tail -f runs/*/log.jsonl
```

Every line is one JSON object with `ts` (RFC 3339 UTC), `event` and fields. Events: `run_start`, `test_start`, `test_end` (with `outcome`), `channel_stats` (every second, then a `final` one) and `run_end`. A test that stalls shows as a `test_start` with no `test_end`; stop the run with Ctrl-C or SIGTERM.

## Where the evidence lands

`runs/<UTC time>-<suite>-<id>/` (`runs/` is git-ignored):

| File | What |
|---|---|
| `results.ctrf.json`, `junit.xml` | Every test, pass or fail ([CTRF](https://ctrf.io) and JUnit) |
| `log.jsonl` | The live log |
| `manifest.json` | Suite, seed, times, git SHA, versions, OS, channel settings, binaries with hashes, markers, expectations |
| `captures/*.pcap` | Every packet the simulated channel forwarded, one file per channel (open in Wireshark) |
| `logs/` | Logs of programs the tests ran |
| `summary.md` | Short human summary, linking each file |
| `SHA256SUMS` | Hashes of all of the above (`sha256sum -c SHA256SUMS` works) |

Beside the folder: `runs/<same name>.check.json`, the checker's verdict. See [docs/checker.md](docs/checker.md).

## The simulated channel

`oasa-lab channel` is a UDP proxy that adds loss, delay, jitter, reordering, duplication, corruption, a rate limit, an MTU and replayed packets, all repeatable from `--seed`. Point endpoint A at the A port and endpoint B at the B port:

```
oasa-lab channel --a 127.0.0.1:5001 --b 127.0.0.1:5002 --loss 0.1 --reorder 0.1 --seed 7 --pcap out.pcap
```

From Python: `from oasa_lab.channel import Channel, ChannelSettings`. Other repos can use either. Details are in the module docstring of [oasa_lab/channel.py](oasa_lab/channel.py). Captures are raw-IPv4 pcap files (link type 101), so Wireshark shows addresses, ports and UDP payload.

## More

- Plan: [docs/r-com2-plan.md](docs/r-com2-plan.md)
- Checker: [docs/checker.md](docs/checker.md)
- Outside software we use: [docs/software-used.md](docs/software-used.md)
- Radios, adapters and waveforms live in [oasa-radio](https://github.com/joinoasa/oasa-radio). Encryption lives in [oasa-sec](https://github.com/joinoasa/oasa-sec).

Status: M1 (lab skeleton). Licensed under [Apache-2.0](LICENSE).
