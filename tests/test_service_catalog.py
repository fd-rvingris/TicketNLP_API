"""Service catalog tests: the shipped file and made-up variations of it."""

import json
from pathlib import Path

import pytest

import ticket_analysis.service_catalog as sc


def write(folder: Path, catalog: dict) -> Path:
    path = folder / "service_catalog.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    return path


def shipped() -> dict:
    return json.loads(sc.CATALOG_PATH.read_text(encoding="utf-8"))


def test_shipped_catalog_loads_with_every_category_and_tier():
    catalog = sc.load_catalog()
    assert len(catalog["categories"]) == 36
    assert list(catalog["tiers"]) == ["essential", "select", "signature"]
    by_id = {c["id"]: c for c in catalog["categories"]}
    assert by_id["manage_users_groups_roles"]["name"] == "Manage Users\\Groups\\Roles"
    assert by_id["critical_incidents"]["tiers"] == {"essential": "N/A", "select": "N/A", "signature": "CS"}


def test_implied_coverage_follows_the_tier_codes():
    assert sc.implied_coverage({"essential": "CH", "select": "CH", "signature": "CH"}) == "included"
    assert sc.implied_coverage({"essential": "N/A", "select": "CH", "signature": "CH"}) == "included"
    assert sc.implied_coverage({"essential": "N/A", "select": "CS", "signature": "CS"}) == "cs_incident"
    assert sc.implied_coverage({"essential": "N/A", "select": "CE", "signature": "CE"}) == "ce_or_sow"
    assert sc.implied_coverage({"essential": "CH", "select": "CS", "signature": "CS"}) is None
    assert sc.implied_coverage({"essential": "N/A", "select": "N/A", "signature": "N/A"}) is None


def test_the_shipped_catalog_has_five_colour_and_code_conflicts():
    found = {c["id"]: c for c in sc.conflicts(sc.load_catalog())}
    assert set(found) == {"performance_tuning_proactive", "general_troubleshooting", "data_environment_assessment",
                          "data_corruption", "failures_and_outages"}
    c = found["data_corruption"]
    assert (c["coverage"], c["implied_coverage"]) == ("ce_or_sow", "cs_incident")
    assert "CS" in c["question"] and "CE hours" in c["question"]
    assert found["general_troubleshooting"]["implied_coverage"] == "ce_or_sow"


@pytest.mark.parametrize("change, message", [
    (lambda c: c["categories"][0]["tiers"].pop("select"), "select"),
    (lambda c: c["categories"][0]["tiers"].update(select="XX"), "XX"),
    (lambda c: c["categories"][0].update(coverage="free"), "free"),
    (lambda c: c["categories"][1].update(id=c["categories"][0]["id"]), "listed twice"),
    (lambda c: c["categories"][0].update(extra=1), "extra"),
])
def test_a_broken_catalog_fails_naming_the_problem(tmp_path, change, message):
    catalog = shipped()
    change(catalog)
    with pytest.raises(ValueError, match=message):
        sc.load_catalog(write(tmp_path, catalog))
