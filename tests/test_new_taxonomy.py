"""New taxonomy mapping tests: the shipped mapping, and counts over made-up tickets with a fake classifier."""

import copy
import json
from pathlib import Path

import pytest

import ticket_analysis.service_catalog as sc
import tools.new_taxonomy as nt
from ticket_analysis.taxonomy import TAXONOMY
from ticket_analysis.tickets import make_ticket


def write(folder: Path, mapping: dict) -> Path:
    path = folder / "new_taxonomy_map.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


def shipped() -> dict:
    return json.loads(nt.MAP_PATH.read_text(encoding="utf-8"))


def test_shipped_mapping_covers_every_current_type_with_known_categories():
    catalog = sc.load_catalog()
    mapping = nt.load_mapping(catalog=catalog)
    assert set(mapping.types) == {t["id"] for t in TAXONOMY["types"]}
    ids = {c["id"] for c in catalog["categories"]}
    for slices in mapping.types.values():
        assert slices[-1]["when"] == {}
        for s in slices:
            assert (s["category"] is None) == (s["status"] == "no_home")
            assert s["category"] in ids | {None} and set(s["candidates"]) <= ids
    assert all(set(d["categories"]) <= ids for d in mapping.discussions)


@pytest.mark.parametrize("change, message", [
    (lambda m: m["types"].pop("backup_restore"), "backup_restore"),
    (lambda m: m["types"].update(made_up=m["types"]["other"]), "made_up"),
    (lambda m: m["types"]["other"].insert(0, {"label": "x", "when": {"subject": "a"}, "category": None,
                                              "status": "no_home"}) or m["types"]["other"].reverse(), "last slice"),
    (lambda m: m["types"]["other"][0].update(category="nowhere", status="mapped"), "nowhere"),
    (lambda m: m["types"]["other"][0].update(category="general_request"), "no_home"),
    (lambda m: m["types"]["database_refresh"][0].update(category=None), "category"),
    (lambda m: m["types"]["other"].insert(0, {"label": "x", "when": {"subject": "("}, "category": None,
                                              "status": "no_home"}), "does not compile"),
    (lambda m: m.update(catalog_version="old"), "catalog"),
])
def test_a_broken_mapping_fails_naming_the_problem(tmp_path, change, message):
    mapping = shipped()
    change(mapping)
    with pytest.raises(ValueError, match=message):
        nt.load_mapping(write(tmp_path, mapping))


def test_the_first_matching_slice_wins():
    mapping = nt.load_mapping()
    jobs = mapping.types["scheduled_jobs"]
    pick = lambda **kw: jobs[nt.match(jobs, **{"alert": False, "source": "model", "subject": "", "text": "", **kw})]
    assert pick(subject="Job failed", text="DatabaseBackup - USER_DATABASES")["category"] == "backup_management"
    assert pick(subject="Cenlar - Overnight Process Monitoring")["category"] == "overnight_process_management"
    assert pick(subject="Job did not run")["category"] == "general_troubleshooting"
    staff = mapping.types["staff_access"]
    assert nt.match(staff, alert=False, source="rule:staff_logon", subject="", text="") == 0
    assert nt.match(staff, alert=False, source="model", subject="", text="") == 1
    outage = mapping.types["outage_availability"]
    assert outage[nt.match(outage, alert=True, source="alert_name", subject="", text="")]["label"].startswith("Instance down")


def fake_classify(results: dict):
    """classify(subject, text) from a fixed {subject: (type, source, is_alert)} table."""
    def classify(subject, text):
        kind, source, alert = results[subject]
        return {"primary_type": kind, "type_source": source, "alert": {"alert_name": "x"} if alert else None}
    return classify


def test_summary_counts_slices_categories_gaps_and_zendesk_categories():
    tickets = [make_ticket(i, f"[Log on to Acme {i}] password=hunter{i}", "", weak_labels={"category": "Client Management"})
               for i in range(3)]
    tickets += [make_ticket(10, "Low Disk Space alert", "", weak_labels={"category": "(CH) Capacity Reporti"}),
                make_ticket(11, "Refresh UAT from prod", "", weak_labels={"category": "(CH) Real-time Monitoring & Al"})]
    results = {t["subject"]: ("staff_access", "rule:staff_logon", False) for t in tickets[:3]}
    results.update({"Low Disk Space alert": ("capacity_storage", "alert_name", True),
                    "Refresh UAT from prod": ("database_refresh", "rule:database_refresh", False)})
    s = nt.summary(tickets, fake_classify(results), sc.load_catalog(), nt.load_mapping())
    assert (s["tickets"], s["alert_tickets"]) == (5, 1)
    staff = next(t for t in s["types"] if t["type"] == "staff_access")
    assert [x["tickets"] for x in staff["slices"]] == [3, 0]
    assert staff["slices"][0]["basis"] == "source" and staff["slices"][1]["basis"] == "remainder"
    assert staff["slices"][0]["zendesk"] == [{"category": "Client Management", "tickets": 3}]
    assert "hunter" not in json.dumps(s)  # example subjects are masked
    assert s["no_home"][0]["type"] == "staff_access" and s["no_home"][0]["tickets"] == 3
    cats = {c["id"]: c for c in s["categories"]}
    assert cats["capacity_reporting"]["alert_tickets"] == 1 and cats["capacity_reporting"]["tickets"] == 1
    assert "bi_analytics" in s["unmapped_categories"] and "capacity_reporting" not in s["unmapped_categories"]
    assert s["status_tickets"] == {"mapped": 1, "judgment": 1, "no_home": 3}
    assert s["alerts"]["essential_na"] == 0
    zd = {z["zendesk_category"]: z for z in s["zendesk_check"]}
    assert zd["(CH) Capacity Reporti"]["category"] == "capacity_reporting" and zd["(CH) Capacity Reporti"]["agree"] == 1
    assert zd["(CH) Real-time Monitoring & Al"]["agree"] == 0
    assert len(s["conflicts"]) == 5


def test_discussions_say_when_every_category_has_the_same_offering():
    s = nt.summary([], fake_classify({}), sc.load_catalog(), nt.load_mapping())
    by_title = {d["title"]: d for d in s["discussions"]}
    # same codes, but General Troubleshooting is coloured included and Advanced Troubleshooting CE hours
    trouble = by_title["Troubleshooting depth and timing"]
    assert trouble["same_codes"] is True and trouble["same_offering"] is False
    assert by_title["Architecture and modernization"]["same_offering"] is True
    assert by_title["Routine maintenance or index strategy"]["same_codes"] is False
    assert by_title["Routine maintenance or index strategy"]["categories"][0]["tiers"]["essential"] == "CH"
