"""Claude pack tests with made-up tickets; no network."""

import json

import claude_pack as cp
from common import read_jsonl as common_read
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


# --- Type labeling rounds ------------------------------------------------------------

BANNER = ("CAUTION: This email originated from outside of the organization. Do not click links or open "
          "attachments unless you recognize the sender and know the content is safe.")


def test_type_records_are_cleaned_then_masked_and_cut():
    t = human(7, f"{BANNER}\r\n\r\nThe backup failed. Password=Hunter2\r\n\r\nThanks,\r\nJane Doe", subject="[EXTERNAL] Backup")
    r = cp.type_record(t, max_chars=4000)
    assert r["subject"] == "Backup" and "CAUTION" not in r["description"] and "Jane Doe" not in r["description"]
    assert "Hunter2" not in r["description"] and "backup failed" in r["description"] and not r["truncated"]
    assert cp.type_record(t, max_chars=10)["truncated"]


class LeansWeak:
    """A stand-in type model: tickets mentioning 'refresh' lean to database_refresh."""
    classes_ = ["database_refresh", "performance"]

    def predict_proba(self, X):
        import numpy as np
        return np.array([[0.9, 0.1] if "refresh" in (s + d) else [0.1, 0.9] for s, d in X])


def test_pick_for_types_skips_labeled_and_alerts_and_over_samples_weak_types():
    tickets = [human(i, "refresh QA from prod" if i < 3 else "slow query") for i in range(20)]
    tickets.append(make_ticket("a1", "Alert", "refresh", is_alert=1))
    picked, weak_n = cp.pick_for_types(tickets, {"0"}, n=4, weak_share=0.5, seed=1, type_model=LeansWeak())
    ids = [str(t["ticket_id"]) for t in picked]
    assert weak_n == 2 and set(ids[:2]) == {"1", "2"} and "0" not in ids and "a1" not in ids and len(ids) == 4
    again, _ = cp.pick_for_types(tickets, {"0"}, n=4, weak_share=0.5, seed=1, type_model=LeansWeak())
    assert [t["ticket_id"] for t in again] == [t["ticket_id"] for t in picked]
    plain, none_weak = cp.pick_for_types(tickets, set(), n=50, weak_share=0.5, seed=1)
    assert none_weak == 0 and len(plain) == 20


def test_type_prompt_lists_every_type_intent_and_rule():
    text = cp.type_prompt("round_001", ["batch_001", "batch_002"])
    assert all(f"- {t['id']}:" in text for t in cp.TAXONOMY["types"])
    assert all(f"- {i['id']}:" in text for i in cp.TAXONOMY["intents"])
    assert "Validation Results reports are always outage_availability" in text
    assert "types_round_001_batch_001.jsonl" in text


def test_types_round_then_import(tmp_path, monkeypatch, capsys):
    tickets = [human(i, f"slow query number {i}") for i in range(6)]
    monkeypatch.setattr(cp, "TYPES_DIR", tmp_path / "types")
    monkeypatch.setattr(cp, "LABEL_PATHS", (tmp_path / "none.jsonl",))
    monkeypatch.setattr(cp, "read_jsonl", lambda path: tickets if "tickets" in str(path) else common_read(path))
    cp.main(["types", "--tickets", "4", "--weak-share", "0", "--batch-tickets", "3"])
    manifest = json.loads((tmp_path / "types" / "round_001" / "manifest.json").read_text(encoding="utf-8"))
    ids = [tid for b in manifest["batches"].values() for tid in b]
    assert list(manifest["batches"]) == ["batch_001", "batch_002"] and len(ids) == 4
    assert (tmp_path / "types" / "round_001" / "prompt.md").exists()
    # a second round leaves out tickets already sent in the first
    cp.main(["types", "--tickets", "4", "--weak-share", "0"])
    second = json.loads((tmp_path / "types" / "round_002" / "manifest.json").read_text(encoding="utf-8"))
    assert not set(ids) & {tid for b in second["batches"].values() for tid in b}

    returned = tmp_path / "types_round_001_batch_001.jsonl"
    good = {"primary_type": "performance", "secondary_types": [], "intent": "fix_incident", "confidence": "high"}
    lines = ["```", json.dumps({"ticket_id": ids[0], **good}), json.dumps({"ticket_id": ids[1], **good, "intent": "nope"}),
             json.dumps({"ticket_id": "999", **good}), "```"]
    returned.write_text("\n".join(lines), encoding="utf-8")
    out = tmp_path / "claude_types.jsonl"
    cp.main(["import-types", str(returned), "--out", str(out)])
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert [(r["ticket_id"], r["round"], r["batch"], r["labeler"]) for r in rows] == [(ids[0], "round_001", "batch_001", "claude")]
    report = capsys.readouterr().out
    assert "1 x unknown intent" in report and "1 x ticket_id not in any round" in report and "still missing" in report
    cp.main(["import-types", str(tmp_path / "types_round_001_*.jsonl"), "--out", str(out)])  # a wildcard; adds nothing new
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1
