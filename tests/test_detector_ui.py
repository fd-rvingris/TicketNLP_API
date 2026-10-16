"""Detector UI tests with made-up tickets and a fake Zendesk session; no network."""


import numpy as np
import pytest
import requests

import detector_ui as ui
import server_detector as sd
from common import ZendeskClient, make_ticket


class FixedModel(sd.ServerModel):
    """Scores fixed by name, so the app can be tested without fitting."""

    def __init__(self, hosts, instances):
        super().__init__(host_threshold=0.5, instance_threshold=0.5)
        self.fixed_hosts, self.fixed_instances = set(hosts), set(instances)

    def scores(self, cands):
        return np.array([[0.9 if c["host_ok"] and c["host"] in self.fixed_hosts else 0.1,
                          0.8 if c["inst_ok"] and c["host"] in self.fixed_instances else 0.0] for c in cands])


TRAIN = [make_ticket(1, "x", "y", organization_id="100", organization_name="Acme Corp", is_alert=0,
                     dw_servers=["SQLA01\\REPORTS", "SQLA02"]),
         make_ticket(2, "x", "y", organization_name="Cobalt", is_alert=0, dw_servers=["cob01"])]


def app(zendesk=None, subdomain=None):
    bundle = {"model": FixedModel({"sqla01", "sqla02", "newhost07"}, {"reports"}),
              "directory": sd.LocalServerDirectory.from_sources(TRAIN, None), "trained_on": "t.jsonl",
              "created_at": "2026-10-16"}
    return ui.App(bundle, ui.client_names(TRAIN), zendesk, subdomain, 8765)


def test_ticket_id_from_url_or_number():
    assert ui.ticket_id_from("12345", "acme") == "12345"
    assert ui.ticket_id_from("#12345", "acme") == "12345"
    assert ui.ticket_id_from("https://acme.zendesk.com/agent/tickets/987", "acme") == "987"
    assert ui.ticket_id_from("https://ACME.zendesk.com/agent/tickets/987/events", "acme") == "987"
    with pytest.raises(ValueError, match="not on the configured"):
        ui.ticket_id_from("https://other.zendesk.com/agent/tickets/987", "acme")
    for bad in ("https://acme.zendesk.com.evil.test/agent/tickets/1", "http://acme.zendesk.com/agent/tickets/1", "abc"):
        with pytest.raises(ValueError):
            ui.ticket_id_from(bad, "acme")


def test_client_names_and_pasted_ticket_client():
    names = ui.client_names(TRAIN)
    assert names == {"100": "Acme Corp", "name:cobalt": "Cobalt"}
    t = ui.ticket_for_text("s", "body", "100", names)
    assert t["organization_id"] == "100" and t["organization_name"] == "Acme Corp"
    t = ui.ticket_for_text("s", "body", "name:cobalt", names)
    assert t["organization_id"] is None and t["organization_name"] == "Cobalt"
    assert ui.ticket_for_text("s", "body", None, names)["organization_id"] is None


def test_analyze_text_returns_detections_and_all_candidates():
    a = app()
    r = a.analyze_text({"subject": "Slow", "text": "The REPORTS instance on SQLA01 and NEWHOST07 are slow",
                        "client": "100"})
    found = {d["text"]: d for d in r["detections"]}
    assert found["REPORTS"]["host"] == "sqla01" and found["REPORTS"]["status"] == "known"
    assert found["NEWHOST07"]["status"] == "possible_new_host"
    assert r["client"] == {"key": "100", "name": "Acme Corp", "in_directory": True, "hosts": 2, "instances": 1}
    texts = [c["text"] for c in r["candidates"]]
    assert "SQLA01" in texts and r["candidates"][0]["p_host"] >= r["candidates"][-1]["p_host"]
    assert r["thresholds"] == {"host": 0.5, "instance": 0.5}


def test_analyze_text_returns_table_alert_fields_and_spans():
    text = ("Client Name Environment Name Server Alert Type Open Date (UTC) Message\n"
            "Fake Co (7) Env1 (E1) FAKESQL09 Collection errors 2025-01-18 11:00:48 UTC ServerName: FAKESQL09 (SQL Server)")
    r = app().analyze_text({"text": text})
    assert r["is_alert"] and r["alert_fields"]["server"] == "FAKESQL09"
    spans = {s["field"]: text[s["start"]:s["end"]] for s in r["alert_spans"]}
    assert spans["client_name"] == "Fake Co" and spans["alert_name"] == "Collection errors"
    assert app().analyze_text({"text": "SQLA01 is down"})["alert_spans"] == []


def test_analyze_text_thresholds_and_validation():
    a = app()
    r = a.analyze_text({"text": "SQLA01 is down", "client": "100", "host_threshold": 0.95})
    assert r["detections"] == [] and r["thresholds"]["host"] == 0.95
    assert a.analyze_text({"text": "SQLA01", "host_threshold": "abc"})["thresholds"]["host"] == 0.5
    with pytest.raises(ValueError, match="Paste"):
        a.analyze_text({"text": "  "})
    with pytest.raises(ValueError, match="longer"):
        a.analyze_text({"text": "x" * (ui.MAX_TEXT + 1)})


def test_info_lists_clients_and_zendesk_state():
    info = app().info()
    assert [c["name"] for c in info["clients"]] == ["Acme Corp", "Cobalt"]
    assert info["zendesk"] is None and info["thresholds"]["host"] == 0.5


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls, self.headers, self.auth = responses, [], {}, None

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        status, body = self.responses.get(url.rsplit("/api/v2/", 1)[-1], (404, {}))
        resp = requests.Response()
        resp.status_code = status
        resp._content = __import__("json").dumps(body).encode()
        resp.url = url
        return resp


def zendesk(responses):
    return ZendeskClient("acme", "me@example.com", "token", session=FakeSession(responses), sleep=lambda s: None)


def test_analyze_zendesk_reads_ticket_and_organization():
    zd = zendesk({"tickets/55.json": (200, {"ticket": {"id": 55, "subject": "Help", "organization_id": 100,
                                                       "description": "SQLA02 is slow", "created_at": "2026-10-01"}}),
                  "organizations/100.json": (200, {"organization": {"name": "Acme Corp"}})})
    r = app(zd, "acme").analyze_zendesk({"url": "https://acme.zendesk.com/agent/tickets/55"})
    assert r["ticket"]["ticket_id"] == "55" and r["client"]["key"] == "100"
    assert [d["host"] for d in r["detections"]] == ["sqla02"]
    assert all("/tickets/55.json" in u or "/organizations/100.json" in u for u in zd.session.calls)


def test_analyze_zendesk_errors_are_readable():
    a = app(zendesk({"tickets/7.json": (403, {})}), "acme")
    with pytest.raises(ValueError, match="no permission"):
        a.analyze_zendesk({"url": "7"})
    with pytest.raises(ValueError, match="not configured"):
        app().analyze_zendesk({"url": "7"})


def test_page_has_both_inputs():
    html = ui.PAGE_PATH.read_text(encoding="utf-8")
    for needle in ('id="text"', 'id="analyze"', 'id="zd-url"', 'id="fetch"', "/api/analyze", "/api/zendesk"):
        assert needle in html
    assert "http://" not in html.replace("http://127.0.0.1", "")  # loads nothing from the network
