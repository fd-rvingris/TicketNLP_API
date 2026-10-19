import pytest

from tools.common import compute_metrics, label_metrics, latest_rows, percentile, read_labels
from tools.report import update_labels


def ok_row(tid, ptype="backup_restore", intent="fix_incident", matches=(), cmdb=1, dropped=0, returned=None,
           latency=1000, cost=0.01):
    matches = list(matches)
    return {"ticket_id": tid, "error": None, "stop_category": None, "fallback_used": False,
            "output": {"primary_type": ptype, "intent": intent, "confidence": "high"},
            "matches": matches, "mentions_returned": returned if returned is not None else len(matches) + dropped,
            "dropped_mentions": dropped, "cmdb_instances_for_client": cmdb,
            "input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "latency_ms": latency, "estimated_cost_usd": cost}


def failed_row(tid, error="refusal", category="cyber"):
    return ok_row(tid) | {"error": error, "output": None, "stop_category": category}


def m(status, kind="server"):
    return {"text": "x", "kind": kind, "status": status, "server_names": []}


def test_compute_metrics():
    rows = [
        ok_row("1", matches=[m("matched"), m("unmatched"), m("matched", "database")], dropped=1, latency=100),
        ok_row("2", ptype="unknown", intent="unknown", matches=[m("ambiguous")], latency=200),
        ok_row("3", matches=[m("unmatched")], cmdb=0, latency=300),  # no CMDB data: excluded from match rate
        failed_row("4"),
    ]
    met = compute_metrics(rows)
    assert met["tickets"] == 4 and met["ok"] == 3
    assert met["failed"] == {"refusal": 1}
    assert met["refusal_categories"] == {"cyber": 1}
    assert met["type_unknown_rate"] == pytest.approx(1 / 3)
    assert met["quote_drop_rate"] == pytest.approx(1 / 6)
    assert met["host_mentions_with_cmdb"] == 3
    assert met["match_rate"] == pytest.approx(1 / 3)
    assert met["cost_per_1000_usd"] == pytest.approx(10.0)
    assert met["latency_p50_ms"] == 200


def test_percentile_nearest_rank():
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile([], 50) is None


def test_latest_rows_keeps_last_attempt():
    rows = [failed_row("1"), ok_row("1"), ok_row("2")]
    assert latest_rows(rows)["1"]["error"] is None


def test_label_metrics():
    rows = {"1": ok_row("1", "backup_restore"), "2": ok_row("2", "performance"),
            "3": ok_row("3", "performance", intent="request_change"), "4": failed_row("4")}
    labels = {"1": {"correct_type": "backup_restore", "correct_intent": "fix_incident", "mentions_correct": "y"},
              "2": {"correct_type": "backup_restore", "correct_intent": "", "mentions_correct": "partial"},
              "3": {"correct_type": "printer", "correct_intent": "request_change"},
              "4": {"correct_type": "backup_restore"}}
    lm = label_metrics(rows, labels)
    assert lm["type"]["labeled"] == 2 and lm["type"]["accuracy"] == 0.5
    assert lm["type"]["confusion"][("backup_restore", "performance")] == 1
    assert lm["type"]["per_class"]["backup_restore"] == {"precision": 1.0, "recall": 0.5, "support": 2}
    assert lm["type"]["per_class"]["performance"]["precision"] == 0.0
    assert lm["intent"]["accuracy"] == 1.0
    assert lm["invalid_labels"] == 1
    assert lm["mentions_correct"] == {"y": 1, "partial": 1}


def test_update_labels_keeps_human_entries(tmp_path):
    path = tmp_path / "labels.csv"
    path.write_text("ticket_id,subject,predicted_type,correct_type,correct_intent,mentions_correct,notes\n"
                    "1,Old subject,performance,backup_restore,fix_incident,y,checked by RV\n"
                    "9,Other run,scheduled_jobs,scheduled_jobs,,,\n", encoding="utf-8-sig")
    rows = {"1": ok_row("1", "backup_restore", matches=[m("matched") | {"text": "SQLPROD01"}]), "2": ok_row("2")}
    tickets = {"1": {"subject": "Backup failed"}, "2": {"subject": "Slow query"}}
    update_labels("opus-v1", rows, tickets, path)
    labels = read_labels(path)
    assert labels["1"]["correct_type"] == "backup_restore" and labels["1"]["notes"] == "checked by RV"
    assert labels["1"]["predicted_type"] == "backup_restore" and labels["1"]["predicted_mentions"] == "SQLPROD01"
    assert labels["1"]["subject"] == "Backup failed" and labels["1"]["predicted_run"] == "opus-v1"
    assert labels["2"]["correct_type"] == ""
    assert labels["9"]["correct_type"] == "scheduled_jobs"  # tickets outside this run are kept
