"""Apply the ticket taxonomy locally, without an LLM.

Alerts take their type from the alert-name table (alert_types.json), looked up by the parsed alert family
and name. Other tickets, and alerts the table does not know, go to the keyword rules in
type_rules.py, which read the text with email boilerplate removed (text_clean.py), and then to TF-IDF and
logistic regression trained on Claude's labels (tools/ticket_types.py trains and saves the model). A model
result whose top probability is low or close to the second is flagged uncertain. Used by the detector UI's
Classify tab. Nothing leaves this machine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import joblib
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .alert_parser import parse_alert
from .paths import DATA_DIR
from .taxonomy import TAXONOMY_VERSION, TicketType
from .text_clean import clean_text
from .type_rules import apply_rules

RETURNED_DIR = DATA_DIR / "claude_pack" / "returned"
ALERT_TYPES_PATH = Path(__file__).parent / "alert_types.json"
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
# Where classify() took the type and the intent from, with a one-line meaning each (the API's catalog). Each
# keyword rule adds a type source rule:<name> (type_rules.RULES).
TYPE_SOURCES = {
    "model": "The local model trained on Claude's labels; check 'uncertain'.",
    "alert_name": "The alert-name table: the alert's name decides the type.",
    "alert_name_partly": "The alert-name table: the name usually decides, but the body can move the alert to another type.",
    "alert_default": "The alert-name table's default for a name that does not decide the type.",
}
INTENT_SOURCES = {
    "model": "The local model trained on Claude's labels.",
    "alert_rule": "Alerts are fix_incident unless they say the condition cleared (provide_information).",
}


# --- Alert-name table ------------------------------------------------------------------


class _Entry(BaseModel):
    """What a row gives an alert: its type, how far the name decides it (Yes, Partly or No), the alert
    tickets it covered when the table was made, and a note for people."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: str
    type: TicketType
    decides: Literal["Yes", "Partly", "No"]
    tickets: int = Field(ge=0)
    note: str = ""

    @field_validator("family")
    @classmethod
    def _known_family(cls, value):
        if value not in ALERT_FAMILIES:
            raise ValueError(f"unknown alert family {value!r}")
        return value


class _Name(_Entry):
    alert_name: str  # "" is an alert with no name


class _Pattern(_Entry):
    pattern: str  # a case-insensitive regular expression searched for in the alert name

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value):
        try:
            re.compile(value)
        except re.error as e:
            raise ValueError(f"pattern {value!r} does not compile: {e}") from None
        return value


class _Table(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    taxonomy_version: str
    names: list[_Name]
    patterns: list[_Pattern]


@dataclass(frozen=True)
class AlertTable:
    """The alert-name table: exact (family, name) rows, then name patterns in file order. Patterns cover
    rule names that carry a client's name, so no client is named in the file."""
    version: str
    names: dict[tuple[str, str], dict] = field(default_factory=dict)
    patterns: list[tuple[re.Pattern, dict]] = field(default_factory=list)

    def lookup(self, family: str, name: str) -> dict | None:
        """The row for an alert, or None: an exact name first, else the first pattern that matches."""
        entry = self.names.get((family, name.lower()))
        if entry is None:
            entry = next((e for rx, e in self.patterns if e["family"] == family and rx.search(name)), None)
        return entry

    def entries(self) -> list[dict]:
        return list(self.names.values()) + [e for _, e in self.patterns]

    def __len__(self) -> int:
        return len(self.names) + len(self.patterns)


def load_alert_table(path: Path = ALERT_TYPES_PATH) -> AlertTable:
    """The alert-name table from alert_types.json. Raises ValueError, naming the problem, when the file is
    for another taxonomy version, a row has an unknown family, type or decides value or extra fields, a
    pattern does not compile, or two rows share a (family, name)."""
    try:
        table = _Table.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except ValidationError as e:
        raise ValueError(f"{path.name}: {e}") from None
    if table.taxonomy_version != TAXONOMY_VERSION:
        raise ValueError(f"{path.name} is for taxonomy {table.taxonomy_version}, not {TAXONOMY_VERSION}")
    names = {}
    for row in table.names:
        key = (row.family, row.alert_name.lower())
        if key in names:
            raise ValueError(f"{path.name}: {row.family} alert name {row.alert_name!r} is listed twice")
        names[key] = row.model_dump(mode="json")
    patterns = [(re.compile(p.pattern, re.IGNORECASE), p.model_dump(mode="json")) for p in table.patterns]
    return AlertTable(table.version, names, patterns)


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


def bundle_problem(bundle: dict) -> str | None:
    """Why a loaded bundle cannot be used as it is, or None: it must be for the current taxonomy version
    and the text cleaning setting it is scored with (tools/ticket_types.py retrains it otherwise)."""
    if bundle.get("taxonomy_version") != TAXONOMY_VERSION:
        return f"the type model is for taxonomy {bundle.get('taxonomy_version')}, not {TAXONOMY_VERSION}"
    if bundle.get("clean", False) != MODEL_CLEAN:
        return f"the type model was trained with cleaning {bundle.get('clean', False)}, not {MODEL_CLEAN}"
    if not {"primary_type", "intent"} <= set(bundle.get("models") or {}):
        return "the type model file has no type and intent models"
    return None


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


def classify(subject: str, text: str, bundle: dict, alert_table: AlertTable | None) -> dict:
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
    entry = alert_table.lookup(family, name) if alert_table else None
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
