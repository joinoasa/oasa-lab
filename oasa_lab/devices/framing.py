"""Serial framing of oasa-radio (COBS + CRC-16), for talking to a LoRa modem from the lab.

Wire format: 00 | COBS(len(2, big-endian) | packet | crc16(2, big-endian)) | 00
CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF). Spec: oasa-radio docs/serial-framing.md.
This is the lab's own small copy, so the lab does not need the Rust code to read what a modem says.
"""

from __future__ import annotations

DEFAULT_MTU = 252


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    out = bytearray()
    block = bytearray()
    for byte in data:
        if byte == 0:
            out.append(len(block) + 1)
            out += block
            block.clear()
        else:
            block.append(byte)
            if len(block) == 254:
                out.append(255)
                out += block
                block.clear()
    out.append(len(block) + 1)
    out += block
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        block = data[i + 1:i + code]
        if code == 0 or len(block) != code - 1:
            raise ValueError("bad COBS block")
        out += block
        i += code
        if code != 255 and i < len(data):
            out.append(0)
    return bytes(out)


def encode_frame(packet: bytes, mtu: int = DEFAULT_MTU) -> bytes:
    if not 1 <= len(packet) <= mtu:
        raise ValueError(f"packet must be 1 to {mtu} bytes")
    body = len(packet).to_bytes(2, "big") + packet
    body += crc16(body).to_bytes(2, "big")
    return b"\x00" + cobs_encode(body) + b"\x00"


class FrameDecoder:
    """Feed bytes as they arrive; get complete packets. Bad frames are counted, never raised."""

    def __init__(self, mtu: int = DEFAULT_MTU):
        self.mtu = mtu
        self.buf = bytearray()
        self.drops = {"oversize": 0, "bad_cobs": 0, "bad_length": 0, "bad_crc": 0}

    @property
    def bad_frames(self) -> int:
        return sum(self.drops.values())

    def feed(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        longest = self.mtu + 4 + 1 + (self.mtu + 4) // 254 + 2  # longest legal frame, without delimiters
        for byte in data:
            if byte != 0:
                if len(self.buf) <= longest:
                    self.buf.append(byte)
                continue
            raw, self.buf = bytes(self.buf), bytearray()
            if not raw:
                continue
            if len(raw) > longest:
                self.drops["oversize"] += 1
                continue
            try:
                body = cobs_decode(raw)
            except ValueError:
                self.drops["bad_cobs"] += 1
                continue
            if len(body) < 5 or int.from_bytes(body[:2], "big") != len(body) - 4:
                self.drops["bad_length"] += 1
                continue
            if crc16(body[:-2]) != int.from_bytes(body[-2:], "big"):
                self.drops["bad_crc"] += 1
                continue
            if len(body) - 4 > self.mtu:
                self.drops["oversize"] += 1
                continue
            out.append(body[2:-2])
        return out
