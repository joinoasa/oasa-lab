"""Classic libpcap files.

Every packet is stored as a raw IPv4 packet (LINKTYPE_RAW, 101) holding a UDP datagram, so
Wireshark shows source and destination address and port and decodes the UDP payload. The UDP
checksum is 0 ("not computed", legal for IPv4). Timestamps are the wall-clock time of forwarding.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

LINKTYPE_RAW = 101
_MAGIC = 0xA1B2C3D4
Addr = tuple[str, int]


def _ip_checksum(header: bytes) -> int:
    total = sum(struct.unpack("!%dH" % (len(header) // 2), header))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def build_ipv4_udp(src: Addr, dst: Addr, payload: bytes, ident: int = 0) -> bytes:
    total = 20 + 8 + len(payload)
    if total > 65535:
        raise ValueError("packet too large for IPv4")
    header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45, 0, total, ident & 0xFFFF, 0x4000, 64, 17, 0,
        socket.inet_aton(src[0]), socket.inet_aton(dst[0]),
    )
    header = header[:10] + struct.pack("!H", _ip_checksum(header)) + header[12:]
    udp = struct.pack("!HHHH", src[1], dst[1], 8 + len(payload), 0)
    return header + udp + payload


class PcapWriter:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = open(self.path, "wb")
        self._fh.write(struct.pack("<IHHiIII", _MAGIC, 2, 4, 0, 0, 65535, LINKTYPE_RAW))
        self._fh.flush()
        self._lock = threading.Lock()
        self._ident = 0
        self.count = 0

    def write(self, src: Addr, dst: Addr, payload: bytes, ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        with self._lock:
            if self._fh.closed:
                return
            self._ident += 1
            frame = build_ipv4_udp(src, dst, payload, self._ident)
            sec = int(ts)
            usec = int((ts - sec) * 1_000_000)
            self._fh.write(struct.pack("<IIII", sec, usec, len(frame), len(frame)) + frame)
            self._fh.flush()
            self.count += 1

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()


@dataclass
class PcapPacket:
    ts: float
    src: Addr
    dst: Addr
    payload: bytes


def read_pcap(path: Path) -> list[PcapPacket]:
    """Read a file written by PcapWriter (little-endian, LINKTYPE_RAW, IPv4/UDP)."""
    data = Path(path).read_bytes()
    magic, _vmaj, _vmin, _tz, _sig, _snap, link = struct.unpack("<IHHiIII", data[:24])
    if magic != _MAGIC or link != LINKTYPE_RAW:
        raise ValueError("not a pcap written by oasa-lab")
    out: list[PcapPacket] = []
    pos = 24
    while pos < len(data):
        sec, usec, incl, _orig = struct.unpack("<IIII", data[pos:pos + 16])
        frame = data[pos + 16:pos + 16 + incl]
        pos += 16 + incl
        ihl = (frame[0] & 0x0F) * 4
        src_ip, dst_ip = socket.inet_ntoa(frame[12:16]), socket.inet_ntoa(frame[16:20])
        sport, dport, ulen, _ck = struct.unpack("!HHHH", frame[ihl:ihl + 8])
        out.append(PcapPacket(sec + usec / 1e6, (src_ip, sport), (dst_ip, dport),
                              frame[ihl + 8:ihl + ulen]))
    return out
