"""The RF power sensor driver: the simulated backend, its refusals, and the real TCP and usbtmc paths against fakes."""

import os
import pty
import socket
import threading

import pytest

from oasa_lab.devices import DeviceError, Inventory, PowerSensor, Unavailable, make_device
from oasa_lab.devices import powersensor as ps
from oasa_lab.devices.inventory import DEFAULT_SIM


def sensor(**cfg):
    sim = cfg.pop("sim", {})
    dev = {"kind": "power_sensor", "model": "keysight-u2004a", "transport": "usbtmc", "port": "sim:p",
           "serial": "SIM-PSENS-0001", **cfg, "sim": sim}
    inv = Inventory.from_dict({"lab": {"backend": "sim"}, "devices": {"p": dev}}, "test")
    return make_device("p", inv, None, None)


def test_it_opens_on_the_built_in_sim_bench():
    inv = Inventory.from_dict(DEFAULT_SIM, "test", force_sim=True)
    assert inv.has("power_sensor")
    with make_device("psens0", inv, None, None) as dev:
        info = dev.info()
        assert dev.identify() == {"maker": "OASA", "model": "SIM-POWER-SENSOR", "serial": "SIM-PSENS-0001",
                                  "firmware": "0.1"}
    assert isinstance(dev, PowerSensor)
    assert (info["kind"], info["backend"], info["serial"], info["command_table"]) == \
        ("power_sensor", "sim", "SIM-PSENS-0001", "keysight-u2004a")
    assert info["commands_confirmed"] is False   # nothing beyond IEEE 488.2 is confirmed yet


def test_zero_frequency_and_read():
    with sensor(sim={"level_dbm": -27.5, "noise_db": 0.05, "seed": 3}) as dev:
        dev.zero()
        dev.set_frequency(915e6)
        assert dev.read_dbm(count=20) == pytest.approx(-27.5, abs=0.05)
        info = dev.info()
        assert info["zeroed"] is True and info["frequency_hz"] == 915e6
        sent = dev.port.written
        assert sent[:3] == ["*IDN?", "*CLS", "UNIT:POW DBM"]
        assert "SENS:FREQ 915000000" in sent and "CAL:ZERO:AUTO ONCE" in sent


def test_the_sensor_never_transmits():
    """Nothing in it asks the TX gate: it only reads."""
    with sensor() as dev:
        dev.tx_gate = lambda: "no transmit in this test"
        dev.zero()
        dev.set_frequency(920e6)
        dev.read_dbm()


def test_a_reading_needs_the_frequency_correction_first():
    with sensor() as dev:
        with pytest.raises(DeviceError, match="set_frequency"):
            dev.read_dbm()


@pytest.mark.parametrize("hz", [1e3, 6.1e9, float("nan"), -1])
def test_a_frequency_outside_the_model_range_is_refused(hz):
    with sensor() as dev:
        before = list(dev.port.written)
        with pytest.raises(DeviceError, match="outside"):
            dev.set_frequency(hz)
        assert dev.port.written == before


def test_sensor_refuses_a_reading_near_its_maximum_input():
    with sensor(max_input_dbm=10, sim={"level_dbm": 8.0, "noise_db": 0}) as dev:
        dev.set_frequency(915e6)
        with pytest.raises(DeviceError, match="Remove the signal"):
            dev.read_dbm()


def test_scpi_not_a_number_is_no_reading():
    with sensor() as dev:
        dev.set_frequency(915e6)
        dev.port.level_dbm = 9.91e37
        with pytest.raises(DeviceError, match="no valid reading"):
            dev.read_dbm()


def test_a_wrong_unit_or_a_bad_idn_is_refused():
    with pytest.raises(DeviceError, match="lab.toml expects"):
        sensor(serial="SIM-PSENS-0001", sim={"idn": "OASA,SIM-POWER-SENSOR,OTHER-1,0.1"}).open()
    with pytest.raises(DeviceError, match="maker,model,serial,firmware"):
        sensor(sim={"idn": "a power sensor"}).open()


def test_unknown_and_stub_models():
    with pytest.raises(DeviceError, match="model must be one of"):
        sensor(model="acme-9000").open()
    with pytest.raises(Unavailable, match="NRP-Z protocol"):
        sensor(model="rs-nrp-z11").open()
    with pytest.raises(DeviceError, match="transport must be"):
        sensor(transport="gpib").open()


def test_the_command_tables_claim_only_the_standard_commands():
    for name, t in ps.COMMANDS.items():
        assert {"idn", "clear", "opc", "unit_dbm", "zero", "freq", "read", "confirmed"} <= set(t), name
        assert set(t["confirmed"]) <= set(ps.IEEE4882), f"{name}: confirm a command against the guide before listing it"
        assert t["idn"] == "*IDN?" and t["opc"] == "*OPC?"
    assert not set(ps.STUB_MODELS) & set(ps.COMMANDS)


# --- the real transports against fakes -----------------------------------------------------------
class FakeScpi:
    """Answers like a sensor: *IDN?, *OPC? and READ?; records the rest."""

    def __init__(self, reading="-31.25"):
        self.reading, self.seen = reading, []

    def answer(self, line):
        self.seen.append(line)
        return {"*IDN?": "Maker,U2004A,MY00000000,A1.0", "*OPC?": "1", "READ?": self.reading}.get(line)


def test_tcp_transport_against_a_fake_server():
    fake = FakeScpi()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        buf = b""
        while True:
            data = conn.recv(4096)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                reply = fake.answer(line.decode())
                if reply is not None:
                    conn.sendall(reply.encode() + b"\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    inv = Inventory.from_dict({"devices": {"p": {"kind": "power_sensor", "model": "keysight-u2004a", "transport": "tcp",
                                                 "host": "127.0.0.1", "tcp_port": srv.getsockname()[1],
                                                 "serial": "MY00000000"}}}, "test")
    with make_device("p", inv, None, None) as dev:
        dev.set_frequency(2.4e9)
        assert dev.read_dbm() == pytest.approx(-31.25)
        assert dev.info()["backend"] == "hardware"
    srv.close()
    assert "SENS:FREQ 2400000000" in fake.seen


def test_usbtmc_transport_against_a_pty():
    """A pty stands in for /dev/usbtmc0: each write is a command, each read one reply."""
    fake = FakeScpi("-12.5")
    master, slave = pty.openpty()
    import tty
    tty.setraw(slave)
    stop = threading.Event()

    def serve():
        buf = b""
        while not stop.is_set():
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            buf += data
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                reply = fake.answer(line.decode())
                if reply is not None:
                    os.write(master, reply.encode())

    threading.Thread(target=serve, daemon=True).start()
    inv = Inventory.from_dict({"devices": {"p": {"kind": "power_sensor", "model": "generic", "transport": "usbtmc",
                                                 "port": os.ttyname(slave)}}}, "test")
    try:
        with make_device("p", inv, None, None) as dev:
            dev.set_frequency(915e6)
            assert dev.read_dbm() == pytest.approx(-12.5)
            assert dev.info()["serial"] == "MY00000000"
    finally:
        stop.set()
        os.close(slave)
        os.close(master)
