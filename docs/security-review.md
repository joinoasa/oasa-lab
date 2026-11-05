# Security review: the job service, the job sandbox, the TX interlock and the checker

**Status:** v0.1, 2026-11-05 · **Kind:** internal review, written by an AI agent (Claude) from the code at `066418e`; not an outside review · **Owner:** citruscybersec

Part of T-BENCH1's release ([release.md](release.md)). The design and its threat table are in [remote-lab.md](remote-lab.md); this page records what an independent read of the code found.

## Scope and method

The code and deploy files only: `oasa_lab/service/` (every module), `policy.py`, `jobrunner.py`, `suites.py`, `gitsafe.py`, `hw.py`, the checker's file handling, all of `deploy/` (units, polkit rule, udev, scripts, examples), `docs/remote-lab.md`, `pyproject.toml`, the lock files, CI and dependabot, and the auth handlers of the installed MCP SDK (2.3.0). Nothing was run against the network or the lab. Findings were checked against the code before being written here.

## Findings

No high-severity finding. Two medium findings are to be fixed before T-BENCH1 is marked done.

| ID | Severity | Finding | Fix | Status |
|---|---|---|---|---|
| M1 | Medium | **The service (or any local process) can reach the networked radios directly.** The E310 (UHD) and a Pluto (iiod) take unauthenticated network commands. The service unit allows IPv4/IPv6 with no address filter (`deploy/systemd/oasa-lab.service`, `RestrictAddressFamilies`), and the firewall's output policy is accept. Code execution in the internet-facing service could transmit at any gain without a job, so without the interlock, `--confirm-wiring` or the antenna caps. [remote-lab.md](remote-lab.md) says a taken-over service cannot make a *job* transmit, which is true, but this path is not in its table. | Firewall: on the lab interface, allow output only for the job user (and root); `IPAddressDeny` for the lab subnet on the service and on cloudflared; move the presence probe into a job-user unit or drop it; add the row to the threat table. | Open |
| M2 | Medium | **A runner can choose any older commit on `main`.** `POST /api/jobs` accepts `commit` for the runner role, and `min_commit` defaults to empty. The TX gate, the per-device antenna caps and the gain clamp are code in that checkout, while `[confirm_wiring]` is keyed by suite name only. So an old commit with a weaker gate runs under today's confirmations. | `05-service.sh` sets `min_commit` to the installed commit; `commit` becomes admin-only. Later, bind a confirmation to the suite file's hash. | Open |
| L1 | Low | job-run checks a radiating suite against the policy's caps as a whole, not per device; the per-device rules are checkout code. With `devices_file` empty on real hardware, a committed `lab.toml` could define the wiring and losses the checker trusts. | job-run: require `devices_file` on real hardware, load it with the installed parser, refuse unless the wiring exists, `rf_setup = "antenna"` matches `radiates`, and every antenna device is capped and a USRP. | Open |
| L2 | Low | The job unit has unrestricted network egress, so merged code can send out what it can read (the device file with serials, the policy). Known in remote-lab.md. | `IPAddressDeny=any`, `IPAddressAllow=` the radios. | Open |
| L3 | Low | Unauthenticated OAuth endpoints (`/register` in the MCP SDK) read the whole request body; large concurrent bodies could exhaust the service's memory (2 GB limit) and restart it, interrupting a job. Read from the code, not tried. | An outer ASGI layer refusing bodies over 64 KB on every route but `/mcp`; a Cloudflare body-size rule. | Open |
| L4 | Low | The audit log is a plain file the service owns (a compromised service can rewrite it), grows with every request including refused ones, and trusts `Cf-Connecting-Ip` from any local client. | Also log to the journal (root-owned); rotate by size; record both addresses. | Open |
| L5 | Low | Cancelled jobs never trigger retention, and there is no per-user submit rate, so submit-then-stop loops fill the disk up to the free-space floor. | Retain on cancel; per-user rate limit. | Open |
| L6 | Low | Bundle downloads and log streams have no concurrency limit; each download re-packs up to 500 MB in the service's tmp. | Pack once when the bundle is collected; a per-user cap. | Open |
| L7 | Low | cloudflared runs as root with no sandbox; its apt key fingerprint is printed, not pinned. | A drop-in with its own user and hardening; pin the key. | Open |
| L8 | Low | The installer runs `pip install git+...@SHA` as root. No repo code runs at install today (no `setup.py`), but a commit could add some. | Build the wheel as an unprivileged user, install by hash. | Open |
| I1 | Info | The service unit has device access and `AF_NETLINK` it does not use. | Drop them; add `PrivateDevices=yes`. | Open |
| I2 | Info | A deploy example holds the owner's real login, numeric id and personal email; AGENTS.md says examples hold placeholders. | Replace with placeholders (owner's call). | Ask the owner |
| I3 | Info | Dependabot may not cover `deploy/requirements.lock`; no pip-audit in CI; actions pinned by tag; `pyjwt` imported directly but only a transitive dependency. | Add the lock to dependabot, pip-audit in CI, pin actions by SHA, declare `pyjwt`. | Open |
| I4 | Info | The checker's copy of `suites/` comes from `git archive`, which honours in-tree `.gitattributes`, so a merged commit could make it differ from what ran. | Build it from `ls-tree`/`cat-file` as `suite_texts` does. | Open |
| I5 | Info | Tokens are not bound to a surface: OAuth tokens for the connector work on the REST API, and API tokens on the connector hostname (which has no Cloudflare Access). Same users either way. | Check the audience per surface. | Open |
| I6 | Info | The connector's tool text says job ids are 12 hex characters; they are 32 (a 12-character prefix is refused). | Fix the text. | Open |
| I7 | Info | A flood of `/authorize` can evict an in-progress sign-in (100 flows kept). Documented; the Cloudflare rate rule mitigates it. | None beyond the rule. | Accepted |

## Checked and found sound

- **API tokens:** 256-bit random, stored as SHA-256, compared in constant time; removing a user revokes access on the next request on every surface.
- **OAuth:** tokens stored hashed (0600); refresh tokens rotate and a reused one revokes its family; 1 h access tokens; single-use 5-minute codes bound to the client; only Claude's exact callback URLs register; PKCE S256; resource checked; consent page with HMAC CSRF token, SameSite=Strict cookie, Origin and Sec-Fetch-Site checks; users matched by numeric GitHub id; CSP and frame denial on pages.
- **Cloudflare Access JWT:** RS256 only; audience, issuer, expiry and issue time required.
- **Transport:** Origin checks on the API, host guard and DNS-rebinding protection on every route, loopback-only bind enforced, no proxy headers, fixed path sets per hostname in the tunnel.
- **Authorization:** roles checked at each route or tool and again in the job manager; pause and resume admin-only.
- **Input validation:** suite names by pattern and existence at the commit; seeds as bounded integers (booleans refused); profiles from the config; commits as 40 hex, on the branch, above the floor; job ids 32 hex anchored; unknown fields refused.
- **No caller input reaches a shell, a free path or suite code:** every subprocess takes an argument list; git runs with hooks and external protocols off and no user or system config; `suite.toml` is read from git objects as size-capped data by the installed parser, never by running the commit.
- **job-run:** strict request parsing; the commit rechecked in the root-owned mirror; the policy file's owner, mode and link status checked; the owner's pause file honoured; one job at a time; an environment built only from the policy; own process group, output cap and timeout.
- **Interlock timing:** the service and job-run decide on the same commit, and job-run decides on the checkout it runs from a fresh read of the policy; a stale service view can only refuse.
- **Job sandbox:** its own user, no capabilities, no new privileges, a syscall filter, read-only system, service state hidden, closed device policy (USB and serial only), memory, task and time limits, the whole control group killed at the end. The polkit rule allows only start and stop of correctly named units.
- **Evidence intake:** the bundle copied by a no-follow directory walk with size, count and depth caps; only the verified copy served; the installed checker run isolated (`-I`, empty environment, timeout); tarballs built from the link-free copy.
- **Secrets:** the GitHub client secret only in a root 0600 file, never passed to jobs, the checker or git; the audit writer scrubs secret-looking keys and drops query strings; job environments hold nothing secret.
- **Deploy:** root-owned venv with hash-pinned dependencies; the program installed at an exact commit; `check.sh` verifies modes and owners; default-deny inbound firewall with automatic rollback; the mirror updated by its own user with object checks.

## Risks that remain by design

- **The owner's settings are trusted:** `rf_setup`, `[confirm_wiring]` and `[antenna]` caps. Antenna caps are TX gain settings, not radiated power (that depends on the antenna and frequency).
- **Merged code runs with the radios.** The radio-layer gate, the caps and the wiring check are code in the commit; job-run's lock is a check before start. They catch mistakes, not malicious merged code. The controls are branch protection, review and `min_commit` (M2).
- **Evidence comes from the code under test.** The checker verifies consistency and limits; it cannot detect fabricated but self-consistent evidence from malicious merged code.
- **Outside parties are trusted:** the owner's GitHub and Claude accounts, Cloudflare, GitHub, Anthropic.
- **Text from runs reaches Claude** (summaries, log lines, stop reasons). Mitigated by the connector's instructions and its narrow tool set.
- **The platform:** systemd, polkit and the kernel.

## Next

Fix M1 and M2 (the firewall part of M1 is a lab firewall change: the owner applies it), then re-check them here and mark T-BENCH1's security row done. The low and info findings go into issues.
