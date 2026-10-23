"""Unit tests for the channel, the pcap writer and the statistics helpers."""

import ast
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

import oasa_lab.checker
from oasa_lab.channel import STAT_KEYS, ChannelSettings, Direction
from oasa_lab.checker.core import _mavlink_tiles
from oasa_lab.checker.pcapread import PcapError, parse_pcap
from oasa_lab.checker.stats import binomial_bound, chi2_sf, poisson_limit
from oasa_lab.pcap import PcapWriter, read_pcap
from oasa_lab.testing import mavlink2_heartbeat

NOISY = dict(loss=0.2, duplicate=0.1, corrupt=0.1, reorder=0.1, replay=0.1, jitter_ms=3, delay_ms=5)


def decisions(seed, n=500, **over):
    d = Direction("a_to_b", ChannelSettings(seed=seed, **{**NOISY, **over}))
    out = [d.process(bytes([i % 256]) * 40, now=float(i)) for i in range(n)]
    return d, out


def test_same_seed_same_decisions():
    d1, o1 = decisions(42)
    d2, o2 = decisions(42)
    assert d1.trace == d2.trace and o1 == o2


def test_different_seed_different_decisions():
    assert decisions(1)[0].trace != decisions(2)[0].trace


def test_directions_use_different_streams():
    s = ChannelSettings(seed=3, **NOISY)
    a, b = Direction("a_to_b", s), Direction("b_to_a", s)
    for i in range(300):
        a.process(b"x" * 20)
        b.process(b"x" * 20)
    assert a.trace != b.trace


def test_decisions_do_not_depend_on_timing_or_delay_settings():
    """Wall-clock time, rate limiting and delay values change *when*, never *which*."""
    base = decisions(9)[0].trace
    assert decisions(9, rate_kbps=64)[0].trace == base
    assert decisions(9, delay_ms=50, jitter_ms=10)[0].trace == base
    d = Direction("a_to_b", ChannelSettings(seed=9, **NOISY))
    for i in range(500):
        d.process(bytes([i % 256]) * 40, now=1000.0 * i)
    assert d.trace == base


def test_stats_add_up():
    d, out = decisions(5, n=2000, mtu=0)
    st = d.stats
    assert st["sent"] == 2000
    assert sum(len(x) for x in out) == st["sent"] - st["dropped"] + st["duplicated"] + st["replayed"]
    assert set(st) == set(STAT_KEYS)


def test_corrupt_changes_exactly_one_byte():
    d = Direction("a_to_b", ChannelSettings(seed=1, corrupt=1.0))
    for _ in range(200):
        (e,) = d.process(b"\x00" * 30)
        assert sum(x != 0 for x in e.data) == 1


def test_mtu_and_certain_loss():
    d = Direction("a_to_b", ChannelSettings(seed=1, mtu=10))
    assert d.process(b"x" * 11) == [] and d.stats["too_big"] == 1
    assert len(d.process(b"x" * 10)) == 1
    d = Direction("a_to_b", ChannelSettings(seed=1, loss=1.0))
    assert d.process(b"x") == [] and d.stats["dropped"] == 1


def test_rate_limit_queues_packets():
    d = Direction("a_to_b", ChannelSettings(rate_kbps=8))  # 1000 bytes/s
    delays = [d.process(b"x" * 100, now=0.0)[0].delay for _ in range(3)]
    assert delays == pytest.approx([0.1, 0.2, 0.3])


@pytest.mark.parametrize("bad", [dict(loss=1.5), dict(delay_ms=-1), dict(mtu=70000), dict(seed=-1), dict(reorder=-0.1)])
def test_bad_settings_rejected(bad):
    with pytest.raises(ValueError):
        ChannelSettings(**bad)


def test_unknown_setting_rejected():
    with pytest.raises(ValueError):
        ChannelSettings.from_dict({"lossy": 0.1})


def test_pcap_roundtrip_and_reader_agree(tmp_path):
    path = tmp_path / "x.pcap"
    w = PcapWriter(path)
    sent = [(("127.0.0.1", 4000 + i), ("127.0.0.1", 5000), bytes([i]) * (i + 1)) for i in range(20)]
    for src, dst, pl in sent:
        w.write(src, dst, pl, ts=1700000000.5)
    w.close()
    ours = read_pcap(path)
    theirs = parse_pcap(path.read_bytes())  # the checker's own, separate reader
    assert [(p.src, p.dst, p.payload) for p in ours] == sent
    assert [(p.src, p.dst, p.payload) for p in theirs] == sent
    assert ours[0].ts == pytest.approx(1700000000.5)


def test_pcap_header_is_standard(tmp_path):
    path = tmp_path / "x.pcap"
    w = PcapWriter(path)
    w.write(("127.0.0.1", 1), ("127.0.0.1", 2), b"hello")
    w.close()
    raw = path.read_bytes()
    magic, vmaj, vmin, _, _, snap, link = struct.unpack("<IHHiIII", raw[:24])
    assert (magic, vmaj, vmin, link) == (0xA1B2C3D4, 2, 4, 101)
    ip = raw[24 + 16:24 + 16 + 20]
    total = 0
    for i in range(0, 20, 2):
        total += (ip[i] << 8) | ip[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    assert total == 0xFFFF, "IPv4 header checksum must verify"


@pytest.mark.skipif(shutil.which("tcpdump") is None, reason="tcpdump not installed")
def test_tcpdump_can_read_the_pcap(tmp_path):
    path = tmp_path / "x.pcap"
    w = PcapWriter(path)
    w.write(("127.0.0.1", 1111), ("127.0.0.1", 2222), b"hello")
    w.close()
    out = subprocess.run(["tcpdump", "-nn", "-r", str(path)], capture_output=True, text=True)
    assert "127.0.0.1.1111 > 127.0.0.1.2222: UDP, length 5" in out.stdout + out.stderr


def test_checker_rejects_bad_pcaps():
    for blob in (b"", b"\x00" * 30, struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)):
        with pytest.raises(PcapError):
            parse_pcap(blob)


def test_lab_markers_are_random_per_test_and_repeatable(lab):
    m1, m2 = lab.marker(), lab.marker()
    assert m1 != m2 and m1.startswith("OASA-MARK-") and len(m1) >= 24


def test_chi_square_known_values():
    assert chi2_sf(3.841, 1) == pytest.approx(0.05, abs=1e-3)
    assert chi2_sf(18.307, 10) == pytest.approx(0.05, abs=1e-3)
    assert chi2_sf(255, 255) == pytest.approx(0.49, abs=0.02)
    assert chi2_sf(1000, 255) < 1e-80


def test_binomial_and_poisson_helpers():
    lo, hi = binomial_bound(1000, 0.1)
    assert lo < 100 < hi and hi - lo < 100
    assert poisson_limit(0.1) >= 3


def test_mavlink_scanner_logic():
    hb = mavlink2_heartbeat(7)
    assert _mavlink_tiles(hb, 0)
    assert _mavlink_tiles(hb + hb, 0)  # two frames in one packet
    assert not _mavlink_tiles(hb + b"\x00", 0)  # length does not match the packet
    assert not _mavlink_tiles(b"\x00" + hb, 0)
    assert _mavlink_tiles(b"\x00" * 4 + hb, 4)
    v1 = bytes([0xFE, 3, 0, 1, 1, 0, 1, 2, 3, 0, 0])
    assert _mavlink_tiles(v1, 0)


def test_checker_imports_only_the_standard_library():
    """The checker must not use the runner, channel, pcap writer or plugin."""
    import sys

    root = Path(oasa_lab.checker.__file__).parent
    stdlib = sys.stdlib_module_names
    for f in root.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative: must stay inside the checker package
                    assert node.level == 1 and (node.module or "") in ("core", "pcapread", "stats", "") , f"{f.name}: {node.module}"
                    continue
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for n in names:
                assert n in stdlib, f"{f.name} imports {n}, which is not standard library"
