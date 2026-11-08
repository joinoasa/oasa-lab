"""The bench calibration file: every validation rule, interpolation, and lab.toml's [calibration] table."""

import tomllib
from pathlib import Path

import pytest

from oasa_lab import calibration as cal
from oasa_lab.calibration import CalibrationError
from oasa_lab.devices import Inventory, InventoryError
from oasa_lab.suites import repo_root

EXAMPLE = repo_root() / "calibration.example.toml"


def good() -> dict:
    return tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))


def problems(edit) -> list[str]:
    d = good()
    edit(d)
    return cal.validate(d)


def test_the_example_file_is_valid():
    assert cal.validate(good()) == []
    c = cal.load(EXAMPLE)
    assert c.date == "2026-01-15" and len(c.sha256) == 64
    assert c.manifest_entry() == {"sha256": c.sha256, "date": "2026-01-15", "version": 1, "valid": True, "used": False}


def _set(path, value):
    def edit(d):
        t = d
        for k in path[:-1]:
            t = t[k]
        t[path[-1]] = value
    return edit


def _del(path):
    def edit(d):
        t = d
        for k in path[:-1]:
            t = t[k]
        del t[path[-1]]
    return edit


# Each case breaks one rule of the bench validator (version 1, axis lengths, losses, cert age, serials, TX spread).
BAD = [
    ("version", _set(["version"], 2), "version must be 1"),
    ("date", _set(["date"], "15/01/2026"), "date must be YYYY-MM-DD"),
    ("cert date", _set(["power_sensor", "cal_cert_date"], "soon"), "cal_cert_date must be YYYY-MM-DD"),
    ("old cert", _set(["power_sensor", "cal_cert_date"], "2024-12-01"), "older than 12 months"),
    ("serial in clear", _set(["power_sensor", "serial"], "MY12345678"), "serial in clear"),
    ("serial-like field", _set(["power_sensor", "label"], "ABCDEF1234"), "look like serials"),
    ("vna serial", _set(["vna", "serial"], "X1"), "vna.serial in clear"),
    ("no paths", _set(["paths"], {}), "no [paths.*] tables"),
    ("loss length", _set(["paths", "b200_to_e310", "loss_db"], [60.4, 60.4]), "loss_db has 2 values for 5 frequencies"),
    ("phase length", _set(["paths", "b200_to_e310", "phase_deg"], [1.0]), "phase_deg has 1 values for 5"),
    ("loss not positive", _set(["paths", "b200_to_e310", "loss_db"], [60.4, 60.4, 0.0, 61.1, 62.3]), "is not positive"),
    ("under 60 dB", _set(["paths", "e310_to_b200", "loss_db"], [60.5, 59.9, 61.1, 61.2, 62.4]),
     "under the 60 dB minimum between transmitters"),
    ("under 40 dB rx-only", _set(["paths", "b200_to_sensor", "loss_db"], [40.3, 39.0]), "under 40 dB toward a receive-only"),
    ("rx-only flag off", _del(["paths", "b200_to_sensor", "receive_only"]), "under the 60 dB minimum"),
    ("empty route", _set(["paths", "b200_to_e310", "route"], []), "route is empty"),
    ("tx rows", _set(["radios", "b200", "tx", "dbm_out"], [[0.0] * 10]), "dbm_out must be 2 rows x 10 gains"),
    ("tx columns", _set(["radios", "b200", "tx", "dbm_out"], [[0.0] * 10, [0.0] * 9]), "dbm_out must be 2 rows x 10"),
    ("rx columns", _set(["radios", "b200", "rx", "floor_dbfs"], [[0.0] * 5, [0.0] * 4]), "floor_dbfs must be 2 rows x 5"),
    ("tx spread", _set(["radios", "b200", "tx", "spread_db_max"], 0.3), "repeat the measurement"),
    ("tx spread missing", _del(["radios", "b200", "tx", "spread_db_max"]), "repeat the measurement"),
    ("tx falls", _set(["radios", "b200", "tx", "dbm_out"], [[-60.0, -50.1, -40.2, -30.3, -20.5, -21.5, -1.2, 7.5, 12.0, 13.5],
                                                             [-60.1, -50.2, -40.3, -30.4, -20.6, -10.9, -1.3, 7.4, 11.9, 13.4]]),
     "falls as gain rises"),
    # Beyond the bench validator: what interpolation needs.
    ("freq not rising", _set(["paths", "b200_to_e310", "freq_mhz"], [915, 920, 2450, 2400, 5800]), "rising numbers"),
    ("gain not rising", _set(["radios", "b200", "rx", "gain_db"], [0, 20, 20, 60, 76]), "rising numbers"),
    ("loss not a number", _set(["paths", "b200_to_e310", "loss_db"], [60.4, "x", 61.0, 61.1, 62.3]), "must be numbers"),
    ("path not a table", _set(["paths", "b200_to_e310"], 5), "must be a table"),
]


@pytest.mark.parametrize("name,edit,expect", BAD, ids=[b[0] for b in BAD])
def test_bad_calibration_files_are_refused(name, edit, expect):
    bad = problems(edit)
    assert any(expect in b for b in bad), bad


def test_a_file_that_is_not_toml_is_refused(tmp_path):
    p = tmp_path / "calibration.toml"
    p.write_text("version = = 1\n")
    with pytest.raises(CalibrationError, match="cannot read"):
        cal.load(p)
    with pytest.raises(CalibrationError, match="cannot read"):
        cal.load(tmp_path / "missing.toml")


def test_parse_lists_every_problem(tmp_path):
    text = EXAMPLE.read_text().replace("version = 1", "version = 3").replace("spread_db_max = 0.15", "spread_db_max = 0.5")
    with pytest.raises(CalibrationError) as exc:
        cal.parse(text.encode())
    assert len(exc.value.problems) == 2


def test_an_unquoted_toml_date_is_accepted():
    text = EXAMPLE.read_text().replace('date = "2026-01-15"', "date = 2026-01-15")
    assert cal.parse(text.encode()).date == "2026-01-15"


def test_a_calibration_older_than_the_last_recabling_is_refused():
    raw = EXAMPLE.read_bytes()
    assert cal.parse(raw, recabled="2026-01-15").date == "2026-01-15"
    with pytest.raises(CalibrationError, match="older than the last re-cabling"):
        cal.parse(raw, recabled="2026-02-01")


# --- interpolation ---------------------------------------------------------------------------
def test_path_loss_is_interpolated_between_measured_frequencies():
    c = cal.load(EXAMPLE)
    assert c.path_loss_db("b200_to_e310", 915) == 60.4
    assert c.path_loss_db("b200_to_e310", 2425) == pytest.approx(61.05)
    assert c.path_loss_db("b200_to_e310", 5800) == 62.3
    for f in (900, 5801):
        with pytest.raises(CalibrationError, match="outside the measured range"):
            c.path_loss_db("b200_to_e310", f)
    with pytest.raises(CalibrationError, match="no \\[paths.nope\\]"):
        c.path_loss_db("nope", 915)


def test_tx_power_is_interpolated_over_gain_and_frequency():
    c = cal.load(EXAMPLE)
    assert c.tx_dbm("b200", 40, 915) == -20.5
    assert c.tx_dbm("b200", 45, 915) == pytest.approx((-20.5 - 10.8) / 2)
    assert c.tx_dbm("b200", 40, 917.5) == pytest.approx(-20.55)
    with pytest.raises(CalibrationError, match="outside"):
        c.tx_dbm("b200", 90, 915)
    with pytest.raises(CalibrationError, match="no \\[radios.e310.tx\\]"):
        c.tx_dbm("e310", 10, 915)


def test_rx_dbfs_becomes_dbm():
    c = cal.load(EXAMPLE)
    assert c.rx_dbm("b200", -20.0, 40, 915) == pytest.approx(-70.0)
    assert c.rx_dbm("b200", -20.0, 30, 917.5) == pytest.approx(-20.0 + (-40.0 - 40.1) / 2)
    assert c.rx_floor_dbfs("e310", 70, 920) == -72.0


def test_interp_edge_cases():
    assert cal.interp([915.0], [3.0], 915) == 3.0
    with pytest.raises(CalibrationError):
        cal.interp([], [], 1)


# --- lab.toml [calibration] ------------------------------------------------------------------
def test_lab_toml_path_is_relative_to_lab_toml(tmp_path):
    (tmp_path / "lab.toml").write_text('[calibration]\npath = "cal/calibration.toml"\nrecabled = "2026-01-10"\n')
    inv = Inventory.load(tmp_path / "lab.toml", sim=False)
    assert inv.calibration == {"path": str(tmp_path / "cal" / "calibration.toml"), "recabled": "2026-01-10"}
    with pytest.raises(CalibrationError, match="cannot read"):
        cal.load_configured(inv)
    assert cal.manifest_entry_for(inv) is None   # configured but not there: nothing to record


def test_no_calibration_section_means_none(tmp_path):
    (tmp_path / "lab.toml").write_text('[lab]\nname = "x"\n')
    inv = Inventory.load(tmp_path / "lab.toml", sim=False)
    assert inv.calibration == {} and cal.load_configured(inv) is None and cal.manifest_entry_for(inv) is None


@pytest.mark.parametrize("text,msg", [
    ("[calibration]\n", "path must be a file name"),
    ('[calibration]\npath = 5\n', "path must be a file name"),
    ('[calibration]\npath = "c.toml"\nrecabled = "last week"\n', "recabled must be YYYY-MM-DD"),
    ('[calibration]\npath = "c.toml"\nmax_age = 3\n', "unknown keys"),
    ('calibration = "c.toml"\n', "must be a table"),
])
def test_bad_calibration_tables_in_lab_toml_are_refused(tmp_path, text, msg):
    (tmp_path / "lab.toml").write_text(text)
    with pytest.raises(InventoryError, match=msg):
        Inventory.load(tmp_path / "lab.toml", sim=False)


def test_manifest_entry_of_a_configured_file(tmp_path):
    (tmp_path / "calibration.toml").write_bytes(EXAMPLE.read_bytes())
    (tmp_path / "lab.toml").write_text('[calibration]\npath = "calibration.toml"\n')
    inv = Inventory.load(tmp_path / "lab.toml", sim=False)
    e = cal.manifest_entry_for(inv)
    assert e == {"sha256": cal.load(EXAMPLE).sha256, "date": "2026-01-15", "version": 1, "valid": True, "used": False}
    (tmp_path / "calibration.toml").write_text(EXAMPLE.read_text().replace("spread_db_max = 0.15", "spread_db_max = 0.9"))
    e = cal.manifest_entry_for(inv)
    assert e["valid"] is False and e["date"] == "2026-01-15"
    (tmp_path / "calibration.toml").write_text("not toml = =")
    e = cal.manifest_entry_for(inv)
    assert e["valid"] is False and e["date"] is None and len(e["sha256"]) == 64


def test_the_example_holds_no_device_serials():
    d = good()
    assert "serial" not in d["power_sensor"] and "serial" not in d["vna"]
    assert Path(EXAMPLE).read_text().count("PLACEHOLDER") >= 1
