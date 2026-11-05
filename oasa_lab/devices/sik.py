"""SiK telemetry radios (Holybro SiK V3 and other SiK radios) on a USB serial port.

A SiK radio is transparent: bytes in one radio's serial port come out of the other's. `oasa-radio bridge
--radio serial` frames its packets on that byte stream, so the lab's tap (`ModemLink`, the same as for the
LoRa modem) can see every frame each radio delivered: what anyone with a SiK radio on the same settings
would get. The settings are read with AT commands when the radio is opened (`sikat.py`), before any
packet flows, and go into the manifest. The driver never changes a setting; `oasa-radio sik set` does.

lab.toml:

    [devices.sik_a]
    kind = "sik"
    port = "/dev/serial/by-id/usb-FTDI_..."   # the radio's USB serial port
    usb_serial = "A10ABCDE"                    # the suite refuses a port that belongs to another radio
    serial = "A10ABCDE"
    baud = 57600                               # the radio's SERIAL_SPEED
    mtu = 242                                  # oasa-radio bridge --mtu (one frame per SiK packet)
    [devices.sik_a.radio]
    profile = "sik-64k"                        # the airtime model
    [devices.sik_a.sik]                        # what the radio must report (ATI5), for --sik-expect
    netid = 4242
    air_speed = 64
    ecc = 0
    mavlink = 0
"""

from __future__ import annotations

from typing import Any

from ..profiles import profile_settings
from . import sikat
from .base import Device, DeviceError, Unavailable
from .modem import ModemLink, PortLike, SerialPort, check_usb_serial
from .sim import SimWorld

DEFAULT_MTU = 242   # framed, 249 bytes: fits one SiK packet (250 bytes with ECC off)


class SikRadio(Device):
    kind = "sik"

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.port: PortLike | None = None
        self._link: ModemLink | None = None
        self.report: sikat.Report | None = None
        self.mtu = int(self.cfg.get("mtu", DEFAULT_MTU))
        self.baud = int(self.cfg.get("baud", 57_600))

    @property
    def radio(self) -> dict[str, Any]:
        return dict(self.cfg.get("radio") or {"profile": "sik-64k"})

    @property
    def expect(self) -> dict[str, int]:
        """The settings lab.toml says this radio must have (SiK names)."""
        return sikat.normalize(self.cfg.get("sik") or {})

    @property
    def settings(self) -> dict[str, int]:
        return dict(self.report.settings) if self.report else {}

    def model(self) -> dict[str, Any]:
        """The airtime model of this radio's profile."""
        return profile_settings(str(self.radio.get("profile", "sik-64k")))

    def _open(self) -> None:
        if self.backend == "sim":
            if not isinstance(self.world, SimWorld):
                raise Unavailable(f"{self.id}: the simulated backend needs a SimWorld")
            from .simsik import SimSikPort
            sim = {**self.expect, **sikat.normalize(self.cfg.get("sim") or {})}
            air = self.world.get_sik_air(str(self.radio.get("profile", "sik-64k")))
            self.port = SimSikPort(self.id, air, sim)
        else:
            path = str(self.cfg.get("port", ""))
            if not path:
                raise DeviceError(f"{self.id}: 'port' is missing in lab.toml")
            check_usb_serial(self.id, path, self.cfg.get("usb_serial"))
            self.port = SerialPort(path, self.baud, bool(self.cfg.get("dtr", True)), bool(self.cfg.get("rts", False)))
        try:
            self.report = sikat.read_report(self.port)
        except sikat.AtError as exc:
            self.port.close()
            raise DeviceError(f"{self.id}: {exc}") from None

    @property
    def link(self) -> ModemLink:
        """The tap on the serial port (made on first use: no AT command may run after this)."""
        if self._link is None:
            if self.port is None:
                raise DeviceError(f"{self.id}: open() first")
            self._link = ModemLink(self.id, self.port, self.mtu, gate=self.require_tx)
        return self._link

    def reread(self) -> sikat.Report:
        """Read the settings again (only while no tap reads the port)."""
        if self._link is not None:
            raise DeviceError(f"{self.id}: the tap owns the port; read settings through oasa-radio sik show instead")
        self.report = sikat.read_report(self.port)
        return self.report

    def remote(self, commands: list[str]) -> None:
        """Send `RT...` commands to the linked radio over the air (H1 tests). Each must answer OK."""
        self.require_tx("remote AT command")
        if self._link is not None:
            raise DeviceError(f"{self.id}: the tap owns the port")
        try:
            sikat.remote(self.port, commands)
        except sikat.AtError as exc:
            raise DeviceError(f"{self.id}: {exc}") from None

    def _close(self) -> None:
        if self._link:
            self._link.stop()
        if self.port:
            self.port.close()

    def _info(self) -> dict[str, Any]:
        s = self.settings
        return {"model": self.cfg.get("model", "SiK telemetry radio"),
                "serial": self.cfg.get("serial") or self.cfg.get("usb_serial"),
                "firmware": self.report.version if self.report else None,
                "port": self.cfg.get("port"), "baud": self.baud, "mtu": self.mtu,
                "settings": s, "expect": self.expect,
                "radio": {"profile": self.radio.get("profile", "sik-64k"),
                          "min_freq_khz": s.get("MIN_FREQ"), "max_freq_khz": s.get("MAX_FREQ"),
                          "tx_power_dbm": s.get("TXPOWER"),
                          "airtime": {k: v for k, v in self.model().items() if k != "profile"}}}
