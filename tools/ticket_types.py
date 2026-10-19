"""Train the taxonomy classifier (ticket_analysis.ticket_types) and build the detector UI's taxonomy summary.

    python -m tools.ticket_types train [--labels FILE ...] [--model-path PATH]

Fits on Claude's taxonomy sample labels plus later Claude Desktop rounds (LABEL_PATHS) and saves
data/models/ticket_types.joblib. Prints counts only, never ticket text. Nothing leaves this machine.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import joblib

from ticket_analysis.jsonl import now_iso, read_jsonl
from ticket_analysis.paths import DATA_DIR, REPO_ROOT
from ticket_analysis.taxonomy import TAXONOMY, TAXONOMY_VERSION
from ticket_analysis.ticket_types import MAX_CHARS, MODEL_CLEAN, MODEL_PATH, RETURNED_DIR, features, load_alert_map
from tools.baseline import build_model, read_labels

LABELS_PATH = RETURNED_DIR / "taxonomy_sample_labels.jsonl"
# Type labels from later Claude Desktop rounds (claude_pack.py types / import-types); they override the
# taxonomy sample for the same ticket
CLAUDE_TYPES_PATH = DATA_DIR / "labels" / "claude_types.jsonl"
LABEL_PATHS = (LABELS_PATH, CLAUDE_TYPES_PATH)
PROMPT_PATH = REPO_ROOT / "prompts" / "classify_v2.md"
TICKET_FILES = [DATA_DIR / "train_tickets.jsonl", DATA_DIR / "test_tickets.jsonl"]
TARGETS = ("primary_type", "intent")


# --- Model -----------------------------------------------------------------------------


def train(tickets: list[dict], labels: dict[str, dict], seed: int = 0) -> dict:
    """A type model and an intent model fitted on the labeled tickets found in tickets."""
    rows = [(t, labels[str(t["ticket_id"])]) for t in tickets if str(t["ticket_id"]) in labels]
    if not rows:
        raise ValueError("None of the labeled tickets are in the ticket files")
    X = features([t for t, _ in rows], MAX_CHARS, clean=MODEL_CLEAN)
    models = {target: build_model(seed).fit(X, [lab[target] for _, lab in rows]) for target in TARGETS}
    return {"models": models, "tickets": len(rows), "created_at": now_iso(), "taxonomy_version": TAXONOMY_VERSION,
            "clean": MODEL_CLEAN, "counts": {target: Counter(lab[target] for _, lab in rows) for target in TARGETS}}


def current_labels(paths=LABEL_PATHS) -> dict[str, dict]:
    """Merged labels from the label files (later files win), leaving out rows made for another
    taxonomy version. Rows without a version (the original taxonomy sample) count as current."""
    return {k: r for k, r in read_labels([p for p in paths if Path(p).exists()]).items()
            if r.get("taxonomy_version", TAXONOMY_VERSION) == TAXONOMY_VERSION}


def load_or_train(model_path: Path = MODEL_PATH, labels_path=LABEL_PATHS,
                  ticket_files=TICKET_FILES, tickets: list[dict] | None = None, log=print) -> dict | None:
    """The saved model when it matches the current taxonomy and no label file changed since it was
    saved, else one trained now and saved; None when there are no labels. labels_path is one label
    file or several (later files win)."""
    paths = [Path(labels_path)] if isinstance(labels_path, (str, Path)) else [Path(p) for p in labels_path]
    present = [p for p in paths if p.exists()]
    if model_path.exists():
        bundle = joblib.load(model_path)
        newer = [p.name for p in present if p.stat().st_mtime > model_path.stat().st_mtime]
        current = bundle.get("taxonomy_version") == TAXONOMY_VERSION and bundle.get("clean", False) == MODEL_CLEAN
        if current and not newer:
            return bundle
        log(f"Ticket type model is for taxonomy {bundle.get('taxonomy_version')} with cleaning "
            f"{bundle.get('clean', False)}" + (f"; newer labels in {', '.join(newer)}" if newer else "")
            + "; retraining")
    labels = current_labels(present)
    if not labels:
        log(f"Ticket types: no labels in {', '.join(map(str, paths))}; the Classify tab is off")
        return None
    if tickets is None:
        tickets = [t for p in ticket_files if Path(p).exists() for t in read_jsonl(p)]
    bundle = train(tickets, labels)
    bundle["labels"] = [str(p) for p in present]
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, model_path)
    log(f"Ticket types: trained on {bundle['tickets']:,} labeled tickets, saved to {model_path}")
    return bundle


# --- Taxonomy summary for the page -----------------------------------------------------


def prompt_rules(path: Path = PROMPT_PATH) -> list[str]:
    """The primary-type rules and tie-breaks of the classify prompt, one line each."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if "Choosing the primary type" not in text:
        return []
    block = text.split("Choosing the primary type", 1)[1].split("Ticket types:", 1)[0]
    return [re.sub(r"^(\d+\.|-)\s*", "", line.strip()) for line in block.splitlines()
            if re.match(r"\s*(\d+\.|-)\s", line)]


def summary(bundle: dict | None, alert_map: dict) -> dict:
    """Types and intents with their definitions, Claude's sample label counts and the alert tickets the
    table maps to each type."""
    counts = (bundle or {}).get("counts") or {}
    alert_tickets = Counter()
    for e in alert_map.values():
        alert_tickets[e["type"]] += e["tickets"]
    return {"version": TAXONOMY_VERSION,
            "types": [{**t, "sample": counts.get("primary_type", {}).get(t["id"], 0),
                       "alert_tickets": alert_tickets.get(t["id"], 0)} for t in TAXONOMY["types"]],
            "intents": [{**i, "sample": counts.get("intent", {}).get(i["id"], 0)} for i in TAXONOMY["intents"]],
            "sample_tickets": (bundle or {}).get("tickets", 0), "alert_names": len(alert_map),
            "alert_table_tickets": sum(alert_tickets.values()), "rules": prompt_rules()}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    tr = sub.add_parser("train", help="fit on the label file and save the model")
    tr.add_argument("--labels", type=Path, nargs="+", default=list(LABEL_PATHS),
                    help="label files, later ones winning for the same ticket")
    tr.add_argument("--model-path", type=Path, default=MODEL_PATH)
    args = p.parse_args(argv)
    if args.model_path.exists():
        args.model_path.unlink()  # train always refits
    bundle = load_or_train(args.model_path, args.labels)
    if bundle:
        print(f"Alert table: {len(load_alert_map()):,} alert names")


if __name__ == "__main__":
    main()
