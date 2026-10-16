# Ticket Classifier PoC

A throwaway proof of concept that measures whether an LLM can classify
Fortified Data Zendesk tickets (type, intent) and extract the servers and SQL
instances they mention, matched against the Keystone CMDB. It is not
integrated with Keystone. What carries forward is the taxonomy, the prompt,
the output schema and the hand-labeled results.

## What it does

1. Gets tickets from Zendesk (read-only GET requests) or from an export file.
2. Classifies each ticket with one Claude API call that returns structured
   output: primary type, secondary types, intent, confidence, a short
   rationale, and server/instance mentions with a quote from the ticket text.
3. Rejects any mention whose quote is not in the ticket, then matches mentions
   to the ticket's client instances from a CMDB export.
4. Reports accuracy against hand labels, the unknown rate, the CMDB match rate,
   cost and latency, and compares runs across models or prompt versions.

## Setup (Windows, PowerShell)

    cd C:\src\ticket-classifier-poc
    C:\Python314\python.exe -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    Copy-Item .env.example .env   # then fill in the values
    python -m pytest              # unit tests; no network access

`.env` values:

| Variable | Purpose |
| --- | --- |
| ANTHROPIC_API_KEY | Anthropic API key (PoC workspace with a spend limit) |
| ZENDESK_SUBDOMAIN | e.g. fortifieddata |
| ZENDESK_EMAIL / ZENDESK_API_TOKEN | Agent email and API token; the code only issues GET requests |

## Inputs

- data/tickets.jsonl: one ticket per line with ticket_id, subject,
  description, organization_id, organization_name, created_at, tags, and
  weak_labels: the Zendesk ticket type plus any custom field whose title
  looks like a type or category, kept for comparison only.
- data/instances.csv (optional): client_name, zendesk_organization_id,
  server_name, environment. Export from the Keystone CMDB (u_instance with
  its client). Without it, mentions are extracted but not matched. Tickets
  are matched to a client by organization id, then by organization name.
- samples/sample_tickets.jsonl: two fictional tickets for trying the prompt
  without Zendesk access (`--tickets samples\sample_tickets.jsonl`).

## Commands

    python fetch_tickets.py --since 2026-09-01 --limit 100
    python fetch_tickets.py --ids 12345,12346
    python load_export.py path\to\export.csv
    python classify.py --limit 20 --dry-run          # show the prompt only
    python classify.py --limit 20                     # default model and effort
    python classify.py --model claude-haiku-5-5 --run-name haiku
    python report.py --run latest                     # metrics + labels.csv
    python compare.py --runs opus-v1 haiku            # side-by-side metrics
    python view_tickets.py --open                     # browse tickets in a local HTML page
    python view_tickets.py --run latest --open        # the same, with a run's results
    python baseline.py                                # non-LLM baseline on the training/test exports
    python split_alerts.py data\train_tickets.csv     # writes train_tickets_alerts.csv and _human.csv

Runs are resumable: already classified tickets in a run are skipped. A run
keeps its model, effort, prompt version and taxonomy version; reusing its name
with different settings is refused.

`fetch_tickets.py` uses the Zendesk Search API (newest first, at most 1,000
tickets per query) for `--since`, and adds or replaces tickets in
data/tickets.jsonl by ticket_id. `load_export.py` accepts .csv (common Zendesk
export column names), .json and .jsonl/.ndjson files. CSV cells containing the
text NULL (as SSMS writes SQL NULL) are treated as empty. Optional is_alert
(1/0) and dw_servers (semicolon-separated) columns from the DW2.0 export are
kept on each ticket for review and reporting; they are never sent to the model.
The same holds for the optional staff columns (assignee_key,
status_assignee_keys, team, client_pod, client_primary_dba), which are kept
under `staff` on each ticket for grouping or filtering by DBA. The assignee
values are DW2.0 surrogate keys until the dimension that names them is known.
The export queries fix the end of their date windows (@Today) so a rerun
returns the same tickets. The optional columns dw_platform, dw_severity,
dw_priority, dw_impact and dw_complexity (Zendesk fields from DW2.0) are kept
under `dw_fields`.

Every loaded ticket also gets `alert_fields`, parsed from the text by
alert_parser.py when the subject is a known alert format (null otherwise):
the family (collector "Alert #", [FIRING, Backup Exception Report, Volume
Capacity) and, for collector and [FIRING alerts, labeled fields such as
alert_name, severity, level, server, instance, platform and message. Like
the DW2.0 columns, these are for review and analysis and are never sent to
the model.

`view_tickets.py` writes data/viewer/<ticket file>[-<run>].html: a single page
with no network access that lists the tickets with search and filters (alert
or human, Zendesk type, category, client, alert name, and predicted type when
--run is given), and shows each ticket's fields, weak labels, DW2.0 servers
and fields, parsed alert fields, any run result, and its text either as
exported or as sent to the model, with masked secrets highlighted. Keys: j/k
to move, / to search, t to switch the text view. The page contains ticket
text, so keep it under data/.

The page's "Open ticket file…" button, or dropping a file on the page, loads
another .jsonl (or .json) ticket file in its place. The file is read in the
browser only; nothing is uploaded or saved. The page masks it with the
patterns from common.py, which view_tickets.py embeds, so "As sent to model"
matches classify.py; tests/test_viewer.py checks this in Node.js and skips
the check when Node is not installed. `python view_tickets.py --blank`
writes data/viewer/viewer.html with no tickets, for opening files this way.
A file opened in the page carries no run results.

`classify.py` options:

| Flag | Default | Purpose |
| --- | --- | --- |
| --tickets PATH | data/tickets.jsonl | Ticket file to read |
| --limit N | all | Only the first N tickets in the file |
| --model ID | claude-opus-5-5 | Model, e.g. claude-sonnet-5-5 or claude-haiku-5-5 |
| --effort LEVEL | low | low, medium, high, xhigh or max |
| --prompt-version V | v1 | Uses prompts/classify_V.md |
| --run-name NAME | model family + prompt version, e.g. opus-v1 (effort appended when not low) | Output folder under data/runs |
| --workers N | 4 | Parallel API calls |
| --max-tokens N | 8000 | Output limit per call; thinking counts toward it |
| --retry-failed | off | Re-classify tickets whose last row failed |
| --dry-run | off | Print the prompt for the first selected ticket; no API call |

The dry run prints the system prompt and user message with the ticket
description withheld (console rule below); the full rendered prompt is saved
to data/dry-run/<ticket_id>.txt.

## How a ticket is classified

- One API call per ticket, no tools. The response is constrained with
  structured outputs (`output_config.format`) to a JSON schema generated from
  schema.py and validated again with Pydantic.
- Effort is set explicitly (`low` by default). Thinking stays on; it cannot
  be disabled on claude-opus-5-5.
- For claude-opus-5-5, claude-opus-5, claude-sonnet-5-5 and claude-fable-5-1
  the request opts into server-side refusal fallbacks (`fallbacks: "default"`),
  so a safety-classifier decline is retried on the model Anthropic recommends.
  claude-haiku-5-5 has no fallback. Rows served by a fallback model record
  served_model and fallback_used.
- A `refusal` or `max_tokens` stop reason, output that fails validation, or an
  API error is written as a failed row with an error field instead of stopping
  the run. Authentication, permission and unknown-model errors stop the run.
- The system prompt (role, taxonomy, rules) is identical for every ticket and
  is prompt-cached; the ticket and its client's server list go in the user
  message.
- Estimated cost uses the price table in common.py, which is marked as
  needing confirmation against current Anthropic pricing.

Each run writes data/runs/<run-name>/run.json (settings) and results.jsonl,
one row per ticket with ticket_id, run_name, model, served_model, effort,
prompt_version, taxonomy_version, the parsed output, dropped_mentions, matches
(matched, ambiguous or unmatched with server_names), stop_reason, token
counts, latency_ms, estimated_cost_usd and error.

## Non-LLM baseline

`baseline.py` sets the bar the LLM has to beat. It trains TF-IDF features
(word 1-2 grams, subject and first 8,000 description characters fitted
separately) with logistic regression on data/train_tickets.jsonl and scores
on data/test_tickets.jsonl, predicting the Zendesk weak labels zendesk_type
and category. These come from the DW2.0 exports in docs/dw-export-train.sql and
docs/dw-export-test.sql, which never share a ticket; the script refuses files
that do. Classes with fewer than 20 training tickets are merged into
"(merged rare)", and tickets without the label are left out. It reports a
learning curve (500, 1,000, 2,000 and all training tickets), accuracy and
macro F1 for all tickets, alerts and human tickets, a majority-class reference,
per-class precision and recall, the most common errors, and whether sub type
is redundant with zendesk_type. Options: --targets, --min-count, --sizes,
--max-chars, --seed, --train, --test. The report goes to
data/reports/baseline-<timestamp>.md; the console shows metrics only. It runs
locally and sends nothing to any service.

`split_alerts.py` splits a DW2.0 .csv export or a .jsonl ticket file by its
is_alert column into <name>_alerts and <name>_human files of the same format
next to it (or in --out-dir), so alerts and human tickets can be trained and
scored separately, for example `python baseline.py --train
data\train_tickets_alerts.jsonl --test data\test_tickets_alerts.jsonl`. CSV
rows are copied unchanged. Tickets without a readable is_alert value go in
neither file and are counted.

## Labeling

`report.py` creates or updates data/labels.csv with the predicted values and
empty columns: correct_type, correct_intent, mentions_correct (y/n/partial),
notes. Fill them in Excel using the ids in taxonomy.json, and close the file
before the next run. The next `report.py` run computes accuracy, a confusion
matrix, and per-class precision and recall. Existing human entries are never
overwritten. Reports are written to data/reports/.

## Files that carry forward to Keystone

- taxonomy.json: versioned type and intent lists (v0 is a draft for the
  taxonomy workshop)
- prompts/classify_v*.md: versioned system prompts and user message templates
- schema.py: output schema generated from the taxonomy
- data/labels.csv: the start of the evaluation set (contains client data;
  move it only into approved storage)

## Data handling rules

- Never commit .env or data/. Both are gitignored.
- Do not store this folder in OneDrive or any synced location.
- Delete data/ when the PoC ends unless the labels move into Keystone.
- Obvious secrets (passwords, connection-string credentials, bearer tokens,
  private keys, meeting passcodes and PINs, Teams join-link passcodes) are
  masked before any text is sent to the model.
- The scripts print ticket IDs and subjects only, never descriptions.
