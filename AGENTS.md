# AGENTS.md: working in oasa-lab

Read by AI coding agents (Claude Code, Codex, Cursor and others) and useful for people too. This repo is the **OASA test lab**: test suites, instrument and radio drivers, and the evidence checker (tech tree node T-BENCH1).

## Rules

Follow the [OASA software standard](https://joinoasa.org/docs/software-standard/). In this repo especially:

- **Every run leaves evidence:** a result file (CTRF JSON), live logs (JSON lines), a manifest (git SHAs, firmware hashes, hardware serials, settings) and any radio captures (SigMF). Never report a result you can't point to in a run's evidence.
- **The checker decides, not the test.** A run counts only after the independent checker re-verifies its evidence.
- **Radios over cable or in the shielded box** unless a test plan says otherwise.
- **Lab hardware is reached only over WireGuard.** SDR network tools have no authentication; never expose them.
- **No secrets in results or logs.** Keys, codes and passwords never appear in evidence.

## Commands

| Command | What it does |
|---|---|
| `pip install -e .` | Install (Python 3.11+) |
| `oasa-lab list` | List suites |
| `oasa-lab run SUITE [--seed N] [--out DIR]` | Run a suite, write the evidence bundle to `runs/`, run the checker. Exit 1 if a test or the checker fails |
| `oasa-lab check BUNDLE_DIR` | Run the independent checker only; writes `BUNDLE_DIR.check.json` |
| `oasa-lab channel --help` | Run the simulated channel standalone |
| `tail -f runs/*/log.jsonl` | Watch a run live |
| `pytest -p oasa_lab.pytest_plugin suites/l0-lab` | A suite without a bundle (quick local loop) |

Run `l0-lab`, `l1-channel` and `l1-plaintext-control` before every pull request; CI does the same. Details: [README](README.md), [checker](docs/checker.md), [software used](docs/software-used.md).

## Adding a suite or test

- A suite is `suites/<name>/` with a `suite.toml` (description, `expectations`, `[captures]` flags) and pytest files. Use the `lab` fixture: `lab.channel(...)`, `lab.marker()`, `lab.file()`, `lab.binary()`.
- List known-bad cases (attacker, wrong key, replay) in `expectations`. The checker fails the run if one is missing or skipped.
- Mark captures `encrypted = true` only if the traffic is ciphertext. Add a plaintext control for any new scanner rule.
- The checker (`oasa_lab/checker/`) imports the standard library only. Keep it that way.
- Any new outside package goes in [docs/software-used.md](docs/software-used.md) (permissive licenses only).

## Pull requests

- Branch, then open a pull request into `main`. CI must pass.
- One topic per pull request. Say what changed and what you checked.
- Commit with your GitHub no-reply email.

Writing style: short, clear, not wordy.
