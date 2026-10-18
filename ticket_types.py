"""Apply the ticket taxonomy locally, without an LLM.

Alerts take their type from the alert-name table in the taxonomy notes
(data/claude_pack/returned/taxonomy_notes.md, "Alert name to v1 type"), looked up by the parsed alert
family and name. Other tickets, and alerts the table does not know, go to the keyword rules in
type_rules.py, which read the text with email boilerplate removed (text_clean.py), and then to TF-IDF and
logistic regression trained on label files (Claude's taxonomy sample labels plus later Claude Desktop
rounds, LABEL_PATHS), the same features as baseline.py. A model result whose top probability is low or close to the second is flagged
uncertain. Used by detector_ui.py's Classify tab.

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
from baseline import build_model, features, read_labels
from common import DATA_DIR, now_iso, read_jsonl
from schema import TAXONOMY, TAXONOMY_VERSION
from text_clean import clean_text
from type_rules import apply_rules

RETURNED_DIR = DATA_DIR / "claude_pack" / "returned"
NOTES_PATH = RETURNED_DIR / "taxonomy_notes.md"
LABELS_PATH = RETURNED_DIR / "taxonomy_sample_labels.jsonl"
# Type labels from later Claude Desktop rounds (claude_pack.py types / import-types); they override the
# taxonomy sample for the same ticket
CLAUDE_TYPES_PATH = DATA_DIR / "labels" / "claude_types.jsonl"
LABEL_PATHS = (LABELS_PATH, CLAUDE_TYPES_PATH)
MODEL_PATH = DATA_DIR / "models" / "ticket_types.joblib"
PROMPT_PATH = Path(__file__).parent / "prompts" / "classify_v2.md"
TICKET_FILES = [DATA_DIR / "train_tickets.jsonl", DATA_DIR / "test_tickets.jsonl"]
TARGETS = ("primary_type", "intent")
ALERT_FAMILIES = {"collector", "firing", "backup_exception_report", "volume_capacity", "unparsed"}
MAX_CHARS = 8000
TOP_K = 3
# The model reads the raw text: cleaning it lowered cross-validated type accuracy (see baseline.features)
MODEL_CLEAN = False
# A model type is uncertain below this top probability or within this gap of the second. On Claude's 600
# labels (5-fold cross-validation, seeds 0 to 2) these flag about 30% of model-decided tickets, which are
# right about 27% of the time against about 89% for the rest.
UNCERTAIN_P = 0.30
UNCERTAIN_GAP = 0.05
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


def _top(model, X, k: int = TOP_K) -> list[dict]:
    p = model.predict_proba(X)[0]
    order = p.argsort()[::-1][:k]
    return [{"id": str(model.classes_[i]), "p": round(float(p[i]), 4)} for i in order]


# --- Classification --------------------------------------------------------------------


def uncertainty(candidates: list[dict], has_subject: bool = True) -> tuple[bool, str]:
    """Whether the model's top type is uncertain (top probability below UNCERTAIN_P or within
    UNCERTAIN_GAP of the second) and a short reason."""
    top = candidates[0]
    second = candidates[1] if len(candidates) > 1 else {"id": "", "p": 0.0}
    advice = "check by hand" if has_subject else "add a subject or check by hand"
    if top["p"] - second["p"] < UNCERTAIN_GAP:
        return True, f"close call between {top['id']} and {second['id']}; {advice}"
    if top["p"] < UNCERTAIN_P:
        return True, f"low confidence: {top['id']} at {top['p']:.0%}, {second['id']} next at {second['p']:.0%}; {advice}"
    return False, ""


def _rule_or_model(out: dict, subject: str, text: str) -> None:
    """A keyword rule's type when one fires on the cleaned text, else the model's, flagged when uncertain."""
    hit = apply_rules(*clean_text(subject, text))
    if hit:
        out["primary_type"], out["type_source"] = hit[0], f"rule:{hit[1]}"
        out["uncertain"], out["uncertain_reason"] = False, ""
        return
    out["primary_type"], out["type_source"] = out["type_candidates"][0]["id"], "model"
    out["uncertain"], out["uncertain_reason"] = uncertainty(out["type_candidates"], bool(subject.strip()))


def classify(subject: str, text: str, bundle: dict, alert_map: dict) -> dict:
    """The primary type and intent for one ticket, where each came from, the model's top candidates,
    whether the type is uncertain and why, and, for an alert, its parsed family and name and the table
    entry it matched. Order: the alert-name table, then the keyword rules, then the model."""
    subject, text = subject or "", text or ""
    X = features([{"subject": subject, "description": text}], MAX_CHARS, clean=bundle.get("clean", False))
    type_top, intent_top = _top(bundle["models"]["primary_type"], X), _top(bundle["models"]["intent"], X)
    out = {"primary_type": type_top[0]["id"], "type_source": "model", "type_candidates": type_top,
           "intent": intent_top[0]["id"], "intent_source": "model", "intent_candidates": intent_top,
           "uncertain": False, "uncertain_reason": "", "alert": None, "notes": []}
    fields = parse_alert(subject, text)
    if not fields:
        _rule_or_model(out, subject, text)
        return out
    family, name = fields.get("family") or "unparsed", fields.get("alert_name") or ""
    entry = alert_map.get((family, name.lower()))
    out["alert"] = {"family": family, "layout": fields.get("layout"), "alert_name": name, "entry": entry}
    resolved = bool(RESOLVED_RE.search(f"{subject}\n{text}"))
    out["intent"], out["intent_source"] = ("provide_information" if resolved else "fix_incident"), "alert_rule"
    if entry is None:
        _rule_or_model(out, subject, text)
        if out["type_source"] == "model":
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
