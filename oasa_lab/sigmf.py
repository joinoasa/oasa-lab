"""Write radio recordings as SigMF (https://sigmf.org): a `.sigmf-meta` JSON file and a `.sigmf-data` file.

Our own small writer (the reference library is LGPL-3.0). The checker reads the files again with
its own code. Our extra fields use the `oasa:` prefix. File names: recordings/NN-name.sigmf-*.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__

SIGMF_VERSION = "1.2.0"


def rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha512_file(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:48] or "rec"


SIM_PREFIX = "sim:"   # a device id that starts like this is a simulation, not an entry in the hardware manifest


def is_simulated(device: str) -> bool:
    return device.startswith(SIM_PREFIX)


SAMPLE_BYTES = {"cu8": 2, "ci8": 2, "ci16_le": 4, "cf32_le": 8}


def write_recording(run_dir: Path, index: int, name: str, data: bytes | Path, *, datatype: str, sample_rate: float,
                    center_hz: float, start_time: float, device: str, hw: str, run_id: str, purpose: str,
                    description: str = "", annotations: list[dict[str, Any]] | None = None,
                    burst_check: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the two files and return the manifest entry.

    A `device` that starts with `sim:` is a simulated source: the metadata gets `oasa:simulated: true`.
    """
    simulated = is_simulated(device)
    base = f"recordings/{index:02d}-{slug(name)}"
    (run_dir / "recordings").mkdir(exist_ok=True)
    data_path = run_dir / f"{base}.sigmf-data"
    if isinstance(data, (bytes, bytearray)):
        data_path.write_bytes(bytes(data))
    else:
        shutil.move(str(data), data_path)
    size = data_path.stat().st_size
    n = size // SAMPLE_BYTES[datatype]
    meta = {
        "global": {
            "core:datatype": datatype,
            "core:sample_rate": float(sample_rate),
            "core:version": SIGMF_VERSION,
            "core:num_channels": 1,
            "core:sha512": sha512_file(data_path),
            "core:description": description or purpose,
            "core:recorder": f"oasa-lab {__version__}",
            "core:hw": hw,
            "oasa:run_id": run_id,
            "oasa:device": device,
            "oasa:simulated": simulated,
            "oasa:purpose": purpose,
        },
        "captures": [{"core:sample_start": 0, "core:frequency": float(center_hz), "core:datetime": rfc3339(start_time)}],
        "annotations": [dict(a) for a in (annotations or [])],
    }
    meta_path = run_dir / f"{base}.sigmf-meta"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    entry: dict[str, Any] = {
        "base": base, "meta": f"{base}.sigmf-meta", "data": f"{base}.sigmf-data", "device": device,
        "datatype": datatype, "sample_rate": float(sample_rate), "center_hz": float(center_hz),
        "start": rfc3339(start_time), "samples": n, "purpose": purpose,
        "simulated": simulated,
    }
    if burst_check:
        entry["burst_check"] = burst_check
    return entry
