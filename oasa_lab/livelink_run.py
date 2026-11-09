"""The live-link run shared by l2-sdr-live-link (50 min) and l2-sdr-live-soak (24 h). Limits and reasons: each suite's
suite.toml. Design: docs/sdr-live-link.md.

Alice's side is the B200mini, Bob's the E310. Per radio: a helper moves samples between the radio and two FIFOs
(hardware) and an `oasa-radio bridge --radio sdr` turns frames into UDP datagrams. On the simulated bench two
`oasa-radio waveform cable` processes join the bridges' FIFOs instead (gain, clock offset, noise).

    app A (ping) -> oasa-link Alice -> bridge B200 -> helper B200 =60 dB=> helper E310 -> bridge E310 -> oasa-link Bob
    -> app B (echo, stamps the arrival) -> ... the same way back
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from oasa_lab import livelink as ll
from oasa_lab.devices import Unavailable
from oasa_lab.hw import Hardware, WiringRefused
from oasa_lab.linkharness import HOST, LinkProc, MissingBinaries, find_bins, make_identity, trust
from oasa_lab.sdrlink import radio_bin
from oasa_lab.testing import free_udp_port

MTU = 200
SIM_DURATION_S, SIM_RATE_HZ = 30.0, 10.0     # a debug build at a fifth of the rates: 10 messages a second each way
HELPER_START_S = 90.0                        # a B200 loads its firmware and FPGA image; an E310 claim can wait 30 s
SESSION_WAIT_S = 30.0
BRIDGE_READY_S = 15.0                        # each sdr bridge prints {"event":"start",...} once its FIFOs are open
SETTLE_S = 2.0                               # radios streaming before the handshake (F10 runs 8 Nov: a handshake
                                             # started on the helper's first event lost its first message: 5.1 s)


def wait_until(cond, timeout: float, step: float = 0.2) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def run_live(lab, hw: Hardware, duration_key: str = "duration_s") -> None:
    """One live-link run; `duration_key` picks the hardware duration from [sdr_live] (duration_s for the 50-minute
    suite, soak_duration_s for the 24-hour soak). The simulated bench always runs SIM_DURATION_S."""
    try:
        sec = find_bins(lab)
    except MissingBinaries as exc:
        pytest.skip(str(exc))
    radio = radio_bin()
    if radio is None:
        pytest.skip("OASA_RADIO_BIN_DIR is not set or has no oasa-radio")
    try:
        hw.use_wiring()
        b200, e310 = hw.first("b200"), hw.first("e310")
    except (WiringRefused, Unavailable) as exc:
        pytest.skip(str(exc))
    try:
        cfg = ll.LiveConfig.from_dict(hw.inv.sdr_live)
    except ll.LiveConfigError as exc:
        pytest.fail(str(exc))
    sim = b200.backend == "sim" or e310.backend == "sim"
    helper = None
    if not sim:
        if cfg.helper is None or not cfg.helper.is_file():
            pytest.skip(f"the radio helper is not installed ([sdr_live] helper = {cfg.helper}); docs/sdr-live-link.md")
        try:
            helper = cfg.check_helper()
        except ll.LiveConfigError as exc:
            pytest.fail(str(exc))
        lab.binary(helper)
    for dev in (b200, e310):
        dev.require_tx("the live link")
    lab.binary(radio)
    duration, rate = (SIM_DURATION_S, SIM_RATE_HZ) if sim else (cfg[duration_key], cfg["rate_hz"])
    n = int(duration * rate)
    rt = cfg["bridge_rt_prio"] if (not sim and ll.rt_allowed(cfg["bridge_rt_prio"])) else 0
    cpus = {k: (cfg[f"{k}_cpus"] if not sim and ll.pin_allowed(cfg[f"{k}_cpus"]) else "") for k in ("b200", "e310")}

    fdir = ll.fifo_dir()
    keys = Path(tempfile.mkdtemp(prefix="sdr-live-keys-"))   # identities stay out of the bundle
    procs: list[ll.Proc] = []
    links: list[LinkProc] = []
    try:
        f = ll.mkfifos(fdir, ["b.tx", "b.rx", "e.tx", "e.rx"])
        port = {k: free_udp_port() for k in ("bridge_b", "bridge_e", "link_a", "link_b", "plain_a", "plain_b",
                                             "app_a", "app_b")}
        mode = ll.SIM_BRIDGE if sim else ll.HW_BRIDGE
        lab.log("live_setup", simulated=sim, duration_s=duration, rate_hz=rate, messages=n, fifo_dir=str(fdir),
                bridge_args=mode, bridge_rt_prio=rt, b200_cpus=cpus["b200"], e310_cpus=cpus["e310"],
                b200_helper_args=None if sim else cfg["b200_helper_args"],
                e310_helper_args=None if sim else cfg["e310_helper_args"])

        def bridge(side: str, cfo: float, listen: int, send: int, cpus: str) -> ll.Proc:
            cmd = [str(radio), "bridge", "--radio", "sdr", "--tx", str(f[f"{side}.tx"]), "--rx", str(f[f"{side}.rx"]),
                   "--cfo-hz", f"{cfo:g}", "--udp-listen", f"{HOST}:{listen}", "--udp-send", f"{HOST}:{send}",
                   "--mtu", str(MTU), *mode]
            if cfg["sdr_log"]:
                cmd += ["--sdr-log", str(lab.file(f"{side}-sdr.jsonl"))]
            return ll.Proc(f"bridge-{side}", ll.wrap(cmd, cpus, rt), lab.file(f"bridge-{side}.jsonl"))

        def started(p: ll.Proc) -> bool:
            return any(e.get("event") == "start" for e in p.events()) or not p.alive()

        if sim:
            for i, (src, dst, cfo) in enumerate((("b.tx", "e.rx", 300.0), ("e.tx", "b.rx", -300.0))):
                procs.append(ll.Proc(f"cable-{src[0]}", [
                    str(radio), "waveform", "cable", "--in", str(f[src]), "--out", str(f[dst]), "--gain-db", "-20",
                    "--noise-dbfs", "-62", "--cfo-hz", f"{cfo:g}", "--sample-rate", f"{ll.SIM_RATE:.0f}",
                    "--seed", str((lab.seed or 0) * 2 + i + 1)], lab.file(f"cable-{src[0]}.jsonl")))
            procs.append(bridge("e", 0.0, port["bridge_e"], port["link_b"], ""))
            procs.append(bridge("b", 0.0, port["bridge_b"], port["link_a"], ""))
        else:
            max_s = duration + ll.TX_MARGIN_S
            for dev, side, tx_f, rx_f in ((e310, "e", cfg["e310_tx_freq"], cfg["b200_tx_freq"]),
                                          (b200, "b", cfg["b200_tx_freq"], cfg["e310_tx_freq"])):
                try:
                    cmd = ll.helper_command(helper, dev, tx=f[f"{side}.tx"], rx=f[f"{side}.rx"], tx_freq=tx_f,
                                            rx_freq=rx_f, tx_gain=cfg[f"{dev.kind}_tx_gain"], rx_gain=cfg["rx_gain"],
                                            max_seconds=max_s, extra=cfg[f"{dev.kind}_helper_args"])
                except ll.LiveConfigError as exc:
                    pytest.fail(str(exc))
                h = ll.Proc(f"helper-{side}", ll.wrap(cmd, cpus[dev.kind]), lab.file(f"helper-{dev.kind}.jsonl"))
                procs.append(h)
                procs.append(bridge(side, cfg[f"{dev.kind}_cfo_hz"], port[f"bridge_{side}"],
                                    port["link_b" if side == "e" else "link_a"], cpus[dev.kind]))
                assert wait_until(lambda: started(h), HELPER_START_S), f"{dev.id}: the helper did not start"
                assert h.alive(), f"{dev.id}: the helper exited ({h.p.returncode}); see logs/{h.out.name}"
            lab.log("helpers_rt", **{p.name: [e for e in p.events() if e.get("event") in ("rt", "rt_refused")]
                                     for p in procs if p.name.startswith("helper")})
            # The handshake limit times session setup over a running link (bench: 0.04-0.05 s with the radios
            # streaming), not radio start-up: wait for both bridges' start event, then let the streams settle.
            bridges = [p for p in procs if p.name.startswith("bridge")]
            ready = wait_until(lambda: all(any(e.get("event") == "start" for e in b.events()) for b in bridges),
                               BRIDGE_READY_S)
            time.sleep(SETTLE_S)
            dead = [p.name for p in procs if not p.alive()]
            lab.log("live_ready", bridges_ready=ready, settle_s=SETTLE_S, stopped=dead)
            assert not dead, f"stopped before the handshake: {dead}; see logs/"

        alice, bob = make_identity(sec, keys, "alice"), make_identity(sec, keys, "bob")
        trust(sec, alice, bob)
        trust(sec, bob, alice)
        links.append(LinkProc(sec, bob, "alice", bind=(HOST, port["link_b"]), remote=(HOST, port["bridge_e"]),
                              initiate=False, plain_listen=(HOST, port["plain_b"]), plain_send=(HOST, port["app_b"]),
                              log_path=lab.file("link-bob.jsonl"), mtu=MTU))
        t0 = time.monotonic()
        link_a = LinkProc(sec, alice, "bob", bind=(HOST, port["link_a"]), remote=(HOST, port["bridge_b"]),
                          initiate=True, plain_listen=(HOST, port["plain_a"]), plain_send=(HOST, port["app_a"]),
                          log_path=lab.file("link-alice.jsonl"), mtu=MTU)
        links.append(link_a)
        up = wait_until(lambda: bool(link_a.seen("session_established")), SESSION_WAIT_S, step=0.005)
        hs = round(time.monotonic() - t0, 3)
        dead = [p.name for p in procs if not p.alive()]
        assert up, f"no session in {SESSION_WAIT_S:g} s (stopped: {dead or 'none'}); see logs/link-*.jsonl"
        assert lab.measure("handshake_s", "handshake over the live link", hs, "s"), f"{hs} s"

        echo_out, ping_out = lab.file("echo.json"), lab.file("ping.json")
        echo = ll.Proc("echo", ll.traffic_cmd("echo", f"{HOST}:{port['app_b']}", f"{HOST}:{port['plain_b']}", str(n),
                                              str(echo_out)), lab.file("echo.log"))
        procs.append(echo)
        time.sleep(0.5)
        ping = ll.Proc("ping", ll.traffic_cmd("ping", f"{HOST}:{port['app_a']}", f"{HOST}:{port['plain_a']}", str(n),
                                              f"{1.0 / rate:.6f}", str(ping_out)), lab.file("ping.log"))
        procs.append(ping)
        end = time.monotonic() + duration + 60
        while ping.alive() and time.monotonic() < end:
            gone = [p.name for p in procs if not p.alive() and p.name not in ("echo", "ping")]
            if gone:
                break
            time.sleep(1.0)
        gone = [p.name for p in procs if not p.alive() and p.name not in ("echo", "ping")]
        ping.stop(5)
        echo.stop()
        assert not gone, f"stopped during the run: {', '.join(gone)}; see their logs"
        assert ping_out.is_file() and echo_out.is_file(), "the traffic tools wrote no result; see logs/ping.log"
        pr, er = json.loads(ping_out.read_text()), json.loads(echo_out.read_text())
    finally:
        for lk in links:
            lk.stop()
        for p in reversed(procs):
            p.stop()
        shutil.rmtree(fdir, ignore_errors=True)
        shutil.rmtree(keys, ignore_errors=True)

    assert pr["bad"] == 0 and er["bad"] == 0, f"messages nobody sent arrived (ping {pr['bad']}, echo {er['bad']})"
    assert pr["duplicates"] == 0 and er["duplicates"] == 0, "a message arrived twice"
    rx_b = er["received"]
    ok = [lab.measure("loss_a2b_pct", "lost Alice to Bob", round(100.0 * (n - rx_b) / n, 4), "%"),
          lab.measure("loss_b2a_pct", "lost Bob to Alice", round(100.0 * (rx_b - pr["back"]) / max(1, rx_b), 4), "%")]
    lab.log("live_result", messages=n, received_by_bob=rx_b, back_at_alice=pr["back"],
            **{d: {k: v for k, v in pr[d].items() if k != "hist"} for d in ("a2b", "b2a", "rtt")},
            outliers=len(pr["outliers"]))
    lab.log("live_hours", hours=pr.get("hours", []))
    if not sim:
        for d in ("a2b", "b2a"):
            s = pr[d]
            assert s["n"], f"{d}: no delays measured"
            ok += [lab.measure(f"one_way_p50_{d}_ms", f"one-way median {d}", s["p50"], "ms"),
                   lab.measure(f"one_way_p99_{d}_ms", f"one-way p99 {d}", s["p99"], "ms"),
                   lab.measure(f"one_way_p999_{d}_ms", f"one-way p99.9 {d}", s["p999"], "ms"),
                   lab.measure(f"one_way_max_{d}_ms", f"one-way worst {d} (largest 10-min max)", s["max"], "ms")]
        helpers = [p for p in procs if p.name.startswith("helper")]
        reopens = sum(1 for p in helpers for e in p.events() if e.get("event") == "reopened")
        lab.log("helper_stop", **{p.name: next((e for e in reversed(p.events()) if e.get("event") == "stop"), None)
                                  for p in helpers})
        ok.append(lab.measure("reopens", "radio reopens", reopens))
    assert all(ok), "a measurement is outside its limits; see the measurement events"
