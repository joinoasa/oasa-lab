"""R-COM1: an off-the-shelf SiK pair on cable, through oasa-radio bridge at each end.

    app -> bridge A -> pty -> tap -> SiK A ~~ cable ~~ SiK B -> tap -> pty -> bridge B -> app

Needs `oasa-radio` in $OASA_RADIO_BIN_DIR with `sik` and `--sik-expect` (oasa-radio's settings check, H1).
Without it the tests are SKIPPED with the reason, and a skipped suite does not pass the checker.

What it shows:
  - both radios report the settings lab.toml declares (ATI5, read at open) and can link: same NETID and
    air settings, MAVLINK framing off, a NETID that is not the default;
  - packets go both ways through the bridges unchanged, once and in order; delivery and latency against
    the sik-64k model; a burst against the serial and air rate; the MTU holds;
  - known-bad: the bridge refuses a radio whose settings differ from what it expects, and after another
    radio changed ours over the air with RT commands (H1), and nothing is bridged.

No encryption here (that is l2-sik-oasa-link): the packets are random test bytes.
"""

import json
import statistics
import subprocess
import time

import pytest

from oasa_lab.devices.sikat import expect_arg
from oasa_lab.hwlink import find_radio_bin
from oasa_lab.linkharness import HOST, MissingBinaries
from oasa_lab.siklink import SikBench, lower_setting, settings_problems
from oasa_lab.testing import Endpoint, free_udp_port, wait_for

N = 30          # packets each way, one at a time
BURST = 40      # MTU packets back to back, A to B


@pytest.fixture
def radio(lab):
    try:
        exe = find_radio_bin(lab)
    except MissingBinaries as exc:
        pytest.skip(str(exc))
    r = subprocess.run([str(exe), "sik", "--help"], capture_output=True, text=True, timeout=10)
    if "--sik-expect" not in r.stdout:
        pytest.skip("this oasa-radio has no SiK settings check (sik, --sik-expect); build oasa-radio with it")
    return exe


def test_radios_report_their_settings(lab, hw):
    bench = SikBench(lab, hw)
    a, b = bench.radios()
    for r in (a, b):
        lab.log("sik_settings", device=r.id, firmware=r.report.version, settings=r.settings, expect=r.expect)
    problems = settings_problems(a, b)
    assert not problems, "; ".join(problems)


def _app_ports():
    return (HOST, free_udp_port()), (HOST, free_udp_port())


def test_bytes_both_ways(lab, hw, radio):
    bench = SikBench(lab, hw)
    mtu = bench.a.mtu
    assert bench.b.mtu == mtu, "both radios need the same mtu in lab.toml"
    app = {"a": Endpoint(), "b": Endpoint()}
    listen = {"a": (HOST, free_udp_port()), "b": (HOST, free_udp_port())}
    br = {s: bench.bridge(radio, r, listen=listen[s], send=app[s].addr, log_name=f"bridge-{s}.jsonl")
          for s, r in (("a", bench.a), ("b", bench.b))}
    failures: list[str] = []

    def check(metric: str, name: str, value: float, unit: str = "") -> None:
        if not lab.measure(metric, name, value, unit):
            failures.append(f"{name} = {value:g} {unit} is outside its limits (see suite.toml)")

    try:
        for s in ("a", "b"):
            br[s].wait_start()
            chk = br[s].seen("sik_check")
            assert chk and chk[-1].get("ok") is True, f"bridge {s}: settings check {chk}"
        time.sleep(0.5)
        for e in app.values():
            e.received.clear()

        excess_all = []
        for src, dst, name in (("a", "b", "a_to_b"), ("b", "a", "b_to_a")):
            sent, t_sent = [], {}
            for i in range(N):
                size = lab.rng.randint(16, mtu)
                p = i.to_bytes(4, "big") + bytes([1 if src == "a" else 2]) + lab.rng.randbytes(size - 5)
                sent.append(p)
                t_sent[p] = time.monotonic()
                app[src].send(p, listen[src])
                time.sleep(2.5 * bench.latency_s(size) + 0.05)
            app[dst].wait_idle(1.0, 30)
            got = [(t, d) for t, d in app[dst].received]
            app[dst].received.clear()
            payloads = [d for _, d in got]
            if any(d not in t_sent for d in payloads):
                failures.append(f"{name}: a packet arrived that was never sent (changed on the way)")
            if len(set(payloads)) != len(payloads):
                failures.append(f"{name}: a packet arrived twice")
            order = [sent.index(d) for d in payloads if d in t_sent]
            if order != sorted(order):
                failures.append(f"{name}: packets arrived out of order")
            excess = [t - t_sent[d] - bench.latency_s(len(d)) for t, d in got if d in t_sent]
            excess_all += excess
            delivered = len(set(payloads) & set(t_sent))
            lab.log("sik_metrics", phase=name, sent=N, delivered=delivered,
                    excess_ms=[round(x * 1000, 1) for x in excess],
                    model_ms={len(p): round(bench.latency_s(len(p)) * 1000, 1) for p in sent[:5]})
            check("delivery_ratio", f"delivery {name}", delivered / N)
        if excess_all:
            check("latency_excess_p50_s", "latency excess median", statistics.median(excess_all), "s")
            check("latency_excess_max_s", "latency excess worst", max(excess_all), "s")
        else:
            failures.append("nothing was delivered")

        # The MTU: a packet of exactly mtu bytes arrives whole; one byte more is refused by the bridge.
        big, over = lab.rng.randbytes(mtu), lab.rng.randbytes(mtu + 1)
        app["a"].send(over, listen["a"])
        app["a"].send(big, listen["a"])
        wait_for(lambda: len(app["b"].received) >= 1, 10)
        app["b"].wait_idle(1.0, 10)
        got = app["b"].payloads()
        app["b"].received.clear()
        if got != [big]:
            failures.append(f"MTU: sent {mtu} and {mtu + 1} bytes, got {[len(g) for g in got]} (want only {mtu})")

        # A burst one way: the serial port at SERIAL_SPEED should be the only bottleneck.
        burst = [i.to_bytes(4, "big") + lab.rng.randbytes(mtu - 4) for i in range(BURST)]
        t0 = time.monotonic()
        app["a"].send_paced(burst, listen["a"], batch=len(burst))
        wait_for(lambda: len(app["b"].received) >= BURST, BURST * bench.latency_s(mtu) * 4 + 20)
        app["b"].wait_idle(1.0, 10)
        got = app["b"].received[:]
        if [d for _, d in got] != burst:
            failures.append(f"burst: sent {BURST}, got {len(got)} (every packet once, in order, unchanged)")
        elif got:
            span = got[-1][0] - t0
            model = sum(bench.busy_s(len(p)) for p in burst)   # the bottleneck alone: nothing can beat it
            lab.log("sik_metrics", phase="burst", packets=BURST, span_s=round(span, 3), model_s=round(model, 3),
                    goodput_bps=round(sum(map(len, burst)) * 8 / span))
            check("burst_ratio", "burst a_to_b", model / span)
    finally:
        stats = {s: br[s].stop() for s in br}
        for e in app.values():
            e.close()
    lab.log("bridge_stats", a=stats["a"], b=stats["b"])
    drops_a = (stats["a"] or {}).get("drops", {})
    if drops_a.get("too_large") != 1:
        failures.append(f"bridge A should have dropped exactly one packet as too_large, drops: {drops_a}")
    assert not failures, "; ".join(failures)


def _refused(proc, want_name: str) -> list[str]:
    """Why a bridge that should have refused did not (empty: it refused correctly)."""
    errs = []
    try:
        proc.proc.wait(30)
    except subprocess.TimeoutExpired:
        errs.append("the bridge kept running")
    proc.stop()
    chk = proc.seen("sik_check")
    if proc.proc.returncode == 0:
        errs.append("the bridge exited 0")
    if proc.seen("start"):
        errs.append("the bridge started bridging")
    if not chk or chk[-1].get("ok") is not False:
        errs.append(f"no failed sik_check line: {chk}")
    elif not any(m.get("name") == want_name for m in chk[-1].get("mismatch", [])):
        errs.append(f"the mismatch does not name {want_name}: {json.dumps(chk[-1].get('mismatch'))}")
    return errs


def test_bridge_refuses_mismatched_settings(lab, hw, radio):
    """Known-bad: a bridge told to expect another NETID refuses the radio and bridges nothing."""
    bench = SikBench(lab, hw)
    want = bench.expect(bench.a)
    want["NETID"] = (bench.a.settings["NETID"] % 65535) + 1
    sink = Endpoint()
    try:
        br = bench.bridge(radio, bench.a, listen=(HOST, free_udp_port()), send=sink.addr, log_name="bridge-wrong.jsonl",
                          expect=want)
        errs = _refused(br, "NETID")
    finally:
        sink.close()
    lab.log("sik_refused", expect=expect_arg(want), events=br.seen("sik_check"))
    assert not errs, "; ".join(errs)


def test_remote_change_is_caught(lab, hw, radio):
    """Known-bad (H1): radio B changes one of radio A's settings over the air (RT, not saved): TXPOWER one step
    down (never up). A's bridge must refuse. Then A is put back through its own port (+++, ATZ: the change was not
    saved, so a reboot drops it) and read again."""
    bench = SikBench(lab, hw)
    a, b = bench.radios()
    name, new = lower_setting(a.settings)
    reg, orig = a.report.registers[name], a.settings[name]
    want = {**bench.expect(a), name: orig}
    errs: list[str] = []
    try:
        b.remote([f"RTS{reg}={new}"])
        lab.log("sik_remote", by=b.id, target=a.id, command=f"RTS{reg}={new}", setting=name, was=orig, saved=False)
        sink = Endpoint()
        try:
            br = bench.bridge(radio, a, listen=(HOST, free_udp_port()), send=sink.addr,
                              log_name="bridge-after-rt.jsonl", expect=want)
            errs += _refused(br, name)
        finally:
            sink.close()
    except Exception as exc:                       # keep it: the restore below must still run
        errs.append(f"{type(exc).__name__}: {exc}")
    restored, how, why = None, "reboot without saving (+++, ATZ) on its own port", ""
    try:
        restored = a.reboot_unsaved().settings.get(name)
    except Exception as exc:
        why = str(exc)
        if a.hub is not None and a.hub_port is not None:
            how = "USB power cycle through the hub"
            try:
                a.release_link()
                a.power_cycle()
                restored = a.settings.get(name)
            except Exception as exc2:
                why += f"; power cycle: {exc2}"
    ok = restored == orig
    lab.log("sik_restored", target=a.id, setting=name, want=orig, got=restored, how=how, ok=ok, error=why or None)
    if not ok:
        errs.append(f"{a.id}: {name} is {restored} after the restore, want {orig} ({why or 'read back differs'}). "
                    "Nothing was saved: power-cycle the radio")
    assert not errs, "; ".join(errs)
