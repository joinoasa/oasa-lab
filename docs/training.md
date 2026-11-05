# Training: your first runs on the OASA lab

**Status:** v0.1, 2026-11-05 · For new members. About an hour, no radio needed for parts 1 to 3.

By the end you will have run suites on your own laptop, broken a bundle on purpose and watched the checker catch it, and run a suite on the real radio bench from Claude.

## 1. Your laptop (Tier 0a, free)

Needs Python 3.11+ and git (Linux, macOS, or Windows with WSL).

```
git clone https://github.com/joinoasa/oasa-lab && cd oasa-lab
python -m venv .venv && . .venv/bin/activate && pip install -e .
oasa-lab list
oasa-lab run l1-channel
```

**What you should see:** the tests tick by, then

```
tests:   N passed, 0 failed, 0 skipped
checker 1.2: PASS (...)
  1. integrity: PASS ...
  ...
bundle:  runs/<time>-l1-channel-<id>
```

Exit code 0 means the tests passed **and** the checker passed. Either failing gives 1.

## 2. Read a bundle

Open the folder the run printed.

| Open | Look for |
|---|---|
| `summary.md` | The result, the commit, every file |
| `log.jsonl` | One JSON line per event: `test_start`, `test_end`, `channel_stats` every second. `tail -f runs/*/log.jsonl` during a run shows it live |
| `captures/*.pcap` | Every packet the simulated channel carried; open it in Wireshark |
| `manifest.json` | What ran: suite, seed, git SHA, versions, settings |
| `SHA256SUMS` | `sha256sum -c SHA256SUMS` checks every file |
| `../<same name>.check.json` | The checker's verdict, each check with its reason |

The rule: **a result counts only if the checker passed it.** Never report a number you can't point to in a bundle.

## 3. Watch the checker catch things

**Plaintext.** Run the negative control. It sends a marker and MAVLink frames in the clear on purpose:

```
oasa-lab run l1-plaintext-control
```

It passes because the checker **finds** the plaintext. That proves the scan works; every encrypted suite relies on it.

**Tampering.** Change one byte of a bundle and check it again:

```
B=$(ls -d runs/*l1-channel* | tail -1)
printf 'x' >> "$B/log.jsonl"
oasa-lab check "$B"
```

**What you should see:** `integrity: FAIL` naming `log.jsonl`, and exit code 1. Delete that bundle afterwards.

**A hardware suite, simulated.** Every radio suite runs on a simulated bench first:

```
OASA_LAB_SIM=1 oasa-lab run l2-usrp-loopback
```

The checker reports the devices as `SIMULATED` (`radio_evidence: ... 2 SIMULATED`), and `oasa-lab check --require-hardware` refuses such a bundle.

## 4. The real bench, from Claude

The lab's radios (a B200mini and an E310 joined by cable through 60 dB each way, [bench-bom.md](bench-bom.md)) are reached only through the job service. Members with access add the **OASA lab** connector in claude.ai, then ask, for example:

> List the lab suites, then run l2-rx-noise on the bench-rx profile and show me the checker's verdict.

Claude calls `lab_status`, `list_suites`, `run_suite`, `watch_job` and `get_results`. **What you should see:** a job id, live log lines while it runs, then the test counts, each checker result and `"counted": true`.

What you can't do, by design:

- Run anything but a suite from the lab's `main` branch, with a seed and a hardware profile.
- Make a radio transmit unless the owner has set the wiring for that suite in the root-owned lab policy. A transmitting suite that isn't allowed is refused with a message saying why.
- See serial numbers or addresses outside the bundle (bundles stay in the lab's evidence store).

## 5. Write a suite

1. Copy a small suite (`suites/l1-plaintext-control/`) to `suites/l1-yours/`.
2. Edit `suite.toml`: a `description`, and in `expectations` every known-bad case (a wrong key, a replay). The checker fails the run if one is missing or skipped.
3. Use the `lab` fixture: `lab.channel(...)` for a simulated link, `lab.marker()` for a string that must never appear in plaintext, `lab.measure(...)` for any number with limits in `[limits.NAME]`.
4. `oasa-lab run l1-yours`, then a pull request with the checks table from `python3 .oasa/oasa_toml.py run --markdown`.

Hardware suites add `transmits`, `requires`, `wiring` and limits: [hardware-suites.md](hardware-suites.md). Rules: [AGENTS.md](../AGENTS.md).

## Where to go next

- The checker's checks: [checker.md](checker.md)
- How the remote lab is locked down: [remote-lab.md](remote-lab.md)
- Open work: issues labeled `agent-ready`, and [release.md](release.md) for what's left before each node is done
