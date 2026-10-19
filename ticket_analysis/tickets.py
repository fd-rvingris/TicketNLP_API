"""The normalized ticket record shared by every loader."""

from __future__ import annotations

from .alert_parser import parse_alert


def _flag(value) -> bool | None:
    """Read a yes/no value from an export (1/0, true/false, yes/no); anything else is unknown."""
    text = str(value).strip().lower() if value is not None else ""
    return True if text in ("1", "true", "yes", "y") else False if text in ("0", "false", "no", "n") else None


def _server_list(value) -> list[str]:
    """A list of server names from a list or a ';'-separated string, blanks and repeats dropped."""
    items = value if isinstance(value, list) else str(value or "").split(";")
    return list(dict.fromkeys(s.strip() for s in items if str(s).strip()))


STAFF_FIELDS = ["assignee_key", "status_assignee_keys", "team", "client_pod", "client_primary_dba"]
# Zendesk ticket fields from DW2.0 (FactTickets.Platform and DimTicketAttributes), exported with a dw_ prefix
# so they are not confused with the same names parsed from alert text.
DW_FIELDS = ["platform", "severity", "priority", "impact", "complexity"]


def _dw_fields(values: dict | None) -> dict:
    values = values or {}
    return {k: str(values[k]).strip() or None if values.get(k) not in (None, "") else None for k in DW_FIELDS}


def _staff(staff: dict | None) -> dict:
    """Who handled the ticket, from the DW2.0 export: DW2.0 assignee keys, team, and the client's
    POD and primary DBA. Missing fields are None (status_assignee_keys is a list)."""
    staff = staff or {}
    out = {k: (str(staff.get(k)).strip() or None) if staff.get(k) not in (None, "") else None for k in STAFF_FIELDS}
    out["status_assignee_keys"] = _server_list(staff.get("status_assignee_keys"))
    return out


def make_ticket(ticket_id, subject, description, *, organization_id=None, organization_name=None,
                created_at=None, tags=None, weak_labels=None, source="", is_alert=None, dw_servers=None,
                staff=None, dw_fields=None) -> dict:
    """is_alert, dw_servers, staff and dw_fields come from the DW2.0 export (automated monitoring ticket,
    servers with time logged against the ticket, who handled it, and Zendesk platform, severity, priority,
    impact and complexity). alert_fields are parsed from an alert's text by alert_parser.py. All of them are
    kept for review and reporting, never sent to the model."""
    return {
        "ticket_id": str(ticket_id).strip(),
        "subject": subject or "",
        "description": description or "",
        "organization_id": str(organization_id).strip() if organization_id not in (None, "") else None,
        "organization_name": organization_name or None,
        "created_at": created_at or None,
        "tags": list(tags or []),
        "weak_labels": {k: v for k, v in (weak_labels or {}).items() if v not in (None, "", [])},
        "is_alert": _flag(is_alert),
        "dw_servers": _server_list(dw_servers),
        "staff": _staff(staff),
        "dw_fields": _dw_fields(dw_fields),
        "alert_fields": parse_alert(subject or "", description or ""),
        "source": source,
    }
