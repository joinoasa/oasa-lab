"""Real autopilot simulators (PX4 and ArduPilot SITL) behind the oasa-sec link.

Used by the `l1-mavlink-sitl` suite. Everything here is test tooling: it starts a simulator in a
temporary folder, pairs the MAVLink signing key with `oasa-pair` (the way oasa-sec's
scripts/sitl do), and gives the test a pymavlink "ground station" that talks to the autopilot only
through two `oasa-link` processes and a simulated channel.

Needs (otherwise the suite skips and the checker fails the run on purpose):
  OASA_SEC_BIN_DIR   oasa-pair and oasa-link (a build of oasa-sec with the MAVLink guard)
  PX4_DIR            a PX4-Autopilot checkout built with `make px4_sitl_sih`
  ARDUPILOT_DIR      an ardupilot checkout built with `./waf configure --board sitl && ./waf copter`
  pymavlink          `pip install pymavlink`

Keys never enter the evidence bundle: pairing output and key files stay in a temporary folder.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .linkharness import Bins, Identity, make_identity
from .testing import Addr, free_udp_port, wait_for

HOST = "127.0.0.1"
SYSID_GCS, COMPID_GCS = 255, 190


class SitlMissing(Exception):
    """The reason this suite can't run here."""


def need_pymavlink():
    try:
        from pymavlink.dialects.v20 import common  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on the machine
        raise SitlMissing(f"pymavlink is not importable ({exc}); pip install pymavlink") from exc


def find_px4() -> Path:
    d = os.environ.get("PX4_DIR")
    if not d:
        raise SitlMissing("PX4_DIR is not set (a PX4-Autopilot checkout built with make px4_sitl_sih)")
    exe = Path(d) / "build" / "px4_sitl_sih" / "bin" / "px4"
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        raise SitlMissing(f"{exe} not found; build it with `make px4_sitl_sih`")
    return exe


def find_ardupilot() -> Path:
    d = os.environ.get("ARDUPILOT_DIR")
    if not d:
        raise SitlMissing("ARDUPILOT_DIR is not set (an ardupilot checkout built with ./waf copter)")
    exe = Path(d) / "build" / "sitl" / "bin" / "arducopter"
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        raise SitlMissing(f"{exe} not found; build it with `./waf configure --board sitl && ./waf copter`")
    return exe


# --- processes -----------------------------------------------------------------------------
class Proc:
    """A child process in its own process group, killed as a group."""

    def __init__(self, cmd: list[str], cwd: Path, log: Path, env: dict | None = None):
        self.cmd, self.log = cmd, log
        self._fh = open(log, "wb")
        self.p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=self._fh, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, start_new_session=True)

    def alive(self) -> bool:
        return self.p.poll() is None

    def stop(self) -> None:
        if self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGTERM)
                self.p.wait(8)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.p.wait(5)
        self._fh.close()


def _port_open(port: int) -> bool:
    try:
        socket.create_connection((HOST, port), 0.5).close()
        return True
    except OSError:
        return False


# --- the autopilots --------------------------------------------------------------------------
class Autopilot:
    """Base: a simulator in a temporary folder, with a companion port for the drone-side link."""

    kind = "?"
    pair_url = ""  # --autopilot for oasa-pair

    def __init__(self, exe: Path, tmp: Path):
        self.exe, self.tmp = exe, tmp
        self.proc: Proc | None = None
        self.log = tmp / f"{self.kind}-autopilot.log"

    def plain_send(self) -> Addr:
        """Where the drone-side oasa-link sends decrypted packets."""
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def after_pairing(self) -> None:
        """Hook once the key is loaded (ArduPilot starts its TCP bridge here)."""

    def stop(self) -> None:
        if self.proc:
            self.proc.stop()


class Px4(Autopilot):
    """PX4 SITL (SIH, headless). The companion (onboard) port is UDP 14580; GCS 18570."""

    kind = "px4"
    pair_url = f"udpout:{HOST}:14580"
    PORT = 14580

    def plain_send(self) -> Addr:
        return HOST, self.PORT

    def start(self) -> None:
        run = self.tmp / "px4-run"
        run.mkdir()
        build = self.exe.parent.parent  # build/px4_sitl_sih
        src = build.parent.parent  # the PX4-Autopilot checkout
        env = dict(os.environ, PX4_SYS_AUTOSTART="10040", PX4_SIMULATOR="sihsim", PX4_SIM_MODEL="sihsim_quadx")
        self.proc = Proc([str(self.exe), "-d", str(build / "etc"), "-s", "etc/init.d-posix/rcS",
                          "-t", str(src / "test_data"), "-w", "."], run, self.log, env)
        # PX4 streams telemetry to its onboard remote port (14540) as soon as it is up.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((HOST, 14540))
            probe.settimeout(1.0)
            end = time.monotonic() + 90
            while time.monotonic() < end:
                if not self.proc.alive():
                    raise RuntimeError(f"PX4 exited; see {self.log}")
                try:
                    probe.recvfrom(2048)
                    break
                except socket.timeout:
                    pass
            else:
                raise RuntimeError("PX4 did not start streaming within 90 s")
        finally:
            probe.close()
        time.sleep(4)  # let the estimator settle


class ArduPilot(Autopilot):
    """ArduCopter SITL, instance 2: SERIAL0 5780 (never used), SERIAL1 5782, SERIAL2 5783.

    The companion computer is on SERIAL1 as a TCP server; SERIAL0 never authenticates. A small
    bridge turns that TCP stream into the UDP datagrams oasa-link carries."""

    kind = "ardupilot"
    INST = 2
    P0, P1 = 5760 + 10 * INST, 5760 + 10 * INST + 2
    pair_url = f"tcpout:{HOST}:{P1}"

    def __init__(self, exe: Path, tmp: Path):
        super().__init__(exe, tmp)
        self.bridge: TcpUdpBridge | None = None
        self.bridge_in = (HOST, free_udp_port())  # the drone-side link's --plain-send

    def plain_send(self) -> Addr:
        return self.bridge_in

    def start(self) -> None:
        run = self.tmp / "ap-run"
        run.mkdir()
        src = self.exe.parent.parent.parent.parent  # the ardupilot checkout
        self.proc = Proc([str(self.exe), "--model", "+", "--speedup", "1", "--serial0", f"tcp:{self.P0}",
                          "--defaults", str(src / "Tools/autotest/default_params/copter.parm"),
                          f"-I{self.INST}", "--sysid", "1"], run, self.log)
        if not wait_for(lambda: _port_open(self.P1) or not self.proc.alive(), 90, 0.3) or not self.proc.alive():
            raise RuntimeError(f"ArduPilot did not open SERIAL1 (tcp {self.P1}); see {self.log}")
        time.sleep(4)

    def attach_bridge(self, to: Addr) -> None:
        self.bridge = TcpUdpBridge(HOST, self.P1, self.bridge_in, to)

    def stop(self) -> None:
        if self.bridge:
            self.bridge.close()
        super().stop()


# --- TCP <-> UDP bridge (what a companion computer's router does) ----------------------------
def split_frames(buf: bytearray) -> list[bytes]:
    """Cut whole MAVLink 1/2 frames off the front of `buf` (resyncing past junk)."""
    out: list[bytes] = []
    while buf:
        if buf[0] == 0xFE:
            need = 8 + buf[1] if len(buf) > 1 else None
        elif buf[0] == 0xFD:
            need = 12 + buf[1] + (13 if buf[2] & 1 else 0) if len(buf) > 2 else None
        else:
            del buf[0]
            continue
        if need is None or len(buf) < need:
            break
        out.append(bytes(buf[:need]))
        del buf[:need]
    return out


class TcpUdpBridge:
    """Autopilot TCP port <-> UDP. Frames from the autopilot go to `to` as one datagram each;
    datagrams arriving at `listen` are written to the TCP stream. `to` can change (the negative
    control points it at its own channel)."""

    def __init__(self, host: str, tcp_port: int, listen: Addr, to: Addr):
        self.to = to
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.udp.bind(listen)
        self.udp.settimeout(0.2)
        self.tcp = socket.create_connection((host, tcp_port), 10)
        self.tcp.settimeout(0.2)
        self._stop = False
        self._lock = threading.Lock()
        self.threads = [threading.Thread(target=self._tcp_to_udp, daemon=True),
                        threading.Thread(target=self._udp_to_tcp, daemon=True)]
        for t in self.threads:
            t.start()

    def _tcp_to_udp(self) -> None:
        buf = bytearray()
        while not self._stop:
            try:
                data = self.tcp.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            buf += data
            for frame in split_frames(buf):
                try:
                    self.udp.sendto(frame, self.to)
                except OSError:
                    pass

    def _udp_to_tcp(self) -> None:
        while not self._stop:
            try:
                data, _ = self.udp.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                with self._lock:
                    self.tcp.sendall(data)
            except OSError:
                return

    def close(self) -> None:
        self._stop = True
        for s in (self.tcp, self.udp):
            try:
                s.close()
            except OSError:
                pass


# --- pairing ----------------------------------------------------------------------------------
@dataclass
class Paired:
    drone: Identity
    gcs: Identity
    key: bytes  # the ground station's MAVLink signing key (32 bytes); never logged


def pair(bins: Bins, base: Path, autopilot_url: str, log_dir: Path) -> Paired:
    """oasa-pair init on both, then `drone --autopilot` and `ground --code -`, as scripts/sitl do.
    The drone loads the key into the autopilot and checks it with a signed command before saving."""
    drone = make_identity(bins, base, "drone-1")
    gcs = make_identity(bins, base, "gcs-1")
    port = free_udp_port()  # pairing uses TCP or UDP on --listen; a free number is enough
    log = log_dir / "pair-drone.out"
    with open(log, "wb") as out:
        d = subprocess.Popen([str(bins.pair), "drone", "--dir", str(drone.dir), "--name", "drone-1",
                              "--listen", f"{HOST}:{port}", "--wait", "90", "--autopilot", autopilot_url,
                              "--target", "1:1"], stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            code = None
            for _ in range(100):
                text = log.read_text(errors="replace")
                import re

                m = re.search(r"OASA1-[A-Z2-7]+", text)
                if m:
                    code = m.group(0)
                    break
                time.sleep(0.1)
            if not code:
                raise RuntimeError("oasa-pair drone showed no pairing code")
            g = subprocess.run([str(bins.pair), "ground", "--dir", str(gcs.dir), "--name", "gcs-1",
                                "--connect", f"{HOST}:{port}", "--code", "-"], input=code + "\n",
                               capture_output=True, text=True, timeout=120)
            if g.returncode != 0:
                raise RuntimeError(f"oasa-pair ground failed: {g.stderr.strip()[:300]}")
            if d.wait(120) != 0:
                raise RuntimeError(f"oasa-pair drone failed; see {log}")
        finally:
            if d.poll() is None:
                os.killpg(d.pid, signal.SIGKILL)
    keys = sorted((gcs.dir / "signing-keys").glob("*.bin"))
    if len(keys) != 1:
        raise RuntimeError("the ground station holds no single signing key after pairing")
    key = keys[0].read_bytes()[:32]
    if len(key) != 32 or not (drone.dir / "mavlink-signing-key.bin").exists():
        raise RuntimeError("pairing left no signing key")
    return Paired(drone, gcs, key)


# --- the ground station ---------------------------------------------------------------------------
class Station:
    """A pymavlink-based ground station. Receives on `local`, sends to `peer`.

    Sending and receiving use separate MAVLink objects so the test can send with any key (or none)
    without breaking what it reads. Incoming signatures are not verified: this station reads what the
    autopilot says; whether the autopilot accepts *our* frames is what the tests check."""

    def __init__(self, local: Addr, peer: Addr, key: bytes | None, heartbeat: bool = True):
        need_pymavlink()
        from pymavlink.dialects.v20 import common as mav2

        self.mav2 = mav2
        self.peer = peer
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.sock.bind(local)
        self.sock.settimeout(0.2)
        self.key = key
        self._tx: dict[Any, Any] = {}
        self._tx_lock = threading.Lock()
        self.rx = mav2.MAVLink(None)
        self.rx.robust_parsing = True
        self.msgs: list[tuple[float, Any]] = []
        self.datagrams = 0
        self._stop = False
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=self._recv, daemon=True)]
        if heartbeat:
            self._threads.append(threading.Thread(target=self._beat, daemon=True))
        for t in self._threads:
            t.start()

    # signing / sending
    def _encoder(self, key: bytes | None):
        k = key if key is None else bytes(key)
        if k not in self._tx:
            m = self.mav2.MAVLink(None, SYSID_GCS, COMPID_GCS)
            if k is not None:
                m.signing.secret_key = k
                m.signing.sign_outgoing = True
                m.signing.link_id = 7
                m.signing.timestamp = int((time.time() - 1420070400) * 100000)
            self._tx[k] = m
        return self._tx[k]

    def send(self, msg_fn, *args, key: Any = "own") -> None:
        """Send the message `msg_fn(encoder, *args)` signed with `key` ("own" = the paired key, None = unsigned)."""
        k = self.key if key == "own" else key
        with self._tx_lock:
            enc = self._encoder(k)
            raw = getattr(enc, msg_fn)(*args).pack(enc)
        self.sock.sendto(raw, self.peer)

    def _beat(self) -> None:
        mav = self.mav2
        while not self._stop:
            try:
                self.send("heartbeat_encode", mav.MAV_TYPE_GCS, mav.MAV_AUTOPILOT_INVALID, 0, 0, mav.MAV_STATE_ACTIVE)
            except OSError:
                pass
            time.sleep(1.0)

    def _recv(self) -> None:
        while not self._stop:
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            self.datagrams += 1
            now = time.monotonic()
            try:
                out = self.rx.parse_buffer(data) or []
            except Exception:
                continue
            with self._lock:
                for m in out:
                    self.msgs.append((now, m))

    # reading
    def snapshot(self) -> list[tuple[float, Any]]:
        with self._lock:
            return list(self.msgs)

    def of_type(self, name: str, since: float = 0.0) -> list[tuple[float, Any]]:
        return [(t, m) for t, m in self.snapshot() if t >= since and m.get_type() == name]

    def wait_for(self, name: str, timeout: float, since: float | None = None, pred=None):
        since = time.monotonic() if since is None else since
        hit: list = []

        def seen() -> bool:
            for t, m in self.of_type(name, since):
                if pred is None or pred(m):
                    hit.append(m)
                    return True
            return False

        wait_for(seen, timeout, 0.05)
        return hit[0] if hit else None

    def last(self, name: str):
        got = self.of_type(name)
        return got[-1][1] if got else None

    # commands
    def command(self, cmd: int, *params: float, timeout: float = 10.0, key: Any = "own",
                target=(1, 1)) -> int | None:
        """COMMAND_LONG, resent every second until its COMMAND_ACK. Returns the ack result, or None."""
        p = list(params) + [0.0] * (7 - len(params))
        t0 = time.monotonic()
        end = t0 + timeout
        while time.monotonic() < end:
            self.send("command_long_encode", target[0], target[1], cmd, 0, *p, key=key)
            until = min(end, time.monotonic() + 1.0)
            while time.monotonic() < until:
                for t, m in self.of_type("COMMAND_ACK", t0):
                    if m.command == cmd and m.result != self.mav2.MAV_RESULT_IN_PROGRESS:
                        return m.result
                time.sleep(0.05)
        return None

    def probe(self, key: Any, window: float = 5.0) -> bool:
        """Does the autopilot answer a REQUEST_MESSAGE(AUTOPILOT_VERSION) signed with `key`?"""
        t0 = time.monotonic()
        while time.monotonic() - t0 < window:
            self.send("command_long_encode", 1, 1, self.mav2.MAV_CMD_REQUEST_MESSAGE, 0, 148, 0, 0, 0, 0, 0, 0,
                      key=key)
            time.sleep(0.5)
            if self.of_type("COMMAND_ACK", t0):
                if any(m.command == self.mav2.MAV_CMD_REQUEST_MESSAGE for _, m in self.of_type("COMMAND_ACK", t0)):
                    return True
        return False

    def request_interval(self, msg_id: int, hz: float) -> None:
        self.command(self.mav2.MAV_CMD_SET_MESSAGE_INTERVAL, msg_id, 1e6 / hz, timeout=10)

    def rate(self, name: str, secs: float) -> float:
        t0 = time.monotonic()
        n0 = len(self.of_type(name))
        time.sleep(secs)
        return (len(self.of_type(name, t0))) / secs

    def close(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        for t in self._threads:
            t.join(1)


def new_tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def rm(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


# --- the whole topology for one test ------------------------------------------------------------
@dataclass
class Rig:
    """Module-wide state: the running autopilot, the paired identities and the fixed plaintext ports."""

    ap: Autopilot
    bins: Bins
    paired: Paired
    tmp: Path
    drone_plain: Addr  # the drone-side link's --plain-listen: the autopilot's side talks here
    ground_in: Addr  # the ground-side link's --plain-listen: the station sends here
    ground_out: Addr  # the ground-side link's --plain-send: the station listens here


class Links:
    """autopilot <-> drone oasa-link <-> simulated channel (captured) <-> ground oasa-link <-> Station.

    Use as a context manager inside one test. The channel's capture is declared encrypted by the
    suite, so the checker scans it for markers, MAVLink and non-random bytes."""

    MTU = "1280"  # PX4 batches several frames per datagram

    def __init__(self, lab, rig: Rig, *, loss: float = 0.05, drone_flags: list[str] | None = None,
                 delay_ms: float = 10.0, jitter_ms: float = 3.0, key: bytes | None | str = "paired"):
        from .linkharness import LinkProc

        self.lab, self.rig = lab, rig
        d_bind, g_bind = (HOST, free_udp_port()), (HOST, free_udp_port())
        self.channel = lab.channel(a=g_bind, b=d_bind, loss=loss, delay_ms=delay_ms, jitter_ms=jitter_ms)
        common = ["--mtu", self.MTU]
        # The drone dir holds mavlink-signing-key.bin, so its MAVLink guard is on by default.
        self.drone = LinkProc(rig.bins, rig.paired.drone, "gcs-1", bind=d_bind, remote=self.channel.addr_b,
                              initiate=False, plain_listen=rig.drone_plain, plain_send=rig.ap.plain_send(),
                              log_path=lab.file("link-drone.jsonl"), extra=common + list(drone_flags or []))
        self.ground = LinkProc(rig.bins, rig.paired.gcs, "drone-1", bind=g_bind, remote=self.channel.addr_a,
                               initiate=True, plain_listen=rig.ground_in, plain_send=rig.ground_out,
                               log_path=lab.file("link-ground.jsonl"), extra=common)
        self.station: Station | None = None
        self._key = rig.paired.key if key == "paired" else key
        self.stats: dict[str, dict | None] = {}

    def __enter__(self) -> "Links":
        if not self.ground.wait_event("session_established", 40):
            raise AssertionError("the link did not establish a session; see logs/ in the bundle")
        self.station = Station(self.rig.ground_out, self.rig.ground_in, self._key)
        return self

    def finish(self) -> None:
        if self.station:
            self.station.close()
            self.station = None
        self.stats["ground"] = self.ground.stop()
        self.stats["drone"] = self.drone.stop()
        self.channel.stop()

    def blocked(self) -> int:
        """Datagrams the drone-side guard has dropped so far (from its log)."""
        return sum(1 for e in self.drone.seen("drop") if e.get("reason") == "mavlink_blocked")

    def __exit__(self, *exc: Any) -> None:
        if self.station:
            self.station.close()
        self.ground.stop()
        self.drone.stop()
