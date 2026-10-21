"""Finding, checking and loading the model files. The API never trains a model.

The joblib files are pickles, so they are loaded only from MODEL_DIR (a file name, never a path), and
when MODEL_SHA256 is set every one must match its hash. With MODEL_STORE set they are first downloaded
from the store's models/<MODEL_VERSION>/ folder into MODEL_DIR. The alert-name table ships with the code
(ticket_analysis/alert_types.json), not with the models.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import ticket_analysis.server_detector as sd
import ticket_analysis.ticket_types as tt

from .settings import Settings
from .storage import open_store

log = logging.getLogger("api.models")


class ModelError(RuntimeError):
    """A model file is missing, does not match its hash, or cannot be used; the service is not ready."""


@dataclass(frozen=True)
class ModelFiles:
    server: Path
    types: Path


def version_of(created_at) -> str | None:
    """A bundle's creation time as a version string, 2026-10-17T15:10:15Z."""
    if not created_at:
        return None
    try:
        when = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return str(created_at)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _inside(path: Path, folder: Path) -> Path:
    resolved, root = path.resolve(), folder.resolve()
    if resolved.parent != root:
        raise ModelError(f"{path.name} is not directly in MODEL_DIR")
    return resolved


def _check_hash(settings: Settings, path: Path) -> None:
    if not settings.sha256:
        return
    expected = settings.sha256.get(path.name)
    if not expected:
        raise ModelError(f"MODEL_SHA256 has no hash for {path.name}")
    if sha256_of(path) != expected:
        raise ModelError(f"{path.name} does not match its MODEL_SHA256 hash")


def fetch_file(settings: Settings, name: str) -> Path:
    """One model file in MODEL_DIR, downloaded first from models/<MODEL_VERSION>/ when MODEL_STORE is set.
    It must be a plain file name directly in MODEL_DIR and match its MODEL_SHA256 hash when set."""
    folder = settings.model_dir
    if settings.model_store:
        store = open_store(settings.model_store)
        store.download(f"models/{settings.model_version}/{name}", folder / name)
    path = _inside(folder / name, folder)
    if not path.is_file():
        raise ModelError(f"{path.name} not found in {folder}")
    _check_hash(settings, path)
    return path


def fetch_model_files(settings: Settings) -> ModelFiles:
    """The local paths of the model files."""
    files = ModelFiles(fetch_file(settings, settings.server_model_file), fetch_file(settings, settings.types_model_file))
    if settings.model_store:
        log.info("Model version %s downloaded from %s", settings.model_version, settings.model_store)
    return files


def load_server_bundle(path: Path) -> dict:
    try:
        bundle = sd.load_model(path)
    except ModuleNotFoundError as e:
        if e.name == "server_detector":
            raise ModelError(f"{path.name} was saved before the package split; run python -m tools.convert_model "
                             "on it before uploading") from None
        raise ModelError(f"{path.name} needs a module that is not installed: {e.name}") from None
    if not isinstance(bundle, dict) or not isinstance(bundle.get("model"), sd.ServerModel) \
            or not isinstance(bundle.get("directory"), sd.LocalServerDirectory):
        raise ModelError(f"{path.name} is not a server detector bundle")
    return bundle


def load_types_bundle(path: Path) -> dict:
    bundle = tt.load_model(path)
    if not isinstance(bundle, dict):
        raise ModelError(f"{path.name} is not a type model bundle")
    problem = tt.bundle_problem(bundle)
    if problem:
        raise ModelError(f"{path.name}: {problem}; retrain it with python -m tools.ticket_types train")
    return bundle
