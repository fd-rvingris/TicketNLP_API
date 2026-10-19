"""Structured fields from automated alert tickets.

Alerts are machine-generated from a few fixed templates (layouts). Each layout has a parser that
returns spans: where each value sits in the subject or the description, as {field, start, end, in,
row?}. parse_alert() tries the layouts in a fixed order (LAYOUTS), takes the first that matches and
builds the fields from its spans; alert_spans() returns the spans for highlighting.

    collector_table     "Client Name Environment Name Server Alert Type Open Date (UTC) Message" header,
                        one row of values, then "Label: value" lines
    backup_report       Backup Exception Report: "from server X" subject, DATABASENAME ... table rows
    grafana             [FIRING alerts: "**Labels**" blocks of "label = value" lines, one per alert
    volume_capacity     "Volume Capacity-X:\\ on HOST" alerts: Resource, Source, Metric, value reported
    collector_markdown  the collector's labels flattened onto one line: "* *AlertId*: 1 * *Alert*: ..."
    collector_labeled   the collector's "Label value" lines (AlertId, Alert, Client Name, Server ...)

Every layout is also recognised by its body, so text pasted without its subject is parsed. The
fields are kept on each ticket for review, filtering and the server detector; they are never sent
to the model.

The commands that re-parse ticket files and measure coverage are in tools/alert_parser.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

COLLECTOR_LABELS = {
    "AlertId": "alert_id",
    "Alert Collection": "alert_name",
    "Alert": "alert_name",
    "Client Name": "client_name",
    "Server": "server",
    "Platform": "platform",
    "Instance": "instance",
    "Collector Machine": "collector",
    "Alias": "alias",
    "Keyword": "keyword",
    "Message": "message",
    "Severity": "severity",
    "PreviousLevel": "previous_level",
    "Previous Level": "previous_level",
    "Level": "level",
    "Open Date": "open_date",
    "Close Date": "close_date",
    "OK Collections": "ok_collections",
    "Failures": "failures",
    "Description": "description",
    "Recommendations": "recommendations",
}
FIRING_LABELS = {
    "alertname": "alert_name",
    "severity": "severity",
    "instance": "instance",
    "job": "job",
    "summary": "summary",
    "description": "description",
    "env": "env",
    "dimension_DBInstanceIdentifier": "db_instance",
}
# Fields that name the alert's own servers, the one list the server detector reads. True: the value
# is a list (comma or space separated); False: one name.
SERVER_FIELDS = {"server": False, "instance": False, "db_instance": False, "target": False,
                 "hosts": True, "backup_servers": True, "instancenames": True, "instances": True, "targets": True}
# Field names derived from labels ("Host(s):" gives host_s) that are renamed
FIELD_KEYS = {"host_s": "hosts", "servername": "server_name", "databasename": "database"}
FREE_TEXT = {"message", "description", "recommendations", "summary"}  # may continue on following lines

_H = r"[^\S\r\n]"  # horizontal space, including non-breaking spaces
_LINE = re.compile(r"[^\r\n]+")


def _label_pattern(labels, separator: str = rf"{_H}+") -> re.Pattern:
    # Longest first, so "Alert Collection" wins over "Alert" and "PreviousLevel" over "Level".
    names = sorted(labels, key=len, reverse=True)
    return re.compile(r"^(" + "|".join(re.escape(n) for n in names) + rf")(?:(?:{separator})(.*))?$")


_COLLECTOR_LINE = _label_pattern(COLLECTOR_LABELS)
_FIRING_LINE = _label_pattern(FIRING_LABELS, rf"{_H}*={_H}*|{_H}+")  # "severity = critical"
_SERVER_NAME = re.compile(rf"ServerName:{_H}*(\S+)(?:{_H}+\(([^)]*)\))?")
_HOSTS = re.compile(rf"Host\(s\):{_H}*(\S+)")
# "Label: value", the label a capitalized phrase of up to 40 characters ("Host(s)", "Minutes Delay")
_FIELD_LINE = re.compile(rf"^{_H}*(?P<label>[A-Z][A-Za-z0-9 ()/_\-]{{0,40}}?){_H}*:{_H}*(?P<value>.*?){_H}*\r?$")


def _field_key(label: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return FIELD_KEYS.get(key, key)


def _lines(text: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
    """(start, end) of each non-blank line between start and end, without surrounding space."""
    out = []
    for m in _LINE.finditer(text, start, len(text) if end is None else end):
        s, e = m.span()
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            out.append((s, e))
    return out


def _trimmed(text: str, s: int, e: int) -> tuple[int, int]:
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    return s, e


def _span(field: str, start: int, end: int, where: str = "description", row: int | None = None) -> dict:
    sp = {"field": field, "start": start, "end": end, "in": where}
    if row is not None:
        sp["row"] = row
    return sp


def span_value(subject: str, description: str, span: dict) -> str:
    """A span's value: its text, with each line trimmed and blank lines dropped (free-text fields
    span several lines)."""
    src = (subject if span.get("in") == "subject" else description) or ""
    return "\n".join(x.strip() for x in src[span["start"]:span["end"]].splitlines() if x.strip())


@dataclass(frozen=True)
class Layout:
    name: str                                     # "collector_labeled", "collector_table", "backup_report", ...
    family: str                                   # the family value of earlier versions, kept for compatibility
    matches: Callable[[str, str], bool]           # (subject, description) -> bool
    spans: Callable[[str, str], list[dict]]       # {field, start, end, in: subject|description, row?}
    summarize: Callable[[dict, list[dict]], None] | None = None  # fields, rows -> adds summary fields


# --- Labeled lines (collector, Grafana) ---------------------------------------------


def labeled_spans(text: str, line_re: re.Pattern, labels: dict[str, str], *, extra: re.Pattern | None = None,
                  stop: re.Pattern | None = None, block: str | None = None) -> list[dict]:
    """Spans of "<label> <value>" lines. The first occurrence of a field wins; free-text fields take
    following unlabeled lines (up to a line matching stop); an empty label takes the next line when
    that line is not a label. extra matches other "key = value" lines (Grafana's own labels). With
    block, each line labeled block starts a new row, and first-wins applies per row."""
    lines = _lines(text)
    spans: list[dict] = []
    seen: set[str] = set()
    current = None  # the free-text span that unlabeled lines continue
    row = -1 if block else None

    def label_of(s: int, e: int):
        line = text[s:e]
        m = line_re.match(line)
        if m:
            return labels[m.group(1)], m.start(2) if m.group(2) is not None else None, m.group(1)
        x = extra.match(line) if extra else None
        if x:
            return _field_key(x["key"]), x.start("value"), x["key"]
        return None

    i = 0
    while i < len(lines):
        s, e = lines[i]
        i += 1
        lab = label_of(s, e)
        if lab is None:
            if stop and stop.match(text[s:e]):
                current = None
            elif current is not None:
                current["end"] = e
            continue
        key, vstart, raw_label = lab
        if block and key == block:
            row += 1
            seen = set()
        vs, ve = _trimmed(text, s + vstart, e) if vstart is not None else (e, e)
        if raw_label == "Alert Collection" and text[vs:ve] == "errors":
            # "Alert Collection errors" is the alert "Collection errors", not the label "Alert Collection"
            vs = s + raw_label.index("Collection")
        if ve <= vs and i < len(lines) and label_of(*lines[i]) is None and not (stop and stop.match(text[slice(*lines[i])])):
            (vs, ve), i = lines[i], i + 1  # older vertical layout: the value is on the next line
        if key in seen:
            current = None
            continue
        if ve > vs:
            sp = _span(key, vs, ve, row=row if block and row >= 0 else None)
            spans.append(sp)
            seen.add(key)
            current = sp if key in FREE_TEXT else None
        else:
            current = None
    return spans


def _message_servers(text: str, spans: list[dict]) -> list[dict]:
    """Collection-error alerts leave Server and Platform empty and flatten the details into the
    message ("ServerName: HOST (SQL Server) Host(s): HOST Database: ..."): spans for server, platform
    and hosts from the message, when the labeled fields are empty."""
    have = {s["field"] for s in spans}
    msg = next((s for s in spans if s["field"] == "message"), None)
    if not msg:
        return []
    out = []
    named = _SERVER_NAME.search(text, msg["start"], msg["end"])
    if named and "server" not in have:
        out.append(_span("server", *named.span(1)))
        if named.group(2) and "platform" not in have:
            out.append(_span("platform", *_trimmed(text, *named.span(2))))
    hosts = _HOSTS.search(text, msg["start"], msg["end"])
    if hosts and "hosts" not in have:
        out.append(_span("hosts", *hosts.span(1)))
    return out


_ALERT_SUBJECT = re.compile(r"Alert[^\S\r\n]*#")
_REPLY = re.compile(r"^\s*(?:re|fw|fwd)\s*:", re.I)


def _collector_lines(description: str) -> set[str]:
    return {m.group(1) for s, e in _lines(description or "") for m in [_COLLECTOR_LINE.match(description[s:e])] if m}


def collector_labeled_matches(subject: str, description: str) -> bool:
    """An 'Alert #' subject that is not a reply, or a body with four or more collector labels
    including Alert or AlertId."""
    if _ALERT_SUBJECT.search(subject or "") and not _REPLY.match(subject or ""):
        return True
    found = _collector_lines(description)
    return len(found) >= 4 and bool(found & {"AlertId", "Alert", "Alert Collection"})


def collector_labeled_spans(subject: str, description: str) -> list[dict]:
    text = description or ""
    spans = labeled_spans(text, _COLLECTOR_LINE, COLLECTOR_LABELS)
    return spans + _message_servers(text, spans)


# Grafana: "label = value" lines; a block per alert starts at its alertname line
_FIRING_EXTRA = re.compile(rf"^(?P<key>[A-Za-z_][A-Za-z0-9_]*){_H}*={_H}*(?P<value>.*)$")
_FIRING_STOP = re.compile(r"^(?:\*\*.*\*\*|Source)$")
_FIRING_BODY = re.compile(rf"^{_H}*alertname{_H}*=", re.M)


def grafana_matches(subject: str, description: str) -> bool:
    return (subject or "").startswith("[FIRING") or bool(_FIRING_BODY.search(description or ""))


def grafana_spans(subject: str, description: str) -> list[dict]:
    text = description or ""
    spans = labeled_spans(text, _FIRING_LINE, FIRING_LABELS, extra=_FIRING_EXTRA, stop=_FIRING_STOP, block="alert_name")
    if spans and all(s.get("row") == 0 for s in spans):
        for s in spans:  # one alert: no rows
            del s["row"]
    return spans


def grafana_summary(fields: dict, rows: list[dict]) -> None:
    """Several alerts in one message: each field's first value, the number of alerts, and every
    instance and target."""
    for r in rows:
        for k, v in r.items():
            fields.setdefault(k, v)
    if len(rows) > 1:
        fields["alerts"] = str(len(rows))
        for one, many in (("instance", "instances"), ("target", "targets")):
            values = list(dict.fromkeys(r[one] for r in rows if r.get(one)))
            if len(values) > 1:
                fields[many] = ", ".join(values)


# The collector's labels on one Markdown line: "* *AlertId*: 123 * *Alert*: Low Disk Space * ..."
_MARKDOWN_LABEL = re.compile(rf"\*{_H}*\*(?P<label>[A-Za-z][A-Za-z ()]{{0,30}}?)\*:")


def _markdown_markers(text: str, s: int, e: int) -> list[re.Match]:
    marks = list(_MARKDOWN_LABEL.finditer(text, s, e))
    return marks if sum(m["label"] in COLLECTOR_LABELS for m in marks) >= 3 else []


def collector_markdown_matches(subject: str, description: str) -> bool:
    text = description or ""
    return any(_markdown_markers(text, s, e) for s, e in _lines(text))


def collector_markdown_spans(subject: str, description: str) -> list[dict]:
    text = description or ""
    spans: list[dict] = []
    seen: set[str] = set()
    for s, e in _lines(text):
        marks = _markdown_markers(text, s, e)
        for k, m in enumerate(marks):
            end = marks[k + 1].start() if k + 1 < len(marks) else e
            key = COLLECTOR_LABELS.get(m["label"]) or _field_key(m["label"])
            vs, ve = _trimmed(text, m.end(), end)
            if ve > vs and key not in seen:
                spans.append(_span(key, vs, ve))
                seen.add(key)
    return spans + _message_servers(text, spans)


# --- Collector table layout ------------------------------------------------------------

_TABLE_HEADER = re.compile(rf"^{_H}*Client Name{_H}+Environment Name{_H}+Server{_H}+Alert Type{_H}+"
                           rf"(?P<date>Open|Close){_H}+Date{_H}+\(UTC\){_H}+Message{_H}*\r?$", re.M)
# "<client> (<id>) <environment> (<collector machine>) <server> <alert type> <yyyy-mm-dd hh:mm:ss> UTC
# <message>". The server column may be empty, so the part between the brackets and the date is split later.
_TABLE_ROW = re.compile(rf"^{_H}*(?P<client_name>.+?){_H}+\((?P<client_id>\d+)\){_H}+(?P<environment>.+?){_H}+"
                        rf"\((?P<collector>[^()]*)\)(?P<middle>.*?){_H}+"
                        rf"(?P<date>\d{{4}}-\d{{2}}-\d{{2}}{_H}\d{{2}}:\d{{2}}:\d{{2}}){_H}+UTC{_H}*(?P<message>.*?){_H}*$")


def collector_table_matches(subject: str, description: str) -> bool:
    return _TABLE_HEADER.search(description or "") is not None


def table_spans(text: str) -> list[dict]:
    """Where each value of the collector's table layout sits in the text, with offsets into text as
    given (no line-ending changes). Empty when the text has no table header.

    The row is the first non-blank line after the header: client (id), environment (collector
    machine), server, alert type, open (or close) date UTC, message. The server column may be empty;
    the server is then the 'ServerName:' value in the message. A 'Label: value' message and every
    'Label: value' line after the row become fields named after the label (Host(s) becomes hosts)."""
    text = text or ""
    header = _TABLE_HEADER.search(text)
    if not header:
        return []
    spans: list[dict] = []

    def add(field: str, start: int, end: int) -> None:
        if end > start:
            spans.append(_span(field, start, end))

    lines = [m for m in _LINE.finditer(text, header.end()) if m.group().strip()]
    row = _TABLE_ROW.match(lines[0].group().rstrip()) if lines else None
    if row:
        base = lines[0].start()
        for name in ("client_name", "client_id", "environment", "collector"):
            add(name, base + row.start(name), base + row.end(name))
        add("open_date" if header["date"] == "Open" else "close_date", base + row.start("date"), base + row.end("date"))
        add("message", base + row.start("message"), base + row.end("message"))
        mid = base + row.start("middle")
        tokens = [(t.group(), mid + t.start(), mid + t.end()) for t in re.finditer(r"\S+", row["middle"])]
        msg = base + row.start("message")
        named = _SERVER_NAME.search(row["message"])
        empty_column = re.match(rf"{_H}{{2,}}", row["middle"]) is not None
        server, alert = None, tokens
        if named and named.group(1) in [t[0] for t in tokens]:
            cut = [t[0] for t in tokens].index(named.group(1))
            server, alert = tokens[cut], tokens[cut + 1:]
        elif tokens and not empty_column:
            server, alert = tokens[0], tokens[1:]
        if server:
            add("server", server[1], server[2])
        elif named:
            add("server", msg + named.start(1), msg + named.end(1))
        if alert:
            add("alert_name", alert[0][1], alert[-1][2])
        if named and named.group(2):
            add("platform", msg + named.start(2), msg + named.end(2))
        pair = _FIELD_LINE.match(row["message"])
        if pair and not named:
            add(_field_key(pair["label"]), msg + pair.start("value"), msg + pair.end("value"))
    for line in lines[1 if row else 0:]:
        pair = _FIELD_LINE.match(line.group())
        if pair:
            add(_field_key(pair["label"]), line.start() + pair.start("value"), line.start() + pair.end("value"))
    return spans


def parse_table(text: str) -> dict[str, str] | None:
    """Fields of the collector's table layout (see table_spans), or None when the text has no table
    header."""
    return parse_alert("", text) if collector_table_matches("", text) else None


# --- Backup Exception Report ------------------------------------------------------------

_BACKUP_SUBJECT = re.compile(rf"Backup Exception Report from server{_H}+(?P<server>\S+)")
_BACKUP_HEADER = re.compile(rf"^{_H}*DATABASENAME{_H}+BACKUP TYPE[^\r\n]*\r?$", re.M)
# "<database> <type> <Mon d yyyy h:mmAM> <hours> <minutes> [<latest backup server>]", read from the date
# outwards, so a database name may contain spaces.
_BACKUP_ROW = re.compile(rf"^{_H}*(?P<database>\S.*?){_H}+(?P<backup_type>\S+){_H}+"
                         rf"(?P<latest_backup>[A-Z][a-z]{{2}}{_H}+\d{{1,2}}{_H}+\d{{4}}{_H}+\d{{1,2}}:\d{{2}}[AP]M)"
                         rf"{_H}+(?P<hours_without>-?\d+)(?:{_H}+(?P<minutes_without>-?\d+))?"
                         rf"(?:{_H}+(?P<backup_server>\S+))?{_H}*\r?$")
_BACKUP_PARAM = re.compile(rf"^{_H}*(?P<label>@\w+|Report Creation Time){_H}+(?P<value>.*?){_H}*\r?$")
# "### PRODUCTION SERVER: PLEASE INVESTIGATE.", or the Availability Group variant's "### HOST missing backup report ..."
_BACKUP_NOTE = re.compile(rf"^#{{2,4}}{_H}*(?P<note>.*?(?:SERVER|[Ss]erver|missing backup report)\b.*?){_H}*\r?$", re.M)


def backup_report_matches(subject: str, description: str) -> bool:
    return (subject or "").startswith("Backup Exception Report") or _BACKUP_HEADER.search(description or "") is not None


def backup_spans(subject: str, text: str) -> list[dict]:
    """Where each value of a Backup Exception Report sits, with offsets into the subject (field
    'server' from 'from server X') or the description. Each table row gives database, backup_type,
    latest_backup, hours_without, minutes_without and, in the Availability Group variant,
    backup_server; row numbers start at 0. Parameter lines (@HoursForFull, Report Creation Time)
    become fields named after the label."""
    spans: list[dict] = []
    m = _BACKUP_SUBJECT.search(subject or "")
    if m:
        spans.append(_span("server", m.start("server"), m.end("server"), "subject"))
    text = text or ""
    header = _BACKUP_HEADER.search(text)
    if not header:
        return spans
    note = _BACKUP_NOTE.search(text, 0, header.start())
    if note:
        spans.append(_span("environment_note", note.start("note"), note.end("note")))
    row_no = 0
    for line in _LINE.finditer(text):  # parameters come before the table in the Availability Group variant
        r = _BACKUP_ROW.match(line.group()) if line.start() > header.start() else None
        if r:
            for name in ("database", "backup_type", "latest_backup", "hours_without", "minutes_without",
                         "backup_server"):
                if r.group(name):
                    spans.append(_span(name, line.start() + r.start(name), line.start() + r.end(name), row=row_no))
            row_no += 1
            continue
        p = _BACKUP_PARAM.match(line.group())
        if p and p["value"]:
            spans.append(_span(_field_key(p["label"]), line.start() + p.start("value"), line.start() + p.end("value")))
    return spans


def backup_summary(fields: dict, rows: list[dict]) -> None:
    """The databases and backup types listed (comma-separated, in order, without repeats), the number
    of rows, the largest hours without backup and the latest backup servers."""
    if not rows:
        return
    fields["databases"] = ", ".join(dict.fromkeys(r["database"] for r in rows))
    fields["backup_types"] = ", ".join(dict.fromkeys(r["backup_type"] for r in rows))
    fields["backup_rows"] = str(len(rows))
    fields["max_hours_without"] = str(max(int(r["hours_without"]) for r in rows))
    servers = [r["backup_server"] for r in rows if r.get("backup_server")]
    if servers:
        fields["backup_servers"] = ", ".join(dict.fromkeys(servers))


def parse_backup_report(subject: str, text: str) -> dict:
    """Fields of a Backup Exception Report (see backup_spans and backup_summary)."""
    fields = parse_alert(subject, text) if backup_report_matches(subject, text) else {}
    return {k: v for k, v in (fields or {}).items() if k not in ("family", "layout")}


# --- Volume Capacity ---------------------------------------------------------------------

# Subject "Volume Capacity-C:\ on HOST ..."; other alerts of the same template name another datasource
# and get their server from the body. Body detail line "<id> <severity> - ... - Resource: HOST -
# Source: Volume Capacity-C:\ Metric: <metric> value reported: <n> on: ... on host HOST ... <n> percent
# used, ... yyyy-mm-dd hh:mm:ss"
_VOLUME_SUBJECT = re.compile(rf"(?P<alert_name>Volume{_H}*Capacity)-(?P<volume>\S+?){_H}+on{_H}+(?P<server>\S+)")
_VOLUME_DETAIL = re.compile(rf"Resource:{_H}*\S+{_H}+-{_H}+Source:")
_VOLUME_PARTS = [
    re.compile(rf"^{_H}*(?P<alert_id>[A-Za-z]+\d+){_H}+(?P<severity>[A-Za-z]+){_H}+-{_H}"),
    re.compile(rf"Resource:{_H}*(?P<server>\S+)"),
    re.compile(rf"Source:{_H}*(?P<source>\S.*?){_H}+Metric:"),
    re.compile(rf"Metric:{_H}*(?P<metric>\S+)"),
    re.compile(rf"value reported:{_H}*(?P<value>-?[\d.]+)"),
    re.compile(rf"on host{_H}+(?P<hosts>\S+)"),
    re.compile(rf"(?P<percent_used>[\d.]+){_H}+percent used"),
    re.compile(rf"(?P<open_date>\d{{4}}-\d{{2}}-\d{{2}}{_H}\d{{2}}:\d{{2}}:\d{{2}})(?:{_H}+(?P<time_zone>[A-Z]{{2,4}})\b)?"),
]


def volume_capacity_matches(subject: str, description: str) -> bool:
    return (subject or "").startswith("Volume Capacity") or _VOLUME_DETAIL.search(description or "") is not None


def volume_capacity_spans(subject: str, description: str) -> list[dict]:
    """The subject's alert name, volume and server; the detail line's alert id, severity, resource
    (server), source, metric, value, host and percent used; every other 'Label: value' line (a label
    with no value takes the next line when that line is not a label)."""
    spans: list[dict] = []
    m = _VOLUME_SUBJECT.search(subject or "")
    if m:
        for name in ("alert_name", "volume", "server"):
            spans.append(_span(name, m.start(name), m.end(name), "subject"))
    text = description or ""
    lines = _lines(text)
    seen: set[str] = set()

    def add(field: str, s: int, e: int) -> None:
        if e > s and field not in seen:
            spans.append(_span(field, s, e))
            seen.add(field)

    i = 0
    while i < len(lines):
        s, e = lines[i]
        i += 1
        line = text[s:e]
        if _VOLUME_DETAIL.search(line):
            for part in _VOLUME_PARTS:
                p = part.search(line)
                if p:
                    for name, value in p.groupdict().items():
                        if value:
                            add(name, s + p.start(name), s + p.end(name))
            continue
        pair = _FIELD_LINE.match(line)
        if not pair or line.startswith(("http:", "https:")):
            continue
        vs, ve = s + pair.start("value"), s + pair.end("value")
        if ve <= vs and i < len(lines) and not _FIELD_LINE.match(text[slice(*lines[i])]):
            (vs, ve), i = lines[i], i + 1
        add(_field_key(pair["label"]), vs, ve)
    return spans


# --- Registry ------------------------------------------------------------------------------

LAYOUTS: list[Layout] = [
    Layout("collector_table", "collector", collector_table_matches, lambda subject, d: table_spans(d)),
    Layout("backup_report", "backup_exception_report", backup_report_matches, backup_spans, backup_summary),
    Layout("grafana", "firing", grafana_matches, grafana_spans, grafana_summary),
    Layout("volume_capacity", "volume_capacity", volume_capacity_matches, volume_capacity_spans),
    Layout("collector_markdown", "collector", collector_markdown_matches, collector_markdown_spans),
    Layout("collector_labeled", "collector", collector_labeled_matches, collector_labeled_spans),
]


def alert_family(subject: str) -> str | None:
    """The family an alert subject names, or None (the subject alone; parse_alert also reads the body)."""
    subject = subject or ""
    if "Alert #" in subject:
        return "collector"
    if subject.startswith("[FIRING"):
        return "firing"
    if subject.startswith("Backup Exception Report"):
        return "backup_exception_report"
    if subject.startswith("Volume Capacity"):
        return "volume_capacity"
    return None


def match_layout(subject: str, description: str) -> Layout | None:
    """The first layout in LAYOUTS whose matches() accepts the text, or None."""
    return next((lay for lay in LAYOUTS if lay.matches(subject or "", description or "")), None)


def alert_spans(subject: str, description: str) -> list[dict]:
    """Where each alert value sits, as {field, start, end, in: subject|description, row?}; empty
    when the text is not a known alert layout."""
    layout = match_layout(subject, description)
    return layout.spans(subject or "", description or "") if layout else []


def parse_alert(subject: str, description: str) -> dict | None:
    """The alert's family, layout and fields, or None when the text is not a known alert layout.
    Fields come from the layout's spans; the first value of a field wins, and fields of repeated
    rows are summarized by the layout (databases, backup_rows, alerts)."""
    subject, description = subject or "", description or ""
    layout = match_layout(subject, description)
    if layout is None:
        return None
    fields: dict[str, str] = {"family": layout.family, "layout": layout.name}
    rows: dict[int, dict] = {}
    for s in layout.spans(subject, description):
        value = span_value(subject, description, s)
        if not value:
            continue
        if "row" in s:
            rows.setdefault(s["row"], {}).setdefault(s["field"], value)
        else:
            fields.setdefault(s["field"], value)
    if layout.summarize:
        layout.summarize(fields, [rows[i] for i in sorted(rows)])
    return fields
