# Bench 1: bill of materials (as built)

**Status:** v0.1, 2026-11-05 · Prices in USD, checked 2026-11-05 where a link is given; *est.* = estimate. For gear to buy by budget see [lab-kit.md](lab-kit.md).

The bench every L2 suite in this repo has run on so far: two SDRs joined by coax through fixed attenuators, a programmable supply for the embedded radio, and one Linux VM that runs the job service. Serial numbers and network addresses are in the owner's private device file, never here.

## Radios and RF

| Qty | Part | Price | What it does here |
|---|---|---|---|
| 1 | [Ettus USRP B200mini](https://www.ettus.com/all-products/usrp-b200mini/) (USB 3, AD9364, 1×1, 70 MHz–6 GHz) | $1,503 (Ettus list) | Transmits and receives; USB to the lab host. TX/RX port sends, RX2 receives |
| 1 | [Ettus USRP E310](https://www.ettus.com/all-products/e310/) (embedded, AD9361, 2×2) | not listed; Ettus says limited quantities, not for new designs | Receives on RX2-A and RX2-B (one stream, both channels: direction finding), transmits on TRX-A; reached over Ethernet |
| 6 | SMA fixed attenuators covering 915 MHz: 2× 30 dB, 2× 20 dB, 2× 10 dB | ~$60 *est.* | 60 dB on each path (30 + 20 + 10), the lab's minimum between two transmitters |
| 4+ | SMA male–male coax cables | ~$30 *est.* | The two paths: B200 TX/RX → E310 RX2-B, E310 TRX-A → B200 RX2 |

Wiring: [hardware-suites.md](hardware-suites.md) (`usrp`). Measured on this bench: the two directions read about 17 dB apart at equal gains (the radios differ, not the pads; found by swapping the attenuator stacks).

## Power and control

| Qty | Part | Price | What it does here |
|---|---|---|---|
| 1 | [KORAD KA3005P](https://www.digikey.com/en/products/detail/sra-soldering-products/KA3005P/10709868) programmable supply, 30 V 5 A, USB serial | ~$165 | Powers the E310 at 6 V and reads its current (`l2-rx-noise`). The driver never goes above 6 V or 3 A and never cuts the output: the E310 needs a person to press PWR after a power loss |
| 1 | USB 3 port on the lab host | — | B200mini (passed through to the VM) |
| 1 | Ethernet switch port | — | E310 |

## Lab host

| Qty | Part | Price | What it does here |
|---|---|---|---|
| 1 | Linux VM: 4 vCPU, 6 GB RAM, 64 GB disk, Ubuntu 24.04 LTS, on an existing Proxmox host | $0 (existing host) | Runs the job service, UHD 4.6 (from Ubuntu), the evidence store. Decodes W1 at real time with all 4 cores |
| 1 | Cloudflare Tunnel (free plan) | $0 | The only way in from the internet: the job service's API and the Claude connector, nothing else ([remote-lab.md](remote-lab.md)) |
| 1 | A domain on Cloudflare DNS, two hostnames (connector, API) | existing domain | Public names for the tunnel ([deploy/README.md](../deploy/README.md) step 7) |
| 1 | Cloudflare Zero Trust (free plan): an Access application on the API hostname | $0 | Email sign-in (or a service token for scripts) in front of the REST API and downloads |
| 1 | GitHub OAuth app | $0 | Proves who signs in to the Claude connector; users are matched by numeric GitHub id |
| 1 | WireGuard on the lab host and the owner's laptop | $0 | Maintenance only (SSH); never used by the service or by Claude |

## Software (all open, installed by `deploy/`)

| Part | Licence | Notes |
|---|---|---|
| oasa-lab (this repo) | Apache-2.0 | Job service, suites, drivers, checker |
| oasa-radio, oasa-sec | Apache-2.0 | Binaries the suites run, pinned by SHA-256 in the owner's install script |
| UHD 4.6 (Ubuntu package) | GPL-3.0 | Separate processes only (`rx_samples_to_file`, `tx_samples_from_file`, ...); never imported or shipped |
| Python 3.12, systemd, polkit | various open | Job sandbox: one systemd unit per job, as its own user |

## Totals

| | |
|---|---|
| Radios, RF, power | about **$1,760** plus the E310 (price not listed) |
| Host, tunnel | $0 with an existing machine |

## Adding next

For direction finding ([oasa-radio#7](https://github.com/joinoasa/oasa-radio/issues/7)): a 2-way SMA splitter, matched RG-316 cables of known lengths, 50 Ω terminators; later three matched 915 MHz antennas. For R-COM1: one off-the-shelf telemetry pair (Holybro SiK V3, $58.99).
