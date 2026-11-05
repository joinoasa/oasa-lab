"""SiK radios: the AT client, the simulated radios, the driver and the checker's SiK rules. No hardware."""

import random
import time

import pytest

from oasa_lab.checker.core import SIK_LINK_KEYS, _device_errors
from oasa_lab.devices import Inventory, SikRadio, SimHub, SimWorld, make_device
from oasa_lab.devices import sikat
from oasa_lab.devices.inventory import DEFAULT_SIM
from oasa_lab.devices.simsik import SIM_VERSION, SimSikPort
from oasa_lab.siklink import wire_len
from oasa_lab.devices.framing import encode_frame

ATI5 = (b"ATI5\r\nS0:FORMAT=25\r\nS1:SERIAL_SPEED=57\r\nS2:AIR_SPEED=64\r\nS3:NETID=25\r\nS4:TXPOWER=20\r\n"
        b"S5:ECC=0\r\nS6:MAVLINK=1\r\nS15:MAX_WINDOW=131\r\n")


def test_link_keys_are_the_same_in_the_checker_and_the_lab():
    assert SIK_LINK_KEYS == sikat.LINK_KEYS


def test_ati5_parses_and_refuses_what_is_not_a_setting():
    s, regs = sikat.parse_ati5(ATI5)
    assert s["NETID"] == 25 and regs["MAX_WINDOW"] == 15 and len(s) == 8
    s, _ = sikat.parse_ati5(b"\x00\xfe junk from the air\nATI5\nS3:NETID=7\nOK\n")
    assert s == {"NETID": 7}
    for bad in (b"", b"ATI5\r\n", b"ATI5\r\nS3:NETID=1\r\nhello\r\n", b"ATI5\r\nS3:NETID=1\r\nS3:NETID=2\r\n",
                b"ATI5\r\nS3:NETID=-1\r\n", b"ATI5\r\nS3:netid=1\r\n", b"ATI5\r\nS3:NETID=1\xff\r\n"):
        with pytest.raises(sikat.AtError):
            sikat.parse_ati5(bad)


def test_errors_never_show_bytes_from_the_radio():
    with pytest.raises(sikat.AtError) as e:
        sikat.parse_ati5(b"ATI5\r\nOASA-MARK-0123456789\r\nS3:NETID=1\r\n")
    assert "MARK" not in str(e.value)
    rng = random.Random(3)
    for _ in range(3000):
        r = bytes(rng.choice(b"S0123456789:=NETIDOK_\r\n ATI5\xff\x00") for _ in range(rng.randrange(200)))
        try:
            sikat.parse_ati5(r)
        except sikat.AtError as exc:
            assert len(str(exc)) < 200


def test_expect_arg_matches_oasa_radio():
    assert sikat.expect_arg({"NETID": 7, "AIR_SPEED": 64}) == "air_speed=64,netid=7"
    assert sikat.normalize({"netid": 7, "air-speed": "64"}) == {"NETID": 7, "AIR_SPEED": 64}


def test_wire_len_is_the_longest_frame():
    for n in (1, 16, 242, 250, 300):
        assert len(encode_frame(b"\x01" * n, 400)) <= wire_len(n)
        assert len(encode_frame(b"\x00" * n, 400)) <= wire_len(n)
    assert wire_len(242) == 249   # fits one 250-byte SiK packet


@pytest.fixture
def pair():
    w = SimWorld(1)
    air = w.get_sik_air()
    ports = []

    def make(name, **settings):
        p = SimSikPort(name, air, {"NETID": 4242, "MAVLINK": 0, **settings})
        ports.append(p)
        return p
    yield make
    w.close()


def read_for(port, s):
    end, out = time.time() + s, b""
    while time.time() < end:
        out += port.read(0.05)
    return out


def test_sim_reads_settings_and_goes_back_to_data_mode(pair):
    a, b = pair("a"), pair("b")
    r = sikat.read_report(a)
    assert r.version == SIM_VERSION and r.settings["NETID"] == 4242 and r.settings["MAVLINK"] == 0
    assert not a.at_mode
    a.write(b"\x00data\x00")
    assert read_for(b, 0.4).endswith(b"\x00data\x00")


def test_sim_needs_the_guard_time(pair):
    a, b = pair("a"), pair("b")
    a.write(b"x+++")                 # no second of silence before: data, not an escape
    time.sleep(1.4)
    assert not a.at_mode
    assert read_for(b, 0.3) == b"x+++"
    time.sleep(1.1)
    a.write(b"+++")
    time.sleep(0.5)
    a.write(b"y")                    # a byte inside the second guard cancels it
    time.sleep(1.3)
    assert not a.at_mode


def test_sim_radios_on_different_settings_do_not_hear_each_other(pair):
    a, b = pair("a"), pair("b", NETID=1)
    a.write(b"\x00hello\x00")
    assert read_for(b, 0.4) == b""
    with pytest.raises(sikat.AtError):
        sikat.remote(b, ["RTS4=1"])   # no link, no answer
    assert a.params["TXPOWER"] == 20


def test_sim_remote_commands_change_the_other_radio(pair):
    """H1 in miniature: RT needs nothing but the same air settings."""
    a, b = pair("a"), pair("b")
    sikat.remote(b, ["RTS4=11"])
    assert a.params["TXPOWER"] == 11 and a.saved["TXPOWER"] == 20
    with pytest.raises(sikat.AtError):
        sikat.remote(b, ["ATS4=1"])  # not a remote command


def test_driver_records_firmware_and_settings_and_refuses_no_sik():
    inv = Inventory.from_dict(DEFAULT_SIM, "test", force_sim=True)
    world = SimWorld(2, inv.wiring)
    try:
        d = make_device("sik_a", inv, SimHub(inv.hub), world).open()
        assert isinstance(d, SikRadio)
        info = d.info()
        assert info["kind"] == "sik" and info["firmware"] == SIM_VERSION
        assert info["settings"]["NETID"] == 4242 and info["expect"]["NETID"] == 4242
        assert not _device_errors([info], None)[0]
        bad = {**info, "settings": {**info["settings"], "NETID": 25}}
        assert "did not have the declared settings" in " ".join(_device_errors([bad], None)[0])
        other = {**info, "id": "sik_b", "expect": {}, "settings": {**info["settings"], "AIR_SPEED": 128}}
        assert "cannot link: AIR_SPEED" in " ".join(_device_errors([info, other], None)[0])
        mav = {**info, "expect": {}, "settings": {**info["settings"], "MAVLINK": 1}}
        assert "MAVLINK" in " ".join(_device_errors([mav], None)[0])
        assert "record its settings" in " ".join(_device_errors([{**info, "settings": {}}], None)[0])
        no_mav = {k: v for k, v in info["settings"].items() if k != "MAVLINK"}
        assert "no MAVLINK" in " ".join(_device_errors([{**info, "expect": {}, "settings": no_mav}], None)[0])
        real = {**info, "backend": "hardware", "serial": "A1", "firmware": "SiK 2.2 on HM-TRP"}
        assert not _device_errors([real], None)[0]
        assert "at least NETID and MAVLINK" in " ".join(_device_errors([{**real, "expect": {}}], None)[0])
        assert "at least NETID and MAVLINK" in " ".join(_device_errors([{**real, "expect": {"NETID": 4242}}], None)[0])
        real = {**info, "backend": "hardware", "firmware": "n/a"}
        assert "firmware" in " ".join(_device_errors([real], None)[0])
        d.close()
    finally:
        world.close()


def test_a_radio_that_starts_mismatched_fails_the_settings_check():
    """The simulated radios start from lab.toml's expectation; a [sim] table makes one differ. Then the suite's
    check and the checker must both say no."""
    from oasa_lab.siklink import settings_problems
    import copy
    data = copy.deepcopy(DEFAULT_SIM)
    data["devices"]["sik_b"]["sim"] = {"NETID": 25, "MAVLINK": 1}
    inv = Inventory.from_dict(data, "test", force_sim=True)
    world = SimWorld(3, inv.wiring)
    try:
        a = make_device("sik_a", inv, SimHub(inv.hub), world).open()
        b = make_device("sik_b", inv, SimHub(inv.hub), world).open()
        problems = " ".join(settings_problems(a, b))
        assert "sik_b does not have the settings" in problems and "MAVLINK framing must be off" in problems
        assert "NETID is the SiK default" in problems and "cannot link: ['NETID']" in problems
        errs = " ".join(_device_errors([a.info(), b.info()], None)[0])
        assert "did not have the declared settings" in errs and "cannot link: NETID" in errs
        assert not settings_problems(a, a)
    finally:
        world.close()


def test_lower_setting_never_raises_power():
    from oasa_lab.siklink import lower_setting
    assert lower_setting({"TXPOWER": 20}) == ("TXPOWER", 17)
    assert lower_setting({"TXPOWER": 11}) == ("TXPOWER", 8)
    assert lower_setting({"TXPOWER": 2}) == ("TXPOWER", 1)
    assert lower_setting({"TXPOWER": 1, "DUTY_CYCLE": 100}) == ("DUTY_CYCLE", 90)


def test_reboot_unsaved_puts_back_a_remote_change(pair):
    a, b = pair("a"), pair("b")
    sikat.remote(b, ["RTS4=11"])
    assert a.params["TXPOWER"] == 11
    sikat.reboot_unsaved(a)
    time.sleep(1.2)
    assert sikat.read_report(a).settings["TXPOWER"] == 20 and a.reboots == 1


def test_ok_after_air_bytes_and_banner():
    assert sikat.confirmed_at(b"AT\r\nOK\r\n")
    assert not sikat.confirmed_at(b"\x9f\x01air bytesOK\r\n")    # an OK without our echo is not ours
    assert not sikat.confirmed_at(b"AT\r\nOK")                     # not a finished line yet
    assert sikat.has_line(b"ATO\r\n", "ATO") and not sikat.has_line(b"ATO", "ATO")
    assert sikat.parse_ati(b"ATI\r\nOK\r\nRFD SiK 2.65 on RFD900X\r\n") == "RFD SiK 2.65 on RFD900X"
    with pytest.raises(sikat.AtError):
        sikat.parse_ati(b"ATI\r\nsomething else\r\n")


def test_a_peer_forging_replies_cannot_fake_command_mode(pair):
    """A hostile host behind radio B answers whatever AT traffic it hears from the air, at once. Our +++ goes on
    the air, so it hears that and sends OK; nothing after it may be ours, and the settings read are A's real ones."""
    import threading
    a, b = pair("a", NETID=25), pair("b", NETID=25)
    seen, stop = bytearray(), threading.Event()

    def forge():
        while not stop.is_set():
            got = b.read(0.02)
            if not got:
                continue
            seen.extend(got)
            reply = b""
            if b"+++" in got:
                reply += b"OK\r\n"
            if b"AT\r" in got:
                reply += b"AT\r\nOK\r\n"
            if b"ATI\r" in got:
                reply += b"ATI\r\nSiK 2.2 on FORGED\r\n"
            if b"ATI5\r" in got:
                reply += b"ATI5\r\nS3:NETID=4242\r\nS6:MAVLINK=0\r\n"
            if b"ATO\r" in got:
                reply += b"ATO\r\n"
            if reply:
                b.write(reply)
    t = threading.Thread(target=forge, daemon=True)
    t.start()
    try:
        r = sikat.read_report(a)
    finally:
        stop.set()
        t.join(2)
    assert r.version == SIM_VERSION and r.settings["NETID"] == 25
    assert b"+++" in seen and b"AT" not in bytes(seen), bytes(seen)
    assert not a.at_mode


@pytest.mark.parametrize("i", range(10))
def test_back_in_data_mode_every_time(pair, i):
    """ATO counts only with its full echo, and the client waits after it: the radio is in data mode on return."""
    a, b = pair("a"), pair("b")
    sikat.read_report(a)
    assert not a.at_mode
