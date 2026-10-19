# Build Prompt 6: Ticket Analysis API for Azure

Use this prompt in a new Claude Code session opened in C:\src\ticket-classifier-poc.
It builds on prompt 5 (the taxonomy classifier with keyword rules and the uncertain flag).

## Goal

Expose the server detector and the taxonomy classifier as a JSON HTTP API that can run in Azure
Container Apps, with an OpenAPI (OAS 3.1) document checked into the repo. Callers send ticket text
with an optional subject and an optional Zendesk organization id; the API returns detected servers
and instances as key/value pairs, the taxonomy type and intent, or both. The API imports only the
ticket_analysis package (never tools/), and its image installs only requirements-api.txt
(requirements.txt plus the web and MCP dependencies).

Decisions already made:

- Hosting: Azure Container Apps, from a Docker image in Azure Container Registry.
- Auth: not decided. Build one pluggable auth layer with modes `none` (local only), `api_key`
  (default) and `entra` (Entra ID bearer tokens); switching is configuration, not code.
- zendeskOrgId: used by the detector only (it picks that client's known server names). The taxonomy
  classifier ignores it until the org id experiment is decided.
- Detection pairs: each detection is a key (entity type) and a value (the name), with details.
- Known names: the detector's dictionary of each client's servers and instances becomes a separate,
  cached directory hydrated from the Keystone CMDB, refreshed on a schedule, instead of being frozen
  inside the model file.
- Callers: mainly Keystone; also an MCP server for AI assistants and direct calls from other
  applications. One ticket per request for now; no batch endpoint.
- Data residency: our Azure infrastructure is sufficient; no region constraint.
- Versioning: the `/v1` contract should change rarely. Model and directory versions are reported in
  every response and can change without an API change, so quarterly retraining (to be designed later)
  does not touch callers.

## Read first

README.md (sections "Server and instance detector", "Trying the model by hand"),
tools/detector_ui.py (`analyze()`, `ticket_for_text()`, `App.classify_text()`, `make_app()`),
ticket_analysis/server_detector.py (`detect_servers()`, the detection dict built around line 1083,
the status values, the `ServerDirectory` protocol, `LocalServerDirectory`, `load_model()` and how the
model's features use the directory), ticket_analysis/names.py (`load_cmdb()`, `parse_server_name()`,
data/instances.csv columns), ticket_analysis/ticket_types.py (`classify()`, `load_alert_map()`,
`load_model()`), tools/ticket_types.py (`summary()`), ticket_analysis/type_rules.py,
ticket_analysis/taxonomy.py (TAXONOMY), ticket_analysis/tickets.py (`make_ticket()`),
ticket_analysis/masking.py (`mask_secrets()`), tests/conftest.py.

## Data handling rules (strict)

- data/ holds real client ticket text and is gitignored. Never print or paste ticket text, client
  names, people's names or server names from data/ into the conversation.
- The model files and the known-names directory are client data too: server_detector.joblib holds
  every client's known server and instance names, the directory snapshot holds the CMDB's, and
  ticket_types.joblib's vocabulary contains words from tickets. They never go into git or into the
  Docker image. In Azure they live in a private Blob container read with the app's managed identity.
- Keystone CMDB credentials come from Key Vault, never from code or the repo. The CMDB API's details
  are not known yet: build against an interface with a stub, and never call a real CMDB in tests.
- The API never logs request bodies, subjects, ticket text or detected names. Log method, path,
  status, latency, request id, text length and whether an org id was given.
- No network access in tests (tests/conftest.py blocks sockets). Tests and docs use made-up names
  only (FAKESQL01, Fake Co, CORP\svc-fake, zendeskOrgId 999001).
- Do not deploy to Azure and do not commit. Commit only when asked.

## Environment notes

- Python is .venv\Scripts\python.exe (3.14). Run tests with
  `.venv\Scripts\python.exe -m pytest -q -W error::SyntaxWarning`.
- Create requirements-api.txt: `-r requirements.txt` plus FastAPI, Uvicorn and the MCP Python SDK,
  with exact pinned versions that support Python 3.14. Leave requirements.txt as the package's own
  dependencies (Pydantic 2 and scikit-learn are already pinned there), and add requirements-api.txt
  to requirements-tools.txt so local test runs have everything.
- The Bash tool is Git Bash, and its heredocs collapse `\\` to `\`. Write any file containing
  backslashes with the Write or Edit tool.

## Task

### 1. Service layer (`api/service.py`)

A small module with no HTTP code that loads the models once and exposes `detect()`, `classify()`
and `analyze()`. Reuse the existing functions rather than copying logic:

- First move `ticket_for_text()` and the result shaping in `analyze()` from tools/detector_ui.py
  into the package (for example ticket_analysis/analysis.py), and have the detector UI import them
  from there, so the UI and the API share one implementation and the API never imports tools/.
  Do the same for any other helper both need. Existing tests must pass unchanged apart from imports.
- Detection: build the ticket with the moved `ticket_for_text()` (zendeskOrgId becomes the client
  key; an unknown or missing id means no client's known names apply), load the bundle with
  `ticket_analysis.server_detector.load_model()` and call `detect_servers()`. Use the model's saved
  thresholds; do not accept thresholds in requests.
- Taxonomy: load the bundle with `ticket_analysis.ticket_types.load_model()` and call `classify()`
  with it and `load_alert_map()`. The API must never train a model, so never call
  `tools.ticket_types.load_or_train()`: if a model file is missing or does not match the taxonomy
  version, the service is not ready (see /ready).
- Joblib files are pickles: load only from the configured model directory, and when
  `MODEL_SHA256` values are configured, refuse files whose hash does not match.

### 1a. Known-names directory hydrated from the Keystone CMDB (`api/directory.py`)

The detector reads known names only through the `ServerDirectory` protocol, so the API passes it a
directory loaded from a snapshot instead of the one saved in the model file.

- **Snapshot format:** one versioned JSON file (`directory-<UTC timestamp>.json`) holding, per client
  key (Zendesk org id), the hosts and (host, instance) pairs with their source (`cmdb` or `alert`)
  and environment, plus the snapshot's creation time and record counts. Names are normalized with
  `parse_server_name()`.
- **Sources merged into a snapshot:** the Keystone CMDB (through a `CmdbSource` interface), and the
  names the current directory learned from alert fields, so the detector loses nothing the CMDB does
  not hold. Keep `dw_servers` names out unless the current directory uses them the same way.
- **`CmdbSource` implementations:** `CsvCmdbSource` (data/instances.csv format, for local runs and
  tests) and `KeystoneCmdbSource`, a stub with the settings it will need (`CMDB_BASE_URL`, auth from
  Key Vault, page size, timeout) and a clear `NotImplementedError` until the Keystone API is known.
  List in the report what we need to learn about that API.
- **Refresh:** `python -m api.refresh_directory` builds a snapshot from the sources and writes it to
  the store (local folder or Blob container), keeping the last N. In Azure it runs as a Container
  Apps scheduled job (daily by default). A snapshot that fails validation (empty, or fewer than a set
  share of the previous snapshot's records) is not published.
- **Cache in the API:** load the newest valid snapshot at startup; check the store for a newer one
  every `DIRECTORY_REFRESH_MINUTES` (default 60) and swap it in atomically without a restart. If the
  store is unreachable, keep serving the last snapshot and report its age in /ready detail logs.
  Responses report the snapshot timestamp in `models.directory`.
- **Effect on accuracy:** the detector's features depend on the directory, and it was trained with
  the local one. Before relying on the CMDB-hydrated directory, run `python -m tools.server_detector evaluate` (or
  an equivalent) with a snapshot built from instances.csv plus alert names and report the change
  against the saved directory. The API must not change detection results when the snapshot holds the
  same names as the model's directory (test this).

### 2. Endpoints (`api/app.py`, FastAPI)

All under `/v1`. Request body for the three POST endpoints:

```json
{"text": "string, required, 1 to 200,000 characters",
 "subject": "string, optional, up to 1,000 characters",
 "zendeskOrgId": "string or integer, optional",
 "ticketId": "string, optional, echoed back for the caller's correlation; not used or logged"}
```

| Method and path | Returns |
|---|---|
| `POST /v1/detect` | Detected servers and instances |
| `POST /v1/classify` | Taxonomy type and intent |
| `POST /v1/analyze` | Both, from one request |
| `GET /v1/catalog` | Every key and every enumerated value the responses can contain |
| `GET /health` | Liveness, no auth, no detail |
| `GET /ready` | 200 when both models are loaded, else 503; no auth, no model detail |

Detection response (made-up values):

```json
{"requestId": "…", "apiVersion": "1.0.0",
 "models": {"serverDetector": "2026-10-17T11:10:00Z"},
 "client": {"zendeskOrgId": "999001", "known": true},
 "isAlert": false,
 "entities": {"host": ["FAKESQL01"], "instance": ["REPORTS"], "host_instance": ["FAKESQL01\\REPORTS"]},
 "detections": [
   {"key": "host_instance", "value": "FAKESQL01\\REPORTS", "host": "FAKESQL01", "instance": "REPORTS",
    "status": "known", "field": "description", "start": 40, "end": 57, "typoOf": null, "siblingOf": null}
 ]}
```

`entities` is the compact key/value view (each key lists its distinct values in order of first
appearance); `detections` keeps one entry per occurrence with its position. Map the detector's
internal kinds to the keys `host`, `instance` (a bare instance name) and `host_instance`, and carry
the detector's status, typo and sibling fields under camelCase names. Include `host` on a bare
instance when the detector paired it.

Classification response:

```json
{"requestId": "…", "apiVersion": "1.0.0",
 "models": {"ticketTypes": "…", "taxonomyVersion": "v1-draft"},
 "primaryType": "access_security", "typeSource": "rule:access_request",
 "typeCandidates": [{"id": "access_security", "p": 0.33}],
 "intent": "request_change", "intentSource": "model", "intentCandidates": [],
 "uncertain": false, "uncertainReason": "",
 "alert": null, "notes": []}
```

`POST /v1/analyze` returns `{"requestId", "apiVersion", "models", "detection": {…}, "taxonomy": {…}}`
with the two bodies above minus their own envelope fields.

`GET /v1/catalog` returns, generated from code and taxonomy.json, never hard-coded twice:

- `detectionKeys`: host, instance, host_instance, each with a one-line description;
- `detectionFields`: every field of a detection with its type and, for enumerated fields
  (`status`, `field`), every allowed value with a one-line meaning;
- `types` and `intents`: id, label and definition from taxonomy.json;
- `typeSources` and `intentSources`: model, alert_name, alert_name_partly, alert_default,
  alert_rule and each `rule:<name>` in type_rules.RULES, with a one-line meaning;
- `apiVersion`, `taxonomyVersion` and the loaded model timestamps.

It must not list any client's known names.

Use camelCase for all JSON field names; ids inside values (type ids, statuses) keep their existing
spelling.

### 2a. MCP server (`api/mcp_server.py`)

Expose the same operations to AI assistants with the official MCP Python SDK (pinned), as tools
`detect_servers`, `classify_ticket`, `analyze_ticket` and `get_catalog`, with the same inputs and
JSON outputs as the HTTP endpoints and descriptions written for a model choosing between them. The
tools call the service layer directly, not the HTTP API.

- Serve it from the same container at `/mcp` over the streamable HTTP transport, behind the same auth
  layer as `/v1`, and as a stdio server (`python -m api.mcp_server`) for local use.
- Tool results never include another client's known names; `get_catalog` returns what
  `/v1/catalog` returns.
- Test the tools in-process (list tools, call each with made-up input).

### 2b. Versioning

- The URL version (`/v1`) changes only for breaking changes. Within v1, changes are additive: new
  optional request fields, new response fields, new enum values. Say in the OpenAPI description that
  clients must tolerate unknown fields and enum values (new types, statuses or rule names), and that
  `/v1/catalog` lists the current values.
- Every response carries `apiVersion` and a `models` object: `serverDetector`, `ticketTypes` and
  `directory` (each a version string from the artifact, such as its creation timestamp) and
  `taxonomyVersion`.
- Model files live in the store under a version folder (`models/<version>/`); `MODEL_VERSION` selects
  one, so a retrained model is rolled out or rolled back by changing a setting and starting a new
  Container Apps revision. A detector model saved before the package split names its classes
  `server_detector.*` and does not load; run `python -m tools.convert_model` on it before uploading. Automatic quarterly retraining is out of scope; describe in
  docs/azure-deploy.md where it would plug in (a scheduled job that retrains, evaluates against a
  holdout and publishes a new version folder only if it is not worse).

### 3. Errors and limits

- Errors use RFC 9457 problem details (`application/problem+json`): 400 or 422 for invalid input,
  401 and 403 for auth, 413 when the body exceeds the limit, 415 for a non-JSON body, 503 when the
  models are not loaded, 500 without internal detail.
- Body limit 1 MB; text limit 200,000 characters (as detector_ui.MAX_TEXT); subject 1,000.
- Every response carries an `X-Request-Id` header (echo the caller's if given and well formed).
- Model inference is CPU-bound: run it in a thread pool so the event loop stays responsive, and
  document the worker count setting.

### 4. Auth (`api/auth.py`)

One FastAPI dependency applied to every `/v1` route, chosen by `AUTH_MODE`:

- `none`: allowed only when `ALLOW_NO_AUTH=true` is also set; log a warning at startup.
- `api_key` (default): `X-API-Key` header compared in constant time against a set of keys from the
  `API_KEYS` setting (in Azure, a Key Vault reference). Support several keys so they can be rotated.
- `entra`: validate an Entra ID bearer token (issuer, audience, expiry, signature against the
  tenant's published keys, cached) and require a configured app role. Settings: `ENTRA_TENANT_ID`,
  `ENTRA_AUDIENCE`, `ENTRA_REQUIRED_ROLE`. Tests use a locally generated key pair, not the network.

The OpenAPI document declares both security schemes (`ApiKeyAuth` and `EntraBearer`).

### 5. OpenAPI document

- FastAPI generates the schema from Pydantic models. Give every model, field and endpoint a
  description and a made-up example.
- `python -m api.export_openapi` writes docs/openapi.yaml (OAS 3.1). A test fails when the committed
  file differs from the generated one.
- Serve the schema at `/openapi.json`; serve the interactive docs at `/docs` only when
  `ENABLE_DOCS=true`.

### 6. Configuration

All settings from environment variables, read once with Pydantic settings, documented in
docs/api.md: `MODEL_DIR`, `SERVER_MODEL_FILE`, `TYPES_MODEL_FILE`, `TAXONOMY_NOTES_FILE`,
`MODEL_SHA256` (optional), `AUTH_MODE`, `API_KEYS`, the Entra settings, `ALLOW_NO_AUTH`,
`ENABLE_DOCS`, `WORKERS`, `LOG_LEVEL`, `MODEL_VERSION`, `MODEL_STORE` (local folder or Blob
container URL), `DIRECTORY_STORE`, `DIRECTORY_REFRESH_MINUTES`, `DIRECTORY_KEEP`, the CMDB settings
and `MCP_ENABLED`. Nothing defaults to a path under data/ except for local runs.

### 7. Container and Azure

- Dockerfile: python:3.14-slim, non-root user, copying only ticket_analysis/, api/,
  requirements.txt and requirements-api.txt (no tools/, data/, tests/ or docs/) and installing
  requirements-api.txt, listening on port 8000, `HEALTHCHECK` on /health.
- docs/azure-deploy.md: step-by-step `az` CLI instructions, not run in this session, for a resource
  group, Container Registry, Container Apps environment with Log Analytics, the app with a
  system-assigned managed identity, Key Vault for API keys and CMDB credentials, a private Blob
  container for model versions and directory snapshots with only the app's and the refresh job's
  identities granted access, and the directory refresh as a scheduled Container Apps job. HTTPS ingress only;
  liveness probe /health, readiness probe /ready; minimum one replica to avoid cold starts while
  models load. Note where API Management could sit in front if the auth decision goes that way.
### 7a. Run, demo and debug locally

The API must be easy to run on this machine for demos and debugging, with the same code paths as
Azure.

- **One command:** `python -m api --local` starts Uvicorn on 127.0.0.1:8000 with a local profile:
  models and taxonomy notes from data/models and data/claude_pack/returned, a directory snapshot built
  from data/instances.csv and the model's alert names on first start (no CMDB), `AUTH_MODE=none`,
  `/docs` enabled, auto-reload on code changes, and readable console logs. It refuses to bind to
  anything but loopback without auth. `--auth api_key` runs it with a key from a local `.env` file
  (gitignored) to demo auth.
- **Swagger UI:** `/docs` lets you try every endpoint in the browser; every request model has a
  made-up example filled in, including the access request email from prompt 5.
- **Example requests:** docs/api-examples.http (VS Code REST Client) and
  docs/api-examples.ps1 (PowerShell `Invoke-RestMethod`) covering each endpoint, with and without
  subject and zendeskOrgId, an alert, an auth failure and an oversized body. Made-up text only.
- **Debugger:** a `.vscode/launch.json` entry "API (local)" that runs the local profile under the
  debugger, so breakpoints in the service layer, the detector and the classifier work.
- **Container locally:** `docker compose up` (docker-compose.yml) builds the image and runs it with
  data/models and a local directory snapshot mounted read-only, to check the Azure image before
  deploying.
- **MCP locally:** instructions in docs/api.md for connecting Claude Code or the MCP Inspector to
  the stdio server and to `http://127.0.0.1:8000/mcp`.
- **Debug detail:** with `DEBUG_RESPONSES=true` (refused unless `AUTH_MODE=none` on loopback),
  responses add the detector's scored candidates and the classifier's cleaned text and rule
  checks, as the detector UI shows them. Off by default and never on in Azure.
- **The existing detector UI stays the visual demo.** Add a `--api URL` option to tools/detector_ui.py only
  if it is simple; otherwise note it as a follow-up.

### 8. Tests

With FastAPI's TestClient and small models trained in the tests from made-up tickets (as
tests/test_ticket_types.py does), not the files in data/:

- each endpoint: success, a missing `text`, an oversized body, subject and zendeskOrgId optional,
  an integer and a string org id, an unknown org id;
- detection keys and the `entities` view for a made-up host, bare instance and host_instance;
- `analyze` equals `detect` plus `classify` for the same input;
- `catalog` lists every status the detector can produce and every rule in type_rules.RULES, and no
  client names;
- auth: each mode, wrong and missing key, key rotation, an expired and a wrong-audience token;
- /ready is 503 when a model file is missing and the API never trains one;
- no request text appears in captured logs;
- the committed docs/openapi.yaml matches the generated schema;
- directory: a snapshot built from a made-up instances CSV and alert names; validation refuses an
  empty or shrunken snapshot; the API swaps in a newer snapshot without a restart and keeps the old
  one when the store fails; the same names give the same detections as the model's own directory;
- MCP: the tools are listed and each returns the same result as its HTTP endpoint;
- `ticketId` is echoed and never logged; responses carry the model and directory versions.

## Acceptance

- The three POST endpoints, /v1/catalog, /health and /ready behave as specified, with JSON responses.
- docs/openapi.yaml is OAS 3.1, complete, and matches the code.
- The image builds without data/ and starts with models from a mounted directory.
- Changing AUTH_MODE switches auth without code changes.
- The server detector's and classifier's results through the API equal their results when called
  directly.
- All tests pass; nothing is deployed or committed.
- The directory refresh builds, validates and publishes a snapshot from the CSV source, and the API
  picks it up without a restart. The Keystone source is a documented stub.
- The MCP tools work over stdio and at `/mcp`.
- `python -m api --local` starts the API on this machine with the local models; the example requests
  in docs/api-examples.http and docs/api-examples.ps1 all run against it; the VS Code debug entry
  stops at a breakpoint; `docker compose up` serves the same responses from the container.
- Finish with a short report: the endpoints and MCP tools, an example request and response with
  made-up data, how to run it locally, the detection change measured with a CSV-built snapshot, what
  we need to learn about the Keystone CMDB API, and what remains for the auth decision, the Azure
  deployment and scheduled retraining.
