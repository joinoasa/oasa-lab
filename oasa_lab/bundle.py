"""Write the evidence bundle's own files: manifest.json, summary.md, SHA256SUMS."""

from __future__ import annotations

import json
import platform
import sys
from importlib import metadata
from pathlib import Path

from . import __version__
from .channel import ChannelSettings
from .context import RunContext, git_info, sha256_file
from .jsonl import now_rfc3339
from .suites import repo_root

MANIFEST_SCHEMA = 1


def write_manifest(ctx: RunContext) -> dict:
    packages = {}
    for d in metadata.distributions():
        name = d.metadata["Name"]
        if name:
            packages[name.lower()] = d.version
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "run_id": ctx.run_id,
        "suite": ctx.suite.name,
        "description": ctx.suite.description,
        "seed": ctx.seed,
        "start": ctx.start,
        "end": now_rfc3339(),
        "lab": {"version": __version__, **git_info(repo_root())},
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation()},
        "os": {"system": platform.system(), "release": platform.release(),
               "machine": platform.machine(), "platform": platform.platform()},
        "packages": dict(sorted(packages.items())),
        "channels": ctx.channels,
        "captures": ctx.captures,
        "binaries": sorted(ctx.binaries.values(), key=lambda b: b["path"]),
        "expectations": list(ctx.suite.expectations),
        "markers": list(ctx.markers),
    }
    (ctx.run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def write_sums(run_dir: Path) -> None:
    """SHA256SUMS in `sha256sum` format over every other file in the bundle."""
    lines = []
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS":
            lines.append(f"{sha256_file(p)}  {p.relative_to(run_dir).as_posix()}")
    (run_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_summary(run_dir: Path) -> str:
    """summary.md, built from the other files in the bundle."""
    manifest = json.loads((run_dir / "manifest.json").read_text())
    ctrf = json.loads((run_dir / "results.ctrf.json").read_text())
    s = ctrf["results"]["summary"]
    tests = ctrf["results"]["tests"]
    stats = {}
    for line in (run_dir / "log.jsonl").read_text().splitlines():
        rec = json.loads(line)
        if rec["event"] == "channel_stats" and rec.get("final"):
            stats[rec["channel"]] = rec
    lab = manifest["lab"]
    out = [
        f"# Run {manifest['suite']}",
        "",
        f"- Result: **{s['passed']} passed, {s['failed']} failed, {s['skipped']} skipped** ({s['tests']} tests)",
        f"- Started {manifest['start']}, ended {manifest['end']}",
        f"- Seed {manifest['seed']}, run id `{manifest['run_id']}`",
        f"- oasa-lab {lab['version']} at `{lab['git_sha'][:12]}`{' (dirty)' if lab['git_dirty'] else ''}; "
        f"Python {manifest['python']['version']} on {manifest['os']['platform']}",
        "- The independent checker's verdict is in `<this folder>.check.json`, beside this folder "
        "(it is not part of the hashed bundle).",
        "",
        "## Tests",
        "",
        "| Test | Result | Note |",
        "|---|---|---|",
    ]
    for t in tests:
        note = (t.get("message") or "").replace("\n", " ").replace("|", "/")[:120]
        out.append(f"| `{t['name']}` | {t['status']} | {note} |")
    if manifest["channels"]:
        out += ["", "## Channels", "",
                "| Channel | Test | Settings | A to B (sent / dropped / forwarded) | B to A |", "|---|---|---|---|---|"]
        for c in manifest["channels"]:
            st = stats.get(c["id"], {})
            defaults = ChannelSettings().to_dict()
            cfg = ", ".join(f"{k}={v}" for k, v in c["settings"].items()
                            if k == "seed" or (v != defaults.get(k) and k != "profile"))
            if c["settings"].get("profile"):
                cfg = f"profile {c['settings']['profile']}, " + cfg
            fmt = lambda d: (f"{d['sent']} / {d['dropped']} / {d['forwarded']}" if d else "n/a")  # noqa: E731
            out.append(f"| {c['id']} | `{c['test'].split('::')[-1]}` | {cfg} | "
                       f"{fmt(st.get('a_to_b'))} | {fmt(st.get('b_to_a'))} |")
    if manifest["binaries"]:
        out += ["", "## Binaries", ""]
        out += [f"- `{b['name']}` {b.get('version') or ''} sha256 `{b['sha256'][:16]}...`" for b in manifest["binaries"]]
    out += ["", "## Files", ""]
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name not in ("SHA256SUMS", "summary.md"):
            rel = p.relative_to(run_dir).as_posix()
            out.append(f"- [{rel}]({rel})")
    out += ["- [SHA256SUMS](SHA256SUMS) (check with `sha256sum -c SHA256SUMS`)", ""]
    text = "\n".join(out)
    (run_dir / "summary.md").write_text(text, encoding="utf-8")
    return text
