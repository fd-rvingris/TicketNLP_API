"""Apply the ticket taxonomy locally, without an LLM.

Alerts take their type from the alert-name table in the taxonomy notes
(data/claude_pack/returned/taxonomy_notes.md, "Alert name to v1 type"), looked up by the parsed alert
family and name. Other tickets, and alerts the table does not know, go to the keyword rules in
type_rules.py, which read the text with email boilerplate removed (text_clean.py), and then to TF-IDF and
logistic regression trained on Claude's labels (tools/ticket_types.py trains and saves the model). A model
result whose top probability is low or close to the second is flagged uncertain. Used by the detector UI's
Classify tab. Nothing leaves this machine.
"""

from __future__ import annotations

import re
from pathlib import Path

import joblib
import numpy as np

from .alert_parser import parse_alert
from .paths import DATA_DIR
from .taxonomy import TAXONOMY_VERSION  # noqa: F401 -- the version a saved model is checked against
from .text_clean import clean_text
from .type_rules import apply_rules

RETURNED_DIR = DATA_DIR / "claude_pack" / "returned"
NOTES_PATH = RETURNED_DIR / "taxonomy_notes.md"
MODEL_PATH = DATA_DIR / "models" / "ticket_types.joblib"
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


def features(tickets: list[dict], max_chars: int, clean: bool = False) -> np.ndarray:
    """Two text columns, subject and description, as a 2-D object array. With clean, email boilerplate
    is removed first (text_clean.py); ticket_types.py scores with the same setting it trained with.
    Off by default: on Claude's 600 labels, averaged over five cross-validation seeds, cleaning lowered
    type accuracy from 74.5% to 73.8%."""
    rows = []
    for t in tickets:
        subject, desc = t.get("subject") or "", t.get("description") or ""
        if clean:
            subject, desc = clean_text(subject, desc)
        rows.append([subject, desc[:max_chars]])
    return np.array(rows, dtype=object).reshape(len(rows), 2)


def load_model(path: Path = MODEL_PATH) -> dict:
    """The saved type and intent model bundle (see tools/ticket_types.py)."""
    return joblib.load(path)


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
