"""tools/result_summary.py: public run summaries never carry serials or addresses."""

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "tools" / "result_summary.py"


def bundle(tmp_path: Path, description: str = "a test") -> Path:
    name = "20260101T000000Z-l2-x-00000000"
    manifest = {"suite": "l2-x", "description": description, "seed": 1, "run_id": "r" * 32,
                "start": "s", "end": "e", "lab": {"git_sha": "a" * 40, "git_dirty": False},
                "job": {"id": "b" * 32, "rf_setup": "cabled", "wiring_confirmed": "usrp"},
                "binaries": [{"name": "oasa-radio", "sha256": "c" * 64, "version": "oasa-radio 0.1"}],
                "hardware": {"devices": [{"id": "e3100", "kind": "e310", "backend": "hardware", "model": "E310",
                                          "serial": "SERIAL123", "addr": "lab-e310.local", "firmware": "mpm 5.3"}],
                             "rf": {"setup": "cabled", "wiring": "usrp", "paths": []}},
                "recordings": []}
    files = {
        f"{name}/manifest.json": json.dumps(manifest),
        f"{name}/log.jsonl": json.dumps({"event": "measurement", "metric": "m", "name": "n", "value": 1,
                                         "unit": "dB", "min": 0, "max": 2, "ok": True}) + "\n",
        f"{name}/results.ctrf.json": json.dumps({"results": {"summary": {"passed": 1, "failed": 0, "skipped": 0},
                                                             "tests": [{"name": "t", "status": "passed"}]}}),
        f"{name}.check.json": json.dumps({"checker": {"version": "1.2"}, "ok": True,
                                          "checks": [{"name": "integrity", "status": "pass", "reason": "ok"}]}),
    }
    out = tmp_path / "b.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        for n, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(n)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return out


def run(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(TOOL), str(path)], capture_output=True, text=True, timeout=30)


def test_summary_keeps_results_and_drops_serials(tmp_path):
    r = run(bundle(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "**counted**" in r.stdout and "`oasa-radio`" in r.stdout and "| `m` |" in r.stdout
    assert "SERIAL123" not in r.stdout and "lab-e310.local" not in r.stdout


def test_summary_refuses_an_address_or_serial_anywhere(tmp_path):
    for leak in ("bench at 10.1.2.3", "unit SERIAL123 on the bench"):
        r = run(bundle(tmp_path, description=leak))
        assert r.returncode == 1 and r.stdout == "" and "REFUSED" in r.stderr, leak
