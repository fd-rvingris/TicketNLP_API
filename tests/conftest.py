import socket
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any test that tries to open a network connection."""
    def guard(*args, **kwargs):
        raise RuntimeError("tests must not use the network")
    real_connect, real_socketpair = socket.socket.connect, socket.socketpair

    def socketpair(*args, **kwargs):
        # On Windows an asyncio event loop builds its self-pipe from a loopback connection; that is not network use
        with monkeypatch.context() as m:
            m.setattr(socket.socket, "connect", real_connect)
            return real_socketpair(*args, **kwargs)
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "socketpair", socketpair)


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


@pytest.fixture(autouse=True)
def api_settings_from_tests_only(monkeypatch):
    """The API reads its settings from the environment; keep the developer's own settings out of tests."""
    from api.settings import Settings
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
