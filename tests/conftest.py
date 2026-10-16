import socket
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any test that tries to open a network connection."""
    def guard(*args, **kwargs):
        raise RuntimeError("tests must not use the network")
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


def fake_response(stop_reason="end_turn", text=None, model="claude-opus-5-5", category=None, iterations=None):
    """A stand-in for the SDK's BetaMessage with only the fields the code reads."""
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                            cache_read_input_tokens=1500, iterations=iterations)
    details = SimpleNamespace(type="refusal", category=category, explanation=None) if stop_reason == "refusal" else None
    return SimpleNamespace(model=model, stop_reason=stop_reason, content=content, usage=usage,
                           stop_details=details, _request_id="req_test")
