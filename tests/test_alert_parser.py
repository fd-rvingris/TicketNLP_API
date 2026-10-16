from alert_parser import alert_family, parse_alert
from common import make_ticket

COLLECTOR = """AlertId 123456
Alert FD - SQL Agent Job Failure
Client Name Example Client
Server EXSQL01
Platform Microsoft SQL Server
Instance Job: Nightly Load, Category: Data
Collector Machine
Alias
Message Error: The job failed.
The last step to run was step 1.
Severity Warning
Level Failure
PreviousLevel OK
Open Date 07/21/2026 17:00:00 UTC
OK Collections 0
Failures 3
"""


def test_alert_family_from_subject():
    assert alert_family("Example Client - Alert #42 - FD - SQL Agent Job Failure") == "collector"
    assert alert_family("[FIRING:1] HighCPU") == "firing"
    assert alert_family("Backup Exception Report for 07/21") == "backup_exception_report"
    assert alert_family("Volume Capacity warning") == "volume_capacity"
    assert alert_family("Please reset my password") is None
    assert alert_family("") is None


def test_collector_alert_fields():
    a = parse_alert("Alert #42 - FD - SQL Agent Job Failure", COLLECTOR)
    assert a["family"] == "collector"
    assert a["alert_id"] == "123456"
    assert a["alert_name"] == "FD - SQL Agent Job Failure"
    assert a["client_name"] == "Example Client"
    assert a["server"] == "EXSQL01" and a["platform"] == "Microsoft SQL Server"
    assert a["instance"] == "Job: Nightly Load, Category: Data"
    assert a["message"] == "Error: The job failed.\nThe last step to run was step 1."
    assert a["severity"] == "Warning" and a["level"] == "Failure" and a["previous_level"] == "OK"
    assert a["failures"] == "3" and a["ok_collections"] == "0"
    assert "collector" not in a and "alias" not in a  # empty labels are left out


def test_alert_collection_and_previous_level_spellings():
    text = "AlertId 7\nAlert Collection Blocking\nClient Name X\nPrevious Level Warning\nLevel Critical\n"
    a = parse_alert("X - Alert #7 - Blocking", text)
    assert a["alert_name"] == "Blocking"
    assert a["previous_level"] == "Warning" and a["level"] == "Critical"


def test_vertical_layout_takes_the_next_line():
    text = "AlertId\n99\nAlert\nDisk Space\nServer\nEXSQL02\nSeverity\nCritical\n"
    a = parse_alert("Alert #99", text)
    assert (a["alert_id"], a["alert_name"], a["server"], a["severity"]) == ("99", "Disk Space", "EXSQL02", "Critical")


def test_first_value_wins_and_crlf():
    a = parse_alert("Alert #1", "Alert First\r\nSeverity Warning\r\nAlert Second\r\n")
    assert a["alert_name"] == "First" and a["severity"] == "Warning"


def test_firing_alert_fields():
    text = ("CAUTION: This email originated outside the organization.\nView in Grafana\n"
            "alertname = HighCPU\nseverity = critical\ninstance db-01:9100\njob=node\n"
            "summary CPU above 90%\nfor 10 minutes\ndescription Sustained load\nSent by Grafana\n")
    a = parse_alert("[FIRING:1] HighCPU", text)
    assert a["family"] == "firing"
    assert (a["alert_name"], a["severity"], a["instance"], a["job"]) == ("HighCPU", "critical", "db-01:9100", "node")
    assert a["summary"] == "CPU above 90%\nfor 10 minutes"
    assert a["description"] == "Sustained load\nSent by Grafana"


def test_other_families_and_human_tickets():
    assert parse_alert("Backup Exception Report", "DATABASENAME x") == {"family": "backup_exception_report"}
    assert parse_alert("Need access", "Severity Critical") is None


def test_make_ticket_parses_alert_fields_and_keeps_dw_fields():
    t = make_ticket(1, "Alert #42 - Job Failure", COLLECTOR,
                    dw_fields={"platform": "SQL Server", "severity": " Sev 3 ", "priority": "", "other": "x"})
    assert t["alert_fields"]["alert_name"] == "FD - SQL Agent Job Failure"
    assert t["dw_fields"] == {"platform": "SQL Server", "severity": "Sev 3", "priority": None, "impact": None,
                              "complexity": None}
    assert make_ticket(2, "Help", "Severity Critical")["alert_fields"] is None
