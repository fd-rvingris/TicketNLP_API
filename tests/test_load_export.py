import json

from common import make_ticket
from load_export import from_csv, from_json

HEADER = "ticket_id,subject,description,organization_id,organization_name,created_at,type,sub type,category,categories,dw_servers,is_alert\n"


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8-sig", newline="")
    return path


def test_csv_keeps_alert_flag_servers_and_quoted_multiline_description(tmp_path):
    path = write(tmp_path, "export.csv", HEADER +
                 '101,Disk full,"Line one, with a comma\nLine two",555,Acme,2026-07-20,incident,Incident,Storage,'
                 '"Storage; Host",SQL01; ; SQL02\\INST; SQL01,1\n')
    [t] = from_csv(path, "export:test")
    assert t["description"] == "Line one, with a comma\nLine two"
    assert t["is_alert"] is True
    assert t["dw_servers"] == ["SQL01", "SQL02\\INST"]
    assert t["weak_labels"] == {"zendesk_type": "incident", "sub type": "Incident", "category": "Storage",
                                "categories": "Storage; Host"}


def test_csv_null_text_from_ssms_is_empty(tmp_path):
    path = write(tmp_path, "export.csv", HEADER + "102,Question,NULL,NULL,Acme,2026-07-21,task,NULL,NULL,NULL,NULL,0\n")
    [t] = from_csv(path, "export:test")
    assert t["description"] == ""
    assert t["organization_id"] is None
    assert t["dw_servers"] == []
    assert t["is_alert"] is False
    assert t["weak_labels"] == {"zendesk_type": "task"}


def test_csv_without_new_columns_leaves_them_unknown(tmp_path):
    path = write(tmp_path, "export.csv", "id,subject,description\n7,Hi,There\n")
    [t] = from_csv(path, "export:test")
    assert t["is_alert"] is None and t["dw_servers"] == []


def test_json_passes_alert_flag_and_servers(tmp_path):
    path = write(tmp_path, "export.jsonl", '{"id": 9, "subject": "s", "description": "d", "is_alert": 1, '
                                           '"dw_servers": "A; B"}\n')
    [t] = from_json(path, "export:test")
    assert t["is_alert"] is True and t["dw_servers"] == ["A", "B"]


def test_make_ticket_flag_values():
    assert make_ticket(1, "", "", is_alert="yes")["is_alert"] is True
    assert make_ticket(1, "", "", is_alert="false")["is_alert"] is False
    assert make_ticket(1, "", "", is_alert="maybe")["is_alert"] is None
    assert make_ticket(1, "", "", dw_servers=["A", "a ", "B"])["dw_servers"] == ["A", "a", "B"]


STAFF_HEADER = HEADER.rstrip("\n") + ",assignee_key,status_assignee_keys,team,client_pod,client_primary_dba\n"


def test_csv_keeps_staff_columns_out_of_weak_labels(tmp_path):
    path = write(tmp_path, "export.csv", STAFF_HEADER +
                 "103,Disk full,d,555,Acme,2026-07-20,incident,Incident,Storage,Storage,SQL01,1,"
                 "42,42; 17; 42,Support,POD A,Pat Lee\n")
    [t] = from_csv(path, "export:test")
    assert t["staff"] == {"assignee_key": "42", "status_assignee_keys": ["42", "17"], "team": "Support",
                          "client_pod": "POD A", "client_primary_dba": "Pat Lee"}
    assert set(t["weak_labels"]) == {"zendesk_type", "sub type", "category", "categories"}


def test_staff_null_and_missing_columns_are_empty(tmp_path):
    path = write(tmp_path, "export.csv", STAFF_HEADER +
                 "104,Q,d,555,Acme,2026-07-21,task,NULL,NULL,NULL,NULL,0,NULL,,NULL,NULL,NULL\n")
    [t] = from_csv(path, "export:test")
    assert t["staff"] == {"assignee_key": None, "status_assignee_keys": [], "team": None,
                          "client_pod": None, "client_primary_dba": None}
    [old] = from_csv(write(tmp_path, "old.csv", "id,subject,description\n7,Hi,There\n"), "export:test")
    assert old["staff"]["assignee_key"] is None and old["staff"]["status_assignee_keys"] == []


def test_jsonl_round_trip_keeps_staff(tmp_path):
    ticket = make_ticket(9, "s", "d", staff={"assignee_key": 42, "status_assignee_keys": ["42", "17"]})
    [t] = from_json(write(tmp_path, "t.jsonl", json.dumps(ticket) + "\n"), "export:test")
    assert t["staff"]["assignee_key"] == "42" and t["staff"]["status_assignee_keys"] == ["42", "17"]
