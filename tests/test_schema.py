import json

import pytest
from pydantic import ValidationError

from ticket_analysis.taxonomy import TAXONOMY, TAXONOMY_PATH, Intent, TicketClassification, TicketType
from tools.schema import OUTPUT_SCHEMA


def valid_output(**overrides) -> dict:
    data = {"primary_type": "backup_restore", "secondary_types": [], "intent": "fix_incident",
            "confidence": "high", "rationale": "Backup failed.", "mentions": []}
    return data | overrides


def test_taxonomy_file_shape():
    data = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    assert data["version"] == "v1-draft"
    for section in ("types", "intents"):
        ids = [e["id"] for e in data[section]]
        assert len(ids) == len(set(ids))
        assert "unknown" in ids
        for entry in data[section]:
            assert entry["label"] and entry["definition"]


def test_enums_match_taxonomy_exactly():
    assert [t.value for t in TicketType] == [t["id"] for t in TAXONOMY["types"]]
    assert [i.value for i in Intent] == [i["id"] for i in TAXONOMY["intents"]]
    assert len(TicketType) == 20 and len(Intent) == 7


def test_api_schema_uses_taxonomy_enums_and_closed_objects():
    defs = OUTPUT_SCHEMA["$defs"]
    assert defs["TicketType"]["enum"] == [t["id"] for t in TAXONOMY["types"]]
    assert defs["Intent"]["enum"] == [i["id"] for i in TAXONOMY["intents"]]
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    assert defs["Mention"]["additionalProperties"] is False
    assert set(OUTPUT_SCHEMA["required"]) == {"primary_type", "secondary_types", "intent", "confidence",
                                              "rationale", "mentions"}


def test_rejects_value_outside_taxonomy():
    with pytest.raises(ValidationError):
        TicketClassification.model_validate(valid_output(primary_type="printer_jam"))


def test_rejects_extra_fields():
    with pytest.raises(ValidationError):
        TicketClassification.model_validate(valid_output(severity="high"))


def test_caps_secondary_types_and_mentions():
    mentions = [{"text": f"S{i}", "kind": "server", "evidence_quote": f"S{i}"} for i in range(30)]
    parsed = TicketClassification.model_validate(
        valid_output(secondary_types=["performance", "scheduled_jobs", "capacity_storage"], mentions=mentions))
    assert len(parsed.secondary_types) == 2
    assert len(parsed.mentions) == 25


def test_prompt_rules_name_only_taxonomy_ids():
    import re
    from pathlib import Path
    text = (Path(__file__).parent.parent / "prompts" / "classify_v2.md").read_text(encoding="utf-8")
    rules = text.split("Choosing the primary type", 1)[1].split("Ticket types:", 1)[0]
    ids = {t["id"] for t in TAXONOMY["types"]} | {i["id"] for i in TAXONOMY["intents"]}
    named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", rules))
    assert named and named <= ids
