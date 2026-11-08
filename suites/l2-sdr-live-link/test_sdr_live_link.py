"""The encrypted link live over the SDR bench, 50 minutes. Limits and reasons: suite.toml. The run itself:
oasa_lab/livelink_run.py (shared with l2-sdr-live-soak). Design: docs/sdr-live-link.md."""

from oasa_lab.hw import Hardware
from oasa_lab.livelink_run import run_live


def test_live_link(lab, hw: Hardware):
    run_live(lab, hw, "duration_s")
