"""Taxonomy and output schema for one classified ticket, generated from taxonomy.json.

The type and intent enums are built from the taxonomy file at import time, so the
schema sent to the model and the validation in code can never drift from it.
"""

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

TAXONOMY_PATH = Path(__file__).parent / "taxonomy.json"
TAXONOMY = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
TAXONOMY_VERSION = TAXONOMY["version"]

TicketType = StrEnum("TicketType", [(t["id"], t["id"]) for t in TAXONOMY["types"]])
Intent = StrEnum("Intent", [(i["id"], i["id"]) for i in TAXONOMY["intents"]])

MAX_SECONDARY_TYPES = 2
MAX_MENTIONS = 25


class Confidence(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class MentionKind(StrEnum):
    server = "server"
    instance = "instance"
    database = "database"
    other = "other"


class Mention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="The name exactly as written in the ticket.")
    kind: MentionKind
    evidence_quote: str = Field(
        description="A short excerpt copied character for character from the ticket that contains the name."
    )


class TicketClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_type: TicketType
    secondary_types: list[TicketType] = Field(
        max_length=MAX_SECONDARY_TYPES,
        description="Zero to two other types that clearly also apply. Never repeat the primary type.",
    )
    intent: Intent
    confidence: Confidence
    rationale: str = Field(description="At most two sentences explaining the choice.")
    mentions: list[Mention] = Field(
        max_length=MAX_MENTIONS,
        description="Servers, SQL Server instances, and databases named in the ticket.",
    )

    # The API cannot enforce array lengths, so trim overlong lists here instead of
    # failing the whole ticket.
    @field_validator("secondary_types", mode="before")
    @classmethod
    def _cap_secondary(cls, value):
        return value[:MAX_SECONDARY_TYPES] if isinstance(value, list) else value

    @field_validator("mentions", mode="before")
    @classmethod
    def _cap_mentions(cls, value):
        return value[:MAX_MENTIONS] if isinstance(value, list) else value

