"""One simulator per autopilot for the whole module: start it, pair the signing key, tear it down.

The simulator, the pairing and the plaintext ports live for the module. Each test starts its own
pair of oasa-link processes and its own captured channel (see oasa_lab.sitl.Links)."""

import shutil

import pytest

from oasa_lab import context
from oasa_lab.linkharness import MissingBinaries, find_bins
from oasa_lab.pytest_plugin import Lab
from oasa_lab.sitl import (ArduPilot, HOST, Px4, Rig, SitlMissing, find_ardupilot, find_px4, need_pymavlink, new_tmp,
                           pair, rm)
from oasa_lab.testing import free_udp_port


@pytest.fixture(scope="module", params=["px4", "ardupilot"])
def rig(request):
    kind = request.param
    ctx = context.current()
    recorder = Lab(ctx, f"rig-{kind}")  # records binaries in the manifest
    try:
        need_pymavlink()
        bins = find_bins(recorder)
        exe = find_px4() if kind == "px4" else find_ardupilot()
    except (MissingBinaries, SitlMissing) as exc:
        pytest.skip(str(exc))
    recorder.binary(exe)
    tmp = new_tmp(f"oasa-sitl-{kind}-")
    ap = (Px4 if kind == "px4" else ArduPilot)(exe, tmp)
    try:
        ap.start()
        paired = pair(bins, tmp, ap.pair_url, tmp)
        r = Rig(ap=ap, bins=bins, paired=paired, tmp=tmp, drone_plain=(HOST, free_udp_port()),
                ground_in=(HOST, free_udp_port()), ground_out=(HOST, free_udp_port()))
        if isinstance(ap, ArduPilot):
            ap.attach_bridge(r.drone_plain)
        ctx.log.write("sitl_ready", autopilot=kind, binary=str(exe))
        yield r
    finally:
        ap.stop()
        if ap.log.exists():  # the simulator's console: no keys in it
            shutil.copy(ap.log, ctx.run_dir / "logs" / ap.log.name)
        rm(tmp)
