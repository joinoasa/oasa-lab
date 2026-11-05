# When a node is done

**Status:** Draft v0.1, 2026-11-05 · **Owner:** citruscybersec

The [release standard](https://joinoasa.org/docs/projects/#release-standard) asks for six things: a working build, docs someone else can follow, a bill of materials (hardware), test results, a security review (anything that communicates) and a training guide. This page says what each one means, node by node, for the nodes this lab tests, and where each stands. A lead marks a node `status:done` on its [techtree](https://github.com/joinoasa/techtree) issue when every row is ticked.

Rules for every node:

- **Test results** means counted runs (the checker passed) that anyone can look at: a summary in [results/](results/) with the job id, commit, binary hashes and every measurement, with serial numbers and addresses removed. The full bundles stay in the lab's evidence store.
- **Security review** means a written review in the repo: what was looked at, every finding with its fix or why it is accepted, and what is left as a known risk. A review written by an AI agent counts as an *internal* review and says so; an outside review is better and can come later. Findings marked high are fixed before release.
- **Training guide** means a new member can follow it from nothing to a passing run, with the commands and what they should see.

## T-BENCH1: Test bench and test software (oasa-lab)

| | Done means | Status |
|---|---|---|
| Build | `pip install -e .` and CI green on `main`; the job service installs from `deploy/` on a fresh Ubuntu 24.04 VM and `deploy/check.sh` passes | ✅ CI green; service installed and checked (32 ok) |
| Docs | README, [hardware suites](hardware-suites.md), [remote lab](remote-lab.md), [checker](checker.md) | ✅ |
| BOM | The bench as built, with parts, prices and what each does: [bench-bom.md](bench-bom.md) | ✅ (this PR) |
| Test results | Counted runs on real hardware for every L2 suite the bench has gear for (`l2-rx-noise`, `l2-usrp-loopback`, `l2-waveform-w1`, `l2-e310-two-channel`), published in [results/](results/); L0–L1 green in CI | ✅ four counted hardware runs in [results/](results/README.md); L0–L1 green in CI |
| Security review | Of the job service, the job sandbox, the TX interlock and the checker: [security-review.md](security-review.md) | ⬜ written (internal): no high findings; M1 and M2 to fix first |
| Training guide | From a laptop to a first counted run, and how to read the evidence: [training.md](training.md) | ✅ (this PR) |

## R-SEC1: Device identity, keys, pairing (oasa-sec)

| | Done means | Status |
|---|---|---|
| Build | `cargo build --locked` and CI green; `oasa-pair` works on Linux | ✅ |
| Docs | [Pairing design](https://github.com/joinoasa/oasa-sec/blob/main/docs/oasa-sec-v0-pairing.md) and README | ✅ |
| BOM | None (software) | n/a |
| Test results | Unit and known-answer tests in CI (pairing, wrong code, identity files); the lab's `l1-mavlink-sitl` counted on PX4 and ArduPilot simulators (signed commands work, unsigned and wrong-key ones are ignored); results published | ⬜ CI green; `l1-mavlink-sitl` needs the simulators (not in CI), no counted run published |
| Security review | Of identity storage, the pairing code and handshake, and the signing key handover, in oasa-sec | ⬜ |
| Training guide | Pair a ground station and a drone (or two laptops) and confirm the fingerprints | ⬜ |

## R-COM1: Basic radio link, off the shelf (oasa-radio)

| | Done means | Status |
|---|---|---|
| Build | `cargo build --locked`, CI green; `oasa-radio bridge` with the serial adapter | ✅ |
| Docs | README, [serial framing](https://github.com/joinoasa/oasa-radio/blob/main/docs/serial-framing.md) | ✅ |
| BOM | One radio pair the guide uses, with its cables and attenuators | ⬜ needs a radio pair (SiK telemetry, about $59, is the cheapest) |
| Test results | A counted lab run of an off-the-shelf pair on cable: bytes both ways, loss and latency against its profile | ⬜ blocked on the radio pair |
| Security review | Short: the adapter moves bytes only and never parses or logs them; a radio's own encryption never counts | ⬜ |
| Training guide | Wire two radios, run the bridge both ends, send packets | ⬜ |

## R-COM2: Encrypted link, OASA-SEC v0 (oasa-sec, oasa-radio)

Needs R-COM1 and R-SEC1 done first.

| | Done means | Status |
|---|---|---|
| Build | `oasa-link` builds and CI is green | ✅ |
| Docs | [Link spec v0](https://github.com/joinoasa/oasa-sec/blob/main/docs/oasa-sec-v0-link.md) | ✅ |
| BOM | The radios the results used | ⬜ |
| Test results | `l1-oasa-link` counted (loss, reorder, replay, attacker, rekey); the link over at least one real radio counted with ciphertext-only air: `l2-sdr-link` on the SDR bench ([sdr-link.md](sdr-link.md)) and over the R-COM1 radio | ⬜ L1 in CI; L2 not built yet |
| Security review | Of the link protocol and implementation, in oasa-sec | ⬜ |
| Training guide | Encrypted MAVLink (or any UDP) between two machines over a radio | ⬜ |

## Next

1. T-BENCH1: fix the review's M1 (radios reachable from the service) and M2 (old commits under today's wiring confirmations). Then the owner marks it done, which unlocks T-BENCH2 (the remote autonomous lab, largely built already).
2. R-COM2: `l2-sdr-link` (in progress).
3. R-SEC1: security review and guide in oasa-sec.
4. R-COM1: needs a radio pair.
