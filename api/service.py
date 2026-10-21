"""The service layer, with no HTTP code: loads the models once and answers detect(), classify(),
analyze() and catalog() with the JSON bodies the HTTP endpoints return.

It reuses the package's functions: ticket_analysis.analysis builds the ticket and runs the detector,
ticket_analysis.ticket_types classifies. The known names come from the DirectoryCache (directory
snapshots), not from the model file, except as a fallback. Model inference is CPU-bound and runs in a
thread pool of WORKERS threads, so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import ticket_analysis.server_detector as sd
import ticket_analysis.ticket_types as tt
from ticket_analysis import analysis
from ticket_analysis.taxonomy import TAXONOMY, TAXONOMY_VERSION
from ticket_analysis.text_clean import clean_text
from ticket_analysis.type_rules import RULES

from . import API_VERSION
from .directory import DirectoryCache, DirectoryState, SnapshotStore
from .models import ModelError, fetch_model_files, load_server_bundle, load_types_bundle, version_of
from .settings import Settings
from .storage import StoreError, open_store

log = logging.getLogger("api.service")

# The detector's kinds as response keys; a HOST\MSSQLSERVER name (no named instance) is the host itself.
KEY_OF_KIND = {"host": "host", "bare_instance": "instance", "host_instance": "host_instance"}
DETECTION_KEYS = {key: sd.KINDS[kind] for kind, key in KEY_OF_KIND.items()}


LOAD_RETRY_FIRST, LOAD_RETRY_MAX = 10, 300  # seconds between model load attempts


class NotReady(RuntimeError):
    """The models are not loaded (yet, or because a file is missing or refused)."""


@dataclass(frozen=True)
class TicketInput:
    text: str
    subject: str = ""
    org_id: str | None = None
    ticket_id: str | None = None


def _detection(d: dict) -> dict:
    kind = "host" if d["kind"] == "host_instance" and not d["instance"] else d["kind"]
    key = KEY_OF_KIND[kind]
    value = d["instance"] if key == "instance" else f"{d['host']}\\{d['instance']}" if key == "host_instance" else d["host"]
    return {"key": key, "value": value, "text": d["text"], "host": d["host"], "instance": d["instance"],
            "status": d["status"], "field": d["field"], "start": d["start"], "end": d["end"],
            "typoOf": d["typo_of"], "siblingOf": d["sibling_of"], "hostCandidates": list(d["candidates"]),
            "environment": d["environment"], "score": d["score"]}


def _debug_candidate(c: dict) -> dict:
    return {"text": c["text"], "field": c["field"], "start": c["start"], "end": c["end"], "kind": c["kind"],
            "hostCandidate": c["host_candidate"], "instanceCandidate": c["instance_candidate"],
            "pHost": c["p_host"], "pInstance": c["p_instance"], "rule": c["rule"]}


def detection_body(result: dict, org_id: str | None) -> dict:
    """The detection part of a response from analysis.detect()'s result."""
    detections = [_detection(d) for d in result["detections"]]
    entities = {key: [] for key in DETECTION_KEYS}
    for d in detections:
        if d["value"] not in entities[d["key"]]:
            entities[d["key"]].append(d["value"])
    return {"client": {"zendeskOrgId": org_id, "known": bool(org_id) and result["client"]["in_directory"]},
            "isAlert": result["is_alert"], "entities": entities, "detections": detections}


def _without_table_note(note: str, table_note: str) -> str:
    """A classify() note without the alert table's own note, which was written from real tickets and can
    name a client or a server; callers get the remark, not the table's text."""
    if table_note and table_note in note:
        return note.replace(table_note, "").rstrip(" :;,.") + "."
    return note


def classification_body(r: dict) -> dict:
    """The classification part of a response from ticket_types.classify()'s result."""
    alert, table_note = r["alert"], ""
    if alert:
        entry = alert["entry"]
        table_note = (entry or {}).get("note") or ""
        alert = {"family": alert["family"], "layout": alert["layout"], "alertName": alert["alert_name"],
                 "tableEntry": None if not entry else {k: entry[k] for k in ("type", "decides", "tickets")}}
    return {"primaryType": r["primary_type"], "typeSource": r["type_source"], "typeCandidates": r["type_candidates"],
            "intent": r["intent"], "intentSource": r["intent_source"], "intentCandidates": r["intent_candidates"],
            "uncertain": r["uncertain"], "uncertainReason": r["uncertain_reason"], "alert": alert,
            "notes": [_without_table_note(n, table_note) for n in r["notes"]]}


def _first_line(doc: str | None) -> str:
    return " ".join((doc or "").strip().split("\n\n")[0].split())


def type_sources() -> dict[str, str]:
    out = dict(tt.TYPE_SOURCES)
    out.update({f"rule:{name}": "Keyword rule: " + _first_line(rule.__doc__) for name, rule in RULES})
    return out


def _json_type(prop: dict) -> str:
    options = prop.get("anyOf") or [prop]
    names = []
    for o in options:
        t = o.get("type", "object")
        names.append(f"array of {o.get('items', {}).get('type', 'object')}" if t == "array" else t)
    nullable = "null" in names
    names = [n for n in names if n != "null"]
    return " or ".join(names) + (" or null" if nullable else "")


def catalog_body(alert_table_version: str) -> dict:
    """Every key and enumerated value the responses can contain, from the code and taxonomy.json."""
    from .schemas import Detection
    props = Detection.model_json_schema()["properties"]
    enums = {"key": DETECTION_KEYS, "status": sd.STATUSES, "field": sd.FIELDS}
    fields = [{"name": name, "type": _json_type(p), "description": p.get("description", ""),
               "values": [{"value": v, "description": d} for v, d in enums[name].items()] if name in enums else None}
              for name, p in props.items()]
    return {"taxonomyVersion": TAXONOMY_VERSION, "alertTableVersion": alert_table_version,
            "detectionKeys": [{"key": k, "description": d} for k, d in DETECTION_KEYS.items()],
            "detectionFields": fields,
            "types": [{k: t[k] for k in ("id", "label", "definition")} for t in TAXONOMY["types"]],
            "intents": [{k: i[k] for k in ("id", "label", "definition")} for i in TAXONOMY["intents"]],
            "typeSources": [{"value": v, "description": d} for v, d in type_sources().items()],
            "intentSources": [{"value": v, "description": d} for v, d in tt.INTENT_SOURCES.items()]}


class Service:
    def __init__(self, settings: Settings, *, snapshot_store: SnapshotStore | None = None):
        self.settings = settings
        self.executor = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="inference")
        self.detector: dict | None = None
        self.types: dict | None = None
        self.alert_table = tt.load_alert_table()  # ships with the code; a broken file fails here
        self.directories: DirectoryCache | None = None
        self.problem: str | None = None  # why the service is not ready, for logs
        self.versions = {"serverDetector": None, "ticketTypes": None}
        self._snapshot_store = snapshot_store
        self._catalog = catalog_body(self.alert_table.version)
        self._loading: threading.Thread | None = None
        self._closed = threading.Event()

    # --- Loading ----------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self.detector is not None and self.types is not None and self.directories is not None

    def load(self) -> bool:
        """Load the models and the directory. Never trains: a missing or refused file leaves the
        service not ready, with the reason in self.problem and the log."""
        try:
            files = fetch_model_files(self.settings)
            detector = load_server_bundle(files.server)
            types = load_types_bundle(files.types)
        except (ModelError, StoreError, OSError) as e:
            self.problem = str(e)
            log.error("Not ready: %s", self.problem)
            return False
        except Exception as e:  # an unreadable pickle and the like; the message may name internals only
            self.problem = f"model files could not be loaded ({type(e).__name__})"
            log.error("Not ready: %s", self.problem)
            return False
        version = version_of(detector.get("created_at"))
        model = detector["model"]
        fallback = DirectoryState(detector["directory"], f"model:{version}", "model", None, None)
        store = self._snapshot_store
        if store is None and self.settings.directory_store:
            try:
                store = SnapshotStore(open_store(self.settings.directory_store))
            except StoreError as e:
                log.warning("Directory store not usable (%s); serving the model's own directory", e)
        cache = DirectoryCache(store, fallback, on_load=model.knowledge)
        model.knowledge(fallback.directory)
        try:
            cache.refresh()
        except Exception as e:  # refresh() keeps the fallback on store and snapshot errors; this is a last guard
            log.warning("Directory snapshot not loaded (%s); serving the model's own directory", type(e).__name__)
        self.versions = {"serverDetector": version, "ticketTypes": version_of(types.get("created_at"))}
        self.types, self.detector, self.directories = types, detector, cache
        self.problem = None
        log.info("Ready: server detector %s, ticket types %s (taxonomy %s, alert table %s with %d rows), directory %s",
                 self.versions["serverDetector"], self.versions["ticketTypes"], TAXONOMY_VERSION,
                 self.alert_table.version, len(self.alert_table),
                 cache.current.version)
        return True

    def start_loading(self) -> None:
        """Load in a background thread, so /health answers while the models load. A failed load is
        retried with backoff (10 s doubling to 5 minutes): Container Apps does not restart a replica
        whose readiness fails, and a cause such as a role assignment still propagating clears by itself."""
        self._loading = threading.Thread(target=self._load_until_ready, name="load-models", daemon=True)
        self._loading.start()

    def _load_until_ready(self) -> None:
        delay = LOAD_RETRY_FIRST
        while not self._closed.is_set() and not self.load():
            log.info("Retrying the model load in %d s", delay)
            if self._closed.wait(delay):
                return
            delay = min(delay * 2, LOAD_RETRY_MAX)

    def wait_loaded(self, timeout: float | None = None) -> bool:
        if self._loading:
            self._loading.join(timeout)
        return self.ready

    def refresh_directory(self) -> bool:
        """Swap in a newer directory snapshot if there is one; keep the current one on any failure."""
        if not self.ready:
            return False
        swapped = self.directories.refresh()
        if swapped:
            keep = [s.directory for s in (self.directories.current, self.directories.previous) if s]
            self.detector["model"].forget_knowledge(keep)
        return swapped

    def readiness(self) -> dict:
        """Detail for the /ready log line (never returned to callers)."""
        d = self.directories
        return {"ready": self.ready, "problem": self.problem,
                "directory": d.current.version if d else None,
                "directoryAgeHours": round(d.age_seconds() / 3600, 1) if d and d.age_seconds() is not None else None,
                "directoryStoreError": d.last_error if d else None}

    def close(self) -> None:
        self._closed.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    async def run(self, fn, *args):
        """Run fn in the inference thread pool."""
        return await asyncio.get_running_loop().run_in_executor(self.executor, functools.partial(fn, *args))

    # --- Responses ----------------------------------------------------------------------

    def _require_ready(self) -> None:
        if not self.ready:
            raise NotReady(self.problem or "the models are still loading")

    def model_versions(self, state: DirectoryState | None = None) -> dict:
        state = state or (self.directories.current if self.directories else None)
        return {**self.versions, "directory": state.version if state else None, "taxonomyVersion": TAXONOMY_VERSION,
                "alertTableVersion": self.alert_table.version, "modelVersion": self.settings.model_version or None}

    def envelope(self, request_id: str, ticket_id: str | None, state: DirectoryState | None = None) -> dict:
        return {"requestId": request_id, "apiVersion": API_VERSION, "ticketId": ticket_id,
                "models": self.model_versions(state)}

    def _detect(self, inp: TicketInput, state: DirectoryState) -> dict:
        model = self.detector["model"]
        ticket = analysis.ticket_for_org(inp.subject or "", inp.text, inp.org_id)
        result = analysis.detect(ticket, model, state.directory)
        body = detection_body(result, inp.org_id)
        if self.settings.debug_responses:
            body["debug"] = {"thresholds": result["thresholds"],
                             "candidates": [_debug_candidate(c) for c in
                                            analysis.scored_candidates(ticket, state.directory, model)]}
        return body

    def _classify(self, inp: TicketInput) -> dict:
        subject = inp.subject or ""
        body = classification_body(tt.classify(subject, inp.text, self.types, self.alert_table))
        if self.settings.debug_responses:
            cleaned = clean_text(subject, inp.text)
            body["debug"] = {"cleanedSubject": cleaned[0], "cleanedText": cleaned[1],
                             "rules": [{"rule": name, "type": rule(*cleaned)} for name, rule in RULES]}
        return body

    def detect(self, inp: TicketInput, request_id: str) -> dict:
        self._require_ready()
        state = self.directories.current  # one directory for the whole request, whatever a refresh swaps in
        return {**self.envelope(request_id, inp.ticket_id, state), **self._detect(inp, state)}

    def classify(self, inp: TicketInput, request_id: str) -> dict:
        self._require_ready()
        state = self.directories.current
        return {**self.envelope(request_id, inp.ticket_id, state), **self._classify(inp)}

    def analyze(self, inp: TicketInput, request_id: str) -> dict:
        self._require_ready()
        state = self.directories.current
        return {**self.envelope(request_id, inp.ticket_id, state), "detection": self._detect(inp, state),
                "taxonomy": self._classify(inp)}

    def catalog(self, request_id: str) -> dict:
        return {**self.envelope(request_id, None), **self._catalog}

