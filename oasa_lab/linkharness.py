"""Run the oasa-sec encrypted link (oasa-pair, oasa-link) through the simulated channel.

Contract with oasa-sec (see suites/l1-oasa-link): binaries are found in $OASA_SEC_BIN_DIR.
Link logs go into the bundle (logs/); keys stay in a temp folder and never enter the bundle.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .testing import Endpoint, free_udp_port, wait_for

HOST = "127.0.0.1"
_SECRETISH = re.compile(r"secret|private|passphrase|password|seed", re.I)


class MissingBinaries(Exception):
    """Raised with the reason the link suite can't run here."""


@dataclass
class Bins:
    pair: Path
    link: Path


def find_bins(lab=None) -> Bins:
    d = os.environ.get("OASA_SEC_BIN_DIR")
    if not d:
        raise MissingBinaries("OASA_SEC_BIN_DIR is not set (folder with oasa-pair and oasa-link)")
    root = Path(d)
    pair, link = root / "oasa-pair", root / "oasa-link"
    missing = [p.name for p in (pair, link) if not (p.is_file() and os.access(p, os.X_OK))]
    if missing:
        raise MissingBinaries(f"{', '.join(missing)} not found or not executable in OASA_SEC_BIN_DIR={d}")
    if lab is not None:
        lab.binary(pair)
        lab.binary(link)
    return Bins(pair, link)


@dataclass
class Identity:
    name: str
    dir: Path
    public_key: str
    fingerprint: str


def _run(cmd: list[str], timeout: float = 30) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({r.returncode}): {r.stderr.strip()[:300]}")
    return r.stdout


def make_identity(bins: Bins, base: Path, name: str) -> Identity:
    d = base / name
    _run([str(bins.pair), "init", "--dir", str(d), "--name", name])
    info = json.loads(_run([str(bins.pair), "show", "--dir", str(d)]))
    if not re.fullmatch(r"[0-9a-f]{64}", info["public_key"]):
        raise RuntimeError("oasa-pair show: public_key is not 64 hex characters")
    return Identity(name, d, info["public_key"], info.get("fingerprint", ""))


def trust(bins: Bins, me: Identity, peer: Identity) -> None:
    _run([str(bins.pair), "trust", "--dir", str(me.dir), "--name", peer.name, "--key", peer.public_key])


def redact(rec: Any) -> Any:
    if isinstance(rec, dict):
        return {k: ("[redacted]" if _SECRETISH.search(k) else redact(v)) for k, v in rec.items()}
    if isinstance(rec, list):
        return [redact(v) for v in rec]
    return rec


class LinkProc:
    """One oasa-link process. Its JSON-lines output is kept in memory and saved to log_path."""

    def __init__(self, bins: Bins, me: Identity, peer: str, *, bind: tuple[str, int],
                 remote: tuple[str, int] | None, initiate: bool, plain_listen: tuple[str, int],
                 plain_send: tuple[str, int], log_path: Path, rekey_secs: int | None = None,
                 duration: float | None = None, counter_bytes: int | None = None, mtu: int | None = None):
        cmd = [str(bins.link), "--dir", str(me.dir), "--peer", peer, "--bind", f"{bind[0]}:{bind[1]}",
               "--plain-listen", f"{plain_listen[0]}:{plain_listen[1]}",
               "--plain-send", f"{plain_send[0]}:{plain_send[1]}"]
        if remote:
            cmd += ["--remote", f"{remote[0]}:{remote[1]}"]
        if initiate:
            cmd += ["--initiate"]
        if counter_bytes:
            cmd += ["--counter-bytes", str(counter_bytes)]
        if rekey_secs:
            cmd += ["--rekey-secs", str(rekey_secs)]
        if duration:
            cmd += ["--duration", str(duration)]
        if mtu:
            cmd += ["--mtu", str(mtu)]
        self.cmd, self.log_path = cmd, log_path
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     stdin=subprocess.DEVNULL, bufsize=1)
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self) -> None:
        assert self.proc.stdout
        with open(self.log_path, "w", encoding="utf-8") as out:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = redact(json.loads(line))
                    if not isinstance(rec, dict):
                        raise ValueError
                except ValueError:
                    rec = {"event": "raw_output", "line": line[:300]}
                out.write(json.dumps(rec, separators=(",", ":")) + "\n")
                out.flush()
                with self._lock:
                    self.events.append(rec)

    def seen(self, event: str) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e.get("event") == event]

    def wait_event(self, event: str, timeout: float = 15.0) -> bool:
        return wait_for(lambda: bool(self.seen(event)) or self.proc.poll() is not None and bool(self.seen(event)),
                        timeout)

    def stop(self) -> dict | None:
        """SIGTERM, wait for the final stats line, return it."""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(8)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)
        self._t.join(3)
        st = self.seen("stats")
        return st[-1] if st else None

    def drops(self) -> dict[str, int]:
        st = self.seen("stats")
        return dict(st[-1].get("drops", {})) if st else {}


class LinkPair:
    """Link A (initiator) and link B joined through a simulated channel, plus the two 'apps':
    app_a / app_b are plain UDP endpoints that feed and receive each link's plaintext side."""

    def __init__(self, lab, bins: Bins, base: Path, *, rekey_secs: int | None = None,
                 duration: float | None = None, counter_bytes: int | None = None, mtu: int | None = None,
                 **channel_settings: Any):
        self.lab, self.bins = lab, bins
        self.a_id = make_identity(bins, base, "alice")
        self.b_id = make_identity(bins, base, "bob")
        trust(bins, self.a_id, self.b_id)
        trust(bins, self.b_id, self.a_id)
        self.app_a, self.app_b = Endpoint(), Endpoint()
        self.a_bind, self.b_bind = (HOST, free_udp_port()), (HOST, free_udp_port())
        self.a_plain, self.b_plain = (HOST, free_udp_port()), (HOST, free_udp_port())
        self.channel = lab.channel(a=self.a_bind, b=self.b_bind, **channel_settings)
        kw = dict(rekey_secs=rekey_secs, duration=duration, counter_bytes=counter_bytes, mtu=mtu)
        self.link_b = LinkProc(bins, self.b_id, "alice", bind=self.b_bind, remote=self.channel.addr_b,
                               initiate=False, plain_listen=self.b_plain, plain_send=self.app_b.addr,
                               log_path=lab.file("link-bob.jsonl"), **kw)
        self.t_start = time.monotonic()  # the initiator starts here; handshake time is measured from it
        self.link_a = LinkProc(bins, self.a_id, "bob", bind=self.a_bind, remote=self.channel.addr_a,
                               initiate=True, plain_listen=self.a_plain, plain_send=self.app_a.addr,
                               log_path=lab.file("link-alice.jsonl"), **kw)
        self.stats: dict[str, dict | None] = {}

    def wait_established(self, timeout: float = 30.0) -> float:
        """Returns the time (time.monotonic) the initiator's session was seen up."""
        """Wait for the initiator only. Per the link spec the responder confirms a session when
        the first data packet arrives, so waiting for it here could deadlock if the initiator's
        keepalive is lost. Handshake retries back off (5 s, then longer), hence 30 s."""
        ok = self.link_a.wait_event("session_established", timeout)
        if not ok:
            raise AssertionError("the link did not establish a session in time; see logs/link-*.jsonl in the bundle")
        return time.monotonic()

    def send_a_to_b(self, packets, **kw) -> int:
        return self.app_a.send_paced(packets, self.a_plain, **kw)

    def finish(self) -> None:
        """Let late packets arrive, stop both links (collecting stats), stop the channel."""
        self.app_b.wait_idle(0.8, 20)
        self.stats["alice"] = self.link_a.stop()
        self.stats["bob"] = self.link_b.stop()
        self.channel.stop()

    def close(self) -> None:
        for lp in (self.link_a, self.link_b):
            lp.stop()
        self.app_a.close()
        self.app_b.close()

    def __enter__(self) -> "LinkPair":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
