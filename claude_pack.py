"""Files for labeling and taxonomy work in a Claude chat (claude.ai), and import of what it returns.

  python claude_pack.py labels       # data/claude_pack/labels/batch_NNN.jsonl to upload, in batches
  python claude_pack.py taxonomy     # data/claude_pack/taxonomy/ digest of every ticket, to upload
  python claude_pack.py import-labels data/claude_pack/returned/*.jsonl
  python claude_pack.py check-taxonomy data/claude_pack/returned/taxonomy_v1.json

Everything written is masked with common.mask_secrets, as text sent to the model is. Imported
labels go to data/labels/claude_spans.jsonl (not the DBA label file), in the server_labels row
format, so `python server_labels.py evaluate --labels data/labels/claude_spans.jsonl` scores the
detector against them. The prompts are docs/claude-labeling-prompt.md and
docs/claude-taxonomy-prompt.md. Prints counts only, never ticket text.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import server_detector as sd
import server_labels as sl
from common import DATA_DIR, mask_secrets, now_iso, read_jsonl, write_jsonl

PACK_DIR = DATA_DIR / "claude_pack"
CLAUDE_LABELS_PATH = DATA_DIR / "labels" / "claude_spans.jsonl"
TICKET_FILES = ("train_tickets.jsonl", "test_tickets.jsonl")


def masked(text: str) -> str:
    return mask_secrets(text or "")[0]


def weak(t: dict, key: str) -> str:
    return str((t.get("weak_labels") or {}).get(key) or "")


# --- Labeling batches ------------------------------------------------------------


def label_record(t: dict, max_chars: int) -> dict:
    description = t.get("description") or ""
    return {"ticket_id": str(t["ticket_id"]), "kind": "alert" if sd.is_alert_ticket(t) else "human",
            "client": t.get("organization_name"), "subject": masked(t.get("subject") or ""),
            "description": masked(description[:max_chars]), "truncated": len(description) > max_chars}


def batches(records: list[dict], max_batch_chars: int, max_batch_tickets: int) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for r in records:
        n = len(r["subject"]) + len(r["description"])
        if cur and (size + n > max_batch_chars or len(cur) >= max_batch_tickets):
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    if cur:
        out.append(cur)
    return out


def cmd_labels(args) -> None:
    test = read_jsonl(DATA_DIR / "test_tickets.jsonl")
    train = read_jsonl(DATA_DIR / "train_tickets.jsonl")
    if not test:
        raise SystemExit("Missing data/test_tickets.jsonl")
    # The same human sample the DBA Label tab hands out, so Claude and DBA labels can be compared.
    picked = sl.sample_tickets(test, args.test_human, args.seed, "human")
    picked += sl.sample_tickets(train, args.train_human, args.seed, "human")
    picked += sl.sample_tickets(test, args.alerts, args.seed, "alerts")
    records = [label_record(t, args.max_chars) for t in picked]
    out_dir = PACK_DIR / "labels"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("batch_*.jsonl"):
        old.unlink()
    groups = batches(records, args.batch_chars, args.batch_tickets)
    for i, group in enumerate(groups, 1):
        write_jsonl(out_dir / f"batch_{i:03d}.jsonl", group)
    kinds = Counter(r["kind"] for r in records)
    print(f"{len(records):,} tickets ({kinds['human']:,} human, {kinds['alert']:,} alerts) in {len(groups)} "
          f"batches under {out_dir}")
    print("Upload one batch per message with docs/claude-labeling-prompt.md; save each reply as "
          "data/claude_pack/returned/labels_NNN.jsonl")


# --- Taxonomy digest ---------------------------------------------------------------


def cmd_taxonomy(args) -> None:
    tickets = []
    for name in TICKET_FILES:
        rows = read_jsonl(DATA_DIR / name)
        if not rows:
            raise SystemExit(f"Missing data/{name}")
        tickets += [{**t, "_file": name} for t in rows]
    out_dir = PACK_DIR / "taxonomy"
    out_dir.mkdir(parents=True, exist_ok=True)
    human = [t for t in tickets if t.get("is_alert") is False]
    alerts = [t for t in tickets if sd.is_alert_ticket(t)]

    # 1. Every human ticket's subject with its Zendesk labels: the whole population, compactly.
    with (out_dir / "human_subjects.tsv").open("w", encoding="utf-8", newline="") as f:
        f.write("ticket_id\tzendesk_type\tcategory\tsub_type\tsubject\n")
        for t in human:
            cells = [str(t["ticket_id"]), weak(t, "zendesk_type"), weak(t, "category"), weak(t, "sub type"),
                     masked(t.get("subject") or "")]
            f.write("\t".join(re.sub(r"[\t\r\n]+", " ", c) for c in cells) + "\n")

    # 2. Alerts are templated: counts per family, alert name and Zendesk category instead of text.
    counts = Counter(((t.get("alert_fields") or {}).get("family") or "unparsed",
                      (t.get("alert_fields") or {}).get("alert_name") or "", weak(t, "category")) for t in alerts)
    with (out_dir / "alert_names.tsv").open("w", encoding="utf-8", newline="") as f:
        f.write("family\talert_name\tcategory\ttickets\n")
        for (family, name, cat), n in counts.most_common():
            f.write("\t".join(re.sub(r"[\t\r\n]+", " ", str(c)) for c in (family, name, cat, n)) + "\n")

    # 3. A sample of human tickets with the start of their text, spread across Zendesk categories.
    by_cat = defaultdict(list)
    for t in sorted(human, key=lambda t: str(t["ticket_id"])):
        by_cat[weak(t, "category") or "(none)"].append(t)
    import random
    rng = random.Random(args.seed)
    for rows in by_cat.values():
        rng.shuffle(rows)
    sample, i = [], 0
    while len(sample) < args.sample and any(len(rows) > i for rows in by_cat.values()):
        for rows in by_cat.values():
            if i < len(rows) and len(sample) < args.sample:
                sample.append(rows[i])
        i += 1
    write_jsonl(out_dir / "human_sample.jsonl",
                [{"ticket_id": str(t["ticket_id"]), "zendesk_type": weak(t, "zendesk_type"),
                  "category": weak(t, "category"), "subject": masked(t.get("subject") or ""),
                  "description_start": masked((t.get("description") or "")[:args.sample_chars])} for t in sample])

    # 4. Label counts and the current draft taxonomy, for reference.
    summary = {"generated_at": now_iso(), "tickets": len(tickets), "human": len(human), "alerts": len(alerts),
               "zendesk_type": Counter(weak(t, "zendesk_type") or "(none)" for t in tickets).most_common(),
               "category_human": Counter(weak(t, "category") or "(none)" for t in human).most_common(),
               "category_alerts": Counter(weak(t, "category") or "(none)" for t in alerts).most_common(),
               "alert_family": Counter((t.get("alert_fields") or {}).get("family") or "unparsed"
                                       for t in alerts).most_common()}
    (out_dir / "label_counts.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    (out_dir / "taxonomy_v0.json").write_text((Path(__file__).parent / "taxonomy.json").read_text(encoding="utf-8"),
                                              encoding="utf-8")
    sizes = {p.name: p.stat().st_size for p in sorted(out_dir.iterdir())}
    print(f"{len(tickets):,} tickets ({len(human):,} human, {len(alerts):,} alerts) digested into {out_dir}:")
    for name, size in sizes.items():
        print(f"  {name:22} {size / 1e6:6.2f} MB")
    print("Upload all of them with docs/claude-taxonomy-prompt.md")


# --- Import ------------------------------------------------------------------------


def find_all(text: str, needle: str) -> list[int]:
    """Start offsets of needle in text where it is not part of a longer name; exact case first,
    then ignoring case. A name may follow '$' (MSSQL$REPORTS) or a UNC path's double backslash,
    but not a single backslash, which makes it the instance half of HOST\\INSTANCE."""
    for flags in (0, re.I):
        pattern = re.compile(rf"(?<![A-Za-z0-9_\-.#]){re.escape(needle)}(?![A-Za-z0-9_\-#])", flags)
        starts = [m.start() for m in pattern.finditer(text)
                  if text[max(0, m.start() - 1):m.start()] != "\\" or text[max(0, m.start() - 2):m.start()] == "\\\\"]
        if starts:
            return starts
    return []


def spans_from_mentions(ticket: dict, mentions: list[dict], max_chars: int) -> tuple[list[dict], list[str]]:
    """Every occurrence of each returned mention, as server_labels spans. Longer mentions claim
    their text first, so SQL01\\REPORTS wins over SQL01 inside it."""
    spans, problems = [], []
    for m in sorted(mentions, key=lambda m: -len(str(m.get("text") or ""))):
        text, kind = str(m.get("text") or "").strip(), m.get("kind")
        if kind not in sl.KINDS or not text:
            problems.append(f"bad mention kind or text ({kind})")
            continue
        # A stated field is searched first, and the other only when it has no match (chats mix them
        # up); with no field stated, both are searched.
        stated = m.get("field") if m.get("field") in ("subject", "description") else None
        groups = [["subject", "description"]] if stated is None else \
            [[stated], ["description" if stated == "subject" else "subject"]]
        occurs = False
        for fields in groups:
            for field in fields:
                haystack = sl.field_text(ticket, field, max_chars)
                for start in find_all(haystack, text):
                    occurs = True
                    span = {"field": field, "start": start, "end": start + len(text), "kind": kind,
                            "host": m.get("host")}
                    # Already marked by another mention (a longer name, or the same name listed twice)
                    if not any(s["field"] == field and s["start"] < span["end"] and span["start"] < s["end"]
                               for s in spans):
                        spans.append(span)
            if occurs:
                break
        if not occurs:
            problems.append("mention not found in the ticket text")
    return spans, problems


def cmd_import_labels(args) -> None:
    tickets = {str(t["ticket_id"]): t for name in TICKET_FILES for t in read_jsonl(DATA_DIR / name)}
    stats, problems = Counter(), Counter()
    for path in args.files:
        for n, row in enumerate(read_jsonl(path), 1):
            tid = str(row.get("ticket_id") or "")
            t = tickets.get(tid)
            if t is None:
                problems["unknown ticket_id"] += 1
                continue
            status = "skipped" if row.get("status") == "skipped" else "done"
            spans, issues = spans_from_mentions(t, row.get("mentions") or [], sd.DEFAULT_MAX_CHARS)
            problems.update(issues)
            try:
                label = sl.normalize_row({"status": status, "spans": spans, "notes": row.get("notes")}, t,
                                         args.labeler)
            except ValueError as e:
                problems[f"rejected: {e}"[:80]] += 1
                continue
            if not args.dry_run:
                sl.save_label(label, args.out)
            stats[status] += 1
            stats["spans"] += len(label["spans"])
            stats.update(f"kind {s['kind']}" for s in label["spans"])
    print(f"Imported {stats['done']:,} labeled and {stats['skipped']:,} skipped tickets, {stats['spans']:,} spans "
          f"({', '.join(f'{k[5:]} {v:,}' for k, v in stats.items() if k.startswith('kind '))})"
          + (" [dry run, nothing written]" if args.dry_run else f" into {args.out}"))
    for issue, n in problems.most_common():
        print(f"  {n:,} x {issue}")


def cmd_check_taxonomy(args) -> None:
    """Check a returned taxonomy has the shape taxonomy.json and schema.py expect."""
    t = json.loads(Path(args.file).read_text(encoding="utf-8"))
    errors = []
    if not str(t.get("version") or "").strip():
        errors.append("missing version")
    for key in ("types", "intents"):
        items = t.get(key)
        if not isinstance(items, list) or not items:
            errors.append(f"{key} must be a non-empty list")
            continue
        ids = [i.get("id") for i in items]
        if len(set(ids)) != len(ids):
            errors.append(f"duplicate {key} ids")
        for i in items:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", str(i.get("id") or "")):
                errors.append(f"{key} id not snake_case: {i.get('id')!r}")
            for field in ("label", "definition"):
                if not str(i.get(field) or "").strip():
                    errors.append(f"{key} {i.get('id')!r} has no {field}")
        if not any(i.get("id") == "unknown" for i in items):
            errors.append(f"{key} has no 'unknown' entry (classify.py expects one)")
    print("taxonomy OK: " + ", ".join(f"{len(t[k])} {k}" for k in ("types", "intents")) if not errors
          else "taxonomy problems:\n  " + "\n  ".join(errors))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    lab = sub.add_parser("labels", help="write labeling batches to upload")
    lab.add_argument("--test-human", type=int, default=200, help="the DBA Label tab's sample from the test file")
    lab.add_argument("--train-human", type=int, default=0, help="extra human tickets from the training file")
    lab.add_argument("--alerts", type=int, default=0, help="alert tickets from the test file")
    lab.add_argument("--seed", type=int, default=0, help="must match detector_ui --label-seed to share its sample")
    lab.add_argument("--max-chars", type=int, default=sd.DEFAULT_MAX_CHARS)
    lab.add_argument("--batch-chars", type=int, default=60_000, help="text characters per batch file")
    lab.add_argument("--batch-tickets", type=int, default=25)
    tax = sub.add_parser("taxonomy", help="write the taxonomy digest to upload")
    tax.add_argument("--sample", type=int, default=600, help="human tickets with text, spread across categories")
    tax.add_argument("--sample-chars", type=int, default=800, help="description characters per sampled ticket")
    tax.add_argument("--seed", type=int, default=0)
    imp = sub.add_parser("import-labels", help="import labels returned by Claude")
    imp.add_argument("files", nargs="+", type=Path)
    imp.add_argument("--labeler", default="claude", help="name recorded with the labels")
    imp.add_argument("--out", type=Path, default=CLAUDE_LABELS_PATH)
    imp.add_argument("--dry-run", action="store_true")
    chk = sub.add_parser("check-taxonomy", help="validate a returned taxonomy file")
    chk.add_argument("file", type=Path)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    {"labels": cmd_labels, "taxonomy": cmd_taxonomy, "import-labels": cmd_import_labels,
     "check-taxonomy": cmd_check_taxonomy}[args.command](args)


if __name__ == "__main__":
    main()
