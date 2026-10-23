"""Radio profiles: channel settings that make the simulated channel behave like a real radio.

A profile is a small table of settings (see airtime.py for what they mean). Every number has a
source in docs/profiles.md, with the date it was checked. Numbers we could not confirm are marked
*est.* there. Use one with `ChannelSettings.for_profile("lora-sf7-bw125", loss=0.05)`,
`lab.channel(profile="lora-sf7-bw125", loss=0.05)` or `oasa-lab channel --profile lora-sf7-bw125`.
Settings you pass override the profile's.

Direction names: A is the ground side, B the vehicle or node side. For mLRS, A to B is the uplink.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .airtime import airtime_s


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    settings: dict[str, Any] = field(default_factory=dict)


def _lora(sf: int) -> dict[str, Any]:
    # CR 4/5, 8-symbol preamble, explicit header, CRC on, low data rate optimize when needed.
    return dict(airtime="lora", lora_sf=sf, lora_bw_hz=125_000, lora_cr=1, lora_preamble=8,
                lora_explicit_header=True, lora_crc=True, lora_ldro=-1, mtu=255, half_duplex=True)


_SIK = dict(airtime="bitrate", air_kbps=64.0, fixed_us=1625.0, serial_bps=57_600, half_duplex=True)
_MLRS_UP, _MLRS_DOWN = 64, 82  # serial payload bytes per frame, A to B and B to A

_LIST = [
    Profile("ideal", "No impairments and no airtime. For reference.", {}),
    Profile("lora-sf7-bw125", "LoRa SF7, 125 kHz, CR 4/5. Half-duplex.", _lora(7)),
    Profile("lora-sf9-bw125", "LoRa SF9, 125 kHz, CR 4/5. Half-duplex.", _lora(9)),
    Profile("lora-sf12-bw125", "LoRa SF12, 125 kHz, CR 4/5, low data rate optimize. Half-duplex.", _lora(12)),
    Profile("sik-64k", "SiK telemetry radio, 64 kbps air rate, 57600 baud serial, ECC off. Half-duplex.",
            {**_SIK, "overhead_bytes": 2, "fec_ratio": 1.0, "mtu": 250}),
    Profile("sik-64k-ecc", "SiK, 64 kbps air rate, Golay ECC on (twice the bits on air). Half-duplex.",
            {**_SIK, "overhead_bytes": 6, "fec_ratio": 2.0, "mtu": 118}),
    Profile("mlrs-19hz", "mLRS 19 Hz mode (53 ms frames; 64 B up, 82 B down per frame). Fixed slots.",
            dict(airtime="slots", slot_ms=53.0, slot_payload=_MLRS_UP, slot_payload_rev=_MLRS_DOWN, mtu=280)),
    Profile("mlrs-31hz", "mLRS 31 Hz mode (32 ms frames; 64 B up, 82 B down per frame). Fixed slots.",
            dict(airtime="slots", slot_ms=32.0, slot_payload=_MLRS_UP, slot_payload_rev=_MLRS_DOWN, mtu=280)),
    Profile("mlrs-50hz", "mLRS 50 Hz mode (20 ms frames; 64 B up, 82 B down per frame). Fixed slots.",
            dict(airtime="slots", slot_ms=20.0, slot_payload=_MLRS_UP, slot_payload_rev=_MLRS_DOWN, mtu=280)),
    Profile("wifi-wfb", "WFB-ng on 802.11n, 20 MHz, MCS 1 (13 Mbps), long GI. Full-duplex approximation.",
            dict(airtime="bitrate", air_kbps=13_000.0, symbol_us=4.0, fixed_us=40.0, extra_bits=22,
                 overhead_bytes=56, mtu=1445)),
]
PROFILES: dict[str, Profile] = {p.name: p for p in _LIST}


def profile_names() -> list[str]:
    return list(PROFILES)


def profile_settings(name: str) -> dict[str, Any]:
    """A copy of the profile's settings, including its name as `profile`."""
    try:
        p = PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown profile {name!r}. Profiles: {', '.join(PROFILES)}") from None
    return {**p.settings, "profile": name}


def bitrate_bps(s: dict[str, Any]) -> float:
    """The headline bit rate of a profile, in bits per second (0 = unlimited).

    LoRa: the raw rate, SF * BW / 2^SF * 4/(4+CR). Bitrate models: the air rate. Slots: the A to B
    payload rate. It is the number people quote; real throughput is lower (see the airtime).
    """
    kind = s.get("airtime", "")
    if kind == "lora":
        sf, bw, cr = s["lora_sf"], s["lora_bw_hz"], s.get("lora_cr", 1)
        return sf * bw / (1 << sf) * 4 / (4 + cr)
    if kind == "bitrate":
        return float(s["air_kbps"]) * 1000.0
    if kind == "slots":
        return s["slot_payload"] * 8 / (s["slot_ms"] / 1000.0)
    return 0.0


def describe(name: str, probe_bytes: int = 64) -> dict[str, Any]:
    s = profile_settings(name)
    return {
        "name": name,
        "description": PROFILES[name].description,
        "bitrate_bps": bitrate_bps(s),
        "max_payload": s.get("mtu", 0),
        "airtime_s": airtime_s(s, probe_bytes) if s.get("airtime") else 0.0,
        "half_duplex": bool(s.get("half_duplex", False)),
    }


def _fmt_rate(bps: float) -> str:
    if bps <= 0:
        return "unlimited"
    return f"{bps / 1000:.1f} kbps" if bps < 1_000_000 else f"{bps / 1_000_000:.1f} Mbps"


def _fmt_time(sec: float) -> str:
    if sec <= 0:
        return "0"
    return f"{sec * 1000:.2f} ms" if sec < 10 else f"{sec:.1f} s"


def format_table(probe_bytes: int = 64) -> str:
    rows = [describe(n, probe_bytes) for n in PROFILES]
    head = ("profile", "bit rate", "max payload", f"{probe_bytes}-byte airtime", "description")
    body = [(r["name"], _fmt_rate(r["bitrate_bps"]), str(r["max_payload"] or "none"),
             _fmt_time(r["airtime_s"]), r["description"]) for r in rows]
    widths = [max(len(x[i]) for x in [head, *body]) for i in range(4)]
    lines = ["  ".join(x[i].ljust(widths[i]) for i in range(4)) + "  " + x[4] for x in [head, *body]]
    return "\n".join(lines)
