"""Retraining tests: the gate, version names and publishing, with training and scoring stubbed out."""

import json
from datetime import datetime, timezone

import joblib

import tools.retrain as rt


def server(f1_host, f1_inst=0.5):
    return {"test host": {"precision": 0.8, "recall": 0.8, "f1": f1_host, "spans": 10, "detections": 10},
            "test instance": {"precision": 0.5, "recall": 0.5, "f1": f1_inst, "spans": 4, "detections": 4}}


def types(acc=0.75, f1=0.66, rules=()):
    return {"targets": {"primary_type": {"accuracy": acc, "macro_f1": f1, "tickets": 600},
                        "intent": {"accuracy": 0.83, "macro_f1": 0.67, "tickets": 600}},
            "rules": list(rules)}


def test_new_version_is_the_date_then_numbered(tmp_path):
    now = datetime(2026, 12, 31, 3, tzinfo=timezone.utc)
    assert rt.new_version(tmp_path, now) == "2026-12-31"
    (tmp_path / "2026-12-31").mkdir()
    (tmp_path / "2026-12-31-2").mkdir()
    assert rt.new_version(tmp_path, now) == "2026-12-31-3"


def test_gate_allows_small_drops_and_blocks_larger_ones_and_weak_rules():
    assert rt.gate(server(0.795), server(0.80), types(), types(), tolerance=1.0) == []
    assert rt.gate(server(0.80), None, types(), None, tolerance=1.0) == []  # first run: nothing to compare
    fails = rt.gate(server(0.78), server(0.80), types(acc=0.70), types(acc=0.75), tolerance=1.0)
    assert len(fails) == 2 and "server test host F1" in fails[0] and "primary_type accuracy" in fails[1]
    weak = [{"rule": "ha_terms", "fires": 30, "precision": 0.85}, {"rule": "rare", "fires": 3, "precision": 0.33}]
    assert rt.gate(server(0.8), server(0.8), types(rules=weak), types(), 1.0) == \
        ["rule ha_terms is 85.0% precise on 30 tickets, under 90%"]  # a rule with under 5 fires is not judged


def stub_training(monkeypatch, tmp_path, server_new, server_old):
    def fake_run(*args):
        path = args[args.index("--model-path") + 1]
        joblib.dump({"trained_on": "train_tickets.jsonl"}, path)
    monkeypatch.setattr(rt, "run", fake_run)
    monkeypatch.setattr(rt.sd, "load_model", lambda p: {"which": "new" if "retrain" in str(p) else "old"})
    monkeypatch.setattr(rt, "span_scores", lambda b: server_new if b["which"] == "new" else server_old)
    monkeypatch.setattr(rt, "type_scores", lambda *a: types())
    monkeypatch.setattr(rt, "read_jsonl", lambda p: [])
    monkeypatch.setattr(rt.ttt, "current_labels", lambda: {})
    serving = tmp_path / "models"
    serving.mkdir()
    for name in (rt.SERVER_FILE, rt.TYPES_FILE):
        (serving / name).write_bytes(b"serving")
    return serving


def test_a_passing_run_publishes_installs_and_uploads(tmp_path, monkeypatch):
    serving = stub_training(monkeypatch, tmp_path, server(0.81), server(0.80))
    store = tmp_path / "store"
    args = ["--serving-dir", str(serving), "--versions-dir", str(serving / "versions"),
            "--work-dir", str(tmp_path / "retrain"), "--install", "--store", str(store)]
    assert rt.main(args) == 0
    version = next((serving / "versions").iterdir())
    metrics = json.loads((version / rt.METRICS_FILE).read_text(encoding="utf-8"))
    assert metrics["gate"]["passed"] and set(metrics["sha256"]) == {rt.SERVER_FILE, rt.TYPES_FILE}
    assert (serving / rt.SERVER_FILE).read_bytes() != b"serving"  # installed
    assert (serving / f"server_detector.before-{version.name}.joblib").read_bytes() == b"serving"  # kept
    assert (store / "models" / version.name / rt.SERVER_FILE).exists()


def test_a_failing_run_publishes_nothing_and_exits_1(tmp_path, monkeypatch):
    serving = stub_training(monkeypatch, tmp_path, server(0.70), server(0.80))
    args = ["--serving-dir", str(serving), "--versions-dir", str(serving / "versions"),
            "--work-dir", str(tmp_path / "retrain"), "--install"]
    assert rt.main(args) == 1
    assert not (serving / "versions").exists() and (serving / rt.SERVER_FILE).read_bytes() == b"serving"
    work = next((tmp_path / "retrain").iterdir())
    assert json.loads((work / rt.METRICS_FILE).read_text(encoding="utf-8"))["gate"]["passed"] is False
