"""Compare two or more runs side by side on the tickets they share, and write
a Markdown report to data/reports/."""

import argparse
from datetime import datetime

from common import (REPORTS_DIR, compute_metrics, fmt_pct, label_metrics, latest_rows, md_table, now_iso,
                    read_jsonl, read_labels, resolve_run, summary_rows)
from schema import TAXONOMY

TYPE_IDS = [t["id"] for t in TAXONOMY["types"]]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs", nargs="+", required=True, help="two or more run names")
    args = p.parse_args()
    if len(args.runs) < 2:
        p.error("--runs needs at least two run names")
    return args


def agreement(base: dict, other: dict, field: str) -> tuple[float | None, int]:
    both = [tid for tid in base if base[tid].get("output") and other.get(tid, {}).get("output")]
    same = sum(1 for tid in both if base[tid]["output"][field] == other[tid]["output"][field])
    return (same / len(both) if both else None), len(both)


def main():
    args = parse_args()
    runs = {name: latest_rows(read_jsonl(resolve_run(name) / "results.jsonl")) for name in args.runs}
    shared = set.intersection(*(set(rows) for rows in runs.values()))
    if not shared:
        raise SystemExit("These runs share no tickets.")
    runs = {name: {tid: rows[tid] for tid in sorted(shared)} for name, rows in runs.items()}
    labels = read_labels()

    columns = {name: summary_rows(compute_metrics(list(rows.values())), label_metrics(rows, labels))
               for name, rows in runs.items()}
    metric_names = []
    for rows in columns.values():  # accuracy rows appear only for runs with labels
        metric_names += [n for n, _ in rows if n not in metric_names]
    table = [[n] + [dict(columns[name]).get(n, "n/a") for name in runs] for n in metric_names]

    base_name, *others = list(runs)
    agree_rows = []
    for name in others:
        for field in ("primary_type", "intent"):
            rate, n = agreement(runs[base_name], runs[name], field)
            agree_rows.append([f"{name} vs {base_name}", field, fmt_pct(rate), n])

    type_counts = {name: compute_metrics(list(rows.values()))["types"] for name, rows in runs.items()}
    dist_rows = [[t] + [type_counts[name][t] for name in runs] for t in TYPE_IDS
                 if any(type_counts[name][t] for name in runs)]

    lines = [
        f"# Run comparison: {', '.join(runs)}",
        "",
        f"Generated {now_iso()} on {len(shared)} shared tickets. Costs are estimates from the price table in "
        "common.py, which needs confirmation.",
        "",
        md_table(["metric"] + list(runs), table),
        "",
        "## Agreement",
        "",
        md_table(["runs", "field", "agreement", "tickets"], agree_rows),
        "",
        "## Primary type distribution",
        "",
        md_table(["type"] + list(runs), dist_rows),
        "",
    ]
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"compare-{'-vs-'.join(runs)}-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text("\n".join(lines), encoding="utf-8")

    width = max(len(n) for n in metric_names)
    print(f"{'metric':<{width}}  " + "  ".join(f"{name:>22}" for name in runs))
    for row in table:
        print(f"{row[0]:<{width}}  " + "  ".join(f"{v:>22}" for v in row[1:]))
    for row in agree_rows:
        print(f"Agreement {row[0]} on {row[1]}: {row[2]} of {row[3]}")
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
