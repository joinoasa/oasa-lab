"""Many radios on one channel, simulated (T-MSG1, issue 65).

Text, position and call setup share one long-range LoRa setting; voice conversations share one fast
setting. Event-driven on a simulated clock (oasa_lab/crowdsim.py), so every case repeats from the
seed and the whole suite takes seconds. A case under the 95 % text-delivery mark is logged as
failing: that is a result, and the suite passes when it reports it correctly.
Table and assumptions: docs/crowd-sim.md.
"""

import time

import pytest

from oasa_lab import voicesim
from oasa_lab.channel import ChannelSettings
from oasa_lab.crowdsim import (ACK_BYTES, CALL_S, CALLS, CROWD_N, FINE_N, PLAN_TEXT_DELIVERY, POS_BYTES, PRIO,
                               SETUP_BYTES, TEXT_BYTES, TEXT_SETTINGS, TRAFFIC, VOICE_INTERVAL_MS, VOICE_RATE_BPS,
                               VOICE_SETTINGS, Medium, Msg, TextCase, TextSim, Tx, VoiceCase, largest_n, run_text,
                               run_voice, sizes_to_try)


def report(lab, res, event="crowd_case"):
    lab.log(event, **res)
    name = res["case"]
    ok = [lab.measure("crowd_text_delivery", name, res["text_delivery"]),
          lab.measure("crowd_pos_delivery", name, res["pos_delivery"]),
          lab.measure("crowd_text_delay_p95_s", name, res["text_delay_p95_s"], "s")]
    assert all(ok), res
    assert res["meets_mark"] == (res["text_delivery"] > PLAN_TEXT_DELIVERY), res


def text_combos():
    """(profile, traffic, lbt, calls, listen): every combination in the main table."""
    out = [(p, tr, lbt, calls, "one") for p in TEXT_SETTINGS for tr in TRAFFIC for lbt in (False, True)
           for calls in CALLS]
    out += [(p, "quiet", lbt, 4, "two") for p in TEXT_SETTINGS for lbt in (False, True)]
    return out


def test_sizes_priorities_and_settings():
    oh = voicesim.OASA_OVERHEAD_BYTES
    assert oh == 22
    assert (TEXT_BYTES, POS_BYTES, ACK_BYTES, SETUP_BYTES) == (60 + oh, 20 + oh, 4 + oh, 40 + oh)
    for p in TEXT_SETTINGS + VOICE_SETTINGS:
        assert max(TEXT_BYTES, SETUP_BYTES) <= ChannelSettings.for_profile(p).mtu
        assert ChannelSettings.for_profile(p).half_duplex
    # The plan's order (call setup, voice, text, location); voice is on its own setting here.
    assert PRIO["setup"] < PRIO["text"] < PRIO["pos"]
    assert "lora-sf11-bw250" in TEXT_SETTINGS and "lora-sf9-bw125" in TEXT_SETTINGS
    assert set(CROWD_N) <= set(FINE_N) and CALLS == (0, 1, 2, 4)


def test_largest_n_rule():
    ok, bad = {"meets_mark": True}, {"meets_mark": False}
    assert largest_n({10: ok, 30: ok, 100: bad, 300: bad}) == 30
    assert largest_n({10: bad, 30: ok, 100: ok, 300: bad}) == 100  # small crowds can fail with calls
    assert largest_n({10: bad, 30: bad, 100: bad, 300: bad}) == 0
    assert sizes_to_try({10: ok, 30: ok, 100: bad, 300: bad}) == [20, 50, 75]
    assert sizes_to_try({10: ok, 30: ok, 100: ok, 300: ok}) == [20, 50, 75, 150, 200]
    assert sizes_to_try({10: bad, 30: bad, 100: bad, 300: bad}) == []


def test_text_sweep(lab):
    """Every long-range setting x traffic x LBT x calls at N = 10, 30, 100, 300; then the in-between
    sizes up to the first failing one, for the largest N above the mark."""
    t0 = time.monotonic()
    n_cases = 0
    grid = {}
    for p, tr, lbt, calls, listen in text_combos():
        by_n = {}
        for n in CROWD_N:
            res = run_text(TextCase(p, n, lbt, tr, calls, listen), seed=lab.seed)
            report(lab, res)
            by_n[n] = res
            n_cases += 1
        for n in sizes_to_try(by_n):
            by_n[n] = run_text(TextCase(p, n, lbt, tr, calls, listen), seed=lab.seed)
            report(lab, by_n[n], "crowd_fine")
            n_cases += 1
        lab.log("crowd_largest_n", profile=p, traffic=tr, lbt=lbt, calls=calls, listen=listen,
                largest_n=largest_n(by_n), failing_below=[n for n in sorted(by_n) if n < largest_n(by_n)
                                                          and not by_n[n]["meets_mark"]],
                delivery_by_n={str(n): by_n[n]["text_delivery"] for n in sorted(by_n)})
        if calls == 0:
            grid[(p, tr, lbt)] = by_n
    lab.log("crowd_sweep", cases=n_cases, seed=lab.seed, wall_s=round(time.monotonic() - t0, 2))
    # Sanity comparison only (not an input): delivered (collision-free) airtime with LBT over blind,
    # measured elsewhere at 1.5 to 2.2 times. LBT never delivers less here.
    for p in TEXT_SETTINGS:
        for tr in TRAFFIC:
            for n in (100, 300):
                a, b = grid[(p, tr, False)][n], grid[(p, tr, True)][n]
                ratio = b["clean_air_share"] / a["clean_air_share"]
                lab.log("crowd_lbt_ratio", profile=p, traffic=tr, n=n, offered=a["offered"],
                        clean_blind=a["clean_air_share"], clean_lbt=b["clean_air_share"], ratio=round(ratio, 2))
                assert ratio > 1.0 and b["text_delivery"] >= a["text_delivery"], (a["case"], ratio)


def test_text_alternatives(lab):
    """The stated alternatives: the capture rule, and hidden carriers (30 % of pairs cannot hear each
    other's carrier, so LBT misses them; every receiver still hears both)."""
    for p in TEXT_SETTINGS:
        for n in CROWD_N:
            for lbt, cap, hid in ((False, True, 0.0), (True, True, 0.0), (True, False, 0.3)):
                res = run_text(TextCase(p, n, lbt, "quiet", capture=cap, hidden=hid), seed=lab.seed)
                report(lab, res, "crowd_alt")
    # Hidden carriers can only hurt LBT: at 300 radios on the slowest setting it delivers less.
    base = run_text(TextCase("lora-sf11-bw250", 300, True, "quiet"), seed=lab.seed)
    hid = run_text(TextCase("lora-sf11-bw250", 300, True, "quiet", hidden=0.3), seed=lab.seed)
    assert hid["text_delivery"] < base["text_delivery"]


def test_voice_channel(lab):
    """1, 2 and 4 conversations on each fast setting, direct and relayed, blind and LBT."""
    for p in VOICE_SETTINGS:
        for relayed in (False, True):
            for lbt in (False, True):
                for calls in CALLS[1:]:
                    res = run_voice(VoiceCase(p, calls, lbt, relayed), seed=lab.seed)
                    lab.log("crowd_voice", **res)
                    assert lab.measure("crowd_voice_delivery", res["case"], res["delivery"])
                    assert lab.measure("crowd_voice_delay_p95_ms", res["case"], res["delay_p95_ms"], "ms")
    # One conversation alone, blind, direct: the same as the single-link voice model (l1-voice-lora).
    for p in VOICE_SETTINGS:
        one = run_voice(VoiceCase(p, 1, False, False), seed=lab.seed)
        ref = voicesim.run(voicesim.Case(p, VOICE_RATE_BPS, VOICE_INTERVAL_MS, False), seed=lab.seed)
        assert one["delivery"] == ref["delivery"] == 1.0
        assert one["delay_mean_ms"] == pytest.approx(ref["delay_mean_ms"], abs=1.0)


def test_radio_in_a_call_misses_text(lab):
    """With one receiver, a radio in a call hears nothing on the text setting; with two it does."""
    for listen, want in (("one", False), ("two", True)):
        sim = TextSim(TextCase("lora-sf10-bw250", 3, True, "quiet", calls=1, listen=listen), seed=lab.seed,
                      window_s=1.0)
        sim.radios[1].call = (0.0, CALL_S)
        m = Msg("text", 0, 1, 0.0)
        sim.msgs.append(m)
        sim.enqueue(sim.radios[0], "text", m)
        sim.sim.run(CALL_S / 2)  # all three attempts fall inside the call
        assert (m.got_at is not None) is want, listen
        assert m.attempts == (1 if want else 3)


def test_overloaded_case_is_reported_failing(lab):
    """Known bad: 300 radios on SF11 at 250 kHz with a position every 60 s offer about three times
    the channel's airtime. Both blind and LBT must be reported under the mark, not crash the run."""
    for lbt in (False, True):
        res = run_text(TextCase("lora-sf11-bw250", 300, lbt, "pos60"), seed=lab.seed)
        report(lab, res)
        assert res["offered"] > 1.0
        assert not res["meets_mark"] and res["text_delivery"] < 0.5
    # A light case on the same setting passes: the mark tells them apart.
    ok = run_text(TextCase("lora-sf11-bw250", 10, True, "quiet"), seed=lab.seed)
    report(lab, ok)
    assert ok["meets_mark"]
    # Voice: four relayed conversations on SF8 at 250 kHz offer about 3 times the airtime.
    v = run_voice(VoiceCase("lora-sf8-bw250", 4, True, True), seed=lab.seed)
    lab.log("crowd_voice", **v)
    assert v["offered"] > 1.0 and not v["meets_plan"] and v["delivery"] < 0.5


def test_same_seed_same_results(lab):
    """The same seed gives the same numbers; another seed gives others."""
    cases = [TextCase("lora-sf10-bw250", 100, False, "quiet", calls=2),
             TextCase("lora-sf10-bw250", 100, True, "chatty", calls=1)]
    a = [run_text(c, seed=lab.seed) for c in cases]
    b = [run_text(c, seed=lab.seed) for c in cases]
    other = [run_text(c, seed=lab.seed + 1) for c in cases]
    assert a == b
    assert all(x != y for x, y in zip(a, other))
    va = run_voice(VoiceCase("lora-sf7-bw250", 4, True, False), seed=lab.seed)
    assert va == run_voice(VoiceCase("lora-sf7-bw250", 4, True, False), seed=lab.seed)
    assert va != run_voice(VoiceCase("lora-sf7-bw250", 4, True, False), seed=lab.seed + 1)
    for x, y in zip(a, other):
        lab.log("crowd_repeat", case=x["case"], seed=lab.seed, text_delivery=x["text_delivery"],
                other_seed_text_delivery=y["text_delivery"])


def test_collision_destroys_both():
    """Known bad: two packets that overlap, even by a microsecond, are both lost at every receiver.
    Packets that only touch both arrive. A radio that is sending hears nothing (half-duplex)."""
    m = Medium()
    a, b = Tx(0, "text", 0.0, 1.0, None), Tx(1, "text", 0.999999, 2.0, None)
    m.start(a)
    m.start(b)
    m.end(a)
    m.end(b)
    for r in (2, 3):
        assert not m.got(a, r) and not m.got(b, r)
    assert not m.got(a, 1)  # radio 1 was sending
    assert m.collided == 2
    c = Tx(2, "text", 2.0, 3.0, None)  # starts as b ends
    m.start(c)
    m.end(c)
    assert m.got(c, 0) and not c.hits
    # The capture rule: a packet 6 dB or more above each overlapping one survives; the other is lost.
    power = [[0.0] * 4 for _ in range(4)]
    power[0][3], power[1][3] = 10.0, 3.9  # at radio 3: 6.1 dB apart
    power[0][2], power[1][2] = 10.0, 4.1  # at radio 2: 5.9 dB apart
    k = Medium(power=power)
    a, b = Tx(0, "text", 0.0, 1.0, None), Tx(1, "text", 0.5, 1.5, None)
    k.start(a)
    k.start(b)
    assert k.got(a, 3) and not k.got(b, 3)
    assert not k.got(a, 2) and not k.got(b, 2)
    # Listen-before-send sees a packet on the air, not one that starts after its check.
    lbt = Medium()
    assert not lbt.busy(5)
    lbt.start(Tx(0, "text", 0.0, 1.0, None))
    assert lbt.busy(5)
