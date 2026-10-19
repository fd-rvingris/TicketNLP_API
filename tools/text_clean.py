"""Measure what ticket_analysis.text_clean removes from the ticket files, per pattern, as counts only.

    python -m tools.text_clean [TICKETS.jsonl ...]
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

from common import fmt_pct
from ticket_analysis.jsonl import read_jsonl
from ticket_analysis.paths import DATA_DIR
from ticket_analysis.text_clean import PATTERNS, removed_chars

TICKET_FILES = [DATA_DIR / "train_tickets.jsonl", DATA_DIR / "test_tickets.jsonl"]


# --- Measurement ---------------------------------------------------------------------------


def measure(tickets: list[dict]) -> dict:
    """Per pattern: tickets touched and median characters removed where touched, plus the totals."""
    per, total, fallback = {p: [] for p in PATTERNS}, [], 0
    for t in tickets:
        r = removed_chars(t.get("subject") or "", t.get("description") or "")
        fallback += r.pop("fallback", 0)
        for p, n in r.items():
            per[p].append(n)
        if r:
            total.append(sum(r.values()))
    return {"tickets": len(tickets), "total": total, "per": per, "fallback": fallback}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", type=Path, default=TICKET_FILES)
    args = p.parse_args(argv)
    tickets = [t for f in args.files if f.exists() for t in read_jsonl(f)]
    if not tickets:
        raise SystemExit("No tickets found")
    for name, subset in (("all", tickets), ("human", [t for t in tickets if t.get("is_alert") is False]),
                         ("alerts", [t for t in tickets if t.get("is_alert") is True])):
        if not subset:
            continue
        m = measure(subset)
        n = m["tickets"]
        print(f"{name}: {n:,} tickets, {fmt_pct(len(m['total']) / n)} touched, median "
              f"{statistics.median(m['total']) if m['total'] else 0:,.0f} characters removed where touched, "
              f"{m['fallback']:,} fell back to the raw text")
        for pat in PATTERNS:
            hits = m["per"][pat]
            print(f"  {pat:<9} {fmt_pct(len(hits) / n):>6} of tickets, median "
                  f"{statistics.median(hits) if hits else 0:>6,.0f} characters")


if __name__ == "__main__":
    main()
