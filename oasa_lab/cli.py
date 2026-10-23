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
    if argv and argv[0] in ("serve", "token"):  # the remote job service (needs the `service` extra)
        from .service import server

        return server.serve(argv[1:]) if argv[0] == "serve" else server.token(argv[1:])
    p = argparse.ArgumentParser(prog="oasa-lab", description="OASA test lab.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a suite, write the evidence bundle, check it")
    r.add_argument("suite")
    r.add_argument("--seed", type=int, default=0, help="seed for every random choice (default 0)")
    r.add_argument("--out", default="runs", help="folder for bundles (default runs/)")
    r.add_argument("--confirm-wiring", metavar="NAME",
                   help="a suite that transmits on real hardware runs only when you name its wiring here "
                        "(the cables must match docs/hardware-suites.md)")
    c = sub.add_parser("check", help="run the independent checker on a bundle")
    c.add_argument("bundle")
    c.add_argument("--no-suite-file", action="store_true",
                   help="do not confirm expectations and capture flags against suites/<name>/suite.toml")
    c.add_argument("--json", action="store_true", help="print the full result as JSON")
    c.add_argument("--require-hardware", action="store_true",
                   help="fail if any device in the bundle was simulated (for runs that must be real)")
    sub.add_parser("list", help="list suites")
    d = sub.add_parser("devices", help="open every device in lab.toml and show what it reports")
    d.add_argument("--sim", action="store_true", help="use the simulated bench (same as OASA_LAB_SIM=1)")
    pr = sub.add_parser("profiles", help="list the radio profiles of the simulated channel")
    pr.add_argument("--bytes", type=int, default=64, help="packet size for the airtime column (default 64)")
    sub.add_parser("channel", help="run the simulated channel standalone (oasa-lab channel --help)")
    sub.add_parser("serve", help="run the remote job service and Claude connector (oasa-lab serve --help)")
    sub.add_parser("token", help="make an API token for the job service (oasa-lab token --help)")
    args = p.parse_args(argv)

    if args.cmd == "list":
        suites = list_suites()
        width = max((len(s.name) for s in suites), default=0)
        for s in suites:
            print(f"{s.name.ljust(width)}  {s.description}")
        return 0
    if args.cmd == "devices":
        return _devices(args.sim)
    if args.cmd == "profiles":
        from .profiles import format_table

        print(format_table(args.bytes))
        return 0
    if args.cmd == "check":
        import json

        from .checker import check_bundle, format_result, write_check

        bundle = Path(args.bundle)
        if not bundle.is_dir():
            print(f"oasa-lab: not a folder: {bundle}", file=sys.stderr)
            return 2
        result = check_bundle(bundle, None if args.no_suite_file else suites_dir(),
                              require_hardware=args.require_hardware)
        out = write_check(bundle, result)
        print(json.dumps(result, indent=2, default=str) if args.json else format_result(result))
        print(f"wrote {out}")
        return 0 if result["ok"] else 1
    if args.cmd == "run":
        from .checker import format_result
        from .runner import run_suite

        res = run_suite(args.suite, args.seed, args.out, confirm_wiring=args.confirm_wiring)
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


def _devices(sim: bool) -> int:
    """First thing to run when the gear arrives: does every device in lab.toml open, and what is it?"""
    from .devices import DeviceError, Inventory, InventoryError, SimWorld, make_device, make_hub

    try:
        inv = Inventory.load(sim=True if sim else None)
    except InventoryError as exc:
        print(f"oasa-lab: {exc}", file=sys.stderr)
        return 2
    print(f"inventory: {inv.source} ({inv.backend})")
    if not inv.devices:
        print("no devices. Copy lab.example.toml to lab.toml and edit it, or set OASA_LAB_SIM=1.")
        return 1
    world = SimWorld(0, inv.wiring) if any(inv.backend_of(i) == "sim" for i in inv.devices) else None
    hub = make_hub(inv.hub, sim=inv.backend == "sim")
    bad = 0
    if hub is not None:
        h = hub.info()
        print(f"  hub        {h['backend']:8} {h['model']} {h['serial']}")
    for dev_id in inv.devices:
        dev = make_device(dev_id, inv, hub, world)
        try:
            with dev:
                i = dev.info()
            print(f"  {dev_id:10} {i['backend']:8} {i['model']} | serial {i['serial']} | firmware {i['firmware']}")
        except DeviceError as exc:
            bad += 1
            print(f"  {dev_id:10} FAILED   {exc}")
    if world is not None:
        world.close()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
