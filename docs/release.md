# When a node is done

**Status:** Draft v0.3, 2026-11-05 · **Owner:** citruscybersec

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
| Security review | Of the job service, the job sandbox, the TX interlock and the checker: [security-review.md](security-review.md) | ⬜ written (internal): no high findings; M2 fixed ([#35](https://github.com/joinoasa/oasa-lab/pull/35)), M1 fixed in code ([#37](https://github.com/joinoasa/oasa-lab/pull/37)); the owner applies its firewall on the lab host (`RADIO_ADDRS`, `03-firewall.sh`), then this row is ✅. Low and info findings: [#39](https://github.com/joinoasa/oasa-lab/issues/39) |
| Training guide | From a laptop to a first counted run, and how to read the evidence: [training.md](training.md) | ✅ (this PR) |

## R-SEC1: Device identity, keys, pairing (oasa-sec)

| | Done means | Status |
|---|---|---|
| Build | `cargo build --locked` and CI green; `oasa-pair` works on Linux | ✅ |
| Docs | [Pairing design](https://github.com/joinoasa/oasa-sec/blob/main/docs/oasa-sec-v0-pairing.md) and README | ✅ |
| BOM | None (software) | n/a |
| Test results | Unit and known-answer tests in CI (pairing, wrong code, identity files); the lab's `l1-mavlink-sitl` counted on PX4 and ArduPilot simulators (signed commands work, unsigned and wrong-key ones are ignored); results published | ⬜ CI green; `l1-mavlink-sitl` needs the simulators (not in CI), no counted run published |
| Security review | Of identity storage, the pairing code and handshake, and the signing key handover, in oasa-sec | ✅ internal: [security-review-r-sec1.md](https://github.com/joinoasa/oasa-sec/blob/main/docs/security-review-r-sec1.md); no high; M1 (pairing jam) fixed in [oasa-sec#27](https://github.com/joinoasa/oasa-sec/pull/27), L1 in [#20](https://github.com/joinoasa/oasa-sec/pull/20); lows in [#26](https://github.com/joinoasa/oasa-sec/issues/26) |
| Training guide | Pair a ground station and a drone (or two laptops) and confirm the fingerprints | ✅ [training-r-sec1.md](https://github.com/joinoasa/oasa-sec/blob/main/docs/training-r-sec1.md), [guide-pairing.md](https://github.com/joinoasa/oasa-sec/blob/main/docs/guide-pairing.md) |

## R-COM1: Basic radio link, off the shelf (oasa-radio)

| | Done means | Status |
|---|---|---|
| Build | `cargo build --locked`, CI green; `oasa-radio bridge` with the serial adapter | ✅ |
| Docs | README, [serial framing](https://github.com/joinoasa/oasa-radio/blob/main/docs/serial-framing.md) | ✅ |
| BOM | One radio pair the guide uses, with its cables and attenuators | ⬜ needs a radio pair (SiK telemetry, about $59, is the cheapest) |
| Test results | A counted lab run of an off-the-shelf pair on cable: bytes both ways, loss and latency against its profile | ⬜ blocked on the radio pair |
| Security review | Short: the adapter moves bytes only and never parses or logs them; a radio's own encryption never counts | ⬜ internal review written: [security-review-r-com1.md](https://github.com/joinoasa/oasa-radio/blob/main/docs/security-review-r-com1.md); the adapter is sound. H1 is the SiK radio itself (any radio on the same NETID can change its settings remotely): fix and test with the pair (unique NETID, the bridge checks `ATI5` before starting) |
| Training guide | Wire two radios, run the bridge both ends, send packets | ⬜ |

## R-COM2: Encrypted link, OASA-SEC v0 (oasa-sec, oasa-radio)

Needs R-COM1 and R-SEC1 done first.

| | Done means | Status |
|---|---|---|
| Build | `oasa-link` builds and CI is green | ✅ |
| Docs | [Link spec v0](https://github.com/joinoasa/oasa-sec/blob/main/docs/oasa-sec-v0-link.md) | ✅ |
| BOM | The radios the results used | ⬜ SDR bench: [bench-bom.md](bench-bom.md); the R-COM1 radio pair to add |
| Test results | `l1-oasa-link` counted (loss, reorder, replay, attacker, rekey); the link over at least one real radio counted with ciphertext-only air: `l2-sdr-link` on the SDR bench ([sdr-link.md](sdr-link.md)) and over the R-COM1 radio | ⬜ L1 in CI; **SDR bench counted** ([l2-sdr-link](results/l2-sdr-link-0e21d0f8787f.md)); the R-COM1 radio still to do |
| Security review | Of the link protocol and implementation, in oasa-sec | ✅ internal: [security-review-r-com2.md](https://github.com/joinoasa/oasa-sec/blob/main/docs/security-review-r-com2.md); no high; M1 (crossed sessions never rekeyed) fixed in [oasa-sec#25](https://github.com/joinoasa/oasa-sec/pull/25), I5 in [#24](https://github.com/joinoasa/oasa-sec/pull/24) |
| Training guide | Encrypted MAVLink (or any UDP) between two machines over a radio | ✅ [training-r-com2.md](https://github.com/joinoasa/oasa-sec/blob/main/docs/training-r-com2.md) (SiK steps not yet run on hardware) |

## T-BENCH2: Remote autonomous lab (oasa-lab)

Needs T-BENCH1 done first. One site, run by its owner and AI agents from anywhere; every run leaves checked evidence.

| | Done means | Status |
|---|---|---|
| Build | T-BENCH1's build, plus the remote path working end to end: `cloudflared` up, `deploy/check.sh` passes, the connector connects from claude.ai and an API token reaches `/mcp` and the REST API | ⬜ service installed and checked under T-BENCH1 (32 ok); the tunnel, the connector and Access to re-check when the lab is back online |
| Docs | [remote lab](remote-lab.md), [deploy/README.md](../deploy/README.md), [training-remote.md](training-remote.md) | ✅ |
| BOM | Same as T-BENCH1 plus the lab host and the free accounts the remote path uses (domain, Cloudflare Tunnel and Access, GitHub OAuth app, WireGuard): [bench-bom.md](bench-bom.md) | ✅ |
| Test results | Started remotely, not on the lab PC: `tools/remote_acceptance.py` with an API token through the connector: `l1-channel` and `l2-rx-noise` (bench-rx, re-checked with `--require-hardware`) counted; refused: a transmitting suite whose wiring is not confirmed (`l2-pluto-acceptance`), a run while paused, a pause by a runner. Plus one counted run asked for by the owner in a Claude chat (`via: oauth`). Published in [results/](results/README.md) | ⬜ script passes on the simulated bench (22 of 22) and in `tests/service`; real-lab run waits for the lab to be back online |
| Security review | Covers the remote surface (connector and OAuth, API tokens, Cloudflare Access and Tunnel) for more than one user and for agents running without a person watching | ⬜ T-BENCH1's [review](security-review.md) covers the connector, OAuth, tokens, the Access JWT and the tunnel config; addendum for members, agents and the live Cloudflare settings: [security-review-remote.md](security-review-remote.md) |
| Training guide | From a token (or the owner in Claude) to a counted run started remotely, watched, downloaded and re-checked; what each role may do; stop and pause: [training-remote.md](training-remote.md) | ⬜ every local step run and pasted from real output; the real-lab steps (connector sign-in, Claude Code with a token, Access) to walk once on the lab |

## R-SEC2: Key rotation and device removal (oasa-sec)

Needs R-SEC1 done first.

| | Done means | Status |
|---|---|---|
| Build | `cargo build --locked` and CI green; `oasa-pair remove`, `revoke`, roles and `signer` work on Linux | ✅ |
| Docs | [Rotation spec](https://github.com/joinoasa/oasa-sec/blob/main/docs/oasa-sec-v0-rotation.md) and README | ✅ |
| BOM | None (software) | n/a |
| Test results | Unit, simulated-autopilot and end-to-end tests in CI (removal, revoke over the link, admin votes, rotation, stuck and rollback); the lab's `l1-mavlink-sitl` counted on PX4 and ArduPilot simulators, including the autopilot reboot case in `docs/sitl.md`; results published | ⬜ CI green; `l1-mavlink-sitl` with the lab |
| Security review | Of roles, removal and sync, admin votes and signing-key rotation, in oasa-sec | ⬜ internal review written ([#29](https://github.com/joinoasa/oasa-sec/pull/29)): no high; M1 (signer stops on link loss) fixed in [#33](https://github.com/joinoasa/oasa-sec/pull/33), M2 (rotation request lost on removal) in [#34](https://github.com/joinoasa/oasa-sec/pull/34); follow-ups in [#35](https://github.com/joinoasa/oasa-sec/issues/35). ✅ once those merge |
| Training guide | Roles, remove, revoke over the link, and a signing-key rotation with the stand-in autopilot | ⬜ [#31](https://github.com/joinoasa/oasa-sec/pull/31), every command run verbatim |

## Next

The ordered list of lab runs that closes these nodes is [oasa-lab#49](https://github.com/joinoasa/oasa-lab/issues/49).

1. **T-BENCH1:** apply M1's firewall on the lab host, re-check it, then mark it done (unlocks T-BENCH2).
2. **R-SEC1:** a counted `l1-mavlink-sitl` run published (unlocks R-SEC2 and T-UPD1).
3. **R-SEC2:** the same SITL run; merge #29, #31, #33, #34.
4. **T-BENCH2:** after T-BENCH1, the remote acceptance run, one Claude-chat run, the guide's real-lab steps and the Cloudflare checklist; #47's fixes are required.
5. **R-COM1:** the SiK pair: settings, `l2-sik-link`, BOM and training guide.
6. **R-COM2:** `l2-sik-oasa-link` over the R-COM1 radio, its BOM; done once R-COM1 and R-SEC1 are.
