"""Find SQL Server hosts and named instances in free ticket text without an LLM.

Splits the subject and description into tokens, keeps generous host and
instance candidates, scores each one as host, instance or none with a model
trained on labels derived from alert fields, DW2.0 servers and a known-names
directory, then pairs bare instance names with a host and assigns a status
(known, new instance on a known host, possible new host or instance, ambiguous,
typo of a known name).

Training, evaluation and the commands are in tools/server_detector.py.
Nothing leaves this machine; the CMDB API is a stub.
"""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Protocol

import joblib
import numpy as np
from scipy.sparse import csr_matrix, hstack
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction import DictVectorizer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import MaxAbsScaler

from .alert_parser import COLLECTOR_LABELS, FIRING_LABELS, SERVER_FIELDS
from .names import ParsedName, parse_server_name
from .paths import DATA_DIR

MODEL_PATH = DATA_DIR / "models" / "server_detector.joblib"
DEFAULT_MAX_CHARS = 20_000  # description characters read per ticket
DEFAULT_THRESHOLD = 0.5  # used only when no validation split is available
KNOWLEDGE_CACHE_LIMIT = 100_000  # per-name feature results kept per directory; cleared when full
TOP_PATTERNS = 60  # compressed patterns kept as one-hot features; the rest are "other"
DEFAULT_INSTANCE = "mssqlserver"  # stored as a host-level record, as match_mention() assumes

# --- Text patterns -----------------------------------------------------------

# Whitespace and punctuation split tokens; \ . - _ , : and $ stay inside one, so HOST\INST,
# db01.corp.local, host,1433, tcp:host and MSSQL$INST survive as single tokens.
TOKEN_RE = re.compile(r"[A-Za-z0-9_\\.\-,:$#]+")
SENTENCE_RE = re.compile(r"[.!?](?=\s)|\n")
# The value ends at ; or a quote, as in a connection string, or at whitespace, since server names have
# none: "Server=sql01,1433 is down" in prose marks sql01,1433 only, not the rest of the line. It also
# ends at a parenthesis, so an Oracle TNS descriptor's ADDRESS=(PROTOCOL=TCP)(HOST=...) is not one value.
CONN_RE = re.compile(r"(?i)\b(?:server|data\s+source|address|addr|network\s+address)\s*=\s*([^;'\"\s()]+)")
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
# A confirmed name of the ticket's client (see LocalServerDirectory) found by the dictionary pass scores
# this, so it is detected whatever the model and the thresholds say.
DICTIONARY_SCORE = 1.0
# Name sources the dictionary pass trusts: parsed alert fields and the CMDB export. dw_servers holds
# time-logging entries, including words and abbreviations that are not servers.
CONFIRMED_SOURCES = frozenset({"alert", "cmdb"})
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
    # One name each; "HOST, 1433" is read as HOST,1433
    values += [re.sub(r",\s+(?=\d+$)", ",", alert[k].strip()) for k, is_list in SERVER_FIELDS.items()
               if not is_list and alert.get(k)]
    # Lists: "Host(s):", the backup report's "latest backup server" column, Grafana's instances and targets
    values += re.split(r"[,;\s]+", " ".join(alert.get(k) or "" for k, is_list in SERVER_FIELDS.items() if is_list))
    names = [p for p in (normalize_reference(v) for v in values if v) if p]
    return list(dict.fromkeys(names))


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

    def confirmed_hosts(self, client_key: str | None = None) -> list[str]: ...  # hosts from alerts or the CMDB

    def confirmed_instances(self, client_key: str | None = None) -> list[tuple[str, str]]: ...  # pairs, same sources

    def environment(self, host: str, client_key: str | None) -> str | None: ...  # most often given environment


def alert_environment(ticket: dict) -> str | None:
    """The environment an alert names: the collector table's Environment Name or Grafana's env label."""
    fields = ticket.get("alert_fields") or {}
    value = (fields.get("environment") or fields.get("env") or "").strip()
    return value or None


def ticket_client_keys(ticket: dict) -> tuple[str | None, str | None]:
    org = str(ticket.get("organization_id") or "").strip() or None
    name = (ticket.get("organization_name") or "").strip().lower()
    return org, f"name:{name}" if name else None


class LocalServerDirectory:
    """Known names per client: server-shaped alert fields, dw_servers and CMDB records. Training builds
    one from the local files (tools.directory_build); the API restores one from a snapshot (from_dict).
    Each client is keyed by its Zendesk organization id, or by its name when it has none; a name seen
    with an id is an alias for that id. Names from alert fields and the CMDB export are also kept as
    confirmed (CONFIRMED_SOURCES), with the environment those sources give."""

    def __init__(self):
        self._hosts: dict[str, set[str]] = defaultdict(set)
        self._pairs: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._confirmed_hosts: dict[str, set[str]] = defaultdict(set)
        self._confirmed_pairs: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._env: dict[tuple[str, str], Counter] = defaultdict(Counter)  # (client, host) -> environments
        self._alias: dict[str, str] = {}
        self.skipped = 0  # references that were not server-shaped

    def add(self, org: str | None, name_key: str | None, parsed: ParsedName, source: str = "dw",
            environment: str | None = None) -> None:
        key = org or name_key
        if not key:
            return
        if org and name_key:
            self._alias.setdefault(name_key, org)
        self._hosts[key].add(parsed.host)
        if parsed.instance:
            self._pairs[key].add((parsed.host, parsed.instance))
        if source in CONFIRMED_SOURCES:
            self._confirmed_hosts[key].add(parsed.host)
            if parsed.instance:
                self._confirmed_pairs[key].add((parsed.host, parsed.instance))
            if environment:
                self._env[(key, parsed.host)][environment] += 1

    def add_cmdb_record(self, org, client_name, server_name, environment=None) -> tuple[str, ParsedName] | None:
        """One CMDB server record (a row of data/instances.csv). Returns the client key and the parsed
        name, or None when the name is not server-shaped (counted in skipped) or no client is given."""
        parsed = normalize_reference(server_name)
        if not parsed:
            self.skipped += 1
            return None
        org = str(org or "").strip() or None
        name = (client_name or "").strip().lower()
        name_key = f"name:{name}" if name else None
        self.add(org, name_key, parsed, "cmdb", (environment or "").strip() or None)
        return (org or name_key, parsed) if org or name_key else None

    def to_dict(self) -> dict:
        """Every name as stored, per client key, for a directory snapshot: hosts with whether a confirmed
        source named them and the environments those sources gave (with counts), (host, instance) pairs
        with the same flag, and the name-to-organization aliases. from_dict() restores an identical copy."""
        keys = sorted(set(self._hosts) | set(self._pairs))
        clients = {}
        for key in keys:
            hosts = [{"host": h, "confirmed": h in self._confirmed_hosts.get(key, ()),
                      "environments": dict(sorted(self._env.get((key, h), {}).items()))}
                     for h in sorted(self._hosts.get(key, ()))]
            pairs = [{"host": h, "instance": i, "confirmed": (h, i) in self._confirmed_pairs.get(key, ())}
                     for h, i in sorted(self._pairs.get(key, ()))]
            clients[key] = {"hosts": hosts, "instances": pairs}
        return {"clients": clients, "aliases": dict(sorted(self._alias.items()))}

    @classmethod
    def from_dict(cls, data: dict) -> "LocalServerDirectory":
        d = cls()
        for key, entry in (data.get("clients") or {}).items():
            for r in entry.get("hosts") or ():
                d._hosts[key].add(r["host"])
                if r.get("confirmed"):
                    d._confirmed_hosts[key].add(r["host"])
                if r.get("environments"):
                    d._env[(key, r["host"])].update(r["environments"])
            for r in entry.get("instances") or ():
                pair = (r["host"], r["instance"])
                d._hosts[key].add(r["host"])
                d._pairs[key].add(pair)
                if r.get("confirmed"):
                    d._confirmed_pairs[key].add(pair)
                    d._confirmed_hosts[key].add(r["host"])
        d._alias = dict(data.get("aliases") or {})
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

    def confirmed_hosts(self, client_key: str | None = None) -> list[str]:
        return sorted({h for k in self._keys(client_key) for h in self._confirmed_hosts.get(k, ())})

    def confirmed_instances(self, client_key: str | None = None) -> list[tuple[str, str]]:
        return sorted({p for k in self._keys(client_key) for p in self._confirmed_pairs.get(k, ())})

    def environment(self, host: str, client_key: str | None) -> str | None:
        """The environment alerts or the CMDB give most often for the host (ties: alphabetical)."""
        envs = Counter()
        for k in self._keys(client_key):
            envs.update(self._env.get((k, host), {}))
        return min(envs, key=lambda e: (-envs[e], e)) if envs else None

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
                "confirmed_hosts": len({h for hs in self._confirmed_hosts.values() for h in hs}),
                "confirmed_pairs": len({p for ps in self._confirmed_pairs.values() for p in ps}),
                "hosts_with_environment": len({h for _, h in self._env}),
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
    confirmed_hosts = confirmed_instances = environment = _todo


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
EMPTY_DICTIONARY = (frozenset(), frozenset(), frozenset())


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
        # The dictionary pass: each client's confirmed hosts, (host, instance) pairs and instance names
        self.confirmed: dict[str, tuple[frozenset, frozenset, frozenset]] = {}
        # Each client's word-like hosts (letters only) known from dw_servers alone; against hand labels
        # none of their detections were servers, so they are not host candidates without a strong context
        self.unconfirmed_words: dict[str, frozenset] = {}
        for client in directory.clients():
            hosts = frozenset(directory.confirmed_hosts(client))
            pairs = frozenset(directory.confirmed_instances(client))
            self.confirmed[client] = (hosts, pairs, frozenset(i for _, i in pairs))
            self.unconfirmed_words[client] = frozenset(h for h in set(directory.hosts(client)) - hosts
                                                       if h.isalpha())
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
        if key in self._cache:
            return self._cache[key]
        relation, known = near_match(name, self.profile(client, "host").names)
        return self._remember(key, known if relation in ("typo", "sibling") else None)

    def dictionary(self, client: str | None) -> tuple[frozenset, frozenset, frozenset]:
        """(hosts, (host, instance) pairs, instance names) confirmed for the client; empty when unknown."""
        return self.confirmed.get(client, EMPTY_DICTIONARY) if client else EMPTY_DICTIONARY

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
        return self._remember(key, out)

    def _remember(self, key, value):
        """Cache a per-name result. The cache is cleared when full: the API keeps one Knowledge per
        directory for days, and every new token in caller text would otherwise add an entry."""
        if len(self._cache) >= KNOWLEDGE_CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = value
        return value


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
        m = re.fullmatch(r"([^:]+):(\d{2,5}):([A-Za-z][A-Za-z0-9_$#]*)", raw)  # JDBC thin @host:1521:SID
        if m:
            return [(start, start + len(m.group(1)), frozenset({"port"})),
                    (start + m.start(3), start + len(raw), frozenset({"oracle"}))]
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
    oracle_ticket = bool(ORACLE_RE.search(text))
    client = know.directory.client_for(ticket)
    # The servers an alert names in its own parsed fields (Server, Host(s), ServerName and the like)
    alert_hosts = frozenset(p.host for p in ticket_names({"alert_fields": ticket.get("alert_fields")})) \
        if alert else frozenset()
    cands = []
    for ti, (raw, s0, _) in enumerate(tokens):
        parts = token_parts(raw, s0)
        if not parts:
            continue
        window = [] if hide else words[max(0, ti - 3):ti] + words[ti + 1:ti + 4]
        if strip:
            window = [w for w in window if w not in LABEL_WORDS]
        window_cue = any(w in INSTANCE_CUES or w.startswith(SERVICE_PREFIXES) for w in window)
        host_cue = _after_host_word(*_previous_words(words, ti))
        for s, e, flags in parts:
            c = _candidate(text, s, e, flags, know, window_cue, conn, client, host_cue, oracle_ticket)
            if not c:
                continue
            c.update(ti=ti, sent=bisect.bisect_left(breaks, s), ctx=" ".join(window))
            c["rule"] = "host:alert_field" if c["host_ok"] and c["host"] in alert_hosts else \
                _rule(c, words, ti, know, client)
            cands.append(c)
    _mark_host_nearby(cands)
    return cands


def _candidate(text: str, s: int, e: int, flags: frozenset, know: Knowledge, window_cue: bool, conn,
               client: str | None = None, host_cue: bool = False, oracle_ticket: bool = False) -> dict | None:
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
            "is_ip": bool(IPV4_RE.match(host)), "db_port": _db_port(text, s, e, part)}
    if "\\" in part:
        if not (host_name_ok(host, 2) and instance and INSTANCE_RE.match(instance)):
            return None
        return {**base, "kind": "host_instance", "host": host, "instance": instance, "name": f"{host}\\{instance}",
                "host_ok": True, "inst_ok": False, "cue": window_cue}
    if _oracle_name(text, s, e, flags, know, oracle_ticket):
        # An Oracle SID or service name (a service name may be dotted): an instance, never a host
        return {**base, "kind": "token", "host": host, "instance": None, "name": host,
                "host_ok": False, "inst_ok": True, "cue": True, "oracle": True}
    head = part.split(",")[0]
    dotted = "." in head and not base["is_ip"]
    is_file = dotted and head.rsplit(".", 1)[-1].lower() in FILE_EXTS
    # user@host:1521 (EZConnect, JDBC) is a host with a port, not an email address
    email = (text[e:e + 1] == "@" or text[max(0, s - 1):s] == "@") and "port" not in flags
    strong = in_conn or in_unc or base["has_port"]
    # A confirmed host of the client stays a candidate even when it is a frequent word, if it has a digit
    # or punctuation or follows a server word (so prose mentions of a word-like name do not become noisy
    # training labels); a word-like host known only from dw_servers needs a strong context
    confirmed = host in know.dictionary(client)[0] and (not host.isalpha() or host_cue)
    unconfirmed_word = bool(client) and host in know.unconfirmed_words.get(client, ())
    host_ok = (host_name_ok(host) and not is_file and not email and host != "localhost"
               and (strong or confirmed or (host not in know.stop and not unconfirmed_word)))
    cue = window_cue or base["service"] or (text[max(0, s - 1):s] == "\\" and not in_unc)
    inst_ok = (not dotted and not base["has_port"] and not in_unc and not email and bool(BARE_INSTANCE_RE.match(part))
               and bool(re.search(r"[A-Za-z]", part))
               and (part.lower() in know.known_instances or cue or part == part.upper()))
    if not (host_ok or inst_ok):
        return None
    return {**base, "kind": "token", "host": host, "instance": None, "name": host,
            "host_ok": host_ok, "inst_ok": inst_ok, "cue": cue}


# Oracle SIDs and service names. Oracle syntax always counts: SID= or SID:, SERVICE_NAME, ORACLE_SID,
# GLOBAL_NAME, DB_UNIQUE_NAME, a TNS (SID = X), EZConnect host:1521/SERVICE and JDBC @host:1521:SID.
# Prose ("SID ORCL", "service name PRODCDB", "PDB SALESPDB", "the ORCLPRD database") counts only in a
# ticket that mentions Oracle, for an upper-case name that is not a frequent word.
ORACLE_RE = re.compile(r"(?i)oracle")
ORACLE_KEY_RE = re.compile(r"(?i)(?:\b(?:oracle_sid|sid|service_name|global_name|db_unique_name)\s*[=:]\s*\(?\s*"
                           r"|:(?:1521|1522|1526|2484)/)$")
ORACLE_BEFORE_RE = re.compile(r"(?i)\b(?:sid|service\s+name|[pc]db|pluggable\s+database|container\s+database"
                              r"|database|db)\s+$")
ORACLE_AFTER_RE = re.compile(r"(?i)^\s+(?:database|db|[pc]db|sid)\b")
ORACLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,29}(?:\.[A-Za-z0-9_\-]+){0,5}$")
NOT_ORACLE = frozenset({"oracle", "mssqlserver", "sqlserveragent", "sql", "mysql", "rac", "asm", "rman", "dba",
                        "tns", "sid", "pdb", "cdb", "db", "none", "null", "unknown"})


def _oracle_name(text: str, s: int, e: int, flags: frozenset, know: Knowledge, oracle_ticket: bool) -> bool:
    """True when text[s:e] is written as an Oracle SID or service name (see ORACLE_KEY_RE)."""
    part = text[s:e]
    if not ORACLE_NAME_RE.match(part) or part.lower() in NOT_ORACLE or SERVICE_RE.match(part):
        return False
    if "oracle" in flags:
        return True
    before = text[text.rfind("\n", 0, s) + 1:s]
    if ORACLE_KEY_RE.search(before):
        return True
    end = text.find("\n", e)
    after = text[e:] if end < 0 else text[e:end]
    return (oracle_ticket and part == part.upper() and len(part) >= 3 and part.lower() not in know.stop
            and bool(ORACLE_BEFORE_RE.search(before) or ORACLE_AFTER_RE.match(after)))


# Colon ports that mark a database server: SQL Server, MySQL and MariaDB, PostgreSQL, Oracle, MongoDB,
# and the Prometheus exporters that run on database hosts (mysqld, postgres, sql_exporter, oracledb)
DB_PORTS = frozenset({"1433", "1434", "3306", "33060", "5432", "1521", "1522", "27017",
                      "9104", "9187", "9399", "9161"})


def _db_port(text: str, s: int, e: int, part: str) -> bool:
    """True for a port written the SQL Server way (HOST,PORT or tcp:HOST) or a colon port in DB_PORTS.
    Other colon ports (host:8080, site:443) are web addresses."""
    if "," in part or text[max(0, s - 4):s].lower() == "tcp:":
        return True
    m = re.match(r":(\d{2,5})(?!\d)", text[e:e + 6])
    return bool(m) and m.group(1) in DB_PORTS


def _previous_words(words: list[str], ti: int) -> tuple[str, str]:
    """The two words before token ti; 'Host(s): NAME' and "server's NAME" split off an 's', which is skipped."""
    prev1 = words[ti - 1] if ti >= 1 else ""
    prev2 = words[ti - 2] if ti >= 2 else ""
    if prev1 == "s" and prev2 in HOST_CUE_WORDS:
        prev1, prev2 = prev2, words[ti - 3] if ti >= 3 else ""
    return prev1, prev2


def _after_host_word(prev1: str, prev2: str) -> bool:
    """True after server, host, vm and the like, or after 'server on'."""
    return prev1 in HOST_CUE_WORDS or (prev1 == "on" and prev2 in HOST_CUE_WORDS)


def _rule(c: dict, words: list[str], ti: int, know: Knowledge, client: str | None = None) -> str | None:
    """The name of the rule that marks this candidate as a host or an instance, or None. Instance
    rules: an Oracle SID or service name (see _oracle_name), an MSSQL$ or SQLAgent$ service suffix, '<name> instance', 'instance <name>' and 'instance
    named <name>'. Host rules: HOST\\INSTANCE whose host has a digit, is known or is in a connection
    string; a connection-string value; a UNC host; a name with a port or tcp:; and a name with a
    digit after server, host, vm and the like, or after 'instance on'. Alert template lines
    ('Instance SQL01') are left to the model.

    The dictionary pass comes first and scores DICTIONARY_SCORE: a confirmed host of the ticket's
    client (named in its alerts or the CMDB export), written alone or as HOST\\INSTANCE; a host made of
    letters only also needs a server word before it (against hand labels, 39% of letter-only alert
    hosts were servers where they occurred, against 93% of the others). A confirmed instance name of
    the client counts only next to an instance cue (before this rule, detections of such names were
    0 of 12 correct against hand labels).

    Other known names of the client, as a backup to the model: a known host with a digit (the
    directory holds digit-free abbreviations and words from dw_servers; against hand labels those
    matched correctly 16% of the time, against 75% with a digit); a near known host (one edit with
    the same pattern, or only trailing digits differ; 5 or more characters with a digit); and a known
    instance name written in upper case (instance names are often ordinary words, so lower case
    'reports' in running text does not count; dropping this rule halved correct bare-instance pairing
    on the test file). A port counts only when written the SQL Server way."""
    prev1, prev2 = _previous_words(words, ti)
    next1 = words[ti + 1] if ti + 1 < len(words) else ""
    name = c["host"]
    has_digit = any(ch.isdigit() for ch in name)
    dict_hosts, _, dict_instances = know.dictionary(client)
    host_cue = _after_host_word(prev1, prev2)
    if c["host_ok"] and name in dict_hosts and (c["kind"] == "host_instance" or not name.isalpha() or host_cue):
        return "host:dictionary"
    own_hosts = know.profile(client, "host").names
    if c["kind"] == "token" and c["host_ok"] and has_digit and name in own_hosts:
        return "host:known_name"
    if c.get("oracle"):
        return "instance:oracle"
    if c["inst_ok"] and c["kind"] == "token" and name not in FILLER_WORDS:
        if name in dict_instances and c["cue"] and not c["after_label"]:
            return "instance:dictionary"
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
    if c["has_port"] and c["db_port"]:
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
        know = self._know.get(key)
        if know is None or know.directory is not directory:
            know = Knowledge(directory, self.stop)
            self._know[key] = know
        return know  # not self._know[key]: forget_knowledge() on the refresh thread may pop it meanwhile

    def forget_knowledge(self, keep=()) -> None:
        """Drop the cached Knowledge of every directory not in keep, so a service that swaps in new
        directories does not hold on to the old ones."""
        kept = {id(d) for d in keep}
        for key in list(self._know):  # a copy: inference threads may add entries meanwhile
            if key not in kept:
                self._know.pop(key, None)

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
        match (see _rule) scores at least RULE_SCORE for its class (DICTIONARY_SCORE for the dictionary
        pass) and 0 for the other."""
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
        floor = np.array([DICTIONARY_SCORE if r.endswith(":dictionary") else RULE_SCORE for r in rules])
        host = np.where(host_rule, np.maximum(host, floor), np.where(inst_rule, 0.0, host))
        inst = np.where(inst_rule, np.maximum(inst, floor), np.where(host_rule, 0.0, inst))
        return np.column_stack([host, inst])


def decide(p_host: float, p_inst: float, host_threshold: float, instance_threshold: float) -> str | None:
    if p_host >= host_threshold and p_host >= p_inst:
        return "host"
    if p_inst >= instance_threshold:
        return "instance"
    return "host" if p_host >= host_threshold else None


# --- Detection, pairing and status ---------------------------------------------

# What detect_servers() returns in 'kind' and 'status', with a one-line meaning each (the API's catalog).
KINDS = {
    "host": "A server name on its own (its default instance, MSSQLSERVER, counts as the server).",
    "host_instance": "A HOST\\INSTANCE name: a named instance on a given server.",
    "bare_instance": "A named instance written without its server; 'host' is set when it could be paired.",
}
STATUSES = {
    "known": "The client's known names include this host, pair or instance.",
    "new_instance_on_known_host": "The host is known for the client but this instance on it is not.",
    "possible_new_host": "Not among the client's known hosts (or the client is unknown).",
    "possible_new_instance": "A bare instance the client is not known to have, or paired with a host that does not list it.",
    "ambiguous": "A bare instance that several of the client's hosts have; those hosts are its candidates.",
    "typo_of_known": "One edit away from a known name of the client with the same shape; the known name replaces it.",
}
FIELDS = {"subject": "The ticket subject.", "description": "The ticket description (its first comment)."}


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
    by 'field' (subject or description). 'environment' is the one alerts or the CMDB give for the
    resolved host, when known."""
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
        d["environment"] = directory.environment(d["host"], client) if d["host"] else None
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


def load_model(path: Path = MODEL_PATH) -> dict:
    """The saved detector bundle: {model, directory, trained_on, created_at, config}."""
    return joblib.load(path)
