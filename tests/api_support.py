"""Made-up models, files and settings shared by the API tests. Small models are trained here from made-up
tickets, as tests/test_ticket_types.py does; nothing is read from data/."""

from __future__ import annotations

import random
from functools import lru_cache
from pathlib import Path

import joblib

import ticket_analysis.server_detector as sd
import tools.server_detector as sdt
import tools.ticket_types as ttt
from api.settings import load_settings
from tests.test_ticket_types import labeled
from ticket_analysis.names import ParsedName
from ticket_analysis.tickets import make_ticket
from tools.directory_build import build_directory

ORG, OTHER = "999001", "999002"
DETECTOR_CREATED = "2026-10-17T15:10:15+00:00"
API_KEY, NEW_KEY = "fake-key-0123456789abcdef", "fake-key-fedcba9876543210"


def training_tickets(n: int = 160, seed: int = 3) -> list[dict]:
    """Human tickets for two made-up clients; each lists its servers in dw_servers, as the exports do."""
    rng = random.Random(seed)
    clients = [(ORG, "Fake Co", "fakesql", ["REPORTS", "FINANCE"]), (OTHER, "Other Fake Co", "othsql", ["SALES", "STAGE"])]
    fillers = ["Please look at", "We see slowness on", "Can you check", "Backups failed for", "Patch"]
    tickets = []
    for i in range(n):
        org, name, prefix, insts = clients[i % 2]
        host = f"{prefix}{(i // 2) % 12 + 1:02d}"
        inst = rng.choice(insts)
        if i % 4 == 3:  # a bare instance next to the cue word, with ordinary words around it
            text, dw = f"The {inst} instance on {host} is slow since this morning, it is fine now.", [f"{host}\\{inst}"]
        elif i % 3:
            text, dw = f"{rng.choice(fillers)} {host}\\{inst} today, thanks.", [f"{host}\\{inst}"]
        else:
            text, dw = f"{rng.choice(fillers)} {host} today, it is slow.", [host]
        tickets.append(make_ticket(i, "Help", text, organization_id=org, organization_name=name, is_alert=0,
                                   dw_servers=dw))
    return tickets


def training_directory() -> sd.LocalServerDirectory:
    """The training tickets' names (dw_servers) plus two alert-confirmed hosts with an environment."""
    d = build_directory(training_tickets(), None)
    d.add(ORG, "name:fake co", ParsedName("fakesql01", None, None), "alert", "Production")
    d.add(ORG, "name:fake co", ParsedName("fakesql02", "reports", None), "alert", "Test")
    return d


@lru_cache(maxsize=1)
def trained() -> tuple[sd.ServerModel, sd.LocalServerDirectory, dict]:
    directory = training_directory()
    model, _ = sdt.fit_config(training_tickets(), directory, kind="logreg", alert_context="strip_labels",
                              instance_model=False, seed=0, max_chars=5000, neg_ratio=10)
    model.host_threshold, model.instance_threshold = 0.5, 0.3
    types = ttt.train(*labeled())
    return model, directory, types


def write_models(folder: Path, *, server: bool = True, types: bool = True) -> Path:
    model, directory, types_bundle = trained()
    folder.mkdir(parents=True, exist_ok=True)
    if server:
        joblib.dump({"model": model, "directory": directory, "created_at": DETECTOR_CREATED,
                     "trained_on": "made_up.jsonl"}, folder / "server_detector.joblib")
    if types:
        joblib.dump(types_bundle, folder / "ticket_types.joblib")
    return folder


def make_settings(tmp_path: Path, **values):
    """Local settings over made-up model files in tmp_path/models; values override them."""
    base = {"model_dir": tmp_path / "models", "directory_store": str(tmp_path / "directory"), "auth_mode": "none",
            "allow_no_auth": True, "api_host": "127.0.0.1", "log_format": "text", "workers": 2,
            "cmdb_source": "none", "mcp_enabled": True}
    base.update(values)
    return load_settings(**base)


def local_client(app):
    """A TestClient that connects as 127.0.0.1, as a local caller does (AUTH_MODE=none answers loopback only)."""
    from fastapi.testclient import TestClient
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


def make_client(tmp_path: Path, *, write: bool = True, **values):
    """A test client over an app whose models are loaded before the first request."""
    from api.app import create_app
    if write:
        write_models(tmp_path / "models")
    settings = make_settings(tmp_path, **values)
    return local_client(create_app(settings, background=False, configure_logging=False))


TICKET = {"text": "The REPORTS instance on FAKESQL01 is slow since this morning. FAKESQL02\\FINANCE is fine.",
          "subject": "Slow reports", "zendeskOrgId": ORG}
