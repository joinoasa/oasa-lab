"""SigMF metadata checks (standard library only). Spec: https://sigmf.org, version 1.x.

We require more than the spec does, because a recording is evidence: sample rate, a centre
frequency and a start time must be there, the dataset hash must match, and the data length must be
a whole number of samples.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

DATATYPE = re.compile(r"^(?P<kind>c|r)(?P<t>f32|f64|i32|i16|u32|u16|i8|u8)(?P<end>_le|_be)?$")
TYPE_BYTES = {"f32": 4, "f64": 8, "i32": 4, "i16": 2, "u32": 4, "u16": 2, "i8": 1, "u8": 1}
VERSION = re.compile(r"^1\.\d+\.\d+")
HEX128 = re.compile(r"^[0-9a-f]{128}$")
SIM_PREFIX = "sim:"  # a device id like this is a simulation with no entry in the hardware manifest
RECORDING_FILE = re.compile(r"^recordings/[A-Za-z0-9._-]+\.sigmf-(meta|data)$")


def sample_bytes(datatype: Any) -> int | None:
    """Bytes per sample for a SigMF datatype string, or None if it is not valid."""
    m = DATATYPE.match(datatype) if isinstance(datatype, str) else None
    if not m:
        return None
    if TYPE_BYTES[m["t"]] > 1 and not m["end"]:
        return None  # multi-byte types must say _le or _be
    if TYPE_BYTES[m["t"]] == 1 and m["end"]:
        return None
    return TYPE_BYTES[m["t"]] * (2 if m["kind"] == "c" else 1)


def sha512_file(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_meta(meta: Any, data_size: int, data_sha512: str) -> tuple[list[str], dict[str, Any]]:
    """Return (problems, facts). `facts` has datatype, sample_rate, samples, frequency, datetime."""
    errs: list[str] = []
    facts: dict[str, Any] = {}
    if not isinstance(meta, dict):
        return ["metadata is not a JSON object"], facts
    g = meta.get("global")
    if not isinstance(g, dict):
        return ["metadata has no 'global' object"], facts
    size = sample_bytes(g.get("core:datatype"))
    if size is None:
        errs.append(f"core:datatype {g.get('core:datatype')!r} is not a valid SigMF datatype")
    else:
        facts["datatype"] = g["core:datatype"]
        if data_size == 0:
            errs.append("the dataset is empty")
        elif data_size % size:
            errs.append(f"dataset is {data_size} bytes, not a whole number of {size}-byte samples")
        else:
            facts["samples"] = data_size // size
    if not (isinstance(g.get("core:version"), str) and VERSION.match(g["core:version"])):
        errs.append("core:version must be a 1.x.y string")
    rate = g.get("core:sample_rate")
    if not (_num(rate) and rate > 0):
        errs.append("core:sample_rate must be a number above 0")
    else:
        facts["sample_rate"] = float(rate)
    if "core:num_channels" in g and g["core:num_channels"] != 1:
        errs.append("only single-channel recordings are supported (core:num_channels must be 1)")
    sha = g.get("core:sha512")
    if not (isinstance(sha, str) and HEX128.match(sha)):
        errs.append("core:sha512 must be 128 lowercase hex characters")
    elif sha != data_sha512:
        errs.append("core:sha512 does not match the dataset file (the data or the metadata was changed)")
    caps = meta.get("captures")
    if not (isinstance(caps, list) and caps):
        errs.append("'captures' must be a non-empty array")
    else:
        last = -1
        for i, c in enumerate(caps):
            if not isinstance(c, dict) or not _int(c.get("core:sample_start")) or c["core:sample_start"] <= last and i:
                errs.append(f"captures[{i}]: core:sample_start must be an integer, rising")
                break
            last = c["core:sample_start"]
        first = caps[0] if isinstance(caps[0], dict) else {}
        if first.get("core:sample_start") != 0:
            errs.append("the first capture segment must start at sample 0")
        if not _num(first.get("core:frequency")):
            errs.append("captures[0] needs core:frequency")
        else:
            facts["frequency"] = float(first["core:frequency"])
        dt = first.get("core:datetime")
        if not (isinstance(dt, str) and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$", dt)):
            errs.append("captures[0] needs core:datetime as an RFC 3339 UTC time")
        else:
            facts["datetime"] = dt
    ann = meta.get("annotations")
    if not isinstance(ann, list):
        errs.append("'annotations' must be an array")
    else:
        n = facts.get("samples")
        for i, a in enumerate(ann):
            if not isinstance(a, dict) or not _int(a.get("core:sample_start")) or a["core:sample_start"] < 0:
                errs.append(f"annotations[{i}]: core:sample_start must be an integer, 0 or more")
                continue
            cnt = a.get("core:sample_count", 1)
            if not _int(cnt) or cnt < 1 or (n is not None and a["core:sample_start"] + cnt > n):
                errs.append(f"annotations[{i}]: core:sample_count must be 1 or more and inside the dataset")
    return errs, facts
