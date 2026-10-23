"""How long a packet is on the air.

One function, `airtime_s(settings, nbytes, direction)`. `settings` is a mapping (the channel
settings as a dict, as written in the manifest). Three models, chosen by `settings["airtime"]`:

  lora     LoRa symbols. The Semtech SX126x formula (the same integer arithmetic as the SX126x
           driver, `sx126x_get_lora_time_on_air_numerator`). Settings: lora_sf, lora_bw_hz, lora_cr,
           lora_preamble, lora_explicit_header, lora_crc, lora_ldro (-1 = on when a symbol is over 16 ms).
  bitrate  A bit rate plus framing. time = fixed_us + (bytes + overhead_bytes) * 8 * fec_ratio
           / air_kbps + extra_bits / air_kbps. With symbol_us > 0 the data part is rounded up to
           whole symbols (OFDM). Settings: air_kbps, fixed_us, overhead_bytes, fec_ratio,
           symbol_us, extra_bits.
  slots    Fixed time slots (frame-based radios). A packet takes ceil(bytes / payload) slots of
           slot_ms. Settings: slot_ms, slot_payload (A to B), slot_payload_rev (B to A, 0 = same).

The checker (oasa_lab/checker/core.py) repeats this formula on purpose: it must not import the lab.
Keep the two in step. A test in suites/l0-lab compares them.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

MODELS = ("lora", "bitrate", "slots")
LDRO_SYMBOL_S = 0.016  # low data rate optimize when one symbol is longer than 16 ms (Semtech AN)


def lora_symbol_s(sf: int, bw_hz: int) -> float:
    return (1 << sf) / bw_hz


def lora_ldro(sf: int, bw_hz: int, setting: int = -1) -> int:
    """-1 = automatic (on when a symbol is over 16 ms), else 0 or 1 as given."""
    if setting in (0, 1):
        return setting
    return 1 if lora_symbol_s(sf, bw_hz) > LDRO_SYMBOL_S else 0


def lora_airtime_s(nbytes: int, sf: int, bw_hz: int, cr: int = 1, preamble: int = 8,
                   explicit_header: bool = True, crc: bool = True, ldro: int = -1) -> float:
    """cr is 1..4 for coding rate 4/5..4/8. Same steps as the Semtech SX126x driver."""
    ldro = lora_ldro(sf, bw_hz, ldro)
    num = 8 * nbytes + (16 if crc else 0) - 4 * sf + (20 if explicit_header else 0)
    if sf <= 6:
        den = 4 * sf
    else:
        num += 8
        den = 4 * (sf - 2) if ldro else 4 * sf
    num = max(num, 0)
    symbols = -(-num // den) * (cr + 4) + preamble + 12
    if sf <= 6:
        symbols += 2
    return (4 * symbols + 1) * (1 << (sf - 2)) / bw_hz


def bitrate_airtime_s(nbytes: int, air_kbps: float, fixed_us: float = 0.0, overhead_bytes: int = 0,
                      fec_ratio: float = 1.0, symbol_us: float = 0.0, extra_bits: int = 0) -> float:
    bits = (nbytes + overhead_bytes) * 8 * fec_ratio + extra_bits
    if symbol_us > 0:
        per_symbol = air_kbps * symbol_us / 1000.0  # bits in one symbol
        data = math.ceil(round(bits / per_symbol, 9)) * symbol_us / 1e6
    else:
        data = bits / (air_kbps * 1000.0)
    return fixed_us / 1e6 + data


def slots_needed(nbytes: int, payload: int) -> int:
    return max(1, -(-nbytes // payload))


def airtime_s(s: Mapping[str, Any], nbytes: int, direction: str = "a_to_b") -> float:
    """On-air time in seconds of one packet of `nbytes`, or 0.0 when there is no airtime model."""
    kind = s.get("airtime", "")
    if not kind:
        return 0.0
    if kind == "lora":
        return lora_airtime_s(nbytes, int(s["lora_sf"]), int(s["lora_bw_hz"]), int(s.get("lora_cr", 1)),
                              int(s.get("lora_preamble", 8)), bool(s.get("lora_explicit_header", True)),
                              bool(s.get("lora_crc", True)), int(s.get("lora_ldro", -1)))
    if kind == "bitrate":
        return bitrate_airtime_s(nbytes, float(s["air_kbps"]), float(s.get("fixed_us", 0)),
                                 int(s.get("overhead_bytes", 0)), float(s.get("fec_ratio", 1.0)),
                                 float(s.get("symbol_us", 0)), int(s.get("extra_bits", 0)))
    if kind == "slots":
        payload = int(s["slot_payload"])
        if direction == "b_to_a" and int(s.get("slot_payload_rev", 0)):
            payload = int(s["slot_payload_rev"])
        return slots_needed(nbytes, payload) * float(s["slot_ms"]) / 1000.0
    raise ValueError(f"unknown airtime model {kind!r}")
