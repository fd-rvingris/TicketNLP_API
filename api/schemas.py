"""Request and response models of /v1. FastAPI builds the OpenAPI document from them, so every model
and field has a description and a made-up example. Field names are camelCase; ids inside values (type
ids, statuses, rule names) keep their existing spelling.

Enumerated values (detection keys, statuses, types, intents, sources) are plain strings in the schema:
they grow within v1, so clients must accept values they do not know. GET /v1/catalog lists them.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ticket_analysis.analysis import MAX_TEXT

MAX_SUBJECT = 1_000
MAX_TICKET_ID = 256
REQUEST_ID = "3f2b8c1e9a7d4e6f8b0c2d4e6f8a0b1c"


def _omit_none(value) -> bool:
    return value is None


# --- Request ----------------------------------------------------------------------------

Text = Annotated[str, Field(min_length=1, max_length=MAX_TEXT, examples=["FAKESQL01 is down since 06:00."],
                            description=f"The ticket text: its description or first comment, 1 to {MAX_TEXT:,} "
                                        "characters. The detector reads the first 20,000 characters and the "
                                        "classifier the first 8,000.")]
Subject = Annotated[str | None, Field(max_length=MAX_SUBJECT, examples=["Slow reports"],
                                      description=f"The ticket subject, up to {MAX_SUBJECT:,} characters. Optional, "
                                                  "but alerts and several rules are recognized by it.")]
OrgIdString = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{1,20}$")]
OrgIdInt = Annotated[int, Field(ge=0, strict=True)]
ZendeskOrgId = Annotated[OrgIdInt | OrgIdString | None, Field(
    examples=[999001],
    description="The Zendesk organization id of the ticket's client, as a string of digits or an integer. The "
                "detector resolves names against that client's known servers and instances only; missing or "
                "unknown, no client's names apply. The taxonomy classifier ignores it.")]
TicketId = Annotated[Annotated[str, StringConstraints(max_length=MAX_TICKET_ID)] | int | None, Field(
    examples=["12345"],
    description="The caller's own ticket id, echoed back for correlation. Not used and not logged.")]

ACCESS_EMAIL = (
    "CAUTION: This email originated from outside of the organization. Do not click links or open attachments "
    "unless you recognize the sender and know the content is safe.\r\n\r\n"
    "Hi Fortified Data Support,\r\n\r\n"
    "Could you please help me get READ access for the schema below, Server=FAKESQL01, Database=FAKE_DB, for the "
    "execution user CORP\\svc-fake-batch. Please approve.\r\n\r\n"
    "Thanks,\r\nJane Doe\r\nData Engineer | Fake Co\r\n")
ALERT_TEXT = ("AlertId 123456\nAlert FD - SQL Agent Job Failure\nClient Name Fake Co\nServer FAKESQL01\n"
              "Platform Microsoft SQL Server\nInstance Job: Nightly Load, Category: Data\n"
              "Message Error: The job failed.\nSeverity Warning\nLevel Failure\nPreviousLevel OK\n"
              "Open Date 07/21/2026 17:00:00 UTC\n")

REQUEST_EXAMPLES = {
    "access_request": {
        "summary": "Access request email (rule:access_request)",
        "value": {"text": ACCESS_EMAIL, "subject": "READ access request", "zendeskOrgId": 999001, "ticketId": "12345"}},
    "servers": {
        "summary": "A host, a bare instance and a HOST\\INSTANCE name",
        "value": {"text": "The REPORTS instance on FAKESQL01 is slow since this morning. FAKESQL02\\FINANCE is fine.",
                  "subject": "Slow reports", "zendeskOrgId": "999001"}},
    "alert": {
        "summary": "A collector alert (alert-name table)",
        "value": {"text": ALERT_TEXT, "subject": "Fake Co - Alert #42 - FD - SQL Agent Job Failure",
                  "zendeskOrgId": "999001"}},
    "text_only": {
        "summary": "Text only: no subject and no client",
        "value": {"text": "Disk space is low on the data drive of FAKESQL01."}},
}

_MODELS_EXAMPLE = {"serverDetector": "2026-10-17T15:10:15Z", "ticketTypes": "2026-10-17T20:53:21Z",
                   "directory": "2026-10-19T06:00:00Z", "taxonomyVersion": "v1-draft", "alertTableVersion": "1",
                   "modelVersion": None}


class TicketRequest(BaseModel):
    """One ticket. Unknown fields are refused, so a misspelt zendeskOrgId is not silently ignored."""
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [REQUEST_EXAMPLES["servers"]["value"]]})

    text: Text
    subject: Subject = None
    zendeskOrgId: ZendeskOrgId = None
    ticketId: TicketId = None

    def org_id(self) -> str | None:
        return None if self.zendeskOrgId is None else str(self.zendeskOrgId)

    def ticket_id(self) -> str | None:
        return None if self.ticketId is None else str(self.ticketId)


# --- Envelope -----------------------------------------------------------------------------


class ModelVersions(BaseModel):
    """The model and directory versions that produced the response. They change with retraining and
    directory refreshes, without an API change."""
    model_config = ConfigDict(json_schema_extra={"examples": [_MODELS_EXAMPLE]})

    serverDetector: str | None = Field(description="The server detector model's creation time.",
                                       examples=["2026-10-17T15:10:15Z"])
    ticketTypes: str | None = Field(description="The taxonomy classifier model's creation time.",
                                    examples=["2026-10-17T20:53:21Z"])
    directory: str | None = Field(description="The known-names directory snapshot's creation time, or "
                                  "model:<time> when the model's own directory is in use.",
                                  examples=["2026-10-19T06:00:00Z"])
    taxonomyVersion: str = Field(description="The taxonomy the type and intent ids belong to.", examples=["v1-draft"])
    alertTableVersion: str = Field(description="The alert-name table's version (ticket_analysis/alert_types.json).",
                                   examples=["1"])
    modelVersion: str | None = Field(description="The store's model version folder (MODEL_VERSION), when set.",
                                     examples=["2026-10-17"])


class Envelope(BaseModel):
    """The fields every /v1 response starts with."""
    requestId: str = Field(description="The request id, also in the X-Request-Id header.", examples=[REQUEST_ID])
    apiVersion: str = Field(description="The API contract version.", examples=["1.0.0"])
    ticketId: str | None = Field(None, description="The caller's ticketId, echoed; null when not given.",
                                 examples=["12345"])
    models: ModelVersions = Field(description="The model and directory versions that produced the response.")


# --- Detection ----------------------------------------------------------------------------


class Detection(BaseModel):
    """One occurrence of a server or instance name in the ticket."""
    key: str = Field(description="The entity type: host, instance (a bare instance name) or host_instance.",
                     examples=["host_instance"])
    value: str = Field(description="The name: lower case, without domain or port, corrected when status is "
                       "typo_of_known; HOST\\INSTANCE for host_instance.", examples=["fakesql02\\finance"])
    text: str = Field(description="The name as written, the text at field[start:end].", examples=["FAKESQL02\\FINANCE"])
    host: str | None = Field(description="The host; for a bare instance, the host it was paired with, if any.",
                             examples=["fakesql02"])
    instance: str | None = Field(description="The named instance, if any.", examples=["finance"])
    status: str = Field(description="How the name resolved against the client's known names.", examples=["known"])
    field: str = Field(description="Where the name is: subject or description.", examples=["description"])
    start: int = Field(description="Start offset in the field, in characters.", examples=[62])
    end: int = Field(description="End offset in the field, in characters (exclusive).", examples=[79])
    typoOf: str | None = Field(description="The known name this one is a typo of (status typo_of_known).",
                               examples=[None])
    siblingOf: str | None = Field(description="A known name that differs only in trailing digits.", examples=[None])
    hostCandidates: list[str] = Field(description="For an ambiguous bare instance, the client's hosts that have it.",
                                      examples=[[]])
    environment: str | None = Field(description="The environment the CMDB or alerts give for the host.",
                                    examples=["Production"])
    score: float = Field(description="The detector's score for this name, 0 to 1.", examples=[0.95])


class Entities(BaseModel):
    """The compact key/value view: each key's distinct values in order of first appearance."""
    host: list[str] = Field(description="Hosts named on their own.", examples=[["fakesql01"]])
    instance: list[str] = Field(description="Instances named without their host.", examples=[["reports"]])
    host_instance: list[str] = Field(description="HOST\\INSTANCE names.", examples=[["fakesql02\\finance"]])


class ClientInfo(BaseModel):
    """The client the names were resolved against."""
    zendeskOrgId: str | None = Field(description="The organization id the request gave.", examples=["999001"])
    known: bool = Field(description="Whether the directory has known names for that organization.", examples=[True])


class DetectionResult(BaseModel):
    """The servers and instances in one ticket."""
    client: ClientInfo = Field(description="The organization the request named and whether its names are known.")
    isAlert: bool = Field(description="Whether the text is a monitoring alert the parser recognized.", examples=[False])
    entities: Entities = Field(description="Each key's distinct values in order of first appearance.")
    detections: list[Detection] = Field(description="One entry per occurrence, in text order (subject first).")
    debug: dict | None = Field(None, exclude_if=_omit_none,
                               description="Only with DEBUG_RESPONSES on a local run: every scored candidate.")


class DetectResponse(DetectionResult, Envelope):
    """The response of POST /v1/detect."""
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "requestId": REQUEST_ID, "apiVersion": "1.0.0", "ticketId": None, "models": _MODELS_EXAMPLE,
        "client": {"zendeskOrgId": "999001", "known": True}, "isAlert": False,
        "entities": {"host": ["fakesql01"], "instance": ["reports"], "host_instance": ["fakesql02\\finance"]},
        "detections": [
            {"key": "instance", "value": "reports", "text": "REPORTS", "host": "fakesql01", "instance": "reports",
             "status": "known", "field": "description", "start": 4, "end": 11, "typoOf": None, "siblingOf": None,
             "hostCandidates": [], "environment": "Production", "score": 0.95},
            {"key": "host", "value": "fakesql01", "text": "FAKESQL01", "host": "fakesql01", "instance": None,
             "status": "known", "field": "description", "start": 24, "end": 33, "typoOf": None, "siblingOf": None,
             "hostCandidates": [], "environment": "Production", "score": 1.0},
            {"key": "host_instance", "value": "fakesql02\\finance", "text": "FAKESQL02\\FINANCE", "host": "fakesql02",
             "instance": "finance", "status": "known", "field": "description", "start": 62, "end": 79,
             "typoOf": None, "siblingOf": None, "hostCandidates": [], "environment": None, "score": 1.0}]}]})


# --- Classification -----------------------------------------------------------------------


class Candidate(BaseModel):
    """A type or intent the model considered, with its probability."""
    id: str = Field(description="A type or intent id.", examples=["access_security"])
    p: float = Field(description="The model's probability.", examples=[0.33])


class AlertTableEntry(BaseModel):
    """The alert-name table row (alert_types.json) the alert matched, by exact name or name pattern. The
    table's free-text note is not returned: it was written from real tickets."""
    type: str = Field(description="The type the table gives.", examples=["scheduled_jobs"])
    decides: str = Field(description="Whether the name decides the type: Yes, Partly or No.", examples=["Partly"])
    tickets: int = Field(description="Alert tickets with this name when the table was made.", examples=[1259])


class AlertInfo(BaseModel):
    """The monitoring alert the parser recognized in the text."""
    family: str = Field(description="The alert family.", examples=["collector"])
    layout: str | None = Field(description="The alert layout.", examples=["collector_labeled"])
    alertName: str = Field(description="The alert name parsed from the text.", examples=["FD - SQL Agent Job Failure"])
    tableEntry: AlertTableEntry | None = Field(description="The alert-name table row, when the table has the name.")


class ClassificationResult(BaseModel):
    """The taxonomy type and intent of one ticket."""
    primaryType: str = Field(description="The taxonomy type id.", examples=["access_security"])
    typeSource: str = Field(description="Where the type came from: model, alert_name, alert_name_partly, "
                            "alert_default or rule:<name>.", examples=["rule:access_request"])
    typeCandidates: list[Candidate] = Field(description="The model's top types, highest first.")
    intent: str = Field(description="The taxonomy intent id.", examples=["request_change"])
    intentSource: str = Field(description="Where the intent came from: model or alert_rule.", examples=["model"])
    intentCandidates: list[Candidate] = Field(description="The model's top intents, highest first.")
    uncertain: bool = Field(description="True when the model decided the type with low confidence or a close "
                            "second; check by hand.", examples=[False])
    uncertainReason: str = Field(description="Why it is uncertain; empty otherwise.", examples=[""])
    alert: AlertInfo | None = Field(description="The parsed alert, for alerts; null otherwise.")
    notes: list[str] = Field(description="Remarks on the result, such as an alert name the table does not decide.",
                             examples=[[]])
    debug: dict | None = Field(None, exclude_if=_omit_none,
                               description="Only with DEBUG_RESPONSES on a local run: the cleaned text and each "
                                           "rule's result.")


class ClassifyResponse(ClassificationResult, Envelope):
    """The response of POST /v1/classify."""
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "requestId": REQUEST_ID, "apiVersion": "1.0.0", "ticketId": "12345", "models": _MODELS_EXAMPLE,
        "primaryType": "access_security", "typeSource": "rule:access_request",
        "typeCandidates": [{"id": "access_security", "p": 0.33}, {"id": "database_refresh", "p": 0.12}],
        "intent": "request_change", "intentSource": "model",
        "intentCandidates": [{"id": "request_change", "p": 0.61}, {"id": "fix_incident", "p": 0.2}],
        "uncertain": False, "uncertainReason": "", "alert": None, "notes": []}]})


class AnalyzeResponse(Envelope):
    """The response of POST /v1/analyze: both bodies without their own envelope fields."""
    detection: DetectionResult = Field(description="What POST /v1/detect returns, without the envelope.")
    taxonomy: ClassificationResult = Field(description="What POST /v1/classify returns, without the envelope.")


# --- Catalog ------------------------------------------------------------------------------


class Described(BaseModel):
    """An enumerated value and its meaning."""
    value: str = Field(description="The value as responses carry it.", examples=["known"])
    description: str = Field(description="What it means.",
                             examples=["The client's known names include this host, pair or instance."])


class DetectionKey(BaseModel):
    """A detection key (entity type)."""
    key: str = Field(description="The key as responses carry it.", examples=["host_instance"])
    description: str = Field(description="What it means.",
                             examples=["A HOST\\INSTANCE name: a named instance on a given server."])


class DetectionField(BaseModel):
    """A field of a detection."""
    name: str = Field(description="The field name.", examples=["status"])
    type: str = Field(description="The JSON type: string, integer, number, boolean or array of string, with "
                      "'or null' when it can be null.", examples=["string"])
    description: str = Field(description="What the field holds.",
                             examples=["How the name resolved against the client's known names."])
    values: list[Described] | None = Field(description="For enumerated fields, every value and its meaning.")


class TaxonomyEntry(BaseModel):
    """A taxonomy type or intent from taxonomy.json."""
    id: str = Field(description="The id responses carry.", examples=["access_security"])
    label: str = Field(description="A short label.", examples=["Access and security"])
    definition: str = Field(description="The taxonomy's definition.", examples=["Logins, users, roles and permissions."])


class CatalogResponse(Envelope):
    """The response of GET /v1/catalog: every key and enumerated value the responses can contain."""
    taxonomyVersion: str = Field(description="The taxonomy the types and intents belong to.", examples=["v1-draft"])
    alertTableVersion: str = Field(description="The alert-name table's version.", examples=["1"])
    detectionKeys: list[DetectionKey] = Field(description="The detection keys.")
    detectionFields: list[DetectionField] = Field(description="Every field of a detection.")
    types: list[TaxonomyEntry] = Field(description="The taxonomy's types.")
    intents: list[TaxonomyEntry] = Field(description="The taxonomy's intents.")
    typeSources: list[Described] = Field(description="Every typeSource value, including rule:<name> per keyword rule.")
    intentSources: list[Described] = Field(description="Every intentSource value.")


# --- Probes and errors --------------------------------------------------------------------


class Status(BaseModel):
    """A probe's answer."""
    status: str = Field(description="ok for /health, ready for /ready.", examples=["ok"])


class FieldError(BaseModel):
    """One field that failed validation. The rejected value is never repeated."""
    loc: list[str | int] = Field(description="Where the problem is, such as ['body', 'text'].", examples=[["body", "text"]])
    msg: str = Field(description="What is wrong.", examples=["Field required"])
    type: str = Field(description="The error type.", examples=["missing"])


class Problem(BaseModel):
    """RFC 9457 problem details (application/problem+json). Never repeats the request's text."""
    type: str = Field("about:blank", description="Always about:blank; use status and code.", examples=["about:blank"])
    title: str = Field(description="The HTTP status phrase.", examples=["Unprocessable Content"])
    status: int = Field(description="The HTTP status.", examples=[422])
    detail: str | None = Field(None, description="What went wrong, for people.",
                               examples=["The request body does not match the schema."])
    code: str = Field(description="A stable machine-readable reason.", examples=["validation_error"])
    requestId: str | None = Field(None, description="The request id, as in X-Request-Id.", examples=[REQUEST_ID])
    apiVersion: str = Field(description="The API contract version.", examples=["1.0.0"])
    errors: list[FieldError] | None = Field(None, exclude_if=_omit_none,
                                            description="For 422, each field that failed validation.")
