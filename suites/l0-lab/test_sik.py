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
        real = {**info, "backend": "hardware", "firmware": "n/a"}
        assert "firmware" in " ".join(_device_errors([real], None)[0])
        d.close()
    finally:
        world.close()
