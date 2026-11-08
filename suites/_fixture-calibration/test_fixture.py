"""Reads the bench calibration through the lab fixture (skipped when lab.toml configures none)."""


def test_reads_the_calibration(lab):
    c = lab.calibration
    assert c is not None and c.used
    loss = c.path_loss_db("b200_to_e310", 915)
    lab.log("calibrated_level", path_loss_db=loss, rx_dbm=c.rx_dbm("b200", -20.0, 40, 915))
    assert loss >= 60
