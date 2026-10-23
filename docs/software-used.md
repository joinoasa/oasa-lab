# Software used

Outside packages the lab adopts (for the [OASA software register](https://joinoasa.org/docs/software-register/)). Permissive licenses only. Dates: October 2026.

## Runtime

| Package | Version | License | Why |
|---|---|---|---|
| [pytest](https://docs.pytest.org) | 9.1.1 (`>=8,<10`) | MIT | The test runner. One command for people and agents; writes JUnit XML itself. |
| [pytest-json-ctrf](https://github.com/infopulse/pytest-common-test-report-json) | 0.6.1 (`>=0.6.1,<0.7`) | MIT | Writes the results in [CTRF](https://ctrf.io) format. We use it instead of writing our own. |

Installed with them (pytest's own dependencies):

| Package | Version | License |
|---|---|---|
| pluggy | 1.7.0 | MIT |
| iniconfig | 2.3.1 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| Pygments | 2.21.0 | BSD-2-Clause |

## Hardware extra (`pip install -e '.[hw]'`, lab PC only)

Optional. The simulated bench and CI need none of these. Rule: a hardware driver that is not permissive stays an optional extra or a separate process, is never shipped, and is flagged here. The owner allows LGPL and GPL tools in tests only.

| Package | Version | License | How used | Flag |
|---|---|---|---|---|
| [pyserial](https://github.com/pyserial/pyserial) | 3.5 (`>=3.5,<4`) | BSD-3-Clause | Serial port to the LoRa modems | |
| [pyadi-iio](https://github.com/analogdevicesinc/pyadi-iio) | 0.0.21 (`>=0.0.20,<0.1`) | "ADI BSD" (BSD-3 plus extra clauses) | Drives the Pluto+ (`adi.ad9361`) | **Not OSI-permissive.** One clause says use must be with an Analog Devices component. We only run it against a Pluto (AD9363) in the lab; nothing is shipped. Check before using it elsewhere |
| [pylibiio](https://github.com/analogdevicesinc/libiio) | 1.0 | LGPL-2.1-or-later | Dependency of pyadi-iio | **LGPL.** Not modified, not shipped |
| libiio | 1.0 | LGPL-2.1 | C library under pylibiio | **LGPL.** System library, not shipped |
| numpy | any | BSD-3-Clause and others | Dependency of pyadi-iio | |

Tools run as **separate processes** (never imported, never shipped):

| Tool | License | Use | Flag |
|---|---|---|---|
| [rtl-sdr-blog](https://github.com/rtlsdrblog/rtl-sdr-blog) `rtl_sdr`, `rtl_test` | GPL-2.0 | Records raw IQ from the RTL-SDR Blog V4 (the V4 needs this fork) | **GPL.** Subprocess only |
| [uhubctl](https://github.com/mvp/uhubctl) | GPL-2.0 | Per-port USB power (one of two hub options) | **GPL.** Optional subprocess |
| [ykushcmd](https://github.com/Yepkit/ykush) | Apache-2.0 | Per-port USB power for the YKUSH3 | |

The lab writes its own SigMF files and checks them with its own code (stdlib only), and has its own pure-Python FFT for tone measurement.

## Job service and Claude connector (optional extra: `pip install -e '.[service]'`)

For `oasa-lab serve` ([remote-lab.md](remote-lab.md)). The lab, the checker and CI suites do not need any of this. One direct dependency, `mcp>=2.3,<3`; it brings the web server and the rest. We use the SDK's own OAuth endpoints and Starlette (instead of adding FastAPI or an OAuth library). Versions as installed in October 2026.

| Package | Version | License | Why |
|---|---|---|---|
| [mcp](https://github.com/modelcontextprotocol/python-sdk) (official MCP Python SDK) | 2.3.0 (`>=2.3,<3`) | MIT | The Claude connector (streamable HTTP) and the OAuth server endpoints Claude needs: discovery, dynamic registration, PKCE, token checks. |

Installed with it:

| Package | Version | License | Role |
|---|---|---|---|
| starlette | 1.9.0 | BSD-3-Clause | The web app (our REST API and sign-in pages sit on it) |
| uvicorn | 0.54.0 | BSD-3-Clause | The server, bound to 127.0.0.1 |
| httpx2 (+ httpcore2, h11) | 2.16.0 / 2.16.0 / 0.16.0 | BSD-3-Clause / BSD-3-Clause / MIT | HTTP client: the GitHub sign-in calls, and the MCP client in tests |
| pydantic (+ pydantic-core, annotated-types, typing-inspection) | 2.14.1 / 2.50.1 / 0.8.0 / 0.4.4 | MIT | Tool argument checking |
| mcp-types | 2.3.0 | MIT | MCP message types |
| PyJWT | 2.15.1 | MIT | Checks the Cloudflare Access JWT (RS256) |
| cryptography (+ cffi, pycparser) | 50.0.2 | Apache-2.0 OR BSD-3-Clause (cffi MIT, pycparser BSD-3-Clause) | RSA signature check for PyJWT (`pyjwt[crypto]`) |
| anyio | 4.15.1 | MIT | Async I/O |
| sse-starlette | 3.5.0 | BSD-3-Clause | Streaming for MCP |
| python-multipart | 0.0.32 | Apache-2.0 | Reads the consent form |
| jsonschema (+ jsonschema-specifications, referencing, rpds-py, attrs) | 4.26.0 / 2025.9.1 / 0.37.0 / 2026.9.1 / 26.1.0 | MIT | MCP schema checks |
| opentelemetry-api | 1.45.1 | Apache-2.0 | Imported by the SDK; we turn nothing on |
| click, idna, truststore | 8.5.0 / 3.20 | BSD-3-Clause / BSD-3-Clause / MIT | Pulled in by uvicorn, httpx2 |
| typing-extensions | 4.16.0 | PSF-2.0 (Python's own permissive license) | Typing backports |

The exact versions and file hashes are pinned in `deploy/requirements.lock`; the deploy installs with `--require-hashes`. The lock files are made with `uv` (Apache-2.0 OR MIT), a build-time tool only, never installed on the lab PC. `oasa-lab job-run` uses the standard library and calls `git` and (through the service) `systemctl`; both are system tools we call, not link.

The service itself uses only these and the standard library. It reads the lab's own suites from git; it adds no code from outside to a run.

**Programs on the lab PC** (system packages by `deploy/01-packages.sh` and `06-cloudflared.sh`; run as separate programs, not linked or copied into this repo):

| Program | License | Why |
|---|---|---|
| [cloudflared](https://github.com/cloudflare/cloudflared) | Apache-2.0 | The tunnel to Cloudflare. Outbound only. |
| [WireGuard](https://www.wireguard.com) (`wireguard-tools`, kernel module) | GPL-2.0 | Maintenance access for the owner |
| [nftables](https://netfilter.org/projects/nftables/) | GPL-2.0 | The firewall |
| [polkit](https://gitlab.freedesktop.org/polkit/polkit) (`polkitd`) | LGPL-2.0-or-later | Lets the service user start and stop only job units (`deploy/polkit/50-oasa-lab.rules`) |
| [systemd](https://systemd.io) | LGPL-2.1-or-later | Runs the service, the job unit (sandbox) and the mirror timer |
| [git](https://git-scm.com) | GPL-2.0 | Mirror of the lab repo, clones for each job |
| [libiio](https://github.com/analogdevicesinc/libiio) (`libiio-utils`) | LGPL-2.1 (library), GPL-2.0 (tools) | Pluto+ access by the tests |
| [rtl-sdr](https://osmocom.org/projects/rtl-sdr/wiki) | GPL-2.0 | RTL-SDR access by the tests |
| [uhubctl](https://github.com/mvp/uhubctl) | GPL-2.0 | Power-switching the USB hub |

These are tools the owner installs and the tests may call. (For the service tests only: [node](https://nodejs.org) (MIT), optional, runs the polkit rule file in `tests/service/test_polkit.py`; the test is skipped without it.) Copyleft licenses apply to them, not to this repo, because nothing links against them. If a test ever needs one as a library, it goes through review first.

## Build only

| Package | Version | License | Why |
|---|---|---|---|
| [setuptools](https://github.com/pypa/setuptools) | 84.2.1 (`>=68`) | MIT | Builds the `oasa-lab` package (`pip install -e .`). Not needed to run. |

## Read, not used

The radio profiles ([profiles.md](profiles.md)) take numbers and equations from the Semtech SX126x driver, ArduPilot SiK, mLRS and WFB-ng sources and docs. No code from them is copied or linked. If a profile ever needs one of them as a dependency, it goes in this table first.

## Standard library only

The simulated channel, the pcap writer and the whole checker (also the job runner) use only the Python 3.11+ standard library (`asyncio`, `socket`, `struct`, `hashlib`, `tomllib`, `xml.etree`). The checker takes no outside package on purpose, so it is easy to audit.

## CI (GitHub Actions)

| Action | Pinned to | License | Why |
|---|---|---|---|
| actions/checkout | v4 | MIT | Get the code. |
| actions/setup-python | v5 | MIT | Python 3.11+. |
| actions/upload-artifact | v4 | MIT | Keep each evidence bundle. |
| actions/attest-build-provenance | v2 | MIT | Sign a tarball of each bundle (build provenance). |

The `lora-link` job also checks out [oasa-sec](https://github.com/joinoasa/oasa-sec) and [oasa-radio](https://github.com/joinoasa/oasa-radio) and builds them with Rust, to run `l2-lora-link` on the simulated bench.

## Considered, not adopted

- **pyrtlsdr:** GPL-3.0-or-later. We run `rtl_sdr` as a process instead.
- **SigMF Python library:** LGPL-3.0. SigMF is a small JSON format; the lab has its own writer, and the checker its own validator.
- **FastAPI:** Starlette (already there through the MCP SDK) is enough for a dozen routes.
- **Authlib or another OAuth library:** the MCP SDK already serves the OAuth endpoints Claude needs; we add only the provider, the consent page and the GitHub step.
- **Cloudflare Access "Managed OAuth" as the connector's sign-in:** beta, no source says Claude.ai works with it. See [remote-lab.md](remote-lab.md).
- **tcpdump / Wireshark:** handy for looking at the captures, but not a dependency. A unit test uses tcpdump if it is installed.
- **scapy, dpkt:** a pcap writer is about 40 lines, so we wrote it.
- **jsonschema:** not needed; the checker validates the manifest by hand.
- **tc/netem:** needs root and Linux. The simulated channel runs anywhere and is repeatable from a seed, which netem is not.

## Later (not in M1)

labgrid (LGPL-2.1, so check before adopting) and cosign (Apache-2.0) come with signing (M3 to M5).
