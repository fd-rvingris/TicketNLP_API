"""What serving the detector with a CMDB-hydrated directory snapshot changes, against the directory
saved in the model file (docs/prompt-6-ticket-api.md, "Effect on accuracy").

    python -m tools.directory_effect [--instances data/instances.csv] [--snapshot FILE] [--without-dw]

Builds a snapshot as python -m api.refresh_directory does with its default settings (the CMDB export,
the model's alert names and its dw_servers names; --without-dw leaves those out), unless --snapshot
gives one, and scores the saved model under both directories:

- hand labels: Claude's span labels on the test file and on the training holdout
  (tools/server_labels.py's evaluation), precision and recall per class;
- derived labels on the test file, as `python -m tools.server_detector evaluate` scores its standard
  test, at the model's thresholds;
- the detections that change on the test file (added, removed, other status or host).

Prints figures only, never names or text, and writes a Markdown report under data/reports/.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import ticket_analysis.server_detector as sd
import tools.server_labels as sl
from api.directory import CsvCmdbSource, NoCmdbSource, build_snapshot, snapshot_directory
from ticket_analysis.jsonl import read_jsonl
from ticket_analysis.paths import DATA_DIR, INSTANCES_PATH
from tools.common import REPORTS_DIR, fmt_pct, md_table
from tools.server_detector import HOLDOUT_LABELS_PATH, prf, score_tickets

LABEL_SETS = [("test file", sl.DATA_DIR / "labels" / "claude_spans.jsonl", DATA_DIR / "test_tickets.jsonl"),
              ("training holdout", HOLDOUT_LABELS_PATH, DATA_DIR / "train_tickets.jsonl")]


def _key(d: dict) -> tuple:
    return d["field"], d["start"], d["end"]


def detection_changes(tickets: list[dict], model, before, after) -> Counter:
    out = Counter()
    for t in tickets:
        a = {_key(d): d for d in sd.detect_servers(t, before, model)}
        b = {_key(d): d for d in sd.detect_servers(t, after, model)}
        out["detections before"] += len(a)
        out["detections after"] += len(b)
        removed, added = a.keys() - b.keys(), b.keys() - a.keys()
        out["removed"] += len(removed)
        out["added"] += len(added)
        changed = bool(removed or added)
        for k in a.keys() & b.keys():
            if (a[k]["kind"], a[k]["status"], a[k]["host"], a[k]["instance"]) != \
                    (b[k]["kind"], b[k]["status"], b[k]["host"], b[k]["instance"]):
                out["other kind, status or host"] += 1
                changed = True
            else:
                out["unchanged"] += 1
        out["tickets changed"] += changed
    return out


def hand_label_rows(name: str, r: dict) -> list[list[str]]:
    rows = []
    for cls in ("host", "instance"):
        sp = [s for s in r["spans"] if s["cls"] == cls]
        dt = [d for d in r["detections"] if d["cls"] == cls]
        rows.append([name, cls, sl._frac(sum(d["correct"] for d in dt), len(dt)),
                     sl._frac(sum(s["found"] for s in sp), len(sp))])
    return rows


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model-path", type=Path, default=sd.MODEL_PATH)
    p.add_argument("--instances", type=Path, default=INSTANCES_PATH, help="CMDB export (missing: none)")
    p.add_argument("--snapshot", type=Path, help="a snapshot file to use instead of building one")
    p.add_argument("--without-dw", action="store_true", help="leave out the model directory's dw_servers names")
    p.add_argument("--test", type=Path, default=DATA_DIR / "test_tickets.jsonl")
    p.add_argument("--train", type=Path, default=DATA_DIR / "train_tickets.jsonl")
    args = p.parse_args(argv)
    bundle = sd.load_model(args.model_path)
    model, saved = bundle["model"], bundle["directory"]
    if args.snapshot:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    else:
        cmdb = CsvCmdbSource(args.instances) if args.instances.exists() else NoCmdbSource()
        snapshot = build_snapshot(cmdb, saved, include_dw=not args.without_dw, model_version=bundle.get("created_at"))
    hydrated = snapshot_directory(snapshot)
    stats = {"saved": saved.stats(), "snapshot": hydrated.stats()}
    print(f"Snapshot: {snapshot['counts']['clients']:,} clients, {snapshot['counts']['records']:,} records "
          f"(CMDB {snapshot['sources']['cmdb']['records']:,} records; dw names "
          f"{'included' if snapshot['sources']['dw']['included'] else 'left out'})")

    hand = []
    for name, labels_path, tickets_path in LABEL_SETS:
        labels = sl.load_labels(labels_path)
        tickets = {str(t["ticket_id"]): t for t in read_jsonl(tickets_path)}
        if not labels or not tickets:
            continue
        for label, directory in (("saved", saved), ("snapshot", hydrated)):
            r = sl.evaluate_labels(labels, tickets, {"model": model, "directory": directory})
            hand += [[label] + row for row in hand_label_rows(name, r)]

    test = read_jsonl(args.test)
    oracle = sd.LocalServerDirectory.from_sources(read_jsonl(args.train) + test, INSTANCES_PATH)
    derived = []
    for label, directory in (("saved", saved), ("snapshot", hydrated)):
        recs, _ = score_tickets(model, test, directory, oracle)
        for cls in ("host", "instance"):
            for seg in ("alerts", "human"):
                m = prf([r for r in recs if r["seg"] == seg], cls, getattr(model, f"{cls}_threshold"))
                derived.append([label, cls, seg, fmt_pct(m["precision"]), fmt_pct(m["recall"]), fmt_pct(m["f1"])])
    changes = detection_changes(test, model, saved, hydrated)

    lines = [f"# Directory effect, {datetime.now():%Y-%m-%d %H:%M}", "",
             f"Model {bundle.get('created_at')}; snapshot {snapshot['createdAt']} from "
             f"{snapshot['sources']['cmdb']['kind']} ({snapshot['sources']['cmdb']['records']:,} CMDB records), "
             f"alert names, dw names {'included' if snapshot['sources']['dw']['included'] else 'left out'}.", "",
             "## Directories", "",
             md_table(["", *stats["saved"]], [[k, *[f"{v:,}" for v in s.values()]] for k, s in stats.items()]), "",
             "## Hand labels", "", md_table(["directory", "labels", "class", "precision", "recall"], hand), "",
             "## Derived labels on the test file", "",
             md_table(["directory", "class", "segment", "precision", "recall", "F1"], derived), "",
             "## Detections that change on the test file", "",
             md_table(["", "count"], [[k, f"{v:,}"] for k, v in changes.items()]), ""]
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"directory-effect-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    for row in hand:
        print("  hand    " + "  ".join(f"{c:>20}" for c in row))
    for row in derived:
        print("  derived " + "  ".join(f"{c:>9}" for c in row))
    print("  changes: " + ", ".join(f"{k} {v:,}" for k, v in changes.items()))
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
