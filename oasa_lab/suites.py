"""Find suites. A suite is a folder with a `suite.toml` and pytest tests.

suite.toml:
    description  = "one line"
    expectations = ["test_name", ...]   # known-bad cases that must exist and pass
    [captures]                          # applies to every capture the suite makes
    encrypted         = true|false      # the checker proves "no readable payload" if true
    expect_plaintext  = false           # negative control: the checker must FIND the markers
    expect_mavlink    = false           # negative control: the checker must find MAVLink frames
    skip_header_bytes = 0               # leading protocol bytes left out of the randomness test
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
