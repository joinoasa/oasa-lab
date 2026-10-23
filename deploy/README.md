# Set up the lab PC (Ubuntu 24.04 LTS)

One time. About an hour, most of it waiting on accounts. After this, Claude (and you) can run lab tests remotely, and every run leaves a checked evidence bundle.

How it fits together, the threat model and day-to-day use: [docs/remote-lab.md](../docs/remote-lab.md).

**Steps marked OWNER need you**: an account, a key, a router setting. Scripts never ask for secrets and never contain any.

All scripts are safe to run again. `DRY_RUN=1 sudo ./01-packages.sh` shows what would change and changes nothing.

## What you need first (OWNER)

| Need | Why |
|---|---|
| The lab PC with Ubuntu 24.04 LTS, updated, and you logged in with `sudo` | Runs everything |
| A second network card (USB-Ethernet is fine) for the Pluto+ | The Pluto+ gets its own cable and network |
| A domain on Cloudflare (free plan is enough) and a Cloudflare account | The tunnel and the sign-in page need a name |
| A GitHub account (`citruscybersec`) | Sign-in for the Claude connector |
| A Claude plan that allows custom connectors (Pro, Max, Team or Enterprise) | The chat side |
| WireGuard on your laptop (optional) | Maintenance access over the internet |

## Steps

Work in this folder on the lab PC: `git clone https://github.com/joinoasa/oasa-lab && cd oasa-lab/deploy`.

### 0. Your settings (OWNER)

```
cp setup.env.example setup.env
ip -br link            # find the card the Pluto+ is plugged into
nano setup.env         # LAB_IFACE, hostnames, WireGuard key later
```

Nothing in `setup.env` is secret.

### 1. Packages, drivers, USB rules

```
sudo ./01-packages.sh
```

Installs `libiio-utils` (Pluto), `rtl-sdr`, `uhubctl` (switchable hub), `nftables`, WireGuard tools. Keeps the SDR network tools (iiod, rtl_tcp, SoapyRemote) switched off. Adds USB rules so only the `plugdev` and `dialout` groups touch the radios. Unplug and replug the USB radios afterwards.

Check what the PC sees: `lsusb` (RTL-SDR, the Heltec boards, the hub) and `iio_info -s` (Pluto).

### 2. The lab network for the Pluto+

```
sudo ./02-network.sh
ping -c1 192.168.2.1
```

A fixed address on that one card. No gateway, no DNS, no forwarding: the Pluto+ can reach this PC and nothing else. If the ping fails, your Pluto+ may use another address; set `PLUTO_IP` and `LAB_HOST_IP` in `setup.env` to match its `config.txt` and run the script again.

### 3. WireGuard (OWNER, optional but recommended)

```
sudo ./04-wireguard.sh        # prints this PC's public key
```

On your laptop make a WireGuard key pair, put the **public** key in `setup.env` as `OWNER_WG_PUBKEY`, run `sudo ./04-wireguard.sh` again, and use the laptop config it prints. Forward UDP 51820 on your router to the PC. You now reach the PC (SSH) at `10.77.0.1`, from anywhere, and only you.

### 4. Firewall

Do this after WireGuard, so you do not lock yourself out. If you are logged in over your home network right now, set `SSH_FROM_LAN_CIDR` first (for example `192.168.1.0/24`).

```
sudo ./03-firewall.sh
# open a NEW terminal and check that you can still log in, then within 2 minutes:
sudo ./03-firewall.sh --confirm
```

Default deny inbound. Open: WireGuard, and SSH over WireGuard. If you do nothing, the firewall undoes itself after 2 minutes.

### 5. GitHub sign-in app (OWNER)

GitHub, Settings, Developer settings, OAuth Apps, New OAuth App:

- Homepage URL: `https://<your MCP hostname>`
- Authorization callback URL: `https://<your MCP hostname>/oauth/github/callback`

Keep the **client id** (not secret) and make a **client secret**. Do not put the secret anywhere in this repo.

### 6. The job service

```
sudo ./05-service.sh
```

Creates the `oasalab` user (no login, no shell), installs `oasa-lab serve` as the systemd service `oasa-lab`, and copies `service.toml.example` and `.env.example` to `/etc/oasa-lab/`. It does not start while the config still has placeholders.

OWNER: edit the two files.

```
sudo nano /etc/oasa-lab/service.toml     # hostnames, GitHub client id, USB ids from lsusb
sudo nano /etc/oasa-lab/env          # OASA_LAB_GITHUB_CLIENT_SECRET=...
sudo ./05-service.sh                 # checks the config and starts the service
```

Leave `rf_setup = "unset"` until you have checked the bench: radios joined by cable with attenuators, or inside the shielded box. Then set `"cabled"` or `"shielded"` and `sudo systemctl restart oasa-lab`. Until then, suites that transmit are refused. Real transmitting also needs a `[confirm_wiring]` line per suite in `service.toml`, saying you connected that suite's wiring (see [remote lab](../docs/remote-lab.md)). The device file `/etc/oasa-lab/lab.toml` starts as a copy of `lab.example.toml`; fill in your serials and cable losses.

Make an API token for scripts (optional):

```
sudo -u oasalab /opt/oasa-lab/venv/bin/oasa-lab token owner-laptop --user citruscybersec
```

Paste the printed `[[tokens]]` block into `service.toml`. The token itself is shown once; keep it in your password manager.

### 7. The tunnel (OWNER)

```
sudo ./06-cloudflared.sh       # installs cloudflared, then tells you the account steps
```

You log in to Cloudflare, create the tunnel, point two hostnames at it, and copy the credentials file into `/etc/cloudflared/` (the script prints the exact commands). Run the script again. It writes the tunnel config that exposes **only** the job service, on fixed paths. Everything else returns 404. The SDR ports (iiod 30431, rtl_tcp 1234, SoapyRemote 55132) are never listed.

Then in Cloudflare Zero Trust, Access controls, Applications, add a self-hosted application for the **API hostname** with one rule: Allow, Emails, your email. Copy the application's AUD tag and your team domain into `[cloudflare_access]` in `service.toml`, and restart the service.

**Do not** put Access on the MCP hostname. Claude's servers cannot pass a browser login. See [docs/remote-lab.md](../docs/remote-lab.md) for why that is safe.

### 8. Check it

```
sudo ./check.sh
```

Checks that nothing listens on the network except WireGuard, the SDR ports are closed, the service runs as `oasalab` on `127.0.0.1` only, the API refuses calls without a token, the firewall is on, and the tunnel config names no SDR port.

### 9. Connect Claude (OWNER)

In Claude: Customize, Connectors, Add custom connector. URL: `https://<your MCP hostname>/mcp`. Leave the OAuth client fields empty. Click Connect: a lab page asks you to approve, then GitHub signs you in. Only `citruscybersec` gets through. Then, in a chat: "List the lab suites, run l1-channel and show me the result."

Optional hardening: a Cloudflare WAF rule on the MCP hostname that blocks `/mcp`, `/token`, `/register` and `/revoke` unless the request comes from Anthropic's published range `160.79.104.0/21`. Use only if you connect from Claude on the web or desktop (Claude Code calls from your own machine).

## Update, stop, remove

| Want | Do |
|---|---|
| Upgrade the service | `git pull && sudo ./05-service.sh` (jobs always run the newest `main` anyway) |
| Pause all new runs | In chat: "pause the lab". Or: `sudo touch /var/lib/oasa-lab/PAUSE` |
| Stop the running job | In chat: "stop job <id>". Or `curl -X POST -H "Authorization: Bearer $TOKEN" .../api/jobs/<id>/stop` |
| Stop the service (kills any run) | `sudo systemctl stop oasa-lab` |
| Close the internet door | `sudo systemctl stop cloudflared` |
| Everything off | `sudo systemctl disable --now oasa-lab cloudflared` |
| Cut off Claude only | Claude: remove the connector. PC: delete `/var/lib/oasa-lab/oauth.json` and restart the service |
| Look at what happened | `less /var/lib/oasa-lab/audit.jsonl`, `journalctl -u oasa-lab` |

## Files here

| File | What |
|---|---|
| `01-packages.sh` to `06-cloudflared.sh` | The steps, in order |
| `check.sh` | Read-only health and exposure check |
| `lib.sh` | Shared helpers (dry run, write only if changed) |
| `setup.env.example` | Your choices for the scripts |
| `service.toml.example` | The service config: users, roles, devices, RF setup |
| `.env.example` | The one secret the service needs (GitHub client secret) |
| `cloudflared-config.yml.example` | What the tunnel config looks like |
| `systemd/oasa-lab.service` | The service unit, with sandboxing |
| `udev/99-oasa-lab.rules` | USB access for the radios |
