"""SiK telemetry radios: their AT command protocol, from the lab's side.

The lab's own small copy of what `oasa-radio sik show` does (oasa-radio src/sik.rs, docs/sik.md), so a
SiK driver can read a radio's settings without the Rust tool. SiK behaviour (ArduPilot SiK firmware,
`Firmware/radio/at.c`, `serial.c`, `parameters.c`, `tdm.c`):

- `+++` with 1 s of silence before and after enters command mode; the radio prints `OK`.
- Command mode echoes what it gets, runs a line on CR and ends lines with CR LF.
- `ATI` version, `ATI5` one `Sn:NAME=value` line per setting, `ATSn=v`, `AT&W`, `ATZ`, `ATO`.
- `RT` + a command runs it on the linked radio (no authentication; oasa-radio's review, H1).

Only before or after the radio carries packets, never while: bytes that arrive meanwhile may be packets
from the air. They are thrown away and never shown; only lines that look exactly like a setting count.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Protocol

GUARD_S = 1.2           # SiK wants 1 s (ATP_COUNT_1S); 0.2 s for USB latency
ENTER_WAIT_S = 1.5
REPLY_IDLE_S = 0.3
REPLY_MAX_S = 3.0
REMOTE_MAX_S = 5.0      # an RT command crosses the air twice, in the radios' TDM windows (est.)
REPLY_CAP = 4096
AFTER_ATO_S = 0.1

NAMES = ("FORMAT", "SERIAL_SPEED", "AIR_SPEED", "NETID", "TXPOWER", "ECC", "MAVLINK", "OPPRESEND", "MIN_FREQ",
         "MAX_FREQ", "NUM_CHANNELS", "DUTY_CYCLE", "LBT_RSSI", "MANCHESTER", "RTSCTS", "MAX_WINDOW")
# SiK defaults (parameters.c), with the 915 MHz band the Holybro V3 ships with
DEFAULTS = dict(zip(NAMES, (25, 57, 64, 25, 20, 0, 1, 0, 915_000, 928_000, 50, 100, 0, 0, 0, 131)))
# These must match on both radios of a link (ArduPilot SiK configuration page)
LINK_KEYS = ("AIR_SPEED", "NETID", "ECC", "MIN_FREQ", "MAX_FREQ", "NUM_CHANNELS", "LBT_RSSI", "MAX_WINDOW")
SERIAL_BAUD = {1: 1200, 2: 2400, 4: 4800, 9: 9600, 19: 19_200, 38: 38_400, 57: 57_600, 115: 115_200, 230: 230_400}

_PARAM = re.compile(r"S(\d{1,3}):([A-Z0-9_]{1,32})=(\d{1,10})")


class AtError(Exception):
    """The radio did not answer as a SiK radio does. Messages never hold bytes from the radio."""


class Port(Protocol):
    def read(self, timeout: float = 0.05) -> bytes: ...
    def write(self, data: bytes) -> None: ...


@dataclass
class Report:
    version: str
    settings: dict[str, int] = field(default_factory=dict)   # name -> value, in register order
    registers: dict[str, int] = field(default_factory=dict)  # name -> register number


def _lines(reply: bytes) -> list[str | None]:
    """Only lines ended by LF: a fragment still arriving (an echo before its CR) is not a line yet."""
    out: list[str | None] = []
    end = reply.rfind(b"\n") + 1
    for raw in reply[:end].split(b"\n")[:-1]:
        raw = raw.replace(b"\r", b"")
        out.append(raw.decode("ascii").strip() if all(0x20 <= c < 0x7F for c in raw) else None)
    return out


def _after_echo(reply: bytes, cmd: str) -> list[str | None]:
    ls = _lines(reply)
    idx = [i for i, l in enumerate(ls) if l == cmd]
    return ls[idx[-1] + 1:] if idx else ls


def has_line(reply: bytes, word: str) -> bool:
    return word in _lines(reply)


def confirmed_at(reply: bytes) -> bool:
    """`AT` CR answered with its own echo, then `OK`."""
    ls = _lines(reply)
    return "AT" in ls and "OK" in ls[ls.index("AT") + 1:]


def parse_ati5(reply: bytes) -> tuple[dict[str, int], dict[str, int]]:
    """(settings, registers). Anything after the echo but settings, OK and empty lines is an error."""
    settings: dict[str, int] = {}
    regs: dict[str, int] = {}
    other = 0
    for l in _after_echo(reply, "ATI5"):
        if l is None:
            other += 1
            continue
        if l in ("", "OK"):
            continue
        m = _PARAM.fullmatch(l)
        if not m:
            other += 1
            continue
        reg, name, value = int(m.group(1)), m.group(2), int(m.group(3))
        if name in settings or reg in regs.values() or value > 0xFFFFFFFF:
            raise AtError(f"ATI5 lists S{reg} or {name} twice, or a value out of range")
        settings[name], regs[name] = value, reg
    if other:
        raise AtError(f"ATI5 reply has {other} line(s) that are not settings (wrong baud, or not a SiK radio?)")
    if not settings:
        raise AtError("ATI5 gave no settings (wrong baud, or not a SiK radio?)")
    return settings, regs


def parse_ati(reply: bytes) -> str:
    """The banner, `SiK <version> on <board>` (main.c g_banner_string; RFDesign builds put `RFD ` in front)."""
    for l in _after_echo(reply, "ATI"):
        if l and len(l) <= 64 and (l.startswith("SiK ") or l.startswith("RFD SiK ")):
            return l
    raise AtError("ATI gave no SiK version line (not a SiK radio?)")


class AtSession:
    def __init__(self, port: Port):
        self.port = port

    def _read(self, max_s: float, idle_s: float | None = None, done=None) -> bytes:
        end = time.monotonic() + max_s
        got = bytearray()
        last = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= end or (done and done(bytes(got))):
                return bytes(got)
            if idle_s is not None and got and now - last >= idle_s:
                return bytes(got)
            data = self.port.read(min(0.02, end - now))
            if data:
                got += data
                del got[:max(0, len(got) - REPLY_CAP)]
                last = time.monotonic()

    def enter(self, recover: bool = True) -> None:
        """`recover`: a radio left in command mode is recovered with CR and `AT` CR (in data mode those bytes go
        on the air, where the other side drops them as noise)."""
        self._read(GUARD_S)                   # silence from us; whatever arrives is thrown away
        self.port.write(b"+++")
        # Our +++ also goes on the air, so for the second guard a linked radio could send anything, a fake OK too:
        # throw it all away (the radio's own OK as well). After it, a SiK radio that got +++ is in command mode and
        # passes nothing from the air, so AT must come back as its own echo and OK.
        self._read(GUARD_S)
        if self._confirm_at():
            return
        if recover:
            self.port.write(b"\r")            # it was in command mode already: "+++AT" was one ignored line
            self._read(0.3)
            if self._confirm_at():
                return
        raise AtError("not in command mode after +++ (is it a SiK radio at this baud, and is the port free?)")

    def _confirm_at(self) -> bool:
        self.port.write(b"AT\r")
        return confirmed_at(self._read(ENTER_WAIT_S, done=confirmed_at))

    def command(self, cmd: str) -> bytes:
        self.port.write(cmd.encode("ascii") + b"\r")
        return self._read(REPLY_MAX_S, REPLY_IDLE_S, lambda b: has_line(b, "OK") or has_line(b, "ERROR"))

    def command_ok(self, cmd: str, max_s: float = REPLY_MAX_S) -> None:
        self.port.write(cmd.encode("ascii") + b"\r")
        r = self._read(max_s, done=lambda b: has_line(b, "OK") or has_line(b, "ERROR"))
        if has_line(r, "OK"):
            return
        raise AtError(f"the radio refused {cmd}" if has_line(r, "ERROR") else f"no answer to {cmd}")

    def report(self) -> Report:
        version = parse_ati(self.command("ATI"))
        settings, regs = parse_ati5(self.command("ATI5"))
        return Report(version, settings, regs)

    def leave(self) -> None:
        """ATO prints only its echo; the echo proves the radio got it. Twice, then give up loudly."""
        for _ in range(2):
            self.port.write(b"ATO\r")
            if has_line(self._read(0.5, done=lambda b: has_line(b, "ATO")), "ATO"):
                time.sleep(AFTER_ATO_S)   # SiK echoes before it runs a line and drops serial input meanwhile
                return
        raise AtError("the radio did not confirm ATO; it may still be in command mode")

    def reboot(self) -> None:
        """ATZ: reboot without saving, so unsaved changes are dropped. The radio prints nothing."""
        self.port.write(b"ATZ\r")


def read_report(port: Port) -> Report:
    """Enter command mode, read ATI and ATI5, leave (always)."""
    s = AtSession(port)
    s.enter()
    try:
        r = s.report()
    except AtError:
        try:
            s.leave()
        except AtError:
            pass
        raise
    s.leave()
    return r


def reboot_unsaved(port: Port) -> None:
    """Enter command mode and reboot (ATZ): settings changed but not saved (AT&W) go back to the saved ones."""
    s = AtSession(port)
    s.enter()
    s.reboot()


def remote(port: Port, commands: list[str]) -> None:
    """Run commands on the linked radio (`RT...`), each must answer OK. Never saves (no RT&W) unless asked."""
    s = AtSession(port)
    s.enter()
    try:
        for c in commands:
            if not re.fullmatch(r"RT[A-Z0-9&=?]{0,14}", c):
                raise AtError(f"{c!r} is not a remote AT command")
            s.command_ok(c, REMOTE_MAX_S)
    finally:
        s.leave()


def mismatches(settings: dict[str, int], expect: dict[str, int]) -> list[dict]:
    return [{"name": k, "want": v, "got": settings.get(k)} for k, v in sorted(expect.items()) if settings.get(k) != v]


def expect_arg(expect: dict[str, int]) -> str:
    """The `--sik-expect` value for oasa-radio bridge."""
    return ",".join(f"{k.lower()}={v}" for k, v in sorted(expect.items()))


def normalize(table: dict) -> dict[str, int]:
    """A lab.toml table (`netid = 4242`, `air_speed = 64`) as SiK names."""
    return {str(k).upper().replace("-", "_"): int(v) for k, v in (table or {}).items()}
