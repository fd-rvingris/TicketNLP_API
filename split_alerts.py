"""Split a DW2.0 export into alerts and human tickets by its is_alert column.

Reads a .csv export (as saved from SSMS) or a .jsonl ticket file from
load_export.py and writes <name>_alerts and <name>_human files of the same
format next to it, so each can be loaded, trained and scored on its own.
Tickets whose is_alert value is missing or unreadable go in neither file and
are counted. Prints counts only, never ticket text."""

import argparse
import csv
from pathlib import Path

from common import _flag, read_jsonl, write_jsonl
from load_export import COLUMN_ALIASES, clean

SEGMENTS = {True: "alerts", False: "human"}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path", type=Path, help="export file (.csv or .jsonl)")
    p.add_argument("--out-dir", type=Path, help="folder for the two files (default: next to the input)")
    return p.parse_args()


def output_paths(path: Path, out_dir: Path | None = None) -> dict[bool, Path]:
    folder = out_dir or path.parent
    return {flag: folder / f"{path.stem}_{name}{path.suffix}" for flag, name in SEGMENTS.items()}


def split_csv(path: Path, outputs: dict[bool, Path]) -> dict:
    """Copy each row unchanged into the alerts or human CSV. Returns counts per segment."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = {h.strip().lower(): h for h in reader.fieldnames or []}
        column = next((headers[a] for a in COLUMN_ALIASES["is_alert"] if a in headers), None)
        if not column:
            raise SystemExit(f"No is_alert column in {path}; expected one of {COLUMN_ALIASES['is_alert']}")
        rows = {True: [], False: [], None: []}
        for row in reader:
            rows[_flag(clean(row.get(column)))].append(row)
        fieldnames = reader.fieldnames
    for flag, out in outputs.items():
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows[flag])
    return {flag: len(r) for flag, r in rows.items()}


def split_jsonl(path: Path, outputs: dict[bool, Path]) -> dict:
    tickets = read_jsonl(path)
    groups = {True: [], False: [], None: []}
    for t in tickets:
        groups[_flag(t.get("is_alert"))].append(t)
    for flag, out in outputs.items():
        write_jsonl(out, groups[flag])
    return {flag: len(g) for flag, g in groups.items()}


def main():
    args = parse_args()
    if not args.path.exists():
        raise SystemExit(f"File not found: {args.path}")
    suffix = args.path.suffix.lower()
    if suffix not in (".csv", ".jsonl"):
        raise SystemExit("Expected a .csv export or a .jsonl ticket file")
    outputs = output_paths(args.path, args.out_dir)
    if args.path in outputs.values():
        raise SystemExit("The input is already a split file")
    counts = (split_csv if suffix == ".csv" else split_jsonl)(args.path, outputs)
    for flag, out in outputs.items():
        print(f"{SEGMENTS[flag]:>6}: {counts[flag]:,} tickets -> {out}")
    if counts[None]:
        print(f"warning: {counts[None]:,} tickets have no readable is_alert value and are in neither file")


if __name__ == "__main__":
    main()
