"""Print the crowd tables (docs/crowd-sim.md) from an l1-crowd bundle.

    python3 tools/crowd_table.py runs/<bundle>

Reads the crowd_* lines of the bundle's log.jsonl. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CALLS = (0, 1, 2, 4)


def load(bundle: Path) -> tuple[dict, dict[str, list[dict]]]:
    manifest = json.loads((bundle / "manifest.json").read_text())
    ev: dict[str, list[dict]] = {}
    for line in (bundle / "log.jsonl").read_text().splitlines():
        r = json.loads(line)
        test = r.get("test", "")
        e = r.get("event", "")
        if e == "crowd_case" and not test.endswith("::test_text_sweep"):
            continue
        if e == "crowd_voice" and not test.endswith("::test_voice_channel"):
            continue
        if e.startswith("crowd_"):
            ev.setdefault(e, []).append(r)
    return manifest, ev


def mode(lbt: bool) -> str:
    return "LBT" if lbt else "blind"


def largest(rows: list[dict]) -> str:
    settings = list(dict.fromkeys(r["profile"] for r in rows))
    traffic = list(dict.fromkeys(r["traffic"] for r in rows))
    by = {(r["profile"], r["traffic"], r["lbt"], r["calls"], r["listen"]): r for r in rows}

    def fmt(r: dict | None) -> str:
        if r is None:
            return "–"
        if not r["largest_n"]:
            return "none"
        low = r.get("failing_below") or []
        return str(r["largest_n"]) + (f" (not {', '.join(map(str, low))})" if low else "")

    cols = [(lbt, c) for lbt in (False, True) for c in CALLS]
    out = ["| Setting | Traffic | " + " | ".join(f"{mode(lbt)}, {c} call{'' if c == 1 else 's'}"
                                             for lbt, c in cols) + " |",
           "|---" * (len(cols) + 2) + "|"]
    for s in settings:
        for t in traffic:
            cells = []
            for lbt, c in cols:
                cell = fmt(by.get((s, t, lbt, c, "one")))
                two = by.get((s, t, lbt, c, "two"))
                if two is not None:
                    cell += f"; two receivers: {fmt(two)}"
                cells.append(cell)
            out.append(f"| `{s}` | {t} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def cell(r: dict) -> str:
    d = f"{r['text_delivery'] * 100:.1f} % · {r['text_delay_p95_s']:.1f} s · pos {r['pos_delivery'] * 100:.0f} %"
    return d if r["meets_mark"] else f"**{d}**"


def cases(rows: list[dict], calls: int, listen: str = "one") -> str:
    rows = [r for r in rows if r["calls"] == calls and r["listen"] == listen]
    ns = sorted({r["n"] for r in rows})
    by = {(r["profile"], r["traffic"], r["lbt"], r["n"]): r for r in rows}
    keys = list(dict.fromkeys((r["profile"], r["traffic"], r["lbt"]) for r in rows))
    out = ["| Setting | Traffic | Access | " + " | ".join(f"N = {n}" for n in ns) + " |",
           "|---" * (len(ns) + 3) + "|"]
    for s, t, lbt in keys:
        out.append(f"| `{s}` | {t} | {mode(lbt)} | " + " | ".join(cell(by[(s, t, lbt, n)]) for n in ns) + " |")
    return "\n".join(out)


def offered(rows: list[dict]) -> str:
    rows = [r for r in rows if r["calls"] == 0 and r["lbt"] is False]
    ns = sorted({r["n"] for r in rows})
    by = {(r["profile"], r["traffic"], r["n"]): r for r in rows}
    keys = list(dict.fromkeys((r["profile"], r["traffic"]) for r in rows))
    out = ["| Setting | Traffic | Airtime text / position / ACK | " + " | ".join(f"N = {n}" for n in ns) + " |",
           "|---" * (len(ns) + 3) + "|"]
    for s, t in keys:
        a = by[(s, t, ns[0])]["airtime_ms"]
        out.append(f"| `{s}` | {t} | {a['text']:.0f} / {a['pos']:.0f} / {a['ack']:.0f} ms | "
                   + " | ".join(f"{by[(s, t, n)]['offered'] * 100:.0f} %" for n in ns) + " |")
    return "\n".join(out)


def calls_effect(rows: list[dict]) -> str:
    rows = [r for r in rows if r["traffic"] == "quiet"]
    ns = sorted({r["n"] for r in rows})
    by = {(r["profile"], r["lbt"], r["calls"], r["listen"], r["n"]): r for r in rows}
    out = ["| Setting | Access | Calls | " + " | ".join(f"N = {n}" for n in ns) + " |",
           "|---" * (len(ns) + 3) + "|"]
    for s in dict.fromkeys(r["profile"] for r in rows):
        for lbt in (False, True):
            for c, listen in [(c, "one") for c in CALLS] + [(4, "two")]:
                cells = []
                for n in ns:
                    r = by.get((s, lbt, c, listen, n))
                    if r is None:
                        cells.append("–")
                        continue
                    x = f"{r['text_delivery'] * 100:.1f} %"
                    if c:
                        x += f", in call {r['in_call_share'] * 100:.0f} %, setup {r['setup_acked']}/{r['setups']}"
                    cells.append(x if r["meets_mark"] else f"**{x}**")
                label = f"{c}" + (" (two receivers)" if listen == "two" else "")
                out.append(f"| `{s}` | {mode(lbt)} | {label} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def voice(rows: list[dict]) -> str:
    cols = [(rel, lbt) for rel in ("direct", "relayed") for lbt in (False, True)]
    by = {(r["profile"], r["mode"], r["lbt"], r["calls"]): r for r in rows}
    out = ["| Setting | Conversations | " + " | ".join(f"{rel}, {mode(lbt)}" for rel, lbt in cols) + " |",
           "|---" * (len(cols) + 2) + "|"]
    for s in dict.fromkeys(r["profile"] for r in rows):
        for c in sorted({r["calls"] for r in rows}):
            cells = []
            for rel, lbt in cols:
                r = by[(s, rel, lbt, c)]
                x = (f"{r['offered'] * 100:.0f} % · {r['delivery'] * 100:.1f} % · "
                     f"{r['delay_mean_ms']:.0f} / {r['delay_p95_ms']:.0f} ms")
                cells.append(x if r["meets_plan"] else f"**{x}**")
            out.append(f"| `{s}` | {c} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def alternatives(alt: list[dict], base: list[dict]) -> str:
    rows = [r for r in base if r["traffic"] == "quiet" and r["calls"] == 0] + alt
    ns = sorted({r["n"] for r in rows})

    def label(r: dict) -> str:
        parts = [mode(r["lbt"])]
        if r["capture"]:
            parts.append("capture")
        if r["hidden"]:
            parts.append(f"hidden carriers {r['hidden'] * 100:.0f} %")
        return ", ".join(parts) if len(parts) > 1 else parts[0] + ", any overlap (main)"

    by = {(r["profile"], label(r), r["n"]): r for r in rows}
    order = list(dict.fromkeys(r["profile"] for r in base))
    keys = sorted({(r["profile"], label(r)) for r in rows}, key=lambda k: (order.index(k[0]), k[1]))
    out = ["| Setting | Rule | " + " | ".join(f"N = {n}" for n in ns) + " |", "|---" * (len(ns) + 2) + "|"]
    for s, lab in keys:
        out.append(f"| `{s}` | {lab} | " + " | ".join(cell(by[(s, lab, n)]) for n in ns) + " |")
    return "\n".join(out)


def ratios(rows: list[dict]) -> str:
    out = ["| Setting | Traffic | N | Offered | Clean airtime blind / LBT | LBT ÷ blind |", "|---" * 6 + "|"]
    for r in rows:
        out.append(f"| `{r['profile']}` | {r['traffic']} | {r['n']} | {r['offered'] * 100:.0f} % | "
                   f"{r['clean_blind'] * 100:.1f} % / {r['clean_lbt'] * 100:.1f} % | {r['ratio']:.2f} |")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    bundle = Path(argv[0])
    m, ev = load(bundle)
    if not ev.get("crowd_case"):
        print(f"no crowd_case lines from test_text_sweep in {bundle}", file=sys.stderr)
        return 1
    base = ev["crowd_case"]
    print(f"Run `{m['run_id']}`, seed {m['seed']}, lab {m['lab']['git_sha'][:12]}, "
          f"{len(base) + len(ev.get('crowd_fine', []))} text cases, {len(ev.get('crowd_voice', []))} voice cases.\n")
    print("### Largest N with text delivery above 95 %\n")
    print(largest(ev["crowd_largest_n"]) + "\n")
    print("### Offered load (share of airtime asked for, before retries)\n")
    print(offered(base) + "\n")
    print("### Text and position, no calls\n")
    print("Cells: text delivery · 95th-percentile text delay · position delivery. **Bold**: text delivery 95 % "
          "or less.\n")
    print(cases(base, 0) + "\n")
    print("### Voice calls and the text setting (quiet traffic)\n")
    print(calls_effect(base) + "\n")
    print("### Voice conversations sharing one fast setting\n")
    print("Cells: offered airtime · voice packets delivered · mean / 95th-percentile frame delay. "
          "**Bold**: misses the plan (95 % delivered, under 1 s).\n")
    print(voice(ev["crowd_voice"]) + "\n")
    print("### Alternatives: capture rule and hidden carriers (quiet traffic, no calls)\n")
    print(alternatives(ev.get("crowd_alt", []), base) + "\n")
    print("### Listen-before-send against blind (sanity comparison)\n")
    print(ratios(ev.get("crowd_lbt_ratio", [])) + "\n")
    for c in CALLS[1:]:
        print(f"### All text cases with {c} call{'s' if c > 1 else ''} (one receiver)\n")
        print(cases(base, c) + "\n")
    print("### All text cases with 4 calls (two receivers)\n")
    print(cases(base, 4, "two"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
