"""Structured fields from automated alert tickets.

Most alerts come from the Fortified Data collector ("Alert #N" in the subject) and
put one field per line as "<label> <value>": AlertId, Alert (or Alert Collection),
Client Name, Server, Platform, Instance, Severity, Level, Message and so on. A
label with nothing after it is an empty field, except in an older layout that puts
the value on the next line. Prometheus/Grafana alerts ("[FIRING" in the subject) use
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
FREE_TEXT = {"message", "description", "recommendations", "summary"}  # may continue on following lines


def _label_pattern(labels, separator: str = r"[ \t]+") -> re.Pattern:
    # Longest first, so "Alert Collection" wins over "Alert" and "PreviousLevel" over "Level".
    names = sorted(labels, key=len, reverse=True)
    return re.compile(r"^(" + "|".join(re.escape(n) for n in names) + rf")(?:(?:{separator})(.*))?$")


_COLLECTOR_LINE = _label_pattern(COLLECTOR_LABELS)
_FIRING_LINE = _label_pattern(FIRING_LABELS, r"[ \t]*=[ \t]*|[ \t]+")  # "severity = critical"


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


def parse_alert(subject: str, description: str) -> dict | None:
    """The alert's family and fields, or None when the subject is not a known alert format."""
    family = alert_family(subject)
    if family is None:
        return None
    if family == "collector":
        fields = parse_labeled_lines(description, _COLLECTOR_LINE, COLLECTOR_LABELS)
    elif family == "firing":
        fields = parse_labeled_lines(description, _FIRING_LINE, FIRING_LABELS)
    else:
        fields = {}
    return {"family": family, **fields}
