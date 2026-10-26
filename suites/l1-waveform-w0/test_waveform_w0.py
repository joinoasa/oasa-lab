"""OASA's own waveform, milestone W0 (plan: R-COM2, W0 "Simple modem in simulation").

The modem is in oasa-radio (`oasa-radio waveform ...`, crate oasa-waveform). This suite drives it only
through its command line and the files it writes, so the same checks can later run on IQ from an SDR.
Everything here is simulation; there is no radio and no RF.

What it shows:
  - the bit error rate of BPSK, QPSK and 2-FSK (noncoherent) matches the textbook curves across Eb/N0,
    with the theory computed here in Python, independently of the modem;
  - the IQ the modem writes follows the energy convention (Es = 1, noise N0 per sample for a stated
    Eb/N0), measured from the cf32 file by this suite;
  - a frame with phase offset, sample delay (also sub-sample) and a small frequency offset is received;
  - known-bad cases fail: a claim 1 dB off, a frame with the preamble cut off, pure noise, a damaged frame.

Needs `oasa-radio` in $OASA_RADIO_BIN_DIR. Without it the tests are SKIPPED with the reason, and a skipped
suite does not pass the checker. The seeds are fixed in this file, so the numbers repeat exactly.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from array import array
from pathlib import Path

import pytest

from oasa_lab.linkharness import MissingBinaries

SPS = 8              # samples per symbol (the modem's default)
SYMBOL_RATE = 25_000.0
BITS_PER_SYMBOL = {"bpsk": 1, "qpsk": 2, "fsk2": 1}
PREAMBLE_SYMBOLS = 63 + 16  # m-sequence plus sync word, one bit per symbol

# Eb/N0 points (dB) and bits per point. Chosen so each point has dozens to thousands of errors.
POINTS = {
    "bpsk": [(2.0, 40_000), (4.0, 80_000), (6.0, 120_000), (8.0, 400_000)],
    "qpsk": [(2.0, 40_000), (4.0, 80_000), (6.0, 120_000), (8.0, 400_000)],
    "fsk2": [(2.0, 20_000), (4.0, 20_000), (6.0, 20_000), (8.0, 40_000)],
}
SEEDS = {"bpsk": 11, "qpsk": 12, "fsk2": 13}


def q(x: float) -> float:
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def theory(mod: str, ebn0_db: float) -> float:
    """Textbook BER: BPSK and QPSK Q(sqrt(2 Eb/N0)); noncoherent orthogonal 2-FSK 0.5 exp(-Eb/2N0)."""
    e = 10 ** (ebn0_db / 10)
    return 0.5 * math.exp(-e / 2) if mod == "fsk2" else q(math.sqrt(2 * e))


def z_score(errors: int, bits: int, p: float) -> float:
    return (errors - bits * p) / math.sqrt(bits * p * (1 - p))


@pytest.fixture
def radio(lab):
    d = os.environ.get("OASA_RADIO_BIN_DIR")
    if not d:
        pytest.skip("OASA_RADIO_BIN_DIR is not set (folder with oasa-radio)")
    p = Path(d) / "oasa-radio"
    if not (p.is_file() and os.access(p, os.X_OK)):
        pytest.skip(str(MissingBinaries(f"oasa-radio not found or not executable in OASA_RADIO_BIN_DIR={d}")))
    lab.binary(p)
    return p


def run(radio: Path, *args: str, timeout: float = 600) -> list[dict]:
    """Run `oasa-radio waveform ...` and return its JSON lines."""
    r = subprocess.run([str(radio), "waveform", *args], capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0, f"oasa-radio waveform {' '.join(args)} failed: {r.stderr.strip()}"
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


def read_cf32(path: Path) -> array:
    a = array("f")
    a.frombytes(path.read_bytes())
    return a


def power(a: array, start: int, count: int) -> float:
    """Mean |x|^2 per complex sample over a range of samples of an interleaved I/Q float array."""
    s = a[2 * start:2 * (start + count)]
    return sum(v * v for v in s) / count


@pytest.mark.parametrize("mod", ["bpsk", "qpsk", "fsk2"])
def test_ber_matches_theory(lab, radio, mod):
    pts = POINTS[mod]
    rows: list[dict] = []
    # one run per point, because each point has its own number of bits
    for db, bits in pts:
        rows += run(radio, "ber", "--mod", mod, "--ebn0-db", str(db), "--bits", str(bits),
                    "--seed", str(SEEDS[mod]), "--json")
    lab.file(f"ber-{mod}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    failures = []
    for r in rows:
        db, errors, n = r["ebn0_db"], r["errors"], r["bits"]
        p = theory(mod, db)
        z = z_score(errors, n, p)
        lab.log("ber_point", mod=mod, ebn0_db=db, bits=n, errors=errors, ber=errors / n, theory=p, z=round(z, 3),
                frames=r["frames"], crc_ok=r["crc_ok"])
        name = f"{mod} {db:g} dB"
        if not lab.measure("ber_z", name, z):
            failures.append(f"{name}: {errors} errors in {n} bits, theory {p:.4g}, z = {z:+.2f}")
        lab.measure("detected_fraction", name, r["detected"] / r["frames"])
        lab.measure("theory_rel_error", name, abs(r["theory"] - p) / p)
    assert not failures, "; ".join(failures)


def test_same_seed_same_numbers(lab, radio):
    def strip(rows):
        return [{k: v for k, v in r.items() if k != "ts"} for r in rows]

    args = ["ber", "--mod", "qpsk", "--ebn0-db", "3", "--bits", "20000", "--json"]
    a = strip(run(radio, *args, "--seed", "5"))
    b = strip(run(radio, *args, "--seed", "5"))
    c = strip(run(radio, *args, "--seed", "6"))
    assert a == b, "the same seed gave different numbers"
    assert a[0]["errors"] != c[0]["errors"], "a different seed gave the same noise"


@pytest.mark.parametrize("mod", ["bpsk", "qpsk", "fsk2"])
def test_iq_follows_the_energy_convention(lab, radio, tmp_path, mod):
    """Measure the cf32 file itself: signal energy per symbol, and noise at the stated Eb/N0."""
    failures = []
    # (1) noiseless frame: the energy of a symbol (sum of |x|^2 over its samples) is 1
    clean = tmp_path / "clean"
    info = run(radio, "iq", "--mod", mod, "--out", str(clean), "--payload-len", "100", "--delay", "0", "--tail", "0",
               "--seed", "2")[0]
    x = read_cf32(Path(info["data"]))
    assert len(x) == 2 * info["samples"] and info["frame_samples"] == info["samples"]
    nsym = PREAMBLE_SYMBOLS + (2 + 100 + 2) * 8 // BITS_PER_SYMBOL[mod]   # preamble, sync, length, payload, CRC
    energy = sum(v * v for v in x) / nsym
    if not lab.measure("symbol_energy", f"{mod} frame", energy):
        failures.append(f"energy per symbol {energy:.4f}, expected 1")
    # (2) noise only in front of a frame, at 6 dB Eb/N0: N0 = Eb / 10^0.6, Eb = 1 / bits per symbol
    ebn0_db = 6.0
    noisy = tmp_path / "noisy"
    lead = 20_000
    info = run(radio, "iq", "--mod", mod, "--out", str(noisy), "--payload-len", "16", "--ebn0-db", str(ebn0_db),
               "--delay", str(lead), "--seed", "3")[0]
    y = read_cf32(Path(info["data"]))
    n0 = (1.0 / BITS_PER_SYMBOL[mod]) / 10 ** (ebn0_db / 10)
    ratio = power(y, 0, lead) / n0
    if not lab.measure("noise_power_ratio", f"{mod} at {ebn0_db:g} dB", ratio):
        failures.append(f"noise power is {ratio:.4f} of N0 for Eb/N0 = {ebn0_db} dB")
    # SNR per sample = Es/N0 / SPS: the modem reports it; check its arithmetic
    want = 10 * math.log10(BITS_PER_SYMBOL[mod] * 10 ** (ebn0_db / 10) / SPS)
    assert abs(info["snr_per_sample_db"] - want) < 1e-6
    assert not failures, "; ".join(failures)
    shutil.copy(Path(info["data"]), lab.file(f"{mod}-noisy.sigmf-data"))   # keep the noisy file in the evidence
    shutil.copy(Path(info["meta"]), lab.file(f"{mod}-noisy.sigmf-meta"))


@pytest.mark.parametrize("mod", ["bpsk", "qpsk", "fsk2"])
def test_frame_survives_phase_delay_and_frequency_offset(lab, radio, tmp_path, mod):
    """A frame at 20 dB Eb/N0 with a carrier phase, a whole-sample and a fractional delay, and a frequency
    offset of 0.3 % of the symbol rate is received, CRC good, payload exact. The file is cf32_le SigMF."""
    payload = bytes(lab.rng.randrange(256) for _ in range(40)).hex()
    base = tmp_path / f"frame-{mod}"
    info = run(radio, "iq", "--mod", mod, "--out", str(base), "--ebn0-db", "20", "--payload-hex", payload,
               "--phase-deg", "137", "--delay", "251", "--frac-delay", "0.37", "--cfo", "0.003",
               "--symbol-rate", str(SYMBOL_RATE), "--center-hz", "915000000", "--seed", "4")[0]
    meta = json.loads(Path(info["meta"]).read_text())
    g = meta["global"]
    assert g["core:datatype"] == "cf32_le" and g["core:version"] and g["core:num_channels"] == 1
    assert g["core:sample_rate"] == SYMBOL_RATE * SPS
    assert meta["captures"][0]["core:frequency"] == 915e6 and meta["captures"][0]["core:datetime"]
    assert Path(info["data"]).stat().st_size == 8 * info["samples"]
    rows = run(radio, "decode", "--mod", mod, "--in", str(base), "--hex")
    frames = [r for r in rows if r["event"] == "frame"]
    lab.log("decode", mod=mod, frames=len(frames), start=[f["start"] for f in frames],
            cfo=[f["cfo_cycles_per_symbol"] for f in frames])
    assert len(frames) == 1, f"expected one frame, found {len(frames)}"
    f = frames[0]
    assert f["crc_ok"] is True and f["payload_hex"] == payload
    assert abs(f["start"] - 251) <= 1, f"frame found at sample {f['start']}, sent at 251.37"
    if mod != "fsk2":
        assert abs(f["cfo_cycles_per_symbol"] - 0.003) < 5e-4
    shutil.copy(Path(info["data"]), lab.file(f"{mod}-frame.sigmf-data"))
    shutil.copy(Path(info["meta"]), lab.file(f"{mod}-frame.sigmf-meta"))


def test_wrong_ebn0_claim_is_rejected(lab, radio):
    """Known-bad: the check must be able to fail. Measure BPSK at 4 dB; the theory for 3 dB and for 5 dB
    (1 dB either way) must both be far outside the same limits, and the true 4 dB inside."""
    row = run(radio, "ber", "--mod", "bpsk", "--ebn0-db", "4", "--bits", "80000", "--seed", "21", "--json")[0]
    errors, n = row["errors"], row["bits"]
    assert abs(z_score(errors, n, theory("bpsk", 4.0))) < 3.0, "the true value should pass"
    for wrong in (3.0, 5.0):
        z = z_score(errors, n, theory("bpsk", wrong))
        lab.log("wrong_claim", claimed_db=wrong, z=round(z, 2))
        assert abs(z) > 10.0, f"a claim of {wrong} dB for a 4 dB run is only {z:.1f} sigma off"
    # a classic mistake: using SNR per sample (9 dB lower at 8 samples per symbol) for Eb/N0
    z = z_score(errors, n, theory("bpsk", 4.0 - 10 * math.log10(SPS)))
    assert abs(z) > 10.0, "treating per-sample SNR as Eb/N0 should be far off"


@pytest.mark.parametrize("mod", ["bpsk", "qpsk", "fsk2"])
def test_frame_without_preamble_is_not_detected(lab, radio, tmp_path, mod):
    """Known-bad: cut the preamble and sync word (and a few symbols) off the front of a clean frame."""
    base = tmp_path / "full"
    info = run(radio, "iq", "--mod", mod, "--out", str(base), "--payload-len", "30", "--delay", "100", "--tail", "100",
               "--seed", "5")[0]
    assert [r for r in run(radio, "decode", "--mod", mod, "--in", str(base)) if r["event"] == "frame" and r["crc_ok"]], \
        "the intact frame must decode (otherwise this test proves nothing)"
    raw = Path(info["data"]).read_bytes()
    cut = 8 * (100 + (PREAMBLE_SYMBOLS + 8) * SPS)   # delay + preamble + sync + 8 body symbols
    cutbase = tmp_path / "cut"
    shutil.copy(info["meta"], f"{cutbase}.sigmf-meta")
    Path(f"{cutbase}.sigmf-data").write_bytes(raw[cut:])
    rows = run(radio, "decode", "--mod", mod, "--in", str(cutbase))
    good = [r for r in rows if r["event"] == "frame" and r["crc_ok"]]
    assert not good, "a frame without a preamble was accepted"


def test_noise_only_gives_no_frames(lab, radio, tmp_path):
    """Known-bad: 400 000 samples of Gaussian noise (a million positions across three modulations). The false
    alarm rate per position is (1 - 0.04)^(M-1), about 1e-11 for M = 632 (see oasa-radio docs/waveform-w0.md)."""
    n = 400_000
    noise = array("f", (lab.rng.gauss(0.0, math.sqrt(0.5)) for _ in range(2 * n)))   # N0 = 1 per complex sample
    for mod in ("bpsk", "qpsk", "fsk2"):
        base = tmp_path / f"noise-{mod}"
        Path(f"{base}.sigmf-data").write_bytes(noise.tobytes())
        rows = run(radio, "decode", "--mod", mod, "--in", str(base))
        done = rows[-1]
        lab.log("noise_only", mod=mod, samples=done["samples"], frames=done["frames"])
        assert done["samples"] == n
        assert done["frames"] == 0, f"{mod}: {done['frames']} false frames in noise"


@pytest.mark.parametrize("mod", ["bpsk", "qpsk", "fsk2"])
def test_damaged_frame_fails_the_crc(lab, radio, tmp_path, mod):
    """Known-bad: strong noise over a stretch of the payload. The frame is found (its preamble is intact) but
    its CRC must fail; a damaged payload must never be passed on as good."""
    base = tmp_path / "frame"
    delay = 100
    payload_len = 60
    info = run(radio, "iq", "--mod", mod, "--out", str(base), "--payload-len", str(payload_len), "--delay", str(delay),
               "--tail", "100", "--seed", "6")[0]
    x = read_cf32(Path(info["data"]))
    first = delay + (PREAMBLE_SYMBOLS + 16 + 80) * SPS       # well into the body
    for i in range(first, first + 5 * SPS):                   # five symbols of heavy noise
        x[2 * i] += lab.rng.gauss(0.0, 3.0)
        x[2 * i + 1] += lab.rng.gauss(0.0, 3.0)
    Path(f"{base}.sigmf-data").write_bytes(x.tobytes())
    rows = run(radio, "decode", "--mod", mod, "--in", str(base))
    frames = [r for r in rows if r["event"] == "frame"]
    lab.log("damaged", mod=mod, frames=[(f["start"], f["crc_ok"]) for f in frames])
    assert frames, "the damaged frame's preamble should still be found"
    assert not any(f["crc_ok"] for f in frames), "a damaged payload passed the CRC"
