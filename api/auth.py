"""One auth layer for /v1, chosen by AUTH_MODE; switching is configuration, not code.

- none: no check. Allowed only with ALLOW_NO_AUTH=true on a loopback address (settings.py refuses
  anything else) and logged as a warning at startup.
- api_key (default): the X-API-Key header, compared in constant time with every key in API_KEYS
  (in Azure a Key Vault reference). Several keys let a key be rotated without downtime.
- entra: an Entra ID bearer token (Authorization: Bearer ...), checked for signature against the
  tenant's published keys (cached), issuer, audience and expiry, and required to carry the app role
  ENTRA_REQUIRED_ROLE.

401 means no or bad credentials, 403 valid credentials without the required role.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from typing import Callable, Mapping

from fastapi import Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from .settings import Settings

log = logging.getLogger("api.auth")

JWKS_URL = "https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
LEEWAY_SECONDS = 60
JWKS_CACHE_SECONDS = 3600

api_key_scheme = APIKeyHeader(name="X-API-Key", scheme_name="ApiKeyAuth", auto_error=False,
                              description="An API key from API_KEYS (AUTH_MODE=api_key).")
bearer_scheme = HTTPBearer(scheme_name="EntraBearer", bearerFormat="JWT", auto_error=False,
                           description="An Entra ID access token for the API's app registration, carrying the "
                                       "required app role (AUTH_MODE=entra).")


class AuthError(Exception):
    def __init__(self, status: int, code: str, detail: str, scheme: str | None = None):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail
        self.headers = {"WWW-Authenticate": scheme} if scheme and status == 401 else {}


class AuthUnavailable(Exception):
    """The token signing keys could not be fetched; a server-side problem (503)."""


@dataclass(frozen=True)
class Principal:
    mode: str
    caller: str  # for logs: key-<n> or the token's app id; never a secret
    roles: tuple[str, ...] = field(default=())


def _missing_key() -> AuthError:
    return AuthError(401, "missing_api_key", "Send an API key in the X-API-Key header.", "ApiKey")


def _missing_token() -> AuthError:
    return AuthError(401, "missing_token", "Send an Entra ID access token as Authorization: Bearer <token>.", "Bearer")


def _bearer(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class Authenticator:
    """key_resolver(token) returns the public key that signed an Entra token. The default fetches the
    tenant's keys over HTTPS and caches them; tests pass a local key instead."""

    def __init__(self, settings: Settings, key_resolver: Callable[[str], object] | None = None):
        self.mode = settings.auth_mode
        self._keys = [k.encode("utf-8") for k in settings.api_key_list]
        self.tenant, self.audiences, self.role = settings.entra_tenant_id, settings.audiences, settings.entra_required_role
        self.issuers = [f"https://login.microsoftonline.com/{self.tenant}/v2.0", f"https://sts.windows.net/{self.tenant}/"]
        self._resolve = key_resolver
        if self.mode == "entra" and key_resolver is None:
            import jwt
            client = jwt.PyJWKClient(JWKS_URL.format(tenant=self.tenant), cache_keys=True,
                                     lifespan=JWKS_CACHE_SECONDS, timeout=10)
            self._resolve = lambda token: client.get_signing_key_from_jwt(token).key
        if self.mode == "none":
            log.warning("AUTH_MODE=none: requests are not authenticated (local runs only)")

    def check(self, api_key: str | None, bearer_token: str | None) -> Principal:
        if self.mode == "none":
            return Principal("none", "anonymous")
        if self.mode == "api_key":
            return self._check_key(api_key)
        return self._check_token(bearer_token)

    def missing_credentials(self, headers: Mapping[str, str]) -> AuthError | None:
        """The error check() would raise when the request carries no credential at all, so the
        middleware can refuse it before reading the body; None when there is one or none is needed."""
        if self.mode == "api_key" and not headers.get("x-api-key"):
            return _missing_key()
        if self.mode == "entra" and not _bearer(headers.get("authorization")):
            return _missing_token()
        return None

    def _check_key(self, api_key: str | None) -> Principal:
        if not api_key:
            raise _missing_key()
        given = api_key.encode("utf-8")
        match = None
        for n, key in enumerate(self._keys, 1):  # compare with every key, so timing does not reveal which
            if hmac.compare_digest(given, key):
                match = n
        if match is None:
            raise AuthError(401, "invalid_api_key", "The API key is not valid.", "ApiKey")
        return Principal("api_key", f"key-{match}")

    def _check_token(self, token: str | None) -> Principal:
        import jwt
        if not token:
            raise _missing_token()
        try:
            if jwt.get_unverified_header(token).get("alg") != "RS256":
                raise AuthError(401, "invalid_token", "The token is not signed with RS256.", 'Bearer error="invalid_token"')
            key = self._resolve(token)
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audiences, issuer=self.issuers,
                                leeway=LEEWAY_SECONDS, options={"require": ["exp", "iss", "aud"]})
        except AuthError:
            raise
        except jwt.ExpiredSignatureError:
            raise AuthError(401, "token_expired", "The token has expired.", 'Bearer error="invalid_token"') from None
        except jwt.InvalidAudienceError:
            raise AuthError(401, "invalid_audience", "The token is not for this API.", 'Bearer error="invalid_token"') from None
        except jwt.InvalidIssuerError:
            raise AuthError(401, "invalid_issuer", "The token is not from the expected tenant.",
                            'Bearer error="invalid_token"') from None
        except jwt.PyJWKClientConnectionError:
            raise AuthUnavailable() from None
        except (jwt.InvalidTokenError, jwt.PyJWKClientError, ValueError):
            raise AuthError(401, "invalid_token", "The token is not valid.", 'Bearer error="invalid_token"') from None
        if claims.get("tid") not in (None, self.tenant):
            raise AuthError(401, "invalid_issuer", "The token is not from the expected tenant.",
                            'Bearer error="invalid_token"')
        roles = tuple(claims.get("roles") or ())
        if self.role not in roles:
            raise AuthError(403, "missing_role", f"The token does not carry the {self.role} app role.")
        return Principal("entra", str(claims.get("azp") or claims.get("appid") or claims.get("oid") or "unknown"), roles)


def require_auth(request: Request, api_key: str | None = Security(api_key_scheme),
                 bearer: HTTPAuthorizationCredentials | None = Security(bearer_scheme)) -> Principal:
    """The FastAPI dependency applied to every /v1 route. A plain function, so FastAPI runs it in its
    thread pool and a key fetch never blocks the event loop."""
    principal = request.app.state.authenticator.check(api_key, bearer.credentials if bearer else None)
    request.state.caller = principal.caller
    return principal
