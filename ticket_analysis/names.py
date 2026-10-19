"""Server name normalization and the CMDB instance export."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .paths import INSTANCES_PATH

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


@dataclass(frozen=True)
class ParsedName:
    host: str
    instance: str | None
    port: str | None


def parse_server_name(name: str) -> ParsedName | None:
    """Normalize a server reference: lower case, drop a tcp: prefix, split
    HOST,PORT and HOST\\INSTANCE, and strip the domain from an FQDN."""
    s = name.strip().strip("[]\"'`").strip().lower()
    if s.startswith("tcp:"):
        s = s[4:]
    port = None
    if "," in s:
        s, _, p = s.partition(",")
        port = p.strip() if p.strip().isdigit() else None
    host, _, instance = s.partition("\\")
    host = host.strip()
    if not _IPV4.match(host):
        host = host.split(".", 1)[0]
    if not host:
        return None
    return ParsedName(host, instance.strip() or None, port)


@dataclass(frozen=True)
class Instance:
    server_name: str
    parsed: ParsedName
    environment: str


@dataclass
class Cmdb:
    by_org: dict[str, list[Instance]]
    by_client: dict[str, list[Instance]]

    def for_ticket(self, ticket: dict) -> list[Instance]:
        """Instances of the ticket's own client only, by Zendesk org id, then by client name."""
        org = ticket.get("organization_id")
        if org and org in self.by_org:
            return self.by_org[org]
        return self.by_client.get((ticket.get("organization_name") or "").strip().lower(), [])


def load_cmdb(path: Path = INSTANCES_PATH) -> Cmdb:
    cmdb = Cmdb({}, {})
    if not path.exists():
        return cmdb
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("server_name") or "").strip()
            parsed = parse_server_name(name) if name else None
            if not parsed:
                continue
            inst = Instance(name, parsed, (row.get("environment") or "").strip())
            org = (row.get("zendesk_organization_id") or "").strip()
            client = (row.get("client_name") or "").strip().lower()
            if org:
                cmdb.by_org.setdefault(org, []).append(inst)
            if client:
                cmdb.by_client.setdefault(client, []).append(inst)
    return cmdb


def match_mention(text: str, instances: list[Instance]) -> dict:
    """Match one mention against the client's instances.

    Same host is required. A named instance matches that instance, or else the
    host-level record; a bare host matches the host-level record, or else every
    instance on that host. One distinct server_name is "matched", several are
    "ambiguous", none is "unmatched". Ports are ignored.
    """
    parsed = parse_server_name(text)
    names: list[str] = []
    if parsed:
        same_host = [i for i in instances if i.parsed.host == parsed.host]
        host_level = [i for i in same_host if i.parsed.instance is None]
        if parsed.instance:
            pick = [i for i in same_host if i.parsed.instance == parsed.instance] or host_level
        else:
            pick = host_level or same_host
        names = sorted({i.server_name for i in pick})
    status = "unmatched" if not names else "matched" if len(names) == 1 else "ambiguous"
    return {"status": status, "server_names": names}
