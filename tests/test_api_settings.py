"""Settings, the local profile and the python -m api runner (Uvicorn is replaced; nothing is served)."""

import pytest

import api.__main__ as runner
from api.settings import ConfigError, load_settings, local_profile
from ticket_analysis.paths import DATA_DIR


@pytest.fixture
def served(monkeypatch):
    """Uvicorn replaced; the runner writes the profile into os.environ, so it is restored afterwards."""
    import os

    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr("api.refresh_directory.bootstrap_local_snapshot", lambda settings: None)
    saved = dict(os.environ)
    yield calls
    os.environ.clear()
    os.environ.update(saved)


def test_the_local_profile_points_at_data_and_turns_auth_off(monkeypatch):
    p = local_profile()
    assert p["MODEL_DIR"] == str(DATA_DIR / "models") and p["AUTH_MODE"] == "none" and p["ALLOW_NO_AUTH"] == "true"
    assert p["API_HOST"] == "127.0.0.1" and p["ENABLE_DOCS"] == "true" and p["CMDB_SOURCE"] == "csv"
    monkeypatch.setenv("MODEL_DIR", "D:/elsewhere")
    assert local_profile()["MODEL_DIR"] == "D:/elsewhere"  # the environment wins
    assert "ALLOW_NO_AUTH" not in local_profile("api_key")


def test_nothing_defaults_to_data_outside_the_local_profile():
    s = load_settings()
    assert "data" not in str(s.model_dir).replace("\\", "/").split("/")
    assert not s.directory_store and not s.model_store and s.cmdb_csv_path is None
    assert s.auth_mode == "api_key" and s.enable_docs is False and s.debug_responses is False


def test_model_file_names_must_not_be_paths():
    for name in ("../server_detector.joblib", "sub/server_detector.joblib", "C:\\models\\x.joblib"):
        with pytest.raises(ConfigError, match="file name"):
            load_settings(server_model_file=name)
    with pytest.raises(ConfigError, match="MODEL_VERSION"):
        load_settings(model_store="D:/store")


def test_the_local_runner_serves_loopback_with_reload(monkeypatch, served):
    runner.main(["--local"])
    kw = served[0]
    assert kw["host"] == "127.0.0.1" and kw["port"] == 8000 and kw["reload"] is True and kw["factory"] is True


def test_the_local_runner_refuses_other_addresses_without_auth(monkeypatch, served):
    with pytest.raises(SystemExit, match="loopback"):
        runner.main(["--local", "--host", "0.0.0.0"])
    assert not served


def test_api_key_auth_needs_a_key_in_env_file(monkeypatch, tmp_path, served):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit, match="API_KEYS="):
        runner.main(["--local", "--auth", "api_key"])
    (tmp_path / ".env").write_text("API_KEYS=fake-local-key-0123456789\n", encoding="utf-8")
    runner.main(["--local", "--auth", "api_key", "--host", "0.0.0.0", "--no-reload"])
    assert served[-1]["host"] == "0.0.0.0" and served[-1]["reload"] is False


def test_debug_responses_only_without_auth_on_loopback(monkeypatch, served):
    runner.main(["--local", "--debug-responses", "--no-reload"])
    assert served
    monkeypatch.setenv("API_KEYS", "fake-local-key-0123456789")
    with pytest.raises(SystemExit, match="DEBUG_RESPONSES"):
        runner.main(["--local", "--auth", "api_key", "--debug-responses"])


def test_the_container_runner_needs_real_settings(monkeypatch, served):
    with pytest.raises(SystemExit, match="API_KEYS"):
        runner.main([])
    monkeypatch.setenv("API_KEYS", "fake-container-key-0123456789")
    runner.main([])
    assert served[-1]["host"] == "0.0.0.0" and served[-1]["reload"] is False
