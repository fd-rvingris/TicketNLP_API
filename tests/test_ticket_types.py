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
    r = tt.classify("Disk space low", "disk space low on the data drive, volume nearly full", bundle, {})
    assert (r["primary_type"], r["type_source"], r["intent"]) == ("capacity_storage", "model", "fix_incident")
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


EMAIL_ACCESS = (
    "CAUTION: This email originated from outside of the organization. Do not click links or open attachments "
    "unless you recognize the sender and know the content is safe.\r\n\r\n"
    "Hi Fortified Data Support,\r\n\r\n"
    "Could you please help me get READ access for the schema below, Server=FAKESQL01, Database=FAKE_DB, for the "
    "execution user CORP\\svc-fake-batch. Please approve.\r\n\r\n"
    "![](https://fake.example.com/image001.png)\r\n\r\n"
    "Thanks,\r\nJane Doe\r\nData Engineer | Fake Co\r\nPROD support\r\n\r\n"
    "This message is intended only for the use of the addressee and may contain information that is privileged "
    "and confidential. Any disclosure, copying, distribution or use of this message is prohibited.")


def test_the_email_access_request_is_access_security_with_or_without_a_subject():
    bundle = tt.train(*labeled())
    for subject in ("", "READ access request"):
        r = tt.classify(subject, EMAIL_ACCESS, bundle, {})
        assert (r["primary_type"], r["type_source"]) == ("access_security", "rule:access_request")
        assert r["uncertain"] is False and r["type_candidates"]


def test_a_rule_decides_ahead_of_the_model_but_not_ahead_of_the_alert_table(tmp_path):
    bundle = tt.train(*labeled())
    r = tt.classify("[Log on to FAKESQL01]", "the nightly full backup failed", bundle, {})
    assert (r["primary_type"], r["type_source"]) == ("staff_access", "rule:staff_logon")
    path = tmp_path / "notes.md"
    path.write_text(NOTES, encoding="utf-8")
    r = tt.classify("Example Client - Alert #42 - FD - SQL Agent Job Failure", COLLECTOR + "\ngrant read access to the user",
                    bundle, tt.load_alert_map(path))
    assert r["type_source"] == "alert_name_partly"


def test_uncertain_flag():
    close = [{"id": "database_refresh", "p": 0.17}, {"id": "access_security", "p": 0.14}]
    assert tt.uncertainty(close, has_subject=False) == (
        True, "close call between database_refresh and access_security; add a subject or check by hand")
    low = [{"id": "performance", "p": 0.25}, {"id": "capacity_storage", "p": 0.12}]
    flag, reason = tt.uncertainty(low)
    assert flag and reason.startswith("low confidence: performance at 25%") and reason.endswith("check by hand")
    assert tt.uncertainty([{"id": "performance", "p": 0.8}, {"id": "capacity_storage", "p": 0.1}]) == (False, "")
    # the classify result carries the flag for model decisions
    r = tt.classify("", "something unrelated entirely", tt.train(*labeled()), {})
    assert r["type_source"] == "model" and isinstance(r["uncertain"], bool) and "uncertain_reason" in r


def test_summary_lists_taxonomy_counts_and_rules():
    s = tt.summary(tt.train(*labeled()), {("collector", "x"): {"type": "performance", "tickets": 7}})
    types = {t["id"]: t for t in s["types"]}
    assert types["backup_restore"]["sample"] == 12 and types["performance"]["alert_tickets"] == 7
    assert s["rules"] and all(r for r in s["rules"])
    assert len(s["types"]) == len(tt.TAXONOMY["types"])
