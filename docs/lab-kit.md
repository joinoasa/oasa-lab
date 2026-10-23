# Lab kit: recommended gear by budget

**Status:** v0.1 · **Prices checked:** October 2026 (USD). *est.* = estimate, check before buying. Plan: [r-com2-plan.md](r-com2-plan.md) §5.

Each tier adds to the one before. Buy in order within a tier. **Open** means open firmware, drivers or hardware design we can study and change.

> **Safety:** never connect two transmitters by coax without enough attenuation between them (60 dB or more for LoRa and SiK), or you can burn the receiver. Test over cable or in a shielded box; over-the-air tests follow the test plan and local rules.

## Summary

| Tier | Budget | Adds | Unlocks |
|---|---|---|---|
| 0a Free | $0 | Any laptop | L0–L1: everything in simulation, full evidence and checker |
| 0b Starter | ~$120 | 2 LoRa boards, receive-only SDR, cables, attenuators | M3: first encrypted packets over real radios; check the air is unreadable |
| 1a Telemetry | ~$500 total | SiK pair, mLRS, lab host, VNA | M4 over real drone telemetry radios |
| 1b First SDRs | ~$1,200 total | 2 Plutos, power switching | Waveform W1–W2, SDR adapters |
| 2 Automated | ~$3,700 total | Programmable attenuator, clock reference, bladeRF, Wi-Fi video, shielded box, remote access | M5: unattended sweeps run by agents |
| 3 Full | ~$8,600 total | Spectrum analyzer, more SDRs, long-range telemetry | Wideband, multi-channel and long-range work |

## 0a. Free

| Item | Price | Why | Open |
|---|---|---|---|
| Any laptop (Linux, macOS or Windows with WSL) | $0 | Runs every L0–L1 suite | n/a |

## 0b. Starter (~$120)

| Item | Price | Why | Open | Have |
|---|---|---|---|---|
| 2× [Heltec WiFi LoRa 32 V3](https://heltec.org/project/wifi-lora-32-v3/) (915 MHz in the US) | $36–40 | Cheapest real radio pair; ESP32-S3 + SX1262; runs RNode or our firmware | Firmware yes | |
| [RTL-SDR Blog V4L](https://www.rtl-sdr.com/buy-rtl-sdr-dvb-t-dongles/) | $37.95 | Receive only: record what goes on the air and check it's unreadable | Drivers yes | |
| SMA cables, 2× 30 dB fixed attenuators, adapters | ~$30–50 *est.* | Join the boards by coax safely | n/a | |
| Old laptop or Raspberry Pi you already have | $0 | Runs the lab | n/a | |

## 1a. Telemetry (~$380 more, ~$500 total)

| Item | Price | Why | Open | Have |
|---|---|---|---|---|
| [Holybro SiK Telemetry Radio V3](https://holybro.com/products/sik-telemetry-radio-v3) pair, 915 MHz | $58.99 | The standard cheap drone telemetry link | Firmware yes (SiK) | |
| [MatekSys mLRS MR900-30](https://racedayquads.com/products/mateksys-mlrs-900mhz-module-kit-mr900-30-tx) TX kit | $41.99 (+ RX, price not found) | Open long-range link built for MAVLink | Firmware yes | |
| [Raspberry Pi 5 8 GB](https://www.raspberrypi.com/products/raspberry-pi-5/) | ~$175 *est.* | Always-on lab host and test ground station | Software yes | |
| [NanoVNA-H4](https://eleshop.eu/nanovna-h4.html) | ~$55 | Check antennas, cables and attenuators | Firmware yes | |
| 50 Ω SMA terminations ([ANNE-50+](https://www.minicircuits.com/WebStore/dashboard.html?model=ANNE-50%2B)), more cables | ~$50 | Terminate unused ports | n/a | |

## 1b. First SDRs (~$660 more, ~$1,200 total)

| Item | Price | Why | Open | Have |
|---|---|---|---|---|
| 2× [ADALM-Pluto](https://shop.richardsonrfpd.com/Products/Product/ADALM-PLUTO) | $268.10 each | Cheapest transmit-and-receive SDR pair; our own waveforms (W1–W2) | Firmware and HDL yes | |
| SMA splitters (2- and 4-way) | ~$40 *est.* | Feed one signal to several receivers | n/a | |
| [Yepkit YKUSH3](https://www.yepkit.eu) USB power switch | ~$50 *est.* | Power-cycle radios from a script | Software yes | |
| 2× ESP32-S3 boards | ~$35 | Spare boards for firmware tests | n/a | |

## 2. Automated (~$2,500 more, ~$3,700 total)

| Item | Price | Why | Open | Have |
|---|---|---|---|---|
| [Mini-Circuits RCDAT-6000-60](https://www.minicircuits.com/WebStore/dashboard.html?model=RCDAT-6000-60) | $917 | Programmable attenuator: agents sweep signal level unattended | Control API documented | |
| [bladeRF 2.0 micro xA4](https://www.nuand.com/product/bladerf-xa4/) | $540 | Small (24 g) SDR that can fly; 56 MHz bandwidth | Software and HDL yes | |
| Shielded box (e.g. Ramsey STE series) | ~$300–600 *est.* (quote) | Over-the-air tests without radiating | n/a | |
| [Leo Bodnar Mini GPSDO](https://www.leobodnar.com/shop/) | $196 | Shared 10 MHz clock for timing across SDRs | n/a | |
| 2× Alfa AWUS036ACH (RTL8812AU) | ~$160 | WFB-ng video and high-rate link | Driver yes | |
| Remote bench supply (Riden RD6006P-W) | ~$100 *est.* | Power control over the network | Firmware partly | |
| WireGuard router ([GL.iNet](https://www.gl-inet.com)) and 8-port managed switch | ~$150 *est.* | Maintenance over WireGuard; separate lab network ([remote-lab.md](remote-lab.md)) | Firmware yes (OpenWrt) | |

## 3. Full (~$4,900 more, ~$8,600 total)

| Item | Price | Why | Open | Have |
|---|---|---|---|---|
| [Siglent SSA3021X](https://www.tequipment.net/Siglent/rf-microwave-emi/) spectrum analyzer | $1,395 | Calibrated measurements, network control | No | |
| [USRP B210](https://www.ettus.com/all-products/UB210-KIT/) | $2,387 | 2×2 reference SDR used by most research | Drivers (UHD) yes | |
| [LimeSDR Mini 2.0](https://www.crowdsupply.com/lime-micro/limesdr-mini-2-0) | $630 | Second wideband SDR family | Hardware yes | |
| [RFD900x](https://irlock.com/products/rfd900-telemetry-bundle) bundle | $473.81 | Long-range telemetry, common on fixed wings | Firmware yes (SiK-based) | |

## Swaps and notes

- **HackRF:** HackRF One is retired (not in stock); HackRF Pro is about $400. Fully open, but half duplex. A good alternative to one Pluto.
- **LoRa:** RAK WisBlock (about $20–26) instead of Heltec if we want modular boards.
- **Small x86 host:** an Intel N100 mini PC instead of the Pi if we need more CPU for SDR work (price not checked).
- Check stock and prices before buying; several pages were out of stock in October 2026.
