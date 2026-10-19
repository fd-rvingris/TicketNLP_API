"""The /v1 endpoints, probes, errors and limits, with small models trained from made-up tickets."""

import logging

import pytest

import ticket_analysis.server_detector as sd
import ticket_analysis.ticket_types as tt
from api import API_VERSION
from tests.api_support import DETECTOR_CREATED, ORG, TICKET, make_client, trained
from tests.test_ticket_types import EMAIL_ACCESS
from ticket_analysis.analysis import ticket_for_org
from ticket_analysis.taxonomy import TAXONOMY, TAXONOMY_VERSION


@pytest.fixture
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def post(client, path, body, **headers):
    return client.post(path, json=body, headers=headers)


def test_detect_returns_keys_entities_and_detections(client):
    r = post(client, "/v1/detect", TICKET)
    assert r.status_code == 200
    j = r.json()
    assert j["apiVersion"] == API_VERSION and j["client"] == {"zendeskOrgId": ORG, "known": True}
    assert j["isAlert"] is False and j["ticketId"] is None
    by_text = {d["text"]: d for d in j["detections"]}
    assert by_text["FAKESQL01"]["key"] == "host" and by_text["FAKESQL01"]["value"] == "fakesql01"
    assert by_text["FAKESQL02\\FINANCE"]["key"] == "host_instance"
    assert by_text["FAKESQL02\\FINANCE"]["value"] == "fakesql02\\finance"
    assert by_text["FAKESQL02\\FINANCE"]["host"] == "fakesql02" and by_text["FAKESQL02\\FINANCE"]["instance"] == "finance"
    rep = by_text["REPORTS"]
    assert rep["key"] == "instance" and rep["value"] == "reports" and rep["host"] == "fakesql01"
    for d in j["detections"]:
        assert TICKET[d["field"] if d["field"] == "subject" else "text"][d["start"]:d["end"]] == d["text"]
        assert set(d) == {"key", "value", "text", "host", "instance", "status", "field", "start", "end", "typoOf",
                          "siblingOf", "hostCandidates", "environment", "score"}
    # the compact view: each key's distinct values in order of first appearance
    assert j["entities"] == {"host": ["fakesql01"], "instance": ["reports"], "host_instance": ["fakesql02\\finance"]}
    assert by_text["FAKESQL01"]["environment"] == "Production"


def test_detect_equals_the_detector_called_directly(client):
    model, directory, _ = trained()
    expected = sd.detect_servers(ticket_for_org(TICKET["subject"], TICKET["text"], ORG), directory, model)
    got = post(client, "/v1/detect", TICKET).json()["detections"]
    assert [(d["start"], d["end"], d["status"], d["host"], d["score"]) for d in got] == \
        [(d["start"], d["end"], d["status"], d["host"], d["score"]) for d in expected]


def test_classify_equals_the_classifier_called_directly(client):
    _, _, types = trained()
    r = post(client, "/v1/classify", {"text": EMAIL_ACCESS, "subject": "READ access request", "zendeskOrgId": ORG})
    assert r.status_code == 200
    j = r.json()
    direct = tt.classify("READ access request", EMAIL_ACCESS, types, {})
    assert (j["primaryType"], j["typeSource"]) == ("access_security", "rule:access_request")
    assert (j["intent"], j["intentSource"], j["uncertain"]) == (direct["intent"], direct["intent_source"], False)
    assert j["typeCandidates"] == direct["type_candidates"] and j["intentCandidates"] == direct["intent_candidates"]
    assert j["alert"] is None and j["notes"] == [] and "debug" not in j


def test_classify_reports_the_alert_table_entry(client):
    from tests.test_alert_parser import COLLECTOR
    j = post(client, "/v1/classify", {"text": COLLECTOR, "subject": "Fake Co - Alert #42 - FD - SQL Agent Job Failure"}).json()
    assert (j["primaryType"], j["typeSource"], j["intentSource"]) == ("scheduled_jobs", "alert_name_partly", "alert_rule")
    assert j["alert"]["alertName"] == "FD - SQL Agent Job Failure" and j["alert"]["tableEntry"]["decides"] == "Partly"
    assert j["notes"]


def test_analyze_equals_detect_plus_classify(client):
    body = {**TICKET, "ticketId": "T-1"}
    a, d, c = (post(client, p, body).json() for p in ("/v1/analyze", "/v1/detect", "/v1/classify"))
    envelope = {"requestId", "apiVersion", "ticketId", "models"}
    assert set(a) == envelope | {"detection", "taxonomy"}
    assert a["detection"] == {k: v for k, v in d.items() if k not in envelope}
    assert a["taxonomy"] == {k: v for k, v in c.items() if k not in envelope}
    assert a["models"] == d["models"] == c["models"]


def test_subject_and_org_id_are_optional_and_either_type(client):
    assert post(client, "/v1/detect", {"text": "FAKESQL01 is down"}).status_code == 200
    as_int = post(client, "/v1/detect", {"text": "FAKESQL01 is down", "zendeskOrgId": int(ORG)}).json()
    as_str = post(client, "/v1/detect", {"text": "FAKESQL01 is down", "zendeskOrgId": f" {ORG} "}).json()
    assert as_int["client"] == as_str["client"] == {"zendeskOrgId": ORG, "known": True}
    assert as_int["detections"] == as_str["detections"]
    for path in ("/v1/classify", "/v1/analyze"):
        assert post(client, path, {"text": "disk space low on the data drive"}).status_code == 200


def test_an_unknown_or_missing_org_id_applies_no_clients_names(client):
    known = post(client, "/v1/detect", {"text": "Please check FAKESQL01 today", "zendeskOrgId": ORG}).json()
    assert known["detections"][0]["status"] == "known"
    for body in ({"text": "Please check FAKESQL01 today", "zendeskOrgId": "999999"},
                 {"text": "Please check FAKESQL01 today"}):
        j = post(client, "/v1/detect", body).json()
        assert j["client"]["known"] is False
        assert all(d["status"] != "known" and d["hostCandidates"] == [] and d["typoOf"] is None
                   and d["environment"] is None for d in j["detections"])


def test_ticket_id_is_echoed_and_versions_are_reported(client):
    j = post(client, "/v1/detect", {**TICKET, "ticketId": 12345}).json()
    assert j["ticketId"] == "12345"
    assert j["models"] == {"serverDetector": DETECTOR_CREATED.replace("+00:00", "Z"),
                           "ticketTypes": trained()[2]["created_at"].replace("+00:00", "Z"),
                           "directory": f"model:{DETECTOR_CREATED.replace('+00:00', 'Z')}",
                           "taxonomyVersion": TAXONOMY_VERSION, "modelVersion": None}


def test_request_id_header_is_echoed_when_well_formed(client):
    r = post(client, "/v1/detect", TICKET, **{"X-Request-Id": "caller-req.42"})
    assert r.headers["x-request-id"] == "caller-req.42" == r.json()["requestId"]
    r = post(client, "/v1/detect", TICKET, **{"X-Request-Id": "not ok <script>"})
    assert r.headers["x-request-id"] != "not ok <script>" and len(r.headers["x-request-id"]) == 32
    assert client.get("/health").headers["x-request-id"]


def test_missing_text_and_bad_fields_are_problem_details(client):
    for path in ("/v1/detect", "/v1/classify", "/v1/analyze"):
        r = post(client, path, {"subject": "no text"})
        assert r.status_code == 422 and r.headers["content-type"].startswith("application/problem+json")
        j = r.json()
        assert j["code"] == "validation_error" and j["errors"][0]["loc"] == ["body", "text"]
        assert j["requestId"] == r.headers["x-request-id"]
    for body in ({"text": ""}, {"text": "x", "subject": "s" * 1001}, {"text": "x", "zendeskOrgId": "Fake Co"},
                 {"text": "x", "zendeskOrgId": True}, {"text": "x", "zendeskOrgID": ORG}):
        assert post(client, "/v1/detect", body).status_code == 422, body
    r = post(client, "/v1/detect", {"text": "x" * 200_001})
    assert r.status_code == 422 and "x" * 50 not in r.text  # never echoes the rejected value


def test_invalid_json_non_json_and_oversized_bodies(client):
    r = client.post("/v1/detect", content=b'{"text": ', headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_json"
    r = client.post("/v1/detect", content=b"text=x", headers={"content-type": "text/plain"})
    assert r.status_code == 415 and r.json()["code"] == "unsupported_media_type"
    big = b'{"text": "' + b"x" * 1_000_001 + b'"}'
    r = client.post("/v1/detect", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "body_too_large"

    def chunks():  # no Content-Length: the limit applies to the bytes received
        yield b'{"text": "'
        for _ in range(11):
            yield b"x" * 100_000
        yield b'"}'
    r = client.post("/v1/detect", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_unknown_paths_and_methods_are_problems(client):
    r = client.get("/v1/nothing")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert client.get("/v1/detect").status_code == 405


def test_health_and_ready_need_no_auth_and_give_no_detail(tmp_path):
    from tests.api_support import API_KEY
    with make_client(tmp_path, auth_mode="api_key", api_keys=API_KEY, api_host="0.0.0.0") as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/ready").json() == {"status": "ready"}
        assert c.post("/v1/detect", json=TICKET).status_code == 401


def test_catalog_lists_keys_values_and_rules(client):
    from ticket_analysis.type_rules import RULES
    j = client.get("/v1/catalog").json()
    assert [k["key"] for k in j["detectionKeys"]] == ["host", "instance", "host_instance"]
    fields = {f["name"]: f for f in j["detectionFields"]}
    assert set(fields) == {"key", "value", "text", "host", "instance", "status", "field", "start", "end", "typoOf",
                           "siblingOf", "hostCandidates", "environment", "score"}
    assert {v["value"] for v in fields["status"]["values"]} == set(sd.STATUSES)
    assert {v["value"] for v in fields["field"]["values"]} == {"subject", "description"}
    assert fields["start"]["type"] == "integer" and fields["host"]["type"] == "string or null"
    assert all(f["description"] for f in fields.values())
    sources = {s["value"] for s in j["typeSources"]}
    assert {f"rule:{name}" for name, _ in RULES} | {"model", "alert_name", "alert_name_partly", "alert_default"} == sources
    assert {s["value"] for s in j["intentSources"]} == {"model", "alert_rule"}
    assert len(j["types"]) == len(TAXONOMY["types"]) and all(t["definition"] for t in j["types"])
    assert j["taxonomyVersion"] == TAXONOMY_VERSION and j["models"]["serverDetector"]


def test_catalog_lists_every_status_the_detector_produces_and_no_client_names(client):
    import numpy as np

    from api.service import detection_body
    from ticket_analysis.analysis import detect
    from ticket_analysis.names import ParsedName

    class FixedModel(sd.ServerModel):
        def __init__(self):
            super().__init__(host_threshold=0.5, instance_threshold=0.5)

        def scores(self, cands):
            return np.array([[0.9 if c["host_ok"] and c["host"] in {"fakesql01", "newhost07", "fakesgl01"} else 0.1,
                              0.8 if c["inst_ok"] and c["host"] in {"reports", "zzinst"} else 0.0] for c in cands])

    d = sd.LocalServerDirectory()
    for host, inst in (("fakesql01", "reports"), ("fakesql02", "reports"), ("fakesql01", None)):
        d.add(ORG, None, ParsedName(host, inst, None), "cmdb")
    texts = {"known": "FAKESQL01 is down", "new_instance_on_known_host": "FAKESQL01\\NEWINST is down",
             "possible_new_host": "NEWHOST07 is down", "typo_of_known": "FAKESGL01 is down",
             "ambiguous": "The REPORTS instance is slow", "possible_new_instance": "The ZZINST instance is slow"}
    seen = set()
    for status, text in texts.items():
        body = detection_body(detect(ticket_for_org("", text, ORG), FixedModel(), d), ORG)
        seen |= {x["status"] for x in body["detections"]}
        assert status in {x["status"] for x in body["detections"]}, status
    catalog = {v["value"] for f in client.get("/v1/catalog").json()["detectionFields"] if f["name"] == "status"
               for v in f["values"]}
    assert seen == set(sd.STATUSES) == catalog
    text = client.get("/v1/catalog").text.lower()
    assert "fakesql" not in text and "othsql" not in text and "fake co" not in text


def test_not_ready_when_a_model_file_is_missing_and_nothing_is_trained(tmp_path):
    from tests.api_support import write_models
    folder = write_models(tmp_path / "models", types=False)
    with make_client(tmp_path, write=False) as c:
        assert c.get("/health").status_code == 200
        r = c.get("/ready")
        assert r.status_code == 503 and r.json()["code"] == "not_ready" and "ticket_types" not in r.text
        r = c.post("/v1/detect", json=TICKET)
        assert r.status_code == 503 and r.headers["retry-after"]
        assert c.get("/v1/catalog").status_code == 200  # the catalog needs no models
    assert sorted(p.name for p in folder.iterdir()) == ["server_detector.joblib", "taxonomy_notes.md"]


def test_not_ready_for_a_type_model_of_another_taxonomy(tmp_path):
    import joblib

    from tests.api_support import write_models
    folder = write_models(tmp_path / "models")
    bundle = joblib.load(folder / "ticket_types.joblib")
    joblib.dump({**bundle, "taxonomy_version": "v0"}, folder / "ticket_types.joblib")
    with make_client(tmp_path, write=False) as c:
        assert c.get("/ready").status_code == 503
        assert "v0" not in c.get("/ready").text


def test_a_model_whose_hash_differs_is_refused(tmp_path):
    from api.models import sha256_of
    from tests.api_support import write_models
    folder = write_models(tmp_path / "models")
    good = f"server_detector.joblib={sha256_of(folder / 'server_detector.joblib')}," \
           f"ticket_types.joblib={sha256_of(folder / 'ticket_types.joblib')}"
    with make_client(tmp_path, write=False, model_sha256=good) as c:
        assert c.get("/ready").status_code == 200
    bad = good.replace("ticket_types.joblib=", "ticket_types.joblib=0")
    with make_client(tmp_path, write=False, model_sha256=bad) as c:
        assert c.get("/ready").status_code == 503


def test_the_api_imports_no_tools_and_never_trains(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from tests.api_support import write_models
    write_models(tmp_path / "models", types=False)
    code = ("import sys\n"
            "for name in ('tools', 'anthropic', 'requests'):\n"
            "    sys.modules[name] = None\n"
            "from api.service import Service\n"
            "from api.settings import load_settings\n"
            f"s = load_settings(model_dir=r'{tmp_path / 'models'}', auth_mode='none', allow_no_auth=True, "
            "api_host='127.0.0.1')\n"
            "svc = Service(s)\n"
            "print(svc.load(), svc.ready)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent.parent, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["False", "False"]
    assert not (tmp_path / "models" / "ticket_types.joblib").exists()


def test_no_request_text_or_names_appear_in_logs(tmp_path, caplog, capsys):
    from api.app import create_app
    from tests.api_support import make_settings, local_client, write_models
    write_models(tmp_path / "models")
    secret = "Zebra-Quokka-7731 on FAKESQL01\\REPORTS"
    with caplog.at_level(logging.DEBUG):
        app = create_app(make_settings(tmp_path, log_format="json", log_level="DEBUG"), background=False)
        with local_client(app) as c:
            for path in ("/v1/detect", "/v1/classify", "/v1/analyze"):
                assert c.post(path, json={"text": secret, "subject": "Quokka subject", "zendeskOrgId": ORG,
                                          "ticketId": "TID-55501"}).status_code == 200
            c.post("/v1/detect", json={"text": secret, "subject": "s" * 2000})
            c.post("/v1/detect", content=b'{"text": "Zebra-Quokka', headers={"content-type": "application/json"})
    logged = caplog.text + capsys.readouterr().err
    for needle in ("Zebra", "Quokka", "fakesql01", "FAKESQL01", "REPORTS", "TID-55501"):
        assert needle not in logged, needle
    lines = [r for r in caplog.records if r.name == "api.request" and r.path == "/v1/detect"]
    assert lines and lines[0].textLength == len(secret) and lines[0].orgIdGiven is True
    assert {"method", "path", "status", "latencyMs", "requestId"} <= set(vars(lines[0]))


def test_inference_runs_in_the_thread_pool(tmp_path):
    import threading

    from api.app import create_app
    from tests.api_support import make_settings, local_client, write_models
    write_models(tmp_path / "models")
    app = create_app(make_settings(tmp_path, workers=3), background=False, configure_logging=False)
    svc = app.state.service
    seen = []
    original = svc._detect

    def spy(inp, state):
        seen.append(threading.current_thread().name)
        return original(inp, state)
    svc._detect = spy
    with local_client(app) as c:
        assert c.post("/v1/detect", json=TICKET).status_code == 200
    assert seen and seen[0].startswith("inference") and svc.executor._max_workers == 3


def test_debug_responses_add_candidates_and_rule_checks_locally(tmp_path):
    with make_client(tmp_path, debug_responses=True) as c:
        j = c.post("/v1/analyze", json=TICKET).json()
    cand = j["detection"]["debug"]["candidates"]
    assert cand and {"pHost", "pInstance", "rule", "hostCandidate"} <= set(cand[0])
    assert {r["rule"] for r in j["taxonomy"]["debug"]["rules"]} == {"staff_logon", "validation_results", "sip_review",
                                                                   "database_refresh", "access_request", "ha_terms"}
    assert "cleanedText" in j["taxonomy"]["debug"]


def test_without_auth_only_loopback_requests_are_answered(tmp_path):
    from fastapi.testclient import TestClient
    with make_client(tmp_path) as c:
        assert c.get("/health").status_code == 200
        r = c.get("/health", headers={"Host": "rebind.fake.example:8000"})  # a DNS-rebinding page
        assert r.status_code == 403 and r.json()["code"] == "loopback_only"
        r = TestClient(c.app).post("/v1/detect", json=TICKET)  # another client address and Host
        assert r.status_code == 403


def test_a_request_without_credentials_is_refused_before_its_body_is_read(tmp_path):
    from tests.api_support import API_KEY
    with make_client(tmp_path, auth_mode="api_key", api_keys=API_KEY, api_host="0.0.0.0") as c:
        big = b'{"text": "' + b"x" * 1_000_001 + b'"}'
        r = c.post("/v1/detect", content=big, headers={"content-type": "application/json"})
        assert r.status_code == 401 and r.json()["code"] == "missing_api_key" and r.headers["www-authenticate"]
        r = c.post("/mcp", content=b"{}", headers={"content-type": "application/json"})
        assert r.status_code == 401
        r = c.post("/v1/detect", content=big, headers={"content-type": "application/json", "X-API-Key": API_KEY})
        assert r.status_code == 413


def test_the_alert_tables_own_notes_never_reach_callers(client):
    from tests.test_alert_parser import COLLECTOR
    r = post(client, "/v1/classify", {"text": COLLECTOR, "subject": "Fake Co - Alert #42 - FD - SQL Agent Job Failure"})
    j = r.json()
    assert j["alert"]["tableEntry"] == {"type": "scheduled_jobs", "decides": "Partly", "tickets": 1259}
    assert j["notes"] == ["The body can move this alert to another type."]
    assert "DatabaseBackup" not in r.text


def test_a_failed_model_load_is_retried_until_it_succeeds(tmp_path, monkeypatch):
    import api.service as service_module
    from api.service import Service
    from tests.api_support import make_settings, write_models
    monkeypatch.setattr(service_module, "LOAD_RETRY_FIRST", 0.05)
    svc = Service(make_settings(tmp_path))
    attempts = []
    original = svc.load

    def load():
        attempts.append(1)
        if len(attempts) == 2:  # the files arrive after the first failure, as a role assignment propagates
            write_models(tmp_path / "models")
        return original()
    svc.load = load
    svc.start_loading()
    assert svc.wait_loaded(timeout=60) and len(attempts) >= 2
    svc.close()
