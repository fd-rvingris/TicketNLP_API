import subprocess
import sys
from pathlib import Path

import joblib
import pytest

import ticket_analysis.server_detector as sd
from tools.convert_model import BACKUP_SUFFIX, OLD_MODULE, convert

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
