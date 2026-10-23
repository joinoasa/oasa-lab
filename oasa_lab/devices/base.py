"""The common shape of a lab device: open, info, close, power_cycle.

Every driver has two backends. `sim` needs no hardware and runs in CI. `hardware` talks to the
real thing and imports its (optional) libraries only when opened. `info()` always returns the same
keys, so the manifest looks the same either way, with `backend` telling which it was.
"""

from __future__ import annotations

import time
from typing import Any


class DeviceError(Exception):
    """The device did not do what was asked."""


class Unavailable(DeviceError):
    """The device, a library or a tool it needs is not there. Suites skip on this."""


INFO_KEYS = ("id", "kind", "backend", "model", "serial", "firmware")


class Device:
    kind = "device"

    def __init__(self, dev_id: str, cfg: dict[str, Any], backend: str, hub: "Any | None" = None, world: "Any | None" = None):
        self.id, self.cfg, self.backend, self.hub, self.world = dev_id, dict(cfg), backend, hub, world
        self.is_open = False

    # --- to override ---------------------------------------------------------------------
    def _open(self) -> None:
        raise NotImplementedError

    def _close(self) -> None:
        pass

    def _info(self) -> dict[str, Any]:
        """model, serial, firmware, plus anything else worth recording."""
        raise NotImplementedError

    # --- the common shape ----------------------------------------------------------------
    @property
    def hub_port(self) -> int | None:
        p = self.cfg.get("hub_port")
        return int(p) if p is not None else None

    def open(self) -> "Device":
        if self.is_open:
            return self
        if self.hub is not None and self.hub_port is not None and not self.hub.is_on(self.hub_port):
            raise Unavailable(f"{self.id}: hub port {self.hub_port} is powered off")
        self._open()
        self.is_open = True
        return self

    def info(self) -> dict[str, Any]:
        if not self.is_open:
            raise DeviceError(f"{self.id}: open() before info()")
        data = {"id": self.id, "kind": self.kind, "backend": self.backend, "hub_port": self.hub_port}
        data.update(self._info())
        for k in ("model", "serial", "firmware"):
            data[k] = str(data.get(k) or "n/a")
        return data

    def close(self) -> None:
        if self.is_open:
            try:
                self._close()
            finally:
                self.is_open = False

    def power_cycle(self, off_s: float = 2.0, timeout_s: float = 60.0) -> float:
        """Cut the device's USB power through the hub, turn it on again, reopen it.
        Returns how long it took to come back (seconds)."""
        if self.hub is None or self.hub_port is None:
            raise Unavailable(f"{self.id}: no hub port in the inventory, cannot power cycle")
        t0 = time.monotonic()
        self.close()
        self.hub.power_cycle(self.hub_port, off_s)
        end = t0 + timeout_s
        last: Exception | None = None
        while time.monotonic() < end:
            try:
                self.open()
                return time.monotonic() - t0
            except DeviceError as exc:  # still booting
                last = exc
                time.sleep(0.5)
        raise DeviceError(f"{self.id}: did not come back within {timeout_s:.0f} s ({last})")

    def __enter__(self) -> "Device":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()
