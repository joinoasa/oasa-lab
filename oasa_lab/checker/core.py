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

from .pcapread import Packet, PcapError, parse_pcap
from .stats import binomial_bound, chi2_bytes, poisson_limit

CHECKER_VERSION = "1.0"
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

    def __init__(self, root: Path, suites_dir: Path | None):
        self.root = root
        self.suites_dir = suites_dir
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
        need(isinstance(c.get("endpoints"), dict), f"{c['id']}: endpoints missing")
    files: set[str] = set()
    for i, cap in enumerate(caps or []):
        if not isinstance(cap, dict):
            errs.append(f"captures[{i}] is not an object")
            continue
        f = cap.get("file")
        need(isinstance(f, str) and bool(CAPTURE_PATH.match(f)), f"captures[{i}].file must be captures/NAME.pcap")
        need(isinstance(f, str) and f not in files, f"captures[{i}].file is listed twice")
        files.add(str(f))
        need(cap.get("channel") in ids, f"captures[{i}] names an unknown channel")
        for k in ("encrypted", "expect_plaintext", "expect_mavlink"):
            need(isinstance(cap.get(k), bool), f"captures[{i}].{k} must be true or false")
        need(not (cap.get("encrypted") is True and (cap.get("expect_plaintext") is True or cap.get("expect_mavlink") is True)),
             f"captures[{i}]: a capture cannot be both encrypted and a plaintext control")
        sk = cap.get("skip_header_bytes")
        need(isinstance(sk, int) and not isinstance(sk, bool) and 0 <= sk <= 64, f"captures[{i}].skip_header_bytes must be 0 to 64")
        need(isinstance(cap.get("markers"), list) and all(x in (markers or []) for x in cap["markers"]),
             f"captures[{i}].markers must be a subset of markers")
    need(len(ids) == len(chans or []) and files == {c.get("capture") for c in chans or []},
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


def check_numbers(b: Bundle) -> dict:
    m = b.manifest()
    log = b.log()
    finals: dict[str, dict] = {}
    for r in log:
        if r["event"] == "channel_stats" and r.get("final") is True:
            if r.get("channel") in finals:
                raise Fail(f"two final channel_stats lines for {r.get('channel')}")
            finals[r.get("channel")] = r
    errs, rows, skipped = [], [], []
    declared = {c["id"] for c in m.get("channels", [])}
    for extra in sorted(set(finals) - declared):
        errs.append(f"log has channel_stats for {extra}, which the manifest does not list")
    for ch in m.get("channels", []):
        cid, s = ch["id"], ch["settings"]
        st = finals.get(cid)
        if st is None:
            errs.append(f"{cid}: no final channel_stats line in the log")
            continue
        cap = next(c for c in m["captures"] if c["channel"] == cid)
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
            if not isinstance(x, dict) or any(not isinstance(x.get(k), int) or x[k] < 0 for k in STAT_KEYS):
                errs.append(f"{cid} {d}: channel_stats incomplete")
                continue
            tag = f"{cid} {d}"
            expected_fwd = (x["sent"] - x["dropped"] - x["too_big"] - x["unrouted"]
                            + x["duplicated"] + x["replayed"] - x["abandoned"])
            if x["forwarded"] != expected_fwd:
                errs.append(f"{tag}: forwarded {x['forwarded']} but the counts add up to {expected_fwd}")
            if len(by_dir[d]) != x["forwarded"]:
                errs.append(f"{tag}: log says {x['forwarded']} forwarded, capture holds {len(by_dir[d])}")
            if x["abandoned"]:
                errs.append(f"{tag}: {x['abandoned']} packets were abandoned in flight")
            n_loss = x["sent"] - x["too_big"] - x["unrouted"]
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
    if errs:
        raise Fail("; ".join(errs[:6]) + (f" (and {len(errs) - 6} more)" if len(errs) > 6 else ""), errors=errs, rows=rows)
    if not m.get("channels"):
        return {"reason": "no channels in this run; nothing to check", "details": {}}
    return {"reason": f"{len(m['channels'])} channel(s): stats match the captures; {sum(r['result'] == 'ok' for r in rows)} "
                      f"rates within 5 sigma" + (f"; {len(skipped)} skipped (too few packets)" if skipped else ""),
            "details": {"rows": rows}}


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


CHECKS: list[tuple[int, str, Callable[[Bundle], dict]]] = [
    (1, "integrity", check_integrity),
    (2, "manifest", check_manifest),
    (3, "results_match_log", check_results),
    (4, "no_readable_payload", check_payload),
    (5, "numbers_make_sense", check_numbers),
    (6, "known_bad_cases_ran", check_expectations),
]


def check_bundle(bundle: Path | str, suites_dir: Path | str | None = None) -> dict:
    root = Path(bundle)
    b = Bundle(root, Path(suites_dir) if suites_dir else None)
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
