# Training: run the lab from anywhere

**Status:** v0.1, 2026-11-05 · For members with access and the owner. About 45 minutes. Part of T-BENCH2 ([release.md](release.md)).

By the end you will have started a counted run on the lab from far away, watched it live, downloaded its evidence, checked it yourself, and seen what your role may and may not do. [training.md](training.md) covers the lab on your own laptop; [remote-lab.md](remote-lab.md) explains how the remote side is locked down.

Every command below was run on 2026-11-05 against a local `oasa-lab serve` on the simulated bench, and the output is real (trimmed). Steps marked **on the real lab** need the lab PC, its Cloudflare hostnames, GitHub sign-in or real radios.

## Who may do what

| Role | May | May not |
|---|---|---|
| `viewer` | List suites, see the lab and every job, read logs, results and bundles | Run, stop, pause |
| `runner` | All of the above; run a suite (head of `main`, a seed, a profile); stop **own** jobs | Stop others' jobs, pause or resume, pick a commit |
| `admin` (the owner) | Everything: stop any job, pause and resume the lab, run an older commit | Clear the owner's local pause file (only a shell on the PC can) |

Nobody can send code, a shell command, a path, or a commit not on `main`. A suite that transmits runs only if the owner confirmed its wiring in the root-owned lab policy.

## 1. Get access

| You are | You need | From |
|---|---|---|
| The owner, in a Claude chat | The **OASA lab** connector in claude.ai, signed in with GitHub | [deploy/README.md](../deploy/README.md) step 9 (**on the real lab**) |
| A member, in a Claude chat | A `[[users]]` entry with your numeric GitHub id and role; then add the connector | The owner (**on the real lab**) |
| A member or agent with a script | An API token (shown once; the config keeps only its hash); for the REST hostname also Cloudflare Access (your email in its policy, or an Access service token for a robot) | The owner (**on the real lab**) |

The owner makes a token on the lab PC with `sudo -u oasalab /opt/oasa-lab/venv/bin/oasa-lab token NAME --user USER` and restarts the service. Keep your token in a password manager or an environment variable, never in a file you commit.

## 2. Try it on your laptop first

The same service, simulated bench, no radios. Needs Python 3.11+.

```
git clone https://github.com/joinoasa/oasa-lab && cd oasa-lab
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[service]'
oasa-lab token member-laptop --user member
```

```
Token (shown once, keep it secret):
  oasa_…

Add to service.toml:

[[tokens]]
name   = "member-laptop"
user   = "member"
sha256 = "2875a6a6fd414eaf43fb1c6c0b223899e243b57779960a5f9304749498dade64"
```

Make one token each for `owner`, `member` and `viewer`, then write a throwaway `service.toml` (only for trying it out: `sandbox.mode = "none"` runs jobs as you, with no isolation):

```toml
[service]
host = "127.0.0.1"
port = 8742
state_dir = "state"
repo = "/path/to/a/full/clone/of/oasa-lab"   # not a shallow clone: the service fetches main from it
min_free_mb = 100

[sandbox]
mode = "none"

[lab]
rf_setup = "unset"

[inventory]
backend = "simulated"
[[inventory.devices]]
id = "b200"
kind = "b200"
[[inventory.devices]]
id = "e310"
kind = "e310"
[[inventory.devices]]
id = "korad"
kind = "korad"

[profiles.bench-rx]
requires = ["b200", "e310", "korad"]
description = "B200mini and E310 on cable, KORAD supply (receive-only suites)"

[github]
client_id = "unused-locally"

[[users]]
name = "owner"
role = "admin"
github_id = 1
[[users]]
name = "member"
role = "runner"
github_id = 2
[[users]]
name = "viewer"
role = "viewer"
github_id = 3

# then the three [[tokens]] blocks
```

```
oasa-lab serve --config service.toml --check
oasa-lab serve --config service.toml
```

```
oasa-lab: config ok (3 user(s), rf_setup=unset, listening on 127.0.0.1:8742)
oasa-lab: rf_setup is 'unset': suites that transmit will be refused.
```

In another terminal:

```
L=http://127.0.0.1:8742                 # on the real lab: https://<API hostname>
export TOKEN=oasa_…                      # the member token
A="Authorization: Bearer $TOKEN"
```

On the real lab the REST hostname is behind Cloudflare Access: open it once in a browser and sign in with your email, or for a script add `-H "CF-Access-Client-Id: …" -H "CF-Access-Client-Secret: …"` (an Access service token the owner made).

## 3. Check you are in

```
curl -s $L/healthz
curl -s -o /dev/null -w '%{http_code}\n' $L/api/lab
curl -s -H "$A" $L/api/lab
```

```
{"ok":true}
401
{"version":"0.1.0","paused":null,"rf_setup":"unset","transmit_allowed":false,"running":null,"queued":[],
 "devices":[{"id":"b200","kind":"b200","present":true,"detail":"simulated"}, …],
 "profiles":{"bench-rx":{"requires":["b200","e310","korad"],…,"ready":true}},"inventory_backend":"simulated"}
```

No token, no answer. On the real lab `inventory_backend` is `usb`, `rf_setup` is `cabled`, and the devices are the bench's ([bench-bom.md](bench-bom.md)).

## 4. Start a counted run

```
curl -s -H "$A" $L/api/suites            # every suite at the head of main: transmits, requires
curl -s -H "$A" -d '{"suite":"l1-channel","seed":7}' $L/api/jobs
```

```
{"id":"c0b58b1a7d2041bda24263846bb2cb35","suite":"l1-channel","seed":7,"profile":null,
 "requested_by":"member","via":"api-token","commit":"907de9ef7003e7ff03a3dbf43a18831bfc0d71aa",
 "status":"queued","queue_position":1,"transmits":false,"wiring_confirmed":null,
 "links":{"status":"/api/jobs/c0b58b1a…","log":"/api/jobs/c0b58b1a…/log"}, …}
```

Keep the full 32-character `id`. One job runs at a time; others wait in the queue.

## 5. Watch it

```
ID=c0b58b1a7d2041bda24263846bb2cb35
curl -sN -H "$A" "$L/api/jobs/$ID/log?follow=1"
```

```
data: {"ts":"2026-11-05T19:12:11.769Z","event":"run_start","suite":"l1-channel","seed":7, …}

data: {"ts":"2026-11-05T19:12:12.823Z","event":"test_start","test":"suites/l1-channel/test_channel_characterization.py::test_transparent_by_default"}
…
event: end
data: {"status": "passed", "offset": 18556}
```

Live JSON lines (55 for this run), then `event: end`. Without `follow=1` you get the lines so far and an `offset`; pass `?offset=N` next time for only the new ones. `?stream=stdout` shows pytest's own output.

## 6. Get the results and the checker's report

```
curl -s -H "$A" $L/api/jobs/$ID/results
```

```
counted True counts {'total': 16, 'passed': 16, 'failed': 0, 'skipped': 0}
checker oasa-lab-checker 1.2 ok True
  integrity pass - all 23 files match SHA256SUMS; none missing or extra
  manifest pass - all required fields present and sane; 18 channel(s), 18 capture(s)
  results_match_log pass - 16 tests: CTRF, log.jsonl and junit.xml agree (16 passed, 0 failed, 0 skipped)
  no_readable_payload pass - 18 capture(s): 18 not declared encrypted
  numbers_make_sense pass - 18 channel(s): stats match the captures; 150 rates within 5 sigma; …
  known_bad_cases_ran pass - 3 expected test(s) ran and passed (confirmed against the suite file)
  radio_evidence pass - no hardware, recordings or measurements in this run
oasa-lab-checker 1.2, the job service's own copy, run on the evidence after the job ended
```

(Shown summarized; the answer is JSON with the same fields, plus `summary_md` and download links.) **A run counts only if `"counted": true`**: the tests passed and the service's own checker passed the copy of the evidence it took after the job ended.

Download the evidence and check it yourself:

```
curl -s -H "$A" -o check.json $L/api/jobs/$ID/check.json
curl -s -H "$A" -OJ $L/api/jobs/$ID/bundle.tar.gz
tar -xzf 20261105T191211Z-l1-channel-35e72a94.tar.gz
(cd 20261105T191211Z-l1-channel-35e72a94 && sha256sum -c --quiet SHA256SUMS && echo "sha256: all OK")
oasa-lab check 20261105T191211Z-l1-channel-35e72a94
```

```
sha256: all OK
checker 1.2: PASS (20261105T191211Z-l1-channel-35e72a94)
  1. integrity: PASS: all 23 files match SHA256SUMS; none missing or extra
  …
  7. radio_evidence: PASS: no hardware, recordings or measurements in this run
```

Run `oasa-lab check` from a clone at the run's commit, so it confirms the known-bad cases against the same suite file. For a hardware run add `--require-hardware`: it refuses simulated devices. To publish a counted run: `python3 tools/result_summary.py BUNDLE.tar.gz > docs/results/SUITE-JOB12.md` ([results/](results/README.md)).

## 7. A hardware suite

Name a profile from `/api/lab`:

```
curl -s -H "$A" -d '{"suite":"l2-rx-noise","profile":"bench-rx"}' $L/api/jobs
```

On the simulated bench it passes with 3 tests and the checker reports the devices as simulated. **On the real lab** the same call runs on the B200mini, the E310 and the KORAD: receive only, so it needs no wiring confirmation. A suite that transmits (`l2-usrp-loopback`, `l2-waveform-w1`, …) runs only after the owner confirms its wiring (step 8).

## 8. What gets refused, and why

Real answers from the service (the HTTP status in brackets):

| You try | As | The service says |
|---|---|---|
| Run anything | viewer | `your role may not do this` [403] |
| Pick a commit | runner | `only an admin may pick a commit; leave it out to run the head of the branch` [403] |
| Send a path | anyone | `suite must be a suite name (lowercase letters, digits, dashes)` [400] |
| An unknown profile | runner | `unknown hardware profile; known: bench-rx` [400] |
| A transmitting suite, simulated lab with `rf_setup` unset | runner | `refused: suite 'l2-usrp-loopback' transmits, and this lab's rf_setup is 'unset' (must be one of: cabled, shielded)` [403] |
| A transmitting suite, real hardware, wiring not confirmed | runner | `refused: suite 'l2-usrp-loopback' transmits on real hardware. Connect wiring 'usrp' as in docs/hardware-suites.md, then set [confirm_wiring] l2-usrp-loopback = 'usrp' in the lab policy file and restart the service` [403] |
| A suite whose devices are missing | runner | `missing device(s): b200, e310, korad` [409] |
| Stop someone else's job | runner | `your role may not stop this job` [403] |
| Pause the lab | runner | `your role may not do this` [403] |
| Run while the lab is paused | anyone | `the lab is paused by owner: changing cables` [423] |

The real-hardware refusal above came from a second local service with `inventory.backend = "usb"` and `rf_setup = "cabled"`: the same check the lab runs, before any device is touched. Only the owner, as root on the lab PC, can add `[confirm_wiring]` to `/etc/oasa-lab/lab-policy.toml`.

## 9. Stop and pause

Stop your own job (a runner may; an admin may stop any). `J` is the job's id:

```
curl -s -H "$A" -d '{"reason":"wrong seed"}' $L/api/jobs/$J/stop
```

```
… "status":"stopped","stop_reason":"stopped by member: wrong seed" …
```

A stopped run is never counted (`"counted": false`). A job still in the queue becomes `cancelled`.

Pause the lab (admin only): no new runs, the queue is cancelled; add `"stop_running": true` to also stop the running job.

```
OWNER="Authorization: Bearer $OWNER_TOKEN"          # an admin's token
curl -s -H "$OWNER" -d '{"reason":"changing cables"}' $L/api/pause
curl -s -H "$A" -d '{"suite":"l1-channel"}' $L/api/jobs
curl -s -H "$OWNER" -X POST $L/api/resume
```

```
{"by":"owner","reason":"changing cables","since":"2026-11-05T19:13:10.562Z","cancelled":[],"stopped":null}
{"error":"the lab is paused by owner: changing cables"}
{"resumed":true}
```

The owner's own switch is a file the API cannot clear. Locally it is `state/LOCAL_PAUSE`; **on the real lab** `sudo touch /etc/oasa-lab/PAUSE`:

```
{"error":"the lab is paused by owner (local file): paused by file"}
{"resumed":false,"still_paused":{"by":"owner (local file)","reason":"paused by file","local":true}}
```

Delete the file to resume.

## 10. From Claude or another agent

**In a Claude chat (on the real lab).** With the connector added, ask in plain words:

> Check the lab status, run l2-rx-noise on the bench-rx profile, watch it, and show me the checker's verdict.

Claude calls `lab_status`, `run_suite`, `watch_job` and `get_results`, the same calls as above with the same roles. It may say a run counted only when `get_results` shows `"counted": true`. "Pause the lab" works only for the owner.

**An agent with an API token.** The connector (`https://<MCP hostname>/mcp`) accepts a bearer API token as well as Claude's sign-in, so any MCP client can use it. For Claude Code (**on the real lab**, not run here):

```
claude mcp add --transport http oasa-lab https://<MCP hostname>/mcp --header "Authorization: Bearer $TOKEN"
```

If the owner added the optional WAF rule that lets only Anthropic's addresses reach `/mcp` ([deploy/README.md](../deploy/README.md) step 9), a client on your own machine is blocked; use the REST API instead.

**Give agents a runner token.** An agent that runs without a person watching reads text other people control (job summaries, log lines, stop reasons), so it should never hold an admin token: an injected instruction could make it pause the lab, stop other jobs or resume a pause a person set. A runner can only run suites and stop its own jobs ([security-review-remote.md](security-review-remote.md), RM1).

`tools/remote_acceptance.py` is a worked example of an agent through the connector. It runs the T-BENCH2 acceptance: two counted runs, downloads and a local re-check, then the refusals. It pauses and resumes the lab only with `--attended`, when a person is watching; without it, that check is skipped. Run locally:

```
OASA_LAB_TOKEN=<owner token> OASA_LAB_RUNNER_TOKEN=<member token> \
  python3 tools/remote_acceptance.py --url $L --out runs/remote-acceptance --attended
```

```
  [ok] run_suite l2-pluto-acceptance refused (transmit lock): refused: suite 'l2-pluto-acceptance' transmits, and this lab's rf_setup is 'unset' …
  [ok] run_suite l1-channel accepted: job 484a9edfdf614ce383330838682a8f75
    484a9edfdf61 passed (55 log lines)
  [ok] l1-channel counted: status passed, checker 1.2 PASS, counts {'total': 16, 'passed': 16, 'failed': 0, 'skipped': 0}
  [ok] l1-channel re-checked here: all 7 checks pass …
  [ok] l2-rx-noise counted: status passed, checker 1.2 PASS, counts {'total': 3, 'passed': 3, 'failed': 0, 'skipped': 0}
  [ok] runner may not pause_lab: your role may not do this
  [ok] run_suite l1-channel refused while paused: the lab is paused by owner: remote acceptance: …
  [ok] resume_lab: {"resumed": true}
- **Result:** PASS (22 of 22 checks)
```

## 11. Every call leaves a trace

The service writes one JSON line per request and tool call to its audit log (on the real lab `/var/lib/oasa-lab/state/audit.jsonl`, owner only): who, how, what, and the answer. Token values never appear in it.

```
{"ts":"2026-11-05T19:11:35.741Z","kind":"tool","tool":"pause_lab","actor":"member","via":"api-token","credential":"api-token:member-laptop","args":{"stop_running":false},"status":"forbidden"}
{"ts":"2026-11-05T19:13:10.562Z","kind":"http","method":"POST","path":"/api/pause","status":200,"actor":"owner","via":"api-token","remote":"127.0.0.1","ms":1,"credential":"owner-laptop","action":"pause","stop_running":false}
```

## Local vs the real lab

| | Your laptop | The real lab |
|---|---|---|
| Address | `http://127.0.0.1:8742` | `https://<MCP hostname>/mcp` (connector), `https://<API hostname>` (REST, behind Cloudflare Access), both through a Cloudflare Tunnel |
| Sign-in | API tokens | API tokens; GitHub sign-in for the Claude connector |
| Jobs run | as you, no sandbox (`mode = "none"`) | as `oasalab-job` in a hardened systemd unit (`mode = "systemd"`) |
| Radios | simulated (`OASA_LAB_SIM=1`) | B200mini, E310, KORAD on cable |
| Transmit lock | `rf_setup` in `service.toml` | `rf_setup` and `[confirm_wiring]` in root-owned `/etc/oasa-lab/lab-policy.toml` |
| Owner's pause | `state/LOCAL_PAUSE` | `/etc/oasa-lab/PAUSE` |
