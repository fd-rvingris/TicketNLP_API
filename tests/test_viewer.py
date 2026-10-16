import json
import re

from common import make_ticket
from view_tickets import build_html


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
