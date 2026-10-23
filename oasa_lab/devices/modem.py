"""LoRa modems on a serial port (the Heltec boards), and the tap that records what they hear.

The modem firmware speaks oasa-radio's serial framing (docs/serial-framing.md in oasa-radio): a
packet in goes out on the air, a packet heard on the air comes out. Settings (frequency, spreading
factor, power) are fixed in the firmware; `lab.toml` says what they were flashed to, and the SDR
recording checks the airtime independently.

`ModemLink` sits between the serial port and whatever uses the modem. It decodes every frame in
both directions and keeps it with a time stamp. The frames the modem delivers are "what anyone on
the air would get": the air check scans them. It can also show a pseudo-terminal, so a real
`oasa-radio bridge --radio serial --device PATH` runs through the tap without knowing.
"""

from __future__ import annotations

import errno
import os
import pty
import select
import threading
import time
import tty
from dataclasses import dataclass
from typing import Any, Protocol

from .base import Device, DeviceError, Unavailable
from .framing import DEFAULT_MTU, FrameDecoder, encode_frame
from .sim import SimModemPort, SimWorld, radio_airtime_settings


class PortLike(Protocol):
    def read(self, timeout: float = 0.05) -> bytes: ...
    def write(self, data: bytes) -> None: ...
    def close(self) -> None: ...


class SerialPort:
    """pyserial (BSD-3-Clause), imported only when a real modem is opened."""

    def __init__(self, path: str, baud: int, dtr: bool = True, rts: bool = False):
        try:
            import serial  # type: ignore
        except ImportError:
            raise Unavailable("pyserial is not installed (pip install 'oasa-lab[hw]')") from None
        try:
            self.s = serial.Serial()
            self.s.port, self.s.baudrate, self.s.timeout = path, baud, 0.05
            self.s.dtr, self.s.rts = dtr, rts  # set before open: some boards reset on RTS
            self.s.open()
        except (OSError, serial.SerialException) as exc:
            raise DeviceError(f"cannot open {path}: {exc}") from None

    def read(self, timeout: float = 0.05) -> bytes:
        try:
            return self.s.read(self.s.in_waiting or 1)
        except OSError as exc:
            raise DeviceError(f"serial read failed: {exc}") from None

    def write(self, data: bytes) -> None:
        self.s.write(data)
        self.s.flush()

    def close(self) -> None:
        self.s.close()


@dataclass
class Frame:
    ts: float      # wall-clock seconds when the bytes reached the host
    data: bytes    # the packet inside the frame


class ModemLink:
    def __init__(self, name: str, port: PortLike, mtu: int = DEFAULT_MTU, gate: "Callable[[str], None] | None" = None):
        self.name, self.port, self.mtu = name, port, mtu
        self.gate = gate  # called before any byte goes to the modem; raises TxRefused to stop it
        self.rx: list[Frame] = []   # modem to host: what it heard on the air
        self.tx: list[Frame] = []   # host to modem: what it was asked to send
        self.dec_rx, self.dec_tx = FrameDecoder(mtu), FrameDecoder(mtu)
        self._master = self._slave = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.errors: list[str] = []
        self._threads = [threading.Thread(target=self._dev_loop, daemon=True, name=f"{name}-rx")]
        for t in self._threads:
            t.start()

    # --- use ------------------------------------------------------------------------------
    def send_packet(self, packet: bytes) -> None:
        if self.gate:
            self.gate("send_packet")
        self.port.write(encode_frame(packet, self.mtu))
        with self._lock:
            self.tx.append(Frame(time.time(), packet))

    def attach_pty(self) -> str:
        """A pseudo-terminal path to give a bridge as `--device`. Bytes pass both ways and are recorded."""
        if self._master is None:
            self._master, self._slave = pty.openpty()
            tty.setraw(self._slave)
            os.set_blocking(self._master, False)
            t = threading.Thread(target=self._host_loop, daemon=True, name=f"{self.name}-tx")
            self._threads.append(t)
            t.start()
        return os.ttyname(self._slave)

    def rx_packets(self) -> list[Frame]:
        with self._lock:
            return list(self.rx)

    def wait_idle(self, quiet: float = 0.5, timeout: float = 30.0) -> None:
        """Wait until nothing has come from the modem for `quiet` seconds."""
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                last = self.rx[-1].ts if self.rx else 0.0
            if time.time() - max(last, start) >= quiet:
                return
            time.sleep(0.02)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(2)
        for fd in (self._master, self._slave):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._master = self._slave = None

    # --- threads --------------------------------------------------------------------------
    def _dev_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.port.read(0.05)
            except DeviceError as exc:
                self.errors.append(str(exc))
                return
            if not data:
                continue
            ts = time.time()
            with self._lock:
                self.rx += [Frame(ts, p) for p in self.dec_rx.feed(data)]
            if self._master is not None:
                try:
                    os.write(self._master, data)
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EIO):
                        self.errors.append(f"pty write: {exc}")

    def _host_loop(self) -> None:
        while not self._stop.is_set():
            r, _, _ = select.select([self._master], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(self._master, 4096)
            except OSError:
                continue
            if data and self.gate:
                try:
                    self.gate("pty write")
                except DeviceError as exc:
                    self.errors.append(str(exc))
                    continue
            if data:
                self.port.write(data)
                ts = time.time()
                with self._lock:
                    self.tx += [Frame(ts, p) for p in self.dec_tx.feed(data)]


class SerialRadio(Device):
    """One LoRa modem. `link` is its tap."""

    kind = "serial-radio"

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.link: ModemLink | None = None
        self.port: PortLike | None = None
        self.mtu = int(self.cfg.get("mtu", DEFAULT_MTU))

    @property
    def radio(self) -> dict[str, Any]:
        return dict(self.cfg.get("radio") or {})

    def airtime_settings(self) -> dict[str, Any]:
        """The LoRa airtime model for this modem's declared settings."""
        return radio_airtime_settings(self.radio)

    def _open(self) -> None:
        if self.backend == "sim":
            if not isinstance(self.world, SimWorld):
                raise Unavailable(f"{self.id}: the simulated backend needs a SimWorld")
            self.port = SimModemPort(self.id, self.world.get_air(self.radio), self.radio, self.mtu)
        else:
            path = str(self.cfg.get("port", ""))
            if not path:
                raise DeviceError(f"{self.id}: 'port' is missing in lab.toml")
            self._check_usb_serial(path)
            self.port = SerialPort(path, int(self.cfg.get("baud", 115200)),
                                   bool(self.cfg.get("dtr", True)), bool(self.cfg.get("rts", False)))
        self.link = ModemLink(self.id, self.port, self.mtu, gate=self.require_tx)

    def _check_usb_serial(self, path: str) -> None:
        """If lab.toml gives a USB serial number, the port must belong to that board."""
        want = self.cfg.get("usb_serial")
        if not want:
            return
        try:
            from serial.tools import list_ports  # type: ignore
        except ImportError:
            raise Unavailable("pyserial is not installed (pip install 'oasa-lab[hw]')") from None
        real = os.path.realpath(path)
        for p in list_ports.comports():
            if os.path.realpath(p.device) == real:
                if str(p.serial_number) != str(want):
                    raise DeviceError(f"{self.id}: {path} is USB serial {p.serial_number}, lab.toml says {want}")
                return
        raise DeviceError(f"{self.id}: {path} not found among the serial ports")

    def _close(self) -> None:
        if self.link:
            self.link.stop()
        if self.port:
            self.port.close()

    def _info(self) -> dict[str, Any]:
        s = self.airtime_settings()
        return {"model": self.cfg.get("model", "LoRa modem"), "serial": self.cfg.get("serial") or self.cfg.get("usb_serial"),
                "firmware": self.cfg.get("firmware"), "firmware_sha256": self.cfg.get("firmware_sha256"),
                "port": self.cfg.get("port"), "mtu": self.mtu,
                "radio": {"freq_mhz": float(self.radio.get("freq_mhz", 915.0)),
                          "tx_power_dbm": self.radio.get("tx_power_dbm"),
                          "airtime": {k: v for k, v in s.items() if k.startswith(("lora_", "airtime"))},
                          "profile": s.get("profile")}}
