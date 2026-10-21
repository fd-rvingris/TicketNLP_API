"""How the current taxonomy would map to the new service catalog, for the detector UI's New taxonomy tab.

new_taxonomy_map.json is a proposal for review: for each current type, ordered slices that each name the
catalog category the slice would take (or none), whether that is a clear mapping, a judgment call or a
gap, and the categories that could also fit. summary() classifies every ticket with the current local
classifier, puts it in its type's first matching slice and counts the result per slice and category,
with the catalog's colour and code conflicts and the depth and intent discussions. Nothing leaves this
machine; example subjects are masked.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import ticket_analysis.service_catalog as sc
from ticket_analysis.masking import mask_secrets
from ticket_analysis.taxonomy import TAXONOMY, TAXONOMY_VERSION

MAP_PATH = Path(__file__).parent / "new_taxonomy_map.json"
EXAMPLES = 3
ZENDESK_TOP = 3  # the Zendesk categories shown per slice, as evidence of where the team files these today
# A Zendesk category carrying a tier prefix, such as "(CH) Backup Management"; values are cut at 30 characters
ZENDESK_TIER = re.compile(r"^\((CH|CS|CE)\)\s*(.+)$")


class _When(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert: bool | None = None
    source: str | None = None
    subject: str | None = None
    text: str | None = None


class _Slice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    when: _When
    category: str | None
    status: Literal["mapped", "judgment", "no_home"]
    candidates: list[str] = []
    note: str = ""


class _Discussion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    categories: list[str] = Field(min_length=2)
    difference: str
    why_hard: str
    signals: str
    options: list[str]


class _Map(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    taxonomy_version: str
    catalog_version: str
    note: str = ""
    types: dict[str, list[_Slice]]
    discussions: list[_Discussion]


@dataclass(frozen=True)
class Mapping:
    version: str
    types: dict[str, list[dict]]  # each slice also carries its compiled patterns under "_re"
    discussions: list[dict]


def load_mapping(path: Path = MAP_PATH, catalog: dict | None = None) -> Mapping:
    """The mapping from new_taxonomy_map.json. Raises ValueError, naming the problem, when it is for
    another taxonomy or catalog version, misses or adds a type, a type's last slice has conditions, a
    category is unknown or does not agree with the status, or a pattern does not compile."""
    catalog = catalog or sc.load_catalog()
    try:
        m = _Map.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except ValidationError as e:
        raise ValueError(f"{path.name}: {e}") from None
    if m.taxonomy_version != TAXONOMY_VERSION:
        raise ValueError(f"{path.name} is for taxonomy {m.taxonomy_version}, not {TAXONOMY_VERSION}")
    if m.catalog_version != catalog["version"]:
        raise ValueError(f"{path.name} is for catalog {m.catalog_version}, not {catalog['version']}")
    current = [t["id"] for t in TAXONOMY["types"]]
    if missing := [t for t in current if t not in m.types]:
        raise ValueError(f"{path.name}: no slices for {', '.join(missing)}")
    if extra := [t for t in m.types if t not in current]:
        raise ValueError(f"{path.name}: {', '.join(extra)} is not a current type")
    ids = {c["id"] for c in catalog["categories"]}
    types = {}
    for kind in current:
        slices = []
        for i, s in enumerate(m.types[kind]):
            where = f"{path.name}: {kind} slice {i + 1} ({s.label})"
            if (i == len(m.types[kind]) - 1) != (s.when.model_dump(exclude_none=True) == {}):
                raise ValueError(f"{where}: only the last slice of a type has no conditions, and it must have none")
            if s.category is not None and s.category not in ids:
                raise ValueError(f"{where}: {s.category!r} is not a catalog category")
            if (s.category is None) != (s.status == "no_home"):
                raise ValueError(f"{where}: a category is given exactly when the status is not no_home")
            if unknown := [c for c in s.candidates if c not in ids]:
                raise ValueError(f"{where}: {', '.join(unknown)} is not a catalog category")
            compiled = {}
            for field in ("subject", "text"):
                pattern = getattr(s.when, field)
                if pattern is not None:
                    try:
                        compiled[field] = re.compile(pattern, re.IGNORECASE)
                    except re.error as e:
                        raise ValueError(f"{where}: pattern {pattern!r} does not compile: {e}") from None
            slices.append({**s.model_dump(), "when": s.when.model_dump(exclude_none=True), "_re": compiled})
        types[kind] = slices
    for d in m.discussions:
        if unknown := [c for c in d.categories if c not in ids]:
            raise ValueError(f"{path.name}: discussion {d.title!r} names {', '.join(unknown)}, not catalog categories")
    return Mapping(m.version, types, [d.model_dump() for d in m.discussions])


def match(slices: list[dict], *, alert: bool, source: str, subject: str, text: str) -> int:
    """The index of the first slice whose conditions all hold (the last slice has none)."""
    for i, s in enumerate(slices):
        w, rx = s["when"], s["_re"]
        if "alert" in w and w["alert"] != alert:
            continue
        if "source" in w and w["source"] != source:
            continue
        if "subject" in rx and not rx["subject"].search(subject):
            continue
        if "text" in rx and not rx["text"].search(f"{subject}\n{text}"):
            continue
        return i
    return len(slices) - 1


def _basis(when: dict) -> str:
    """How a slice's tickets are picked: keywords (an estimate), the classifier's source, or the rest."""
    if "subject" in when or "text" in when:
        return "keywords"
    return "source" if when else "remainder"


def summary(tickets: list[dict], classify: Callable[[str, str], dict], catalog: dict, mapping: Mapping,
            examples: int = EXAMPLES) -> dict:
    """The page's data: every ticket classified with classify(subject, text) (ticket_types.classify with
    the model and alert table bound), placed in its type's first matching slice, and counted per slice,
    per catalog category and per Zendesk category that already carries a tier prefix."""
    counts = defaultdict(Counter)  # (type, slice) -> tickets, alerts
    filed = defaultdict(Counter)  # (type, slice) -> Zendesk category today
    shown = defaultdict(list)
    by_category = defaultdict(Counter)
    zendesk = defaultdict(Counter)
    total = alerts = 0
    for t in tickets:
        subject, text = t.get("subject") or "", t.get("description") or ""
        r = classify(subject, text)
        kind, is_alert = r["primary_type"], r["alert"] is not None
        i = match(mapping.types[kind], alert=is_alert, source=r["type_source"], subject=subject, text=text)
        key = (kind, i)
        counts[key]["tickets"] += 1
        counts[key]["alerts"] += is_alert
        if len(shown[key]) < examples and subject.strip() and subject not in shown[key]:
            shown[key].append(subject)
        category = mapping.types[kind][i]["category"]
        by_category[category]["tickets"] += 1
        by_category[category]["alerts"] += is_alert
        zd = str((t.get("weak_labels") or {}).get("category") or "")
        filed[key][zd or "(none)"] += 1
        if ZENDESK_TIER.match(zd):
            zendesk[zd][category] += 1
        total += 1
        alerts += is_alert

    labels = {t["id"]: t["label"] for t in TAXONOMY["types"]}
    types, no_home, status = [], [], Counter({"mapped": 0, "judgment": 0, "no_home": 0})
    for kind, slices in mapping.types.items():
        rows = []
        for i, s in enumerate(slices):
            c = counts[(kind, i)]
            row = {k: v for k, v in s.items() if k not in ("_re", "when")}
            row.update(when=s["when"], basis=_basis(s["when"]), tickets=c["tickets"], alerts=c["alerts"],
                       examples=[mask_secrets(x)[0] for x in shown[(kind, i)]],
                       zendesk=[{"category": k, "tickets": v} for k, v in filed[(kind, i)].most_common(ZENDESK_TOP)])
            rows.append(row)
            status[s["status"]] += c["tickets"]
            if s["status"] == "no_home":
                no_home.append({"type": kind, **row})
        types.append({"type": kind, "label": labels[kind], "tickets": sum(r["tickets"] for r in rows),
                      "alerts": sum(r["alerts"] for r in rows), "slices": rows})
    types.sort(key=lambda t: -t["tickets"])
    no_home.sort(key=lambda r: -r["tickets"])

    cats = [{**c, "implied_coverage": sc.implied_coverage(c["tiers"]),
             "tickets": by_category[c["id"]]["tickets"], "alert_tickets": by_category[c["id"]]["alerts"]}
            for c in catalog["categories"]]
    by_id = {c["id"]: c for c in cats}
    discussions = []
    for d in mapping.discussions:
        members = [{k: by_id[c][k] for k in ("id", "name", "tiers", "coverage", "tickets")} for c in d["categories"]]
        same_codes = all(m["tiers"] == members[0]["tiers"] for m in members)
        same = same_codes and all(m["coverage"] == members[0]["coverage"] for m in members)
        discussions.append({**d, "categories": members, "same_codes": same_codes, "same_offering": same})
    alert_cats = sorted((c for c in cats if c["alert_tickets"]), key=lambda c: -c["alert_tickets"])
    names = {c["name"]: c["id"] for c in catalog["categories"]}
    zendesk_check = []
    for zd, landed in sorted(zendesk.items(), key=lambda kv: -sum(kv[1].values())):
        tier, cut = ZENDESK_TIER.match(zd).groups()
        target = next((i for n, i in names.items() if n.startswith(cut.strip())), None)
        zendesk_check.append({"zendesk_category": zd, "tier": tier, "category": target,
                              "tickets": sum(landed.values()), "agree": landed.get(target, 0) if target else 0,
                              "landed": [{"category": k, "tickets": v} for k, v in landed.most_common()]})
    return {"catalog_version": catalog["version"], "mapping_version": mapping.version,
            "taxonomy_version": TAXONOMY_VERSION, "tiers": catalog["tiers"], "offerings": catalog["offerings"],
            "coverage": catalog["coverage"], "tickets": total, "alert_tickets": alerts,
            "status_tickets": dict(status), "types": types, "categories": cats, "no_home": no_home,
            "unmapped_categories": [c["id"] for c in cats if not c["tickets"]],
            "conflicts": sc.conflicts(catalog),
            "questions": [{"id": c["id"], "name": c["name"], "question": c["question"]}
                          for c in catalog["categories"] if c.get("question")],
            "discussions": discussions,
            "alerts": {"by_category": [{k: c[k] for k in ("id", "name", "tiers", "alert_tickets")} for c in alert_cats],
                       "essential_na": sum(c["alert_tickets"] for c in cats if c["tiers"]["essential"] == "N/A")},
            "zendesk_check": zendesk_check}
