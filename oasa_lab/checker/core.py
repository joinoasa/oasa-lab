"""The independent checker.

Standard library only. It does not import the runner, the channel, the pcap writer or the pytest
plugin, and it believes nothing the run says about itself: it re-reads the files, recomputes the
hashes and counts, and re-scans the captures. See docs/checker.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tomllib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import radio as radio_mod
from . import sigmf as sigmf_mod
from .air import airtime as air_time
from .air import check_air
from .pcapread import Packet, PcapError, parse_pcap
from .stats import binomial_bound, chi2_bytes, poisson_limit

CHECKER_VERSION = "1.2"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
SUITE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
CAPTURE_PATH = re.compile(r"^captures/[A-Za-z0-9._-]+\.pcap$")
OUTCOMES = {"passed", "failed", "skipped"}
MIN_RANDOMNESS_BYTES = 5000
CHI2_P_FLOOR = 1e-6  # a random capture fails this test about once in a million
MIN_TRIALS = 100  # fewer packets than this and a rate cannot be tested
MIN_MARKER_LEN = 12
FALSE_MAVLINK_PER_OFFSET = 2e-5  # upper estimate of chance matches in random data
MAX_FILE = 512 * 1024 * 1024


class Fail(Exception):
    """Raised inside a check to fail it with a reason."""

    def __init__(self, reason: str, **details: Any):
        super().__init__(reason)
        self.reason, self.details = reason, details


def parse_time(text: Any) -> datetime:
    if not isinstance(text, str) or not TIME.match(text):
        raise ValueError(f"not an RFC 3339 UTC time: {text!r}")
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Bundle:
    """Lazy, defensive reader for one bundle folder."""

    def __init__(self, root: Path, suites_dir: Path | None, require_hardware: bool = False):
        self.root = root
        self.suites_dir = suites_dir
        self.require_hardware = require_hardware
        self._cache: dict[str, Any] = {}

    def read_bytes(self, rel: str) -> bytes:
        p = self.root / rel
        if p.is_symlink() or not p.is_file():
            raise Fail(f"{rel} is missing or not a plain file")
        if p.stat().st_size > MAX_FILE:
            raise Fail(f"{rel} is too large to check")
        return p.read_bytes()

    def json(self, rel: str) -> Any:
        if rel not in self._cache:
            try:
                self._cache[rel] = json.loads(self.read_bytes(rel))
            except json.JSONDecodeError as exc:
                raise Fail(f"{rel} is not valid JSON ({exc})")
        return self._cache[rel]

    def manifest(self) -> dict:
        m = self.json("manifest.json")
        if not isinstance(m, dict):
            raise Fail("manifest.json is not an object")
        return m

    def log(self) -> list[dict]:
        if "log" not in self._cache:
            rows = []
            for n, line in enumerate(self.read_bytes("log.jsonl").decode("utf-8", "replace").splitlines(), 1):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    raise Fail(f"log.jsonl line {n} is not valid JSON")
                if not isinstance(rec, dict) or "event" not in rec or "ts" not in rec:
                    raise Fail(f"log.jsonl line {n} lacks ts or event")
                try:
                    rec["_t"] = parse_time(rec["ts"])
                except ValueError as exc:
                    raise Fail(f"log.jsonl line {n}: {exc}")
                rows.append(rec)
            self._cache["log"] = rows
        return self._cache["log"]

    def suite_decl(self, suite: str) -> dict | None:
        if self.suites_dir is None:
            return None
        f = self.suites_dir / suite / "suite.toml"
        if not f.is_file():
            raise Fail(f"suite {suite!r} is not declared in {self.suites_dir}")
        return tomllib.loads(f.read_text(encoding="utf-8"))

    def packets(self, rel: str) -> list[Packet]:
        key = "pcap:" + rel
        if key not in self._cache:
            try:
                self._cache[key] = parse_pcap(self.read_bytes(rel))
            except PcapError as exc:
                raise Fail(f"{rel}: {exc}")
        return self._cache[key]


# --- check 1: integrity -------------------------------------------------------------------
def check_integrity(b: Bundle) -> dict:
    sums_file = b.root / "SHA256SUMS"
    if not sums_file.is_file():
        raise Fail("SHA256SUMS is missing")
    listed: dict[str, str] = {}
    for n, line in enumerate(sums_file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = re.match(r"^([0-9a-f]{64})  (.+)$", line)
        if not m:
            raise Fail(f"SHA256SUMS line {n} is not in sha256sum format")
        digest, rel = m.groups()
        parts = rel.split("/")
        if rel.startswith("/") or ".." in parts or "" in parts or "\\" in rel or rel == "SHA256SUMS":
            raise Fail(f"SHA256SUMS line {n} has an unsafe path: {rel!r}")
        if rel in listed:
            raise Fail(f"SHA256SUMS lists {rel} twice")
        listed[rel] = digest
    present: set[str] = set()
    odd: list[str] = []
    for dirpath, dirnames, filenames in os.walk(b.root, followlinks=False):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(b.root).as_posix()
            if full.is_symlink():
                odd.append(rel)
        for name in filenames:
            full = Path(dirpath) / name
            if not full.is_symlink():
                present.add(full.relative_to(b.root).as_posix())
    present.discard("SHA256SUMS")
    missing = sorted(set(listed) - present)
    extra = sorted(present - set(listed))
    bad = sorted(rel for rel in set(listed) & present if sha256_file(b.root / rel) != listed[rel])
    problems = []
    if odd:
        problems.append(f"symbolic links not allowed: {odd}")
    if missing:
        problems.append(f"missing files: {missing}")
    if extra:
        problems.append(f"files not in SHA256SUMS: {extra}")
    if bad:
        problems.append(f"hash mismatch: {bad}")
    details = {"files": len(listed), "missing": missing, "extra": extra, "mismatched": bad}
    if problems:
        raise Fail("; ".join(problems), **details)
    return {"reason": f"all {len(listed)} files match SHA256SUMS; none missing or extra", "details": details}


# --- check 2: manifest --------------------------------------------------------------------
def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


AIR_MODELS = {
    "lora": ("lora_sf", "lora_bw_hz"),
    "bitrate": ("air_kbps",),
    "slots": ("slot_ms", "slot_payload"),
}


def _air_settings_errors(cid: str, s: dict) -> list[str]:
    """The airtime settings are optional (older bundles have none), but if present they must make sense."""
    kind = s.get("airtime", "")
    errs: list[str] = []
    if not isinstance(kind, str) or kind not in ("", *AIR_MODELS):
        return [f"{cid}: settings.airtime must be empty or one of {', '.join(AIR_MODELS)}"]
    if not isinstance(s.get("profile", ""), str):
        errs.append(f"{cid}: settings.profile must be text")
    for k in ("max_queue", "serial_bps"):
        if k in s and not (_num(s[k]) and s[k] >= 0):
            errs.append(f"{cid}: settings.{k} must be 0 or more")
    if "duty_cycle" in s and not (_num(s["duty_cycle"]) and 0 <= s["duty_cycle"] <= 1):
        errs.append(f"{cid}: settings.duty_cycle must be 0 to 1")
    if "half_duplex" in s and not isinstance(s["half_duplex"], bool):
        errs.append(f"{cid}: settings.half_duplex must be true or false")
    if not kind:
        if s.get("half_duplex") or s.get("max_queue") or s.get("duty_cycle"):
            errs.append(f"{cid}: half_duplex, max_queue and duty_cycle need an airtime model")
        return errs
    for k in AIR_MODELS[kind]:
        if not (_num(s.get(k)) and s[k] > 0):
            errs.append(f"{cid}: settings.{k} must be above 0 for the {kind} model")
    if s.get("rate_kbps"):
        errs.append(f"{cid}: rate_kbps and an airtime model cannot be combined")
    try:
        airtime_probe = air_time(s, 64, "a_to_b")
        if not (math.isfinite(airtime_probe) and airtime_probe > 0):
            errs.append(f"{cid}: the airtime model gives {airtime_probe} s for 64 bytes")
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        errs.append(f"{cid}: the airtime settings are incomplete or invalid ({type(exc).__name__}: {exc})")
    return errs


def check_manifest(b: Bundle) -> dict:
    m = b.manifest()
    errs: list[str] = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            errs.append(msg)

    need(m.get("schema") == 1, "schema must be 1")
    need(isinstance(m.get("run_id"), str) and bool(HEX32.match(m["run_id"])), "run_id must be 32 hex characters")
    need(isinstance(m.get("suite"), str) and bool(SUITE_NAME.match(m["suite"])), "suite name is missing or odd")
    need(isinstance(m.get("seed"), int) and not isinstance(m.get("seed"), bool), "seed must be an integer")
    start = end = None
    try:
        start, end = parse_time(m.get("start")), parse_time(m.get("end"))
        need(end > start, "end must be after start")
        need(end <= datetime.now(timezone.utc) + timedelta(seconds=60), "end is in the future")
    except ValueError as exc:
        errs.append(str(exc))
    lab = m.get("lab") if isinstance(m.get("lab"), dict) else {}
    need(bool(HEX40.match(str(lab.get("git_sha", "")))), "lab.git_sha must be 40 lowercase hex characters")
    need(isinstance(lab.get("git_dirty"), bool), "lab.git_dirty must be true or false")
    need(isinstance(lab.get("version"), str) and bool(lab.get("version")), "lab.version missing")
    py = m.get("python") if isinstance(m.get("python"), dict) else {}
    need(bool(re.match(r"^\d+\.\d+\.\d+", str(py.get("version", "")))), "python.version missing or odd")
    osd = m.get("os") if isinstance(m.get("os"), dict) else {}
    need(all(isinstance(osd.get(k), str) and osd.get(k) for k in ("system", "release", "machine")),
         "os.system, os.release and os.machine are required")
    pk = m.get("packages")
    need(isinstance(pk, dict) and all(isinstance(v, str) for v in (pk or {}).values()), "packages must map name to version")
    need(isinstance(pk, dict) and "pytest" in pk and "oasa-lab" in pk, "packages must include pytest and oasa-lab")

    markers = m.get("markers")
    need(isinstance(markers, list) and all(isinstance(x, str) and len(x) >= MIN_MARKER_LEN for x in markers),
         f"markers must be a list of strings of at least {MIN_MARKER_LEN} characters")
    need(isinstance(markers, list) and len(set(markers)) == len(markers), "markers must be unique")
    exp = m.get("expectations")
    need(isinstance(exp, list) and all(isinstance(x, str) and x for x in exp), "expectations must be a list of test names")
    for i, bn in enumerate(m.get("binaries") if isinstance(m.get("binaries"), list) else []):
        need(isinstance(bn, dict) and bool(HEX64.match(str(bn.get("sha256", "")))), f"binaries[{i}].sha256 must be 64 hex characters")
    need(isinstance(m.get("binaries"), list), "binaries must be a list")

    chans = m.get("channels") if isinstance(m.get("channels"), list) else None
    caps = m.get("captures") if isinstance(m.get("captures"), list) else None
    need(chans is not None, "channels must be a list")
    need(caps is not None, "captures must be a list")
    ids: set[str] = set()
    for i, c in enumerate(chans or []):
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or c["id"] in ids:
            errs.append(f"channels[{i}] needs a unique id")
            continue
        ids.add(c["id"])
        s = c.get("settings")
        if not isinstance(s, dict):
            errs.append(f"{c['id']}: settings missing")
            continue
        for k in ("loss", "reorder", "duplicate", "corrupt", "replay"):
            need(_num(s.get(k)) and 0 <= s[k] <= 1, f"{c['id']}: settings.{k} must be 0 to 1")
        for k in ("delay_ms", "jitter_ms", "rate_kbps", "mtu"):
            need(_num(s.get(k)) and s[k] >= 0, f"{c['id']}: settings.{k} must be 0 or more")
        need(isinstance(s.get("seed"), int) and not isinstance(s.get("seed"), bool), f"{c['id']}: settings.seed must be an integer")
        errs.extend(_air_settings_errors(c["id"], s))
        need(c.get("profile", s.get("profile", "")) == s.get("profile", ""), f"{c['id']}: profile name and settings.profile differ")
        need(isinstance(c.get("endpoints"), dict), f"{c['id']}: endpoints missing")
    files: set[str] = set()
    chan_files: set[str] = set()
    dev_ids = {d.get("id") for d in (m.get("hardware") or {}).get("devices", []) if isinstance(d, dict)}
    for i, cap in enumerate(caps or []):
        if not isinstance(cap, dict):
            errs.append(f"captures[{i}] is not an object")
            continue
        f = cap.get("file")
        need(isinstance(f, str) and bool(CAPTURE_PATH.match(f)), f"captures[{i}].file must be captures/NAME.pcap")
        need(isinstance(f, str) and f not in files, f"captures[{i}].file is listed twice")
        files.add(str(f))
        if cap.get("kind") == "air":  # frames a radio modem heard, not a simulated channel
            need(cap.get("device") in dev_ids, f"captures[{i}] names a device the manifest's hardware does not list")
            need("channel" not in cap, f"captures[{i}]: an air capture has no channel")
        else:
            need(cap.get("channel") in ids, f"captures[{i}] names an unknown channel")
            chan_files.add(str(f))
        for k in ("encrypted", "expect_plaintext", "expect_mavlink"):
            need(isinstance(cap.get(k), bool), f"captures[{i}].{k} must be true or false")
        need(not (cap.get("encrypted") is True and (cap.get("expect_plaintext") is True or cap.get("expect_mavlink") is True)),
             f"captures[{i}]: a capture cannot be both encrypted and a plaintext control")
        sk = cap.get("skip_header_bytes")
        need(isinstance(sk, int) and not isinstance(sk, bool) and 0 <= sk <= 64, f"captures[{i}].skip_header_bytes must be 0 to 64")
        need(isinstance(cap.get("markers"), list) and all(x in (markers or []) for x in cap["markers"]),
             f"captures[{i}].markers must be a subset of markers")
    need(len(ids) == len(chans or []) and chan_files == {c.get("capture") for c in chans or []},
         "every channel needs exactly one capture and the lists must agree")
    # every pcap in the bundle must be declared, or it would escape the scan
    undeclared = sorted(
        p.relative_to(b.root).as_posix() for p in (b.root / "captures").glob("*") if p.is_file()
    ) if (b.root / "captures").is_dir() else []
    need(set(undeclared) <= files, f"captures not declared in the manifest: {sorted(set(undeclared) - files)}")
    if isinstance(m.get("suite"), str) and SUITE_NAME.match(m["suite"]):
        try:
            b.suite_decl(m["suite"])
        except Fail as f:
            errs.append(f.reason)
    if errs:
        raise Fail("; ".join(errs[:8]) + (f" (and {len(errs) - 8} more)" if len(errs) > 8 else ""), errors=errs)
    note = " (git tree was dirty)" if lab.get("git_dirty") else ""
    return {"reason": f"all required fields present and sane; {len(ids)} channel(s), {len(files)} capture(s){note}",
            "details": {"git_dirty": lab.get("git_dirty"), "suite_declaration": "used" if b.suites_dir else "not used"}}


# --- check 3: results agree with the log ---------------------------------------------------
def ctrf_tests(b: Bundle) -> list[dict]:
    c = b.json("results.ctrf.json")
    try:
        if c["reportFormat"] != "CTRF":
            raise Fail("reportFormat is not CTRF")
        res = c["results"]
        tests, summary = res["tests"], res["summary"]
    except (KeyError, TypeError):
        raise Fail("results.ctrf.json is not well-formed CTRF (needs reportFormat, results.tests, results.summary)")
    if not isinstance(tests, list) or not isinstance(summary, dict):
        raise Fail("results.ctrf.json: tests must be a list and summary an object")
    for i, t in enumerate(tests):
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not isinstance(t.get("status"), str):
            raise Fail(f"results.ctrf.json: tests[{i}] needs name and status")
    return tests


def check_results(b: Bundle) -> dict:
    m = b.manifest()
    tests = ctrf_tests(b)
    summary = b.json("results.ctrf.json")["results"]["summary"]
    errs: list[str] = []
    names = [t["name"] for t in tests]
    if not tests:
        errs.append("no tests were run")
    if len(set(names)) != len(names):
        errs.append("duplicate test names in CTRF")
    actual = {k: sum(1 for t in tests if t["status"] == k) for k in ("passed", "failed", "skipped", "pending", "other")}
    actual["tests"] = len(tests)
    for k, v in actual.items():
        if summary.get(k) != v:
            errs.append(f"CTRF summary.{k} is {summary.get(k)!r} but the test list has {v}")
    # log
    log = b.log()
    if not log or log[0]["event"] != "run_start":
        errs.append("log.jsonl must begin with run_start")
    if not log or log[-1]["event"] != "run_end":
        errs.append("log.jsonl must end with run_end")
    if sum(1 for r in log if r["event"] == "run_start") != 1 or sum(1 for r in log if r["event"] == "run_end") != 1:
        errs.append("log.jsonl needs exactly one run_start and one run_end")
    for r in (log[0], log[-1]) if log else ():
        if r.get("run_id") != m.get("run_id"):
            errs.append(f"{r['event']} run_id differs from the manifest")
    if log and log[0].get("seed") != m.get("seed"):
        errs.append("run_start seed differs from the manifest")
    for prev, cur in zip(log, log[1:]):
        if (prev["_t"] - cur["_t"]).total_seconds() > 1.0:
            errs.append("log time stamps go backwards")
            break
    try:
        s, e = parse_time(m.get("start")), parse_time(m.get("end"))
        if log and ((log[0]["_t"] - s).total_seconds() < -1 or (log[-1]["_t"] - e).total_seconds() > 1):
            errs.append("log times fall outside the manifest's start and end")
    except ValueError:
        pass
    started: dict[str, int] = {}
    ended: dict[str, str] = {}
    for i, r in enumerate(log):
        if r["event"] == "test_start":
            t = r.get("test")
            if t in started:
                errs.append(f"{t} has two test_start lines")
            started[t] = i
        elif r["event"] == "test_end":
            t = r.get("test")
            if t in ended:
                errs.append(f"{t} has two test_end lines")
            elif t not in started:
                errs.append(f"{t} has test_end without test_start")
            if r.get("outcome") not in OUTCOMES:
                errs.append(f"{t}: log outcome {r.get('outcome')!r} is not passed, failed or skipped")
            ended[t] = r.get("outcome")
    ctrf = {t["name"]: t["status"] for t in tests}
    for t in sorted(set(ctrf) | set(started) | set(ended)):
        if t not in ctrf:
            errs.append(f"{t} is in the log but not in CTRF")
        elif t not in started or t not in ended:
            errs.append(f"{t} is in CTRF but has no complete test_start/test_end in the log")
        elif ended[t] != ctrf[t]:
            errs.append(f"{t}: log says {ended[t]}, CTRF says {ctrf[t]}")
    if log and log[-1]["event"] == "run_end":
        for k in ("passed", "failed", "skipped"):
            if log[-1].get(k) != actual[k]:
                errs.append(f"run_end {k}={log[-1].get(k)!r} but CTRF has {actual[k]}")
    # JUnit: count what is in it, do not trust its attributes
    try:
        raw = b.read_bytes("junit.xml")
        if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
            raise Fail("junit.xml contains a DOCTYPE or ENTITY; refusing to parse")
        root = ET.fromstring(raw)
        cases = list(root.iter("testcase"))
        j = {"passed": 0, "failed": 0, "skipped": 0}
        for c in cases:
            if c.find("failure") is not None or c.find("error") is not None:
                j["failed"] += 1
            elif c.find("skipped") is not None:
                j["skipped"] += 1
            else:
                j["passed"] += 1
        for k in j:
            if j[k] != actual[k]:
                errs.append(f"junit.xml has {j[k]} {k}, CTRF has {actual[k]}")
    except ET.ParseError as exc:
        errs.append(f"junit.xml is not valid XML ({exc})")
    except Fail as f:
        errs.append(f.reason)
    if errs:
        raise Fail("; ".join(errs[:6]) + (f" (and {len(errs) - 6} more)" if len(errs) > 6 else ""), errors=errs)
    return {"reason": f"{len(tests)} tests: CTRF, log.jsonl and junit.xml agree "
                      f"({actual['passed']} passed, {actual['failed']} failed, {actual['skipped']} skipped)",
            "details": {k: actual[k] for k in ("tests", "passed", "failed", "skipped")}}


# --- check 4: no readable payload ----------------------------------------------------------
def _mavlink_tiles(buf: bytes, off: int) -> bool:
    """True if buf[off:] is exactly one or more complete MAVLink 1 or 2 frames."""
    pos, n = off, 0
    while pos < len(buf):
        magic = buf[pos]
        if magic == 0xFE:  # v1: magic len seq sysid compid msgid payload crc16
            if pos + 6 > len(buf) or buf[pos + 3] == 0:
                return False
            size = 6 + buf[pos + 1] + 2
        elif magic == 0xFD:  # v2: magic len incompat compat seq sysid compid msgid(3) payload crc16 [sig 13]
            if pos + 10 > len(buf) or buf[pos + 2] not in (0, 1) or buf[pos + 5] == 0:
                return False
            size = 10 + buf[pos + 1] + 2 + (13 if buf[pos + 2] & 1 else 0)
        else:
            return False
        pos += size
        n += 1
    return n > 0 and pos == len(buf)


def scan_capture(packets: list[Packet], markers: list[str], skip: int) -> dict:
    payloads = [p.payload for p in packets]
    found = sorted({m for m in markers if any(m.encode() in pl for pl in payloads)})
    offsets = sorted({0, skip})
    mav = sum(1 for pl in payloads if any(_mavlink_tiles(pl, o) for o in offsets))
    mav_limit = poisson_limit(len(payloads) * len(offsets) * FALSE_MAVLINK_PER_OFFSET)
    counts = [0] * 256
    nbytes = 0
    for pl in set(payloads):  # identical copies (duplicates, replays) would distort the statistic
        body = pl[skip:]
        nbytes += len(body)
        for byte, c in _hist(body).items():
            counts[byte] += c
    chi2 = p = None
    if nbytes >= MIN_RANDOMNESS_BYTES:
        chi2, p = chi2_bytes(counts)
    return {"packets": len(payloads), "markers_found": found, "mavlink_frames": mav,
            "mavlink_chance_limit": mav_limit, "random_bytes_tested": nbytes,
            "chi2": None if chi2 is None else round(chi2, 1), "chi2_p": p,
            "looks_random": None if p is None else p >= CHI2_P_FLOOR}


def _hist(data: bytes) -> dict[int, int]:
    h: dict[int, int] = {}
    for byte in data:
        h[byte] = h.get(byte, 0) + 1
    return h


def check_payload(b: Bundle) -> dict:
    m = b.manifest()
    markers = m.get("markers", [])
    decl = b.suite_decl(m["suite"]) if b.suites_dir else None
    results, errs, notes = [], [], []
    for cap in m.get("captures", []):
        f = cap["file"]
        pk = b.packets(f)
        r = scan_capture(pk, markers, cap["skip_header_bytes"])
        r["file"] = f
        if decl is not None:
            d = decl.get("captures", {})
            for key in ("encrypted", "expect_plaintext", "expect_mavlink"):
                if bool(d.get(key, False)) != cap[key]:
                    errs.append(f"{f}: manifest says {key}={cap[key]} but the suite file says {bool(d.get(key, False))}")
            if int(d.get("skip_header_bytes", 0)) != cap["skip_header_bytes"]:
                errs.append(f"{f}: skip_header_bytes differs from the suite file")
        if cap["encrypted"]:
            r["mode"] = "encrypted"
            if not pk:
                errs.append(f"{f}: capture is empty, so it proves nothing")
            if r["markers_found"]:
                errs.append(f"{f}: readable marker(s) in an encrypted capture: {r['markers_found']}")
            if r["mavlink_frames"] > r["mavlink_chance_limit"]:
                errs.append(f"{f}: {r['mavlink_frames']} packets are MAVLink frames (chance limit {r['mavlink_chance_limit']})")
            if r["looks_random"] is False:
                errs.append(f"{f}: payload bytes do not look random (chi-square {r['chi2']}, p={r['chi2_p']:.2g})")
            if r["looks_random"] is None:
                notes.append(f"{f}: too few bytes ({r['random_bytes_tested']}) for the randomness test")
        elif cap["expect_plaintext"] or cap["expect_mavlink"]:
            r["mode"] = "negative control"
            if cap["expect_plaintext"]:
                want = cap["markers"]
                if not want:
                    errs.append(f"{f}: negative control declares no marker")
                missing = [x for x in want if x not in r["markers_found"]]
                if missing:
                    errs.append(f"{f}: negative control failed, the scanner did not find marker(s) {missing}")
                if r["looks_random"] is True:
                    errs.append(f"{f}: negative control failed, plaintext passed the randomness test")
            if cap["expect_mavlink"] and r["mavlink_frames"] <= r["mavlink_chance_limit"]:
                errs.append(f"{f}: negative control failed, the scanner did not find MAVLink frames")
        else:
            r["mode"] = "not declared encrypted"
        results.append(r)
    if errs:
        raise Fail("; ".join(errs[:6]), errors=errs, captures=results)
    if not results:
        return {"reason": "no captures in this run; nothing to scan", "details": {"captures": []}}
    tally: dict[str, int] = {}
    for r in results:
        tally[r["mode"]] = tally.get(r["mode"], 0) + 1
    modes = ", ".join(f"{n} {mode}" for mode, n in sorted(tally.items()))
    return {"reason": f"{len(results)} capture(s): {modes}" + (f". Note: {len(notes)} too small for the randomness test" if notes else ""),
            "details": {"captures": results, "notes": notes}}


# --- check 5: numbers make sense -----------------------------------------------------------
STAT_KEYS = ("sent", "dropped", "duplicated", "reordered", "corrupted", "replayed", "too_big",
             "unrouted", "abandoned", "forwarded")
OPTIONAL_STAT_KEYS = ("queue_dropped",)  # added with the airtime model; absent (= 0) in older logs


def check_numbers(b: Bundle) -> dict:
    m = b.manifest()
    log = b.log()
    finals: dict[str, dict] = {}
    for r in log:
        if r["event"] == "channel_stats" and r.get("final") is True:
            if r.get("channel") in finals:
                raise Fail(f"two final channel_stats lines for {r.get('channel')}")
            finals[r.get("channel")] = r
    errs, rows, skipped, air_rows = [], [], [], []
    declared = {c["id"] for c in m.get("channels", [])}
    for extra in sorted(set(finals) - declared):
        errs.append(f"log has channel_stats for {extra}, which the manifest does not list")
    for ch in m.get("channels", []):
        cid, s = ch["id"], ch["settings"]
        st = finals.get(cid)
        if st is None:
            errs.append(f"{cid}: no final channel_stats line in the log")
            continue
        cap = next(c for c in m["captures"] if c.get("channel") == cid)
        pk = b.packets(cap["file"])
        ep = ch["endpoints"]
        dest = {"a_to_b": ep.get("b"), "b_to_a": ep.get("a")}
        by_dir = {d: [p for p in pk if dest[d] is not None and f"{p.dst[0]}:{p.dst[1]}" == dest[d]] for d in dest}
        if sum(len(v) for v in by_dir.values()) != len(pk):
            errs.append(f"{cid}: capture holds packets addressed to neither endpoint")
        if s["mtu"] and any(len(p.payload) > s["mtu"] for p in pk):
            errs.append(f"{cid}: capture holds a packet larger than mtu {s['mtu']}")
        for d in ("a_to_b", "b_to_a"):
            x = st.get(d)
            if isinstance(x, dict):
                x = {**dict.fromkeys(OPTIONAL_STAT_KEYS, 0), **x}
            if not isinstance(x, dict) or any(not isinstance(x.get(k), int) or x[k] < 0
                                              for k in STAT_KEYS + OPTIONAL_STAT_KEYS):
                errs.append(f"{cid} {d}: channel_stats incomplete")
                continue
            tag = f"{cid} {d}"
            expected_fwd = (x["sent"] - x["dropped"] - x["too_big"] - x["unrouted"] - x["queue_dropped"]
                            + x["duplicated"] + x["replayed"] - x["abandoned"])
            if x["queue_dropped"] and not s.get("max_queue"):
                errs.append(f"{tag}: {x['queue_dropped']} queue drops, but max_queue is 0 (no limit)")
            if x["forwarded"] != expected_fwd:
                errs.append(f"{tag}: forwarded {x['forwarded']} but the counts add up to {expected_fwd}")
            if len(by_dir[d]) != x["forwarded"]:
                errs.append(f"{tag}: log says {x['forwarded']} forwarded, capture holds {len(by_dir[d])}")
            if x["abandoned"]:
                errs.append(f"{tag}: {x['abandoned']} packets were abandoned in flight")
            n_loss = x["sent"] - x["too_big"] - x["unrouted"] - x["queue_dropped"]
            survivors = n_loss - x["dropped"]
            for name, key, p, trials, slack in (
                ("loss", "dropped", s["loss"], n_loss, 0.0),
                ("duplicate", "duplicated", s["duplicate"], survivors, 0.0),
                ("corrupt", "corrupted", s["corrupt"], survivors, 0.0),
                ("reorder", "reordered", s["reorder"], survivors, 0.0),
                ("replay", "replayed", s["replay"], survivors, 1.0),
            ):
                k = x[key]
                row = {"channel": cid, "direction": d, "metric": name, "configured": p,
                       "trials": trials, "count": k}
                if p == 0:
                    ok = k == 0
                    why = "must be 0"
                elif p == 1 and name != "replay":
                    ok = k == trials
                    why = f"must be {trials}"
                elif trials < MIN_TRIALS:
                    skipped.append(f"{tag} {name}: only {trials} packets")
                    row["result"] = "skipped (too few packets)"
                    rows.append(row)
                    continue
                else:
                    lo, hi = binomial_bound(trials, p)
                    ok = lo - slack <= k <= hi
                    why = f"expected {lo:.0f} to {hi:.0f}"
                    row["measured"] = round(k / trials, 4)
                row["result"] = "ok" if ok else "OUT OF BOUNDS"
                rows.append(row)
                if not ok:
                    errs.append(f"{tag} {name}: {k} of {trials} (configured {p}); {why}")
        air_errs, air_info = check_air(cid, s, by_dir)
        errs.extend(air_errs)
        if air_info["model"]:
            air_rows.append({"channel": cid, **air_info})
    if errs:
        raise Fail("; ".join(errs[:6]) + (f" (and {len(errs) - 6} more)" if len(errs) > 6 else ""),
                   errors=errs, rows=rows, airtime=air_rows)
    if not m.get("channels"):
        return {"reason": "no channels in this run; nothing to check", "details": {}}
    note = f"; airtime respected on {len(air_rows)} channel(s)" if air_rows else ""
    return {"reason": f"{len(m['channels'])} channel(s): stats match the captures; {sum(r['result'] == 'ok' for r in rows)} "
                      f"rates within 5 sigma" + note + (f"; {len(skipped)} skipped (too few packets)" if skipped else ""),
            "details": {"rows": rows, "airtime": air_rows}}


# --- check 6: known-bad cases ran and passed -----------------------------------------------
def _matches(exp: str, name: str) -> bool:
    return name == exp or name.startswith(exp + "[") or name.endswith("::" + exp) or \
        (("::" + exp + "[") in name)


def check_expectations(b: Bundle) -> dict:
    m = b.manifest()
    exps = list(m.get("expectations", []))
    decl = b.suite_decl(m["suite"]) if b.suites_dir else None
    if decl is not None:
        missing = [e for e in decl.get("expectations", []) if e not in exps]
        if missing:
            raise Fail(f"the manifest dropped expectation(s) the suite file requires: {missing}")
    tests = ctrf_tests(b)
    log = {r.get("test"): r for r in b.log() if r["event"] == "test_end"}
    errs, rows = [], []
    for e in exps:
        hits = [t for t in tests if _matches(e, t["name"])]
        if not hits:
            errs.append(f"expected test {e!r} is not in the results")
            continue
        for t in hits:
            lg = log.get(t["name"], {})
            if t["status"] != "passed" or lg.get("outcome") != "passed":
                why = t.get("message") or lg.get("reason") or ""
                errs.append(f"expected test {t['name']} did not pass ({t['status']}{': ' + why if why else ''})")
            rows.append({"expectation": e, "test": t["name"], "status": t["status"]})
    if errs:
        raise Fail("; ".join(errs[:6]), errors=errs, rows=rows)
    if not exps:
        return {"reason": "the suite declares no known-bad cases", "details": {"decl": decl is not None}}
    src = "confirmed against the suite file" if decl is not None else "taken from the manifest only"
    return {"reason": f"{len(exps)} expected test(s) ran and passed ({src})", "details": {"rows": rows}}



# --- check 7: radio evidence ----------------------------------------------------------------
DEVICE_KINDS = ("pluto", "rtlsdr", "serial-radio")
BACKENDS = ("sim", "hardware")
RF_SETUPS = ("cabled", "shielded")
MIN_LOSS_TX_DB = 60.0       # cable loss between a transmitter and any receiver that a transmitter could damage
MIN_LOSS_RX_ONLY_DB = 40.0  # ... and a receive-only device (an RTL-SDR)
RUN_SLACK_S = 2.0           # a recording may start this long before the run or end after it
LIMIT_KEYS = ("min", "max")


def _sums_listed(b: Bundle) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (b.root / "SHA256SUMS").read_text(encoding="utf-8", errors="replace").splitlines():
        mm = re.match(r"^([0-9a-f]{64})  (.+)$", line)
        if mm:
            out[mm.group(2)] = mm.group(1)
    return out


def _device_errors(devices: Any, hub: Any) -> tuple[list[str], dict[str, dict]]:
    errs: list[str] = []
    by_id: dict[str, dict] = {}
    if not isinstance(devices, list):
        return ["hardware.devices must be a list"], by_id
    for i, d in enumerate(devices):
        if not isinstance(d, dict) or not isinstance(d.get("id"), str) or d["id"] in by_id:
            errs.append(f"hardware.devices[{i}] needs a unique id")
            continue
        by_id[d["id"]] = d
        tag = f"device {d['id']}"
        if d.get("kind") not in DEVICE_KINDS:
            errs.append(f"{tag}: kind must be one of {DEVICE_KINDS}")
        if d.get("backend") not in BACKENDS:
            errs.append(f"{tag}: backend must be sim or hardware")
        for k in ("model", "serial", "firmware"):
            if not (isinstance(d.get(k), str) and d[k]):
                errs.append(f"{tag}: {k} is missing")
        if d.get("backend") == "hardware":
            if d.get("serial") in ("n/a", None, ""):
                errs.append(f"{tag}: a real device must have a serial number on record")
            if d.get("kind") in ("pluto", "serial-radio") and d.get("firmware") in ("n/a", None, ""):
                errs.append(f"{tag}: a real {d.get('kind')} must have its firmware version on record")
    if hub is not None and not (isinstance(hub, dict) and hub.get("backend") in BACKENDS):
        errs.append("hardware.hub needs a backend (sim or hardware)")
    return errs, by_id


def _rf_errors(rf: Any, by_id: dict[str, dict], transmits: bool) -> list[str]:
    if rf is None:
        return ["the suite transmits but the manifest has no hardware.rf (RF setup and cable losses)"] if transmits else []
    errs: list[str] = []
    if not isinstance(rf, dict) or rf.get("setup") not in RF_SETUPS:
        return [f"hardware.rf.setup must be one of {RF_SETUPS} (radiating over the air is not allowed at this level)"]
    paths = rf.get("paths")
    if not (isinstance(paths, list) and paths):
        return ["hardware.rf.paths must list the cable paths with their loss"]
    for i, p in enumerate(paths):
        if not isinstance(p, dict) or not all(isinstance(p.get(k), str) and p[k] for k in ("name", "from", "to")) \
                or not _num(p.get("loss_db")) or not isinstance(p.get("receiver_transmits"), bool):
            errs.append(f"hardware.rf.paths[{i}] needs name, from, to, loss_db and receiver_transmits")
            continue
        for end in ("from", "to"):
            if p[end].split(":")[0] not in by_id:
                errs.append(f"RF path {p['name']}: {end} {p[end]!r} is not a device in the manifest")
        need = MIN_LOSS_TX_DB if p["receiver_transmits"] else MIN_LOSS_RX_ONLY_DB
        if p["loss_db"] < need:
            errs.append(f"RF path {p['name']}: {p['loss_db']:g} dB is below the {need:g} dB required "
                        f"before a {'radio' if p['receiver_transmits'] else 'receive-only device'}")
    return errs


def _burst_errors(b: Bundle, m: dict, rec: dict, meta_facts: dict, data_path: Path, by_id: dict[str, dict]) -> tuple[list[str], dict]:
    bc = rec["burst_check"]
    tag = rec["base"]
    errs: list[str] = []
    if not (isinstance(bc, dict) and isinstance(bc.get("captures"), list) and bc["captures"] and isinstance(bc.get("model"), dict)):
        return [f"{tag}: burst_check needs captures (a list) and model (LoRa airtime settings)"], {}
    model = bc["model"]
    overhead = bc.get("overhead_bytes", 0)
    if model.get("airtime") != "lora" or not _int_ge0(overhead):
        return [f"{tag}: burst_check.model must be a lora airtime model and overhead_bytes 0 or more"], {}
    errs.extend(_air_settings_errors(tag, dict(model)))
    caps = {c.get("file"): c for c in m.get("captures", []) if isinstance(c, dict)}
    frames: list[tuple[float, float]] = []
    for f in bc["captures"]:
        cap = caps.get(f)
        if cap is None or cap.get("kind") != "air":
            errs.append(f"{tag}: burst_check names {f!r}, which is not an air capture in the manifest")
            continue
        # the model must be what the modem was declared to run, so it cannot be tuned to fit the recording
        declared = ((by_id.get(cap.get("device"), {}).get("radio") or {}).get("airtime")) or {}
        for k, v in model.items():
            if k in declared and declared[k] != v:
                errs.append(f"{tag}: burst_check.model.{k}={v!r} but {cap.get('device')} was declared with {declared[k]!r}")
        try:
            for p in b.packets(f):
                frames.append((p.ts, air_time(model, len(p.payload) + overhead, "a_to_b")))
        except Fail as exc:
            errs.append(exc.reason)
    if errs:
        return errs, {}
    frames.sort()
    if not frames:
        return [f"{tag}: the air captures hold no frames, so there is nothing to line the recording up with"], {}
    try:
        levels, win = radio_mod.window_levels(data_path, meta_facts["datatype"], meta_facts["sample_rate"])
    except radio_mod.RadioError as exc:
        return [f"{tag}: {exc}"], {}
    bursts, _noise = radio_mod.detect_bursts(levels, win)
    t0 = parse_time(meta_facts["datetime"]).timestamp()
    r = radio_mod.align(frames, bursts, t0)
    r["recording"] = tag
    if r["matched"] < radio_mod.MIN_MATCH_FRACTION * r["frames"]:
        errs.append(f"{tag}: only {r['matched']} of {r['frames']} frames line up with a burst of the right length "
                    f"({r['duration_mismatch']} had a burst at the right time but the wrong length)")
    allowed = max(2, math.ceil(0.1 * r["bursts"]))
    if r["unmatched_bursts"] > allowed:
        errs.append(f"{tag}: {r['unmatched_bursts']} bursts on the air match no frame (more than {allowed} allowed)")
    if r["mean_snr_db"] is not None and r["mean_snr_db"] < radio_mod.MIN_SNR_DB:
        errs.append(f"{tag}: bursts are only {r['mean_snr_db']} dB above the noise (need {radio_mod.MIN_SNR_DB:g})")
    if abs(r["offset_s"]) > radio_mod.MAX_OFFSET_S:
        errs.append(f"{tag}: recording clock is {r['offset_s']} s from the host clock (limit {radio_mod.MAX_OFFSET_S} s)")
    return errs, r


def _int_ge0(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _recording_errors(b: Bundle, m: dict, recs: Any, by_id: dict[str, dict]) -> tuple[list[str], list[dict]]:
    errs: list[str] = []
    bursts: list[dict] = []
    if not isinstance(recs, list):
        return ["recordings must be a list"], bursts
    sums = _sums_listed(b)
    seen: set[str] = set()
    declared_files: set[str] = set()
    run_start = run_end = None
    try:
        run_start, run_end = parse_time(m.get("start")), parse_time(m.get("end"))
    except ValueError:
        pass
    for i, rec in enumerate(recs):
        if not isinstance(rec, dict):
            errs.append(f"recordings[{i}] is not an object")
            continue
        base, meta_rel, data_rel = rec.get("base"), rec.get("meta"), rec.get("data")
        if not (isinstance(base, str) and meta_rel == f"{base}.sigmf-meta" and data_rel == f"{base}.sigmf-data"
                and sigmf_mod.RECORDING_FILE.match(str(meta_rel)) and sigmf_mod.RECORDING_FILE.match(str(data_rel))):
            errs.append(f"recordings[{i}]: base, meta and data must be recordings/NAME and its two SigMF files")
            continue
        if base in seen:
            errs.append(f"{base} is listed twice")
            continue
        seen.add(base)
        declared_files.update((meta_rel, data_rel))
        if rec.get("device") not in by_id:
            errs.append(f"{base}: device {rec.get('device')!r} is not a device in the manifest")
        for rel in (meta_rel, data_rel):
            if rel not in sums:
                errs.append(f"{rel} is not hashed in SHA256SUMS")
        data_path, meta_path = b.root / data_rel, b.root / meta_rel
        if any(p.is_symlink() or not p.is_file() for p in (data_path, meta_path)):
            errs.append(f"{base}: a SigMF file is missing or not a plain file")
            continue
        try:
            meta = json.loads(meta_path.read_bytes())
        except json.JSONDecodeError as exc:
            errs.append(f"{meta_rel} is not valid JSON ({exc})")
            continue
        problems, facts = sigmf_mod.validate_meta(meta, data_path.stat().st_size, sigmf_mod.sha512_file(data_path))
        errs.extend(f"{base}: {p}" for p in problems)
        if problems:
            continue
        g = meta["global"]
        if g.get("oasa:run_id") != m.get("run_id"):
            errs.append(f"{base}: oasa:run_id is not this run's id (a recording from another run?)")
        if g.get("oasa:device") != rec.get("device"):
            errs.append(f"{base}: oasa:device differs from the manifest")
        for key, got in (("datatype", facts["datatype"]), ("sample_rate", facts["sample_rate"]),
                         ("center_hz", facts["frequency"]), ("samples", facts["samples"])):
            if rec.get(key) != got:
                errs.append(f"{base}: manifest {key}={rec.get(key)!r} but the SigMF file says {got!r}")
        t = parse_time(facts["datetime"])
        if run_start and run_end and not (run_start - timedelta(seconds=RUN_SLACK_S) <= t <= run_end + timedelta(seconds=RUN_SLACK_S)):
            errs.append(f"{base}: recorded at {facts['datetime']}, outside the run ({m.get('start')} to {m.get('end')})")
        if rec.get("burst_check") is not None:
            e, r = _burst_errors(b, m, rec, facts, data_path, by_id)
            errs.extend(e)
            if r:
                bursts.append(r)
    folder = b.root / "recordings"
    if folder.is_dir():
        for p in sorted(folder.iterdir()):
            rel = p.relative_to(b.root).as_posix()
            if rel not in declared_files:
                errs.append(f"{rel} is in recordings/ but not listed in the manifest")
    return errs, bursts


def _measurement_errors(b: Bundle, decl: dict | None) -> tuple[list[str], int]:
    errs: list[str] = []
    rows = [r for r in b.log() if r["event"] == "measurement"]
    limits = (decl or {}).get("limits", {}) if decl is not None else None
    seen: set[str] = set()
    for r in rows:
        metric, value = r.get("metric"), r.get("value")
        lo, hi = r.get("min"), r.get("max")
        if not isinstance(metric, str) or not _num(value) or any(x is not None and not _num(x) for x in (lo, hi)):
            errs.append(f"a measurement line is malformed: {metric!r}")
            continue
        seen.add(metric)
        inside = (lo is None or value >= lo) and (hi is None or value <= hi)
        if r.get("ok") is not inside:
            errs.append(f"{r.get('name', metric)}: the log says ok={r.get('ok')} but {value} against {lo}..{hi} is {inside}")
        elif not inside:
            errs.append(f"{r.get('name', metric)} = {value:g} is outside its limits ({lo}..{hi})")
        if limits is not None:
            want = limits.get(metric)
            if want is None:
                errs.append(f"measurement {metric!r} has no [limits.{metric}] in the suite file")
            elif (want.get("min"), want.get("max")) != (lo, hi):
                errs.append(f"{r.get('name', metric)}: limits {lo}..{hi} differ from the suite file's "
                            f"{want.get('min')}..{want.get('max')}")
    if limits:
        for metric in limits:
            if metric not in seen:
                errs.append(f"limit {metric!r} is declared in the suite file but nothing was measured against it")
    return errs, len(rows)


def check_radio(b: Bundle) -> dict:
    m = b.manifest()
    decl = b.suite_decl(m["suite"]) if b.suites_dir else None
    hw = m.get("hardware") if isinstance(m.get("hardware"), dict) else {}
    recs = m.get("recordings", [])
    transmits = m.get("transmits", False)
    requires = m.get("requires", [])
    errs: list[str] = []
    if not isinstance(transmits, bool):
        errs.append("transmits must be true or false")
        transmits = False
    if not (isinstance(requires, list) and all(isinstance(x, str) for x in requires)):
        errs.append("requires must be a list of device names")
        requires = []
    if decl is not None:
        if bool(decl.get("transmits", False)) != transmits:
            errs.append(f"manifest says transmits={transmits} but the suite file says {bool(decl.get('transmits', False))}")
        dropped = [r for r in decl.get("requires", []) if r not in requires]
        if dropped:
            errs.append(f"the manifest dropped hardware the suite file requires: {dropped}")
    device_errs, by_id = _device_errors(hw.get("devices", []), hw.get("hub"))
    errs.extend(device_errs)
    for req in requires:
        kind, _, n = req.partition(":")
        have = (1 if hw.get("hub") else 0) if kind == "hub" else sum(1 for d in by_id.values() if d.get("kind") == kind)
        if have < (int(n) if n.isdigit() else 1):
            errs.append(f"the suite requires {req} but the manifest lists {have}")
    errs.extend(_rf_errors(hw.get("rf"), by_id, transmits))
    rec_errs, burst_rows = _recording_errors(b, m, recs, by_id)
    errs.extend(rec_errs)
    meas_errs, n_meas = _measurement_errors(b, decl)
    errs.extend(meas_errs)
    sim_ids = sorted(i for i, d in by_id.items() if d.get("backend") == "sim")
    if b.require_hardware and (sim_ids or not by_id):
        errs.append("real hardware was required but " + (f"these devices were simulated: {sim_ids}" if sim_ids else "no device is on record"))
    if errs:
        raise Fail("; ".join(errs[:6]) + (f" (and {len(errs) - 6} more)" if len(errs) > 6 else ""), errors=errs, bursts=burst_rows)
    if not (by_id or recs or requires or transmits or n_meas):
        return {"reason": "no hardware, recordings or measurements in this run", "details": {}}
    bits = [f"{len(by_id)} device(s)" + (f" ({len(sim_ids)} SIMULATED)" if sim_ids else "")]
    if transmits:
        bits.append(f"RF {hw['rf']['setup']}, {len(hw['rf']['paths'])} path(s) at or above the minimum loss")
    if recs:
        bits.append(f"{len(recs)} SigMF recording(s) listed, hashed and valid")
    if burst_rows:
        bits.append(f"bursts line up with airtime in {len(burst_rows)} ({sum(r['matched'] for r in burst_rows)} frames)")
    if n_meas:
        bits.append(f"{n_meas} measurement(s) inside the suite's limits")
    return {"reason": "; ".join(bits), "details": {"simulated": sim_ids, "bursts": burst_rows}}


CHECKS: list[tuple[int, str, Callable[[Bundle], dict]]] = [
    (1, "integrity", check_integrity),
    (2, "manifest", check_manifest),
    (3, "results_match_log", check_results),
    (4, "no_readable_payload", check_payload),
    (5, "numbers_make_sense", check_numbers),
    (6, "known_bad_cases_ran", check_expectations),
    (7, "radio_evidence", check_radio),
]


def check_bundle(bundle: Path | str, suites_dir: Path | str | None = None, require_hardware: bool = False) -> dict:
    root = Path(bundle)
    b = Bundle(root, Path(suites_dir) if suites_dir else None, require_hardware)
    results = []
    for num, name, fn in CHECKS:
        try:
            out = fn(b)
            results.append({"id": num, "name": name, "status": "pass", "reason": out["reason"],
                            "details": out.get("details", {})})
        except Fail as f:
            results.append({"id": num, "name": name, "status": "fail", "reason": f.reason, "details": f.details})
        except Exception as exc:  # a broken bundle must fail, never crash the checker
            results.append({"id": num, "name": name, "status": "fail",
                            "reason": f"checker could not read the bundle: {type(exc).__name__}: {exc}", "details": {}})
    sums = root / "SHA256SUMS"
    return {
        "checker": {"name": "oasa-lab-checker", "version": CHECKER_VERSION},
        "bundle": root.resolve().name,
        "sha256sums_sha256": sha256_file(sums) if sums.is_file() else None,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "suite_declaration": str(b.suites_dir) if b.suites_dir else None,
        "ok": all(r["status"] == "pass" for r in results),
        "checks": results,
    }


def write_check(bundle: Path | str, result: dict) -> Path:
    """check.json goes BESIDE the bundle: <bundle>.check.json. It is a verdict about the hashed
    set, not part of it, and it records the hash of SHA256SUMS so it can't be paired with the
    wrong bundle."""
    root = Path(bundle).resolve()
    out = root.parent / f"{root.name}.check.json"
    out.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    return out


def format_result(result: dict) -> str:
    lines = [f"checker {result['checker']['version']}: {'PASS' if result['ok'] else 'FAIL'} ({result['bundle']})"]
    for c in result["checks"]:
        lines.append(f"  {c['id']}. {c['name']}: {c['status'].upper()}: {c['reason']}")
    return "\n".join(lines)
