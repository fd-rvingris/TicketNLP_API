"""The known-names directory as versioned snapshots hydrated from the Keystone CMDB.

The detector reads known names only through the ServerDirectory protocol, so the API passes it a
directory loaded from a snapshot instead of the one frozen in the model file.

A snapshot is one JSON file, directory-<UTC timestamp>.json, holding per client key (the Zendesk
organization id, or name:<client name> for a CMDB client without one) the hosts and (host, instance)
pairs with their sources and environments, plus its creation time and record counts. Sources:

- cmdb: the Keystone CMDB, through a CmdbSource (CsvCmdbSource reads the data/instances.csv format;
  KeystoneCmdbSource is a stub until the Keystone API is known);
- alert: the names the model's directory learned from alert fields, so the detector loses nothing the
  CMDB does not hold;
- dw: the model directory's DW2.0 time-logging names, kept as known but not confirmed, as the model's
  directory uses them (DIRECTORY_INCLUDE_DW, on by default: leaving them out lowered host recall on the
  hand labels; see docs/api.md).

Names are normalized as the directory stores them (parse_server_name(), the default instance as the
host alone). python -m api.refresh_directory builds, validates and publishes a snapshot; the API's
DirectoryCache loads the newest valid one and swaps in newer ones without a restart.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol

from pydantic import SecretStr

from ticket_analysis.server_detector import CONFIRMED_SOURCES, LocalServerDirectory

from .storage import Store, StoreError

log = logging.getLogger("api.directory")

FORMAT = "ticket-analysis/server-directory"
FORMAT_VERSION = 1
SOURCES = ("cmdb", "alert", "dw")
_NAME = re.compile(r"^directory-(\d{8}T\d{6}Z)\.json$")


class SnapshotError(ValueError):
    """A snapshot is unreadable, of another format, or failed validation."""


# --- CMDB sources ------------------------------------------------------------------------


@dataclass(frozen=True)
class CmdbRecord:
    """One server or instance record: server_name is HOST, HOST\\INSTANCE or an FQDN."""
    zendesk_org_id: str | None
    client_name: str | None
    server_name: str
    environment: str | None = None


class CmdbSource(Protocol):
    kind: str

    def records(self) -> Iterable[CmdbRecord]: ...


class NoCmdbSource:
    """No CMDB: a snapshot of the model's alert names alone."""
    kind = "none"

    def records(self) -> Iterable[CmdbRecord]:
        return []


class CsvCmdbSource:
    """The CMDB export in data/instances.csv format: server_name, environment, zendesk_organization_id and
    client_name columns. For local runs and tests. A missing file is an empty CMDB when allow_missing."""
    kind = "csv"

    def __init__(self, path: Path, allow_missing: bool = False):
        self.path, self.allow_missing = Path(path), allow_missing

    def records(self) -> Iterable[CmdbRecord]:
        if not self.path.exists():
            if self.allow_missing:
                log.warning("CMDB export %s not found; the snapshot holds alert names only", self.path.name)
                return
            raise SnapshotError(f"CMDB export not found: {self.path}")
        with self.path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                yield CmdbRecord(row.get("zendesk_organization_id"), row.get("client_name"),
                                 row.get("server_name") or "", row.get("environment"))


class KeystoneCmdbSource:
    """The Keystone CMDB API. A stub until the API is known (see docs/api.md, "Keystone CMDB"): it keeps
    the settings it will need and raises NotImplementedError. Credentials come from Key Vault through
    a Container Apps secret reference (CMDB_API_KEY), never from code or the repo.

    Expected shape, to confirm with the Keystone team: a paged list of server records, each with the
    client's Zendesk organization id, the client name, the server or HOST\\INSTANCE name and the
    environment; records() yields one CmdbRecord each."""
    kind = "keystone"

    def __init__(self, base_url: str, api_key: SecretStr | None = None, page_size: int = 500,
                 timeout_seconds: float = 30):
        self.base_url, self.api_key, self.page_size, self.timeout_seconds = base_url, api_key, page_size, timeout_seconds

    def records(self) -> Iterable[CmdbRecord]:
        raise NotImplementedError("The Keystone CMDB API is not known yet; use CMDB_SOURCE=csv or none "
                                  "(see docs/api.md, Keystone CMDB)")


def cmdb_source(settings, *, local: bool = False) -> CmdbSource:
    if settings.cmdb_source == "none":
        return NoCmdbSource()
    if settings.cmdb_source == "csv":
        if not settings.cmdb_csv_path:
            raise SnapshotError("CMDB_SOURCE=csv needs CMDB_CSV_PATH")
        return CsvCmdbSource(settings.cmdb_csv_path, allow_missing=local)
    return KeystoneCmdbSource(settings.cmdb_base_url, settings.cmdb_api_key, settings.cmdb_page_size,
                              settings.cmdb_timeout_seconds)


# --- Building and reading snapshots --------------------------------------------------------


def utc_stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot_name(created_at: str) -> str:
    """directory-<UTC timestamp>.json for a createdAt of the form 2026-10-19T06:00:00Z."""
    return "directory-" + created_at.replace("-", "").replace(":", "") + ".json"


def is_snapshot_name(name: str) -> bool:
    return bool(_NAME.match(name.rsplit("/", 1)[-1]))


def build_snapshot(cmdb: CmdbSource, model_directory: LocalServerDirectory | None = None, *,
                   include_dw: bool = False, model_version: str | None = None,
                   created_at: datetime | None = None) -> dict:
    """A snapshot from the CMDB and the model directory's alert names (and its dw_servers names when
    include_dw). The names, confirmed flags and environment counts are kept exactly as the directory
    stores them, so snapshot_directory() of a snapshot holding the model directory's names behaves as
    that directory does."""
    created = utc_stamp(created_at or datetime.now(timezone.utc))
    sources: dict[tuple, set[str]] = defaultdict(set)
    acc = LocalServerDirectory()
    model_counts = Counter()
    if model_directory is not None:
        state = model_directory.to_dict()
        kept = {}
        for key, entry in state["clients"].items():
            hosts = [h for h in entry["hosts"] if h["confirmed"] or include_dw]
            pairs = [p for p in entry["instances"] if p["confirmed"] or include_dw]
            for h in hosts:
                sources[(key, h["host"], None)].add("alert" if h["confirmed"] else "dw")
            for p in pairs:
                sources[(key, p["host"], p["instance"])].add("alert" if p["confirmed"] else "dw")
                sources[(key, p["host"], None)].add("alert" if p["confirmed"] else "dw")
            model_counts.update("alert" if r["confirmed"] else "dw" for r in hosts + pairs)
            if hosts or pairs:
                kept[key] = {"hosts": hosts, "instances": pairs}
        acc = LocalServerDirectory.from_dict({"clients": kept, "aliases": state["aliases"]})
    cmdb_records = 0
    for r in cmdb.records():
        cmdb_records += 1
        added = acc.add_cmdb_record(r.zendesk_org_id, r.client_name, r.server_name, r.environment)
        if added:
            key, parsed = added
            sources[(key, parsed.host, None)].add("cmdb")
            if parsed.instance:
                sources[(key, parsed.host, parsed.instance)].add("cmdb")
    state = acc.to_dict()
    clients = {}
    for key, entry in state["clients"].items():
        clients[key] = {
            "hosts": [{"host": h["host"], "sources": sorted(sources[(key, h["host"], None)]),
                       "environments": h["environments"]} for h in entry["hosts"]],
            "instances": [{"host": p["host"], "instance": p["instance"],
                           "sources": sorted(sources[(key, p["host"], p["instance"])])} for p in entry["instances"]],
        }
    snapshot = {"format": FORMAT, "formatVersion": FORMAT_VERSION, "createdAt": created,
                "sources": {"cmdb": {"kind": cmdb.kind, "records": cmdb_records, "skipped": acc.skipped},
                            "alert": {"model": model_version, "records": model_counts["alert"]},
                            "dw": {"included": include_dw, "records": model_counts["dw"]}},
                "clients": clients, "aliases": state["aliases"]}
    snapshot["counts"] = counts(snapshot)
    return snapshot


def counts(snapshot: dict) -> dict:
    clients = snapshot.get("clients") or {}
    by_source = Counter()
    hosts = pairs = 0
    for entry in clients.values():
        hosts += len(entry.get("hosts") or ())
        pairs += len(entry.get("instances") or ())
        for r in (entry.get("hosts") or []) + (entry.get("instances") or []):
            by_source.update(r.get("sources") or ())
    return {"clients": len(clients), "hosts": hosts, "instances": pairs, "records": hosts + pairs,
            "bySource": {s: by_source.get(s, 0) for s in SOURCES}}


def snapshot_directory(snapshot: dict) -> LocalServerDirectory:
    """The directory a snapshot describes. A name is confirmed when the CMDB or an alert named it."""
    clients = {}
    for key, entry in (snapshot.get("clients") or {}).items():
        clients[key] = {
            "hosts": [{"host": h["host"], "confirmed": bool(set(h.get("sources") or ()) & CONFIRMED_SOURCES),
                       "environments": h.get("environments") or {}} for h in entry.get("hosts") or ()],
            "instances": [{"host": p["host"], "instance": p["instance"],
                           "confirmed": bool(set(p.get("sources") or ()) & CONFIRMED_SOURCES)}
                          for p in entry.get("instances") or ()],
        }
    return LocalServerDirectory.from_dict({"clients": clients, "aliases": snapshot.get("aliases") or {}})


def parse_snapshot(data: bytes) -> dict:
    try:
        snapshot = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SnapshotError("not valid JSON") from None
    if not isinstance(snapshot, dict) or snapshot.get("format") != FORMAT:
        raise SnapshotError("not a directory snapshot")
    if snapshot.get("formatVersion") != FORMAT_VERSION:
        raise SnapshotError(f"format version {snapshot.get('formatVersion')} is not {FORMAT_VERSION}")
    clients, aliases = snapshot.get("clients"), snapshot.get("aliases") or {}
    if not isinstance(clients, dict) or not isinstance(aliases, dict):
        raise SnapshotError("clients and aliases must be objects")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in aliases.items()):
        raise SnapshotError("an alias is not a string")
    for key, entry in clients.items():
        hosts = entry.get("hosts", []) if isinstance(entry, dict) else None
        pairs = entry.get("instances", []) if isinstance(entry, dict) else None
        if not isinstance(hosts, list) or not isinstance(pairs, list):
            raise SnapshotError("a client entry needs hosts and instances lists")
        for r in hosts + pairs:
            sources = r.get("sources") if isinstance(r, dict) else None
            known = isinstance(sources, list) and sources and all(isinstance(x, str) and x in SOURCES for x in sources)
            if not isinstance(r, dict) or not _name(r.get("host")) or not known:
                raise SnapshotError("a record has no host or no known sources")
        if not all(_name(r.get("instance")) for r in pairs):
            raise SnapshotError("an instance record has no instance")
        for r in hosts:
            env = r.get("environments", {})
            if not isinstance(env, dict) or not all(isinstance(k, str) and type(v) is int and v >= 0
                                                    for k, v in env.items()):
                raise SnapshotError("a host record's environments are not name-to-count pairs")
    if not isinstance(snapshot.get("createdAt"), str) or _parse_time(snapshot["createdAt"]) is None:
        raise SnapshotError("no createdAt time")
    return snapshot


def _name(value) -> bool:
    return isinstance(value, str) and bool(value)


def validate(snapshot: dict, previous: dict | None = None, min_share: float = 0.9) -> list[str]:
    """Reasons not to publish: an empty snapshot, or one with fewer than min_share of the previous
    snapshot's records (a CMDB outage or a broken export should not empty the directory)."""
    problems = []
    now = counts(snapshot)
    if not now["clients"] or not now["records"]:
        problems.append("the snapshot is empty")
    if previous is not None:
        before = counts(previous)["records"]
        if before and now["records"] < min_share * before:
            problems.append(f"the snapshot has {now['records']:,} records, fewer than {min_share:.0%} of the "
                            f"previous snapshot's {before:,}")
    return problems


def encode(snapshot: dict) -> bytes:
    return json.dumps(snapshot, ensure_ascii=False, indent=1, sort_keys=False).encode("utf-8")


class SnapshotStore:
    """Snapshots in a Store, newest first by name (the name carries the UTC creation time)."""

    def __init__(self, store: Store):
        self.store = store

    @property
    def location(self) -> str:
        return self.store.location

    def names(self) -> list[str]:
        return sorted((n for n in self.store.list() if is_snapshot_name(n)), reverse=True)

    def read(self, name: str) -> dict:
        return parse_snapshot(self.store.read(name))

    def newest_valid(self, skip: Iterable[str] = ()) -> tuple[str, dict] | None:
        """The newest snapshot that parses and is not empty; unusable ones are logged and skipped. A
        store failure is raised, since an older snapshot would fail the same way."""
        skip = set(skip)
        for name in self.names():
            if name in skip:
                continue
            try:
                snapshot = self.read(name)
                if validate(snapshot):
                    raise SnapshotError("it is empty")
            except StoreError:
                raise
            except SnapshotError as e:
                log.warning("Skipping snapshot %s: %s", name, e)
                continue
            except Exception as e:  # anything else about one file must not stop the others
                log.warning("Skipping snapshot %s (%s)", name, type(e).__name__)
                continue
            return name, snapshot
        return None

    def publish(self, snapshot: dict, *, keep: int, min_share: float) -> str:
        """Write the snapshot if it passes validate() against the newest valid one, then keep only the
        newest `keep` snapshots. Raises SnapshotError when it does not pass."""
        latest = self.newest_valid()
        problems = validate(snapshot, latest[1] if latest else None, min_share)
        if problems:
            raise SnapshotError("not published: " + "; ".join(problems))
        name = snapshot_name(snapshot["createdAt"])
        if latest and name <= latest[0]:
            raise SnapshotError(f"not published: {latest[0]} is not older than this snapshot")
        self.store.write(name, encode(snapshot))
        for old in self.names()[keep:]:
            self.store.delete(old)
        return name


# --- The API's cache -----------------------------------------------------------------------


@dataclass(frozen=True)
class DirectoryState:
    directory: LocalServerDirectory
    version: str  # the snapshot's createdAt, or model:<model created_at> for the model's own directory
    source: str  # "snapshot" or "model"
    name: str | None  # the snapshot file
    created_at: datetime | None


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class DirectoryCache:
    """The directory the API serves: the newest valid snapshot, checked for a newer one on each
    refresh() and swapped in atomically (one reference assignment; a request keeps the state it
    started with). When the store fails, the last state stays and the error is kept for /ready logs.
    Without a store, or until a first snapshot loads, it serves the model's own directory.

    on_load(directory) runs before a swap, so the detector's per-directory features are computed off
    the request path."""

    def __init__(self, store: SnapshotStore | None, fallback: DirectoryState,
                 on_load: Callable[[LocalServerDirectory], None] | None = None):
        self.store, self.fallback, self.on_load = store, fallback, on_load
        self.current = fallback
        self.previous: DirectoryState | None = None
        self.last_error: str | None = None
        self.last_checked: datetime | None = None
        self._lock = threading.Lock()  # one refresh at a time

    def age_seconds(self) -> float | None:
        if self.current.created_at is None:
            return None
        return (datetime.now(timezone.utc) - self.current.created_at).total_seconds()

    def refresh(self) -> bool:
        """Load the newest valid snapshot newer than the current one, or, when the current snapshot has
        been removed from the store (a rollback), the newest valid one. True when swapped."""
        if self.store is None:
            return False
        with self._lock:
            self.last_checked = datetime.now(timezone.utc)
            try:
                names = self.store.names()
            except Exception as e:  # StoreError, or an OSError from a mount; keep serving the current one
                return self._unreachable(e)
            current = self.current.name
            if current and current not in names:
                log.warning("Snapshot %s is no longer in the store; loading the newest valid one", current)
                candidates = names
            else:
                candidates = [n for n in names if not current or n > current]
            self.last_error = None
            for name in candidates:  # newest first
                try:
                    state = self._load(name)
                except StoreError as e:
                    return self._unreachable(e)
                if state is not None:
                    self.previous, self.current = self.current, state
                    return True
            if self.current.source == "model":
                log.warning("No valid directory snapshot in %s; serving the model's own directory",
                            self.store.location)
            return False

    def _unreachable(self, e: Exception) -> bool:
        self.last_error = str(e) if isinstance(e, StoreError) else type(e).__name__
        age = self.age_seconds()
        log.warning("Directory store unreachable; serving %s (%s old): %s", self.current.version,
                    f"{age / 3600:.1f} h" if age is not None else "age unknown", self.last_error)
        return False

    def _load(self, name: str) -> DirectoryState | None:
        """The state for one snapshot, or None when it cannot be used (logged). Store errors are raised."""
        try:
            snapshot = self.store.read(name)
            if validate(snapshot):
                raise SnapshotError("it is empty")
            directory = snapshot_directory(snapshot)
            if self.on_load:
                self.on_load(directory)
        except StoreError:
            raise
        except SnapshotError as e:
            log.warning("Skipping snapshot %s: %s", name, e)
            return None
        except Exception as e:  # a snapshot that parses but cannot be used
            log.warning("Skipping snapshot %s (%s)", name, type(e).__name__)
            return None
        c = snapshot.get("counts") or counts(snapshot)
        log.info("Directory snapshot %s loaded: %s clients, %s records", name, c.get("clients"), c.get("records"))
        return DirectoryState(directory, snapshot["createdAt"], "snapshot", name, _parse_time(snapshot["createdAt"]))
