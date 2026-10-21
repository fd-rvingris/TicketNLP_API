import subprocess
import sys
from pathlib import Path

import joblib
import pytest

import ticket_analysis.server_detector as sd
from tools.convert_model import BACKUP_SUFFIX, OLD_MODULE, convert
from tools.directory_build import build_directory

REPO_ROOT = Path(__file__).parent.parent
LOAD = ("import joblib, sys; b = joblib.load(sys.argv[1]); m = b['model']; "
        "print(type(m).__module__, type(m).__name__, m.host_threshold)")


def old_style_model(path: Path, monkeypatch) -> None:
    """A model file saved before the package existed: its classes are stored as server_detector.*"""
    with monkeypatch.context() as m:
        m.setattr(sd.ServerModel, "__module__", OLD_MODULE)
        m.setitem(sys.modules, OLD_MODULE, sd)
        joblib.dump({"model": sd.ServerModel(host_threshold=0.42), "trained_on": "made_up.jsonl"}, path)


def load_elsewhere(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", LOAD, str(path)], cwd=REPO_ROOT, capture_output=True, text=True)


def test_an_old_model_does_not_load_after_the_move(tmp_path, monkeypatch):
    path = tmp_path / "server_detector.joblib"
    old_style_model(path, monkeypatch)
    out = load_elsewhere(path)
    assert out.returncode != 0 and "server_detector" in out.stderr


def test_convert_rewrites_the_module_path_and_keeps_the_model(tmp_path, monkeypatch):
    path = tmp_path / "server_detector.joblib"
    old_style_model(path, monkeypatch)
    backup = convert(path)
    assert backup == tmp_path / f"server_detector{BACKUP_SUFFIX}.joblib" and backup.exists()
    out = load_elsewhere(path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["ticket_analysis.server_detector", "ServerModel", "0.42"]
    assert sys.modules.get(OLD_MODULE) is not sd


def test_convert_refuses_a_second_run(tmp_path, monkeypatch):
    path = tmp_path / "server_detector.joblib"
    old_style_model(path, monkeypatch)
    convert(path)
    with pytest.raises(SystemExit, match="already converted"):
        convert(path)


def test_convert_keeps_a_trained_model_and_its_directory(tmp_path, monkeypatch):
    """A fitted model holds _Linear and a LocalServerDirectory too; all three classes must be remapped."""
    import tools.server_detector as sdt
    from tests.test_server_detector import human, synthetic_tickets
    tickets = synthetic_tickets(n=150)
    directory = build_directory(tickets, None)
    model, _ = sdt.fit_config(tickets, directory, kind="logreg", alert_context="strip_labels", instance_model=False,
                              seed=0, max_chars=5000, neg_ratio=10)
    probe = human(999, r"Please restart FAKESQL01\REPORTS and check the FINANCE instance.", subject="Made up")
    expected = sd.detect_servers(probe, directory, model, host_threshold=0.5, instance_threshold=0.3)
    path = tmp_path / "server_detector.joblib"
    with monkeypatch.context() as m:
        for cls in (sd.ServerModel, sd._Linear, sd.LocalServerDirectory):
            m.setattr(cls, "__module__", OLD_MODULE)
        m.setitem(sys.modules, OLD_MODULE, sd)
        joblib.dump({"model": model, "directory": directory}, path)
    assert load_elsewhere(path).returncode != 0
    convert(path)
    bundle = sd.load_model(path)
    assert {type(bundle["model"]).__module__, type(bundle["model"].main).__module__,
            type(bundle["directory"]).__module__} == {"ticket_analysis.server_detector"}
    assert sd.detect_servers(probe, bundle["directory"], bundle["model"], host_threshold=0.5,
                             instance_threshold=0.3) == expected
