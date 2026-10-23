# AGENTS.md: working in oasa-lab

Read by AI coding agents (Claude Code, Codex, Cursor and others) and useful for people too. This repo is the **OASA test lab**: test suites, instrument and radio drivers, and the evidence checker (tech tree node T-BENCH1).

## Rules

Follow the [OASA software standard](https://joinoasa.org/docs/software-standard/). In this repo especially:

- **Every run leaves evidence:** a result file (CTRF JSON), live logs (JSON lines), a manifest (git SHAs, firmware hashes, hardware serials, settings) and any radio captures (SigMF). Never report a result you can't point to in a run's evidence.
- **The checker decides, not the test.** A run counts only after the independent checker re-verifies its evidence.
- **Radios over cable or in the shielded box** unless a test plan says otherwise.
- **Lab hardware is reached only over WireGuard.** SDR network tools have no authentication; never expose them.
- **No secrets in results or logs.** Keys, codes and passwords never appear in evidence.

## Pull requests

- Branch, then open a pull request into `main`. CI must pass.
- One topic per pull request. Say what changed and what you checked.
- Commit with your GitHub no-reply email.

Writing style: short, clear, not wordy.
