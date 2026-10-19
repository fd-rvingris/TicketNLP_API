"""Commands for ticket_analysis.alert_parser:

    python -m tools.alert_parser TICKETS.jsonl [...]            re-parse alert_fields in place
    python -m tools.alert_parser coverage TICKETS.jsonl [...]   per-layout coverage (counts only)
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

from ticket_analysis.alert_parser import LAYOUTS, parse_alert
from ticket_analysis.jsonl import read_jsonl, write_jsonl
from ticket_analysis.server_detector import ticket_names
from tools.common import md_table


# --- Ticket files ----------------------------------------------------------------------------


def refresh_file(path) -> dict:
    """Re-parse alert_fields for every ticket in a .jsonl ticket file, in place, after a parser change.
    Returns counts of tickets whose fields changed, by layout."""
    path = Path(path)
    tickets = read_jsonl(path)
    changed = Counter()
    for t in tickets:
        fields = parse_alert(t.get("subject") or "", t.get("description") or "")
        if fields != t.get("alert_fields"):
            changed[(fields or {}).get("layout") or "none"] += 1
            t["alert_fields"] = fields
    if changed:
        write_jsonl(path, tickets)
    return {"tickets": len(tickets), "changed": dict(changed)}


def coverage(tickets: list[dict], top: int = 6) -> list[dict]:
    """Per layout: alerts (is_alert tickets), how many it parses with fields beyond the family, how
    many name a server (a server-shaped value in SERVER_FIELDS), and the most-filled fields. A last row counts alerts no layout
    recognises, and human tickets a layout recognises."""
    stats: dict[str, dict] = defaultdict(lambda: {"alerts": 0, "parsed": 0, "server": 0, "fields": Counter()})
    humans = Counter()
    for t in tickets:
        fields = parse_alert(t.get("subject") or "", t.get("description") or "")
        name = (fields or {}).get("layout") or "unrecognised"
        if t.get("is_alert") is not True:
            if fields:
                humans[name] += 1
            continue
        st = stats[name]
        st["alerts"] += 1
        if fields:
            keys = set(fields) - {"family", "layout"}
            st["parsed"] += bool(keys)
            st["server"] += bool(ticket_names({"alert_fields": fields}))
            st["fields"].update(keys)
    order = [lay.name for lay in LAYOUTS] + ["unrecognised"]
    out = []
    for name in order:
        if name not in stats and name not in humans:
            continue
        st = stats[name]
        n = st["alerts"]
        out.append({"layout": name, "alerts": n, "parsed": st["parsed"], "server": st["server"],
                    "human_matches": humans.get(name, 0),
                    "top_fields": [(k, c / n) for k, c in st["fields"].most_common(top)] if n else []})
    return out


def render_coverage(rows: list[dict]) -> str:
    total = sum(r["alerts"] for r in rows)

    def pct(a, b):
        return "n/a" if not b else f"{a / b:.0%}" if a in (0, b) or 0.01 < a / b < 0.99 else f"{a / b:.1%}"

    table = [[r["layout"], f"{r['alerts']:,}", pct(r["parsed"], r["alerts"]), pct(r["server"], r["alerts"]),
              ", ".join(f"{k} {p:.0%}" for k, p in r["top_fields"]), r["human_matches"]] for r in rows]
    recognised = sum(r["alerts"] for r in rows if r["layout"] != "unrecognised")
    with_server = sum(r["server"] for r in rows)
    table.append(["All alerts", f"{total:,}", pct(sum(r["parsed"] for r in rows), total), pct(with_server, total),
                  f"recognised {pct(recognised, total)}", sum(r["human_matches"] for r in rows)])
    return md_table(["Layout", "Alerts", "Parsed", "With a server", "Most-filled fields", "Human tickets matched"],
                    table)


def main(argv=None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        raise SystemExit("usage: python -m tools.alert_parser TICKETS.jsonl [...]  (re-parses alert_fields in place)\n"
                         "       python -m tools.alert_parser coverage TICKETS.jsonl [...]")
    if args[0] == "coverage":
        print(render_coverage(coverage([t for p in args[1:] for t in read_jsonl(Path(p))])))
    else:
        for p in args:
            print(p, refresh_file(p))


if __name__ == "__main__":
    main()
