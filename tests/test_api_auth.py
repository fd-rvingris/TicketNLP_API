"""Auth modes: none, api_key and entra. Entra tokens are signed with a key pair made here; no network."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api.auth import Authenticator, AuthError
from api.settings import ConfigError, load_settings
from tests.api_support import API_KEY, NEW_KEY, TICKET, make_settings, local_client, write_models

TENANT = "00000000-0000-0000-0000-000000000001"
AUDIENCE = "api://fake-ticket-analysis"
ROLE = "Tickets.Analyze"


@pytest.fixture(scope="module")
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, other


def token(private, *, aud=AUDIENCE, iss=f"https://login.microsoftonline.com/{TENANT}/v2.0", roles=(ROLE,),
          exp_in=600, tid=TENANT, alg="RS256"):
    now = int(time.time())
    claims = {"aud": aud, "iss": iss, "tid": tid, "iat": now, "nbf": now, "exp": now + exp_in, "azp": "fake-app-id",
              "roles": list(roles)}
    return jwt.encode(claims, private, algorithm=alg, headers={"kid": "fake-kid"})


def app_client(tmp_path, authenticator=None, **values):
    from api.app import create_app
    write_models(tmp_path / "models")
    settings = make_settings(tmp_path, api_host="0.0.0.0", **values)
    return local_client(create_app(settings, background=False, configure_logging=False, authenticator=authenticator))


def entra_settings(tmp_path):
    return make_settings(tmp_path, api_host="0.0.0.0", auth_mode="entra", entra_tenant_id=TENANT,
                         entra_audience=f"{AUDIENCE},fake-client-id", entra_required_role=ROLE)


def test_api_key_mode_needs_a_valid_key(tmp_path):
    with app_client(tmp_path, auth_mode="api_key", api_keys=API_KEY) as c:
        r = c.post("/v1/detect", json=TICKET)
        assert r.status_code == 401 and r.json()["code"] == "missing_api_key" and r.headers["www-authenticate"]
        r = c.post("/v1/detect", json=TICKET, headers={"X-API-Key": "fake-wrong-key-000000"})
        assert r.status_code == 401 and r.json()["code"] == "invalid_api_key"
        assert c.post("/v1/detect", json=TICKET, headers={"X-API-Key": API_KEY}).status_code == 200
        assert c.get("/v1/catalog").status_code == 401
        # the auth check runs before the body is looked at
        assert c.post("/v1/detect", json={"subject": "no text"}).status_code == 401


def test_several_keys_allow_rotation(tmp_path):
    with app_client(tmp_path, auth_mode="api_key", api_keys=f"{API_KEY}, {NEW_KEY}") as c:
        for key in (API_KEY, NEW_KEY):
            assert c.post("/v1/classify", json=TICKET, headers={"X-API-Key": key}).status_code == 200
    with app_client(tmp_path, auth_mode="api_key", api_keys=NEW_KEY) as c:  # the old key retired
        assert c.post("/v1/classify", json=TICKET, headers={"X-API-Key": API_KEY}).status_code == 401


def test_none_mode_needs_allow_no_auth_and_loopback(tmp_path):
    with pytest.raises(ConfigError, match="ALLOW_NO_AUTH"):
        load_settings(auth_mode="none", api_host="127.0.0.1").check_serving()
    with pytest.raises(ConfigError, match="loopback"):
        load_settings(auth_mode="none", allow_no_auth=True, api_host="0.0.0.0").check_serving()
    load_settings(auth_mode="none", allow_no_auth=True, api_host="127.0.0.1").check_serving()
    with pytest.raises(ConfigError, match="API_KEYS"):
        load_settings(auth_mode="api_key").check_serving()
    with pytest.raises(ConfigError, match="16 characters"):
        load_settings(auth_mode="api_key", api_keys="short").check_serving()
    with pytest.raises(ConfigError, match="ENTRA_TENANT_ID"):
        load_settings(auth_mode="entra").check_serving()
    with pytest.raises(ConfigError, match="DEBUG_RESPONSES"):
        load_settings(auth_mode="api_key", api_keys=API_KEY, debug_responses=True).check_serving()


def test_a_config_error_never_repeats_secret_values():
    with pytest.raises(ConfigError) as e:
        load_settings(auth_mode="api_key", api_keys="fake-secret-value-123456", workers=0)
    assert "fake-secret-value" not in str(e.value) and "workers" in str(e.value)


def test_switching_auth_mode_is_configuration_only(tmp_path, keys):
    private, _ = keys
    for values, headers in (({"auth_mode": "api_key", "api_keys": API_KEY}, {"X-API-Key": API_KEY}),
                            ({"auth_mode": "none", "allow_no_auth": True, "api_host": "127.0.0.1"}, {})):
        from api.app import create_app
        write_models(tmp_path / "models")
        with local_client(create_app(make_settings(tmp_path, **values), background=False, configure_logging=False)) as c:
            assert c.post("/v1/detect", json=TICKET, headers=headers).status_code == 200


def test_entra_token_checks(tmp_path, keys):
    private, other = keys
    auth = Authenticator(entra_settings(tmp_path), key_resolver=lambda t: private.public_key())
    assert auth.check(None, token(private)).caller == "fake-app-id"
    assert auth.check(None, token(private, aud="fake-client-id")).mode == "entra"
    assert auth.check(None, token(private, iss=f"https://sts.windows.net/{TENANT}/")).mode == "entra"
    cases = {"missing_token": None, "token_expired": token(private, exp_in=-3600),
             "invalid_audience": token(private, aud="api://another-api"),
             "invalid_issuer": token(private, iss="https://login.microsoftonline.com/another-tenant/v2.0"),
             "invalid_token": token(other)}
    for code, tok in cases.items():
        with pytest.raises(AuthError) as e:
            auth.check(None, tok)
        assert (e.value.status, e.value.code) == (401, code), code
    with pytest.raises(AuthError) as e:
        auth.check(None, token(private, roles=("Tickets.Read",)))
    assert (e.value.status, e.value.code) == (403, "missing_role")
    with pytest.raises(AuthError):
        auth.check(None, jwt.encode({"aud": AUDIENCE}, "fake-shared-secret-long-enough-0000", algorithm="HS256"))


def test_entra_mode_over_http(tmp_path, keys):
    private, _ = keys
    settings = entra_settings(tmp_path)
    auth = Authenticator(settings, key_resolver=lambda t: private.public_key())
    with app_client(tmp_path, authenticator=auth, auth_mode="entra", entra_tenant_id=TENANT,
                    entra_audience=AUDIENCE, entra_required_role=ROLE) as c:
        ok = {"Authorization": f"Bearer {token(private)}"}
        assert c.post("/v1/detect", json=TICKET, headers=ok).status_code == 200
        r = c.post("/v1/detect", json=TICKET, headers={"Authorization": f"Bearer {token(private, exp_in=-3600)}"})
        assert r.status_code == 401 and r.json()["code"] == "token_expired"
        r = c.post("/v1/detect", json=TICKET, headers={"Authorization": f"Bearer {token(private, roles=())}"})
        assert r.status_code == 403 and r.json()["code"] == "missing_role"
        # an API key does nothing in entra mode
        assert c.post("/v1/detect", json=TICKET, headers={"X-API-Key": API_KEY}).status_code == 401


def test_the_mcp_endpoint_is_behind_the_same_auth(tmp_path):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with app_client(tmp_path, auth_mode="api_key", api_keys=API_KEY) as c:
        r = c.post("/mcp", json=rpc, headers=headers)
        assert r.status_code == 401 and r.json()["code"] == "missing_api_key"
        r = c.post("/mcp", json=rpc, headers={**headers, "X-API-Key": "fake-wrong-key-000000"})
        assert r.status_code == 401
        r = c.post("/mcp", json=rpc, headers={**headers, "X-API-Key": API_KEY})
        assert r.status_code not in (401, 403)


def test_openapi_declares_both_security_schemes(tmp_path):
    with app_client(tmp_path, auth_mode="api_key", api_keys=API_KEY) as c:
        doc = c.get("/openapi.json").json()
    schemes = doc["components"]["securitySchemes"]
    assert schemes["ApiKeyAuth"] == {"type": "apiKey", "in": "header", "name": "X-API-Key",
                                     "description": schemes["ApiKeyAuth"]["description"]}
    assert schemes["EntraBearer"]["scheme"] == "bearer" and schemes["EntraBearer"]["bearerFormat"] == "JWT"
    assert doc["paths"]["/v1/detect"]["post"]["security"] == [{"ApiKeyAuth": []}, {"EntraBearer": []}]
    assert "security" not in doc["paths"]["/health"]["get"]
