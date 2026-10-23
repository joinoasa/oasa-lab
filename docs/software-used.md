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

## Build only

| Package | Version | License | Why |
|---|---|---|---|
| [setuptools](https://github.com/pypa/setuptools) | 84.2.1 (`>=68`) | MIT | Builds the `oasa-lab` package (`pip install -e .`). Not needed to run. |

## Read, not used

The radio profiles ([profiles.md](profiles.md)) take numbers and equations from the Semtech SX126x driver, ArduPilot SiK, mLRS and WFB-ng sources and docs. No code from them is copied or linked. If a profile ever needs one of them as a dependency, it goes in this table first.

## Standard library only

The simulated channel, the pcap writer and the whole checker use only the Python 3.11+ standard library (`asyncio`, `socket`, `struct`, `hashlib`, `tomllib`, `xml.etree`). The checker takes no outside package on purpose, so it is easy to audit.

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
- **tcpdump / Wireshark:** handy for looking at the captures, but not a dependency. A unit test uses tcpdump if it is installed.
- **scapy, dpkt:** a pcap writer is about 40 lines, so we wrote it.
- **jsonschema:** not needed; the checker validates the manifest by hand.
- **tc/netem:** needs root and Linux. The simulated channel runs anywhere and is repeatable from a seed, which netem is not.

## Later (not in M1)

labgrid (LGPL-2.1, so check before adopting) and cosign (Apache-2.0) come with signing (M3 to M5).
