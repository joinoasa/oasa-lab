"""Read a classic pcap with raw-IPv4 UDP packets (standard library only)."""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass


class PcapError(ValueError):
    pass


@dataclass
class Packet:
    ts: float
    src: tuple[str, int]
    dst: tuple[str, int]
    payload: bytes


def parse_pcap(data: bytes) -> list[Packet]:
    if len(data) < 24:
        raise PcapError("file shorter than a pcap header")
    magic = struct.unpack("<I", data[:4])[0]
    if magic == 0xA1B2C3D4:
        end = "<"
    elif magic == 0xD4C3B2A1:
        end = ">"
    else:
        raise PcapError("bad pcap magic")
    _, _, _, _, _, link = struct.unpack(end + "HHiIII", data[4:24])
    if link != 101:
        raise PcapError(f"unsupported link type {link} (expected 101, raw IP)")
    out: list[Packet] = []
    pos = 24
    while pos < len(data):
        if pos + 16 > len(data):
            raise PcapError("truncated record header")
        sec, usec, incl, _orig = struct.unpack(end + "IIII", data[pos:pos + 16])
        frame = data[pos + 16:pos + 16 + incl]
        if len(frame) != incl:
            raise PcapError("truncated packet")
        pos += 16 + incl
        if len(frame) < 28 or frame[0] >> 4 != 4 or frame[9] != 17:
            raise PcapError("record is not an IPv4 UDP packet")
        ihl = (frame[0] & 0x0F) * 4
        total = struct.unpack("!H", frame[2:4])[0]
        sport, dport, ulen = struct.unpack("!HHH", frame[ihl:ihl + 6])
        if total != len(frame) or ihl + ulen != total:
            raise PcapError("inconsistent IP/UDP lengths")
        out.append(Packet(sec + usec / 1e6,
                          (socket.inet_ntoa(frame[12:16]), sport),
                          (socket.inet_ntoa(frame[16:20]), dport),
                          frame[ihl + 8:ihl + ulen]))
    return out
