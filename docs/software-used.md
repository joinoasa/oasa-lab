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

## Build only

| Package | Version | License | Why |
|---|---|---|---|
| [setuptools](https://github.com/pypa/setuptools) | 84.2.1 (`>=68`) | MIT | Builds the `oasa-lab` package (`pip install -e .`). Not needed to run. |

## Standard library only

The simulated channel, the pcap writer and the whole checker use only the Python 3.11+ standard library (`asyncio`, `socket`, `struct`, `hashlib`, `tomllib`, `xml.etree`). The checker takes no outside package on purpose, so it is easy to audit.

## CI (GitHub Actions)

| Action | Pinned to | License | Why |
|---|---|---|---|
| actions/checkout | v4 | MIT | Get the code. |
| actions/setup-python | v5 | MIT | Python 3.11+. |
| actions/upload-artifact | v4 | MIT | Keep each evidence bundle. |
| actions/attest-build-provenance | v2 | MIT | Sign a tarball of each bundle (build provenance). |

## Considered, not adopted

- **tcpdump / Wireshark:** handy for looking at the captures, but not a dependency. A unit test uses tcpdump if it is installed.
- **scapy, dpkt:** a pcap writer is about 40 lines, so we wrote it.
- **jsonschema:** not needed; the checker validates the manifest by hand.
- **tc/netem:** needs root and Linux. The simulated channel runs anywhere and is repeatable from a seed, which netem is not.

## Later (not in M1)

labgrid (LGPL-2.1, so check before adopting), cosign (Apache-2.0) and SigMF (CC0 / Apache-2.0 tools) come with the hardware tiers and signing (M3 to M5).
