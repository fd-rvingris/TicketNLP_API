"""Local taxonomy classification tests. All tickets and alerts are made up."""

import ticket_types as tt
from common import make_ticket
from tests.test_alert_parser import COLLECTOR

NOTES = """# Notes

## Alert name to v1 type

| Family | Alert name | Tickets | v1 type | Name decides | Note |
|---|---|---:|---|---|---|
| backup_exception_report | (blank) | 412 | backup_restore | Yes | Family decides |
| collector | FD - SQL Agent Job Failure | 1,259 | scheduled_jobs | Partly | DatabaseBackup moves it to backup_restore |
| collector | errors | 2,142 | monitoring_tooling | No | Inferred default |

## Sample results

| collector | not in the alert section | 1 | performance | Yes | |
"""

TEXTS = {"backup_restore": "the nightly full backup failed, restore test needed",
         "access_security": "please grant read access to the reporting login",
         "capacity_storage": "disk space low on the data drive, volume nearly full"}


def labeled(n=12):
    tickets, labels = [], {}
    for kind, text in TEXTS.items():
        for i in range(n):
            tid = f"{kind}-{i}"
            tickets.append(make_ticket(tid, text.split(",")[0], f"{text} {i}"))
            labels[tid] = {"ticket_id": tid, "primary_type": kind,
                           "intent": "fix_incident" if kind != "access_security" else "request_change"}
    return tickets, labels


def test_alert_map_reads_only_the_alert_section(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text(NOTES, encoding="utf-8")
    m = tt.load_alert_map(path)
    assert set(m) == {("backup_exception_report", ""), ("collector", "fd - sql agent job failure"), ("collector", "errors")}
    assert m[("collector", "fd - sql agent job failure")]["tickets"] == 1259
    assert m[("collector", "errors")]["decides"] == "No"
    assert tt.load_alert_map(tmp_path / "missing.md") == {}


def test_classify_uses_the_model_for_human_tickets():
    bundle = tt.train(*labeled())
    r = tt.classify("Please grant access", "please grant read access to the reporting login", bundle, {})
    assert (r["primary_type"], r["type_source"], r["intent"]) == ("access_security", "model", "request_change")
    assert r["alert"] is None and len(r["type_candidates"]) == 3
    assert bundle["counts"]["primary_type"]["backup_restore"] == 12


def test_classify_uses_the_alert_table_and_the_alert_intent_rule(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text(NOTES, encoding="utf-8")
    bundle, m = tt.train(*labeled()), tt.load_alert_map(path)
    r = tt.classify("Example Client - Alert #42 - FD - SQL Agent Job Failure", COLLECTOR, bundle, m)
    assert (r["primary_type"], r["type_source"], r["intent"]) == ("scheduled_jobs", "alert_name_partly", "fix_incident")
    assert r["alert"]["alert_name"] == "FD - SQL Agent Job Failure" and r["notes"]
    cleared = COLLECTOR.replace("Level Failure", "Level Cleared")
    assert tt.classify("Example Client - Alert #42 - FD - SQL Agent Job Failure", cleared, bundle, m)["intent"] \
        == "provide_information"
    # an alert name the table does not have falls back to the model
    r = tt.classify("Example Client - Alert #9 - Something New", COLLECTOR.replace("FD - SQL Agent Job Failure",
                                                                                  "Something New"), bundle, m)
    assert r["type_source"] == "model" and r["alert"]["entry"] is None


def test_summary_lists_taxonomy_counts_and_rules():
    s = tt.summary(tt.train(*labeled()), {("collector", "x"): {"type": "performance", "tickets": 7}})
    types = {t["id"]: t for t in s["types"]}
    assert types["backup_restore"]["sample"] == 12 and types["performance"]["alert_tickets"] == 7
    assert s["rules"] and all(r for r in s["rules"])
    assert len(s["types"]) == len(tt.TAXONOMY["types"])
