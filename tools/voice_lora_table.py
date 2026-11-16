"""Print the voice-over-LoRa tables (docs/voice-lora-sim.md) from an l1-voice-lora bundle.

    python3 tools/voice_lora_table.py runs/<bundle>

Reads the voice_case lines of test_voice_sweep from the bundle's log.jsonl. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load(bundle: Path) -> tuple[dict, list[dict]]:
    rows, manifest = [], json.loads((bundle / "manifest.json").read_text())
    for line in (bundle / "log.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r.get("event") == "voice_case" and r.get("test", "").endswith("::test_voice_sweep"):
            rows.append(r)
    return manifest, rows


def cell(r: dict) -> str:
    occ = f"{r['occupancy'] * 100:.0f} %"
    if r["carries"]:
        return f"{occ} · {r['delay_mean_ms']:.0f} / {r['delay_p95_ms']:.0f} ms"
    return f"**fail** {occ} · {r['delivery'] * 100:.0f} % · {r['delay_p95_ms']:.0f} ms"


def tables(rows: list[dict]) -> str:
    settings = list(dict.fromkeys(r["profile"] for r in rows))
    rates = sorted({r["rate_bps"] for r in rows})
    by = {(r["profile"], r["rate_bps"], r["interval_ms"], r["mode"]): r for r in rows}
    cols = [(rate, mode) for rate in rates for mode in ("direct", "relayed")]
    out = []
    for iv in sorted({r["interval_ms"] for r in rows}):
        sizes = ", ".join(f"{rate}: {by[(settings[0], rate, iv, 'direct')]['packet_bytes']} B"
                          for rate in rates)
        out.append(f"#### Packet every {iv} ms (packet sizes {sizes})\n")
        out.append("| Setting | " + " | ".join(f"{rate} {mode}" for rate, mode in cols) + " |")
        out.append("|---" * (len(cols) + 1) + "|")
        for s in settings:
            out.append(f"| `{s}` | " + " | ".join(cell(by[(s, rate, iv, mode)]) for rate, mode in cols) + " |")
        out.append("")
    return "\n".join(out)


def best(rows: list[dict]) -> str:
    """Per setting and rate: the lowest-delay packet interval that meets the plan, direct and relayed."""
    settings = list(dict.fromkeys(r["profile"] for r in rows))
    rates = sorted({r["rate_bps"] for r in rows})
    out = ["| Setting | " + " | ".join(f"{rate} {m}" for rate in rates for m in ("direct", "relayed")) + " |",
           "|---" * (2 * len(rates) + 1) + "|"]
    for s in settings:
        cells = []
        for rate in rates:
            for mode in ("direct", "relayed"):
                ok = sorted((r for r in rows if r["profile"] == s and r["rate_bps"] == rate and r["mode"] == mode
                             and r["meets_plan"]), key=lambda r: r["delay_mean_ms"])
                cells.append("no" if not ok else
                             f"{ok[0]['interval_ms']} ms: {ok[0]['occupancy'] * 100:.0f} %, "
                             f"{ok[0]['delay_mean_ms']:.0f} / {ok[0]['delay_p95_ms']:.0f} ms")
        out.append(f"| `{s}` | " + " | ".join(cells) + " |")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    bundle = Path(argv[0])
    m, rows = load(bundle)
    if not rows:
        print(f"no voice_case lines from test_voice_sweep in {bundle}", file=sys.stderr)
        return 1
    print(f"Run `{m['run_id']}`, seed {m['seed']}, lab {m['lab']['git_sha'][:12]}, {len(rows)} cases.\n")
    print("### Meets the plan: lowest delay\n")
    print(best(rows) + "\n")
    edge = [r for r in rows if not r["carries"] and r["plan_delivery_ok"] and r["plan_delay_ok"]]
    if edge:
        print("Over 100 % occupancy but inside the plan's delivery and delay marks over one 30 s spurt "
              "(the queue overflows; a longer spurt loses more):\n")
        for r in edge:
            print(f"- `{r['case']}`: {r['occupancy'] * 100:.0f} %, {r['delivery'] * 100:.1f} % delivered, "
                  f"p95 {r['delay_p95_ms']:.0f} ms")
        print()
    print("### All cases\n")
    print(tables(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
