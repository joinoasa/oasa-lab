"""Find suites. A suite is a folder with a `suite.toml` and pytest tests.

suite.toml:
    description  = "one line"
    expectations = ["test_name", ...]   # known-bad cases that must exist and pass
    [captures]                          # applies to every capture the suite makes
    encrypted         = true|false      # the checker proves "no readable payload" if true
    expect_plaintext  = false           # negative control: the checker must FIND the markers
    expect_mavlink    = false           # negative control: the checker must find MAVLink frames
    skip_header_bytes = 0               # leading protocol bytes left out of the randomness test

Hardware suites (L2) add:
    transmits = true                    # the suite puts RF on a cable; the checker then needs wiring with 60 dB or more
                                        # (a suite with `requires` must say transmits = true or false; on real hardware it is refused otherwise)
    requires  = ["pluto", "rtlsdr", "serial-radio:2"]   # devices from lab.toml; missing ones skip the suite
    wiring    = "pluto"                 # the [wiring.NAME] of lab.toml the cables must match
    [limits.METRIC]                     # pass/fail limits of measurements; the checker re-applies them
    min = 0.6
    max = 1.05
    optional = true                     # may stay unmeasured (for example, needs a calibration); never loosens the limit
    reason = "why these numbers"
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def suites_dir() -> Path:
    env = os.environ.get("OASA_LAB_SUITES")
    return Path(env).resolve() if env else repo_root() / "suites"


@dataclass
class Suite:
    name: str
    path: Path
    description: str
    expectations: list[str] = field(default_factory=list)
    captures: dict = field(default_factory=dict)
    hidden: bool = False
    transmits: bool = False
    transmits_declared: bool = False   # suite.toml says transmits = true|false in so many words
    requires: list[str] = field(default_factory=list)
    wiring: str = ""
    limits: dict = field(default_factory=dict)

    def capture_flags(self) -> dict:
        c = self.captures
        return {
            "encrypted": bool(c.get("encrypted", False)),
            "expect_plaintext": bool(c.get("expect_plaintext", False)),
            "expect_mavlink": bool(c.get("expect_mavlink", False)),
            "skip_header_bytes": int(c.get("skip_header_bytes", 0)),
        }


def load_suite(path: Path) -> Suite:
    data = tomllib.loads((path / "suite.toml").read_text(encoding="utf-8"))
    return Suite(
        name=path.name,
        path=path,
        description=str(data.get("description", "")),
        expectations=[str(x) for x in data.get("expectations", [])],
        captures=dict(data.get("captures", {})),
        hidden=path.name.startswith("_"),
        transmits=bool(data.get("transmits", False)),
        transmits_declared=isinstance(data.get("transmits"), bool),
        requires=[str(x) for x in data.get("requires", [])],
        wiring=str(data.get("wiring", "")),
        limits={str(k): dict(v) for k, v in (data.get("limits") or {}).items()},
    )


def list_suites(include_hidden: bool = False) -> list[Suite]:
    root = suites_dir()
    out = [load_suite(p) for p in sorted(root.iterdir()) if (p / "suite.toml").is_file()] if root.is_dir() else []
    return [s for s in out if include_hidden or not s.hidden]


def find_suite(name: str) -> Suite:
    path = suites_dir() / name
    if not (path / "suite.toml").is_file():
        known = ", ".join(s.name for s in list_suites()) or "none"
        raise SystemExit(f"oasa-lab: no suite named {name!r}. Suites: {known}")
    return load_suite(path)


def main(argv: list[str] | None = None) -> int:
    """`python -I oasa_lab/suites.py --json [--suites-dir DIR]`: what the job service reads from a checkout.

    Standard library only, and it reads files without running anything. A suite whose suite.toml does not
    parse is listed with an `error`, never skipped silently."""
    import argparse
    import json

    ap = argparse.ArgumentParser(description="List the suites as JSON.")
    ap.add_argument("--json", action="store_true", required=True)
    ap.add_argument("--suites-dir")
    args = ap.parse_args(argv)
    root = Path(args.suites_dir).resolve() if args.suites_dir else suites_dir()
    out = []
    for p in sorted(root.iterdir()) if root.is_dir() else []:
        if p.name.startswith("_") or p.is_symlink() or not (p / "suite.toml").is_file():
            continue
        try:
            s = load_suite(p)
        except (OSError, ValueError) as exc:
            out.append({"name": p.name, "error": str(exc)[:200]})
            continue
        out.append({"name": s.name, "description": s.description, "transmits": s.transmits,
                    "transmits_declared": s.transmits_declared, "requires": s.requires, "wiring": s.wiring})
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
