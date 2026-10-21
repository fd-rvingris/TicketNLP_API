"""Training-side construction of a known-names directory: from the local ticket files and the CMDB
export, and copies with names held out for the unseen-name tests. The API builds its directory from
snapshots instead (api/directory.py), so ticket_analysis.server_detector keeps only what both need.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ticket_analysis.paths import INSTANCES_PATH
from ticket_analysis.server_detector import (LocalServerDirectory, alert_environment, ticket_client_keys,
                                             ticket_names)


def add_ticket(directory: LocalServerDirectory, ticket: dict) -> None:
    """The ticket's dw_servers names (unconfirmed) and its alert-field names (confirmed, with the
    environment the alert gives)."""
    org, name_key = ticket_client_keys(ticket)
    for parsed in ticket_names({"dw_servers": ticket.get("dw_servers")}):
        directory.add(org, name_key, parsed, "dw")
    env = alert_environment(ticket)
    for parsed in ticket_names({"alert_fields": ticket.get("alert_fields")}):
        directory.add(org, name_key, parsed, "alert", env)


def add_instances_csv(directory: LocalServerDirectory, path: Path) -> None:
    """Every row of a CMDB export in data/instances.csv format (see api.directory.CsvCmdbSource)."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            directory.add_cmdb_record(row.get("zendesk_organization_id"), row.get("client_name"),
                                      row.get("server_name"), row.get("environment"))


def build_directory(tickets: list[dict], instances_path: Path | None = INSTANCES_PATH) -> LocalServerDirectory:
    """The directory of the tickets' names plus the CMDB export, when the file exists."""
    d = LocalServerDirectory()
    for t in tickets:
        add_ticket(d, t)
    if instances_path and Path(instances_path).exists():
        add_instances_csv(d, Path(instances_path))
    return d


def without_names(directory: LocalServerDirectory, hosts=frozenset(),
                  instances=frozenset()) -> LocalServerDirectory:
    """A copy with the given hosts (and their pairs) and instance names removed."""
    data = directory.to_dict()
    for entry in data["clients"].values():
        entry["hosts"] = [h for h in entry["hosts"] if h["host"] not in hosts]
        entry["instances"] = [p for p in entry["instances"]
                              if p["host"] not in hosts and p["instance"] not in instances]
    return LocalServerDirectory.from_dict(data)
