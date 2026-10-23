"""Time stamps and the live JSON-lines log."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_rfc3339() -> str:
    """UTC time, RFC 3339, millisecond precision, e.g. 2026-10-23T04:10:38.245Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonlLog:
    """Append-only log. One JSON object per line, flushed at once so `tail -f` works."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = open(self.path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        with self._lock:
            if self._fh.closed:
                return
            record = {"ts": now_rfc3339(), "event": event}
            record.update({k: v for k, v in fields.items() if k not in ("ts", "event")})
            self._fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()
