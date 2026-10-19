"""One made-up example per alert layout, in its exact shape (\\r\\n line endings, the variants seen in
the inventory), with every field value and every span checked."""

import pytest

from tests.test_alert_parser import BACKUP, TABLE
from ticket_analysis.alert_parser import LAYOUTS, SERVER_FIELDS, alert_spans, match_layout, parse_alert, span_value
from ticket_analysis.server_detector import ticket_names
from tools.alert_parser import coverage


def check_spans(subject: str, text: str) -> list[dict]:
    """Every span is non-empty, trimmed, single-line spans read exactly their value, and each field's
    first span (outside rows) gives the parsed value."""
    fields = parse_alert(subject, text)
    spans = alert_spans(subject, text)
    first = {}
    for s in spans:
        assert s["in"] in ("subject", "description")
        raw = (subject if s["in"] == "subject" else text)[s["start"]:s["end"]]
        assert raw and raw == raw.strip(), s
        if "\n" not in raw:
            assert span_value(subject, text, s) == raw
        if "row" not in s:
            first.setdefault(s["field"], span_value(subject, text, s))
    for field, value in first.items():
        assert fields[field] == value, field
    return spans


def lines(*parts: str) -> str:
    return "\r\n".join(parts) + "\r\n"


# --- collector_labeled ---------------------------------------------------------------

LABELED = lines("AlertId 4401234", "Alert FD - SQL Agent Job Failure", "Client Name Fake Co", "Server FAKESQL01", "Alias",
                "Platform Microsoft SQL Server", "Instance Job: Nightly Load, Category: Data",
                "Message Error: The job failed.", "The last step to run was step 1.", "Severity Warning",
                "Open Date 07/21/2026 17:00:00 UTC", "Close Date 07/21/2026 18:00:00 UTC", "Failures 3",
                "OK Collections 0", "Collector Machine", "Level 2", "PreviousLevel 1")
# The newer version: no Alias, "Previous Level", Description and Recommendations at the end
LABELED_V2 = lines("AlertId 4401236", "Alert Blocking Alert", "Client Name Fake Co", "Server FAKESQL02",
                   "Platform Microsoft SQL Server", "Instance FAKESQL02\\REPORTS", "Message Blocking for 10 minutes",
                   "Severity Critical", "Open Date 07/21/2026 17:00:00 UTC", "Failures 1", "OK Collections 0",
                   "Collector Machine FAKECOLL01", "Level 3", "Previous Level 1", "Description Blocking Alert",
                   "Recommendations")
# The older vertical order: labels and values on separate lines, no AlertId or Client Name
VERTICAL = lines("Alert", "Long-Running Blocking Chain", "Level", "2", "Previous Level", "1", "Server",
                 "FAKESQL03\\REPORTS", "Alias", "Platform", "Microsoft SQL Server", "Instance", "Message",
                 "Blocking duration is 15 minutes or more (16 minutes)", "Severity", "Information", "Open Date",
                 "2026-07-21 17:00:00 UTC", "Failures", "1", "OK Collections", "0", "Collector Machine")


def test_collector_labeled_fields():
    a = parse_alert("Fake Co - Alert #4401234 - FD - SQL Agent Job Failure", LABELED)
    assert a == {"family": "collector", "layout": "collector_labeled", "alert_id": "4401234",
                 "alert_name": "FD - SQL Agent Job Failure", "client_name": "Fake Co", "server": "FAKESQL01",
                 "platform": "Microsoft SQL Server", "instance": "Job: Nightly Load, Category: Data",
                 "message": "Error: The job failed.\nThe last step to run was step 1.", "severity": "Warning",
                 "open_date": "07/21/2026 17:00:00 UTC", "close_date": "07/21/2026 18:00:00 UTC", "failures": "3",
                 "ok_collections": "0", "level": "2", "previous_level": "1"}
    check_spans("Fake Co - Alert #4401234 - FD - SQL Agent Job Failure", LABELED)


def test_collector_labeled_newer_version_and_vertical_order():
    a = parse_alert("Alert #4401236 - Blocking Alert", LABELED_V2)
    assert (a["layout"], a["instance"], a["collector"], a["previous_level"], a["description"]) == \
        ("collector_labeled", "FAKESQL02\\REPORTS", "FAKECOLL01", "1", "Blocking Alert")
    assert "recommendations" not in a
    check_spans("Alert #4401236 - Blocking Alert", LABELED_V2)
    a = parse_alert("Alert #4401237 - Long-Running Blocking Chain", VERTICAL)
    assert (a["alert_name"], a["level"], a["previous_level"], a["server"], a["platform"], a["severity"]) == \
        ("Long-Running Blocking Chain", "2", "1", "FAKESQL03\\REPORTS", "Microsoft SQL Server", "Information")
    assert a["message"] == "Blocking duration is 15 minutes or more (16 minutes)"
    assert "alias" not in a and "instance" not in a
    check_spans("Alert #4401237", VERTICAL)


def test_collector_labeled_keyword_variant_without_severity():
    text = LABELED.replace("Client Name Fake Co\r\n", "Client Name Fake Co\r\nKeyword Deadlock\r\n") \
        .replace("Severity Warning\r\n", "")
    a = parse_alert("Alert #4401234", text)
    assert a["keyword"] == "Deadlock" and "severity" not in a
    check_spans("Alert #4401234", text)


def test_collector_labeled_is_recognised_by_its_body():
    # Pasted without a subject, or with a subject that has no "Alert #"
    for subject in ("", "SQL Server Replication Agent - Failed"):
        a = parse_alert(subject, LABELED)
        assert a["layout"] == "collector_labeled" and a["server"] == "FAKESQL01"
    # A reply to an alert is recognised only by an alert body
    assert parse_alert("RE: [Fake Co] Update: Alert #4401234 - Low Disk Space", "Done, thanks.") is None
    assert parse_alert("RE: [Fake Co] Update: Alert #4401234", LABELED)["layout"] == "collector_labeled"


def test_collection_errors_message_spans():
    text = lines("AlertId 12345678", "Alert Collection errors", "Client Name Fake Co", "Server", "Platform",
                 "Message Collection errors: ServerName: FAKESQL07 (SQL Server) Host(s): FAKESQL07 Database: "
                 "Error Type: SQL Server is unresponsive", "Open Date 01/14/2025 15:30:46 UTC")
    spans = check_spans("", text)
    got = {s["field"]: text[s["start"]:s["end"]] for s in spans}
    assert (got["alert_name"], got["server"], got["platform"], got["hosts"]) == \
        ("Collection errors", "FAKESQL07", "SQL Server", "FAKESQL07")


# --- collector_markdown ----------------------------------------------------------------

MARKDOWN = ("* *AlertId*: 4401235 * *Alert*: Low Disk Space * *Client Name*: Fake Co * *Server*: FAKESQL03 "
            "* *Alias*: * *Platform*: Microsoft Windows Machine * *Instance*: C: * *Message*: Disk Free space on C: "
            "is 1024MB. * *Severity*: Warning * *Open Date*: 07/21/2026 17:00:00 UTC * *Close Date*: 07/21/2026 "
            "17:30:00 UTC * *Failures*: 1 * *OK Collections*: 0 * *Collector Machine*: * *Level*: 2 "
            "* *PreviousLevel*: 1\r\n")


def test_collector_markdown_fields():
    a = parse_alert("CLOSE - Alert #4401235 - Low Disk Space - FAKESQL03 - C: - Disk Free space ...", MARKDOWN)
    assert a == {"family": "collector", "layout": "collector_markdown", "alert_id": "4401235",
                 "alert_name": "Low Disk Space", "client_name": "Fake Co", "server": "FAKESQL03",
                 "platform": "Microsoft Windows Machine", "instance": "C:", "message": "Disk Free space on C: is 1024MB.",
                 "severity": "Warning", "open_date": "07/21/2026 17:00:00 UTC", "close_date": "07/21/2026 17:30:00 UTC",
                 "failures": "1", "ok_collections": "0", "level": "2", "previous_level": "1"}
    check_spans("", MARKDOWN)
    assert parse_alert("", MARKDOWN)["layout"] == "collector_markdown"


# --- collector_table ---------------------------------------------------------------------


def test_collector_table_spans_and_close_date_header():
    check_spans("FAKECO - Alert #5 - Collection errors", TABLE)
    text = TABLE.replace("Open Date (UTC)", "Close Date (UTC)")
    a = parse_alert("CLOSE - Alert #5 - Collection errors", text)
    assert a["layout"] == "collector_table" and a["close_date"] == "2025-01-14 15:30:46" and "open_date" not in a
    assert a["server"] == "Q9ZZPAP07K" and a["collector"] == "Q9XAB12P"
    check_spans("", text)


def test_collector_table_without_host_lines():
    text = TABLE.replace("Host(s): Q9ZZPAP07K\r\n", "")
    a = parse_alert("", text)
    assert "hosts" not in a and a["error_type"] == "SQL Server is unresponsive"
    check_spans("", text)


# --- backup_report -----------------------------------------------------------------------

# The Availability Group variant: parameters before the table, a server column, an exclusions table
BACKUP_AG = lines("CAUTION: This email originated from outside of the organization.", "",
                  "## Native BackUp Exception Report:", "", "### FAKEAG01 missing backup report for Availability Groups",
                  "#### The following backups needs to be checked to ensure that backup is current:", "",
                  "Parameter Name Parameter Value", "@NamesList FakeList ~ Default", "@InstanceNames FAKEAG01 ~ FAKEAG02",
                  "@HoursForFull -25", "@MinutesForDiff 0", "@MinutesForTranLog -60",
                  "Report Creation Time Mar 3 2026 9:00AM", "",
                  "DATABASENAME BACKUP TYPE LATEST BACKUP DATE HOURS WITHOUT BACKUP MINUTES WITHOUT BACKUP "
                  "LATEST BACKUP SERVER NAME",
                  "FakeSales LOG Mar 3 2026 7:15AM 1 105 FAKEAG02", "FakeHR Full Mar 1 2026 1:00AM 56 3360 FAKEAG01", "",
                  "Database Exclusions Exclude Full Exclude Diff Exclude Log", "FakeTemp 1 1 1")


def test_backup_report_spans():
    subject = "Backup Exception Report from server FAKESQL01\\PROD"
    spans = check_spans(subject, BACKUP)
    assert {s["row"] for s in spans if "row" in s} == {0, 1, 2}


def test_backup_report_availability_group_variant():
    subject = "Backup Exception Report from server FAKEAG01"
    a = parse_alert(subject, BACKUP_AG)
    assert (a["layout"], a["server"], a["databases"], a["backup_types"], a["backup_rows"]) == \
        ("backup_report", "FAKEAG01", "FakeSales, FakeHR", "LOG, Full", "2")
    assert (a["backup_servers"], a["instancenames"], a["max_hours_without"], a["minutesfortranlog"]) == \
        ("FAKEAG02, FAKEAG01", "FAKEAG01 ~ FAKEAG02", "56", "-60")
    assert a["environment_note"] == "FAKEAG01 missing backup report for Availability Groups"
    check_spans(subject, BACKUP_AG)
    hosts = {p.host for p in ticket_names({"alert_fields": a})}
    assert hosts == {"fakeag01", "fakeag02"}


def test_backup_report_subject_only():
    a = parse_alert("Backup Exception Report from server FAKESQL09", "")
    assert a == {"family": "backup_exception_report", "layout": "backup_report", "server": "FAKESQL09"}


# --- grafana ---------------------------------------------------------------------------------

GRAFANA = lines("CAUTION: This email originated from outside of the organization.",
                "2 alerts for alertname = FakeReplicationLag", "View in Grafana", "**[2] Firing**",
                "**Labels**", "alertname = FakeReplicationLag", "Status = Replication is behind on FAKESQL04",
                "env = prod", "instance = fakeexp01:9399", "job = sql_exporter", "severity = critical",
                "target = fakesql04.fake.invalid:1433", "**Annotations**",
                "description = Replication delay on FAKESQL04. VALUE = 42",
                "summary = Replication delay (instance fakeexp01:9399)", "Source",
                "**Labels**", "alertname = FakeReplicationLag", "DatabaseName = FakeSales", "env = prod",
                "instance = fakeexp01:9399", "job = sql_exporter", "severity = critical",
                "target = fakesql05.fake.invalid:1433", "**Annotations**",
                "description = Replication delay on FAKESQL05. VALUE = 40",
                "summary = Replication delay (instance fakeexp01:9399)", "Source", "Sent by Grafana v10.1.0")
GRAFANA_RDS = lines("1 alert for alertname = FakeCpuHigh", "View in Grafana", "**[1] Firing**", "**Labels**",
                    "alertname = FakeCpuHigh", "account_id = 123456789012", "dimension_DBInstanceIdentifier = fake-db-01",
                    "instance = fakeexp02:9106", "job = rds", "name = arn:aws:rds:us-east-1:123456789012:db:fake-db-01",
                    "region = us-east-1", "severity = warning", "**Annotations**",
                    "description = CPU over 80% VALUE = 85.1", "summary = Host high CPU load (instance fakeexp02:9106)",
                    "Source", "Sent by Grafana v10.1.0")


def test_grafana_blocks_are_rows():
    subject = "[FIRING:2] FakeReplicationLag (prod fakeexp01:9399 sql_exporter critical)"
    a = parse_alert(subject, GRAFANA)
    assert a == {"family": "firing", "layout": "grafana", "alert_name": "FakeReplicationLag",
                 "status": "Replication is behind on FAKESQL04", "env": "prod", "instance": "fakeexp01:9399",
                 "job": "sql_exporter", "severity": "critical", "target": "fakesql04.fake.invalid:1433",
                 "description": "Replication delay on FAKESQL04. VALUE = 42",
                 "summary": "Replication delay (instance fakeexp01:9399)", "database": "FakeSales", "alerts": "2",
                 "targets": "fakesql04.fake.invalid:1433, fakesql05.fake.invalid:1433"}
    spans = check_spans(subject, GRAFANA)
    assert {s["row"] for s in spans} == {0, 1}
    assert {p.host for p in ticket_names({"alert_fields": a})} >= {"fakesql04", "fakesql05", "fakeexp01"}


def test_grafana_single_alert_and_body_recognition():
    a = parse_alert("[FIRING:1] FakeCpuHigh (fake-db-01 fakeexp02:9106 rds warning)", GRAFANA_RDS)
    assert (a["db_instance"], a["region"], a["account_id"], a["severity"]) == \
        ("fake-db-01", "us-east-1", "123456789012", "warning")
    assert a["summary"] == "Host high CPU load (instance fakeexp02:9106)" and "alerts" not in a
    spans = check_spans("", GRAFANA_RDS)
    assert all("row" not in s for s in spans)
    # Another subject format of the same template
    assert parse_alert("Fake Co [WARNING] Alert - FakeCpuHigh", GRAFANA_RDS)["layout"] == "grafana"


# --- volume_capacity --------------------------------------------------------------------------

VOLUME_SUBJECT = "Volume Capacity-C:\\ on FAKESQL06 is over the limit that PercentUsed has a value of 91.2345"
VOLUME = lines("FAKEMON 1234567", "Incident Id:", "Fake Monitoring", "Incident opened by:", "Fake Management",
               "Description:", "Device Description: ",
               "Datasource: Volume Capacity-C:\\ - Volume storage capacity utilization and detail",
               "Datapoint: PercentUsed - percent storage used",
               "LMD1234567890 critical - Volumes - Fake Group - Resource: FAKESQL06 - Source: Volume Capacity-C:\\ "
               "Metric: PercentUsed value reported: 91.2345 on: C:\\ Volume C:\\  on host FAKESQL06 is now 91.2345 "
               "percent used, as of 2026-07-21 10:00:00 EDT.",
               "Please free up some space, or adjust the thresholds for this volume.",
               "This state has persisted for 0h 5m.",
               "Alert Rule: This alert matched the rule for Fake Resource/Website Alert Rule",
               "Recipients: Alert now being sent to 1 recipient: fake.person (Fake Escalation)",
               "https://fake.invalid/alert/index#detail/LMD-1234567890")


def test_volume_capacity_fields():
    a = parse_alert(VOLUME_SUBJECT, VOLUME)
    assert a == {"family": "volume_capacity", "layout": "volume_capacity", "alert_name": "Volume Capacity",
                 "volume": "C:\\", "server": "FAKESQL06", "incident_id": "Fake Monitoring",
                 "incident_opened_by": "Fake Management",
                 "datasource": "Volume Capacity-C:\\ - Volume storage capacity utilization and detail",
                 "datapoint": "PercentUsed - percent storage used", "alert_id": "LMD1234567890",
                 "severity": "critical", "source": "Volume Capacity-C:\\", "metric": "PercentUsed",
                 "value": "91.2345", "hosts": "FAKESQL06", "percent_used": "91.2345",
                 "open_date": "2026-07-21 10:00:00", "time_zone": "EDT",
                 "alert_rule": "This alert matched the rule for Fake Resource/Website Alert Rule",
                 "recipients": "Alert now being sent to 1 recipient: fake.person (Fake Escalation)"}
    spans = check_spans(VOLUME_SUBJECT, VOLUME)
    assert [s["in"] for s in spans if s["field"] == "server"] == ["subject", "description"]


def test_volume_capacity_without_subject_or_optional_lines():
    text = VOLUME.replace("Incident opened by:\r\nFake Management\r\nDescription:\r\n", "")
    a = parse_alert("", text)
    assert (a["layout"], a["server"], a["hosts"]) == ("volume_capacity", "FAKESQL06", "FAKESQL06")
    assert "incident_opened_by" not in a and "volume" not in a
    check_spans("", text)


# --- Registry and human text ---------------------------------------------------------------------


@pytest.mark.parametrize("subject,text", [
    ("Server is slow", "Server is slow since this morning.\nClient Name: Fake\nPlease check."),
    ("Need help", "Hi team,\nAlert: the job failed again.\nServer FAKESQL01 is down\nInstance REPORTS\nMessage me back."),
    ("Question about a backup", "Our backup report from server FAKESQL01 says the DIFF failed. Severity high."),
    ("Grafana question", "The dashboard shows alertname: FakeCpuHigh and instance fake:9100."),
    ("Disk", "Resource usage on FAKESQL06 is high. Source unknown."),
])
def test_human_text_is_not_an_alert(subject, text):
    assert parse_alert(subject, text) is None and alert_spans(subject, text) == []


def test_registry_order_and_server_fields():
    assert [lay.name for lay in LAYOUTS] == ["collector_table", "backup_report", "grafana", "volume_capacity",
                                             "collector_markdown", "collector_labeled"]
    # The table layout has an "Alert #" subject too, and is tried first
    assert match_layout("FAKECO - Alert #5 - Collection errors", TABLE).name == "collector_table"
    assert {"server", "hosts", "target", "backup_servers"} <= set(SERVER_FIELDS)
    assert {p.host for p in ticket_names({"alert_fields": {"target": "fakesql08.fake.invalid:1433",
                                                           "hosts": "FAKEA01, FAKEB01"}})} == \
        {"fakesql08", "fakea01", "fakeb01"}
    # "HOST, PORT" with a space is one server
    assert ticket_names({"alert_fields": {"server": "FAKESQL01,  14330"}})[0].host == "fakesql01"


def test_coverage_counts_layouts():
    tickets = [{"is_alert": True, "subject": "Alert #1", "description": LABELED},
               {"is_alert": True, "subject": "RE: Alert #2", "description": "Thanks"},
               {"is_alert": False, "subject": "", "description": MARKDOWN},
               {"is_alert": False, "subject": "Help", "description": "Server is slow"}]
    rows = {r["layout"]: r for r in coverage(tickets)}
    assert (rows["collector_labeled"]["alerts"], rows["collector_labeled"]["server"]) == (1, 1)
    assert rows["unrecognised"]["alerts"] == 1 and rows["collector_markdown"]["human_matches"] == 1
