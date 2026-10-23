"""python -m oasa_lab.checker BUNDLE_DIR [--suites-dir DIR | --no-suite-file] [--out FILE]"""

import argparse
import json
import sys
from pathlib import Path

from .core import check_bundle, format_result, write_check


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m oasa_lab.checker", description="Re-verify an evidence bundle.")
    p.add_argument("bundle")
    p.add_argument("--suites-dir", help="folder with <suite>/suite.toml to confirm expectations and capture flags against")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = p.parse_args(argv)
    bundle = Path(args.bundle)
    if not bundle.is_dir():
        print(f"not a folder: {bundle}", file=sys.stderr)
        return 2
    result = check_bundle(bundle, args.suites_dir)
    out = write_check(bundle, result)
    print(json.dumps(result, indent=2, default=str) if args.json else format_result(result))
    print(f"wrote {out}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
