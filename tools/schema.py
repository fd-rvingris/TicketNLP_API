"""Structured-output schema for the LLM classifier, built from the package's taxonomy models."""

import anthropic

from ticket_analysis.taxonomy import TicketClassification

# JSON schema in the form the structured outputs API accepts (unsupported
# constraints such as maxItems are moved into descriptions by the SDK).
OUTPUT_SCHEMA = anthropic.transform_schema(TicketClassification)
