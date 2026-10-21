# Ticket Analysis API

The API exposes the server detector and the taxonomy classifier of the `ticket_analysis` package over
HTTP (`/v1`) and MCP. It imports only `ticket_analysis` and `api`, never `tools/`, and its image installs
only `requirements-api.txt`. The contract is `docs/openapi.yaml` (OAS 3.1, generated from the code by
`python -m api.export_openapi`; a test fails when it is stale). Deployment to Azure Container Apps is
described in `docs/azure-deploy.md`.

## Endpoints

| Method and path | Returns |
| --- | --- |
| `POST /v1/detect` | Detected servers and instances: a compact `entities` view and one `detections` entry per occurrence |
| `POST /v1/classify` | Taxonomy type and intent, where each came from, the model's candidates and an `uncertain` flag |
| `POST /v1/analyze` | Both, from one request: `{requestId, apiVersion, ticketId, models, detection, taxonomy}` |
| `GET /v1/catalog` | Every detection key and field with its enumerated values, the types and intents, the type and intent sources |
| `GET /health` | Liveness; no auth, no detail |
| `GET /ready` | 200 when both models and the directory are loaded, else 503; no auth, no model detail |
| `POST /mcp` | The MCP endpoint (streamable HTTP, stateless JSON responses), behind the same auth as `/v1` |

The three POST endpoints take one ticket:

```json
{"text": "The REPORTS instance on FAKESQL01 is slow. FAKESQL02\\FINANCE is fine.",
 "subject": "Slow reports", "zendeskOrgId": 999001, "ticketId": "12345"}
```

`text` is required (1 to 200,000 characters); `subject` (up to 1,000 characters), `zendeskOrgId` (an
integer or a string of digits) and `ticketId` (echoed back, never used or logged) are optional. Unknown
fields are refused with 422, so a misspelt `zendeskOrgId` does not pass silently. `zendeskOrgId` selects
the client whose known names the detector resolves against; a missing or unknown id means no client's
names apply (every name is then `possible_new_*`). The classifier ignores it.

A detection response (made-up values):

```json
{"requestId": "3f2b8c1e9a7d4e6f8b0c2d4e6f8a0b1c", "apiVersion": "1.0.0", "ticketId": "12345",
 "models": {"serverDetector": "2026-10-17T15:10:15Z", "ticketTypes": "2026-10-17T20:53:21Z",
            "directory": "2026-10-19T06:00:00Z", "taxonomyVersion": "v1-draft", "modelVersion": null},
 "client": {"zendeskOrgId": "999001", "known": true},
 "isAlert": false,
 "entities": {"host": ["fakesql01"], "instance": ["reports"], "host_instance": ["fakesql02\\finance"]},
 "detections": [
   {"key": "instance", "value": "reports", "text": "REPORTS", "host": "fakesql01", "instance": "reports",
    "status": "known", "field": "description", "start": 4, "end": 11, "typoOf": null, "siblingOf": null,
    "hostCandidates": [], "environment": "Production", "score": 0.95}]}
```

`value` is the normalized name the detector resolved (lower case, no domain or port, corrected when
`status` is `typo_of_known`); `text` is the name as written at `field[start:end]`. A `HOST\MSSQLSERVER`
name is the host itself (key `host`). `hostCandidates` lists the client's own hosts for an `ambiguous`
bare instance. Responses never contain another client's names.

### Versioning

`/v1` changes only for breaking changes. Within v1 changes are additive: new optional request fields,
new response fields and new enum values (types, intents, statuses, sources, rule names). Clients must
ignore fields they do not know and accept values they do not know; `GET /v1/catalog` lists the current
values. Every response carries `apiVersion` and `models`, so a retrained model or a refreshed directory
is visible to callers without an API change.

### Errors and limits

Errors are RFC 9457 problem details (`application/problem+json`) with a stable `code`: 400
`invalid_json`, 401 `missing_api_key`/`invalid_api_key`/`missing_token`/`token_expired`/`invalid_audience`/`invalid_issuer`/`invalid_token`,
403 `missing_role` or `loopback_only`, 404, 405, 413 `body_too_large` (over 1 MB), 415 `unsupported_media_type`, 422
`validation_error` (with the failing fields, never their values), 500 `internal_error` (no internal
detail), 503 `not_ready` or `auth_unavailable`. Every response carries `X-Request-Id`: the caller's when
it is 1 to 128 characters of letters, digits and `._:-`, else a new one.

Inference is CPU-bound and runs in a pool of `WORKERS` threads, so the event loop stays responsive. One
Uvicorn process per container is intended: the models take about 100 MB per process, and Container Apps
scales with replicas.

## MCP tools

| Tool | Same as |
| --- | --- |
| `detect_servers` | `POST /v1/detect` |
| `classify_ticket` | `POST /v1/classify` |
| `analyze_ticket` | `POST /v1/analyze` |
| `get_catalog` | `GET /v1/catalog` |

The tools take the same inputs and return the same JSON as the endpoints (structured content), and call
the service layer directly. They are served at `/mcp` in the API's container (stateless, so any replica
answers) and over stdio with `python -m api.mcp_server` for local use.

## Settings

All settings are environment variables, read once at startup (`api/settings.py`). Nothing defaults to a
path under `data/` except in the local profile (`python -m api --local`, `python -m api.mcp_server`).

| Setting | Default | Meaning |
| --- | --- | --- |
| `MODEL_DIR` | `/models` | Folder the model files are loaded from (and downloaded into when `MODEL_STORE` is set) |
| `SERVER_MODEL_FILE` | `server_detector.joblib` | Detector bundle, a file name in `MODEL_DIR` |
| `TYPES_MODEL_FILE` | `ticket_types.joblib` | Classifier bundle, a file name in `MODEL_DIR` |
| `MODEL_SHA256` | empty | `file=sha256` pairs, comma-separated; when set, every joblib file must match |
| `MODEL_VERSION` | empty | The store's version folder, `models/<MODEL_VERSION>/`; reported as `models.modelVersion` |
| `MODEL_STORE` | empty | A local folder or Blob container URL (`https://<account>.blob.core.windows.net/<container>`) holding `models/<version>/` |
| `DIRECTORY_STORE` | empty | A local folder or Blob URL with an optional path holding `directory-<UTC timestamp>.json` snapshots; empty serves the model's own directory |
| `DIRECTORY_REFRESH_MINUTES` | 60 | How often the API checks the store for a newer snapshot |
| `DIRECTORY_KEEP` | 14 | Snapshots the refresh job keeps |
| `DIRECTORY_MIN_SHARE` | 0.9 | A snapshot with fewer records than this share of the previous one is not published |
| `DIRECTORY_INCLUDE_DW` | true | Keep the model directory's DW2.0 time-logging names (as unconfirmed names) in snapshots |
| `CMDB_SOURCE` | `keystone` | `keystone` (a stub until the API is known), `csv` or `none` |
| `CMDB_CSV_PATH` | empty | The CMDB export in data/instances.csv format, for `CMDB_SOURCE=csv` |
| `CMDB_BASE_URL` | empty | Keystone CMDB API base URL |
| `CMDB_API_KEY` | empty | Keystone credential, a Key Vault reference in Azure |
| `CMDB_PAGE_SIZE` | 500 | Records per page from Keystone |
| `CMDB_TIMEOUT_SECONDS` | 30 | Keystone request timeout |
| `AUTH_MODE` | `api_key` | `none`, `api_key` or `entra` |
| `API_KEYS` | empty | Comma-separated keys of at least 16 characters; several allow rotation |
| `ALLOW_NO_AUTH` | false | Required for `AUTH_MODE=none`, which is also refused unless `API_HOST` is loopback |
| `ENTRA_TENANT_ID` | empty | Tenant whose tokens are accepted |
| `ENTRA_AUDIENCE` | empty | Accepted audiences, comma-separated (the app ID URI and the client id) |
| `ENTRA_REQUIRED_ROLE` | empty | The app role a token must carry |
| `API_HOST` / `API_PORT` | `0.0.0.0` / 8000 | Bind address and port of `python -m api` |
| `ENABLE_DOCS` | false | Serve Swagger UI at `/docs` (`/openapi.json` is always served) |
| `WORKERS` | 4 | Inference threads |
| `MAX_CONCURRENT_REQUESTS` | 64 | Connections and requests one process serves at once; Uvicorn answers 503 beyond it |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `LOG_FORMAT` | `json` | `json` (Azure) or `text` |
| `MCP_ENABLED` | true | Serve `/mcp` |
| `DEBUG_RESPONSES` | false | Add scored candidates, cleaned text and rule checks to responses; refused unless `AUTH_MODE=none` on loopback |

A setting that is missing or unsafe stops the service at startup with a message that never repeats the
values given.

## Auth

`api/auth.py` holds one dependency applied to every `/v1` route and the same check in front of `/mcp`;
`AUTH_MODE` chooses the mode, so switching is configuration.

- `none`: local only (needs `ALLOW_NO_AUTH=true` and a loopback `API_HOST`); logged as a warning.
  Whatever address the server is bound to, it then answers only requests that arrive on a loopback
  address with a loopback Host header (403 `loopback_only` otherwise), so a DNS-rebinding page cannot
  query it.
- `api_key`: the `X-API-Key` header, compared in constant time with every key in `API_KEYS`. To rotate,
  add the new key, move callers, then remove the old one.
- `entra`: an Entra ID access token (`Authorization: Bearer`), checked for its RS256 signature against
  the tenant's published keys (cached for an hour), issuer (v1 or v2 endpoint of `ENTRA_TENANT_ID`),
  audience, expiry (60 seconds leeway) and the app role `ENTRA_REQUIRED_ROLE` (403 without it).

A POST to `/v1` or `/mcp` that carries no credential at all is refused before its body is read, so
anonymous callers cannot make a replica buffer 1 MB bodies; `MAX_CONCURRENT_REQUESTS` caps the rest.
The OpenAPI document declares both schemes, `ApiKeyAuth` and `EntraBearer`, whatever the mode.

## Models and the known-names directory

The API never trains a model. A missing model file, a hash mismatch, a type model for another taxonomy
version, or a detector saved before the package split (run `python -m tools.convert_model` on it) leaves
the service not ready (503), with the reason in the log. The load is retried (after 10 seconds, doubling
to 5 minutes), since Container Apps does not restart a replica whose readiness fails and a cause such as
a role assignment that is still propagating clears by itself.

The alert-name table's free-text notes are not returned (`tableEntry` has type, decides and tickets, and
`notes` keep the remark without the table's text): they were written from real tickets, and some name a
client or a server.

The detector reads known names only through the `ServerDirectory` protocol. The API passes it the newest
valid directory snapshot instead of the directory frozen in the model file:

- **Snapshot:** one JSON file, `directory-<UTC timestamp>.json`, holding per client key (the Zendesk
  organization id, or `name:<client name>` for a CMDB client without one) the hosts with their sources
  and environment counts and the (host, instance) pairs with their sources, plus name-to-organization
  aliases, the creation time and record counts. Sources are `cmdb`, `alert` (names the model's
  directory learned from alert fields) and `dw` (DW2.0 time-logging names, kept as unconfirmed).
- **Refresh:** `python -m api.refresh_directory` builds a snapshot from the CMDB source and the model's
  alert names, refuses to publish it when it is empty or has fewer than `DIRECTORY_MIN_SHARE` of the
  previous snapshot's records (exit code 1), and keeps the newest `DIRECTORY_KEEP`. `--dry-run` builds
  and validates only. It prints counts, never names.
- **Cache:** the API loads the newest valid snapshot at startup, checks for a newer one every
  `DIRECTORY_REFRESH_MINUTES` and swaps it in without a restart; a request keeps the directory it
  started with. When the store is unreachable it keeps serving the last snapshot and logs its age (and
  `/ready` logs the store error). Without a store or a valid snapshot it serves the model's own
  directory, reported as `models.directory = "model:<time>"`.

A snapshot holding the same names as the model's directory gives the same detections (a test checks
this, and on the local test file all 6,620 detections were unchanged). Leaving the DW2.0 names out
changes results, which is why `DIRECTORY_INCLUDE_DW` defaults to true; see "Effect on accuracy" below.

### Effect on accuracy

`python -m tools.directory_effect` builds a snapshot as the refresh job does with its defaults (DW2.0
names included; `--without-dw` leaves them out, `--instances` names the CMDB export) and scores the
saved model under both directories (hand labels, derived labels and the detections that change). Measured on
2026-10-19 with the model of 2026-10-17; data/instances.csv was not on this machine, so the CMDB added no
names:

| Snapshot | Host precision, test file | Host recall, test file | Host recall, training holdout | Bare instance recall, holdout |
| --- | --- | --- | --- | --- |
| The model's own directory | 66.9% (224/335) | 84.5% (224/265) | 88.8% (214/241) | 80.0% (8/10) |
| Alert and DW2.0 names (the default) | 66.9% | 84.5% | 88.8% | 80.0% |
| Alert names only | 65.8% (212/322) | 80.0% (212/265) | 85.5% (206/241) | 60.0% (6/10) |

Alert names only, 441 of the test file's tickets change and 269 of 6,620 detections disappear. Rerun the
tool with the real CMDB export (`--instances`) before deciding whether the DW2.0 names can be dropped.

### Keystone CMDB

`KeystoneCmdbSource` is a stub with the settings it will need; it raises NotImplementedError. To build
it we need to learn:

- the endpoint that lists server and instance records, its paging (page size limit, cursor or offset)
  and rate limits;
- how a client is identified: the Zendesk organization id, a Keystone id with a mapping to it, or a name;
- whether instances come as `HOST\INSTANCE` strings or separate fields, whether FQDNs, ports, aliases
  and cluster or availability group listener names are included, and how the default instance appears;
- the environment field and its values, and whether decommissioned servers are flagged or removed;
- authentication (an Entra app registration the job's managed identity can call, or a key in Key Vault),
  and whether the Container Apps environment can reach it (network path, firewall);
- the expected record count, so `DIRECTORY_MIN_SHARE` can be set, and how often the data changes.

## Run, demo and debug locally

Install the requirements once: `.venv\Scripts\python.exe -m pip install -r requirements-tools.txt`
(it includes requirements-api.txt).

**One command.** `python -m api --local` serves on http://127.0.0.1:8000 with the local profile: models
from data/models, taxonomy notes from data/claude_pack/returned, a directory snapshot in data/directory
(built on first start from data/instances.csv, when present, and the model's alert names), no auth,
`/docs` on, auto-reload on changes to `api/` and `ticket_analysis/`, and readable console logs. It
refuses to bind to anything but loopback without auth. Options: `--auth api_key` (reads `API_KEYS` from
the gitignored `.env`; it prints a generated key to add when there is none), `--debug-responses`,
`--no-reload`, `--host`, `--port`.

**Swagger UI.** http://127.0.0.1:8000/docs lets you try every endpoint; each request has made-up
examples to pick from, including an access request email.

**Example requests.** `docs/api-examples.http` (VS Code REST Client) and `docs/api-examples.ps1`
(PowerShell `Invoke-RestMethod`) cover each endpoint, with and without subject and `zendeskOrgId`, an
alert, an auth failure and an oversized body. For the auth examples run with `--auth api_key`.

**Debugger.** The VS Code launch entry "API (local)" runs the local profile under the debugger without
auto-reload, so breakpoints in `api/service.py`, `ticket_analysis/server_detector.py` and
`ticket_analysis/ticket_types.py` stop.

**Debug detail.** `python -m api --local --debug-responses` adds `debug` to responses: the detector's
scored candidates with their rules, and the classifier's cleaned text and each rule's result, as the
detector UI shows them. It is refused with any auth mode or a non-loopback address, so it is never on
in Azure.

**Container.** `docker compose up --build` builds the Azure image and runs it with data/models, the
taxonomy notes and data/directory mounted read-only and `AUTH_MODE=api_key` with `API_KEYS` from `.env`.
Run `python -m api --local` once first so data/directory has a snapshot. Send the same requests to
http://127.0.0.1:8000 with the `X-API-Key` header.

**MCP.** Over stdio, `python -m api.mcp_server` loads the local models. For Claude Code:

    claude mcp add ticket-analysis -e PYTHONPATH=C:\src\ticket-classifier-poc -- C:\src\ticket-classifier-poc\.venv\Scripts\python.exe -m api.mcp_server

(`PYTHONPATH` makes `api` importable whatever folder the client starts it in; the data paths are
absolute). Over HTTP, with `python -m api --local` running:

    claude mcp add --transport http ticket-analysis-http http://127.0.0.1:8000/mcp

and with `--auth api_key`, add `--header "X-API-Key: <key>"`. The MCP Inspector connects the same way:
`npx @modelcontextprotocol/inspector`, then either the stdio command above or the streamable HTTP URL.

**The detector UI stays the visual demo** (`python -m tools.detector_ui`). It shares the detection code
with the API (`ticket_analysis/analysis.py`) but still loads the model directly; a `--api URL` option is
a follow-up.
