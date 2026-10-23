"""Helpers for suites: UDP endpoints, packet makers and a MAVLink frame for the negative control."""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Iterable

Addr = tuple[str, int]


class Endpoint:
    """A UDP socket on localhost with a thread that collects everything it receives."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
        except OSError:
            pass
        self.sock.bind((host, port))
        self.sock.settimeout(0.05)
        self.addr: Addr = self.sock.getsockname()[:2]
        self.received: list[tuple[float, bytes]] = []
        self._last = time.monotonic()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self.received.append((time.monotonic(), data))
            self._last = time.monotonic()

    def send(self, data: bytes, to: Addr) -> None:
        self.sock.sendto(data, to)

    def send_paced(self, packets: Iterable[bytes], to: Addr, batch: int = 50, gap: float = 0.003) -> int:
        """Send in small bursts so no socket buffer overflows. Returns how many were sent."""
        n = 0
        for n, pkt in enumerate(packets, 1):
            self.sock.sendto(pkt, to)
            if n % batch == 0:
                time.sleep(gap)
        return n

    def payloads(self) -> list[bytes]:
        return [d for _, d in self.received]

    def wait_idle(self, quiet: float = 0.3, timeout: float = 15.0) -> None:
        """Wait until nothing has arrived for `quiet` seconds."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if time.monotonic() - self._last >= quiet:
                return
            time.sleep(0.02)

    def close(self) -> None:
        self._stop = True
        self._thread.join(2)
        self.sock.close()

    def __enter__(self) -> "Endpoint":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def wait_for(cond, timeout: float = 10.0, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def free_udp_port(host: str = "127.0.0.1") -> int:
    """An unused UDP port, for programs that need a number up front."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def make_packet(seq: int, rng, size: int = 64) -> bytes:
    """seq (4 bytes), seq again (4 bytes), then random bytes. The repeat lets a test recognize a
    packet even if the channel corrupted one byte of the number."""
    body = bytes(rng.getrandbits(8) for _ in range(max(0, size - 8)))
    return struct.pack("!II", seq, seq) + body


def identify(data: bytes, sent: dict[int, bytes]) -> tuple[int | None, int]:
    """Match a received packet to a sent one. Returns (seq, bytes that differ), or (None, -1)."""
    if len(data) < 8:
        return None, -1
    a, b = struct.unpack("!II", data[:8])
    for cand in (a, b):
        orig = sent.get(cand)
        if orig is not None and len(orig) == len(data):
            diff = sum(x != y for x, y in zip(orig, data))
            if diff <= 1:
                return cand, diff
    return None, -1


def _crc_x25(data: bytes, crc: int = 0xFFFF) -> int:
    for byte in data:
        tmp = byte ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def mavlink2_heartbeat(seq: int, sysid: int = 1, compid: int = 1) -> bytes:
    """A MAVLink 2 HEARTBEAT frame (message 0, CRC extra 50), in plaintext."""
    payload = struct.pack("<IBBBBB", 0, 2, 3, 81, 4, 3)
    header = struct.pack("<BBBBBBB", len(payload), 0, 0, seq & 0xFF, sysid, compid, 0) + b"\x00\x00"
    crc = _crc_x25(header + payload + bytes([50]))
    return b"\xfd" + header + payload + struct.pack("<H", crc)
