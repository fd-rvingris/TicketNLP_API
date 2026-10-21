"""Every API setting, read once from environment variables (docs/api.md lists them).

Nothing defaults to a path under data/: local runs get their paths from the local profile
(local_profile(), used by python -m api --local and the stdio MCP server).
"""

from __future__ import annotations

import ipaddress
import os
import re
from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ticket_analysis.paths import DATA_DIR

MIN_API_KEY_LENGTH = 16
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ConfigError(ValueError):
    """A setting is missing or unsafe; the service refuses to start."""


def load_settings(**values) -> "Settings":
    """Settings from the environment (values override it), with a readable error that never repeats
    the values given, since some are secrets."""
    try:
        return Settings(**values)
    except ValidationError as e:
        reasons = [(f"{'.'.join(map(str, err['loc']))}: " if err["loc"] else "") + err["msg"].removeprefix("Value error, ")
                   for err in e.errors(include_input=False, include_url=False)]
        raise ConfigError("Invalid settings: " + "; ".join(reasons)) from None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True, protected_namespaces=())

    # --- Models ---------------------------------------------------------------------
    model_dir: Path = Field(Path("/models"), description="Local folder the model files are loaded from. With "
                            "MODEL_STORE set, they are downloaded into it first.")
    server_model_file: str = "server_detector.joblib"
    types_model_file: str = "ticket_types.joblib"
    taxonomy_notes_file: str = Field("taxonomy_notes.md", description="The alert-name table; a file name in "
                                     "MODEL_DIR (and in the store's version folder) or an absolute path.")
    model_sha256: str = Field("", description="file=sha256 pairs, comma-separated; a model file whose hash "
                              "differs is refused.")
    model_version: str = Field("", description="The store's version folder, models/<MODEL_VERSION>/.")
    model_store: str = Field("", description="Local folder or Blob container URL holding models/<version>/.")

    # --- Known-names directory ------------------------------------------------------
    directory_store: str = Field("", description="Local folder or Blob container URL (with an optional path) "
                                 "holding directory-<UTC timestamp>.json snapshots.")
    directory_refresh_minutes: float = Field(60, gt=0)
    directory_keep: int = Field(14, ge=1)
    directory_min_share: float = Field(0.9, ge=0, le=1)
    directory_include_dw: bool = True

    # --- Keystone CMDB (the refresh job) --------------------------------------------
    cmdb_source: Literal["keystone", "csv", "none"] = "keystone"
    cmdb_csv_path: Path | None = None
    cmdb_base_url: str = ""
    cmdb_api_key: SecretStr = SecretStr("")
    cmdb_page_size: int = Field(500, ge=1)
    cmdb_timeout_seconds: float = Field(30, gt=0)

    # --- Auth -----------------------------------------------------------------------
    auth_mode: Literal["none", "api_key", "entra"] = "api_key"
    api_keys: SecretStr = SecretStr("")
    allow_no_auth: bool = False
    entra_tenant_id: str = ""
    entra_audience: str = ""
    entra_required_role: str = ""

    # --- Service --------------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = Field(8000, ge=1, le=65535)
    enable_docs: bool = False
    workers: int = Field(4, ge=1, le=64)
    max_concurrent_requests: int = Field(64, ge=1, description="Connections and requests one process serves at "
                                         "once; Uvicorn answers 503 beyond it (bounds memory: bodies are up to 1 MB).")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    mcp_enabled: bool = True
    debug_responses: bool = False

    @cached_property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.get_secret_value().split(",") if k.strip()]

    @cached_property
    def sha256(self) -> dict[str, str]:
        pairs = [p.strip() for p in self.model_sha256.split(",") if p.strip()]
        out = {}
        for p in pairs:
            name, _, digest = p.partition("=")
            out[name.strip()] = digest.strip().lower()
        return out

    @cached_property
    def audiences(self) -> list[str]:
        return [a.strip() for a in self.entra_audience.split(",") if a.strip()]

    @property
    def loopback(self) -> bool:
        return is_loopback(self.api_host)

    def notes_path(self) -> Path:
        path = Path(self.taxonomy_notes_file)
        return path if path.is_absolute() else self.model_dir / path

    def check_serving(self) -> None:
        """The checks for serving requests (the refresh job does not need them). Raises ConfigError."""
        if self.auth_mode == "none" and not self.allow_no_auth:
            raise ConfigError("AUTH_MODE=none also needs ALLOW_NO_AUTH=true (local runs only)")
        if self.auth_mode == "none" and not self.loopback:
            raise ConfigError("AUTH_MODE=none is allowed only on a loopback address (API_HOST)")
        if self.auth_mode == "api_key":
            if not self.api_key_list:
                raise ConfigError("AUTH_MODE=api_key needs API_KEYS (comma-separated)")
            if any(len(k) < MIN_API_KEY_LENGTH for k in self.api_key_list):
                raise ConfigError(f"Every API key needs at least {MIN_API_KEY_LENGTH} characters")
        if self.auth_mode == "entra":
            missing = [n for n in ("entra_tenant_id", "entra_audience", "entra_required_role") if not getattr(self, n)]
            if missing:
                raise ConfigError("AUTH_MODE=entra needs " + ", ".join(n.upper() for n in missing))
        if self.debug_responses and not (self.auth_mode == "none" and self.loopback):
            raise ConfigError("DEBUG_RESPONSES=true is allowed only with AUTH_MODE=none on a loopback address")

    @model_validator(mode="after")
    def _check(self):
        for name in ("server_model_file", "types_model_file"):
            if not _FILE_NAME.match(getattr(self, name)) or ".." in getattr(self, name):
                raise ValueError(f"{name.upper()} must be a file name in MODEL_DIR, not a path")
        if self.model_store and not self.model_version:
            raise ValueError("MODEL_STORE needs MODEL_VERSION (the models/<version>/ folder to load)")
        if self.model_version and not _FILE_NAME.match(self.model_version):
            raise ValueError("MODEL_VERSION must be a folder name (letters, digits, '.', '_' and '-')")
        return self


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


# --- Local profile ---------------------------------------------------------------------

LOCAL_DIRECTORY = DATA_DIR / "directory"


def local_profile(auth: str = "none") -> dict[str, str]:
    """The settings of a local run: models and taxonomy notes from data/, snapshots in data/directory
    (built from data/instances.csv and the model's alert names on first start), no auth on loopback,
    /docs on and readable console logs. Values already in the environment win."""
    profile = {
        "MODEL_DIR": str(DATA_DIR / "models"),
        "TAXONOMY_NOTES_FILE": str(DATA_DIR / "claude_pack" / "returned" / "taxonomy_notes.md"),
        "DIRECTORY_STORE": str(LOCAL_DIRECTORY),
        "CMDB_SOURCE": "csv",
        "CMDB_CSV_PATH": str(DATA_DIR / "instances.csv"),
        "API_HOST": "127.0.0.1",
        "ENABLE_DOCS": "true",
        "LOG_FORMAT": "text",
        "AUTH_MODE": auth,
    }
    if auth == "none":
        profile["ALLOW_NO_AUTH"] = "true"
    return {k: os.environ.get(k, v) for k, v in profile.items()}
