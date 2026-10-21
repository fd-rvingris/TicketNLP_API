"""The service catalog: the new ticket categories, each with its offering in every service tier and
how the work is covered (included, CE hours or a SOW, or a CS incident).

service_catalog.json is transcribed from the tier category sheet. Each row's colour in the sheet is its
coverage; implied_coverage() derives the coverage the tier codes suggest, and conflicts() lists the rows
where the two disagree, for the team to settle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CATALOG_PATH = Path(__file__).parent / "service_catalog.json"
TIERS = ("essential", "select", "signature")
Offering = Literal["CH", "CS", "CE", "N/A"]
Coverage = Literal["included", "ce_or_sow", "cs_incident"]


class _Tiers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    essential: Offering
    select: Offering
    signature: Offering


class _Category(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    tiers: _Tiers
    coverage: Coverage
    description: str = ""
    question: str = ""  # an open question the sheet itself asks about the row


class _Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    source: str = ""
    tiers: dict[str, str]
    offerings: dict[str, str]
    coverage: dict[str, str]
    categories: list[_Category]


def load_catalog(path: Path = CATALOG_PATH) -> dict:
    """The catalog as a dict. Raises ValueError, naming the problem, when a category lacks a tier, has
    an unknown offering or coverage or an extra field, or two categories share an id."""
    try:
        catalog = _Catalog.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except ValidationError as e:
        raise ValueError(f"{path.name}: {e}") from None
    if list(catalog.tiers) != list(TIERS):
        raise ValueError(f"{path.name}: tiers must be {', '.join(TIERS)}")
    seen = set()
    for c in catalog.categories:
        if c.id in seen:
            raise ValueError(f"{path.name}: category {c.id!r} is listed twice")
        seen.add(c.id)
    return catalog.model_dump(mode="json")


def implied_coverage(tiers: dict[str, str]) -> str | None:
    """The coverage the tier codes suggest: CE in any tier needs CE hours or a SOW, CS alone is a CS
    incident, CH alone is included. None when CH and CS are mixed or no tier offers the work."""
    codes = {tiers[t] for t in TIERS} - {"N/A"}
    if "CE" in codes:
        return "ce_or_sow"
    if codes == {"CS"}:
        return "cs_incident"
    if codes == {"CH"}:
        return "included"
    return None


def _codes(tiers: dict[str, str]) -> str:
    return ", ".join(f"{t.title()} {tiers[t]}" for t in TIERS)


def conflicts(catalog: dict) -> list[dict]:
    """Categories whose row colour (coverage) disagrees with the coverage their tier codes suggest,
    each with the question to put to the team."""
    names = catalog["coverage"]
    out = []
    for c in catalog["categories"]:
        implied = implied_coverage(c["tiers"])
        if implied == c["coverage"]:
            continue
        offered = [t.title() for t in TIERS if c["tiers"][t] != "N/A"]
        question = (f"The row is coloured '{names[c['coverage']]}', but the tier codes are {_codes(c['tiers'])}, "
                    f"which suggests '{names[implied]}'." if implied else
                    f"The row is coloured '{names[c['coverage']]}', but the tier codes ({_codes(c['tiers'])}) "
                    "do not suggest one coverage.")
        if c["coverage"] == "included" and implied == "ce_or_sow":
            question += (f" Is the work included in the service for {' and '.join(offered)}, or does it use CE "
                         "hours or need a SOW?")
        elif c["coverage"] == "ce_or_sow" and implied == "cs_incident":
            question += " Is it a CS incident, or CE hours or a SOW? If CE, should the codes read CE instead of CS?"
        out.append({"id": c["id"], "name": c["name"], "tiers": c["tiers"], "coverage": c["coverage"],
                    "implied_coverage": implied, "question": question})
    return out
