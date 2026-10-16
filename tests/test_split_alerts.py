import csv

from common import make_ticket, read_jsonl, write_jsonl
from load_export import from_csv
from split_alerts import output_paths, split_csv, split_jsonl

HEADER = "ticket_id,subject,description,category,is_alert\n"


def test_output_paths_sit_next_to_the_input(tmp_path):
    outputs = output_paths(tmp_path / "train.csv")
    assert outputs[True] == tmp_path / "train_alerts.csv"
    assert outputs[False] == tmp_path / "train_human.csv"
    assert output_paths(tmp_path / "t.jsonl", tmp_path / "out")[False] == tmp_path / "out" / "t_human.jsonl"


def test_split_csv_keeps_rows_unchanged(tmp_path):
    src = tmp_path / "export.csv"
    src.write_text(HEADER + '1,Alert #5,"Line one, with a comma\nLine two",Monitoring,1\n'
                   "2,Help,please,Troubleshooting,0\n"
                   "3,Odd,row,Monitoring,NULL\n"
                   "4,Alert #6,x,Backups,1\n", encoding="utf-8-sig", newline="")
    outputs = output_paths(src)
    counts = split_csv(src, outputs)
    assert counts == {True: 2, False: 1, None: 1}
    alerts = from_csv(outputs[True], "export:test")
    assert [t["ticket_id"] for t in alerts] == ["1", "4"]
    assert alerts[0]["description"] == "Line one, with a comma\nLine two"
    assert all(t["is_alert"] is True for t in alerts)
    with outputs[False].open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == [{"ticket_id": "2", "subject": "Help", "description": "please", "category": "Troubleshooting",
                     "is_alert": "0"}]


def test_split_csv_without_is_alert_column_stops(tmp_path):
    src = tmp_path / "export.csv"
    src.write_text("ticket_id,subject\n1,x\n", encoding="utf-8")
    try:
        split_csv(src, output_paths(src))
    except SystemExit as e:
        assert "is_alert" in str(e)
    else:
        raise AssertionError("expected SystemExit")


def test_split_jsonl(tmp_path):
    src = tmp_path / "tickets.jsonl"
    write_jsonl(src, [make_ticket("1", "", "", is_alert=1), make_ticket("2", "", "", is_alert=0),
                      make_ticket("3", "", "")])
    outputs = output_paths(src)
    assert split_jsonl(src, outputs) == {True: 1, False: 1, None: 1}
    assert [t["ticket_id"] for t in read_jsonl(outputs[True])] == ["1"]
    assert [t["ticket_id"] for t in read_jsonl(outputs[False])] == ["2"]
