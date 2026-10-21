# Ticket Analysis

Classifies Fortified Data Zendesk tickets (type, intent) and finds the SQL
Server hosts and instances they mention, matched against the known names of
each client. It started as a proof of concept and is being developed into a
ticket analysis API hosted in Azure (docs/api.md). Today it
runs locally: the server detector and the taxonomy classifier work without an
LLM, and an LLM classifier path measures what a single Claude call can do.

## Layout

| Folder | Holds |
| --- | --- |
| ticket_analysis/ | What the API ships: alert parsing, secret masking, text cleaning, server detection, type classification and the taxonomy. It never imports from tools/, and requirements.txt is all it needs. |
| api/ | The ticket analysis API (FastAPI and MCP) over the package: the service layer, auth, directory snapshots and the refresh job. It imports only ticket_analysis; its image installs only requirements-api.txt. See docs/api.md. |
| tools/ | Data loading, the LLM run workflow, training, evaluation, labeling and the review pages. Run each as `python -m tools.<name>` from the repository root. |
| prompts/ | LLM prompts: the classifier's versions and the Claude labeling and taxonomy prompts. |
| docs/ | The API reference, deployment and design notes, and export queries. |
| tests/ | pytest; no network access. |

## What it does

1. Gets tickets from Zendesk (read-only GET requests) or from an export file,
   and parses the fields of alert tickets.
2. Finds servers and instances in the ticket text with a local model
   (ticket_analysis/server_detector.py), and resolves each against the
   client's known names.
3. Classifies the ticket type and intent locally: the alert-name table for
   alerts, keyword rules, then a model trained on Claude's labels
   (ticket_analysis/ticket_types.py).
4. Alternatively, classifies each ticket with one Claude API call that returns
   structured output (primary type, secondary types, intent, confidence, a
   short rationale, and server/instance mentions with a quote from the ticket
   text), rejects any mention whose quote is not in the ticket, and matches
   mentions to the ticket's client instances from a CMDB export.
5. Reports accuracy against hand labels, the unknown rate, the CMDB match rate,
   cost and latency, and compares runs across models or prompt versions.

## Setup (Windows, PowerShell)

    cd C:\src\ticket-classifier-poc
    C:\Python314\python.exe -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements-tools.txt  # the package, the API, the tools and pytest
    Copy-Item .env.example .env            # then fill in the values
    python -m pytest                       # unit tests; no network access

requirements.txt alone is what the ticket_analysis package needs;
requirements-api.txt adds the API (the image installs only that file), and
requirements-tools.txt adds the tools and tests on top.

`.env` values:

| Variable | Purpose |
| --- | --- |
| ANTHROPIC_API_KEY | Anthropic API key (a workspace with a spend limit) |
| ZENDESK_SUBDOMAIN | e.g. fortifieddata |
| ZENDESK_EMAIL / ZENDESK_API_TOKEN | Agent email and API token; the code only issues GET requests |

## Ticket analysis API

    python -m api --local                  # http://127.0.0.1:8000/docs, local models, no auth
    python -m api --local --auth api_key   # with API_KEYS from .env
    python -m api.mcp_server               # the MCP tools over stdio
    python -m api.refresh_directory --local   # a known-names snapshot from data/instances.csv
    python -m api.export_openapi           # regenerate docs/openapi.yaml
    python -m tools.directory_effect       # detection with a snapshot against the model's directory
    docker compose up --build              # the Azure image with data/ mounted read-only

`POST /v1/detect`, `/v1/classify` and `/v1/analyze` take one ticket's text
(optional subject and Zendesk organization id) and return the detected
servers and instances, the taxonomy type and intent, or both; `GET
/v1/catalog` lists every key and value they can contain. The same operations
are MCP tools at `/mcp`. The contract is docs/openapi.yaml; settings, local
runs and the directory snapshots are in docs/api.md, the Azure Container Apps
deployment in docs/azure-deploy.md, and example requests in
docs/api-examples.http and docs/api-examples.ps1.

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

    python -m tools.fetch_tickets --since 2026-09-01 --limit 100
    python -m tools.fetch_tickets --ids 12345,12346
    python -m tools.load_export path\to\export.csv
    python -m tools.classify --limit 20 --dry-run        # show the prompt only
    python -m tools.classify --limit 20                  # default model and effort
    python -m tools.classify --model claude-haiku-5-5 --run-name haiku
    python -m tools.report --run latest                  # metrics + labels.csv
    python -m tools.compare --runs opus-v1 haiku         # side-by-side metrics
    python -m tools.view_tickets --open                  # browse tickets in a local HTML page
    python -m tools.view_tickets --run latest --open     # the same, with a run's results
    python -m tools.baseline                             # non-LLM baseline on the training/test exports
    python -m tools.split_alerts data\train_tickets.csv  # writes train_tickets_alerts.csv and _human.csv
    python -m tools.server_detector evaluate             # non-LLM server and instance detector (see below)
    python -m tools.detector_ui --open                   # try the saved detector on pasted text or a Zendesk ticket
    python -m tools.claude_pack labels                   # server-labeling batches for a Claude chat (prompts/claude-labeling-prompt.md)
    python -m tools.claude_pack taxonomy                 # digest of all tickets for a taxonomy chat (prompts/claude-taxonomy-prompt.md)
    python -m tools.claude_pack import-labels data\claude_pack\returned\labels_*.jsonl
    .\tools\claude_relabel.ps1                           # relabel Claude's server-name label sets with the Claude Code CLI
    python -m tools.retrain --dry-run                    # retrain both models and gate them (see Scheduled retraining)
    python -m tools.claude_pack types                    # a round of type-labeling batches for Claude Desktop (see Labeling)
    python -m tools.claude_pack import-types data\claude_pack\returned\types_round_001_*.jsonl

Runs are resumable: already classified tickets in a run are skipped. A run
keeps its model, effort, prompt version and taxonomy version; reusing its name
with different settings is refused.

`tools/fetch_tickets.py` uses the Zendesk Search API (newest first, at most 1,000
tickets per query) for `--since`, and adds or replaces tickets in
data/tickets.jsonl by ticket_id. `tools/load_export.py` accepts .csv (common Zendesk
export column names), .json and .jsonl/.ndjson files. CSV cells containing the
text NULL (as SSMS writes SQL NULL) are treated as empty. `--exclude FILE ...`
leaves out tickets already in other ticket files; docs/dw-export-eval.sql pulls
a fresh evaluation sample (tickets since the training window, minus the test
export) and loads it with `--exclude data\train_tickets.jsonl
data\test_tickets.jsonl`. Optional is_alert
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
ticket_analysis/alert_parser.py when the text matches one of the alert layouts (null
otherwise): collector labeled lines, the collector's one-line Markdown and
table layouts, Backup Exception Reports, Grafana [FIRING alerts and Volume
Capacity alerts. Layouts are recognised by their body as well as their
subject, so pasted alert text is parsed too. The fields carry the family, the
layout and values such as alert_name, client_name, server, instance, hosts,
severity and open_date; the fields that name servers are listed once, in
`SERVER_FIELDS`, and the server detector reads them from there. Like the
DW2.0 columns, these are for review and analysis and are never sent to the
model. See [docs/alert-parser.md](docs/alert-parser.md).

    python -m tools.alert_parser coverage data\train_tickets.jsonl data\test_tickets.jsonl
    python -m tools.alert_parser data\train_tickets.jsonl ...  # re-parse alert_fields in place
    python -m tools.alert_layouts inventory                    # group alerts by template; masked report and examples

`python -m tools.alert_layouts inventory` groups the alerts of the ticket files by layout
signature (the template's labels and header lines, values removed) and writes
data/reports/alert-layouts-<timestamp>.md and one masked example file per
layout to data/alert_layouts/, to share with the collector owners. Examples
keep template words only; every other word becomes its shape (SQL01 becomes
L3 D2).

`tools/view_tickets.py` writes data/viewer/<ticket file>[-<run>].html: a single page
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
patterns from ticket_analysis/masking.py, which tools/view_tickets.py embeds, so "As sent to model"
matches tools/classify.py; tests/test_viewer.py checks this in Node.js and skips
the check when Node is not installed. `python -m tools.view_tickets --blank`
writes data/viewer/viewer.html with no tickets, for opening files this way.
A file opened in the page carries no run results.

`tools/classify.py` options:

| Flag | Default | Purpose |
| --- | --- | --- |
| --tickets PATH | data/tickets.jsonl | Ticket file to read |
| --limit N | all | Only the first N tickets in the file |
| --model ID | claude-opus-5-5 | Model, e.g. claude-sonnet-5-5 or claude-haiku-5-5 |
| --effort LEVEL | low | low, medium, high, xhigh or max |
| --prompt-version V | v2 | Uses prompts/classify_V.md (v1 was written for taxonomy v0) |
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
  ticket_analysis/taxonomy.py (via tools/schema.py) and validated again with Pydantic.
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
- Estimated cost uses the price table in tools/common.py, which is marked as
  needing confirmation against current Anthropic pricing.

Each run writes data/runs/<run-name>/run.json (settings) and results.jsonl,
one row per ticket with ticket_id, run_name, model, served_model, effort,
prompt_version, taxonomy_version, the parsed output, dropped_mentions, matches
(matched, ambiguous or unmatched with server_names), stop_reason, token
counts, latency_ms, estimated_cost_usd and error.

## Non-LLM baseline

`tools/baseline.py` sets the bar the LLM has to beat. It trains TF-IDF features
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

    python -m tools.baseline --labels data\claude_pack\returned\taxonomy_sample_labels.jsonl

With --labels, it predicts a label file's primary_type and intent instead,
with stratified cross-validation (--folds, 5) over the labeled tickets found in
--train and --test, merging classes with fewer than --label-min-count (5)
tickets. Scores are split by the labeler's own confidence. It shows how well a
taxonomy's classes separate from the ticket text alone. Report:
data/reports/baseline-labels-<timestamp>.md.

For the type, the keyword rules in `ticket_analysis/type_rules.py` run first, as in the
Classify tab: a rule that fires replaces the model's prediction. The rules are
fixed patterns, not fitted, so every ticket is still scored by a model that
never saw it. The report lists each rule's fires and precision against the
labels, and an uncertain-flag table (accuracy of the tickets the model decides
when its top probability is below p or within gap of the second, against the
rest). --no-rules leaves the rules out. --clean has the model read text with
email boilerplate removed (`ticket_analysis/text_clean.py`); the rules always read cleaned
text. `python -m tools.text_clean` measures what the cleaning removes from the
ticket files, per pattern, as counts only.

Measured 2026-10-17 on Claude's 600 labels (seed 0; each cell accuracy /
macro F1):

| Change | Type: all | Type: high | Type: medium or low | Intent: all | access_security F1 | staff_access F1 | database_refresh F1 | deployment F1 |
|---|---|---|---|---|---|---|---|---|
| Before | 73.3% / 65.1% | 79.2% / 66.2% | 55.1% / 42.0% | 83.2% / 67.6% | 70.0% | 94.0% | 44.4% | 78.5% |
| Cleaning for the model | 73.3% / 65.3% | 78.8% / 65.8% | 56.5% / 43.5% | 82.7% / 67.4% | 70.9% | 94.1% | 46.2% | 79.5% |
| Rules (kept) | 74.0% / 65.6% | 79.7% / 66.8% | 56.5% / 43.2% | 83.2% / 67.6% | 73.2% | 94.0% | 44.4% | 79.5% |
| Both | 73.8% / 65.7% | 79.2% / 66.4% | 57.1% / 44.1% | 82.7% / 67.4% | 72.5% | 94.1% | 46.2% | 79.5% |
| Rules + database_refresh + ha_terms (2026-10-19) | 75.0% / 66.6% | 80.6% / 67.6% | 57.8% / 45.1% | 83.2% / 67.6% | 73.2% | 94.0% | 50.0% | 80.5% |

Averaged over five cross-validation seeds, cleaning the model's input lowered
type accuracy from 74.5% to 73.8% (from 75.2% to 74.4% with the rules on), so
the model reads the raw text and cleaning serves the rules only. The rules
(staff_logon 39 fires, validation_results 16, sip_review 21, all 100%
precision; access_request 11 fires, 91%) add 0.7 points of type accuracy.
The access_request rule was tightened once against the labels (refresh,
restore, script and narrower staff-account exclusions), so its precision is
slightly optimistic. Two rules added on 2026-10-19: database_refresh (5 fires,
100%) and ha_terms (30 fires, 96.7%; the model alone was right on 24 of them),
which raised replication_ha_dr F1 from 70.3% to 77.5%. ha_terms' terms were
chosen from these labels, so its precision is optimistic until the bulk labels
measure it.

`tools/split_alerts.py` splits a DW2.0 .csv export or a .jsonl ticket file by its
is_alert column into <name>_alerts and <name>_human files of the same format
next to it (or in --out-dir), so alerts and human tickets can be trained and
scored separately, for example `python -m tools.baseline --train
data\train_tickets_alerts.jsonl --test data\test_tickets_alerts.jsonl`. CSV
rows are copied unchanged. Tickets without a readable is_alert value go in
neither file and are counted.

## Server and instance detector

`ticket_analysis/server_detector.py` finds database server hosts of every engine (SQL Server,
MySQL and MariaDB, PostgreSQL, Oracle, MongoDB, cloud databases) and their instances (SQL Server
named instances, Oracle SIDs and service names) in ticket text without an LLM, including names it
has never seen and instance names written on their own ("the REPORTS instance", "MSSQL$REPORTS",
"SID=ORCL"). It runs locally and
sends nothing to any service. Training, evaluation and the commands are in
tools/server_detector.py.

    python -m tools.server_detector evaluate                                          # all tests on the exports; report to data/reports/
    python -m tools.server_detector train                                             # fit and save data/models/server_detector.joblib
    python -m tools.server_detector detect --tickets data\test_tickets.jsonl          # rows: ticket_id, detected_servers
    python -m tools.server_detector detect --tickets data\test_tickets.jsonl --write  # a copy of each ticket with detected_servers

A model saved before the package split names its classes by the old module
path and no longer loads; `python -m tools.convert_model` rewrites
data/models/server_detector.joblib in place (the original is kept as
server_detector.before-package.joblib) without changing the model.

Options: --train, --test, --host-threshold, --instance-threshold (default:
best F1 on a 20% validation split of the training file), --seed,
--holdout-share (0.2), --max-chars (description characters read, 20,000),
--neg-ratio ('none' training examples per positive, 10), --model-path, and for
detect --tickets, --output (default data/<tickets>-servers.jsonl) and --write.
detect never changes the source file. The viewer shows `detected_servers` in
its own card (kind, host, instance, status, score) when a ticket has them, so
`python -m tools.view_tickets --tickets data\test_tickets-servers.jsonl` after
`detect --write` shows the results.

Each detection is `{text, field, start, end, kind, host, instance, status,
candidates, typo_of, sibling_of, score, rule, evidence, environment}`. Offsets are into the field
named by `field` (subject or description). `kind` is `host`, `host_instance`
(written as HOST\INSTANCE) or `bare_instance`. `environment` is the one the
client's alerts (collector Environment Name, Grafana env) or the CMDB export
give most often for the resolved host, or null. Statuses:

| Status | Meaning |
| --- | --- |
| known | host, or host and instance, in the directory |
| new_instance_on_known_host | host known, instance not known on it |
| possible_new_host | host not in the directory, scored as a host (`sibling_of` set when only trailing digits differ from a known host) |
| possible_new_instance | bare instance not known for the client, host unknown or not paired |
| ambiguous | bare instance known on several of the client's hosts (`candidates`) |
| typo_of_known | one edit (a swap counts as one) from a known name with the same pattern; reported as the known name |

How it works:

1. Tokens split on whitespace and punctuation but keep `\ . - _ , : $`, so
   HOST\INSTANCE, FQDNs, HOST,PORT, tcp:HOST and MSSQL$INSTANCE survive.
   Connection-string values (Server=, Data Source=, Address=; a value ends at
   a parenthesis, so a TNS descriptor is not one value) and UNC hosts are
   flagged, and a JDBC `host:1521:SID` token splits into its host and SID. Host candidates are 3 to 63 characters with a letter, not a
   stop word (a built-in list plus frequent words in human training tickets,
   minus known names), date, ticket number, KB article, error code, file name
   or email part. Instance candidates are 2 to 16 characters and are a known
   instance name, near an instance cue (instance, service, a backslash,
   MSSQL$, SQLAgent$), or upper case; the stop list does not apply to them.
2. Features: character 2-5 grams of the name, shape (case, digits, separators,
   port, IP), a compressed pattern (`aautdb01` -> `L6 D2`), how many hosts and
   clients use the token as an instance name at other clients, context words
   and flags, and the client's naming convention (own prefixes and patterns,
   similarity to the nearest other own name, matching another client's
   convention but not its own). Name features are leave-one-out (a known
   name does not match itself), and for 30% of training tickets the client
   is hidden, so the model also works for unknown clients and new names.
3. Labels come from the data, not hand labeling: in alerts, the parsed server
   or the host of a server-shaped instance is a host; in human tickets, the
   ticket's dw_servers and the directory's hosts and instance names for its
   client. Each HOST\INSTANCE mention in training also yields a synthetic
   bare-instance example with the host removed. 'none' examples are sampled
   down; no class weights.
4. Logistic regression on all features and gradient boosting on the dense
   features are compared on the validation split, along with two ways of
   handling templated alert text (label words removed from the context, or
   alerts' context ignored), and a separate binary instance model. The best
   macro average precision over host and instance is kept.
5. The dictionary pass runs first. A confirmed name of the ticket's client
   (named in its alert fields or the CMDB export, not only in dw_servers)
   scores 1.0, so it is detected whatever the model says: a confirmed host
   written alone or as HOST\INSTANCE, where a host made of letters only also
   needs a server word (server, host, vm and the like) before it, and a
   confirmed instance name next to an instance cue. A confirmed host stays a
   candidate even when it is a frequent word, if it has a digit or
   punctuation or follows a server word; a letters-only host known only
   from dw_servers is not a host candidate unless it sits in a connection
   string, UNC path or has a port (against hand labels, none of those
   detections were servers). Rules then mark unambiguous wording, and a rule
   match scores at least 0.95, so it is detected whatever the model says.
   Instances: Oracle SIDs and service names (`instance:oracle`: SID=,
   ORACLE_SID=, SERVICE_NAME=, a TNS descriptor, EZConnect `host:1521/NAME`
   and JDBC `@host:1521:NAME` always; "SID NAME", "PDB NAME" or "the NAME
   database" only in a ticket that mentions Oracle, for an upper-case name
   that is not a frequent word), `MSSQL$NAME` and `SQLAgent$NAME`, `NAME instance`,
   `instance NAME`, `instance named NAME` (not filler words such as "the" or
   "this", and not alert template lines), and a known instance of the
   ticket's client written in upper case. Hosts: a server the alert names in
   its own parsed fields, including Grafana's `master_host` (`host:alert_field`);
   HOST\INSTANCE whose host has a digit, is known or sits in a connection
   string (so `CORP\jsmith` logins are left to the model); connection-string
   values; UNC hosts; names written the SQL Server way (HOST,PORT or tcp:) or
   with a database or exporter port (:1433, :3306, :5432, :1521, :27017, :9104
   and the like; `user@host:1521` counts, web ports such as :8080 do not); a name with a digit after
   server, host, vm, box, machine or node, or after "instance on"; a known
   host of the client; and a near known host (one edit with the same
   pattern, or only trailing digits differ; 5 or more characters with a
   digit). Each detection's `rule` names the rule, or is null when the model
   alone found it. The report counts how often each rule fires and agrees with
   the derived labels.
6. A bare instance is paired with the nearest host detection in the ticket
   (same sentence, then within 10 tokens, then anywhere) when the directory has
   that pair or no instances for that host; otherwise with the client's single
   host that has it (several are ambiguous); otherwise it stays unpaired.

`evaluate` runs the standard test (train on the training file, score the test
file), an unseen-host test (20% of hosts removed from the directory and from
training) and an unseen-instance test (20% of instance names, reported
separately for names used by one client and names shared across clients),
plus bare-instance pairing accuracy against the ticket's dw_servers. It
reports candidate-generation recall, precision, recall, F1 and average
precision per class for alerts and human tickets, and the most common
false-positive shapes as compressed patterns, never as text. Profiles and the
feature directory use training data only; test labels use the test tickets'
own dw_servers and alert fields.

### Trying the model by hand

    python -m tools.detector_ui --open      # http://127.0.0.1:8765/

`tools/detector_ui.py` serves a page, on 127.0.0.1 by default, for testing the saved model
(run `python -m tools.server_detector train` first). Paste text, optionally pick the client
whose known names apply, and press Analyze (Ctrl+Enter); or give a Zendesk
ticket URL or number and press Fetch and analyze, which reads that ticket's
subject and description (the first comment) and organization with the
read-only client and the .env credentials. URLs on any other Zendesk
subdomain are refused, so the API token only goes to the configured one. The
page highlights each detection in the text, lists kind, host, instance,
status and score, and shows every candidate with its host and instance
probabilities. Changing a threshold re-scores the current text without
fetching it again. The How it works tab (`#how`) walks through the layers
(tokenizing, candidate filter, dictionary pass, rules, model, resolution)
with a made-up example ticket and the dictionary pass's holdout results.
The Alert labels tab (`#alert-labels`) lists the alert tickets in Claude's
span labels (data/labels/claude_spans.jsonl on data/test_tickets.jsonl; set
with --check-labels and --check-tickets), with each label's status, notes and
span count against the saved model's detections. Opening one shows the
ticket with its detections, the labeled spans, and whether each detection
overlaps a labeled span, for checking whether a detection the label does not
cover is wrong or the label missed the server. Secrets are masked character
for character, and the stored alert fields are used, so the detections match
`python -m tools.server_labels evaluate`.
The Taxonomy tab (`#taxonomy`) lists the types and intents with their
definitions, Claude's sample label counts, the alert tickets the alert-name
table maps to each type, and the rules for choosing the primary type. The
Classify ticket tab (`#classify`) applies the taxonomy to pasted text locally
with `ticket_analysis/ticket_types.py`: a parsed alert takes its type from the alert-name
table in data/claude_pack/returned/taxonomy_notes.md (and intent
fix_incident, or provide_information when it says the condition cleared);
anything else goes to the keyword rules in `ticket_analysis/type_rules.py` (a [Log on to ...]
task, a Validation Results report, a single-area SIP review, a database refresh
subject, a client access request, a subject naming an availability
group, replication, log shipping, Data Guard or a failover but not patching),
which read the text with email boilerplate removed, and then to
TF-IDF and logistic regression trained on Claude's 600 sample labels. A model
type below 30% probability, or within 5 points of the second, is marked
uncertain with the reason; in cross-validation those tickets are right about
27% of the time against about 89% for the rest. The How types are chosen tab
(`#how-types`) walks through these steps. The model is trained on first start and saved to
data/models/ticket_types.joblib; `python -m tools.ticket_types train` refits it. Text and tickets stay in memory and on the page; the
console logs request paths only. Options: --port, --model-path, --train (read
for client names) and --open.

### Hand labeling by several DBAs

    python -m tools.detector_ui --host 0.0.0.0  # serve the network; DBAs open http://<this machine>:8765/#label
    python -m tools.server_labels evaluate      # score the saved model against the labels

The page's Label tab hands out a fixed, seeded sample of tickets (200 human
tickets from data/test_tickets.jsonl by default) for DBAs to mark every host,
HOST\INSTANCE and bare instance name in. There is no sign-in: each labeler
types their name, which is recorded with each label and not verified. Each
ticket gets one label, and an evenly spread 15% get two from different people,
to measure agreement. A ticket handed out is held for that person for 30
minutes. Model suggestions show as dashed outlines that count only when
accepted, and can be hidden for unaided labeling. A ticket saved with nothing
marked records that it mentions no servers. Secrets in the text are masked
character for character, so offsets stay exact. Labels are appended to
data/labels/server_spans.jsonl; the latest row per ticket and labeler counts,
and labelers can reopen and correct their own tickets.

Serving the network (`--host 0.0.0.0`) shows client ticket text to anyone who
can reach the machine, so run it only on an internal machine. Zendesk lookups
are off in that mode unless `--zendesk on` is given, because anyone reaching
the page could use the token. The server accepts this machine's name and
addresses as Host headers; add a DNS alias with `--allow-host`. Options:
--label-tickets, --label-sample, --label-kind (human, alerts, all),
--label-overlap, --label-seed and --labels-path.

`python -m tools.server_labels evaluate` scores the saved model on one label per ticket
(the first labeler's): precision, recall, and recall split by whether the
directory already knew the name, which is the measure the derived labels
cannot give, plus results per rule and for the model, bare-instance pairing
where a host was typed, and span agreement between labelers on the
double-labeled tickets. The report goes to data/reports/server-labels-<timestamp>.md.

### Known-names directory

The detector reads known names only through the `ServerDirectory` protocol,
so the CMDB API can replace the local sources without changing it. Names are
normalized by `parse_server_name()`; `client_key` is the Zendesk organization
id, or `name:<organization name>` when there is none; the default instance
MSSQLSERVER counts as the host alone.

| Method | Returns |
| --- | --- |
| hosts(client_key=None) | normalized hosts (all clients when None) |
| instances(client_key=None) | (host, instance) pairs |
| instance_names() | instance name -> number of hosts using it |
| instance_clients() | instance name -> number of clients using it |
| hosts_with_instance(instance, client_key) | hosts that have the instance |
| lookup(host, instance, client_key) | {status, hosts}, with the host and instance rules of `match_mention()` |
| clients() | client keys |
| client_for(ticket) | the ticket's client key, by organization id, then name |
| confirmed_hosts(client_key=None) | hosts named in alert fields or the CMDB export |
| confirmed_instances(client_key=None) | (host, instance) pairs from the same sources |
| environment(host, client_key) | the environment those sources give most often, or None |

`LocalServerDirectory` builds it from server-shaped alert fields, dw_servers
and data/instances.csv when present. Names from alert fields and the CMDB
export are also kept as confirmed, with their environment; dw_servers names
are known but not confirmed. `CmdbApiDirectory` is a stub that raises
NotImplementedError and documents the expected API shape. The API does not
use the directory saved in the model file: it serves versioned snapshots
built from the Keystone CMDB and the model's alert names (api/directory.py,
docs/api.md).

## Labeling

`tools/report.py` creates or updates data/labels.csv with the predicted values and
empty columns: correct_type, correct_intent, mentions_correct (y/n/partial),
notes. Fill them in Excel using the ids in ticket_analysis/taxonomy.json, and close the file
before the next run. The next `python -m tools.report` run computes accuracy, a confusion
matrix, and per-class precision and recall. Existing human entries are never
overwritten. Reports are written to data/reports/.

### Server-name labels from the Claude Code CLI

    .\tools\claude_relabel.ps1 -Limit 1   # one batch, to try it
    .\tools\claude_relabel.ps1            # every remaining batch, then import and score

Claude's span labels (data/labels/claude_spans.jsonl on the test file and
claude_spans_holdout.jsonl on the training holdout) cover database servers of
every engine: SQL Server, MySQL and MariaDB, PostgreSQL, Oracle, MongoDB and
cloud databases, with the Windows or Linux servers they run on. Oracle SIDs and
service names count as bare instances; MySQL, PostgreSQL and MongoDB have
hosts only. The prompt is prompts/claude-labeling-prompt.md. The first labels
covered SQL Server only; they are kept as `*_sqlserver_only.jsonl`.

`tools/claude_relabel.ps1` labels the same tickets again without Claude
Desktop. It writes batches with `python -m tools.claude_pack labels
--from-labels`, sends each batch to `claude -p` with the prompt as the system
prompt, and checks each reply with `claude_pack check-reply` (every ticket
exactly once, valid JSONL), retrying up to three times. Each call is a new
session with no tools, MCP servers, settings or saved history, so no context
carries over between batches. Finished batches are kept under
data/claude_pack/relabel/, so a rerun resumes. When all batches of a set are
done it backs up the old label file once, replaces it with the new labels and
runs `python -m tools.server_labels evaluate`. Options: -Set test|holdout|all,
-Model (default claude-opus-5-5), -Effort, -MaxAttempts, -Limit, -Fresh (new
batches, earlier replies discarded). Ticket text is masked as for Claude
Desktop and sent to Claude under your Claude Code login.

    .\tools\claude_label_all.ps1 -Limit 20   # the next 20 batches of the remaining tickets

`tools/claude_label_all.ps1` (`claude_relabel.ps1 -Set rest`) labels every
ticket the two sets above do not cover, about 11,600 in some 460 batches, into
new files: data/labels/claude_spans_train_rest.jsonl (training data) and
claude_spans_test_rest.jsonl (scored when done). After a failed call it waits
longer each time, and it stops after three failed batches in a row, as when a
usage limit is reached; a rerun resumes.

### More type labels from Claude Desktop

The type model learns from Claude's labels. Accuracy was still rising at the
full 600 (cross-validated type accuracy 57% with 120 training tickets, 66% with
240, 72% with 360, 74% with 480), so more labels should help. Without API
access, they come in rounds through Claude Desktop:

    python -m tools.claude_pack types   # writes data\claude_pack\types\round_NNN\
    python -m tools.claude_pack import-types data\claude_pack\returned\types_round_NNN_*.jsonl
    python -m tools.ticket_types train  # or restart the detector UI, which refits on newer labels
    python -m tools.baseline --labels data\claude_pack\returned\taxonomy_sample_labels.jsonl data\labels\claude_types.jsonl

A round picks unlabeled human tickets from the training and test exports
(--tickets, 500), never one already labeled or sent in an earlier round. Half
(--weak-share) are the tickets the current model leans most toward a weak type
(database_refresh, engine_errors, data_request_query, configuration_change,
install_migrate_upgrade) and the rest are random. Each ticket is cleaned of
email boilerplate (ticket_analysis/text_clean.py), masked, cut at --max-chars (4,000) and
written in batches of 50 with a manifest and prompt.md, which is built from
ticket_analysis/taxonomy.json and the prompt v2 rules. Follow prompt.md: one Claude Desktop
Project with the prompt as its instructions, one chat per batch. import-types
checks every returned line (known ticket, type, intent and confidence),
appends it to data/labels/claude_types.jsonl with its round, batch and
taxonomy version, skips repeats, and lists tickets still missing per batch.
tools/ticket_types.py trains on the taxonomy sample and these labels together, later
labels winning, and leaves out labels made for another taxonomy version. Keep
the evaluation export out of the rounds so it stays an independent test.

## Scheduled retraining

    python -m tools.retrain --dry-run     # train and gate, publish nothing
    python -m tools.retrain --install     # publish a passing version and use it locally
    python -m tools.retrain --store https://<account>.blob.core.windows.net/<container>

Rule changes take effect without retraining; retrain after a batch of rule
changes or when new labels or tickets arrive. `tools/retrain.py` trains both
models into data/retrain/<version>/ (the version is the UTC date), then scores
the new and the serving server detector on every span label set present
(Claude's test and holdout sets, the rest of the test file, DBA labels) and
cross-validates the type model with the keyword rules on. It publishes the
version only when no host or instance F1, type or intent accuracy or macro F1
falls more than --tolerance points (1.0) below the serving version, and every
keyword rule that fires on at least 5 labeled tickets is still at least 90%
precise. A passing version goes to data/models/versions/<version>/ with
metrics.json (figures, gate result, SHA-256 of each model); --install also
copies it into data/models/, keeping the replaced files as
`*.before-<version>.joblib`; --store uploads it to the API's model store and
prints the MODEL_VERSION and MODEL_SHA256 values to serve it. Promoting a
version in the API stays a reviewed change (docs/azure-deploy.md, step 8).
The type model's serving figures come from the serving folder's metrics.json,
since scoring a model on the labels it learned from would flatter it; the
first run has none and compares the server detector only. Exit code 0 means
published, 1 not published, so a scheduler shows a failed run. Export current
tickets from DW2.0 into data/ before it runs.

Quarterly, at 03:00 on the first day of January, April, July and October:

    # cron (Linux)
    0 3 1 1,4,7,10 * cd /opt/ticket-classifier-poc && .venv/bin/python -m tools.retrain --install >> data/retrain/retrain.log 2>&1

    # Windows Task Scheduler, from PowerShell (run once to register; schtasks has no
    # quarterly schedule, so this is a monthly schedule limited to the four months)
    schtasks /Create /TN "Ticket classifier retrain" /SC MONTHLY /M JAN,APR,JUL,OCT /D 1 /ST 03:00 `
      /TR "cmd /c cd /d C:\src\ticket-classifier-poc && .venv\Scripts\python.exe -m tools.retrain --install >> data\retrain\retrain.log 2>&1"

## Files that carry forward to Keystone

- ticket_analysis/taxonomy.json: versioned type and intent lists. v1-draft (20 types, 7
  intents) was proposed by Claude from a digest of all 12,000 tickets
  (prompts/claude-taxonomy-prompt.md) and awaits team review; its notes, with
  the Zendesk category and alert name mappings, are in
  data/claude_pack/returned/taxonomy_notes.md. taxonomy_v0.json keeps the
  earlier draft for comparison.
- prompts/classify_v*.md: versioned system prompts and user message templates
- ticket_analysis/taxonomy.py: the type and intent enums and the output schema,
  generated from the taxonomy; tools/schema.py turns it into the form the
  structured outputs API accepts
- data/labels.csv: the start of the evaluation set (contains client data;
  move it only into approved storage)

## Data handling rules

- Never commit .env or data/. Both are gitignored.
- Do not store this folder in OneDrive or any synced location.
- Keep data/ only on approved machines: the tickets, labels and models in it
  contain client data and move only into approved storage.
- Obvious secrets (passwords, connection-string credentials, bearer tokens,
  private keys, meeting passcodes and PINs, Teams join-link passcodes) are
  masked before any text is sent to the model.
- The scripts print ticket IDs and subjects only, never descriptions.
