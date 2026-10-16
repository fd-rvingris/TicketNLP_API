"""Metrics for one run, the labeling sheet (data/labels.csv), and a Markdown
report in data/reports/."""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from common import (LABELS_PATH, PRICES_PER_MTOK, REPORTS_DIR, TICKETS_PATH, compute_metrics, fmt_pct,
                    label_metrics, latest_rows, md_table, now_iso, read_jsonl, read_labels, resolve_run,
                    summary_rows, write_labels)
from schema import TAXONOMY

TYPE_IDS = [t["id"] for t in TAXONOMY["types"]]
INTENT_IDS = [i["id"] for i in TAXONOMY["intents"]]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", default="latest", help="run name, or 'latest' (default)")
    return p.parse_args()


def update_labels(run_name: str, rows_by_id: dict, tickets: dict, path: Path = LABELS_PATH) -> dict:
    """Add or refresh the predicted columns; never touch the human columns."""
    labels = read_labels(path)
    for tid, row in rows_by_id.items():
        out = row.get("output")
        if not out:
            continue
        entry = labels.setdefault(tid, {"ticket_id": tid})
        entry.update(
            subject=tickets.get(tid, {}).get("subject", ""),
            predicted_run=run_name,
            predicted_type=out["primary_type"],
            predicted_intent=out["intent"],
            predicted_confidence=out["confidence"],
            predicted_mentions="; ".join(m["text"] for m in row["matches"]),
        )
    try:
        write_labels(labels, path)
    except PermissionError:
        print(f"warning: could not write {path} (is it open in Excel?); using the existing file")
    return labels


def distribution(counter: Counter, order: list[str]) -> str:
    total = sum(counter.values())
    rows = [[k, counter[k], fmt_pct(counter[k] / total if total else None)] for k in order if counter[k]]
    return md_table(["value", "tickets", "share"], rows)


def confusion_table(field: dict) -> str:
    classes = field["classes"]
    rows = [[f"**{t}**"] + [field["confusion"][(t, p)] or "" for p in classes] for t in classes]
    return md_table(["label \\ predicted"] + classes, rows)


def per_class_table(field: dict) -> str:
    rows = [[c, fmt_pct(v["precision"]), fmt_pct(v["recall"]), v["support"]] for c, v in field["per_class"].items()]
    return md_table(["class", "precision", "recall", "labeled"], rows)


def weak_label_section(rows_by_id: dict, tickets: dict) -> list[str]:
    """Cross-tabulate each weak label field from Zendesk against the predicted type."""
    by_key = defaultdict(Counter)
    for tid, row in rows_by_id.items():
        if row.get("output"):
            for key, value in (tickets.get(tid, {}).get("weak_labels") or {}).items():
                by_key[key][(str(value), row["output"]["primary_type"])] += 1
    if not by_key:
        return ["No weak labels were found on these tickets."]
    out = []
    for key, pairs in sorted(by_key.items()):
        values = Counter()
        predicted = Counter()
        for (v, p), n in pairs.items():
            values[v] += n
            predicted[p] += n
        columns = [p for p, _ in predicted.most_common()]
        rows = [[v] + [pairs[(v, p)] or "" for p in columns] for v, _ in values.most_common(15)]
        out += [f"### {key}", "", md_table([f"{key} \\ predicted type"] + columns, rows), ""]
        comparable = [(v, p, n) for (v, p), n in pairs.items() if v in TYPE_IDS]
        if comparable:
            agree = sum(n for v, p, n in comparable if v == p)
            total = sum(n for _, _, n in comparable)
            out += [f"Agreement where the value is a taxonomy type id: {fmt_pct(agree / total)} of {total}.", ""]
    return out


def render(run_name: str, config: dict, rows_by_id: dict, tickets: dict, metrics: dict, lm: dict) -> str:
    rows = list(rows_by_id.values())
    lines = [
        f"# Classification report: {run_name}",
        "",
        f"Generated {now_iso()}. Model {config.get('model')}, effort {config.get('effort')}, "
        f"prompt {config.get('prompt_version')}, taxonomy {config.get('taxonomy_version')}.",
        "",
        "Costs are estimates from the price table in common.py, which needs confirmation against current "
        "Anthropic pricing. Rows served by a fallback model are priced at that model's rates.",
        "",
        "## Summary",
        "",
        md_table(["metric", "value"], [list(r) for r in summary_rows(metrics, lm)]),
        "",
        "## Primary type",
        "",
        distribution(metrics["types"], TYPE_IDS),
        "",
        "## Intent",
        "",
        distribution(metrics["intents"], INTENT_IDS),
        "",
        "## Confidence",
        "",
        distribution(metrics["confidence"], ["high", "medium", "low"]),
        "",
        "## Mentions and CMDB matching",
        "",
        f"{metrics['mentions_returned']} mentions returned, {metrics['mentions_dropped']} dropped because the "
        f"evidence quote was not in the ticket ({fmt_pct(metrics['quote_drop_rate'])}). "
        f"{metrics['tickets_with_cmdb']} classified tickets have CMDB instances for their client; their "
        f"{metrics['host_mentions_with_cmdb']} server/instance mentions: "
        + ", ".join(f"{k} {v}" for k, v in sorted(metrics["match_status"].items())) + ".",
        "",
    ]
    if metrics["refusal_categories"]:
        lines += ["## Refusals", "",
                  md_table(["category", "tickets"], [[k, v] for k, v in metrics["refusal_categories"].items()]), ""]

    lines += ["## Accuracy against human labels", ""]
    labeled = any((lm.get(f) or {}).get("labeled") for f in ("type", "intent"))
    if not labeled:
        lines += ["No human labels yet. Fill in correct_type, correct_intent, and mentions_correct in "
                  "data/labels.csv, then run the report again.", ""]
    else:
        if lm["invalid_labels"]:
            lines += [f"{lm['invalid_labels']} label value(s) are not taxonomy ids and were ignored.", ""]
        if lm["mentions_correct"]:
            lines += ["Mentions judged correct: " + ", ".join(f"{k} {v}" for k, v in lm["mentions_correct"].most_common()), ""]
        for field in ("type", "intent"):
            f = lm[field]
            if not f["labeled"]:
                continue
            lines += [f"### {field.title()}: {fmt_pct(f['accuracy'])} of {f['labeled']}", "",
                      confusion_table(f), "", per_class_table(f), ""]

    lines += ["## Comparison with Zendesk fields (weak labels)", ""] + weak_label_section(rows_by_id, tickets)

    attention = [r for r in rows if r.get("error") or (r.get("output") or {}).get("primary_type") == "unknown"]
    if attention:
        lines += ["## Tickets to review", "",
                  md_table(["ticket", "subject", "result"],
                           [[r["ticket_id"], tickets.get(r["ticket_id"], {}).get("subject", "")[:80],
                             (r["error"] or "").split(":")[0] or "unknown type"] for r in attention]), ""]
    return "\n".join(lines)


def main():
    args = parse_args()
    run_dir = resolve_run(args.run)
    run_name = run_dir.name
    config_path = run_dir / "run.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    rows_by_id = latest_rows(read_jsonl(run_dir / "results.jsonl"))
    tickets = {t["ticket_id"]: t for t in read_jsonl(TICKETS_PATH)}
    unpriced = {r.get("served_model") for r in rows_by_id.values()} - set(PRICES_PER_MTOK) - {None}
    if unpriced:
        print(f"warning: no price for {', '.join(sorted(unpriced))}; add it to PRICES_PER_MTOK in common.py")

    labels = update_labels(run_name, rows_by_id, tickets)
    metrics = compute_metrics(list(rows_by_id.values()))
    lm = label_metrics(rows_by_id, labels)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{run_name}-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(render(run_name, config, rows_by_id, tickets, metrics, lm), encoding="utf-8")

    width = max(len(name) for name, _ in summary_rows(metrics, lm))
    print(f"Run {run_name}")
    for name, value in summary_rows(metrics, lm):
        print(f"  {name:<{width}}  {value}")
    print(f"Labels: {LABELS_PATH}")
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
