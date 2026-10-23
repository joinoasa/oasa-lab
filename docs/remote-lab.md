# Remote lab: run tests from a Claude chat

**Status:** v0.1 prototype · **Checked:** October 2026 · Setup: [deploy/README.md](../deploy/README.md)

The owner's lab PC has radios on it. This service lets the owner (and, later, others) run lab suites on it **remotely and without watching**, from a Claude chat in a web browser, a script, or a terminal. Every run leaves the usual evidence bundle, and a run counts only after the independent checker passes it.

It is a **job service**, not remote desktop. You can ask for a named suite. You cannot send code, a shell command or a path.

## How it fits together

```
 Claude chat (browser)                    your laptop (scripts, browser)
        |  HTTPS, OAuth token                    |  HTTPS, Cloudflare Access + API token
        v                                        v
 +----------------------- Cloudflare edge (TLS) -----------------------+
        |  Cloudflare Tunnel: the PC only dials OUT. No inbound port.
        v
 cloudflared  ->  http://127.0.0.1:8742   (only fixed paths; the rest is 404)
                       |
                 oasa-lab serve   (unprivileged user "oasalab", sandboxed)
                  |  OAuth + GitHub sign-in (owner only)   |  REST API
                  |  Claude connector (MCP tools)          |  audit log
                  v
            job queue: ONE job at a time
                  |  clean checkout of the lab repo at a commit on main
                  v
            oasa-lab run SUITE   ->  evidence bundle + checker verdict
                  |
        radios on USB, Pluto+ on its own cable (lab network). SDR ports stay local.

 Maintenance: laptop --WireGuard--> SSH to the PC. Not used by Claude or the service.
```

Code: `oasa_lab/service/`. One process serves both the connector (`/mcp`) and the REST API (`/api/...`), so they share one queue and one audit log.

## Decision: how Claude signs in, and how Cloudflare fits

**What Claude.ai needs.** A custom connector is a URL. For anything that is not public, Claude uses OAuth 2.x: your server answers `401` with a pointer to its protected-resource metadata (RFC 9728), publishes authorization-server metadata (RFC 8414), supports PKCE `S256`, and accepts Claude either by Dynamic Client Registration or by a Client ID Metadata Document. Claude's callback is `https://claude.ai/api/mcp/auth_callback`. The token endpoint must take form-encoded bodies and answer within 10 seconds. A fixed API key or bearer token in a request header exists but is a **beta for a limited set of organizations**. ([Authentication for connectors](https://claude.com/docs/connectors/building/authentication), [Add a custom connector](https://claude.com/docs/connectors/custom/add-unlisted), [Custom connectors help article](https://support.claude.com/en/articles/11503834))

**Options we weighed**

| Option | Verdict |
|---|---|
| Cloudflare Access in front of the connector hostname | **No.** Access wants a browser login or a service-token header. Claude's servers do neither, and header auth is beta. |
| Access "Managed OAuth" (Access acts as the OAuth server) | **No, not yet.** It is in beta, its redirect allowlist example does not mention Claude, and Cloudflare's own guide for a server that runs its own OAuth says not to turn it on. We found no source saying it works with Claude.ai. ([Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/), [Secure MCP servers](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/secure-mcp-servers/)) |
| Claude "MCP tunnels" (no public hostname at all) | **Not available to us.** Research preview, Enterprise plan by request. Worth revisiting if we get it: it would remove the public hostname. ([MCP tunnels](https://claude.com/docs/connectors/mcp-tunnels/troubleshooting)) |
| **The service is the OAuth server; GitHub proves who you are; Cloudflare Tunnel carries the traffic** | **Chosen.** Works with Claude.ai today. One public hostname, no inbound port, owner only. |

**What we built.** The official MCP Python SDK (MIT) provides the OAuth endpoints (`/.well-known/*`, `/register`, `/authorize`, `/token`, `/revoke`), PKCE and bearer checks. We supply the parts it leaves to the server (`oasa_lab/service/oauth.py`):

1. **Registration** only for Claude's own callback URLs (`claude.ai`, `claude.com`). Anyone can ask to register; nobody can register somewhere else. This blocks the classic attack on OAuth proxies: register a client that sends the owner's code to the attacker.
2. **A consent page** on our host, so a link someone sends you never signs you in silently.
3. **GitHub sign-in.** After you approve, GitHub tells us the account's numeric id. Only a user listed in `lab.toml` (matched by id, not by name) gets an authorization code. Anyone else gets `access_denied`. We read the id and throw GitHub's token away.
4. **Tokens:** random, one hour for access, 30 days for refresh (rotated on use), stored only as SHA-256 hashes in a `0600` file. Removing a user from `lab.toml` ends their tokens on the next request.

No crypto is invented here: PKCE, token checks and metadata come from the SDK; GitHub does the identity.

**Where Cloudflare Access does apply.** The REST API and downloads sit on a second hostname with an Access policy (Allow: the owner's email). The service also checks the signed `Cf-Access-Jwt-Assertion` itself (RS256, issuer, audience, email), so a request that skips Cloudflare gets nothing. ([Validating the Access JWT](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/))

**Other ways in** (all end in a user and role from `lab.toml`): an API token (bearer; only its hash is stored) for scripts and other MCP clients. Nothing else is accepted. Identity headers such as `X-Forwarded-User` mean nothing.

## Threat model, in plain words

**What we protect:** the radios (a wrong transmission can break rules or burn a receiver), the lab PC, the integrity of the evidence, the owner's accounts.

**Who we worry about:** a stranger on the internet who finds the hostname; a stranger who tricks the owner into clicking a link; a bug or a bad prompt that makes Claude do something silly; a mistake with the bench setup.

| If this happens | What stops it |
|---|---|
| Stranger finds the hostname and the paths | Every path but `/healthz` and the sign-in pages needs a token. Sign-in needs the owner's GitHub account. |
| Stranger registers an OAuth client | Only Claude's callback URLs are accepted, so a code can only go to Claude. |
| Stranger sends the owner a sign-in link | The owner sees a consent page naming where they will be sent. Even if approved, the code goes to Claude's callback, not to the stranger. |
| Stranger's web page calls the API using the owner's browser | Calls from any other website are refused (Origin check); no CORS headers; bearer or signed JWT needed. |
| Stranger finds the PC's address | There is none: the PC dials out to Cloudflare. Firewall drops all inbound except WireGuard. The service listens on `127.0.0.1` only and refuses any other address in its config. |
| Someone reaches the SDR ports | Never exposed: not in the tunnel, not in the firewall, and the service never forwards to them. `deploy/check.sh` checks it. |
| Caller asks for something else than a suite | Only names from the lab repo's `suites/` at the checked-out commit run. `../`, shell text, paths, hidden suites, unknown fields: refused. Tests cover these. |
| A suite would transmit at the wrong time | The service refuses suites with `transmits = true` unless the owner set `rf_setup` to `cabled` or `shielded`. The setting is in a file only root can edit. |
| Two jobs fight over the radios | One job at a time, first in first out. |
| A run goes wrong | Stop it (SIGTERM, then SIGKILL after 10 s, whole process group). Or pause the lab (refuses new jobs, cancels the queue). Or `touch /var/lib/oasa-lab/PAUSE` from a shell. Runs also have a time limit. |
| Someone claims a result that did not happen | A run counts only if `get_results` shows `"counted": true`: job passed and checker passed. A stopped run is never counted. |
| We need to know what happened | Audit log: one JSON line per request (who, what, when, result), no secrets. |

**What we do not defend against (known limits)**

- **The owner's GitHub or Claude account being taken over.** Turn on two-factor sign-in on both. Whoever is the owner can run suites.
- **Bad code merged to `main`.** Jobs run the newest `main` of this repo. We trust pull-request review. A malicious suite would run as the `oasalab` user and could read the service's memory, including the GitHub client secret. Keep `main` protected. A later step is to run jobs as a second user.
- **Text inside test output tricking Claude** (prompt injection). The connector tells Claude that log text is data. The tools cannot do anything beyond the suites, the interlock and the role limits, so the worst a trick can do is run or stop an allowed suite.
- **Cloudflare, GitHub or Anthropic being compromised or down.** We trust them for transport, identity and the chat. If any is down, the lab is simply unreachable.
- **Physical access, and the bench itself.** The interlock trusts `rf_setup`. Setting it to `cabled` when the antennas are on is the owner's mistake to avoid. Attenuators and a shielded box are the real safety.
- **Floods.** Anyone can hit `/register` and `/authorize` (they hold no power but use memory; both are capped). Add a Cloudflare rate-limit rule if you see abuse.
- **Disk.** Bundles accumulate in `/var/lib/oasa-lab/jobs/`. Prune old ones by hand for now.

## Use it from a Claude chat

One-time: [deploy/README.md](../deploy/README.md), step 9. In short: Claude, Customize, Connectors, Add custom connector, URL `https://<your MCP hostname>/mcp`, Connect, approve, sign in with GitHub.

Then talk normally:

- "What suites can the lab run? Is anything blocked?"
- "Run l1-channel with seed 5 and tell me when it's done."
- "How is job 3f2a... going? Show the last log lines."
- "Show the results and the checker verdict for that job."
- "Stop that job." / "Pause the lab." / "Resume the lab."

| Tool | Does | Role |
|---|---|---|
| `list_suites` | Suites, what they prove, whether they transmit, whether they can run now | viewer |
| `run_suite` | Queue a run: `suite`, `seed`, optional hardware `profile` | runner |
| `job_status` | State, queue position, commit. No id: overview | viewer |
| `watch_job` | Recent live-log lines; pass `since` for only new ones | viewer |
| `get_results` | Counts, each checker check, `summary.md`, links, `counted` | viewer |
| `stop_job` | Stop a queued or running job (own jobs; admin: any) | runner |
| `lab_status` | Devices present, RF setup, paused or not, profiles ready | viewer |
| `pause_lab`, `resume_lab` | The kill switch | admin |

Claude can tell you a run counted only after `get_results` says so. Bundles and `check.json` download from the API host (links in the results); they are not readable through the chat.

## REST API

Same rules, for scripts. Send `Authorization: Bearer <token>` (or go through Cloudflare Access).

```
GET  /api/suites                 POST /api/jobs {"suite":"l1-channel","seed":0,"profile":null,"commit":null}
GET  /api/jobs                   GET  /api/jobs/{id}              POST /api/jobs/{id}/stop
GET  /api/jobs/{id}/log          (add ?follow=1 for live server-sent events; ?stream=stdout for pytest output)
GET  /api/jobs/{id}/results      GET  /api/jobs/{id}/check.json   GET /api/jobs/{id}/bundle.tar.gz
GET  /api/bundles                GET  /api/lab                    POST /api/pause   POST /api/resume
GET  /healthz                    (open; says only {"ok": true})
```

```
TOKEN=...   # from: oasa-lab token owner-laptop --user citruscybersec
curl -sH "Authorization: Bearer $TOKEN" -d '{"suite":"l1-channel"}' https://lab-api.example.com/api/jobs
curl -sNH "Authorization: Bearer $TOKEN" "https://lab-api.example.com/api/jobs/<id>/log?follow=1"
```

`commit` may only be a full commit id that is on `main`. Leave it out for the newest.

## Config: users, roles, devices

`/etc/oasa-lab/lab.toml` (example: [deploy/lab.toml.example](../deploy/lab.toml.example)). Ships with one admin. More people later is a config change:

```toml
[[users]]
name = "alex"
role = "runner"          # viewer | runner | admin
github_id = 123456       # numeric id, not the name
```

Roles live in `oasa_lab/service/config.py` (`PERMISSIONS`). Restart the service after editing.

**Devices** come from a pluggable inventory: `usb` (reads `/sys/bus/usb/devices`, and one TCP connect to the Pluto+'s iiod port to see that it answers; it never opens a radio) or `simulated` (tests and demos). A **profile** names devices that must be present; a suite may also list `requires = ["pluto"]` in its `suite.toml`. Missing devices stop the job before it starts.

**Suites that transmit** declare `transmits = true` in `suite.toml`. Mark any suite that puts RF on a cable or the air.

## Audit log

`/var/lib/oasa-lab/audit.jsonl`, one JSON object per line:

```
{"ts":"2026-10-23T19:04:19.121Z","kind":"http","method":"POST","path":"/api/jobs","status":202,"actor":"citruscybersec","via":"api-token","remote":"203.0.113.9","action":"submit","suite":"l1-channel","job":"dfc81700af0c"}
{"ts":"...","kind":"tool","tool":"run_suite","actor":"citruscybersec","via":"oauth","args":{"suite":"l1-channel","seed":1},"status":"ok","job":"dfc81700af0c"}
{"ts":"...","kind":"job","event":"finished","job":"dfc81700af0c","status":"passed","check_ok":true}
{"ts":"...","kind":"oauth","event":"login_denied","github_login":"stranger"}
```

Refused calls, failed sign-ins and unauthorized requests are logged too. Token values, client secrets and codes are never written; the writer also removes any field whose name looks secret.

## Stop everything

| Level | How |
|---|---|
| One run | Chat: "stop job ...". Or `POST /api/jobs/{id}/stop` |
| No new runs | Chat: "pause the lab". Or on the PC: `sudo touch /var/lib/oasa-lab/PAUSE` (delete it to resume) |
| The service (also kills a run) | `sudo systemctl stop oasa-lab` |
| The internet door | `sudo systemctl stop cloudflared`, or delete the tunnel in Cloudflare |
| Claude's access only | Remove the connector in Claude; delete `/var/lib/oasa-lab/oauth.json`; restart the service |
| Everything | `sudo systemctl disable --now oasa-lab cloudflared`, and `sudo wg-quick down wg0` |

## Sources (checked 2026-10-23)

- Claude: [Authentication for connectors](https://claude.com/docs/connectors/building/authentication), [Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom/add-unlisted), [Building custom connectors via remote MCP](https://support.claude.com/en/articles/11503834), [MCP tunnels](https://claude.com/docs/connectors/mcp-tunnels/troubleshooting)
- Cloudflare: [Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/), [Secure MCP servers](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/secure-mcp-servers/), [Access for MCP (changelog)](https://developers.cloudflare.com/changelog/post/2025-08-26-access-mcp-oauth/), [Validating the Access JWT](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/), [Tunnel configuration file](https://developers.cloudflare.com/tunnel/advanced/local-management/configuration-file/)
- Standards: MCP authorization spec 2025-11-25, RFC 9728 (protected resource metadata), RFC 8414 (server metadata), RFC 7591 (dynamic registration), RFC 7636 (PKCE), RFC 8707 (resource indicators), RFC 8252 (native app redirects)
- Pluto addresses: [Analog Devices Pluto customizing](https://wiki.analog.com/university/tools/pluto/users/customizing) (default 192.168.2.1 for the ADALM-Pluto; **check your Pluto+'s own address**, we found no source for it)
