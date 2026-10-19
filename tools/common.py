"""Shared code for the tools: prompt rendering, the model call, the read-only Zendesk client, and the
metrics used by report.py and compare.py."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv
from pydantic import ValidationError

from ticket_analysis.jsonl import now_iso, read_jsonl, write_jsonl
from ticket_analysis.masking import prepare_ticket_text
from ticket_analysis.names import Cmdb, Instance, match_mention
from ticket_analysis.paths import DATA_DIR, REPO_ROOT
from ticket_analysis.taxonomy import TAXONOMY, TAXONOMY_VERSION, TicketClassification
from tools.schema import OUTPUT_SCHEMA

TICKETS_PATH = DATA_DIR / "tickets.jsonl"
LABELS_PATH = DATA_DIR / "labels.csv"
RUNS_DIR = DATA_DIR / "runs"
REPORTS_DIR = DATA_DIR / "reports"
PROMPTS_DIR = REPO_ROOT / "prompts"

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "low"
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_MAX_TOKENS = 8000  # thinking counts toward max_tokens, so leave headroom
MAX_CLIENT_SERVER_HINTS = 200

# Models that accept server-side refusal fallbacks in the "default" form, which
# re-runs a declined request on the model Anthropic recommends for the refusal
# category. Claude Haiku 5.5 has no server-side fallback, so it is not listed.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens. NEEDS CONFIRMATION: copied from the claude-api skill's
# model table (cached 2026-10-06). cache_write is the 5-minute rate (1.25x input);
# cache_read for Opus 5, Opus 4.8 and Haiku 5.5 assumes 0.1x input. Check
# https://www.anthropic.com/pricing before quoting any cost figure.
PRICES_PER_MTOK = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
    "claude-fable-5-1": {"input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 0.25},
}


def load_env() -> None:
    load_dotenv(REPO_ROOT / ".env")


def merge_tickets(new_tickets: list[dict], path: Path = TICKETS_PATH) -> tuple[int, int]:
    """Add or replace tickets by ticket_id. Returns (added, updated)."""
    existing = {t["ticket_id"]: t for t in read_jsonl(path)}
    added = sum(1 for t in new_tickets if t["ticket_id"] not in existing)
    for t in new_tickets:
        existing[t["ticket_id"]] = t
    write_jsonl(path, list(existing.values()))
    return added, len(new_tickets) - added


# --- Prompt ----------------------------------------------------------------


@dataclass
class Prompt:
    version: str
    system: str
    user_template: str


def fill(template: str, values: dict[str, str]) -> str:
    """Replace {{NAME}} placeholders in one pass, so text inserted for one
    placeholder is never scanned for others."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], template)


def _format_entries(entries: list[dict]) -> str:
    return "\n".join(f"- {e['id']} ({e['label']}): {e['definition']}" for e in entries)


def load_prompt(version: str) -> Prompt:
    path = PROMPTS_DIR / f"classify_{version}.md"
    if not path.exists():
        raise SystemExit(f"Prompt file not found: {path}")
    text = path.read_text(encoding="utf-8")
    _, rest = text.split("<!-- system -->", 1)
    system, user = rest.split("<!-- user -->", 1)
    system = fill(system.strip(), {
        "TYPES": _format_entries(TAXONOMY["types"]),
        "INTENTS": _format_entries(TAXONOMY["intents"]),
    })
    return Prompt(version, system, user.strip())


def render_user_message(prompt: Prompt, subject: str, description: str, client_servers: list[str]) -> str:
    return fill(prompt.user_template, {
        "CLIENT_SERVERS": "\n".join(client_servers) if client_servers else "(none on file)",
        "SUBJECT": subject,
        "DESCRIPTION": description,
    })


def client_server_hints(instances: list[Instance]) -> list[str]:
    return sorted({i.server_name for i in instances}, key=str.lower)[:MAX_CLIENT_SERVER_HINTS]


# --- Evidence quotes -------------------------------------------------------


def normalize_ws(text: str) -> str:
    return " ".join(text.split())


def split_by_evidence(mentions: list, ticket_text: str) -> tuple[list, list]:
    """Keep mentions whose evidence_quote occurs in the ticket text (whitespace
    normalized, case sensitive). Returns (kept, dropped)."""
    haystack = normalize_ws(ticket_text)
    kept, dropped = [], []
    for m in mentions:
        quote = normalize_ws(m.evidence_quote)
        (kept if quote and quote in haystack else dropped).append(m)
    return kept, dropped


# --- Model call ------------------------------------------------------------


class FatalAPIError(Exception):
    """An API error that would repeat for every ticket: bad key, model, or permissions."""


def make_client() -> anthropic.Anthropic:
    # The SDK retries 408/409/429/5xx and connection errors with backoff.
    return anthropic.Anthropic(max_retries=4)


def build_request(prompt: Prompt, user_message: str, *, model: str, effort: str, max_tokens: int) -> dict:
    request = {
        "model": model,
        "max_tokens": max_tokens,
        # The system prompt is identical for every ticket, so cache it.
        "system": [{"type": "text", "text": prompt.system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_message}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    }
    if model in FALLBACK_MODELS:
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"
    return request


def estimate_cost(model: str, row: dict) -> float | None:
    prices = PRICES_PER_MTOK.get(model)
    if not prices or row.get("input_tokens") is None:
        return None
    total = (row["input_tokens"] * prices["input"]
             + row["output_tokens"] * prices["output"]
             + (row.get("cache_creation_input_tokens") or 0) * prices["cache_write"]
             + (row.get("cache_read_input_tokens") or 0) * prices["cache_read"])
    return round(total / 1_000_000, 6)


def new_row(ticket: dict, *, run_name: str, model: str, effort: str, prompt_version: str) -> dict:
    return {
        "ticket_id": ticket["ticket_id"],
        "run_name": run_name,
        "model": model,
        "served_model": None,
        "effort": effort,
        "prompt_version": prompt_version,
        "taxonomy_version": TAXONOMY_VERSION,
        "classified_at": now_iso(),
        "output": None,
        "mentions_returned": 0,
        "dropped_mentions": 0,
        "dropped_mention_details": [],
        "matches": [],
        "cmdb_instances_for_client": 0,
        "secrets_masked": 0,
        "stop_reason": None,
        "stop_category": None,
        "fallback_used": False,
        "input_tokens": None,
        "output_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_read_input_tokens": None,
        "latency_ms": None,
        "estimated_cost_usd": None,
        "request_id": None,
        "error": None,
    }


def process_response(row: dict, response, ticket_text: str, instances: list[Instance]) -> dict:
    """Fill a result row from an API response. Refusals, max_tokens, and invalid
    output become failed rows instead of exceptions."""
    usage = response.usage
    row.update(
        served_model=response.model,
        stop_reason=response.stop_reason,
        request_id=getattr(response, "_request_id", None),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
        cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        fallback_used=any(getattr(it, "type", None) == "fallback_message"
                          for it in (getattr(usage, "iterations", None) or [])),
    )
    # Top-level usage covers the attempt that produced the message only.
    row["estimated_cost_usd"] = estimate_cost(response.model, row) or estimate_cost(row["model"], row)

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        row["stop_category"] = getattr(details, "category", None) if details else None
        row["error"] = "refusal"
        return row
    if response.stop_reason == "max_tokens":
        row["error"] = "max_tokens"
        return row

    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        parsed = TicketClassification.model_validate_json(text)
    except ValidationError as e:
        row["error"] = f"invalid_output: {e.error_count()} validation error(s)"
        row["raw_output"] = text
        return row

    kept, dropped = split_by_evidence(parsed.mentions, ticket_text)
    row["output"] = parsed.model_dump(mode="json")
    row["mentions_returned"] = len(parsed.mentions)
    row["dropped_mentions"] = len(dropped)
    row["dropped_mention_details"] = [m.model_dump(mode="json") for m in dropped]
    row["matches"] = [{"text": m.text, "kind": m.kind.value, **match_mention(m.text, instances)} for m in kept]
    return row


def classify_ticket(client, ticket: dict, prompt: Prompt, cmdb: Cmdb, *, run_name: str, model: str,
                    effort: str, max_tokens: int) -> dict:
    """Classify one ticket with one API call and return its result row."""
    row = new_row(ticket, run_name=run_name, model=model, effort=effort, prompt_version=prompt.version)
    subject, description, masked = prepare_ticket_text(ticket)
    instances = cmdb.for_ticket(ticket)
    row["secrets_masked"] = masked
    row["cmdb_instances_for_client"] = len(instances)
    user_message = render_user_message(prompt, subject, description, client_server_hints(instances))
    request = build_request(prompt, user_message, model=model, effort=effort, max_tokens=max_tokens)

    start = time.perf_counter()
    try:
        response = client.beta.messages.create(**request)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
        raise FatalAPIError(f"{type(e).__name__}: {e.message}") from e
    except anthropic.RateLimitError as e:
        row["error"] = f"rate_limit: {e.message}"
    except anthropic.BadRequestError as e:
        row["error"] = f"bad_request: {e.message}"
    except anthropic.APIStatusError as e:
        row["error"] = f"api_status_{e.status_code}: {e.message}"
    except anthropic.APIConnectionError as e:
        row["error"] = f"connection: {e}"
    row["latency_ms"] = round((time.perf_counter() - start) * 1000)
    if row["error"]:
        return row
    return process_response(row, response, f"{subject}\n{description}", instances)


# --- Zendesk (read-only) ---------------------------------------------------


class ZendeskClient:
    """Read-only Zendesk API client. It only ever issues GET requests."""

    def __init__(self, subdomain: str, email: str, token: str, session=None, sleep=time.sleep):
        self.base = f"https://{subdomain}.zendesk.com/api/v2"
        self.session = session or requests.Session()
        self.session.auth = (f"{email}/token", token)
        self.session.headers.update({"Accept": "application/json"})
        self.sleep = sleep

    @classmethod
    def from_env(cls) -> "ZendeskClient":
        missing = [k for k in ("ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN") if not os.environ.get(k)]
        if missing:
            raise SystemExit(f"Missing in .env: {', '.join(missing)}")
        return cls(os.environ["ZENDESK_SUBDOMAIN"], os.environ["ZENDESK_EMAIL"], os.environ["ZENDESK_API_TOKEN"])

    def get(self, path_or_url: str, params: dict | None = None, max_attempts: int = 6) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{self.base}/{path_or_url.lstrip('/')}"
        for _ in range(max_attempts):
            r = self.session.get(url, params=params, timeout=60)
            if r.status_code == 429 or (r.status_code == 503 and "Retry-After" in r.headers):
                try:
                    wait = float(r.headers.get("Retry-After", "60"))
                except ValueError:
                    wait = 60.0
                print(f"Zendesk returned {r.status_code}; waiting {wait:.0f}s (Retry-After)")
                self.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Zendesk still rate limiting after {max_attempts} attempts: {url}")

    def paginate(self, path: str, key: str, params: dict | None = None, limit: int | None = None):
        """Yield items from every page. Follows cursor pagination (meta.has_more
        and links.next) and offset pagination (next_page)."""
        url, count = path, 0
        while url:
            data = self.get(url, params)
            params = None  # next-page URLs already carry the query
            for item in data.get(key) or []:
                yield item
                count += 1
                if limit and count >= limit:
                    return
            links, meta = data.get("links") or {}, data.get("meta") or {}
            url = links.get("next") if meta.get("has_more") else data.get("next_page")


# --- Labels ----------------------------------------------------------------

LABEL_COLUMNS = ["ticket_id", "subject", "predicted_run", "predicted_type", "predicted_intent",
                 "predicted_confidence", "predicted_mentions", "correct_type", "correct_intent",
                 "mentions_correct", "notes"]
HUMAN_COLUMNS = ["correct_type", "correct_intent", "mentions_correct", "notes"]


def read_labels(path: Path = LABELS_PATH) -> dict[str, dict]:
    if not path.exists():
        return {}
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:  # saved by Excel as plain "CSV (Comma delimited)"
        text = raw.decode("cp1252")
    return {r["ticket_id"].strip(): r for r in csv.DictReader(io.StringIO(text)) if (r.get("ticket_id") or "").strip()}


def write_labels(labels: dict[str, dict], path: Path = LABELS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LABEL_COLUMNS)
        writer.writeheader()
        for row in labels.values():
            writer.writerow({c: row.get(c) or "" for c in LABEL_COLUMNS})


# --- Runs and metrics ------------------------------------------------------


def resolve_run(name: str) -> Path:
    if name == "latest":
        runs = sorted(RUNS_DIR.glob("*/results.jsonl"), key=lambda p: p.stat().st_mtime)
        if not runs:
            raise SystemExit("No runs found in data/runs.")
        return runs[-1].parent
    path = RUNS_DIR / name
    if not (path / "results.jsonl").exists():
        raise SystemExit(f"No results for run '{name}' in {path}")
    return path


def latest_rows(rows: list[dict]) -> dict[str, dict]:
    """The last row per ticket (a --retry-failed rerun appends a newer row)."""
    return {r["ticket_id"]: r for r in rows}


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(pct / 100 * len(ordered)))  # nearest rank
    return ordered[min(rank, len(ordered)) - 1]


def ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def compute_metrics(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("error") and r.get("output")]
    billed = [r for r in rows if r.get("input_tokens") is not None]
    costs = [r["estimated_cost_usd"] for r in billed if r.get("estimated_cost_usd") is not None]
    latencies = [r["latency_ms"] for r in billed if r.get("latency_ms") is not None]
    returned = sum(r["mentions_returned"] for r in ok)
    dropped = sum(r["dropped_mentions"] for r in ok)
    with_cmdb = [r for r in ok if r.get("cmdb_instances_for_client")]
    host_mentions = [m for r in with_cmdb for m in r["matches"] if m["kind"] in ("server", "instance")]
    match_status = Counter(m["status"] for m in host_mentions)
    cost_per_ticket = ratio(sum(costs), len(costs))
    return {
        "tickets": len(rows),
        "ok": len(ok),
        "failed": Counter(r["error"].split(":")[0] for r in rows if r.get("error")),
        "refusal_categories": Counter(str(r.get("stop_category")) for r in rows if r.get("error") == "refusal"),
        "fallback_used": sum(1 for r in rows if r.get("fallback_used")),
        "types": Counter(r["output"]["primary_type"] for r in ok),
        "intents": Counter(r["output"]["intent"] for r in ok),
        "confidence": Counter(r["output"]["confidence"] for r in ok),
        "type_unknown_rate": ratio(sum(1 for r in ok if r["output"]["primary_type"] == "unknown"), len(ok)),
        "intent_unknown_rate": ratio(sum(1 for r in ok if r["output"]["intent"] == "unknown"), len(ok)),
        "mentions_returned": returned,
        "mentions_dropped": dropped,
        "quote_drop_rate": ratio(dropped, returned),
        "tickets_with_cmdb": len(with_cmdb),
        "host_mentions_with_cmdb": len(host_mentions),
        "match_status": match_status,
        "match_rate": ratio(match_status["matched"], len(host_mentions)),
        "mean_input_tokens": ratio(sum(r["input_tokens"] + (r.get("cache_read_input_tokens") or 0)
                                       + (r.get("cache_creation_input_tokens") or 0) for r in billed), len(billed)),
        "mean_output_tokens": ratio(sum(r["output_tokens"] for r in billed), len(billed)),
        "cache_read_tokens": sum(r.get("cache_read_input_tokens") or 0 for r in billed),
        "total_cost_usd": sum(costs),
        "cost_per_ticket_usd": cost_per_ticket,
        "cost_per_1000_usd": cost_per_ticket * 1000 if cost_per_ticket is not None else None,
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
    }


def label_metrics(rows_by_id: dict[str, dict], labels: dict[str, dict]) -> dict:
    """Accuracy, confusion counts, and per-class precision/recall against human labels."""
    type_ids = {t["id"] for t in TAXONOMY["types"]}
    intent_ids = {i["id"] for i in TAXONOMY["intents"]}
    result = {"invalid_labels": 0, "mentions_correct": Counter()}
    pairs = {"type": [], "intent": []}
    for tid, row in rows_by_id.items():
        label = labels.get(tid)
        if not label or not row.get("output"):
            continue
        for field, column, valid, predicted in (("type", "correct_type", type_ids, row["output"]["primary_type"]),
                                                ("intent", "correct_intent", intent_ids, row["output"]["intent"])):
            value = (label.get(column) or "").strip()
            if not value:
                continue
            if value not in valid:
                result["invalid_labels"] += 1
                continue
            pairs[field].append((value, predicted))
        mc = (label.get("mentions_correct") or "").strip().lower()
        if mc:
            result["mentions_correct"][mc] += 1
    for field, field_pairs in pairs.items():
        confusion = Counter(field_pairs)
        classes = sorted({c for pair in field_pairs for c in pair})
        per_class = {}
        for c in classes:
            tp = confusion[(c, c)]
            predicted = sum(n for (_, p), n in confusion.items() if p == c)
            actual = sum(n for (t, _), n in confusion.items() if t == c)
            per_class[c] = {"precision": ratio(tp, predicted), "recall": ratio(tp, actual), "support": actual}
        result[field] = {
            "labeled": len(field_pairs),
            "accuracy": ratio(sum(1 for t, p in field_pairs if t == p), len(field_pairs)),
            "confusion": confusion,
            "classes": classes,
            "per_class": per_class,
        }
    return result


# --- Markdown formatting ---------------------------------------------------


def summary_rows(m: dict, lm: dict | None = None) -> list[tuple[str, str]]:
    """The headline metrics as (name, formatted value) pairs, shared by report and compare."""
    rows = [
        ("Tickets", fmt_num(m["tickets"])),
        ("Classified OK", fmt_num(m["ok"])),
        ("Failed", ", ".join(f"{k} {v}" for k, v in m["failed"].most_common()) or "0"),
        ("Served by a fallback model", fmt_num(m["fallback_used"])),
        ("Type unknown rate", fmt_pct(m["type_unknown_rate"])),
        ("Intent unknown rate", fmt_pct(m["intent_unknown_rate"])),
        ("Mentions returned", fmt_num(m["mentions_returned"])),
        ("Quote-drop rate", fmt_pct(m["quote_drop_rate"])),
        ("CMDB match rate (server/instance mentions)",
         f"{fmt_pct(m['match_rate'])} of {m['host_mentions_with_cmdb']}"),
        ("Ambiguous / unmatched", f"{m['match_status']['ambiguous']} / {m['match_status']['unmatched']}"),
        ("Mean input tokens (incl. cached)", fmt_num(m["mean_input_tokens"])),
        ("Mean output tokens", fmt_num(m["mean_output_tokens"])),
        ("Cost per ticket (estimate)", fmt_usd(m["cost_per_ticket_usd"])),
        ("Cost per 1,000 tickets (estimate)", fmt_usd(m["cost_per_1000_usd"], 2)),
        ("Total cost (estimate)", fmt_usd(m["total_cost_usd"])),
        ("Latency p50 / p95", f"{fmt_num(m['latency_p50_ms'])} / {fmt_num(m['latency_p95_ms'])} ms"),
    ]
    if lm:
        for field in ("type", "intent"):
            f = lm.get(field) or {}
            if f.get("labeled"):
                rows.append((f"{field.title()} accuracy", f"{fmt_pct(f['accuracy'])} of {f['labeled']} labeled"))
    return rows


def fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def fmt_usd(x: float | None, places: int = 4) -> str:
    return "n/a" if x is None else f"${x:,.{places}f}"


def fmt_num(x: float | None, places: int = 0) -> str:
    return "n/a" if x is None else f"{x:,.{places}f}"


def md_table(headers: list[str], rows: list[list]) -> str:
    def cell(v) -> str:
        return str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(cell(h) for h in headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)
