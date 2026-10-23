"""`oasa-lab run`: run a suite, write the evidence bundle, run the checker."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from . import bundle, context
from .checker import check_bundle, write_check
from .context import RunContext
from .suites import Suite, find_suite, repo_root, suites_dir


@dataclass
class RunResult:
    bundle: Path
    pytest_exit: int
    counts: dict
    check: dict

    @property
    def ok(self) -> bool:
        return self.pytest_exit == 0 and self.check["ok"]


def run_suite(suite_name: str, seed: int = 0, out: Path | str = "runs", quiet: bool = False) -> RunResult:
    suite: Suite = find_suite(suite_name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(out)
    run_id = uuid.uuid4().hex
    run_dir = out / f"{stamp}-{suite.name}-{run_id[:8]}"
    run_dir.mkdir(parents=True)
    ctx = RunContext(suite, seed, run_dir, run_id)
    context.set_current(ctx)
    ctx.log.write("run_start", run_id=ctx.run_id, suite=suite.name, seed=seed,
                  description=suite.description, bundle=str(run_dir))
    args = [
        str(suite.path), f"--rootdir={repo_root()}",
        "-c", str(repo_root() / "pyproject.toml"),
        "-p", "oasa_lab.pytest_plugin", "-p", "no:cacheprovider", "--import-mode=importlib",
        "-o", f"junit_suite_name={suite.name}", "-o", "junit_family=xunit2",
        f"--junitxml={run_dir / 'junit.xml'}", f"--ctrf={run_dir / 'results.ctrf.json'}",
        "-q", "--no-header", "-rs",
    ]
    exit_code = int(pytest.main(args))
    ctrf = _read_counts(run_dir / "results.ctrf.json")
    ctx.log.write("run_end", run_id=ctx.run_id, pytest_exit=exit_code, **ctrf)
    ctx.log.close()
    bundle.write_manifest(ctx)
    if (run_dir / "results.ctrf.json").exists():
        bundle.write_summary(run_dir)
    bundle.write_sums(run_dir)
    context.set_current(None)
    check = check_bundle(run_dir, suites_dir=suites_dir())
    write_check(run_dir, check)
    return RunResult(run_dir, exit_code, ctrf, check)


def _read_counts(path: Path) -> dict:
    try:
        s = json.loads(path.read_text())["results"]["summary"]
        return {"total": s["tests"], "passed": s["passed"], "failed": s["failed"], "skipped": s["skipped"]}
    except Exception:
        return {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
