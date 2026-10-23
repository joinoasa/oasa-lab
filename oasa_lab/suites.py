"""Find suites. A suite is a folder with a `suite.toml` and pytest tests.

suite.toml:
    description  = "one line"
    expectations = ["test_name", ...]   # known-bad cases that must exist and pass
    [captures]                          # applies to every capture the suite makes
    encrypted         = true|false      # the checker proves "no readable payload" if true
    expect_plaintext  = false           # negative control: the checker must FIND the markers
    expect_mavlink    = false           # negative control: the checker must find MAVLink frames
    skip_header_bytes = 0               # leading protocol bytes left out of the randomness test
    [captures.control]                  # optional: flags for channels made with lab.channel(control=True),
    encrypted = false                   # e.g. a negative control that carries the same traffic without
    expect_mavlink = true               # the link, in a suite whose other captures are encrypted


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

    def capture_flags(self, control: bool = False) -> dict:
        """Flags for a capture. With control=True: the suite's `[captures.control]` table."""
        if control:
            if not isinstance(self.captures.get("control"), dict):
                raise ValueError(f"suite {self.name} has no [captures.control] table")
            c = self.captures["control"]
        else:
            c = self.captures
        return {
            "encrypted": bool(c.get("encrypted", False)),
            "expect_plaintext": bool(c.get("expect_plaintext", False)),
            "expect_mavlink": bool(c.get("expect_mavlink", False)),
            "skip_header_bytes": int(c.get("skip_header_bytes", 0)),
        }


def parse_suite(name: str, text: str, path: Path | None = None) -> Suite:
    """Parse the text of a suite.toml. The one parser: `oasa-lab run`, the job service and `job-run` all use it."""
    data = tomllib.loads(text)
    return Suite(
        name=name,
        path=path or Path(name),
        description=str(data.get("description", "")),
        expectations=[str(x) for x in data.get("expectations", [])],
        captures=dict(data.get("captures", {})),
        hidden=name.startswith("_"),
        transmits=bool(data.get("transmits", False)),
        transmits_declared=isinstance(data.get("transmits"), bool),
        requires=[str(x) for x in data.get("requires", [])],
        wiring=str(data.get("wiring", "")),
        limits={str(k): dict(v) for k, v in (data.get("limits") or {}).items()},
    )


def load_suite(path: Path) -> Suite:
    return parse_suite(path.name, (path / "suite.toml").read_text(encoding="utf-8"), path)


def suite_facts(s: Suite) -> dict:
    """What the job service and `job-run` need to decide whether a suite may run."""
    return {"name": s.name, "description": s.description, "transmits": s.transmits,
            "transmits_declared": s.transmits_declared, "wiring": s.wiring, "requires": list(s.requires)}


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
