"""Structured fields from automated alert tickets.

Most alerts come from the Fortified Data collector ("Alert #N" in the subject) and
put one field per line as "<label> <value>": AlertId, Alert (or Alert Collection),
Client Name, Server, Platform, Instance, Severity, Level, Message and so on. A
label with nothing after it is an empty field, except in an older layout that puts
the value on the next line. Some collector alerts use a table layout instead (a "Client Name
Environment Name Server ..." header, one row of values, then "Label: value" lines); it is
recognised by its header whatever the subject (parse_table). Prometheus/Grafana alerts ("[FIRING" in the subject) use
lower-case labels such as alertname, severity, instance and summary. Backup
Exception Reports and Volume Capacity alerts are recognised by subject only.

The fields are kept on each ticket for review, filtering and the non-LLM baseline;
they are never sent to the model.
"""

from __future__ import annotations

import re

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
# The collector's table layout ("Collection errors", "No recent monitoring runs"): a header line, one
# row of values, then "Label: value" lines. Field names come from the labels; these are renamed.
TABLE_KEYS = {"host_s": "hosts", "servername": "server_name"}
FREE_TEXT = {"message", "description", "recommendations", "summary"}  # may continue on following lines


def _label_pattern(labels, separator: str = r"[ \t]+") -> re.Pattern:
    # Longest first, so "Alert Collection" wins over "Alert" and "PreviousLevel" over "Level".
    names = sorted(labels, key=len, reverse=True)
    return re.compile(r"^(" + "|".join(re.escape(n) for n in names) + rf")(?:(?:{separator})(.*))?$")


_COLLECTOR_LINE = _label_pattern(COLLECTOR_LABELS)
_FIRING_LINE = _label_pattern(FIRING_LABELS, r"[ \t]*=[ \t]*|[ \t]+")  # "severity = critical"
_TABLE_HEADER = re.compile(r"^[^\S\r\n]*Client Name[^\S\r\n]+Environment Name[^\S\r\n]+Server[^\S\r\n]+Alert Type[^\S\r\n]+"
                           r"Open[^\S\r\n]+Date[^\S\r\n]+\(UTC\)[^\S\r\n]+Message[^\S\r\n]*\r?$", re.M)
# "<client> (<id>) <environment> (<code>) <server> <alert type> <yyyy-mm-dd hh:mm:ss> UTC <message>". The
# server column may be empty, so the part between the environment code and the date is split later.
_TABLE_ROW = re.compile(r"^[^\S\r\n]*(?P<client_name>.+?)[^\S\r\n]+\((?P<client_id>\d+)\)[^\S\r\n]+(?P<environment>.+?)[^\S\r\n]+"
                        r"\((?P<environment_code>[^()]*)\)(?P<middle>.*?)[^\S\r\n]+"
                        r"(?P<open_date>\d{4}-\d{2}-\d{2}[^\S\r\n]\d{2}:\d{2}:\d{2})[^\S\r\n]+UTC[^\S\r\n]*(?P<message>.*?)[^\S\r\n]*$")
_SERVER_NAME = re.compile(r"ServerName:[^\S\r\n]*(\S+)(?:[^\S\r\n]+\(([^)]*)\))?")
# "Label: value", the label a capitalized phrase of up to 40 characters ("Host(s)", "Minutes Delay")
_FIELD_LINE = re.compile(r"^[^\S\r\n]*(?P<label>[A-Z][A-Za-z0-9 ()/_\-]{0,40}?)[^\S\r\n]*:[^\S\r\n]*"
                         r"(?P<value>.*?)[^\S\r\n]*\r?$")
_LINE = re.compile(r"[^\r\n]+")


def _field_key(label: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return TABLE_KEYS.get(key, key)


def alert_family(subject: str) -> str | None:
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


def parse_labeled_lines(text: str, line_re: re.Pattern, labels: dict[str, str]) -> dict[str, str]:
    """Fields from "<label> <value>" lines. The first occurrence of a field wins; free-text fields take
    following unlabeled lines; an empty label takes the next line when that line is not a label."""
    lines = [line.strip() for line in (text or "").replace("\r\n", "\n").split("\n")]
    fields: dict[str, str] = {}
    current = None  # the free-text field that unlabeled lines continue
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line:
            continue
        m = line_re.match(line)
        if not m:
            if current:
                fields[current] += "\n" + line
            continue
        key, value = labels[m.group(1)], (m.group(2) or "").strip()
        if not value:
            nxt = next((j for j in range(i, len(lines)) if lines[j]), None)
            if nxt is not None and not line_re.match(lines[nxt]):
                value, i = lines[nxt], nxt + 1
        if key in fields:
            current = None
            continue
        if value:
            fields[key] = value
        current = key if key in FREE_TEXT and value else None
    return fields


def table_spans(text: str) -> list[dict]:
    """Where each value of the collector's table layout sits in the text, as {field, start, end} with
    offsets into text as given (no line-ending changes), so a page can highlight them. Empty when the
    text has no table header.

    The row is the first non-blank line after the header: client (id), environment (code), server,
    alert type, open date UTC, message. The server column may be empty; the server is then the
    'ServerName:' value in the message. A 'Label: value' message and every 'Label: value' line after
    the row become fields named after the label (Host(s) becomes hosts)."""
    text = text or ""
    header = _TABLE_HEADER.search(text)
    if not header:
        return []
    spans: list[dict] = []

    def add(field: str, start: int, end: int) -> None:
        if end > start:
            spans.append({"field": field, "start": start, "end": end})

    lines = [m for m in _LINE.finditer(text, header.end()) if m.group().strip()]
    row = _TABLE_ROW.match(lines[0].group().rstrip()) if lines else None
    if row:
        base = lines[0].start()
        for name in ("client_name", "client_id", "environment", "environment_code", "open_date", "message"):
            add(name, base + row.start(name), base + row.end(name))
        mid = base + row.start("middle")
        tokens = [(t.group(), mid + t.start(), mid + t.end()) for t in re.finditer(r"\S+", row["middle"])]
        msg = base + row.start("message")
        named = _SERVER_NAME.search(row["message"])
        empty_column = re.match(r"[^\S\r\n]{2,}", row["middle"]) is not None
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
    header. The first value of a field wins."""
    text = text or ""
    if not _TABLE_HEADER.search(text):
        return None
    fields: dict[str, str] = {"layout": "table"}
    for s in table_spans(text):
        fields.setdefault(s["field"], text[s["start"]:s["end"]].strip())
    return fields


_BACKUP_SUBJECT = re.compile(r"Backup Exception Report from server[^\S\r\n]+(?P<server>\S+)")
_BACKUP_HEADER = re.compile(r"^[^\S\r\n]*DATABASENAME[^\S\r\n]+BACKUP TYPE[^\r\n]*\r?$", re.M)
# "<database> <type> <Mon d yyyy h:mmAM> <hours> <minutes> [<latest backup server>]", read from the date
# outwards, so a database name may contain spaces.
_BACKUP_ROW = re.compile(r"^[^\S\r\n]*(?P<database>\S.*?)[^\S\r\n]+(?P<backup_type>\S+)[^\S\r\n]+"
                         r"(?P<latest_backup>[A-Z][a-z]{2}[^\S\r\n]+\d{1,2}[^\S\r\n]+\d{4}[^\S\r\n]+\d{1,2}:\d{2}[AP]M)"
                         r"[^\S\r\n]+(?P<hours_without>-?\d+)(?:[^\S\r\n]+(?P<minutes_without>-?\d+))?"
                         r"(?:[^\S\r\n]+(?P<backup_server>\S+))?[^\S\r\n]*\r?$")
_BACKUP_PARAM = re.compile(r"^[^\S\r\n]*(?P<label>@\w+|Report Creation Time)[^\S\r\n]+(?P<value>.*?)[^\S\r\n]*\r?$")
_BACKUP_NOTE = re.compile(r"^#{2,4}[^\S\r\n]*(?P<note>.*?(?:SERVER|[Ss]erver)\b.*?)[^\S\r\n]*\r?$", re.M)


def backup_spans(subject: str, text: str) -> list[dict]:
    """Where each value of a Backup Exception Report sits, as {field, start, end, row} with offsets into
    the subject (field 'server' from 'from server X') or the description. Each table row gives
    database, backup_type, latest_backup, hours_without, minutes_without and, in the Availability
    Group variant, backup_server; row numbers start at 0. Parameter lines (@HoursForFull, Report
    Creation Time) become fields named after the label."""
    spans: list[dict] = []
    m = _BACKUP_SUBJECT.search(subject or "")
    if m:
        spans.append({"field": "server", "start": m.start("server"), "end": m.end("server"), "in": "subject"})
    text = text or ""
    header = _BACKUP_HEADER.search(text)
    if not header:
        return spans
    note = _BACKUP_NOTE.search(text, 0, header.start())
    if note:
        spans.append({"field": "environment_note", "start": note.start("note"), "end": note.end("note")})
    row_no = 0
    for line in _LINE.finditer(text, header.end()):
        r = _BACKUP_ROW.match(line.group())
        if r:
            for name in ("database", "backup_type", "latest_backup", "hours_without", "minutes_without",
                         "backup_server"):
                if r.group(name):
                    spans.append({"field": name, "start": line.start() + r.start(name),
                                  "end": line.start() + r.end(name), "row": row_no})
            row_no += 1
            continue
        p = _BACKUP_PARAM.match(line.group())
        if p and p["value"]:
            spans.append({"field": _field_key(p["label"]), "start": line.start() + p.start("value"),
                          "end": line.start() + p.end("value")})
    return spans


def parse_backup_report(subject: str, text: str) -> dict:
    """Fields of a Backup Exception Report: the server, the environment note, the databases and the
    backup types listed (comma-separated, in order, without repeats), the number of rows, the oldest
    latest-backup date and the largest hours without backup, and the parameters."""
    fields: dict[str, str] = {}
    rows: dict[int, dict] = {}
    for s in backup_spans(subject, text):
        value = ((subject if s.get("in") == "subject" else text) or "")[s["start"]:s["end"]].strip()
        if "row" in s:
            rows.setdefault(s["row"], {})[s["field"]] = value
        else:
            fields.setdefault(s["field"], value)
    if rows:
        ordered = [rows[i] for i in sorted(rows)]
        fields["databases"] = ", ".join(dict.fromkeys(r["database"] for r in ordered))
        fields["backup_types"] = ", ".join(dict.fromkeys(r["backup_type"] for r in ordered))
        fields["backup_rows"] = str(len(ordered))
        fields["max_hours_without"] = str(max(int(r["hours_without"]) for r in ordered))
        servers = [r["backup_server"] for r in ordered if r.get("backup_server")]
        if servers:
            fields["backup_servers"] = ", ".join(dict.fromkeys(servers))
    return fields


def parse_alert(subject: str, description: str) -> dict | None:
    """The alert's family and fields, or None when the text is not a known alert format. The collector's
    table layout and the Backup Exception Report are also recognised by their header lines, so text
    pasted without its alert subject is parsed too."""
    family = alert_family(subject)
    table = parse_table(description)
    if table is not None:
        return {"family": family or "collector", **table}
    if family is None and _BACKUP_HEADER.search(description or ""):
        family = "backup_exception_report"
    if family is None:
        return None
    if family in ("collector", "volume_capacity"):
        fields = parse_labeled_lines(description, _COLLECTOR_LINE, COLLECTOR_LABELS)
        # "Alert Collection errors" is the alert "Collection errors", not the label "Alert Collection"
        if fields.get("alert_name") == "errors":
            fields["alert_name"] = "Collection errors"
        # Collection-error alerts leave Server empty and flatten the details into the message
        message = fields.get("message") or ""
        named = _SERVER_NAME.search(message)
        if named and not fields.get("server"):
            fields["server"] = named.group(1)
            if named.group(2) and not fields.get("platform"):
                fields["platform"] = named.group(2).strip()
        hosts = re.search(r"Host\(s\):[^\S\r\n]*(\S+)", message)
        if hosts and not fields.get("hosts"):
            fields["hosts"] = hosts.group(1)
    elif family == "firing":
        fields = parse_labeled_lines(description, _FIRING_LINE, FIRING_LABELS)
    elif family == "backup_exception_report":
        fields = parse_backup_report(subject, description)
    else:
        fields = {}
    return {"family": family, **fields}


def refresh_file(path) -> dict:
    """Re-parse alert_fields for every ticket in a .jsonl ticket file, in place, after a parser change.
    Returns counts of tickets whose fields changed, by family."""
    from collections import Counter
    from pathlib import Path

    from common import read_jsonl, write_jsonl
    path = Path(path)
    tickets = read_jsonl(path)
    changed = Counter()
    for t in tickets:
        fields = parse_alert(t.get("subject") or "", t.get("description") or "")
        if fields != t.get("alert_fields"):
            changed[(fields or {}).get("family") or "none"] += 1
            t["alert_fields"] = fields
    if changed:
        write_jsonl(path, tickets)
    return {"tickets": len(tickets), "changed": dict(changed)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        raise SystemExit("usage: python alert_parser.py TICKETS.jsonl [...]  (re-parses alert_fields in place)")
    for p in sys.argv[1:]:
        print(p, refresh_file(p))
