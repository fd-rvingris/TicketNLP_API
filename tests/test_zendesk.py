from tools.common import ZendeskClient
from tools.fetch_tickets import normalize


class FakeResponse:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data or {}, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class FakeSession:
    """Only implements get(), so any other HTTP verb would fail the test."""

    def __init__(self, responses):
        self.responses, self.calls, self.headers, self.auth = list(responses), [], {}, None

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


def client(responses):
    sleeps = []
    session = FakeSession(responses)
    zd = ZendeskClient("acme", "agent@example.com", "tok", session=session, sleep=sleeps.append)
    return zd, session, sleeps


def test_basic_auth_uses_token_form():
    zd, session, _ = client([])
    assert session.auth == ("agent@example.com/token", "tok")


def test_429_honors_retry_after():
    zd, session, sleeps = client([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, {"ok": 1})])
    assert zd.get("tickets/1.json") == {"ok": 1}
    assert sleeps == [7.0]
    assert session.calls[0][0] == "https://acme.zendesk.com/api/v2/tickets/1.json"


def test_offset_pagination_follows_next_page_and_drops_params():
    pages = [FakeResponse(200, {"results": [{"id": 1}, {"id": 2}], "next_page": "https://acme.zendesk.com/p2"}),
             FakeResponse(200, {"results": [{"id": 3}], "next_page": None})]
    zd, session, _ = client(pages)
    assert [r["id"] for r in zd.paginate("search.json", "results", {"query": "type:ticket"})] == [1, 2, 3]
    assert session.calls[1] == ("https://acme.zendesk.com/p2", None)


def test_pagination_stops_at_limit():
    pages = [FakeResponse(200, {"results": [{"id": 1}, {"id": 2}], "next_page": "https://acme.zendesk.com/p2"})]
    zd, session, _ = client(pages)
    assert [r["id"] for r in zd.paginate("search.json", "results", limit=2)] == [1, 2]
    assert len(session.calls) == 1


def test_cursor_pagination():
    pages = [FakeResponse(200, {"ticket_fields": [{"id": 1}], "meta": {"has_more": True},
                                "links": {"next": "https://acme.zendesk.com/c2"}}),
             FakeResponse(200, {"ticket_fields": [{"id": 2}], "meta": {"has_more": False},
                                "links": {"next": "https://acme.zendesk.com/c3"}})]
    zd, session, _ = client(pages)
    assert [f["id"] for f in zd.paginate("ticket_fields.json", "ticket_fields")] == [1, 2]
    assert len(session.calls) == 2


def test_normalize_keeps_weak_labels():
    raw = {"id": 42, "subject": "Disk full", "description": "E: is full", "organization_id": 100,
           "created_at": "2026-10-01T10:00:00Z", "tags": ["disk"], "type": "incident",
           "custom_fields": [{"id": 9, "value": "storage"}, {"id": 10, "value": "ignored"}, {"id": 9, "value": None}]}
    ticket = normalize(raw, {"100": "Acme"}, {"9": "Issue Category"})
    assert ticket["ticket_id"] == "42"
    assert ticket["organization_id"] == "100" and ticket["organization_name"] == "Acme"
    assert ticket["weak_labels"] == {"zendesk_type": "incident", "Issue Category": "storage"}
