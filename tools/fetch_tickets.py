"""Fetch tickets from Zendesk into data/tickets.jsonl. Read-only: GET requests only."""

import argparse
import re
from datetime import date
from pathlib import Path

from ticket_analysis.tickets import make_ticket
from tools.common import TICKETS_PATH, ZendeskClient, load_env, merge_tickets

# Custom ticket fields whose title looks like a type or category are kept as weak labels.
WEAK_LABEL_TITLE = re.compile(r"(?i)\b(type|category|categories|sub-?category|issue|reason|intent)\b")
SEARCH_RESULT_CAP = 1000  # Zendesk Search API limit per query


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--since", type=date.fromisoformat, help="tickets created on or after YYYY-MM-DD")
    source.add_argument("--ids", help="comma-separated ticket ids, e.g. 12345,12346")
    p.add_argument("--limit", type=int, default=100, help="maximum tickets for --since (default 100)")
    p.add_argument("--output", type=Path, default=TICKETS_PATH, help="default data/tickets.jsonl")
    return p.parse_args()


def fetch_by_ids(zd: ZendeskClient, ids: list[str]) -> list[dict]:
    tickets = []
    for i in range(0, len(ids), 100):
        tickets += zd.get("tickets/show_many.json", {"ids": ",".join(ids[i:i + 100])}).get("tickets", [])
    return tickets


def fetch_since(zd: ZendeskClient, since: date, limit: int) -> list[dict]:
    if limit > SEARCH_RESULT_CAP:
        print(f"Zendesk search returns at most {SEARCH_RESULT_CAP} results; capping --limit.")
        limit = SEARCH_RESULT_CAP
    params = {
        "query": f"type:ticket created>={since.isoformat()}",
        "sort_by": "created_at",
        "sort_order": "desc",
        "per_page": min(100, limit),
    }
    return [r for r in zd.paginate("search.json", "results", params, limit=limit) if r.get("result_type", "ticket") == "ticket"]


def organization_names(zd: ZendeskClient, org_ids) -> dict[str, str]:
    ids = sorted({str(o) for o in org_ids if o})
    names = {}
    for i in range(0, len(ids), 100):
        data = zd.get("organizations/show_many.json", {"ids": ",".join(ids[i:i + 100])})
        names.update({str(o["id"]): o["name"] for o in data.get("organizations", [])})
    return names


def weak_label_fields(zd: ZendeskClient) -> dict[str, str]:
    """Custom ticket field id -> title, for fields that look like a type or category."""
    return {str(f["id"]): f["title"] for f in zd.paginate("ticket_fields.json", "ticket_fields")
            if WEAK_LABEL_TITLE.search(f.get("title") or "")}


def normalize(t: dict, org_names: dict[str, str], label_fields: dict[str, str]) -> dict:
    weak = {"zendesk_type": t.get("type")}
    for cf in t.get("custom_fields") or []:
        title = label_fields.get(str(cf.get("id")))
        value = cf.get("value")
        if title and value not in (None, "", []):
            weak[title] = ";".join(map(str, value)) if isinstance(value, list) else str(value)
    org_id = t.get("organization_id")
    return make_ticket(
        t["id"], t.get("subject"), t.get("description"),
        organization_id=org_id,
        organization_name=org_names.get(str(org_id)) if org_id else None,
        created_at=t.get("created_at"),
        tags=t.get("tags"),
        weak_labels=weak,
        source="zendesk_api",
    )


def main():
    args = parse_args()
    load_env()
    zd = ZendeskClient.from_env()
    if args.ids:
        raw = fetch_by_ids(zd, [i.strip() for i in args.ids.split(",") if i.strip()])
    else:
        raw = fetch_since(zd, args.since, args.limit)
    org_names = organization_names(zd, (t.get("organization_id") for t in raw))
    label_fields = weak_label_fields(zd)
    tickets = [normalize(t, org_names, label_fields) for t in raw]
    added, updated = merge_tickets(tickets, args.output)
    for t in tickets:  # IDs and subjects only; never descriptions
        print(f"  {t['ticket_id']:>8}  {t['subject'][:80]}")
    print(f"Fetched {len(tickets)} tickets ({added} new, {updated} updated) into {args.output}")


if __name__ == "__main__":
    main()
