#!/usr/bin/env python3
"""A public summary of one counted run: `python3 tools/result_summary.py BUNDLE.tar.gz > docs/results/NAME.md`.

Reads the bundle and the checker verdict that the job service's download puts beside it. Copies only an
allow-list of fields (suite, job, commit, binary hashes, device kind/model/firmware, wiring and path losses,
test outcomes, checker reasons, measurements). Serial numbers, addresses and ports never get copied; as a
second guard the output is refused if it contains any device's serial or address, or anything shaped like an
IPv4 address. Standard library only.
"""

from __future__ import annotations

import json
import re
import sys
import tarfile

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def load(path: str) -> tuple[dict, dict, list[dict], dict]:
    with tarfile.open(path, "r:gz") as tf:
        names = tf.getnames()

        def read(suffix: str) -> bytes:
            hits = [n for n in names if n.endswith(suffix)]
            if len(hits) != 1:
                sys.exit(f"{path}: expected one {suffix}, found {hits}")
            f = tf.extractfile(hits[0])
            assert f is not None
            return f.read()

        manifest = json.loads(read("/manifest.json"))
        check = json.loads(read(".check.json"))
        log = [json.loads(x) for x in read("/log.jsonl").decode().splitlines() if x.strip()]
        ctrf = json.loads(read("/results.ctrf.json"))
    return manifest, check, log, ctrf


def secrets(manifest: dict) -> set[str]:
    """Every device value that must never be published (serials, addresses, ports, hub ports)."""
    out = set()
    for d in (manifest.get("hardware") or {}).get("devices", []):
        for k in ("serial", "addr", "port", "hub_port", "device_args", "usb"):
            v = d.get(k)
            if isinstance(v, (str, int)) and str(v):
                out.add(str(v))
    return {s for s in out if len(s) >= 4}


def cell(x) -> str:
    return str(x).replace("|", "\\|").replace("\n", " ")


def render(manifest: dict, check: dict, log: list[dict], ctrf: dict) -> str:
    job = manifest.get("job") or {}
    lab = manifest.get("lab") or {}
    hw = manifest.get("hardware") or {}
    rf = hw.get("rf") or {}
    tests = (ctrf.get("results") or {}).get("tests", [])
    summary = (ctrf.get("results") or {}).get("summary", {})
    counted = bool(check.get("ok")) and summary.get("failed", 1) == 0 and summary.get("passed", 0) > 0
    L = [f"# {manifest['suite']}, job {str(job.get('id', '?'))[:12]}", ""]
    L.append(f"- **Result:** {summary.get('passed', 0)} passed, {summary.get('failed', 0)} failed, "
             f"{summary.get('skipped', 0)} skipped; checker {(check.get('checker') or {}).get('version', '?') if isinstance(check.get('checker'), dict) else check.get('checker', '?')} "
             f"**{'PASS' if check.get('ok') else 'FAIL'}**; {'**counted**' if counted else 'not counted'}")
    L.append(f"- **When:** {manifest.get('start')} to {manifest.get('end')} (UTC)")
    L.append(f"- **oasa-lab:** `{lab.get('git_sha')}`{' (dirty tree)' if lab.get('git_dirty') else ''}, "
             f"seed {manifest.get('seed')}, run id `{manifest.get('run_id')}`")
    if job:
        L.append(f"- **Job service:** rf_setup `{job.get('rf_setup')}`, wiring confirmed `{job.get('wiring_confirmed')}`")
    L.append(f"- **What it proves:** {manifest.get('description', '')}")
    L.append("")
    if manifest.get("binaries"):
        L += ["## Binaries", "", "| Name | SHA-256 | Version |", "|---|---|---|"]
        for b in manifest["binaries"]:
            L.append(f"| `{b.get('name')}` | `{b.get('sha256')}` | {cell(b.get('version') or '')} |")
        L.append("")
    if hw.get("devices"):
        L += ["## Hardware", "", "| Device | Kind | Backend | Model | Firmware |", "|---|---|---|---|---|"]
        for d in hw["devices"]:
            L.append(f"| {d.get('id')} | {d.get('kind')} | {d.get('backend')} | {cell(d.get('model') or '')} | "
                     f"{cell(d.get('firmware') or '')} |")
        L.append("")
        if rf:
            L.append(f"RF setup **{rf.get('setup')}**, wiring `{rf.get('wiring')}`.")
            for p in rf.get("paths") or []:
                L.append(f"- {p.get('name')}: {p.get('from')} to {p.get('to')}, {p.get('loss_db')} dB")
            if rf.get("antennas"):
                L.append(f"- antennas: {', '.join(rf['antennas'])}; TX caps {rf.get('antenna_caps')}")
            L.append("")
    L += ["## Tests", "", "| Test | Result |", "|---|---|"]
    for t in tests:
        L.append(f"| `{t.get('name')}` | {t.get('status')} |")
    L += ["", "## Checker", "", "| Check | Result | Reason |", "|---|---|---|"]
    for c in check.get("checks", []):
        L.append(f"| {c.get('name')} | {c.get('status')} | {cell(c.get('reason', ''))} |")
    meas = [e for e in log if e.get("event") == "measurement"]
    if meas:
        L += ["", "## Measurements", "", "| Metric | Name | Value | Unit | Limits | OK |", "|---|---|---|---|---|---|"]
        for m in meas:
            L.append(f"| `{m.get('metric')}` | {cell(m.get('name'))} | {m.get('value')} | {m.get('unit') or ''} | "
                     f"{m.get('min')} to {m.get('max')} | {'yes' if m.get('ok') else 'NO'} |")
    recs = manifest.get("recordings") or []
    if recs:
        L += ["", f"## Recordings", "", f"{len(recs)} SigMF recording(s), hashed in the bundle (kept in the lab's evidence store)."]
    L += ["", "Generated by `tools/result_summary.py` from the bundle and its checker verdict. Serial numbers and "
          "addresses are left out on purpose.", ""]
    return "\n".join(L)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    manifest, check, log, ctrf = load(argv[1])
    text = render(manifest, check, log, ctrf)
    leaks = [s for s in secrets(manifest) if s in text]
    if leaks or IPV4.search(text):
        print(f"REFUSED: the summary would contain a serial or address ({len(leaks)} device value(s), "
              f"ipv4={bool(IPV4.search(text))})", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
