"""Normalize a Zendesk export (CSV, JSON, or JSONL) into data/tickets.jsonl."""

import argparse
import csv
import json
import re
from pathlib import Path

from common import DW_FIELDS, STAFF_FIELDS, TICKETS_PATH, make_ticket, merge_tickets, read_jsonl
from fetch_tickets import WEAK_LABEL_TITLE

# Accepted CSV headers per field, compared case-insensitively.
COLUMN_ALIASES = {
    "ticket_id": ["ticket_id", "id", "ticket id", "#"],
    "subject": ["subject"],
    "description": ["description", "first comment", "body"],
    "organization_id": ["organization_id", "organization id", "org id"],
    "organization_name": ["organization_name", "organization", "organization name"],
    "created_at": ["created_at", "created at", "created"],
    "tags": ["tags"],
    "zendesk_type": ["type", "ticket type"],
    "is_alert": ["is_alert", "is alert"],
    "dw_servers": ["dw_servers", "dw servers"],
} | {field: [field, field.replace("_", " ")] for field in STAFF_FIELDS} | {
    "dw_" + field: ["dw_" + field, "dw " + field] for field in DW_FIELDS}


def clean(value: str | None) -> str:
    """Strip a CSV cell; SSMS writes SQL NULL as the text NULL, which means empty."""
    value = (value or "").strip()
    return "" if value == "NULL" else value


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path", type=Path, help="export file (.csv, .json, .jsonl, or .ndjson)")
    p.add_argument("--output", type=Path, default=TICKETS_PATH, help="default data/tickets.jsonl")
    p.add_argument("--exclude", type=Path, nargs="*", default=[],
                   help="ticket files whose tickets are left out, such as the training and test files, so a new "
                        "export never repeats a ticket already pulled")
    return p.parse_args(argv)


def from_csv(path: Path, source: str) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = {h.strip().lower(): h for h in reader.fieldnames or []}
        col = {key: next((headers[a] for a in aliases if a in headers), None) for key, aliases in COLUMN_ALIASES.items()}
        if not col["ticket_id"]:
            raise SystemExit(f"No ticket id column found; expected one of {COLUMN_ALIASES['ticket_id']}")
        used = {c for c in col.values() if c}
        label_columns = [h for h in reader.fieldnames if h not in used and WEAK_LABEL_TITLE.search(h)]
        tickets = []
        for row in reader:
            value = {key: clean(row.get(c)) if c else "" for key, c in col.items()}
            if not value["ticket_id"]:
                continue
            weak = {"zendesk_type": value["zendesk_type"]} | {h: clean(row.get(h)) for h in label_columns}
            tickets.append(make_ticket(
                value["ticket_id"], value["subject"], value["description"],
                organization_id=value["organization_id"], organization_name=value["organization_name"],
                created_at=value["created_at"], tags=re.split(r"[\s,]+", value["tags"]) if value["tags"] else [],
                weak_labels=weak, source=source, is_alert=value["is_alert"], dw_servers=value["dw_servers"],
                staff={k: value[k] for k in STAFF_FIELDS}, dw_fields={k: value["dw_" + k] for k in DW_FIELDS},
            ))
        return tickets


def from_json(path: Path, source: str) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in (".jsonl", ".ndjson"):
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        data = json.loads(text)
        items = data.get("tickets", []) if isinstance(data, dict) else data
    return [
        make_ticket(
            t.get("ticket_id", t.get("id")), t.get("subject"), t.get("description"),
            organization_id=t.get("organization_id"), organization_name=t.get("organization_name"),
            created_at=t.get("created_at"), tags=t.get("tags"),
            weak_labels=t.get("weak_labels") or {"zendesk_type": t.get("type")}, source=source,
            is_alert=t.get("is_alert"), dw_servers=t.get("dw_servers"),
            staff=t.get("staff") or {k: t.get(k) for k in STAFF_FIELDS},
            dw_fields=t.get("dw_fields") or {k: t.get("dw_" + k) for k in DW_FIELDS},
        )
        for t in items if t.get("ticket_id", t.get("id")) is not None
    ]


def without_tickets_in(tickets: list[dict], paths) -> tuple[list[dict], int]:
    """The tickets minus those whose ticket_id is in any of the given ticket files (missing files are
    refused, so a typo cannot silently let repeats through), and how many were dropped."""
    seen = set()
    for path in paths:
        if not Path(path).exists():
            raise SystemExit(f"File to exclude not found: {path}")
        seen |= {str(t.get("ticket_id")) for t in read_jsonl(Path(path))}
    kept = [t for t in tickets if str(t["ticket_id"]) not in seen]
    return kept, len(tickets) - len(kept)


def main(argv=None):
    args = parse_args(argv)
    if not args.path.exists():
        raise SystemExit(f"File not found: {args.path}")
    source = f"export:{args.path.name}"
    if args.path.suffix.lower() == ".csv":
        tickets = from_csv(args.path, source)
    else:
        tickets = from_json(args.path, source)
    if args.exclude:
        tickets, dropped = without_tickets_in(tickets, args.exclude)
        print(f"Left out {dropped} tickets already in {', '.join(str(p) for p in args.exclude)}")
    added, updated = merge_tickets(tickets, args.output)
    print(f"Loaded {len(tickets)} tickets ({added} new, {updated} updated) into {args.output}")


if __name__ == "__main__":
    main()
