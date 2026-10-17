"""Apply the ticket taxonomy locally, without an LLM.

Alerts take their type from the alert-name table in the taxonomy notes
(data/claude_pack/returned/taxonomy_notes.md, "Alert name to v1 type"), looked up by the parsed alert
family and name. Other tickets, and alerts the table does not decide, are scored by TF-IDF and logistic
regression trained on a label file (Claude's taxonomy sample labels by default), the same features as
baseline.py. Used by detector_ui.py's Classify tab.

Commands: train (fit on the label file and save data/models/ticket_types.joblib). Prints counts only,
never ticket text. Nothing leaves this machine.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import joblib

from alert_parser import parse_alert
from baseline import build_model, features
from common import DATA_DIR, now_iso, read_jsonl
from schema import TAXONOMY, TAXONOMY_VERSION

RETURNED_DIR = DATA_DIR / "claude_pack" / "returned"
NOTES_PATH = RETURNED_DIR / "taxonomy_notes.md"
LABELS_PATH = RETURNED_DIR / "taxonomy_sample_labels.jsonl"
MODEL_PATH = DATA_DIR / "models" / "ticket_types.joblib"
PROMPT_PATH = Path(__file__).parent / "prompts" / "classify_v2.md"
TICKET_FILES = [DATA_DIR / "train_tickets.jsonl", DATA_DIR / "test_tickets.jsonl"]
TARGETS = ("primary_type", "intent")
ALERT_FAMILIES = {"collector", "firing", "backup_exception_report", "volume_capacity", "unparsed"}
MAX_CHARS = 8000
TOP_K = 3
# The taxonomy's alert intent rule: fix_incident unless the alert says the condition cleared
RESOLVED_RE = re.compile(r"(?i)\b(cleared|resolved|succeeded|recovered|back to normal)\b")


# --- Alert-name table ------------------------------------------------------------------


def load_alert_map(path: Path = NOTES_PATH) -> dict[tuple[str, str], dict]:
    """(family, alert name in lower case) -> {type, decides, note, tickets} from the notes' alert table.
    '(blank)' is the empty name. Empty when the file or the section is missing."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    section = text.split("## Alert name to v1 type", 1)
    if len(section) < 2:
        return {}
    body = section[1].split("\n## ", 1)[0]
    out = {}
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0] not in ALERT_FAMILIES:
            continue
        name = "" if cells[1] == "(blank)" else cells[1]
        try:
            tickets = int(cells[2].replace(",", ""))
        except ValueError:
            tickets = 0
        out[(cells[0], name.lower())] = {"family": cells[0], "alert_name": name, "type": cells[3],
                                         "decides": cells[4], "note": cells[5] if len(cells) > 5 else "",
                                         "tickets": tickets}
    return out


# --- Model -----------------------------------------------------------------------------


def train(tickets: list[dict], labels: dict[str, dict], seed: int = 0) -> dict:
    """A type model and an intent model fitted on the labeled tickets found in tickets."""
    rows = [(t, labels[str(t["ticket_id"])]) for t in tickets if str(t["ticket_id"]) in labels]
    if not rows:
        raise ValueError("None of the labeled tickets are in the ticket files")
    X = features([t for t, _ in rows], MAX_CHARS)
    models = {target: build_model(seed).fit(X, [lab[target] for _, lab in rows]) for target in TARGETS}
    return {"models": models, "tickets": len(rows), "created_at": now_iso(), "taxonomy_version": TAXONOMY_VERSION,
            "counts": {target: Counter(lab[target] for _, lab in rows) for target in TARGETS}}


def load_or_train(model_path: Path = MODEL_PATH, labels_path: Path = LABELS_PATH,
                  ticket_files=TICKET_FILES, tickets: list[dict] | None = None, log=print) -> dict | None:
    """The saved model when it matches the current taxonomy, else one trained now and saved; None when
    there is no label file."""
    if model_path.exists():
        bundle = joblib.load(model_path)
        if bundle.get("taxonomy_version") == TAXONOMY_VERSION:
            return bundle
        log(f"Ticket type model is for taxonomy {bundle.get('taxonomy_version')}; retraining")
    if not labels_path.exists():
        log(f"Ticket types: no label file at {labels_path}; the Classify tab is off")
        return None
    labels = {str(r["ticket_id"]): r for r in read_jsonl(labels_path) if r.get("ticket_id") is not None}
    if tickets is None:
        tickets = [t for p in ticket_files if Path(p).exists() for t in read_jsonl(p)]
    bundle = train(tickets, labels)
    bundle["labels"] = str(labels_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, model_path)
    log(f"Ticket types: trained on {bundle['tickets']:,} labeled tickets, saved to {model_path}")
    return bundle


def _top(model, X, k: int = TOP_K) -> list[dict]:
    p = model.predict_proba(X)[0]
    order = p.argsort()[::-1][:k]
    return [{"id": str(model.classes_[i]), "p": round(float(p[i]), 4)} for i in order]


# --- Classification --------------------------------------------------------------------


def classify(subject: str, text: str, bundle: dict, alert_map: dict) -> dict:
    """The primary type and intent for one ticket, where each came from, the model's top candidates
    and, for an alert, its parsed family and name and the table entry it matched."""
    ticket = {"subject": subject or "", "description": text or ""}
    X = features([ticket], MAX_CHARS)
    type_top, intent_top = _top(bundle["models"]["primary_type"], X), _top(bundle["models"]["intent"], X)
    out = {"primary_type": type_top[0]["id"], "type_source": "model", "type_candidates": type_top,
           "intent": intent_top[0]["id"], "intent_source": "model", "intent_candidates": intent_top,
           "alert": None, "notes": []}
    fields = parse_alert(subject, text)
    if not fields:
        return out
    family, name = fields.get("family") or "unparsed", fields.get("alert_name") or ""
    entry = alert_map.get((family, name.lower()))
    out["alert"] = {"family": family, "layout": fields.get("layout"), "alert_name": name, "entry": entry}
    resolved = bool(RESOLVED_RE.search(f"{subject}\n{text}"))
    out["intent"], out["intent_source"] = ("provide_information" if resolved else "fix_incident"), "alert_rule"
    if entry is None:
        out["notes"].append("This alert name is not in the taxonomy's alert table, so the model decides the "
                            "type. The model is trained on human tickets only.")
        return out
    out["primary_type"] = entry["type"]
    out["type_source"] = {"Yes": "alert_name", "Partly": "alert_name_partly"}.get(entry["decides"], "alert_default")
    if entry["decides"] == "Partly":
        out["notes"].append("The body can move this alert to another type: " + entry["note"])
    elif entry["decides"] != "Yes":
        out["notes"].append("The alert name does not decide the type; this is the table's default. "
                            + entry["note"])
    return out


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
    tr.add_argument("--labels", type=Path, default=LABELS_PATH)
    tr.add_argument("--model-path", type=Path, default=MODEL_PATH)
    args = p.parse_args(argv)
    if args.model_path.exists():
        args.model_path.unlink()  # train always refits
    bundle = load_or_train(args.model_path, args.labels)
    if bundle:
        print(f"Alert table: {len(load_alert_map()):,} alert names")


if __name__ == "__main__":
    main()
