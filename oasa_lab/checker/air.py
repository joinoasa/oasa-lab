"""Airtime, worked out again by the checker (standard library only).

This repeats the formulas of oasa_lab/airtime.py on purpose. The checker must not import the
lab, so a bug there cannot hide a bug here. It is written from the datasheet formula, not copied
from the lab's code, and a test in suites/l0-lab compares the two over a grid of settings.

Settings come from the manifest (`channels[].settings`). See docs/checker.md, check 5.
"""

from __future__ import annotations

import math
from typing import Any, Mapping


def _lora(nbytes: int, s: Mapping[str, Any]) -> float:
    """Semtech SX126x datasheet (6.1.4): T = T_preamble + T_payload.

    T_sym = 2^SF / BW. T_preamble = (n_preamble + 4.25) T_sym (plus 2 symbols for SF5 and SF6).
    T_payload = N_payload T_sym, with N_payload = 8 + max(ceil(X / (4 (SF - 2 DE))) (CR + 4), 0)
    and X = 8 PL - 4 SF + 8 [SF >= 7] + 16 CRC + 20 [explicit header]. DE is 0 for SF5 and SF6.
    """
    sf, bw = int(s["lora_sf"]), int(s["lora_bw_hz"])
    cr = int(s.get("lora_cr", 1))
    t_sym = 2.0 ** sf / bw
    ldro = int(s.get("lora_ldro", -1))
    de = ldro if ldro in (0, 1) else (1 if t_sym > 0.016 else 0)
    if sf <= 6:
        de = 0
    x = (8 * nbytes - 4 * sf + (8 if sf >= 7 else 0) + (16 if s.get("lora_crc", True) else 0)
         + (20 if s.get("lora_explicit_header", True) else 0))
    blocks = max(0, -(-x // (4 * (sf - 2 * de))))
    n_payload = 8 + blocks * (cr + 4)
    n_pre = int(s.get("lora_preamble", 8)) + 4.25 + (2 if sf <= 6 else 0)
    return (n_pre + n_payload) * t_sym


def _bitrate(nbytes: int, s: Mapping[str, Any]) -> float:
    rate = float(s["air_kbps"]) * 1000.0
    bits = (nbytes + int(s.get("overhead_bytes", 0))) * 8 * float(s.get("fec_ratio", 1.0)) + int(s.get("extra_bits", 0))
    sym = float(s.get("symbol_us", 0)) / 1e6
    if sym > 0:
        data = math.ceil(round(bits / (rate * sym), 9)) * sym
    else:
        data = bits / rate
    return float(s.get("fixed_us", 0)) / 1e6 + data


def _slots(nbytes: int, s: Mapping[str, Any], direction: str) -> float:
    cap = int(s["slot_payload"])
    if direction == "b_to_a" and int(s.get("slot_payload_rev", 0)) > 0:
        cap = int(s["slot_payload_rev"])
    return max(1, -(-nbytes // cap)) * float(s["slot_ms"]) / 1000.0


def airtime(s: Mapping[str, Any], nbytes: int, direction: str) -> float:
    """Seconds on the air; 0.0 if the settings have no airtime model."""
    kind = s.get("airtime", "")
    if not kind:
        return 0.0
    if kind == "lora":
        return _lora(nbytes, s)
    if kind == "bitrate":
        return _bitrate(nbytes, s)
    if kind == "slots":
        return _slots(nbytes, s, direction)
    raise ValueError(f"unknown airtime model {kind!r}")


# The time stamp of a packet in the capture is when it started to go on the air. Time stamps
# are whole microseconds, so allow a little for rounding.
TOL_ABS_S = 25e-6
TOL_REL = 0.002


def check_air(cid: str, s: Mapping[str, Any],
              by_dir: Mapping[str, list]) -> tuple[list[str], dict[str, Any]]:
    """Check one channel's capture against its airtime model.

    by_dir maps "a_to_b" / "b_to_a" to that direction's capture packets (objects with .ts and
    .payload). Rules, for packets sorted by time stamp:
      - on one medium (both directions if half_duplex, else each direction alone) a packet starts
        no sooner than the previous one's airtime after the previous one started;
      - with duty_cycle d, a direction's next packet starts no sooner than airtime / d after the
        previous one started;
      - with slots, every start in a direction is a whole number of slots from the first.
    Returns (errors, details).
    """
    errs: list[str] = []
    kind = s.get("airtime", "")
    details: dict[str, Any] = {"model": kind, "half_duplex": bool(s.get("half_duplex", False)), "profile": s.get("profile", "")}
    if not kind:
        return errs, details
    duty = float(s.get("duty_cycle", 0) or 0)
    tagged = {d: sorted(((p.ts, len(p.payload)) for p in pk), key=lambda x: x[0]) for d, pk in by_dir.items()}
    groups = {"both directions": [(ts, n, d) for d, v in tagged.items() for ts, n in v]} \
        if s.get("half_duplex") else {d: [(ts, n, d) for ts, n in v] for d, v in tagged.items()}
    worst = None  # smallest (gap - needed) in seconds over the medium rule
    checked = 0
    for name, seq in groups.items():
        seq.sort(key=lambda x: x[0])
        for i in range(len(seq) - 1):
            ts, n, d = seq[i]
            gap = seq[i + 1][0] - ts
            need = airtime(s, n, d)
            checked += 1
            margin = gap - need
            worst = margin if worst is None else min(worst, margin)
            if gap < need - (TOL_ABS_S + TOL_REL * need) and len(errs) < 20:
                errs.append(f"{cid} {name}: packet {i + 2} starts {gap * 1000:.3f} ms after the one before, "
                            f"which needs {need * 1000:.3f} ms on the air (packets overlap)")
    if duty:
        for d, v in tagged.items():
            for i in range(len(v) - 1):
                gap, need = v[i + 1][0] - v[i][0], airtime(s, v[i][1], d) / duty
                if gap < need - (TOL_ABS_S + TOL_REL * need) and len(errs) < 20:
                    errs.append(f"{cid} {d}: packet {i + 2} starts {gap * 1000:.3f} ms after the one before; "
                                f"duty cycle {duty} needs {need * 1000:.3f} ms")
    if kind == "slots":
        slot = float(s["slot_ms"]) / 1000.0
        for d, v in tagged.items():
            for i in range(1, len(v)):
                k = (v[i][0] - v[0][0]) / slot
                off = abs(k - round(k)) * slot
                if off > TOL_ABS_S + TOL_REL * slot and len(errs) < 20:
                    errs.append(f"{cid} {d}: packet {i + 1} is not on the slot grid (off by {off * 1000:.3f} ms)")
                    break
    details.update(pairs_checked=checked, smallest_margin_ms=None if worst is None else round(worst * 1000, 4))
    return errs, details
