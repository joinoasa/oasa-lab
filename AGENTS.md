# AGENTS.md: working in oasa-lab

Read by AI coding agents (Claude Code, Codex, Cursor and others) and useful for people too. This repo is the **OASA test lab**: test suites, instrument and radio drivers, and the evidence checker (tech tree node T-BENCH1).

## Rules

Follow the [OASA software standard](https://joinoasa.org/docs/software-standard/). In this repo especially:

- **Every run leaves evidence:** a result file (CTRF JSON), live logs (JSON lines), a manifest (git SHAs, firmware hashes, hardware serials, settings) and any radio captures (SigMF). Never report a result you can't point to in a run's evidence.
- **The checker decides, not the test.** A run counts only after the independent checker re-verifies its evidence.
- **Radios over cable or in the shielded box** unless a test plan says otherwise.
- **Lab hardware is reached over WireGuard by maintainers; everything else only through the authenticated job service (`oasa-lab serve`).** SDR control ports (iiod, rtl_tcp, SoapyRemote) have no authentication and are never exposed.
- **No secrets in results or logs.** Keys, codes and passwords never appear in evidence.

## Commands

| Command | What it does |
|---|---|
| `pip install -e .` | Install (Python 3.11+) |
| `oasa-lab list` | List suites |
| `oasa-lab run SUITE [--seed N] [--out DIR]` | Run a suite, write the evidence bundle to `runs/`, run the checker. Exit 1 if a test or the checker fails |
| `oasa-lab check BUNDLE_DIR [--require-hardware]` | Run the independent checker only; writes `BUNDLE_DIR.check.json`. `--require-hardware` rejects simulated devices |
| `oasa-lab devices [--sim]` | Open every device in `lab.toml` and print what it reports |
| `oasa-lab run SUITE --confirm-wiring NAME` | Needed to transmit on real hardware (see [hardware suites](docs/hardware-suites.md)) |
| `OASA_LAB_SIM=1 oasa-lab run l2-pluto-acceptance` | Run a hardware suite on the simulated bench |
| `oasa-lab channel --help` | Run the simulated channel standalone |
| `pip install -e '.[service]'` | Add the job service and Claude connector |
| `oasa-lab serve --config service.toml` | Run the remote job service (localhost only); `--check` only checks the config |
| `oasa-lab token NAME --user USER` | Make an API token (shown once; the config keeps only its hash) |
| `pytest -o addopts="" tests/service` | Job service tests (needs the `service` extra; CI runs them) |
| `tail -f runs/*/log.jsonl` | Watch a run live |
| `pytest -p oasa_lab.pytest_plugin suites/l0-lab` | A suite without a bundle (quick local loop) |

Run `l0-lab`, `l1-channel`, `l1-plaintext-control`, `l1-profiles`, and with `OASA_LAB_SIM=1` `l2-pluto-acceptance` and `l2-rf-control` before every pull request; CI does the same (it also runs `l1-oasa-link` and `l2-lora-link` with the oasa-sec and oasa-radio binaries). Details: [README](README.md), [checker](docs/checker.md), [software used](docs/software-used.md).

## Adding a suite or test

- A suite is `suites/<name>/` with a `suite.toml` (description, `expectations`, `[captures]` flags) and pytest files. Use the `lab` fixture: `lab.channel(...)`, `lab.marker()`, `lab.file()`, `lab.binary()`.
- List known-bad cases (attacker, wrong key, replay) in `expectations`. The checker fails the run if one is missing or skipped.
- A hardware suite adds to `suite.toml`: `transmits = true` if it radiates, `requires = ["pluto", ...]` (missing hardware skips it), `wiring = "NAME"`, and `[limits.METRIC]` with `min`/`max` (`optional = true` if it may stay unmeasured). Take numbers with `lab.measure(...)` so the checker can re-apply the limits; get devices from the `hw` fixture; save IQ with `lab.sigmf(...)`. Every device needs a simulated backend so CI can run the suite.
- Never transmit without at least 60 dB between transmitters (40 dB toward a receive-only device). Put the loss of every path in `lab.example.toml`; the checker fails a run below the limit.
- A hardware driver's library must be permissive. LGPL or GPL tools are allowed as optional extras or separate processes only, never shipped, and flagged in [docs/software-used.md](docs/software-used.md).
- Mark captures `encrypted = true` only if the traffic is ciphertext. Add a plaintext control for any new scanner rule.
- The checker (`oasa_lab/checker/`) imports the standard library only. Keep it that way. It has its own copy of the airtime formulas (`checker/air.py`); change both together (a test compares them).
- A radio profile (`oasa_lab/profiles.py`) needs a source for every number in [docs/profiles.md](docs/profiles.md); mark anything unconfirmed *est.* and say why.
- Any new outside package goes in [docs/software-used.md](docs/software-used.md) (permissive licenses only).

## The job service

- `oasa_lab/service/` runs suites for remote callers. Docs: [docs/remote-lab.md](docs/remote-lab.md). Setup scripts: `deploy/`.
- **Never run anything a caller sends.** Only a suite name from the checked-out repo, a seed and a hardware profile from the config. Keep it that way; a test covers the bad inputs.
- A suite that puts RF on a cable or the air sets `transmits = true` in `suite.toml`. The service reads it with the same parser as the runner and refuses it unless the lab is `cabled` or `shielded`, and, on real hardware, the owner has set that suite's wiring in `[confirm_wiring]`. Never add a second parser for `transmits` or `requires`.
- No secrets in the repo, the audit log or job environments. Tokens are stored as hashes. `deploy/*.example` hold placeholders only.
- A change to auth, the interlock or the audit log needs a test in `tests/service/`.

## Pull requests

- Branch, then open a pull request into `main`. CI must pass.
- One topic per pull request. Say what changed and what you checked.
- Commit with your GitHub no-reply email.

Writing style: short, clear, not wordy.
