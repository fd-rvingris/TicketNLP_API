"""Claude pack tests with made-up tickets; no network."""

import json

import claude_pack as cp
import server_labels as sl
from common import make_ticket


def human(tid, text, subject="Help"):
    return make_ticket(tid, subject, text, organization_id="100", organization_name="Acme", is_alert=0)


def test_label_records_are_masked_and_cut():
    t = human(1, "Server=SQLA01;Password=Hunter2;" + "x" * 50)
    r = cp.label_record(t, max_chars=40)
    assert "Hunter2" not in r["description"] and r["truncated"] and r["kind"] == "human"


def test_batches_respect_size_limits():
    recs = [{"subject": "s", "description": "d" * 10} for _ in range(7)]
    groups = cp.batches(recs, max_batch_chars=25, max_batch_tickets=5)
    assert [len(g) for g in groups] == [2, 2, 2, 1]
    assert [len(g) for g in cp.batches(recs, 10_000, 3)] == [3, 3, 1]


def test_find_all_respects_name_boundaries_and_case():
    text = r"SQLA01 and sqla01.corp and SQLA01\REPORTS but not SQLA010 or XSQLA01"
    assert cp.find_all(text, "SQLA01") == [0, 27]
    assert cp.find_all("only lower sqla09 here", "SQLA09") == [11]  # falls back to ignoring case
    assert cp.find_all("nothing here", "SQLA01") == []
    assert cp.find_all(r"copy to \\fileserver01\share", "fileserver01") == [10]  # UNC host
    assert cp.find_all("service MSSQL$REPORTS stopped", "REPORTS") == [14]  # service suffix
    assert cp.find_all(r"SQLA01\REPORTS only", "REPORTS") == []  # instance half of HOST\INSTANCE


def test_spans_from_mentions_marks_every_occurrence_longest_first():
    t = human(1, r"SQLA01\REPORTS is down. SQLA01 rebooted. The FINANCE instance too.", subject="SQLA01 outage")
    spans, problems = cp.spans_from_mentions(t, [
        {"text": "SQLA01", "kind": "host"},
        {"text": r"SQLA01\REPORTS", "kind": "host_instance", "field": "description"},
        {"text": "FINANCE", "kind": "bare_instance", "host": "SQLA03"},
        {"text": "NOPE99", "kind": "host"},
    ], 20_000)
    got = sorted((s["field"], t[s["field"]][s["start"]:s["end"]], s["kind"]) for s in spans)
    assert got == [("description", "FINANCE", "bare_instance"), ("description", "SQLA01", "host"),
                   ("description", r"SQLA01\REPORTS", "host_instance"), ("subject", "SQLA01", "host")]
    assert problems == ["mention not found in the ticket text"]


def test_mentions_in_the_wrong_field_or_listed_twice_still_count():
    t = human(1, "Reboot SQLA07 tonight", subject="Patching")
    spans, problems = cp.spans_from_mentions(t, [{"text": "SQLA07", "kind": "host", "field": "subject"},
                                                 {"text": "SQLA07", "kind": "host", "field": "description"}], 20_000)
    assert [(s["field"], s["start"]) for s in spans] == [("description", 7)] and problems == []


def test_import_writes_server_label_rows(tmp_path, monkeypatch):
    t = human(42, "Please restart SQLA07 tonight")
    monkeypatch.setattr(cp, "read_jsonl", lambda path: [t] if "tickets" in str(path) else
                        [{"ticket_id": "42", "status": "done", "mentions": [{"text": "SQLA07", "kind": "host"}]},
                         {"ticket_id": "43", "status": "done", "mentions": []}])
    out = tmp_path / "claude.jsonl"
    cp.main(["import-labels", "returned.jsonl", "--out", str(out)])
    rows = sl.load_labels(out)
    assert list(rows) == [("42", "claude")]
    span = rows[("42", "claude")]["spans"][0]
    assert (span["text"], span["host"], span["kind"]) == ("SQLA07", "sqla07", "host")


def test_check_taxonomy(tmp_path, capsys):
    good = {"version": "v1", "types": [{"id": "performance", "label": "Performance", "definition": "Slow."},
                                       {"id": "unknown", "label": "Unknown", "definition": "Unclear."}],
            "intents": [{"id": "fix", "label": "Fix", "definition": "Fix it."},
                        {"id": "unknown", "label": "Unknown", "definition": "Unclear."}]}
    path = tmp_path / "t.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    cp.main(["check-taxonomy", str(path)])
    assert "taxonomy OK" in capsys.readouterr().out
    path.write_text(json.dumps({**good, "types": [{"id": "Bad Id", "label": "", "definition": "x"}]}), encoding="utf-8")
    cp.main(["check-taxonomy", str(path)])
    out = capsys.readouterr().out
    assert "snake_case" in out and "no label" in out and "no 'unknown'" in out
