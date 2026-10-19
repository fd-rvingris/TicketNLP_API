"""Keyword rule tests. All names and text are made up."""

import ticket_analysis.type_rules as tr


def test_staff_logon():
    assert tr.staff_logon("[Log on to FAKESQL01] weekly", "") == "staff_access"
    assert tr.staff_logon("[Change Password for svc-fake]", "") == "staff_access"
    assert tr.staff_logon("Cannot log on to FAKESQL01", "") is None


def test_validation_results():
    assert tr.validation_results("Validation Results for Server [FAKESQL01]", "") == "outage_availability"
    assert tr.validation_results("RE: Validation Results for Server [FAKESQL01]", "") == "outage_availability"
    assert tr.validation_results("Question about the validation results", "") is None


def test_sip_review_takes_the_single_area():
    assert tr.sip_review("SIP Security Audit - Fake Co", "index review template") == "access_security"
    assert tr.sip_review("SIP Index and Performance Review - Fake Co", "") == "performance"
    assert tr.sip_review("SIP Capacity Review - Fake Co", "") == "capacity_storage"
    assert tr.sip_review("SIP DR/HA Review - Fake Co", "") == "replication_ha_dr"
    assert tr.sip_review("SIP Database Maintenance Review - Fake Co", "") == "maintenance_patching"
    assert tr.sip_review("SIP Environment Review - Fake Co", "") is None  # spans areas: the model decides
    assert tr.sip_review("Security audit for Fake Co", "") is None  # not a SIP review


def test_database_refresh_fires_on_refresh_subjects():
    assert tr.database_refresh("FAKESQL01-TEST Database refresh", "") == "database_refresh"
    assert tr.database_refresh("RE: DB Refresh request", "") == "database_refresh"
    assert tr.database_refresh("Refresh of the Fake_DB database", "") == "database_refresh"
    assert tr.database_refresh("Refresh Fake_DB from PROD to QA", "") == "database_refresh"
    assert tr.database_refresh("Please refresh UAT from production", "") == "database_refresh"


def test_database_refresh_leaves_other_refreshes_to_the_model():
    assert tr.database_refresh("Statistics refresh failing on FAKESQL01", "") is None
    assert tr.database_refresh("Report refresh to QA is slow", "") is None
    assert tr.database_refresh("Cube refresh from prod source failed", "") is None
    assert tr.database_refresh("Token refresh error", "") is None
    assert tr.database_refresh("Disk full on FAKESQL01", "a database refresh last week") is None  # subject only


def test_access_request_fires_on_client_access_wording():
    assert tr.access_request("", "Please give the reporting user READ access to the Sales schema.") == "access_security"
    assert tr.access_request("Grant access", "Please grant the login CORP\\svc-fake db_datareader.") == "access_security"
    assert tr.access_request("", "Add the login CORP\\jdoe to the Fake_DB database.") == "access_security"
    assert tr.access_request("", "Remove access for the user CORP\\jdoe, who left.") == "access_security"


def test_access_request_does_not_fire_without_access_wording_or_an_account():
    assert tr.access_request("", "The nightly backup failed on FAKESQL01.") is None
    assert tr.access_request("", "We need read access.") is None  # no login, user, role or schema


def test_access_request_leaves_staff_collector_and_other_work_to_their_types():
    text = "Please grant read access to the user"
    assert tr.access_request("", f"{text} for our account on FAKESQL01.") is None
    assert tr.access_request("", f"{text} of the Fortified Data accounts.") is None
    assert tr.access_request("", f"{text} the WISdom collector runs as.") is None
    assert tr.access_request("CHG0012345", f"{text} as part of the change.") is None
    assert tr.access_request("", f"{text} after the refresh to QA.") is None
    assert tr.access_request("", f"{text} after you restore Fake_DB to DEV.") is None
    assert tr.access_request("", f"{text}; run the attached script.") is None


def test_apply_rules_returns_the_first_rule_that_fires():
    assert tr.apply_rules("[Log on to FAKESQL01]", "grant read access to the user") == ("staff_access", "staff_logon")
    assert tr.apply_rules("", "grant read access to the user") == ("access_security", "access_request")
    assert tr.apply_rules("DB refresh", "grant read access to the user") == ("database_refresh", "database_refresh")
    assert tr.apply_rules("", "The disk is full.") is None
    assert tr.apply_rules(None, None) is None
