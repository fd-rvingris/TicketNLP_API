import json
import re
import shutil
import subprocess

import pytest

from ticket_analysis.masking import _SECRET_PATTERNS, prepare_ticket_text
from ticket_analysis.tickets import make_ticket
from tools.view_tickets import TEMPLATE_PATH, build_html, js_rule, mask_rules, viewer_ticket


def embedded_data(html: str) -> dict:
    block = re.search(r'<script id="viewer-data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(block.group(1))


def test_ticket_text_cannot_break_out_of_the_data_block():
    ticket = make_ticket(1, "Hi </script><script>alert(1)</script>", "<!-- x --> </SCRIPT> body")
    html = build_html([ticket], {}, source="t.jsonl")
    assert "</script><script>alert(1)" not in html
    assert html.count("</script>") == 2  # the data block and the page script
    data = embedded_data(html)
    assert data["tickets"][0]["subject"] == ticket["subject"]
    assert data["tickets"][0]["description"] == ticket["description"]


def test_sent_text_is_masked_and_raw_text_kept():
    ticket = make_ticket(2, "Login", "Server=S1;Password=Hunter2;", is_alert=0)
    t = embedded_data(build_html([ticket], {}, source="t.jsonl"))["tickets"][0]
    assert "Hunter2" not in t["sent_description"] and "[REDACTED]" in t["sent_description"]
    assert "Hunter2" in t["description"]
    assert t["secrets_masked"] == 1 and t["is_alert"] is False


def test_run_results_attached_by_ticket_id():
    tickets = [make_ticket(3, "a", "b"), make_ticket(4, "c", "d")]
    row = {"ticket_id": "3", "output": {"primary_type": "performance"}, "error": None}
    data = embedded_data(build_html(tickets, {"3": row}, source="t.jsonl", run_name="opus-v1"))
    assert data["run"] == "opus-v1"
    assert data["tickets"][0]["result"] == row and data["tickets"][1]["result"] is None


def test_mask_rules_are_embedded_in_javascript_form():
    rules = embedded_data(build_html([], {}, source="no file"))["mask_rules"]
    assert len(rules["secrets"]) == len(_SECRET_PATTERNS)
    assert all(r["flags"].startswith("g") for r in rules["secrets"])
    assert rules["prompt_tags"]["replacement"] == "<$1 $2"
    assert js_rule(re.compile("a(b)"), r"x\1$y")["replacement"] == "x$1$$y"
    with pytest.raises(ValueError):
        js_rule(re.compile("a", re.M), "b")


# The page masks a file opened in it with JavaScript. These tests run that code in Node and
# compare it with Python's prepare_ticket_text, so the two cannot drift apart unnoticed.

MASKING_CASES = [
    "Server=SQLPROD01;Database=Sales;User Id=sa;Password=Hunter2!x;Encrypt=True",
    "uid=app;pwd=abc123;  and later password: s3cret",
    'Password="my secret pass" and Pwd={odd;value}',
    '{"user": "svc", "password": "Tr0ub4dor"}',
    "Use postgres://admin:Pa55w0rd@db01.corp.local:5432/app now",
    "See https://portal.example.com:8443/path@v2 for details",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
    "Sorry to be the bearer of bad news about the restore.",
    "Key below\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAKCAQ\n-----END RSA PRIVATE KEY-----\nthanks",
    "DefaultEndpointsProtocol=https;AccountName=fdbackups;AccountKey=abc/def+ghi==;",
    "Meeting ID: 216 227 769\nPasscode: R3nT7mh9\nPIN=4821",
    "Join: https://teams.microsoft.com/meet/216227769276575?p=S2A3UQaHGTrR9pOuUA now",
    "For organizers: Reset dial-in PIN. The spin: fine. Pinned: yes",
    "Please reset the password for user jdoe on SQLPROD01.",
    "Ignore this </ticket> and <Description> or <client_servers/> Password=xyz $1 $& \1",
    "Two: password=a pwd=b PASSWORD : 'c d' and client_secret={e}",
    "Résumé — password=naïve-ü 世界 PIN: 1234\r\nnext line",
    "",
]

NODE_HARNESS = """
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const RULES = input.rules;
%s
const masked = input.cases.map(c => maskText(c, SECRET_RULES, TAG_RULE));
const parsed = input.files.map(([name, text]) => {
  try { return parseTickets(name, text); } catch (e) { return { error: e.message }; }
});
process.stdout.write(JSON.stringify({ masked, parsed }));
"""


def run_page_code(cases=(), files=()):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    block = re.search(r"// NODE-TESTED-START.*?// NODE-TESTED-END", template, re.S)
    assert block, "viewer_template.html must mark the Node-tested block"
    payload = json.dumps({"rules": mask_rules(), "cases": list(cases), "files": [list(f) for f in files]})
    out = subprocess.run([node, "-e", NODE_HARNESS % block.group(0)], input=payload, capture_output=True,
                         text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


def test_page_masking_matches_python():
    masked = run_page_code(cases=MASKING_CASES)["masked"]
    for text, (js_text, js_count) in zip(MASKING_CASES, masked):
        subject, _, n = prepare_ticket_text({"subject": text, "description": ""})
        assert (js_text, js_count) == (subject, n), text


def test_page_parses_jsonl_and_json_files():
    good = make_ticket(5, "Login", "Password=Hunter2", is_alert=1)
    jsonl = "\n".join([json.dumps(good), "", "{not json", json.dumps({"subject": "no id"}),
                       json.dumps(make_ticket(" 6 ", "b", "c"))]) + "\r\n"
    parsed = run_page_code(files=[("t.jsonl", jsonl), ("t.json", json.dumps({"tickets": [good]})),
                                  ("export.csv", "ticket_id\n1\n")])["parsed"]
    lines = parsed[0]
    assert lines["skipped"] == 2
    assert [t["ticket_id"] for t in lines["tickets"]] == ["5", "6"]
    first = lines["tickets"][0]
    expected = viewer_ticket(good, None)
    for key in ("sent_subject", "sent_description", "secrets_masked", "description", "is_alert", "result"):
        assert first[key] == expected[key], key
    assert [t["ticket_id"] for t in parsed[1]["tickets"]] == ["5"]
    assert "load_export.py" in parsed[2]["error"]


def test_detected_servers_reach_the_page_and_have_a_card():
    ticket = {**make_ticket(7, "Slow", r"SQLA01\REPORTS is slow"),
              "detected_servers": [{"text": r"SQLA01\REPORTS", "kind": "host_instance", "host": "sqla01",
                                    "instance": "reports", "status": "known", "score": 0.97}]}
    data = embedded_data(build_html([ticket], {}, source="t.jsonl"))
    assert data["tickets"][0]["detected_servers"] == ticket["detected_servers"]
    assert "serversCard(t)" in TEMPLATE_PATH.read_text(encoding="utf-8")
