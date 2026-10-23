"""Real PX4 and ArduPilot simulators, reached only through the encrypted oasa-sec link.

    autopilot SITL <-> drone oasa-link <-> simulated channel (5% loss, captured) <-> ground oasa-link
        <-> pymavlink "ground station" (this test)

The MAVLink signing key is paired with oasa-pair, so signing runs inside the link as a second layer.
The drone-side link has the MAVLink guard on (its device folder holds the autopilot key).
Every test runs once per autopilot; see conftest.py. Without the binaries, simulators or pymavlink
every test is SKIPPED with the reason, and the checker then fails the run (the guard tests are
listed in `expectations`)."""

import os
import time

import pytest

from oasa_lab.pcap import read_pcap
from oasa_lab.sitl import HOST, Links, Station
from oasa_lab.testing import free_udp_port

NAN = float("nan")
LOSS = 0.05

# MAVLink message and command numbers used below.
ATTITUDE, GLOBAL_POSITION_INT = 30, 33
CMD_DO_SET_MODE, CMD_NAV_LAND, CMD_NAV_TAKEOFF, CMD_ARM_DISARM = 176, 21, 22, 400
FLAG_ARMED = 128


def capture_of(lab):
    return read_pcap(lab.ctx.run_dir / lab.ctx.captures[-1]["file"])


def autopilot_heartbeats(st, since=0.0):
    return [m for _, m in st.of_type("HEARTBEAT", since) if m.get_srcSystem() == 1 and m.type != 6]


def armed(st) -> bool | None:
    hb = autopilot_heartbeats(st)
    return bool(hb[-1].base_mode & FLAG_ARMED) if hb else None


def rel_alt_m(st) -> float | None:
    m = st.last("GLOBAL_POSITION_INT")
    return None if m is None else m.relative_alt / 1000.0


def wait_established_and_alive(st, timeout=30):
    assert st.wait_for("HEARTBEAT", timeout, pred=lambda m: m.get_srcSystem() == 1), "no autopilot heartbeat"


# --- 1. telemetry ------------------------------------------------------------------------------
def test_telemetry_through_the_link(lab, rig):
    with Links(lab, rig, loss=LOSS) as lk:
        st = lk.station
        wait_established_and_alive(st)
        st.request_interval(ATTITUDE, 10)
        st.request_interval(GLOBAL_POSITION_INT, 5)
        time.sleep(2)
        secs = 15
        t0 = time.monotonic()
        time.sleep(secs)
        hb = len(autopilot_heartbeats(st, t0)) / secs
        att = len(st.of_type("ATTITUDE", t0)) / secs
        pos = len(st.of_type("GLOBAL_POSITION_INT", t0)) / secs
        lk.finish()
    lab.log("telemetry_rates", heartbeat_hz=round(hb, 2), attitude_hz=round(att, 2), position_hz=round(pos, 2))
    # 1 Hz, 10 Hz and 5 Hz asked for, 5% lost on the way (and a little slack for timing).
    assert 0.6 <= hb <= 1.3, f"heartbeat {hb:.2f} Hz"
    assert 7.0 <= att <= 12.5, f"attitude {att:.2f} Hz (10 Hz asked for)"
    assert 3.5 <= pos <= 6.5, f"position {pos:.2f} Hz (5 Hz asked for)"
    assert lk.stats["drone"] and lk.stats["drone"]["sent"] > 100


# --- 2. signed commands ---------------------------------------------------------------------------
def test_signed_commands_work(lab, rig):
    with Links(lab, rig, loss=LOSS) as lk:
        st = lk.station
        wait_established_and_alive(st)
        assert st.probe("own", window=15), "a command signed with the paired key got no answer"
        res = st.command(512, 148, timeout=15)  # REQUEST_MESSAGE(AUTOPILOT_VERSION)
        assert res == 0, f"signed REQUEST_MESSAGE was not accepted (result {res})"
        assert st.wait_for("AUTOPILOT_VERSION", 10, since=0.0) is not None
        lk.finish()
    assert lk.blocked() == 0, "the guard dropped ordinary traffic"


def test_unsigned_and_wrong_key_commands_are_ignored(lab, rig):
    wrong = os.urandom(32)
    with Links(lab, rig, loss=LOSS) as lk:
        st = lk.station
        wait_established_and_alive(st)
        assert st.probe("own", window=15), "control: the paired key must work"
        assert not st.probe(None, window=6), "an unsigned command was answered"
        assert not st.probe(wrong, window=6), "a command signed with the wrong key was answered"
        assert st.probe("own", window=10), "control: the paired key must still work"
        lk.finish()


# --- 3. fly ------------------------------------------------------------------------------------------
def test_fly_arm_takeoff_hold_land_disarm(lab, rig):
    px4 = rig.ap.kind == "px4"
    with Links(lab, rig, loss=LOSS) as lk:
        st = lk.station
        wait_established_and_alive(st)
        st.request_interval(GLOBAL_POSITION_INT, 5)
        assert st.wait_for("GLOBAL_POSITION_INT", 20) is not None
        ground_amsl = st.last("GLOBAL_POSITION_INT").alt / 1000.0

        if not px4:  # ArduCopter: GUIDED mode (custom mode 4) takes the commands below
            assert st.command(CMD_DO_SET_MODE, 1, 4, timeout=15) == 0, "could not set GUIDED"
        # Arm. Pre-arm checks need a GPS fix and a settled estimator; try for up to two minutes.
        end = time.monotonic() + 120
        res = None
        while time.monotonic() < end:
            res = st.command(CMD_ARM_DISARM, 1, timeout=8)
            if res == 0 and wait_armed(st, True, 10):
                break
            if not px4 and time.monotonic() > end - 40:  # ArduPilot only: skip pre-arm checks if it still refuses
                st.command(CMD_ARM_DISARM, 1, 2989, timeout=8)
                if wait_armed(st, True, 10):
                    break
            time.sleep(2)
        assert armed(st), f"the autopilot did not arm (last result {res})"
        lab.log("armed")

        # Take off to 10 m (PX4 wants the altitude above sea level, ArduPilot above home).
        if px4:
            res = st.command(CMD_NAV_TAKEOFF, NAN, 0, 0, NAN, NAN, NAN, ground_amsl + 10, timeout=15)
        else:
            res = st.command(CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 10, timeout=15)
        assert res == 0, f"takeoff was not accepted (result {res})"
        peak = 0.0
        end = time.monotonic() + 60
        while time.monotonic() < end and peak < 9.0:
            peak = max(peak, rel_alt_m(st) or 0.0)
            time.sleep(0.2)
        lab.log("climb", peak_m=round(peak, 1))
        assert peak >= 9.0, f"only reached {peak:.1f} m"
        # Hold: stay near 10 m for 6 s.
        hold_end = time.monotonic() + 6
        lows, highs = 99.0, 0.0
        while time.monotonic() < hold_end:
            a = rel_alt_m(st) or 0.0
            lows, highs = min(lows, a), max(highs, a)
            time.sleep(0.2)
        lab.log("hold", low_m=round(lows, 1), high_m=round(highs, 1))
        assert lows >= 8.0 and highs <= 13.0, f"did not hold near 10 m ({lows:.1f} to {highs:.1f})"

        # Land.
        if px4:
            res = st.command(CMD_NAV_LAND, 0, 0, 0, NAN, NAN, NAN, NAN, timeout=15)
        else:
            res = st.command(CMD_DO_SET_MODE, 1, 9, timeout=15)  # LAND
        assert res == 0, f"land was not accepted (result {res})"
        landed = wait_for_value(lambda: (rel_alt_m(st) or 99) < 0.6, 120)
        lab.log("landed", ok=landed, alt_m=rel_alt_m(st))
        assert landed, f"still at {rel_alt_m(st)} m"
        if not wait_armed(st, False, 40):  # both autopilots disarm on their own after landing; help if not
            st.command(CMD_ARM_DISARM, 0, timeout=10)
        assert wait_armed(st, False, 20), "still armed after landing"
        lk.finish()
    assert lk.blocked() == 0, "the guard dropped flight commands"


def wait_for_value(cond, timeout, step=0.3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def wait_armed(st, want: bool, timeout: float) -> bool:
    return wait_for_value(lambda: armed(st) is want, timeout)


# --- 4. the guard --------------------------------------------------------------------------------------
def test_guard_drops_signed_setup_signing(lab, rig):
    """Known-bad case: the ground station (it holds the key) tells the autopilot a new key, signed, through
    the tunnel. The drone-side guard must drop it, and the autopilot's key must stay the old one."""
    new_key = os.urandom(32)
    with Links(lab, rig, loss=LOSS) as lk:
        st = lk.station
        wait_established_and_alive(st)
        assert st.probe("own", window=15), "control: the paired key works before the attempt"
        ts = int((time.time() - 1420070400) * 100000)
        for _ in range(6):  # a few copies, in case the channel loses some
            st.send("setup_signing_encode", 1, 1, list(new_key), ts)
            time.sleep(0.3)
        blocked = wait_for_value(lambda: lk.blocked() >= 1, 10)
        assert blocked, "the guard did not drop the SETUP_SIGNING"
        assert any(e.get("why") == "setup_signing" and e.get("msgid") == 256 for e in lk.drone.seen("drop"))
        time.sleep(1)
        assert st.probe("own", window=15), "the old key stopped working: the autopilot's key changed"
        assert not st.probe(new_key, window=8), "the new key works: SETUP_SIGNING reached the autopilot"
        lk.finish()
    assert lk.stats["drone"]["drops"].get("mavlink_blocked", 0) >= 1


def test_guard_drops_ftp(lab, rig):
    """MAVLink FTP (message 110) is dropped; with --allow-mavlink-ftp the autopilot answers it."""
    with Links(lab, rig, loss=LOSS) as lk:
        st = lk.station
        wait_established_and_alive(st)
        assert st.probe("own", window=15)
        t0 = time.monotonic()
        for i in range(5):
            st.send("file_transfer_protocol_encode", 0, 1, 1, ftp_list_root(i))
            time.sleep(0.4)
        assert wait_for_value(lambda: lk.blocked() >= 1, 10), "the guard did not drop FTP"
        assert any(e.get("why") == "ftp" and e.get("msgid") == 110 for e in lk.drone.seen("drop"))
        time.sleep(1.5)
        assert not st.of_type("FILE_TRANSFER_PROTOCOL", t0), "the autopilot answered an FTP request"
        lk.finish()
    assert lk.stats["drone"]["drops"].get("mavlink_blocked", 0) >= 1


def ftp_list_root(seq: int) -> list[int]:
    """FTP payload: ListDirectory (opcode 3) of "/"."""
    body = bytearray(251)
    body[0:2] = seq.to_bytes(2, "little")
    body[3] = 3  # ListDirectory
    body[4] = 1  # size of the path
    body[12] = ord("/")
    return list(body)


def test_ftp_passes_when_allowed(lab, rig):
    """Control for the test above: the same request gets an answer when the drone allows FTP."""
    with Links(lab, rig, loss=LOSS, drone_flags=["--allow-mavlink-ftp"]) as lk:
        st = lk.station
        wait_established_and_alive(st)
        assert st.probe("own", window=15)
        t0 = time.monotonic()
        got = None
        for i in range(10):
            st.send("file_transfer_protocol_encode", 0, 1, 1, ftp_list_root(i))
            got = st.wait_for("FILE_TRANSFER_PROTOCOL", 1.5, since=t0)
            if got:
                break
        lk.finish()
    assert got is not None, "no FTP answer even though FTP was allowed"
    assert lk.blocked() == 0


def test_guard_drops_signing_param_set(lab, rig):
    """A PARAM_SET of a signing setting is dropped; an ordinary PARAM_SET still works.

    ArduPilot: MAV2_OPTIONS bit 0 turns signing off for SERIAL1 at the next restart (docs/sitl.md).
    PX4 has no signing parameter, so the name is only a stand-in; the guard still drops it."""
    px4 = rig.ap.kind == "px4"
    bad = "MAV_SIGN_CFG" if px4 else "MAV2_OPTIONS"
    ok_name, ok_val = ("MPC_XY_VEL_MAX", 11.0) if px4 else ("WP_SPD", 6.0)
    real = 9  # MAV_PARAM_TYPE_REAL32
    with Links(lab, rig, loss=LOSS) as lk:
        st = lk.station
        wait_established_and_alive(st)
        assert st.probe("own", window=15)
        # An ordinary parameter first: must be changed and echoed back.
        echoed = None
        for _ in range(8):
            st.send("param_set_encode", 1, 1, ok_name.encode(), ok_val, real)
            echoed = st.wait_for("PARAM_VALUE", 1.5, pred=lambda m: m.param_id == ok_name)
            if echoed:
                break
        assert echoed is not None, f"PARAM_SET of {ok_name} got no answer through the guard"
        assert abs(echoed.param_value - ok_val) < 1e-3
        assert lk.blocked() == 0, "the guard dropped an ordinary PARAM_SET"
        # The signing setting: dropped.
        for _ in range(5):
            st.send("param_set_encode", 1, 1, bad.encode(), 1.0, real if px4 else 6)
            time.sleep(0.3)
        for _ in range(3):  # and the extended message
            st.send("param_ext_set_encode", 1, 1, bad.encode(), b"1", 6)
            time.sleep(0.3)
        assert wait_for_value(lambda: lk.blocked() >= 1, 10), "the guard did not drop the signing PARAM_SET"
        assert any(e.get("why") == "signing_param" for e in lk.drone.seen("drop"))
        time.sleep(1.5)
        if not px4:  # the setting did not change
            t0 = time.monotonic()
            val = None
            for _ in range(8):
                st.send("param_request_read_encode", 1, 1, bad.encode(), -1)
                val = st.wait_for("PARAM_VALUE", 1.5, since=t0, pred=lambda m: m.param_id == bad)
                if val:
                    break
            assert val is not None, f"could not read {bad} back"
            assert val.param_value == 0, f"{bad} is {val.param_value}: the PARAM_SET got through"
        assert st.probe("own", window=10)
        lk.finish()
    assert lk.stats["drone"]["drops"].get("mavlink_blocked", 0) >= 1


# --- 5. what is on the wire -------------------------------------------------------------------------------
def test_captures_hold_no_plaintext(lab, rig):
    """A marker sent as a MAVLink text message must not be readable between the two links. The checker
    scans the whole capture again (markers, MAVLink frames, randomness)."""
    marker = lab.marker()
    with Links(lab, rig, loss=LOSS) as lk:
        st = lk.station
        wait_established_and_alive(st)
        for _ in range(20):
            st.send("statustext_encode", 6, marker.encode()[:50])
            time.sleep(0.2)
        time.sleep(3)
        lk.finish()
    pk = capture_of(lab)
    assert len(pk) > 100, "the capture should hold the encrypted traffic"
    assert not any(marker.encode() in p.payload for p in pk)
    assert not any(p.payload[:1] == b"\xfd" and len(p.payload) < 300 and b"OASA-MARK" in p.payload for p in pk)


def test_negative_control_sees_autopilot_mavlink(lab, rig):
    """The same autopilot, with the same ground station, over a channel but WITHOUT the link. The suite
    declares this capture a control: the checker must FIND MAVLink frames in it, which proves its scanner
    sees real autopilot traffic and that the encrypted captures' clean result means something."""
    px4 = rig.ap.kind == "px4"
    a_addr = (HOST, free_udp_port())  # the station
    if px4:
        b_addr = rig.ap.plain_send()  # PX4's onboard port, reached straight through the channel
    else:
        b_addr = rig.ap.plain_send()  # the TCP bridge's UDP side
    ch = lab.channel(a=a_addr, b=b_addr, control=True, loss=0.0, delay_ms=5)
    bridge = getattr(rig.ap, "bridge", None)
    old_to = bridge.to if bridge else None
    if bridge:
        bridge.to = ch.addr_b  # frames from ArduPilot enter the channel, not the link
    st = Station(a_addr, ch.addr_a, rig.paired.key)
    try:
        assert st.wait_for("HEARTBEAT", 30, pred=lambda m: m.get_srcSystem() == 1), "no autopilot heartbeat"
        st.request_interval(ATTITUDE, 10)
        time.sleep(6)
    finally:
        st.close()
        if bridge:
            bridge.to = old_to
        ch.stop()
    pk = capture_of(lab)
    frames = sum(1 for p in pk if p.payload[:1] in (b"\xfd", b"\xfe"))
    lab.log("control", packets=len(pk), mavlink_packets=frames)
    assert len(pk) > 50
    assert frames > 0.8 * len(pk), f"only {frames} of {len(pk)} packets look like MAVLink frames"
