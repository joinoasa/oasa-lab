"""The `oasa-lab` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .suites import list_suites, suites_dir


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "channel":  # has its own long option list
        from .channel import main as channel_main

        return channel_main(argv[1:])
    p = argparse.ArgumentParser(prog="oasa-lab", description="OASA test lab.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a suite, write the evidence bundle, check it")
    r.add_argument("suite")
    r.add_argument("--seed", type=int, default=0, help="seed for every random choice (default 0)")
    r.add_argument("--out", default="runs", help="folder for bundles (default runs/)")
    c = sub.add_parser("check", help="run the independent checker on a bundle")
    c.add_argument("bundle")
    c.add_argument("--no-suite-file", action="store_true",
                   help="do not confirm expectations and capture flags against suites/<name>/suite.toml")
    c.add_argument("--json", action="store_true", help="print the full result as JSON")
    sub.add_parser("list", help="list suites")
    sub.add_parser("channel", help="run the simulated channel standalone (oasa-lab channel --help)")
    args = p.parse_args(argv)

    if args.cmd == "list":
        suites = list_suites()
        width = max((len(s.name) for s in suites), default=0)
        for s in suites:
            print(f"{s.name.ljust(width)}  {s.description}")
        return 0
    if args.cmd == "check":
        import json

        from .checker import check_bundle, format_result, write_check

        bundle = Path(args.bundle)
        if not bundle.is_dir():
            print(f"oasa-lab: not a folder: {bundle}", file=sys.stderr)
            return 2
        result = check_bundle(bundle, None if args.no_suite_file else suites_dir())
        out = write_check(bundle, result)
        print(json.dumps(result, indent=2, default=str) if args.json else format_result(result))
        print(f"wrote {out}")
        return 0 if result["ok"] else 1
    if args.cmd == "run":
        from .checker import format_result
        from .runner import run_suite

        res = run_suite(args.suite, args.seed, args.out)
        c = res.counts
        print()
        print(f"tests:   {c['passed']} passed, {c['failed']} failed, {c['skipped']} skipped")
        print(format_result(res.check))
        print(f"bundle:  {res.bundle}")
        print(f"verdict: {res.bundle.parent / (res.bundle.name + '.check.json')}")
        if c["skipped"] and not c["passed"] and not c["failed"]:
            print("note:    every test was skipped (see the reasons above). Nothing was verified.")
        if res.pytest_exit != 0 and res.check["ok"]:
            print(f"pytest exited with {res.pytest_exit}")
        return 0 if res.ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
