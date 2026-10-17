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
    assert parse_alert("Backup Exception Report", "DATABASENAME x") == {"family": "backup_exception_report",
                                                                     "layout": "backup_report"}
    assert parse_alert("Need access", "Severity Critical") is None


TABLE = ("CAUTION: This email originated from outside of the organization.\r\n\r\n"
         "Client Name Environment Name Server Alert Type Open Date (UTC) Message\r\n"
         "FAKECO - Zeta (123) ZetaAzureEast (Q9XAB12P) Q9ZZPAP07K Collection errors 2025-01-14 15:30:46 UTC "
         "ServerName: Q9ZZPAP07K (SQL Server)\r\n"
         "Host(s): Q9ZZPAP07K\r\nDatabase:\r\nError Type: SQL Server is unresponsive\r\n"
         "First Occurrence: 2025-01-14 15:21:00\r\nLast Occurrence: 2025-01-14 15:26:00\r\n"
         "Possible Solutions: Please make sure the server is up and running\r\n\r\n![](https://example.invalid/x)\r\n")


def test_table_layout_fields():
    a = parse_alert("FAKECO - Alert #5 - Collection errors", TABLE)
    assert a == {"family": "collector", "layout": "collector_table", "client_name": "FAKECO - Zeta", "client_id": "123",
                 "environment": "ZetaAzureEast", "collector": "Q9XAB12P", "server": "Q9ZZPAP07K",
                 "alert_name": "Collection errors", "open_date": "2025-01-14 15:30:46",
                 "message": "ServerName: Q9ZZPAP07K (SQL Server)", "platform": "SQL Server",
                 "hosts": "Q9ZZPAP07K", "error_type": "SQL Server is unresponsive",
                 "first_occurrence": "2025-01-14 15:21:00", "last_occurrence": "2025-01-14 15:26:00",
                 "possible_solutions": "Please make sure the server is up and running"}


def test_table_spans_mark_each_value_where_it_is():
    from alert_parser import table_spans
    text = ("CAUTION: This email originated from outside of the organization.\r\n\r\n\r\n"
            "Client Name Environment Name Server Alert Type Open Date (UTC) Message\r\n"
            "FAKEHOLD - Fake Music (203) Default (FAK-LA01-WIS1)   No recent monitoring runs "
            "2025-01-04 03:47:08 UTC Minutes Delay: 45\r\n"
            "Threshold: 35\r\nDateTime Value: 2025-01-04 03:00:07\r\n")
    got = {s["field"]: text[s["start"]:s["end"]] for s in table_spans(text)}
    assert got == {"client_name": "FAKEHOLD - Fake Music", "client_id": "203", "environment": "Default",
                   "collector": "FAK-LA01-WIS1", "alert_name": "No recent monitoring runs",
                   "open_date": "2025-01-04 03:47:08", "message": "Minutes Delay: 45", "minutes_delay": "45",
                   "threshold": "35", "datetime_value": "2025-01-04 03:00:07"}
    assert table_spans("Client Name: Fake\nServer: x") == []  # no table header


def test_table_layout_without_alert_subject_and_variants():
    # Recognised by its header even when pasted without the alert subject
    a = parse_alert("", TABLE.replace("Q9ZZPAP07K Collection", "fakesql01\\RPT Collection"))
    assert a["family"] == "collector" and a["server"] == "fakesql01\\RPT" and a["alert_name"] == "Collection errors"
    # Empty server column: the server comes from ServerName in the message
    a = parse_alert("", TABLE.replace(") Q9ZZPAP07K Collection", ")   Collection"))
    assert (a["server"], a["alert_name"]) == ("Q9ZZPAP07K", "Collection errors")
    # Empty server column and no ServerName: no server, and the alert type stays whole
    row = "Fake Co (7) Env1 (E1)   No data in recent collections 2025-01-18 11:00:48 UTC Missed runs: 3"
    a = parse_alert("", "Client Name Environment Name Server Alert Type Open Date (UTC) Message\n" + row)
    assert "server" not in a and a["alert_name"] == "No data in recent collections" and a["message"] == "Missed runs: 3"
    # A Windows machine, with the collector machine named like the environment
    row = ("Fake Health (100063) FAKEENV0 (FAKEENV0) EC2FAKE-AB12CDE Collection errors 2025-01-18 11:00:48 UTC "
           "ServerName: EC2FAKE-AB12CDE (Windows Machine)")
    a = parse_alert("", "Client Name Environment Name Server Alert Type Open Date (UTC) Message\n" + row)
    assert (a["client_name"], a["client_id"], a["environment"], a["collector"], a["server"], a["platform"]) \
        == ("Fake Health", "100063", "FAKEENV0", "FAKEENV0", "EC2FAKE-AB12CDE", "Windows Machine")


def test_make_ticket_parses_alert_fields_and_keeps_dw_fields():
    t = make_ticket(1, "Alert #42 - Job Failure", COLLECTOR,
                    dw_fields={"platform": "SQL Server", "severity": " Sev 3 ", "priority": "", "other": "x"})
    assert t["alert_fields"]["alert_name"] == "FD - SQL Agent Job Failure"
    assert t["dw_fields"] == {"platform": "SQL Server", "severity": "Sev 3", "priority": None, "impact": None,
                              "complexity": None}
    assert make_ticket(2, "Help", "Severity Critical")["alert_fields"] is None


BACKUP = ("CAUTION: This email originated from outside of the organization.\r\n\r\n\r\n"
          "## Fortifed Data BackUp Exception Report:\r\n\r\n### PRODUCTION SERVER: PLEASE INVESTIGATE.\r\n\r\n"
          "#### The following backups needs to be checked to ensure that backup is current:\r\n\r\n"
          "DATABASENAME BACKUP TYPE LATEST BACKUP DATE HOURS WITHOUT BACKUP MINUTES WITHOUT BACKUP\r\n"
          "FakeOrionLog Full Jan 1 1900 12:00AM -1 -1\r\n"
          "FakeOrionLog DIFF Jan 1 1900 12:00AM -1 -1\r\n"
          "Fake Sales DB LOG Jan 7 2025 11:15PM 26 1560\r\n\r\n\r\n\r\n"
          "Parameter Value\r\n@HoursForFull -25\r\n@MinutesForDiff -1500\r\n@MinutesForLog -60\r\n@DeviceType 2\r\n"
          "Report Creation Time Jan 8 2025 3:00PM\r\n")


def test_backup_exception_report_fields():
    a = parse_alert(r"Backup Exception Report from server FAKESQL01\PROD", BACKUP)
    assert a == {"family": "backup_exception_report", "layout": "backup_report", "server": r"FAKESQL01\PROD",
                 "environment_note": "PRODUCTION SERVER: PLEASE INVESTIGATE.",
                 "databases": "FakeOrionLog, Fake Sales DB", "backup_types": "Full, DIFF, LOG", "backup_rows": "3",
                 "max_hours_without": "26", "hoursforfull": "-25", "minutesfordiff": "-1500", "minutesforlog": "-60",
                 "devicetype": "2", "report_creation_time": "Jan 8 2025 3:00PM"}
    # Pasted without the subject: recognised by the table header, no server
    a = parse_alert("", BACKUP)
    assert a["family"] == "backup_exception_report" and "server" not in a and a["backup_rows"] == "3"


def test_backup_report_availability_group_variant_has_a_server_column():
    text = BACKUP.replace("MINUTES WITHOUT BACKUP\r\n", "MINUTES WITHOUT BACKUP LATEST BACKUP SERVER NAME\r\n") \
        .replace("12:00AM -1 -1\r\n", "12:00AM -1 -1 FAKEAG02\r\n")
    a = parse_alert("Backup Exception Report from server FAKEAG01", text)
    assert a["server"] == "FAKEAG01" and a["backup_servers"] == "FAKEAG02"


def test_backup_spans_point_at_values():
    from alert_parser import backup_spans
    spans = backup_spans("Backup Exception Report from server FAKESQL01", BACKUP)
    subject = [s for s in spans if s.get("in") == "subject"]
    assert [("Backup Exception Report from server FAKESQL01"[s["start"]:s["end"]]) for s in subject] == ["FAKESQL01"]
    rows = [(s["field"], BACKUP[s["start"]:s["end"]]) for s in spans if s.get("row") == 2]
    assert rows == [("database", "Fake Sales DB"), ("backup_type", "LOG"), ("latest_backup", "Jan 7 2025 11:15PM"),
                    ("hours_without", "26"), ("minutes_without", "1560")]


def test_collection_errors_alert_takes_its_server_from_the_message():
    text = ("AlertId 12345678\nAlert Collection errors\nClient Name Fake Co\nKeyword\nServer\nAlias\nPlatform\n"
            "Instance\nMessage Collection errors: ServerName: FAKESQL07 (SQL Server) Host(s): FAKESQL07 Database: "
            "Error Type: SQL Server is unresponsive\nOpen Date 01/14/2025 15:30:46 UTC\nLevel 3\n")
    a = parse_alert("Fake Co - Alert #12345678 - Collection errors - ServerName: FAKESQL07...", text)
    assert (a["alert_name"], a["server"], a["platform"], a["hosts"]) == \
        ("Collection errors", "FAKESQL07", "SQL Server", "FAKESQL07")


def test_table_header_with_non_breaking_spaces():
    text = ("Client Name Environment Name Server Alert Type Open Date (UTC) Message\n"
            "Fake Co (7) Env1 (E1) FAKESQL09 Collection errors 2025-01-18 11:00:48 UTC "
            "ServerName: FAKESQL09 (SQL Server)")
    a = parse_alert("", text)
    assert a["layout"] == "collector_table" and a["server"] == "FAKESQL09" and a["client_name"] == "Fake Co"
