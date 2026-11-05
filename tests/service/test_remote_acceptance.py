"""tools/remote_acceptance.py against a live service: two counted runs through the connector, then the refusals."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_tool():
    spec = importlib.util.spec_from_file_location("remote_acceptance", ROOT / "tools" / "remote_acceptance.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # its dataclasses look themselves up there
    spec.loader.exec_module(mod)
    return mod


def test_remote_acceptance_passes_on_the_simulated_bench(make_service, tmp_path, monkeypatch, capsys):
    svc = make_service()
    monkeypatch.setenv("OASA_LAB_TOKEN", svc.tokens["owner"])
    monkeypatch.setenv("OASA_LAB_RUNNER_TOKEN", svc.tokens["bob"])
    out = tmp_path / "acc"
    code = load_tool().main(["--url", svc.url, "--out", str(out), "--timeout", "120", "--channel-suite", "t-quick",
                             "--hw-suite", "t-needs", "--hw-profile", "bench", "--tx-suite", "t-tx", "--attended"])
    text = capsys.readouterr().out
    assert code == 0, text
    assert "**Result:** PASS" in text and "**FAIL**" not in text
    assert text.count("| yes |") == 2  # both runs counted
    assert "transmits, and this lab's rf_setup is 'unset'" in text
    assert "the lab is paused by owner" in text and "your role may not do this" in text
    assert svc.lab.jobs.paused is None  # it resumed what it paused
    assert (out / "summary.md").read_text() == text.rstrip("\n") + "\n"
    assert len(list(out.glob("t-*.md"))) == 2  # a results page per run
    for tok in svc.tokens.values():
        assert tok not in text


def test_remote_acceptance_fails_when_a_refusal_does_not_happen(make_service, tmp_path, monkeypatch, capsys):
    svc = make_service(extra={"lab": {"rf_setup": "cabled"}})  # simulated + cabled: the transmit suite is allowed
    monkeypatch.setenv("OASA_LAB_TOKEN", svc.tokens["owner"])
    monkeypatch.delenv("OASA_LAB_RUNNER_TOKEN", raising=False)
    code = load_tool().main(["--url", svc.url, "--out", str(tmp_path / "acc"), "--timeout", "120",
                             "--channel-suite", "t-quick", "--hw-suite", "t-needs", "--hw-profile", "bench",
                             "--tx-suite", "t-tx"])
    text = capsys.readouterr().out
    assert code == 1
    assert "| run_suite t-tx refused (transmit lock) | **FAIL** |" in text


def test_without_attended_it_never_pauses_the_lab(make_service, tmp_path, monkeypatch, capsys):
    # An unattended agent never pauses or resumes the lab (security-review-remote.md, RM1).
    svc = make_service()
    monkeypatch.setenv("OASA_LAB_TOKEN", svc.tokens["owner"])
    monkeypatch.delenv("OASA_LAB_RUNNER_TOKEN", raising=False)
    code = load_tool().main(["--url", svc.url, "--out", str(tmp_path / "acc"), "--timeout", "120",
                             "--channel-suite", "t-quick", "--hw-suite", "t-needs", "--hw-profile", "bench",
                             "--tx-suite", "t-tx"])
    text = capsys.readouterr().out
    assert code == 0, text
    assert "skipped: not --attended" in text
    assert "the lab is paused" not in text
    assert not any(e.get("tool") in ("pause_lab", "resume_lab") for e in svc.audit())
    assert svc.lab.jobs.paused is None
