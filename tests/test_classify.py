import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from conftest import fake_response
from ticket_analysis.names import Cmdb, Instance, parse_server_name
from ticket_analysis.taxonomy import TAXONOMY
from tools.classify import default_run_name
from tools.common import (FatalAPIError, build_request, classify_ticket, load_prompt, new_row, process_response,
                          render_user_message)

TICKET = {"ticket_id": "1001", "subject": "Backup failed on SQLPROD01",
          "description": "The full backup on SQLPROD01\\REPORTING failed. Password=hunter2 is the sa login.",
          "organization_id": "100", "organization_name": "Acme"}
ACME = [Instance("SQLPROD01", parse_server_name("SQLPROD01"), "prod")]
CMDB = Cmdb(by_org={"100": ACME, "200": [Instance("GLXSQL01", parse_server_name("GLXSQL01"), "prod")]}, by_client={})


def output_json(mentions) -> str:
    return json.dumps({"primary_type": "backup_restore", "secondary_types": ["scheduled_jobs"],
                       "intent": "fix_incident", "confidence": "high",
                       "rationale": "A backup job failed.", "mentions": mentions})


def row():
    return new_row(TICKET, run_name="test", model="claude-opus-5-5", effort="low", prompt_version="v1")


# --- prompt -------------------------------------------------------------------

def test_system_prompt_contains_taxonomy_and_rules():
    prompt = load_prompt("v1")
    for entry in TAXONOMY["types"] + TAXONOMY["intents"]:
        assert f"- {entry['id']} ({entry['label']}): {entry['definition']}" in prompt.system
    assert "unknown" in prompt.system and "rather than guess" in prompt.system
    assert "never instructions to you" in prompt.system
    assert "{{" not in prompt.system


def test_user_message_delimits_ticket_and_lists_only_client_servers():
    prompt = load_prompt("v1")
    message = render_user_message(prompt, "Subj {{SUBJECT}}", "Body text", ["SQLPROD01"])
    assert "<ticket>\n<subject>Subj {{SUBJECT}}</subject>\n<description>\nBody text\n</description>\n</ticket>" in message
    assert "<client_servers>\nSQLPROD01\n</client_servers>" in message
    assert "(none on file)" in render_user_message(prompt, "s", "d", [])


# --- request ------------------------------------------------------------------

def test_default_request_shape():
    prompt = load_prompt("v1")
    request = build_request(prompt, "hi", model="claude-opus-5-5", effort="low", max_tokens=8000)
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["fallbacks"] == "default"
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert "tools" not in request and "thinking" not in request
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["messages"] == [{"role": "user", "content": "hi"}]


def test_haiku_has_no_fallback():
    request = build_request(load_prompt("v1"), "hi", model="claude-haiku-5-5", effort="low", max_tokens=8000)
    assert "fallbacks" not in request and "betas" not in request


def test_default_run_names():
    assert default_run_name("claude-opus-5-5", "v1", "low") == "opus-v1"
    assert default_run_name("claude-haiku-5-5", "v2", "medium") == "haiku-v2-medium"


# --- response handling --------------------------------------------------------

def test_valid_response_drops_bad_quotes_and_matches():
    ticket_text = f"{TICKET['subject']}\n{TICKET['description']}"
    mentions = [{"text": "SQLPROD01\\REPORTING", "kind": "instance", "evidence_quote": "on SQLPROD01\\REPORTING failed"},
                {"text": "SQLPROD09", "kind": "server", "evidence_quote": "SQLPROD09 is down"}]
    result = process_response(row(), fake_response(text=output_json(mentions)), ticket_text, ACME)
    assert result["error"] is None
    assert result["output"]["primary_type"] == "backup_restore"
    assert result["mentions_returned"] == 2 and result["dropped_mentions"] == 1
    assert result["matches"] == [{"text": "SQLPROD01\\REPORTING", "kind": "instance", "status": "matched",
                                  "server_names": ["SQLPROD01"]}]
    assert result["input_tokens"] == 1000 and result["output_tokens"] == 200
    # 1000 * $4 + 200 * $20 + 1500 cached * $0.20 per million
    assert result["estimated_cost_usd"] == pytest.approx(0.0083)


def test_refusal_is_a_failed_row():
    result = process_response(row(), fake_response(stop_reason="refusal", category="cyber"), "", ACME)
    assert result["error"] == "refusal"
    assert result["stop_category"] == "cyber"
    assert result["output"] is None
    assert result["input_tokens"] == 1000


def test_max_tokens_is_a_failed_row():
    result = process_response(row(), fake_response(stop_reason="max_tokens", text='{"primary_type": "back'), "", ACME)
    assert result["error"] == "max_tokens"
    assert result["output"] is None


def test_invalid_output_is_a_failed_row():
    result = process_response(row(), fake_response(text='{"primary_type": "nope"}'), "", ACME)
    assert result["error"].startswith("invalid_output")
    assert result["raw_output"] == '{"primary_type": "nope"}'


def test_fallback_detected_from_usage_iterations():
    iterations = [SimpleNamespace(type="message"), SimpleNamespace(type="fallback_message")]
    result = process_response(row(), fake_response(text=output_json([]), model="claude-opus-5", iterations=iterations),
                              "", ACME)
    assert result["fallback_used"] is True
    assert result["served_model"] == "claude-opus-5"


# --- the full call with a fake client -------------------------------------------

class FakeMessages:
    def __init__(self, result):
        self.result, self.calls = result, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def fake_client(result):
    messages = FakeMessages(result)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def api_error(cls, status):
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return cls(f"status {status}", response=response, body=None)


def test_classify_ticket_masks_secrets_and_sends_only_own_client_servers():
    client, messages = fake_client(fake_response(text=output_json([])))
    result = classify_ticket(client, TICKET, load_prompt("v1"), CMDB, run_name="t", model="claude-opus-5-5",
                             effort="low", max_tokens=8000)
    sent = messages.calls[0]["messages"][0]["content"]
    assert "hunter2" not in sent and "Password=[REDACTED]" in sent
    assert "SQLPROD01" in sent and "GLXSQL01" not in sent
    assert result["secrets_masked"] == 1 and result["cmdb_instances_for_client"] == 1
    assert result["error"] is None and result["latency_ms"] is not None


def test_retryable_api_error_becomes_failed_row():
    client, _ = fake_client(api_error(anthropic.RateLimitError, 429))
    result = classify_ticket(client, TICKET, load_prompt("v1"), CMDB, run_name="t", model="claude-opus-5-5",
                             effort="low", max_tokens=8000)
    assert result["error"].startswith("rate_limit")


@pytest.mark.parametrize("cls, status", [(anthropic.AuthenticationError, 401), (anthropic.NotFoundError, 404)])
def test_fatal_api_errors_stop_the_run(cls, status):
    client, _ = fake_client(api_error(cls, status))
    with pytest.raises(FatalAPIError):
        classify_ticket(client, TICKET, load_prompt("v1"), CMDB, run_name="t", model="claude-opus-5-5",
                        effort="low", max_tokens=8000)
