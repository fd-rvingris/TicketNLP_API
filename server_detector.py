"""Find SQL Server hosts and named instances in free ticket text without an LLM.

Splits the subject and description into tokens, keeps generous host and
instance candidates, scores each one as host, instance or none with a model
trained on labels derived from alert fields, DW2.0 servers and a known-names
directory, then pairs bare instance names with a host and assigns a status
(known, new instance on a known host, possible new host or instance, ambiguous,
typo of a known name).

Commands: train (fit and save to data/models/server_detector.joblib), evaluate
(all tests, Markdown report under data/reports/) and detect (write detections
for a ticket file to a new .jsonl). Prints counts and rates only, never ticket
text. Nothing leaves this machine; the CMDB API is a stub.
"""

from __future__ import annotations

import argparse
import bisect
import math
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Protocol

import joblib
import numpy as np
from scipy.sparse import csr_matrix, hstack
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction import DictVectorizer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_curve
from sklearn.preprocessing import MaxAbsScaler

from alert_parser import COLLECTOR_LABELS, FIRING_LABELS
from common import (DATA_DIR, INSTANCES_PATH, REPORTS_DIR, ParsedName, fmt_pct, md_table, now_iso,
                    parse_server_name, read_jsonl, write_jsonl)

MODEL_PATH = DATA_DIR / "models" / "server_detector.joblib"
CLASSES = ("host", "instance", "none")
DEFAULT_MAX_CHARS = 20_000  # description characters read per ticket
DEFAULT_THRESHOLD = 0.5  # used only when no validation split is available
DEFAULT_NEG_RATIO = 10  # at most this many "none" training examples per positive
# Share of training tickets whose features are computed as if their client were unknown. Nearly every
# training ticket has a client with known names, so without this the model leans on "resembles the
# client's names" and scores every candidate as none when the client is unknown or a name is new.
DEFAULT_CLIENT_DROPOUT = 0.3
ALERT_CONTEXT_MODES = ("strip_labels", "token_only")
MODEL_KINDS = ("logreg", "hgb")
TOP_PATTERNS = 60  # compressed patterns kept as one-hot features; the rest are "other"
DEFAULT_INSTANCE = "mssqlserver"  # stored as a host-level record, as match_mention() assumes

# --- Text patterns -----------------------------------------------------------

# Whitespace and punctuation split tokens; \ . - _ , : and $ stay inside one, so HOST\INST,
# db01.corp.local, host,1433, tcp:host and MSSQL$INST survive as single tokens.
TOKEN_RE = re.compile(r"[A-Za-z0-9_\\.\-,:$#]+")
SENTENCE_RE = re.compile(r"[.!?](?=\s)|\n")
CONN_RE = re.compile(r"(?i)\b(?:server|data\s+source|address|addr|network\s+address)\s*=\s*([^;'\"\r\n]+)")
SERVICE_RE = re.compile(r"(?i)^(mssql|sqlagent|sqlserveragent|reportserver|mssqlfdlauncher|sqltelemetry|msolap|"
                        r"mssqlserverolapservice|sqlwriter)\$([A-Za-z0-9_#\-]+)$")
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,62}$")
INSTANCE_RE = re.compile(r"^[a-z_#][a-z0-9_#\-]{0,15}$")
BARE_INSTANCE_RE = re.compile(r"^[A-Za-z_#][A-Za-z0-9_#\-]{1,15}$")
# Dates, ticket and change numbers, KB articles, error codes, sizes, durations, versions and GUIDs.
NOISE_RE = re.compile(
    r"^(kb\d+|0x[0-9a-f]+|(inc|chg|ritm|req|task|prb|sr|cs|ticket|case)\d{3,}|\d+(st|nd|rd|th)|"
    r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d+(\.\d+)?(ms|s|sec|secs|min|mins|h|hr|hrs|gb|mb|kb|tb|k|m|x|pm|am)|"
    r"v?\d+(\.\d+)+|(error|msg|err|event|id)\d+|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{16,})$")
LABEL_PREFIX_RE = re.compile(r"^\s*[A-Za-z][A-Za-z_]*(?: [A-Za-z][A-Za-z_]*)?\s*[:=]\s*$")

INSTANCE_CUES = {"instance", "instances", "service", "services"}
# Rules: unambiguous wording that marks a host or an instance. A rule match scores RULE_SCORE, so it is
# detected at any threshold up to that; the model scores everything else.
RULE_SCORE = 0.95
HOST_CUE_WORDS = {"server", "host", "hostname", "servername", "box", "vm", "machine", "node"}
# Words that sit next to "instance" without being an instance name ("the instance", "on this instance").
FILLER_WORDS = frozenset("""
a an the this that these those on of for in at to from is was are be been and or my our your their its it
sql named called default new old same another other each every any which what one per by with as server
database primary secondary affected problem whole entire
""".split())
SERVICE_PREFIXES = ("mssql$", "sqlagent$")
LABEL_WORDS = {w.lower() for label in (*COLLECTOR_LABELS, *FIRING_LABELS) for w in label.split()}
ALERT_LABEL_LINES = {label.lower() for label in (*COLLECTOR_LABELS, *FIRING_LABELS)}
FILE_EXTS = {
    "log", "bak", "trn", "sql", "txt", "mdf", "ldf", "ndf", "exe", "dll", "csv", "xml", "json", "ps1", "psm1", "bat",
    "cmd", "ini", "config", "xlsx", "xls", "docx", "doc", "pdf", "png", "jpg", "jpeg", "gif", "zip", "rdl", "dtsx",
    "msi", "htm", "html", "js", "py", "cs", "aspx", "trc", "xel", "sqlplan", "dmp", "mdmp", "dat", "bacpac", "dacpac",
    "cer", "pfx", "key", "rpt", "out", "err", "eml", "vbs", "yml", "yaml", "md", "tmp", "msg", "ispac", "bim", "abf",
}
# Common English and support words that are never hosts. Instance candidates do not use this list,
# because instance names are often ordinary words (REPORTS, PROD, SHAREPOINT).
BUILTIN_STOP = frozenset("""
a about above after again against all also am an and any are aren as at be because been before being below
between both but by can cannot could did do does doing done down during each few for from further had has have
having he her here hers him his how i if in into is it its itself just let me more most my no nor not now of off
on once only or other our ours out over own same she should so some such than that the their theirs them then
there these they this those through to too under until up very was we were what when where which while who whom
why will with would you your yours yes ok okay hi hello thanks thank please regards best dear team all
morning afternoon evening today tomorrow yesterday week month year day days hours hour minutes minute time
monday tuesday wednesday thursday friday saturday sunday jan feb mar apr may jun jul aug sep sept oct nov dec
january february march april june july august september october november december
server servers database databases sql instance instances service services job jobs agent backup backups restore
log logs error errors issue issues alert alerts ticket tickets client user users login logins password
query queries table tables index indexes disk disks drive drives space memory cpu file files folder
http https www com net org html email mail subject sent cc bcc re fw fwd note notes see attached attachment
microsoft windows azure aws cluster node nodes replica primary secondary update updates patch patches new old
check checked checking look looking report reports reporting run running ran failed failing slow down help
""".split())

# --- Names -------------------------------------------------------------------


def compress(name: str) -> str:
    """A name's shape as runs of letters (L) and digits (D) with other characters kept:
    'aautdb01' -> 'L6 D2', 'sql-prd-01' -> 'L3 - L3 - D2'."""
    out = []
    for m in re.finditer(r"[^\W\d_]+|\d+|.", name):
        s = m.group()
        out.append(f"L{len(s)}" if s[0].isalpha() else f"D{len(s)}" if s[0].isdigit() else s)
    return " ".join(out)


def edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Optimal string alignment distance (a swap of two neighbors counts 1), or cap + 1 when larger."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return min(prev[-1], cap + 1)


def trailing_digits_only(a: str, b: str) -> bool:
    """True when two names differ only in their trailing digits (aautdb03 vs aautdb01)."""
    sa, sb = a.rstrip("0123456789"), b.rstrip("0123456789")
    return sa == sb and sa != a and sb != b


def near_match(name: str, known) -> tuple[str | None, str | None]:
    """Classify a name against known names of the same kind: ('known', k), ('typo', k) for distance 1
    with the same pattern, ('sibling', k) for distance 1 to 2 differing only in trailing digits, else
    (None, None). A typo wins over a sibling; ties go to the alphabetically first known name."""
    if name in known:
        return "known", name
    typo = sibling = None
    pattern = compress(name)
    for k in sorted(known):
        if abs(len(k) - len(name)) > 2:
            continue
        d = edit_distance(name, k, cap=2)
        if d > 2:
            continue
        if trailing_digits_only(name, k):
            sibling = sibling or k
        elif d == 1 and compress(k) == pattern:
            typo = typo or k
    if typo:
        return "typo", typo
    if sibling:
        return "sibling", sibling
    return None, None


def normalize_reference(value) -> ParsedName | None:
    """A server-shaped reference (HOST, HOST\\INSTANCE, FQDN, HOST,PORT, HOST:PORT or tcp:HOST) as a
    ParsedName, or None for anything else, such as 'Job: ...' or free text. The default instance
    MSSQLSERVER is stored as the host alone."""
    s = str(value or "").strip().strip("[]\"'`").strip()
    if not s or any(ch.isspace() for ch in s):
        return None
    m = re.fullmatch(r"(.+?):(\d{2,5})", s)
    if m and not s.lower().startswith("tcp:"):
        s = m.group(1)
    parsed = parse_server_name(s)
    if not parsed:
        return None
    host, instance = parsed.host, parsed.instance
    if not (IPV4_RE.match(host) or (HOST_RE.match(host) and re.search(r"[a-z]", host))):
        return None
    if instance == DEFAULT_INSTANCE:
        instance = None
    if instance is not None and not INSTANCE_RE.match(instance):
        return None
    return ParsedName(host, instance, parsed.port)


def is_alert_ticket(ticket: dict) -> bool:
    return ticket.get("is_alert") is True or bool(ticket.get("alert_fields"))


def ticket_names(ticket: dict) -> list[ParsedName]:
    """Server-shaped names a ticket carries outside its text: DW2.0 servers and parsed alert fields."""
    values = list(ticket.get("dw_servers") or [])
    alert = ticket.get("alert_fields") or {}
    values += [alert[k] for k in ("server", "instance", "db_instance") if alert.get(k)]
    return [p for p in (normalize_reference(v) for v in values) if p]


# --- Known-names directory -----------------------------------------------------


class ServerDirectory(Protocol):
    """The known hosts and (host, instance) pairs per client. LocalServerDirectory builds it from
    ticket exports and data/instances.csv; CmdbApiDirectory will read it from the CMDB API. The
    detector only uses these methods, so either can be passed to detect_servers().

    Names are normalized as parse_server_name() does: lower case, no domain, no port. client_key is
    the Zendesk organization id, or 'name:<organization name in lower case>' when there is none.
    """

    def hosts(self, client_key: str | None = None) -> list[str]: ...  # normalized hosts

    def instances(self, client_key: str | None = None) -> list[tuple[str, str]]: ...  # (host, instance)

    def instance_names(self) -> Counter[str]: ...  # instance name -> number of hosts using it

    def instance_clients(self) -> Counter[str]: ...  # instance name -> number of clients using it

    def hosts_with_instance(self, instance: str, client_key: str | None) -> list[str]: ...

    def lookup(self, host: str | None, instance: str | None, client_key: str | None) -> dict: ...

    def clients(self) -> list[str]: ...

    def client_for(self, ticket: dict) -> str | None: ...  # the ticket's client key, as Cmdb.for_ticket picks it


def ticket_client_keys(ticket: dict) -> tuple[str | None, str | None]:
    org = str(ticket.get("organization_id") or "").strip() or None
    name = (ticket.get("organization_name") or "").strip().lower()
    return org, f"name:{name}" if name else None


class LocalServerDirectory:
    """Known names from local sources: server-shaped alert fields, dw_servers and data/instances.csv.
    Each client is keyed by its Zendesk organization id, or by its name when it has none; a name seen
    with an id is an alias for that id."""

    def __init__(self):
        self._hosts: dict[str, set[str]] = defaultdict(set)
        self._pairs: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._alias: dict[str, str] = {}
        self.skipped = 0  # references that were not server-shaped

    def add(self, org: str | None, name_key: str | None, parsed: ParsedName) -> None:
        key = org or name_key
        if not key:
            return
        if org and name_key:
            self._alias.setdefault(name_key, org)
        self._hosts[key].add(parsed.host)
        if parsed.instance:
            self._pairs[key].add((parsed.host, parsed.instance))

    def add_ticket(self, ticket: dict) -> None:
        org, name_key = ticket_client_keys(ticket)
        for parsed in ticket_names(ticket):
            self.add(org, name_key, parsed)

    def add_instances_csv(self, path: Path) -> None:
        import csv
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                parsed = normalize_reference(row.get("server_name"))
                if not parsed:
                    self.skipped += 1
                    continue
                org = (row.get("zendesk_organization_id") or "").strip() or None
                name = (row.get("client_name") or "").strip().lower()
                self.add(org, f"name:{name}" if name else None, parsed)

    @classmethod
    def from_sources(cls, tickets: list[dict], instances_path: Path | None = INSTANCES_PATH) -> "LocalServerDirectory":
        d = cls()
        for t in tickets:
            d.add_ticket(t)
        if instances_path and Path(instances_path).exists():
            d.add_instances_csv(Path(instances_path))
        return d

    def without(self, hosts=frozenset(), instances=frozenset()) -> "LocalServerDirectory":
        """A copy with the given hosts (and their pairs) and instance names removed."""
        d = LocalServerDirectory()
        d._alias = dict(self._alias)
        for key, hs in self._hosts.items():
            kept = {h for h in hs if h not in hosts}
            if kept:
                d._hosts[key] = kept
        for key, ps in self._pairs.items():
            kept = {(h, i) for h, i in ps if h not in hosts and i not in instances}
            if kept:
                d._pairs[key] = kept
        return d

    def _keys(self, client_key: str | None) -> list[str]:
        if client_key is None:
            return list(set(self._hosts) | set(self._pairs))
        return [self._alias.get(client_key, client_key)]

    def clients(self) -> list[str]:
        return sorted(set(self._hosts) | set(self._pairs))

    def client_for(self, ticket: dict) -> str | None:
        org, name_key = ticket_client_keys(ticket)
        if org and (org in self._hosts or org in self._pairs):
            return org
        if name_key and name_key in self._alias:
            return self._alias[name_key]
        return org or name_key

    def hosts(self, client_key: str | None = None) -> list[str]:
        return sorted({h for k in self._keys(client_key) for h in self._hosts.get(k, ())})

    def instances(self, client_key: str | None = None) -> list[tuple[str, str]]:
        return sorted({p for k in self._keys(client_key) for p in self._pairs.get(k, ())})

    def instance_names(self) -> Counter:
        return Counter(i for _, i in {p for ps in self._pairs.values() for p in ps})

    def instance_clients(self) -> Counter:
        return Counter(i for ps in self._pairs.values() for i in {i for _, i in ps})

    def hosts_with_instance(self, instance: str, client_key: str | None) -> list[str]:
        return sorted({h for h, i in self.instances(client_key) if i == instance})

    def lookup(self, host: str | None, instance: str | None, client_key: str | None) -> dict:
        """Status of a host, a (host, instance) pair or a bare instance within the client (all clients
        when client_key is None). The same host is required for a pair, as in match_mention(); the
        default instance counts as the host alone."""
        if instance == DEFAULT_INSTANCE:
            instance = None
        hosts = set(self.hosts(client_key))
        if instance is None:
            if host is None:
                return {"status": "unknown", "hosts": []}
            return {"status": "known", "hosts": [host]} if host in hosts else {"status": "unknown_host", "hosts": []}
        if host is None:
            found = self.hosts_with_instance(instance, client_key)
            status = "unknown_instance" if not found else "known" if len(found) == 1 else "ambiguous"
            return {"status": status, "hosts": found}
        if (host, instance) in set(self.instances(client_key)):
            return {"status": "known", "hosts": [host]}
        if host in hosts:
            return {"status": "new_instance_on_known_host", "hosts": [host]}
        return {"status": "unknown_host", "hosts": []}

    def stats(self) -> dict:
        names = self.instance_names()
        by_client = self.instance_clients()
        return {"clients": len(self.clients()), "hosts": len({h for hs in self._hosts.values() for h in hs}),
                "pairs": len({p for ps in self._pairs.values() for p in ps}), "instance_names": len(names),
                "names_on_several_hosts": sum(1 for n in names.values() if n > 1),
                "names_at_several_clients": sum(1 for n in by_client.values() if n > 1),
                "skipped_references": self.skipped}


class CmdbApiDirectory:
    """Placeholder for the Keystone CMDB API, with the same methods as LocalServerDirectory. It calls
    nothing. Expected API shape (to confirm with the CMDB owners):

    GET /clients/{client_key}/servers -> [{"host": "sqlprd01", "instance": "reports" | null,
        "environment": "prod", "fqdn": "sqlprd01.corp.local"}]
    GET /instance-names -> [{"instance": "reports", "hosts": 12, "clients": 4}]

    Open questions: whether a client is identified by Zendesk organization id, Keystone id or name,
    and whether instances come back as HOST\\INSTANCE strings or as separate fields."""

    def __init__(self, base_url: str | None = None):
        self.base_url = base_url

    def _todo(self, *args, **kwargs):
        raise NotImplementedError("The CMDB API does not exist yet; use LocalServerDirectory")

    hosts = instances = instance_names = instance_clients = hosts_with_instance = lookup = clients = client_for = _todo


# --- Client naming conventions -------------------------------------------------


def _floor(n: int) -> int:
    return 1 if n < 3 else max(2, math.ceil(0.1 * n))


class NameProfile:
    """One client's names of one kind: the names, common prefixes (3 to 5 characters) and common
    compressed patterns. With fewer than 3 names every prefix and pattern counts as common.

    The has_* checks are leave-one-out: a name that is itself in the profile is left out first, so a
    known name scores on its resemblance to the client's other names, as an unseen name would.
    Otherwise every training label (all known names) would carry its own answer."""

    def __init__(self, names):
        self.names = frozenset(names)
        self.prefix_counts = Counter(p for nm in self.names for p in {nm[:k] for k in (3, 4, 5) if len(nm) > k})
        self.pattern_counts = Counter(compress(nm) for nm in self.names)
        floor = _floor(len(self.names))
        self.prefixes = frozenset(p for p, c in self.prefix_counts.items() if c >= floor)
        self.patterns = frozenset(p for p, c in self.pattern_counts.items() if c >= floor)

    def _common(self, counts: Counter, key: str, name: str) -> bool:
        own = int(name in self.names)
        c = counts.get(key, 0) - own
        return c > 0 and c >= _floor(len(self.names) - own)

    def has_prefix(self, name: str, k: int) -> bool:
        return len(name) > k and self._common(self.prefix_counts, name[:k], name)

    def has_pattern(self, name: str) -> bool:
        return self._common(self.pattern_counts, compress(name), name)


EMPTY_PROFILE = NameProfile(())


class Knowledge:
    """Everything the features read from the directory, computed once: instance vocabulary, client
    profiles and the stop list."""

    def __init__(self, directory: ServerDirectory, stop: frozenset):
        self.directory = directory
        self.stop = stop
        self.inst_hosts = directory.instance_names()
        self.inst_clients = directory.instance_clients()
        self.inst_hosts_by_client: dict[str, Counter] = defaultdict(Counter)
        for client in directory.clients():
            for _, i in directory.instances(client):
                self.inst_hosts_by_client[i][client] += 1
        self.known_instances = frozenset(self.inst_hosts)
        self.all_hosts = frozenset(directory.hosts())
        self.profiles: dict[str, dict[str, NameProfile]] = {}
        self.prefix_clients = {"host": defaultdict(set), "instance": defaultdict(set)}
        self.pattern_clients = {"host": defaultdict(set), "instance": defaultdict(set)}
        for client in directory.clients():
            prof = {"host": NameProfile(directory.hosts(client)),
                    "instance": NameProfile({i for _, i in directory.instances(client)})}
            self.profiles[client] = prof
            for kind, p in prof.items():
                for x in p.prefixes:
                    self.prefix_clients[kind][x].add(client)
                for x in p.patterns:
                    self.pattern_clients[kind][x].add(client)
        self._cache: dict = {}

    def instance_vocabulary(self, client: str | None, name: str) -> tuple[int, int]:
        """(hosts, clients) using the name as an instance at clients other than the ticket's own. The
        own client is left out because a human ticket's bare instance label is exactly 'a known
        instance of the own client'; counting it would hand the model the label."""
        by_client = self.inst_hosts_by_client.get(name)
        if not by_client:
            return 0, 0
        own = by_client.get(client, 0) if client else 0
        return sum(by_client.values()) - own, len(by_client) - (1 if own else 0)

    def near_known_host(self, client: str | None, name: str) -> str | None:
        """The client's known host this name is a typo or sibling of (see near_match), or None."""
        key = ("near", client, name)
        if key not in self._cache:
            relation, known = near_match(name, self.profile(client, "host").names)
            self._cache[key] = known if relation in ("typo", "sibling") else None
        return self._cache[key]

    def profile(self, client: str | None, kind: str) -> NameProfile:
        return self.profiles.get(client, {}).get(kind, EMPTY_PROFILE) if client else EMPTY_PROFILE

    def profile_features(self, client: str | None, kind: str, name: str) -> dict:
        key = (client, kind, name)
        if key in self._cache:
            return self._cache[key]
        prof = self.profile(client, kind)
        pattern = compress(name)
        own_prefix = max((k for k in (5, 4, 3) if prof.has_prefix(name, k)), default=0)
        own_pattern = prof.has_pattern(name)
        dist = 4
        others = prof.names - {name}  # leave-one-out, see NameProfile
        for k in others:
            if abs(len(k) - len(name)) <= 3:
                dist = min(dist, edit_distance(name, k, cap=3))
                if dist == 0:
                    break
        other = any(self.prefix_clients[kind].get(name[:k], set()) - {client} for k in (5, 4) if len(name) > k) \
            or bool(self.pattern_clients[kind].get(pattern, set()) - {client})
        p = kind[0]
        out = {f"{p}_own_prefix": own_prefix / 5, f"{p}_own_pattern": float(own_pattern),
               # Similarity, not distance: 0 when the client has no other names, so "no information"
               # adds nothing instead of looking like "unlike every known name".
               f"{p}_sim": 1.0 - min(dist / max(len(name), 1), 1.0) if others else 0.0,
               f"{p}_other_conv": float(bool(other) and not own_prefix and not own_pattern)}
        self._cache[key] = out
        return out


def build_stop_words(tickets: list[dict], directory: ServerDirectory, min_share: float = 0.005,
                     min_docs: int = 5) -> frozenset:
    """The built-in list plus alphabetic words in at least min_share of human tickets, minus names in
    the directory, so a frequently mentioned host is not stopped."""
    human = [t for t in tickets if t.get("is_alert") is False]
    df = Counter()
    for t in human:
        text = f"{t.get('subject') or ''}\n{(t.get('description') or '')[:DEFAULT_MAX_CHARS]}".lower()
        df.update({w for w in re.findall(r"[a-z]{3,}", text)})
    floor = max(min_docs, math.ceil(min_share * len(human)))
    names = set(directory.hosts()) | set(directory.instance_names())
    return frozenset((BUILTIN_STOP | {w for w, n in df.items() if n >= floor}) - names)


# --- Candidates ----------------------------------------------------------------


def _trim(raw: str, start: int) -> tuple[str, int]:
    """Strip punctuation around a token; a leading \\\\ (UNC path) is kept."""
    lead = 0
    while lead < len(raw) and (raw[lead] in ".,:-$#" or (raw[lead] == "\\" and not raw.startswith("\\\\", lead))):
        lead += 1
    raw = raw[lead:].rstrip(".,:-\\$#")
    return raw, start + lead


def _split(raw: str, start: int, sep: str) -> list[tuple[int, int, frozenset]]:
    out, pos = [], 0
    for piece in raw.split(sep):
        text, s = _trim(piece, start + pos)
        if text:
            out += token_parts(text, s)
        pos += len(piece) + 1
    return out


def token_parts(raw: str, start: int) -> list[tuple[int, int, frozenset]]:
    """The server-like parts of one token as (start, end, flags): a UNC host, an MSSQL$ or SQLAgent$
    instance, a tcp: name, a HOST,PORT or HOST:PORT name, a HOST\\INSTANCE name, or the token itself."""
    if raw.startswith("\\\\"):
        host = re.match(r"[^\\]*", raw[2:]).group()
        host, s = _trim(host, start + 2)
        return [(s, s + len(host), frozenset({"unc"}))] if host else []
    m = SERVICE_RE.match(raw)
    if m:
        s = start + m.start(2)
        return [(s, s + len(m.group(2)), frozenset({"service"}))]
    if "$" in raw:
        return _split(raw, start, "$")
    low = raw.lower()
    for prefix in ("tcp:", "np:", "lpc:", "admin:"):
        if low.startswith(prefix) and len(raw) > len(prefix):
            text, s = _trim(raw[len(prefix):], start + len(prefix))
            return token_parts(text, s) if text else []
    if ":" in raw:
        if TIME_RE.match(raw):
            return []
        m = re.fullmatch(r"([^:]+):(\d{2,5})", raw)
        if m:
            return [(start, start + len(m.group(1)), frozenset({"port"}))]
        return _split(raw, start, ":")
    if "," in raw:
        if re.fullmatch(r"[^,]+,\d{2,5}", raw):
            return [(start, start + len(raw), frozenset({"port"}))]
        return _split(raw, start, ",")
    if raw.count("\\") > 1:
        return _split(raw, start, "\\")
    return [(start, start + len(raw), frozenset())]


def ticket_text(ticket: dict, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str, int]:
    """The text the detector reads, subject + newline + description, and the subject length."""
    subject = ticket.get("subject") or ""
    return f"{subject}\n{(ticket.get('description') or '')[:max_chars]}", len(subject)


def tokenize(text: str) -> list[tuple[str, int, int]]:
    """(token, start, end) for each token, punctuation trimmed."""
    out = []
    for m in TOKEN_RE.finditer(text):
        raw, s = _trim(m.group(), m.start())
        if raw:
            out.append((raw, s, s + len(raw)))
    return out


def host_name_ok(host: str, min_len: int = 3) -> bool:
    if IPV4_RE.match(host):
        return True
    return (min_len <= len(host) <= 63 and bool(HOST_RE.match(host)) and bool(re.search(r"[a-z]", host))
            and not NOISE_RE.match(host))


def ticket_candidates(ticket: dict, know: Knowledge, *, alert_context: str = "strip_labels",
                      max_chars: int = DEFAULT_MAX_CHARS) -> list[dict]:
    """Host and instance candidates in a ticket's subject and description, without features.

    Host candidates: 3 to 63 characters with a letter (or an IPv4 address), not a stop word, date,
    ticket number, KB article or error code, not a file name or an email address part. Inside a
    connection string, UNC path or with a port the stop list is not applied. Instance candidates:
    bare tokens of 2 to 16 characters with a letter that are a known instance name, are within 3
    tokens of an instance cue (instance, service, a preceding backslash, MSSQL$, SQLAgent$), or are
    written in upper case. HOST\\INSTANCE tokens are always candidates."""
    text, _ = ticket_text(ticket, max_chars)
    alert = is_alert_ticket(ticket)
    hide = alert and alert_context == "token_only"
    strip = alert and alert_context == "strip_labels"
    tokens = tokenize(text)
    words = [t[0].lower() for t in tokens]
    breaks = [m.start() for m in SENTENCE_RE.finditer(text)]
    conn = [m.span(1) for m in CONN_RE.finditer(text)]
    client = know.directory.client_for(ticket)
    cands = []
    for ti, (raw, s0, _) in enumerate(tokens):
        parts = token_parts(raw, s0)
        if not parts:
            continue
        window = [] if hide else words[max(0, ti - 3):ti] + words[ti + 1:ti + 4]
        if strip:
            window = [w for w in window if w not in LABEL_WORDS]
        window_cue = any(w in INSTANCE_CUES or w.startswith(SERVICE_PREFIXES) for w in window)
        for s, e, flags in parts:
            c = _candidate(text, s, e, flags, know, window_cue, conn)
            if not c:
                continue
            c.update(ti=ti, sent=bisect.bisect_left(breaks, s), ctx=" ".join(window))
            c["rule"] = _rule(c, words, ti, know, client)
            cands.append(c)
    _mark_host_nearby(cands)
    return cands


def _candidate(text: str, s: int, e: int, flags: frozenset, know: Knowledge, window_cue: bool, conn) -> dict | None:
    part = text[s:e]
    parsed = parse_server_name(part)
    if not parsed:
        return None
    host, instance = parsed.host, parsed.instance
    in_conn = any(a <= s and e <= b for a, b in conn)
    in_unc = "unc" in flags
    base = {"text": part, "start": s, "end": e, "in_conn": in_conn, "in_unc": in_unc,
            "in_url": text[max(0, s - 2):s] == "//", "service": "service" in flags,
            "has_port": parsed.port is not None or "port" in flags or text[max(0, s - 4):s].lower() == "tcp:",
            "after_label": _after_label(text, s),
            "is_ip": bool(IPV4_RE.match(host)), "sql_port": _sql_port(text, s, e, part)}
    if "\\" in part:
        if not (host_name_ok(host, 2) and instance and INSTANCE_RE.match(instance)):
            return None
        return {**base, "kind": "host_instance", "host": host, "instance": instance, "name": f"{host}\\{instance}",
                "host_ok": True, "inst_ok": False, "cue": window_cue}
    head = part.split(",")[0]
    dotted = "." in head and not base["is_ip"]
    is_file = dotted and head.rsplit(".", 1)[-1].lower() in FILE_EXTS
    email = text[e:e + 1] == "@" or text[max(0, s - 1):s] == "@"
    strong = in_conn or in_unc or base["has_port"]
    host_ok = (host_name_ok(host) and not is_file and not email and host != "localhost"
               and (strong or host not in know.stop))
    cue = window_cue or base["service"] or (text[max(0, s - 1):s] == "\\" and not in_unc)
    inst_ok = (not dotted and not base["has_port"] and not in_unc and not email and bool(BARE_INSTANCE_RE.match(part))
               and bool(re.search(r"[A-Za-z]", part))
               and (part.lower() in know.known_instances or cue or part == part.upper()))
    if not (host_ok or inst_ok):
        return None
    return {**base, "kind": "token", "host": host, "instance": None, "name": host,
            "host_ok": host_ok, "inst_ok": inst_ok, "cue": cue}


def _sql_port(text: str, s: int, e: int, part: str) -> bool:
    """True for a port written the SQL Server way: HOST,PORT, tcp:HOST, or a colon port of 1433 or
    1434. Other colon ports (host:8080, site:443) are web addresses."""
    if "," in part or text[max(0, s - 4):s].lower() == "tcp:":
        return True
    m = re.match(r":(\d{2,5})(?!\d)", text[e:e + 6])
    return bool(m) and m.group(1) in ("1433", "1434")


def _rule(c: dict, words: list[str], ti: int, know: Knowledge, client: str | None = None) -> str | None:
    """The name of the rule that marks this candidate as a host or an instance, or None. Instance
    rules: an MSSQL$ or SQLAgent$ service suffix, '<name> instance', 'instance <name>' and 'instance
    named <name>'. Host rules: HOST\\INSTANCE whose host has a digit, is known or is in a connection
    string; a connection-string value; a UNC host; a name with a port or tcp:; and a name with a
    digit after server, host, vm and the like, or after 'instance on'. Alert template lines
    ('Instance SQL01') are left to the model.

    Known names of the ticket's client, as a backup to the model: a known host with a digit (the
    directory holds digit-free abbreviations and words from dw_servers; against hand labels those
    matched correctly 16% of the time, against 75% with a digit); a near known host (one edit with the
    same pattern, or only trailing digits differ; 5 or more characters with a digit); and a known
    instance name written in upper case (instance names are often ordinary words, so lower case
    'reports' in running text does not count). A port counts only when written the SQL Server way."""
    prev1 = words[ti - 1] if ti >= 1 else ""
    prev2 = words[ti - 2] if ti >= 2 else ""
    next1 = words[ti + 1] if ti + 1 < len(words) else ""
    name = c["host"]
    has_digit = any(ch.isdigit() for ch in name)
    own_hosts = know.profile(client, "host").names
    if c["kind"] == "token" and c["host_ok"] and has_digit and name in own_hosts:
        return "host:known_name"
    if c["inst_ok"] and c["kind"] == "token" and name not in FILLER_WORDS:
        if c["service"]:
            return "instance:service_suffix"
        # Next to "instance", ordinary words are mostly adjectives ("managed instance", "instance
        # running"), so a frequent word counts only when written in upper case or known as an instance.
        word_ok = c["text"] == c["text"].upper() or name in know.known_instances or name not in know.stop
        if not c["after_label"] and word_ok:
            if next1 in ("instance", "instances"):
                return "instance:name_before_instance"
            if prev1 == "instance" or (prev1 in ("named", "called") and prev2 == "instance"):
                # "on instance abc-sql01": a name with a digit, hyphen or dot is the server
                if c["host_ok"] and re.search(r"[\d\-.]", c["text"]):
                    return "host:after_server_word"
                return "instance:name_after_instance"
        if c["text"] == c["text"].upper() and name in know.profile(client, "instance").names:
            return "instance:known_name"
    if not c["host_ok"]:
        return None
    if c["kind"] == "host_instance":
        if has_digit or c["in_conn"] or name in know.all_hosts:
            return "host:host_instance"
        return None
    if c["in_conn"]:
        return "host:connection_string"
    if c["in_unc"]:
        return "host:unc_path"
    if c["has_port"] and c["sql_port"]:
        return "host:port"
    if has_digit and not c["is_ip"] and (prev1 in HOST_CUE_WORDS | {"instance"}
                                         or (prev1 == "on" and prev2 in HOST_CUE_WORDS | {"instance"})):
        return "host:after_server_word"
    if has_digit and len(name) >= 5 and not c["is_ip"] and know.near_known_host(client, name):
        return "host:near_known_name"
    return None


def _after_label(text: str, s: int) -> bool:
    """True when the candidate follows a 'Label:' or 'Label value' start of line."""
    line_start = text.rfind("\n", 0, s) + 1
    prefix = text[line_start:s]
    return bool(LABEL_PREFIX_RE.match(prefix)) or prefix.strip().lower() in ALERT_LABEL_LINES


def _mark_host_nearby(cands: list[dict]) -> None:
    """host_nearby: another host candidate in the same sentence or within 10 tokens."""
    hosts = [c for c in cands if c["host_ok"]]
    per_sentence = Counter(c["sent"] for c in hosts)
    positions = sorted(c["ti"] for c in hosts)
    for c in cands:
        own = 1 if c["host_ok"] else 0
        lo = bisect.bisect_left(positions, c["ti"] - 10)
        hi = bisect.bisect_right(positions, c["ti"] + 10)
        c["host_nearby"] = per_sentence[c["sent"]] - own > 0 or hi - lo - own > 0


def add_features(cands: list[dict], know: Knowledge, ticket: dict, *, hide_client: bool = False) -> None:
    """The dense features of each candidate: token shape, compressed pattern, instance vocabulary,
    context flags and the client's naming convention. hide_client computes them as for a ticket
    whose client is unknown (client dropout in training)."""
    client = None if hide_client else know.directory.client_for(ticket)
    alert = float(is_alert_ticket(ticket))
    for c in cands:
        raw, host = c["text"], c["host"]
        inst_name = c["instance"] if c["kind"] == "host_instance" else host
        letters = [ch for ch in raw if ch.isalpha()]
        vocab = know.instance_vocabulary(client, inst_name)
        f = {
            "len": float(len(host)), "upper_ratio": sum(ch.isupper() for ch in letters) / len(letters) if letters else 0.0,
            "digits": float(sum(ch.isdigit() for ch in host)), "ends_digits": float(host[-1:].isdigit()),
            "mixed": float(bool(letters) and any(ch.isdigit() for ch in host)),
            "has_backslash": float(c["kind"] == "host_instance"), "has_dot": float("." in raw.split("\\")[0]),
            "has_hyphen": float("-" in host), "has_underscore": float("_" in host), "has_port": float(c["has_port"]),
            "is_ip": float(c["is_ip"]), "pattern": compress(host),
            "inst_hosts_log": math.log1p(vocab[0]), "inst_clients_log": math.log1p(vocab[1]),
            "service_suffix": float(c["service"]), "in_conn": float(c["in_conn"]), "in_unc": float(c["in_unc"]),
            "in_url": float(c["in_url"]), "after_label": float(c["after_label"]), "near_cue": float(c["cue"]),
            "host_nearby": float(c["host_nearby"]), "host_ok": float(c["host_ok"]), "inst_ok": float(c["inst_ok"]),
            "host_instance": float(c["kind"] == "host_instance"), "is_alert": alert,
            "client_known": float(client in know.profiles),
        }
        f.update(know.profile_features(client, "host", host))
        f.update(know.profile_features(client, "instance", inst_name))
        c["dense"] = f


# --- Labels --------------------------------------------------------------------


def gold_names(ticket: dict, label_dir: ServerDirectory) -> tuple[set[str], set[str]]:
    """The hosts and bare instance names a ticket's candidates are labeled against. Alerts: the parsed
    server and the host part of a server-shaped instance; no bare instances. Human tickets: hosts and
    instance parts of the ticket's own dw_servers, plus the hosts and instance names the directory
    knows for the ticket's client."""
    if is_alert_ticket(ticket):
        return {p.host for p in ticket_names({"alert_fields": ticket.get("alert_fields")})}, set()
    own = [p for p in (normalize_reference(v) for v in ticket.get("dw_servers") or []) if p]
    client = label_dir.client_for(ticket)
    hosts = {p.host for p in own} | (set(label_dir.hosts(client)) if client else set())
    instances = {p.instance for p in own if p.instance} | ({i for _, i in label_dir.instances(client)} if client else set())
    return hosts, instances


def label_of(c: dict, hosts: set[str], instances: set[str]) -> str:
    if c["host_ok"] and c["host"] in hosts:
        return "host"
    if c["inst_ok"] and c["kind"] == "token" and c["host"] in instances:
        return "instance"
    return "none"


def synthetic_instance(c: dict) -> dict | None:
    """A bare instance example from a HOST\\INSTANCE mention, with the host removed: the instance part
    as its own token, in the same context, with no backslash cue."""
    if c["kind"] != "host_instance" or not c["instance"] or c["instance"] == DEFAULT_INSTANCE:
        return None
    cut = c["text"].index("\\") + 1
    return {**c, "text": c["text"][cut:], "start": c["start"] + cut, "kind": "token", "host": c["instance"],
            "instance": None, "name": c["instance"], "host_ok": False, "inst_ok": True, "has_port": False,
            "in_unc": False, "after_label": False, "cue": c["cue"], "label": "instance", "source": "synthetic"}


def make_examples(tickets: list[dict], know: Knowledge, label_dir: ServerDirectory, *, alert_context: str,
                  max_chars: int = DEFAULT_MAX_CHARS, synthetic: bool = True, exclude_hosts=frozenset(),
                  exclude_instances=frozenset(), neg_ratio: float | None = DEFAULT_NEG_RATIO,
                  client_dropout: float = DEFAULT_CLIENT_DROPOUT, seed: int = 0) -> tuple[list[dict], dict]:
    """Labeled training candidates with features. Candidates naming an excluded host or instance are
    dropped entirely. 'none' examples are sampled down to neg_ratio per positive (None keeps all).
    For a client_dropout share of tickets the features ignore the client (labels do not).
    Returns the examples and counts per label and source."""
    rng = np.random.default_rng(seed)
    pos, neg = [], []
    hidden = 0
    for t in tickets:
        hosts, instances = gold_names(t, label_dir)
        source = "alert" if is_alert_ticket(t) else "human"
        cands = ticket_candidates(t, know, alert_context=alert_context, max_chars=max_chars)
        kept = []
        for c in cands:
            if c["host"] in exclude_hosts or c["host"] in exclude_instances or c["instance"] in exclude_instances:
                continue
            c["label"], c["source"] = label_of(c, hosts, instances), source
            kept.append(c)
            if synthetic and c["label"] == "host":
                s = synthetic_instance(c)
                if s:
                    kept.append(s)
        hide = bool(rng.random() < client_dropout)
        hidden += hide
        add_features(kept, know, t, hide_client=hide)
        for c in kept:
            (neg if c["label"] == "none" else pos).append(c)
    counts = Counter((c["label"], c["source"]) for c in pos + neg)
    if neg_ratio is not None and len(neg) > neg_ratio * max(len(pos), 1):
        pick = rng.choice(len(neg), size=int(neg_ratio * max(len(pos), 1)), replace=False)
        neg = [neg[i] for i in sorted(pick)]
    return pos + neg, {"before_sampling": counts, "none_kept": len(neg), "client_hidden_tickets": hidden}


# --- Model ---------------------------------------------------------------------


def _prep_dense(cands: list[dict], patterns: frozenset) -> list[dict]:
    return [{**c["dense"], "pattern": c["dense"]["pattern"] if c["dense"]["pattern"] in patterns else "other"}
            for c in cands]


class _Linear:
    """Logistic regression on character n-grams of the name, the context words and the dense features.
    char_ngrams=None leaves the n-grams out; C is the inverse regularization strength."""

    def __init__(self, seed: int, C: float = 2.0, char_ngrams: tuple[int, int] | None = (2, 5)):
        self.seed, self.C, self.char_ngrams = seed, C, char_ngrams

    def _matrix(self, cands: list[dict], fit: bool = False):
        names = [c["name"] for c in cands]
        ctx = ["<ctx> " + c["ctx"] for c in cands]
        dense = _prep_dense(cands, self.patterns)
        if fit:
            min_df = 2 if len(cands) > 1000 else 1
            self.char = TfidfVectorizer(analyzer="char_wb", ngram_range=self.char_ngrams or (2, 2), min_df=min_df,
                                        sublinear_tf=True, max_features=200_000, lowercase=True)
            self.words = TfidfVectorizer(token_pattern=r"\S+", min_df=min_df, binary=True, lowercase=False,
                                         max_features=50_000)
            self.dict = DictVectorizer()
            self.scale = MaxAbsScaler()
            parts = [self.char.fit_transform(names), self.words.fit_transform(ctx),
                     self.scale.fit_transform(self.dict.fit_transform(dense))]
        else:
            parts = [self.char.transform(names), self.words.transform(ctx),
                     self.scale.transform(self.dict.transform(dense))]
        if self.char_ngrams is None:
            parts = parts[1:]
        return hstack(parts).tocsr()

    def fit(self, cands: list[dict], y: list[str]) -> "_Linear":
        self.patterns = frozenset(p for p, _ in Counter(c["dense"]["pattern"] for c in cands).most_common(TOP_PATTERNS))
        self.clf = LogisticRegression(C=self.C, max_iter=2000, random_state=self.seed)
        self.clf.fit(self._matrix(cands, fit=True), y)
        return self

    def proba(self, cands: list[dict]) -> dict[str, np.ndarray]:
        p = self.clf.predict_proba(self._matrix(cands))
        return {c: p[:, i] for i, c in enumerate(self.clf.classes_)}


class _Boosted:
    """Gradient boosting on the dense features only: shape, pattern, vocabulary and client profile."""

    def __init__(self, seed: int):
        self.seed = seed

    def _matrix(self, cands: list[dict], fit: bool = False) -> np.ndarray:
        dense = _prep_dense(cands, self.patterns)
        if fit:
            self.dict = DictVectorizer(sparse=False, dtype=np.float32)
            return self.dict.fit_transform(dense)
        return self.dict.transform(dense)

    def fit(self, cands: list[dict], y: list[str]) -> "_Boosted":
        self.patterns = frozenset(p for p, _ in Counter(c["dense"]["pattern"] for c in cands).most_common(TOP_PATTERNS))
        self.clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.1, random_state=self.seed)
        self.clf.fit(self._matrix(cands, fit=True), y)
        return self

    def proba(self, cands: list[dict]) -> dict[str, np.ndarray]:
        p = self.clf.predict_proba(self._matrix(cands))
        return {c: p[:, i] for i, c in enumerate(self.clf.classes_)}


class ServerModel:
    """The fitted detector: a multiclass model over host, instance and none, an optional separate
    binary instance model, the thresholds and the settings candidates were built with."""

    def __init__(self, *, kind: str = "logreg", alert_context: str = "strip_labels", instance_model: bool = False,
                 stop: frozenset = BUILTIN_STOP, max_chars: int = DEFAULT_MAX_CHARS, seed: int = 0,
                 host_threshold: float = DEFAULT_THRESHOLD, instance_threshold: float = DEFAULT_THRESHOLD,
                 linear_params: dict | None = None):
        self.kind, self.alert_context, self.use_instance_model = kind, alert_context, instance_model
        self.linear_params = dict(linear_params or {})
        self.stop, self.max_chars, self.seed = stop, max_chars, seed
        self.host_threshold, self.instance_threshold = host_threshold, instance_threshold
        self._know: dict = {}

    def __getstate__(self):
        return {k: v for k, v in self.__dict__.items() if k != "_know"}

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._know = {}

    def knowledge(self, directory: ServerDirectory) -> Knowledge:
        key = id(directory)
        if key not in self._know or self._know[key].directory is not directory:
            self._know[key] = Knowledge(directory, self.stop)
        return self._know[key]

    def fit(self, examples: list[dict]) -> "ServerModel":
        y = [c["label"] for c in examples]
        main = _Linear(self.seed, **self.linear_params) if self.kind == "logreg" else _Boosted(self.seed)
        self.main = main.fit(examples, y)
        self.instance = None
        if self.use_instance_model:
            inst = [c for c in examples if c["inst_ok"]]
            labels = ["instance" if c["label"] == "instance" else "other" for c in inst]
            if len(set(labels)) == 2:
                self.instance = _Linear(self.seed, **self.linear_params).fit(inst, labels)
        return self

    def scores(self, cands: list[dict]) -> np.ndarray:
        """(n, 2) probabilities of host and instance; a class a candidate is not eligible for is 0. A rule
        match (see _rule) scores at least RULE_SCORE for its class and 0 for the other."""
        if not cands:
            return np.zeros((0, 2))
        p = self.main.proba(cands)
        n = len(cands)
        host = p.get("host", np.zeros(n)) * np.array([c["host_ok"] for c in cands])
        inst_mask = np.array([c["inst_ok"] for c in cands])
        inst = p.get("instance", np.zeros(n))
        if self.instance is not None and inst_mask.any():
            idx = np.flatnonzero(inst_mask)
            inst = np.zeros(n)
            inst[idx] = self.instance.proba([cands[i] for i in idx])["instance"]
        inst = inst * inst_mask
        rules = [c.get("rule") or "" for c in cands]
        host_rule = np.array([r.startswith("host:") for r in rules])
        inst_rule = np.array([r.startswith("instance:") for r in rules])
        host = np.where(host_rule, np.maximum(host, RULE_SCORE), np.where(inst_rule, 0.0, host))
        inst = np.where(inst_rule, np.maximum(inst, RULE_SCORE), np.where(host_rule, 0.0, inst))
        return np.column_stack([host, inst])


def decide(p_host: float, p_inst: float, host_threshold: float, instance_threshold: float) -> str | None:
    if p_host >= host_threshold and p_host >= p_inst:
        return "host"
    if p_inst >= instance_threshold:
        return "instance"
    return "host" if p_host >= host_threshold else None


# --- Detection, pairing and status ---------------------------------------------


def _evidence(text: str, start: int, end: int, width: int = 40) -> str:
    return " ".join(text[max(0, start - width):end + width].split())[:2 * width + 40]


def _correct(name: str, known, status: str) -> tuple[str, str, str | None, str | None]:
    """(name, status, typo_of, sibling_of) after the near-match check against known names."""
    relation, k = near_match(name, known)
    if relation == "typo":
        return k, "typo_of_known", k, None
    if relation == "sibling":
        return name, status, None, k
    return name, status, None, None


def detect_servers(ticket: dict, directory: ServerDirectory, model: ServerModel, *,
                   host_threshold: float | None = None, instance_threshold: float | None = None) -> list[dict]:
    """Hosts, HOST\\INSTANCE names and bare instances in one ticket. Offsets are into the field named
    by 'field' (subject or description)."""
    th_h = model.host_threshold if host_threshold is None else host_threshold
    th_i = model.instance_threshold if instance_threshold is None else instance_threshold
    know = model.knowledge(directory)
    cands = ticket_candidates(ticket, know, alert_context=model.alert_context, max_chars=model.max_chars)
    if not cands:
        return []
    add_features(cands, know, ticket)
    scores = model.scores(cands)
    text, subject_len = ticket_text(ticket, model.max_chars)
    client = directory.client_for(ticket)
    client_hosts = set(directory.hosts(client)) if client else set()
    client_pairs = set(directory.instances(client)) if client else set()
    hosts_with_pairs = {h for h, _ in client_pairs}
    client_instance_names = {i for _, i in client_pairs}

    found = []
    for c, (ph, pi) in zip(cands, scores):
        label = decide(ph, pi, th_h, th_i)
        if label is None:
            continue
        in_subject = c["start"] < subject_len
        offset = 0 if in_subject else subject_len + 1
        d = {"text": c["text"], "field": "subject" if in_subject else "description",
             "start": c["start"] - offset, "end": c["end"] - offset,
             "kind": "bare_instance" if label == "instance" else c["kind"] if c["kind"] == "host_instance" else "host",
             "host": None, "instance": None, "status": None, "candidates": [], "typo_of": None, "sibling_of": None,
             "score": round(float(ph if label == "host" else pi), 4), "rule": c.get("rule"),
             "evidence": _evidence(text, c["start"], c["end"]),
             "_ti": c["ti"], "_sent": c["sent"]}
        if d["kind"] == "bare_instance":
            d["instance"] = c["host"]
        else:
            d["host"] = c["host"]
            d["instance"] = c["instance"] if c["instance"] != DEFAULT_INSTANCE else None
            _host_status(d, directory, client, client_hosts)
        found.append(d)

    host_dets = [d for d in found if d["kind"] != "bare_instance" and d["host"]]
    for d in found:
        if d["kind"] == "bare_instance":
            _pair_instance(d, host_dets, directory, client, client_hosts, client_pairs, hosts_with_pairs,
                           client_instance_names)
    for d in found:
        del d["_ti"], d["_sent"]
    return found


def _host_status(d: dict, directory: ServerDirectory, client: str | None, client_hosts: set[str]) -> None:
    r = directory.lookup(d["host"], d["instance"], client)
    if r["status"] == "known":
        d["status"] = "known"
        return
    if r["status"] == "new_instance_on_known_host":
        on_host = {i for h, i in directory.instances(client) if h == d["host"]}
        d["instance"], d["status"], d["typo_of"], d["sibling_of"] = _correct(d["instance"], on_host,
                                                                             "new_instance_on_known_host")
        if d["typo_of"]:
            d["typo_of"] = f"{d['host']}\\{d['typo_of']}"
        return
    d["host"], d["status"], d["typo_of"], d["sibling_of"] = _correct(d["host"], client_hosts, "possible_new_host")


def _pair_instance(d, host_dets, directory, client, client_hosts, client_pairs, hosts_with_pairs,
                   client_instance_names) -> None:
    """Pair a bare instance with a host: the nearest host detection in the ticket (same sentence, then
    within 10 tokens, then anywhere) when the directory has that pair or no instances for that host;
    otherwise the client's hosts that have this instance; otherwise no host."""
    inst = d["instance"]
    order = sorted(host_dets, key=lambda h: (h["_sent"] != d["_sent"], abs(h["_ti"] - d["_ti"]) > 10,
                                             abs(h["_ti"] - d["_ti"])))
    for h in order:
        if (h["host"], inst) in client_pairs or h["host"] not in hosts_with_pairs:
            d["host"] = h["host"]
            if (h["host"], inst) in client_pairs:
                d["status"] = "known"
            elif h["host"] in client_hosts:
                d["status"] = "new_instance_on_known_host"
            else:
                d["status"] = "possible_new_instance"
            return
    found = directory.hosts_with_instance(inst, client) if client else []
    if not found and inst not in client_instance_names:
        relation, k = near_match(inst, client_instance_names)
        if relation == "typo":
            d["instance"], d["typo_of"], d["status"] = k, k, "typo_of_known"
            found = directory.hosts_with_instance(k, client)
            d["host"] = found[0] if len(found) == 1 else None
            d["candidates"] = found if len(found) > 1 else []
            return
        if relation == "sibling":
            d["sibling_of"] = k
    if len(found) == 1:
        d["host"], d["status"] = found[0], "known"
    elif found:
        d["status"], d["candidates"] = "ambiguous", found
    else:
        d["status"] = "possible_new_instance"


# --- Training and selection ----------------------------------------------------


def split_validation(tickets: list[dict], share: float, seed: int) -> tuple[list[dict], list[dict]]:
    idx = np.random.default_rng(seed).permutation(len(tickets))
    n_val = int(round(share * len(tickets)))
    val = set(idx[:n_val].tolist())
    return [t for i, t in enumerate(tickets) if i not in val], [t for i, t in enumerate(tickets) if i in val]


def holdout_names(names, share: float, seed: int) -> frozenset:
    """A seeded random share of the given names, for a grouped holdout."""
    names = sorted(set(names))
    k = int(round(share * len(names)))
    if not k:
        return frozenset()
    pick = np.random.default_rng(seed).choice(len(names), size=k, replace=False)
    return frozenset(names[i] for i in pick)


def score_tickets(model: ServerModel, tickets: list[dict], directory: ServerDirectory, label_dir: ServerDirectory,
                  batch: int = 200) -> tuple[list[dict], list[dict]]:
    """Score every candidate in the tickets (no sampling) against labels from label_dir. Returns compact
    candidate records and gold-name occurrence records (for candidate recall); no ticket text."""
    know = model.knowledge(directory)
    records, occurrences = [], []
    for i in range(0, len(tickets), batch):
        chunk, cands_all = tickets[i:i + batch], []
        for ti, t in enumerate(chunk, start=i):
            hosts, instances = gold_names(t, label_dir)
            seg = "alerts" if is_alert_ticket(t) else "human"
            cands = ticket_candidates(t, know, alert_context=model.alert_context, max_chars=model.max_chars)
            add_features(cands, know, t)
            for c in cands:
                c["label"], c["seg"], c["ticket"] = label_of(c, hosts, instances), seg, ti
            cands_all += cands
            occurrences += _occurrences(t, cands, hosts, instances, seg, ti, model.max_chars)
        scores = model.scores(cands_all)
        for c, (ph, pi) in zip(cands_all, scores):
            records.append({"label": c["label"], "seg": c["seg"], "ticket": c["ticket"], "ph": float(ph),
                            "pi": float(pi), "kind": c["kind"], "host": c["host"], "instance": c["instance"],
                            "pattern": compress(c["host"]), "start": c["start"], "end": c["end"],
                            "rule": c.get("rule")})
    return records, occurrences


def _occurrences(ticket, cands, hosts, instances, seg, ti, max_chars) -> list[dict]:
    """Each place a gold host or bare instance name occurs in the text, and whether a candidate of the
    right kind covers it. 'own' marks names from the ticket's own dw_servers or alert fields, as
    opposed to names the directory knows for the client, which include email domains, short names and
    words that are not server mentions in that text."""
    text = ticket_text(ticket, max_chars)[0].lower()
    own = ticket_names(ticket)
    own_names = {"host": {p.host for p in own}, "instance": {p.instance for p in own if p.instance}}
    out = []
    for cls, names in (("host", hosts), ("instance", instances)):
        for name in names:
            behind = r"(?<![a-z0-9_\-.\\])" if cls == "instance" else r"(?<![a-z0-9_\-.])"
            for m in re.finditer(behind + re.escape(name) + r"(?![a-z0-9_\-])", text):
                if cls == "instance" and text[m.end():m.end() + 1] == "\\":
                    continue
                if cls == "host":
                    covered = any(c["host_ok"] and c["host"] == name and c["start"] <= m.start() < c["end"]
                                  for c in cands)
                else:
                    covered = any(c["inst_ok"] and c["kind"] == "token" and c["host"] == name
                                  and c["start"] <= m.start() < c["end"] for c in cands)
                out.append({"cls": cls, "name": name, "seg": seg, "ticket": ti, "covered": covered,
                            "own": name in own_names[cls]})
    return out


def average_precision(records: list[dict], cls: str) -> float | None:
    y = np.array([r["label"] == cls for r in records])
    if not y.any():
        return None
    return float(average_precision_score(y, [r["ph" if cls == "host" else "pi"] for r in records]))


def best_f1_threshold(records: list[dict], cls: str) -> float:
    """The model's threshold with the best F1, over candidates no rule marked. Rule matches score
    RULE_SCORE whatever the threshold, so including them would pull the threshold up to that score
    and leave the model nothing to decide."""
    records = [r for r in records if not r.get("rule")]
    y = np.array([r["label"] == cls for r in records])
    if not y.any():
        return DEFAULT_THRESHOLD
    p, r, t = precision_recall_curve(y, [x["ph" if cls == "host" else "pi"] for x in records])
    f1 = 2 * p[:-1] * r[:-1] / np.maximum(p[:-1] + r[:-1], 1e-12)
    return float(np.clip(t[int(np.argmax(f1))], 0.05, RULE_SCORE - 0.05))


def fit_config(train: list[dict], directory: ServerDirectory, *, kind: str, alert_context: str,
               instance_model: bool, seed: int, max_chars: int, neg_ratio: float,
               exclude_hosts=frozenset(), exclude_instances=frozenset(),
               linear_params: dict | None = None) -> tuple[ServerModel, dict]:
    stop = build_stop_words(train, directory)
    model = ServerModel(kind=kind, alert_context=alert_context, instance_model=instance_model, stop=stop,
                        max_chars=max_chars, seed=seed, linear_params=linear_params)
    examples, counts = make_examples(train, model.knowledge(directory), directory, alert_context=alert_context,
                                     max_chars=max_chars, exclude_hosts=exclude_hosts,
                                     exclude_instances=exclude_instances, neg_ratio=neg_ratio, seed=seed)
    model.fit(examples)
    counts["examples"] = len(examples)
    return model, counts


def select_config(train: list[dict], *, seed: int, max_chars: int, neg_ratio: float, val_share: float = 0.2,
                  kinds=MODEL_KINDS, modes=ALERT_CONTEXT_MODES, instances_path: Path | None = INSTANCES_PATH,
                  log=print) -> dict:
    """Fit each model kind and alert-context mode on 80% of the training tickets, score the other 20%,
    and keep the best macro average precision over host and instance. Then try a separate binary
    instance model, and pick thresholds by best F1 on the validation split."""
    fit_t, val_t = split_validation(train, val_share, seed)
    fit_dir = LocalServerDirectory.from_sources(fit_t, instances_path)
    oracle = LocalServerDirectory.from_sources(train, instances_path)
    trials = []
    for mode in modes:
        for kind in kinds:
            start = time.time()
            model, counts = fit_config(fit_t, fit_dir, kind=kind, alert_context=mode, instance_model=False,
                                       seed=seed, max_chars=max_chars, neg_ratio=neg_ratio)
            recs, _ = score_tickets(model, val_t, fit_dir, oracle)
            ap_h, ap_i = average_precision(recs, "host"), average_precision(recs, "instance")
            macro = np.mean([x for x in (ap_h, ap_i) if x is not None]) if (ap_h or ap_i) else 0.0
            trials.append({"kind": kind, "alert_context": mode, "instance_model": False, "ap_host": ap_h,
                           "ap_instance": ap_i, "macro_ap": float(macro), "model": model, "records": recs,
                           "examples": counts["examples"], "seconds": time.time() - start})
            log(f"  {kind:7} {mode:13} AP host {fmt_pct(ap_h)}  AP instance {fmt_pct(ap_i)}  "
                f"({counts['examples']:,} examples, {time.time() - start:.0f}s)")
    best = max(trials, key=lambda t: t["macro_ap"])
    start = time.time()
    model, counts = fit_config(fit_t, fit_dir, kind=best["kind"], alert_context=best["alert_context"],
                               instance_model=True, seed=seed, max_chars=max_chars, neg_ratio=neg_ratio)
    recs, _ = score_tickets(model, val_t, fit_dir, oracle)
    ap_i = average_precision(recs, "instance")
    inst_trial = {"kind": best["kind"], "alert_context": best["alert_context"], "instance_model": True,
                  "ap_host": average_precision(recs, "host"), "ap_instance": ap_i, "records": recs,
                  "examples": counts["examples"], "seconds": time.time() - start}
    inst_trial["macro_ap"] = float(np.mean([x for x in (inst_trial["ap_host"], ap_i) if x is not None] or [0]))
    trials.append(inst_trial)
    log(f"  {best['kind']:7} + binary instance model: AP instance {fmt_pct(ap_i)}")
    chosen = inst_trial if (ap_i or 0) > (best["ap_instance"] or 0) else best
    return {"kind": chosen["kind"], "alert_context": chosen["alert_context"],
            "instance_model": chosen["instance_model"],
            "host_threshold": best_f1_threshold(chosen["records"], "host"),
            "instance_threshold": best_f1_threshold(chosen["records"], "instance"),
            "trials": [{k: v for k, v in t.items() if k not in ("model", "records")} for t in trials],
            "validation_tickets": len(val_t), "fit_tickets": len(fit_t)}


# --- Evaluation ----------------------------------------------------------------

SEGMENTS = ("all", "alerts", "human")
REPORT_THRESHOLDS = (0.3, 0.5, 0.7, 0.9)


def _seg(records: list[dict], seg: str) -> list[dict]:
    return records if seg == "all" else [r for r in records if r["seg"] == seg]


def prf(records: list[dict], cls: str, threshold: float) -> dict:
    key = "ph" if cls == "host" else "pi"
    tp = sum(1 for r in records if r[key] >= threshold and r["label"] == cls)
    pred = sum(1 for r in records if r[key] >= threshold)
    actual = sum(1 for r in records if r["label"] == cls)
    p = tp / pred if pred else None
    rc = tp / actual if actual else None
    f = 2 * p * rc / (p + rc) if p and rc else None
    return {"precision": p, "recall": rc, "f1": f, "actual": actual, "predicted": pred}


def recall_of(occ: list[dict], cls: str, seg: str = "all", names=None, own_only: bool = False) -> tuple[int, int]:
    rows = [o for o in _seg(occ, seg) if o["cls"] == cls and (names is None or o["name"] in names)
            and (o["own"] or not own_only)]
    return sum(o["covered"] for o in rows), len(rows)


def false_positive_shapes(records: list[dict], cls: str, threshold: float, stop: frozenset, n: int = 12):
    key = "ph" if cls == "host" else "pi"
    fps = [r for r in records if r[key] >= threshold and r["label"] != cls]
    shapes = Counter(r["pattern"] for r in fps).most_common(n)
    english = sum(1 for r in fps if r["host"] in stop or r["host"] in BUILTIN_STOP)
    return shapes, len(fps), english


def held_out_recall(records: list[dict], occ: list[dict], *, hosts=None, instances=None, model: ServerModel,
                    shared=None) -> dict:
    """Recall on mentions of held-out names: host candidates whose host is held out, or bare and
    HOST\\INSTANCE candidates whose instance is held out (optionally only names in `shared`)."""
    th_h, th_i = model.host_threshold, model.instance_threshold
    out = {}
    for seg in SEGMENTS:
        rs = _seg(records, seg)
        row = {}
        if hosts is not None:
            gold = [r for r in rs if r["label"] == "host" and r["host"] in hosts]
            row["host"] = (sum(r["ph"] >= th_h for r in gold), len(gold))
            row["host_candidates"] = recall_of(occ, "host", seg, hosts)
        if instances is not None:
            bare = [r for r in rs if r["label"] == "instance" and r["host"] in instances and (shared is None or r["host"] in shared)]
            pair = [r for r in rs if r["label"] == "host" and r["instance"] in instances
                    and (shared is None or r["instance"] in shared)]
            row["bare"] = (sum(r["pi"] >= th_i for r in bare), len(bare))
            row["host_instance"] = (sum(r["ph"] >= th_h for r in pair), len(pair))
            names = instances if shared is None else instances & shared
            row["bare_candidates"] = recall_of(occ, "instance", seg, names)
        out[seg] = row
    return out


def pairing_accuracy(model: ServerModel, tickets: list[dict], directory: ServerDirectory) -> Counter:
    """For bare instances whose true host is known from the same ticket's dw_servers: paired with the
    correct host, ambiguous, unpaired, paired with another host, or not detected as a bare instance."""
    counts = Counter()
    know = model.knowledge(directory)
    for t in tickets:
        truth = defaultdict(set)
        for p in (normalize_reference(v) for v in t.get("dw_servers") or []):
            if p and p.instance:
                truth[p.instance].add(p.host)
        if not truth:
            continue
        cands = ticket_candidates(t, know, alert_context=model.alert_context, max_chars=model.max_chars)
        gold = [c for c in cands if c["inst_ok"] and c["kind"] == "token" and c["host"] in truth]
        if not gold:
            continue
        subject_len = len(t.get("subject") or "")
        dets = {}
        for d in detect_servers(t, directory, model):
            if d["kind"] == "bare_instance":
                start = d["start"] + (0 if d["field"] == "subject" else subject_len + 1)
                dets[start] = d
        for c in gold:
            d = dets.get(c["start"])
            if d is None:
                counts["not detected"] += 1
            elif d["host"] in truth[c["host"]]:
                counts["correct host"] += 1
            elif d["status"] == "ambiguous":
                counts["ambiguous"] += 1
            elif d["host"] is None:
                counts["unpaired"] += 1
            else:
                counts["wrong host"] += 1
    return counts


def evaluate(train: list[dict], test: list[dict], *, seed: int, holdout_share: float, max_chars: int,
             neg_ratio: float, host_threshold: float | None, instance_threshold: float | None,
             instances_path: Path | None = INSTANCES_PATH, log=print) -> dict:
    overlap = {t["ticket_id"] for t in train} & {t["ticket_id"] for t in test}
    if overlap:
        raise SystemExit(f"{len(overlap)} tickets are in both the training and the test file")
    result = {"train_tickets": len(train), "test_tickets": len(test), "seed": seed, "holdout_share": holdout_share,
              "max_chars": max_chars, "neg_ratio": neg_ratio}
    train_dir = LocalServerDirectory.from_sources(train, instances_path)
    oracle = LocalServerDirectory.from_sources(train + test, instances_path)
    result["directory"] = train_dir.stats()
    result["oracle_directory"] = oracle.stats()
    log("Directory (training): " + ", ".join(f"{k} {v:,}" for k, v in result["directory"].items()))

    log("Selecting the model on a validation split of the training file ...")
    cfg = select_config(train, seed=seed, max_chars=max_chars, neg_ratio=neg_ratio, instances_path=instances_path,
                        log=log)
    if host_threshold is not None:
        cfg["host_threshold"] = host_threshold
    if instance_threshold is not None:
        cfg["instance_threshold"] = instance_threshold
    result["config"] = cfg
    common = dict(kind=cfg["kind"], alert_context=cfg["alert_context"], instance_model=cfg["instance_model"],
                  seed=seed, max_chars=max_chars, neg_ratio=neg_ratio)

    def finish(model):
        model.host_threshold, model.instance_threshold = cfg["host_threshold"], cfg["instance_threshold"]
        return model

    log("Standard test: train on the training file, score the test file ...")
    model, counts = fit_config(train, train_dir, **common)
    finish(model)
    result["training_counts"] = counts
    recs, occ = score_tickets(model, test, train_dir, oracle)
    result["standard"] = {"records": recs, "occurrences": occ}
    result["stop_words"] = len(model.stop)
    result["pairing"] = pairing_accuracy(model, test, train_dir)
    result["natural_bare_instances"] = {
        "train_human": counts["before_sampling"].get(("instance", "human"), 0),
        "test_human": sum(1 for r in recs if r["label"] == "instance" and r["seg"] == "human")}
    result["fp"] = {cls: false_positive_shapes(recs, cls, cfg[f"{cls}_threshold"], model.stop)
                    for cls in ("host", "instance")}

    log("Unseen-host test ...")
    held_hosts = holdout_names(oracle.hosts(), holdout_share, seed)
    dir_h = train_dir.without(hosts=held_hosts)
    model_h, _ = fit_config(train, dir_h, exclude_hosts=held_hosts, **common)
    finish(model_h)
    recs_h, occ_h = score_tickets(model_h, test, dir_h, oracle)
    result["unseen_host"] = {"held_out": len(held_hosts),
                             "recall": held_out_recall(recs_h, occ_h, hosts=held_hosts, model=model_h)}

    log("Unseen-instance test ...")
    held_inst = holdout_names(oracle.instance_names(), holdout_share, seed)
    shared = frozenset(n for n, k in oracle.instance_clients().items() if k > 1)
    unique = frozenset(oracle.instance_names()) - shared
    dir_i = train_dir.without(instances=held_inst)
    model_i, _ = fit_config(train, dir_i, exclude_instances=held_inst, **common)
    finish(model_i)
    recs_i, occ_i = score_tickets(model_i, test, dir_i, oracle)
    result["unseen_instance"] = {
        "held_out": len(held_inst), "shared": len(held_inst & shared), "unique": len(held_inst & unique),
        "all": held_out_recall(recs_i, occ_i, instances=held_inst, model=model_i),
        "shared_names": held_out_recall(recs_i, occ_i, instances=held_inst, shared=shared, model=model_i),
        "unique_names": held_out_recall(recs_i, occ_i, instances=held_inst, shared=unique, model=model_i)}
    return result


# --- Report --------------------------------------------------------------------


def _frac(pair: tuple[int, int]) -> str:
    k, n = pair
    return f"{fmt_pct(k / n) if n else 'n/a'} ({k:,}/{n:,})"


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def render_report(r: dict, args) -> str:
    cfg = r["config"]
    recs, occ = r["standard"]["records"], r["standard"]["occurrences"]
    d = r["directory"]
    lines = [
        "# Server and instance detector",
        "",
        f"Generated {now_iso()}. Training file {args.train.name} ({r['train_tickets']:,} tickets), test file "
        f"{args.test.name} ({r['test_tickets']:,} tickets). Seed {r['seed']}, holdout share "
        f"{r['holdout_share']:.0%}, first {r['max_chars']:,} description characters per ticket.",
        "",
        "## Known-names directory",
        "",
        md_table(["measure", "training directory", "training + test (label oracle)"],
                 [[k.replace("_", " "), f"{d[k]:,}", f"{r['oracle_directory'][k]:,}"] for k in d]),
        "",
        "The detector's features read only the training directory. Labels for the test file use the oracle, "
        "which adds the test tickets' own dw_servers and alert fields.",
        "",
        "## Model selection (validation split of the training file)",
        "",
        f"{cfg['fit_tickets']:,} training tickets fit each option and {cfg['validation_tickets']:,} score it. The "
        "option with the best macro average precision over host and instance is kept; a separate binary "
        "instance model replaces the multiclass instance score only when its instance AP is higher.",
        "",
        md_table(["model", "alert context", "binary instance model", "AP host", "AP instance", "macro AP",
                  "examples", "seconds"],
                 [[t["kind"], t["alert_context"], "yes" if t["instance_model"] else "no", fmt_pct(t["ap_host"]),
                   fmt_pct(t["ap_instance"]), fmt_pct(t["macro_ap"]), f"{t['examples']:,}", f"{t['seconds']:.0f}"]
                  for t in cfg["trials"]]),
        "",
        f"Chosen: {cfg['kind']}, alert context {cfg['alert_context']}, binary instance model "
        f"{'yes' if cfg['instance_model'] else 'no'}. Thresholds: host {cfg['host_threshold']:.2f}, instance "
        f"{cfg['instance_threshold']:.2f} (best F1 on the validation split unless set on the command line).",
        "",
        "## Training examples",
        "",
    ]
    before = r["training_counts"]["before_sampling"]
    lines += [md_table(["label", "source", "candidates"],
                       [[lab, src, f"{n:,}"] for (lab, src), n in sorted(before.items())]), "",
              f"'none' candidates were sampled down to {r['neg_ratio']:g} per positive "
              f"({r['training_counts']['none_kept']:,} kept); no class weights. Synthetic bare instances "
              f"(instance part of a HOST\\INSTANCE mention, host removed) added: "
              f"{before.get(('instance', 'synthetic'), 0):,}; they are used in training only. Client features were "
              f"hidden for {r['training_counts']['client_hidden_tickets']:,} training tickets (client dropout "
              f"{DEFAULT_CLIENT_DROPOUT:.0%}). Stop list: "
              f"{r['stop_words']:,} words.", "",
              "## Candidate generation recall (test file)", "",
              "Share of places a labeled host or bare instance name occurs in the text that became a candidate "
              "of the right kind. 'Own names' are the ticket's own dw_servers and alert fields. 'With directory "
              "names' adds every name the directory knows for the client, which includes email domains, "
              "two-character names and ordinary words that are not server mentions where they occur, so it "
              "understates recall.", "",
              md_table(["class", "names"] + list(SEGMENTS),
                       [[cls, label] + [_frac(recall_of(occ, cls, seg, own_only=own)) for seg in SEGMENTS]
                        for cls in ("host", "instance") for label, own in (("own names", True),
                                                                           ("with directory names", False))]),
              "", "## Standard test", ""]
    for cls in ("host", "instance"):
        th = cfg[f"{cls}_threshold"]
        rows = []
        for seg in SEGMENTS:
            rs = _seg(recs, seg)
            ap = average_precision(rs, cls)
            for t in sorted(set(REPORT_THRESHOLDS) | {round(th, 2)}):
                m = prf(rs, cls, t)
                rows.append([seg, f"{t:.2f}" + (" (chosen)" if abs(t - th) < 1e-9 or t == round(th, 2) else ""),
                             fmt_pct(m["precision"]), fmt_pct(m["recall"]), fmt_pct(m["f1"]), f"{m['actual']:,}",
                             f"{m['predicted']:,}", fmt_pct(ap)])
        lines += [f"### {cls}", "", md_table(["segment", "threshold", "precision", "recall", "F1", "labeled",
                                               "predicted", "AP"], rows), ""]
    rule_rows = []
    for rule, n in Counter(r["rule"] for r in recs if r.get("rule")).most_common():
        cls = rule.split(":")[0]
        fired = [r for r in recs if r.get("rule") == rule]
        agree = sum(1 for r in fired if r["label"] == cls)
        rule_rows.append([rule, f"{n:,}", f"{agree:,}", fmt_pct(agree / n),
                          f"{sum(1 for r in fired if r['seg'] == 'alerts'):,}", f"{sum(1 for r in fired if r['seg'] == 'human'):,}"])
    lines += ["### Rules", "",
              "Candidates a rule marked (scored at least " f"{RULE_SCORE:.2f}" ", so detected at the chosen thresholds). "
              "'Agree' counts those whose derived label is the rule's class. Known-name rules use the training "
              "directory, which is also where human-ticket labels come from, so their agreement is high by "
              "construction; the held-out tests below are the honest check.", "",
              md_table(["rule", "fired", "agree", "agree share", "alerts", "human"], rule_rows) if rule_rows else "No rule fired.",
              ""]
    lines += ["Precision and recall are per candidate, one class against the rest. Labels are derived, not hand "
              "labeled: a correct detection of a name the labels do not know counts as a false positive.", ""]
    for cls in ("host", "instance"):
        shapes, n, english = r["fp"][cls]
        lines += [f"### Most common false-positive shapes: {cls}", "",
                  f"{n:,} false positives at the chosen threshold"
                  + (f"; {english:,} of them are common English or support words (stop list)." if cls == "instance"
                     else "."), "",
                  md_table(["shape", "count"], [[s, f"{k:,}"] for s, k in shapes]) if shapes else "None.", ""]
    uh = r["unseen_host"]
    lines += ["## Unseen-host test", "",
              f"{uh['held_out']:,} hosts held out (removed from the directory and from training). Recall on their "
              "mentions in the test file, at the chosen threshold, and candidate recall:", "",
              md_table(["segment", "detected as host", "became a candidate"],
                       [[seg, _frac(uh["recall"][seg]["host"]), _frac(uh["recall"][seg]["host_candidates"])]
                        for seg in SEGMENTS]), ""]
    ui = r["unseen_instance"]
    lines += ["## Unseen-instance test", "",
              f"{ui['held_out']:,} instance names held out ({ui['unique']:,} used by one client, {ui['shared']:,} "
              "shared across clients), with every host pair that uses them. Recall on their test mentions:", ""]
    rows = []
    for label, key in (("all held-out names", "all"), ("unique to one client", "unique_names"),
                       ("shared across clients", "shared_names")):
        for seg in SEGMENTS:
            x = ui[key][seg]
            rows.append([label, seg, _frac(x["bare"]), _frac(x["bare_candidates"]), _frac(x["host_instance"])])
    lines += [md_table(["names", "segment", "bare detected as instance", "bare became a candidate",
                        "HOST\\INSTANCE detected"], rows), ""]
    pairing = r["pairing"]
    total = sum(pairing.values())
    lines += ["## Bare-instance pairing (test file)", "",
              "Bare instance mentions whose true host is known from the same ticket's dw_servers.", "",
              md_table(["outcome", "mentions", "share"],
                       [[k, f"{v:,}", fmt_pct(v / total if total else None)] for k, v in pairing.most_common()])
              if total else "No such mentions in the test file.", ""]
    nb = r["natural_bare_instances"]
    lines += ["## Open questions", "",
              f"- Natural bare-instance mentions in human tickets: {nb['train_human']:,} in training, "
              f"{nb['test_human']:,} in the test file."
              + (" Fewer than about 50 in the test file is too few to evaluate on; a hand-labeled sample is "
                 "recommended." if nb["test_human"] < 50 else ""),
              "- How the CMDB API identifies a client (Zendesk organization id, Keystone id or name) and whether it "
              "returns instances as HOST\\INSTANCE or as separate fields.",
              "- Whether the default instance (MSSQLSERVER) should be stored as a host-level record, as "
              "match_mention() assumes and this detector does today.",
              "- Whether database names should be a separate kind; for now they are 'none' unless they are also "
              "host or instance names.", ""]
    return "\n".join(lines)


def print_summary(r: dict) -> None:
    cfg = r["config"]
    recs, occ = r["standard"]["records"], r["standard"]["occurrences"]
    print(f"Chosen: {cfg['kind']}, alert context {cfg['alert_context']}, binary instance model "
          f"{'yes' if cfg['instance_model'] else 'no'}; thresholds host {cfg['host_threshold']:.2f}, "
          f"instance {cfg['instance_threshold']:.2f}")
    print(f"  {'':22}{'alerts':>24}{'human':>24}")
    for cls in ("host", "instance"):
        th = cfg[f"{cls}_threshold"]
        cand = [_frac(recall_of(occ, cls, seg, own_only=True)) for seg in ("alerts", "human")]
        print(f"  {cls + ' cand. recall (own)':22}{cand[0]:>24}{cand[1]:>24}")
        m = [prf(_seg(recs, seg), cls, th) for seg in ("alerts", "human")]
        print(f"  {cls + ' precision':22}{fmt_pct(m[0]['precision']):>24}{fmt_pct(m[1]['precision']):>24}")
        print(f"  {cls + ' recall':22}{fmt_pct(m[0]['recall']):>24}{fmt_pct(m[1]['recall']):>24}")
    uh = r["unseen_host"]["recall"]
    print(f"  {'held-out host recall':22}{_frac(uh['alerts']['host']):>24}{_frac(uh['human']['host']):>24}")
    ui = r["unseen_instance"]["all"]
    print(f"  {'held-out bare inst.':22}{_frac(ui['alerts']['bare']):>24}{_frac(ui['human']['bare']):>24}")
    print(f"  {'held-out HOST\\INST':22}{_frac(ui['alerts']['host_instance']):>24}"
          f"{_frac(ui['human']['host_instance']):>24}")
    pairing = r["pairing"]
    print("  bare-instance pairing: " + (", ".join(f"{k} {v:,}" for k, v in pairing.most_common()) or "no mentions"))


# --- CLI -----------------------------------------------------------------------


def parse_args(argv=None):
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--train", type=Path, default=DATA_DIR / "train_tickets.jsonl")
    shared.add_argument("--test", type=Path, default=DATA_DIR / "test_tickets.jsonl")
    shared.add_argument("--host-threshold", type=float, help="default: best F1 on a validation split")
    shared.add_argument("--instance-threshold", type=float, help="default: best F1 on a validation split")
    shared.add_argument("--seed", type=int, default=0)
    shared.add_argument("--holdout-share", type=float, default=0.2, help="share of hosts or instance names held out")
    shared.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="description characters read")
    shared.add_argument("--neg-ratio", type=float, default=DEFAULT_NEG_RATIO,
                        help="'none' training examples kept per positive")
    shared.add_argument("--model-path", type=Path, default=MODEL_PATH)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("train", parents=[shared], help="fit on --train and save the model")
    sub.add_parser("evaluate", parents=[shared], help="run all tests and write a report")
    d = sub.add_parser("detect", parents=[shared], help="detect servers in a ticket file")
    d.add_argument("--tickets", type=Path, default=DATA_DIR / "test_tickets.jsonl")
    d.add_argument("--output", type=Path, help="default data/<tickets>-servers.jsonl")
    d.add_argument("--write", action="store_true",
                   help="write a copy of each ticket with detected_servers added, instead of id + detections rows")
    return p.parse_args(argv)


def cmd_train(args) -> None:
    train = read_jsonl(args.train)
    if not train:
        raise SystemExit(f"Missing or empty ticket file: {args.train}")
    print("Selecting the model on a validation split ...")
    cfg = select_config(train, seed=args.seed, max_chars=args.max_chars, neg_ratio=args.neg_ratio)
    directory = LocalServerDirectory.from_sources(train)
    model, counts = fit_config(train, directory, kind=cfg["kind"], alert_context=cfg["alert_context"],
                               instance_model=cfg["instance_model"], seed=args.seed, max_chars=args.max_chars,
                               neg_ratio=args.neg_ratio)
    model.host_threshold = args.host_threshold if args.host_threshold is not None else cfg["host_threshold"]
    model.instance_threshold = (args.instance_threshold if args.instance_threshold is not None
                                else cfg["instance_threshold"])
    args.model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "directory": directory, "trained_on": args.train.name, "created_at": now_iso(),
                 "config": {k: v for k, v in cfg.items() if k != "trials"}}, args.model_path)
    print(f"Trained {model.kind} ({counts['examples']:,} examples); thresholds host {model.host_threshold:.2f}, "
          f"instance {model.instance_threshold:.2f}. Saved to {args.model_path}")


def cmd_evaluate(args) -> None:
    train, test = read_jsonl(args.train), read_jsonl(args.test)
    if not train or not test:
        raise SystemExit(f"Missing or empty ticket file: {args.train if not train else args.test}")
    r = evaluate(train, test, seed=args.seed, holdout_share=args.holdout_share, max_chars=args.max_chars,
                 neg_ratio=args.neg_ratio, host_threshold=args.host_threshold,
                 instance_threshold=args.instance_threshold)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"server-detector-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(render_report(r, args), encoding="utf-8")
    print_summary(r)
    print(f"Report: {path}")


def cmd_detect(args) -> None:
    if not args.model_path.exists():
        raise SystemExit(f"No model at {args.model_path}. Run: python server_detector.py train")
    bundle = joblib.load(args.model_path)
    tickets = read_jsonl(args.tickets)
    if not tickets:
        raise SystemExit(f"Missing or empty ticket file: {args.tickets}")
    output = args.output or args.tickets.with_name(f"{args.tickets.stem}-servers.jsonl")
    if output.resolve() == args.tickets.resolve():
        raise SystemExit("The output must be a new file; the source ticket file is never changed")
    rows, kinds, statuses = [], Counter(), Counter()
    for t in tickets:
        found = detect_servers(t, bundle["directory"], bundle["model"], host_threshold=args.host_threshold,
                               instance_threshold=args.instance_threshold)
        kinds.update(d["kind"] for d in found)
        statuses.update(d["status"] for d in found)
        rows.append({**t, "detected_servers": found} if args.write else
                    {"ticket_id": t["ticket_id"], "detected_servers": found})
    write_jsonl(output, rows)
    print(f"{len(tickets):,} tickets, {sum(kinds.values()):,} detections")
    print("  kinds: " + ", ".join(f"{k} {v:,}" for k, v in kinds.most_common()))
    print("  statuses: " + ", ".join(f"{k} {v:,}" for k, v in statuses.most_common()))
    print(f"Wrote {output}")


def main(argv=None):
    args = parse_args(argv)
    {"train": cmd_train, "evaluate": cmd_evaluate, "detect": cmd_detect}[args.command](args)


if __name__ == "__main__":
    # Run through the imported module, so a saved model refers to server_detector.ServerModel rather
    # than __main__.ServerModel, which other programs (detector_ui.py) could not load.
    import server_detector
    server_detector.main()
