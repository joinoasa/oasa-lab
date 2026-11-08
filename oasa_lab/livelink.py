"""The live link over the SDR bench: two `oasa-radio bridge --radio sdr` processes streaming all the time, each fed
by a radio helper (hardware) or joined by `oasa-radio waveform cable` (simulated bench). Design: docs/sdr-live-link.md.

Hardware, per radio: helper (radio <-> two FIFOs, timed TX) and bridge (FIFOs <-> UDP), both pinned to that radio's
CPUs; the bridge at real-time priority when the host allows it. The helper links UHD (GPL-3.0): it is never part of
this repo. The lab starts the binary the owner installed (`[sdr_live] helper` in lab.toml, checked against
`helper_sha256`) as a separate process, as it starts UHD's example tools. Its TX gain is capped by the radio's
`max_tx_gain` and its run time by `max_tx_seconds`.

Traffic: `ping` sends numbered 32-byte datagrams at a fixed rate into Alice's link (B200mini side); `echo` on Bob's
side (E310 side) stamps the time it received each one into the datagram and sends it straight back. Both run on one
host, so each datagram gives the delay of both directions on one clock, paired by sequence number (not estimated
as half the round trip, and late frames are not lost from the pairing). Tables are fixed size: a growing dict
rehashes at 2/3 of a power of two, a 7-40 ms stall that once looked like the link's worst case (bench, 2026-11-07).

    python -m oasa_lab.livelink echo LISTEN SEND N OUT.json
    python -m oasa_lab.livelink ping LISTEN SEND N GAP_S OUT.json
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PKT = struct.Struct(">Idd")          # seq, t_sent, t_echo (seconds since the epoch, one host clock)
PKT_LEN = 32
WINDOW_S = 600.0                     # the worst-case target is per 10 minutes
HOUR_S = 3600.0                      # the soak reports each hour, so a drift over the day shows
HIST_STEP_MS = 0.05                  # histogram bins: 0.05 ms up to HIST_TOP_MS, then one overflow bin
HIST_TOP_MS = 200.0
MAX_OUTLIERS = 500

# The settings that met every bench target in one hour (2026-11-07, run CLB25FP2; PLAN in the lab's private notes):
# 250 kbaud QPSK at 1 MS/s, IF 175 kHz, detector step 4, timed bursts; E310 lead 2.5 ms with the fast restart after
# a late packet, B200mini lead 2 ms with 250-sample receive packets (1016 bytes), periodic sensor reads off;
# each radio's helper and bridge on their own CPU, bridges at SCHED_FIFO 30.
HW_BRIDGE = ["--tx-mode", "burst", "--symbol-rate", "250000", "--if-hz", "175000", "--decimate", "1",
             "--coarse-step", "4"]
SIM_BRIDGE = ["--tx-mode", "stream", "--sample-rate", "200000", "--symbol-rate", "25000", "--if-hz", "40000",
              "--block", "2048"]      # a fifth of the bench rates, so a debug build keeps up (oasa-radio tests/sdr_link.rs)
SIM_RATE = 200e3
DEFAULTS: dict[str, Any] = {
    "duration_s": 3000.0,             # a counted job runs at most an hour (lab.job_timeout_seconds); 50 min + setup
    "soak_duration_s": 86400.0,       # l2-sdr-live-soak: 24 h (its job time comes from the policy's long_jobs)
    "rate_hz": 100.0,                 # datagrams per second each way
    "b200_tx_freq": 915e6, "e310_tx_freq": 920e6,
    "b200_tx_gain": 40.0, "e310_tx_gain": 28.0,
    "rx_gain": 60.0,
    "b200_cfo_hz": 2250.0, "e310_cfo_hz": -2250.0,
    "b200_helper_args": ["--rt", "--lead-ms", "2", "--sensor-s", "0", "--dev-extra", "recv_frame_size=1016"],
    "e310_helper_args": ["--rt", "--lead-ms", "2.5", "--late-restart", "fast", "--sensor-s", "0"],
    "b200_cpus": "", "e310_cpus": "",  # taskset lists, e.g. "3" and "2"; empty = not pinned
    "bridge_rt_prio": 30,             # 0 = no real-time priority
    "sdr_log": False,                 # the bridges' per-frame logs (about 80 MB per hour each)
}
TX_MARGIN_S = 120.0                   # the helpers' --max-seconds = duration + this


class LiveConfigError(Exception):
    pass


@dataclass
class LiveConfig:
    helper: Path | None = None
    helper_sha256: str = ""
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))

    def __getitem__(self, k: str) -> Any:
        return self.settings[k]

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "LiveConfig":
        """`[sdr_live]` from lab.toml. Unknown keys are refused, so a typo cannot silently fall back to a default."""
        d = dict(d or {})
        helper = d.pop("helper", None)
        sha = str(d.pop("helper_sha256", "")).lower()
        unknown = sorted(set(d) - set(DEFAULTS))
        if unknown:
            raise LiveConfigError(f"[sdr_live]: unknown key(s) {', '.join(unknown)}")
        s = dict(DEFAULTS)
        s.update(d)
        for k in ("duration_s", "soak_duration_s", "rate_hz", "b200_tx_freq", "e310_tx_freq", "b200_tx_gain", "e310_tx_gain",
                  "rx_gain", "b200_cfo_hz", "e310_cfo_hz"):
            if isinstance(s[k], bool) or not isinstance(s[k], (int, float)) or not math.isfinite(s[k]):
                raise LiveConfigError(f"[sdr_live] {k} must be a number")
            s[k] = float(s[k])
        for k in ("duration_s", "soak_duration_s"):
            if not 10 <= s[k] <= 90000:
                raise LiveConfigError(f"[sdr_live] {k} must be 10 to 90000")
        if not 1 <= s["rate_hz"] <= 500:
            raise LiveConfigError("[sdr_live] rate_hz must be 1 to 500")
        for k in ("b200_helper_args", "e310_helper_args"):
            if not (isinstance(s[k], list) and all(isinstance(a, str) for a in s[k])):
                raise LiveConfigError(f"[sdr_live] {k} must be a list of strings")
            bad = [a for a in s[k] if a.lstrip("-").split("=")[0] in ("tx-gain", "max-seconds", "radio", "device-args", "tx", "rx",
                                                         "tx-freq", "rx-freq")]
            if bad:
                raise LiveConfigError(f"[sdr_live] {k} may not set {', '.join(bad)}: the lab sets and caps those")
        for k in ("b200_cpus", "e310_cpus"):
            if not isinstance(s[k], str) or any(c not in "0123456789,-" for c in s[k]):
                raise LiveConfigError(f"[sdr_live] {k} must be a CPU list such as \"2\" or \"2-3\"")
        if not isinstance(s["bridge_rt_prio"], int) or not 0 <= s["bridge_rt_prio"] <= 49:
            raise LiveConfigError("[sdr_live] bridge_rt_prio must be 0 to 49")
        if not isinstance(s["sdr_log"], bool):
            raise LiveConfigError("[sdr_live] sdr_log must be true or false")
        if sha and (len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
            raise LiveConfigError("[sdr_live] helper_sha256 must be 64 hex digits")
        return cls(Path(helper) if helper else None, sha, s)

    def check_helper(self) -> Path:
        """The installed helper, after checking it is the pinned build. Raises LiveConfigError."""
        if self.helper is None:
            raise LiveConfigError("lab.toml has no [sdr_live] helper (the installed radio helper's path)")
        if not (self.helper.is_file() and os.access(self.helper, os.X_OK)):
            raise LiveConfigError(f"[sdr_live] helper {self.helper} is not an executable file")
        if not self.helper_sha256:
            raise LiveConfigError("[sdr_live] helper_sha256 is not set: the lab runs only the pinned helper build")
        got = sha256_file(self.helper)
        if got != self.helper_sha256:
            raise LiveConfigError(f"[sdr_live] helper {self.helper} has sha256 {got}, not the pinned {self.helper_sha256}")
        return self.helper


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fifo_dir() -> Path:
    """FIFOs on a RAM filesystem: on ext4 each FIFO write can wait for the 5 s journal commit (10-70 ms stalls)."""
    for base in (Path("/dev/shm"), Path(os.environ.get("XDG_RUNTIME_DIR", "/nonexistent"))):
        if base.is_dir() and os.access(base, os.W_OK):
            return Path(tempfile.mkdtemp(prefix="oasa-live-", dir=base))
    return Path(tempfile.mkdtemp(prefix="oasa-live-"))


def wrap(cmd: list[str], cpus: str = "", rt_prio: int = 0) -> list[str]:
    """Pin to CPUs (taskset) and/or run at SCHED_FIFO (chrt). Never through a shell."""
    out = list(cmd)
    if rt_prio:
        out = ["chrt", "-f", str(rt_prio)] + out
    if cpus:
        out = ["taskset", "-c", cpus] + out
    return out


def rt_allowed(prio: int) -> bool:
    """Whether this process may use SCHED_FIFO at `prio` (RLIMIT_RTPRIO, RestrictRealtime in the job unit)."""
    if prio <= 0 or shutil.which("chrt") is None:
        return False
    try:
        return subprocess.run(["chrt", "-f", str(prio), "true"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pin_allowed(cpus: str) -> bool:
    """Whether this process may pin to `cpus` (the job unit's sandbox could refuse sched_setaffinity)."""
    if not cpus or shutil.which("taskset") is None:
        return False
    try:
        return subprocess.run(["taskset", "-c", cpus, "true"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def helper_command(helper: Path, dev: Any, *, tx: Path, rx: Path, tx_freq: float, rx_freq: float, tx_gain: float,
                   rx_gain: float, max_seconds: float, extra: list[str]) -> list[str]:
    """The helper's command line for one radio. Gain and time caps come from the device (lab.toml)."""
    if not 0 <= tx_gain <= dev.max_tx_gain:
        raise LiveConfigError(f"{dev.id}: TX gain {tx_gain:g} dB is above its max_tx_gain {dev.max_tx_gain:g}")
    if max_seconds > dev.max_tx_seconds:
        raise LiveConfigError(f"{dev.id}: this run needs {max_seconds:.0f} s of TX and max_tx_seconds is "
                              f"{dev.max_tx_seconds:.0f}; the owner raises it in lab.toml for a longer run")
    cmd = [str(helper), "--radio", dev.kind, "--device-args", dev.device_args(), "--tx", str(tx), "--rx", str(rx),
           "--tx-freq", f"{tx_freq:.0f}", "--rx-freq", f"{rx_freq:.0f}", "--tx-gain", f"{tx_gain:g}",
           "--rx-gain", f"{rx_gain:g}", "--max-seconds", f"{max_seconds:.0f}"]
    if dev.kind == "e310":
        cmd += ["--rx-subdev", str(dev.cfg.get("subdev", "A:0 A:1")),
                "--tx-subdev", str(dev.cfg.get("tx_subdev", dev.default_tx_subdev or "A:0")),
                "--rx-channel", str(int(dev.cfg.get("channel", 1)))]
    return cmd + list(extra)


class Proc:
    """A child process whose output goes to a file in the bundle."""

    def __init__(self, name: str, cmd: list[str], out: Path, cwd: Path | None = None):
        self.name, self.cmd, self.out = name, cmd, out
        self._f = open(out, "wb")
        self.p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self._f, stderr=subprocess.STDOUT, cwd=cwd)

    def alive(self) -> bool:
        return self.p.poll() is None

    def stop(self, wait: float = 8.0) -> int | None:
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
            try:
                self.p.wait(wait)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(5)
        self._f.close()
        return self.p.returncode

    def events(self) -> list[dict[str, Any]]:
        out = []
        try:
            text = self.out.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return out
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out


def mkfifos(d: Path, names: list[str]) -> dict[str, Path]:
    out = {}
    for n in names:
        p = d / n
        os.mkfifo(p, 0o600)
        out[n] = p
    return out


# --- traffic and statistics ----------------------------------------------------------------------------------


def _hist_index(ms: float) -> int:
    nbins = int(HIST_TOP_MS / HIST_STEP_MS)
    return min(nbins, max(0, int(ms / HIST_STEP_MS)))


def summarize(values_ms: Any, times: Any = None, t0: float | None = None) -> dict[str, Any]:
    """Percentiles, max, per-10-minute maxima and a histogram of one direction's delays (ms)."""
    v = sorted(values_ms)
    n = len(v)
    q = (lambda p: round(v[min(n - 1, int(p * n))], 3)) if n else (lambda p: None)
    hist: dict[str, int] = {}
    for x in values_ms:
        k = str(_hist_index(x))
        hist[k] = hist.get(k, 0) + 1
    win: dict[int, float] = {}
    if times is not None and t0 is not None:
        for x, t in zip(values_ms, times):
            w = int((t - t0) // WINDOW_S)
            win[w] = max(win.get(w, 0.0), x)
    return {"n": n, "p50": q(.5), "p90": q(.9), "p99": q(.99), "p999": q(.999), "max": round(v[-1], 3) if n else None,
            "window_max": [round(win[k], 3) for k in sorted(win)], "hist_step_ms": HIST_STEP_MS, "hist": hist}


def echo_main(listen: str, send: str, n: int, out: str, idle_s: float = 10.0) -> None:
    """Bob's app: stamp the receive time into each datagram and send it back. Ends after `idle_s` without traffic
    once something arrived, or on SIGTERM; writes which sequence numbers arrived."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(_addr(listen))
    s.settimeout(0.2)
    dst = _addr(send)
    got = bytearray(n)
    dup = bad = 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    last = None
    while not stop.is_set():
        try:
            d, _ = s.recvfrom(4096)
        except socket.timeout:
            if last is not None and time.monotonic() - last > idle_s:
                break
            continue
        t1 = time.time()
        last = time.monotonic()
        if len(d) != PKT_LEN:
            bad += 1
            continue
        k, t0, _ = PKT.unpack_from(d)
        if k >= n:
            bad += 1
            continue
        if got[k]:
            dup += 1
        got[k] = 1
        s.sendto(PKT.pack(k, t0, t1) + d[PKT.size:], dst)
    Path(out).write_text(json.dumps({"n": n, "received": sum(got), "duplicates": dup, "bad": bad,
                                     "missing_first": [i for i in range(n) if not got[i]][:MAX_OUTLIERS]}))


def ping_main(listen: str, send: str, n: int, gap: float, out: str, drain_s: float = 3.0) -> None:
    """Alice's app: N datagrams GAP seconds apart; for each echo, the delay of both directions (fixed tables)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(_addr(listen))
    dst = _addr(send)
    pad = os.urandom(PKT_LEN - PKT.size)
    a2b, b2a, rtt = array("d", bytes(8 * n)), array("d", bytes(8 * n)), array("d", bytes(8 * n))
    t_rx = array("d", bytes(8 * n))
    back = bytearray(n)
    dup = bad = 0
    i, start = 0, time.time()
    nxt = start
    t_end = None
    while True:
        now = time.time()
        if i < n and now >= nxt:
            s.sendto(PKT.pack(i, now, 0.0) + pad, dst)
            i += 1
            nxt += gap
            if i == n:
                t_end = now + drain_s
        s.settimeout(max(0.0002, min(0.05, nxt - time.time())) if i < n else 0.05)
        try:
            d, _ = s.recvfrom(4096)
            t2 = time.time()
            if len(d) != PKT_LEN or d[PKT.size:] != pad:
                bad += 1
            else:
                k, t0, t1 = PKT.unpack_from(d)
                if k >= i:
                    bad += 1                       # an answer to something never sent: invented
                elif back[k]:
                    dup += 1
                else:
                    back[k] = 1
                    a2b[k], b2a[k], rtt[k], t_rx[k] = (t1 - t0) * 1e3, (t2 - t1) * 1e3, (t2 - t0) * 1e3, t2
        except socket.timeout:
            pass
        if t_end is not None and time.time() > t_end:
            break
    Path(out).write_text(json.dumps(result(n, gap, start, back, a2b, b2a, rtt, t_rx, dup, bad)))


def _pick(values: array, back: bytearray, lo: int = 0, hi: int | None = None) -> array:
    hi = len(back) if hi is None else hi
    return array("d", (values[k] for k in range(lo, hi) if back[k]))


def hours(n: int, gap: float, back: bytearray, a2b: array, b2a: array) -> list[dict[str, Any]]:
    """Per hour of sending (by sequence number: message k is sent at k * gap): messages sent, round-trip loss, and
    each direction's median, p99.9 and worst. A 24-hour soak's drift over the day shows here."""
    per = max(1, round(HOUR_S / gap))
    out = []
    for lo in range(0, n, per):
        hi = min(n, lo + per)
        got = sum(back[lo:hi])
        row: dict[str, Any] = {"hour": lo // per, "sent": hi - lo, "back": got,
                               "loss_pct": round(100.0 * (hi - lo - got) / (hi - lo), 4)}
        for d, arr in (("a2b", a2b), ("b2a", b2a)):
            v = sorted(_pick(arr, back, lo, hi))
            m = len(v)
            row[d] = ({"p50": round(v[m // 2], 3), "p999": round(v[min(m - 1, int(.999 * m))], 3), "max": round(v[-1], 3)}
                      if m else None)
        out.append(row)
    return out


def result(n: int, gap: float, start: float, back: bytearray, a2b: array, b2a: array, rtt: array, t_rx: array,
           dup: int = 0, bad: int = 0) -> dict[str, Any]:
    """What `ping` writes. Built from the fixed tables without per-message Python lists, so a 24-hour run at
    100/s (8.6 million messages) stays well inside the job's memory limit."""
    times = _pick(t_rx, back)
    res: dict[str, Any] = {"n": n, "gap_s": gap, "start": start, "back": len(times), "duplicates": dup, "bad": bad}
    for d, arr in (("a2b", a2b), ("b2a", b2a), ("rtt", rtt)):
        res[d] = summarize(_pick(arr, back), times, start)
    res["hours"] = hours(n, gap, back, a2b, b2a)
    res["outliers"] = []
    for k in range(n):
        if back[k] and (a2b[k] > 20 or b2a[k] > 20):
            res["outliers"].append({"seq": k, "a2b_ms": round(a2b[k], 3), "b2a_ms": round(b2a[k], 3),
                                    "t": round(t_rx[k], 4)})
            if len(res["outliers"]) >= MAX_OUTLIERS:
                break
    res["lost_first"] = []
    for k in range(n):
        if not back[k]:
            res["lost_first"].append(k)
            if len(res["lost_first"]) >= MAX_OUTLIERS:
                break
    return res


def _addr(s: str) -> tuple[str, int]:
    h, p = s.rsplit(":", 1)
    return h, int(p)


def traffic_cmd(*args: str) -> list[str]:
    return [sys.executable, "-I", "-m", "oasa_lab.livelink", *args]


def main(argv: list[str]) -> int:
    if argv[:1] == ["echo"] and len(argv) == 5:
        echo_main(argv[1], argv[2], int(argv[3]), argv[4])
        return 0
    if argv[:1] == ["ping"] and len(argv) == 6:
        ping_main(argv[1], argv[2], int(argv[3]), float(argv[4]), argv[5])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
