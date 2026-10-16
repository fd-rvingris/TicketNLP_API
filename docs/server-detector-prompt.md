# Build Prompt: Server and Instance Detector

Use this prompt in a Claude Code session opened in C:\src\ticket-classifier-poc.

---

## Task

Build a local, non-LLM module that finds SQL Server host and instance names in
free ticket text. For each name it finds, it reports whether the name is a
**known server** (in the known-names directory) or a **possible new server** (not
known, but scored as server-like). It must detect names it has never seen.

Read README.md, docs/alert-parser.md and docs/dw-ticket-schema.md first. README.md
is the specification and must stay accurate when you finish.

## Data handling rules (strict)

- data/ holds real client ticket text and is gitignored. Never print, paste or
  read ticket text (subjects, descriptions, names, emails, server names from
  tickets) into the conversation. Scripts that check data print counts, rates
  and shapes only.
- Nothing in this task calls a network service. The CMDB API does not exist yet;
  see "Known-names directory" below.
- Tests use made-up tickets and server names only, and must pass with no network
  access (tests/conftest.py blocks sockets).
- Reports and model files are written under data/ so they stay out of git.

## What already exists

- `data/train_tickets.jsonl` (10,000) and `data/test_tickets.jsonl` (2,000): DW2.0
  exports loaded by load_export.py. They never share a ticket, and the test file
  is newer. Each ticket has `subject`, `description`, `organization_id`,
  `organization_name`, `is_alert`, `dw_servers` (servers with time logged in
  DW2.0) and `alert_fields`.
- `alert_parser.py`: `alert_fields` holds parsed `server`, `instance` and, for
  Grafana alerts, `instance` or `db_instance`. On the exports, about 90% of
  alerts yield a server-shaped name, and 96% of those match a `dw_servers`
  entry when one exists. About 14% of collector `instance` values are not
  servers (for example `Job: ...`), so only server-shaped values count.
- `common.py`: `parse_server_name()` normalizes a reference (lower case, drops
  `tcp:`, splits `HOST,PORT` and `HOST\INSTANCE`, strips the domain from an FQDN);
  `load_cmdb()` and `match_mention()` match names against `data/instances.csv`
  (client_name, zendesk_organization_id, server_name, environment).
- `baseline.py`: the scikit-learn 1.9.1 style to follow (pinned in
  requirements.txt).

## Design

### 1. Known-names directory

Create an interface so the CMDB API can replace the local sources later with no
change to the detector:

```python
class ServerDirectory(Protocol):
    def known_names(self, client_key: str) -> list[str]: ...   # raw names for one client
    def all_names(self) -> list[str]: ...
    def lookup(self, name: str, client_key: str | None) -> list[str]: ...  # normalized match
```

- `LocalServerDirectory` builds the directory from: server-shaped values in
  `alert_fields`, `dw_servers`, and `data/instances.csv` when present. Key
  clients by Zendesk organization id, falling back to organization name, as
  `Cmdb.for_ticket` does.
- `CmdbApiDirectory` is a stub with the same methods that raises
  `NotImplementedError` and documents the expected API shape. Do not call any
  service.
- Matching uses `parse_server_name()` so `EXSQL01`, `exsql01.corp.local` and
  `EXSQL01,1433` are the same host.

### 2. Candidate generation

Split text into tokens on whitespace and punctuation, but keep `\ . - _ , :`
inside a token so `HOST\INST`, `db01.corp.local`, `host,1433` and `tcp:host`
survive. Also extract values from `Server=`, `Data Source=` and `Address=` in
connection strings, and the host in UNC paths (`\\host\share`).

Keep a token as a candidate if it has 3 to 63 characters and at least one
letter, and is not a common English word (use a small built-in stop list plus
frequent words from human tickets), a pure date, a ticket number, or a KB or
error code pattern. The rule must be generous: missing a real server here
cannot be recovered later. Report candidate recall on the evaluation set.

### 3. Features: what a server name looks like

From the token:
- character n-grams 2 to 5 (TF-IDF, `analyzer="char_wb"`)
- shape: uppercase ratio, digit count, ends in digits, mixes letters and digits,
  length, contains `\`, `.`, `-`, `_` or `,port`, is an IP address
- a compressed pattern such as `AAUTDB01` -> `L4 L2 D2` (one-hot)

From the context:
- the 3 lowercase tokens on each side (bag of words, positions not kept)
- flags for: inside a connection string, inside a UNC path, after a
  `Label:` or `Label value` line

### 4. Features: the client's naming convention

From the directory, build a profile per client: common prefixes (first 3 to 5
characters), common compressed patterns, and the list of known names. Features
per candidate:
- starts with one of its own client's common prefixes
- matches one of its own client's common patterns
- edit distance to the nearest known name of the same client, normalized by length
- matches another client's convention but not its own

After scoring, classify near matches:
- distance 1 to 2 where only trailing digits differ (`AAUTDB03` vs `AAUTDB01`):
  possible new sibling server
- distance 1 with the same pattern otherwise (`AAUTBD01` vs `AAUTDB01`): likely
  typo; report as the known server with `typo_of`

Profiles must be built from training data only during evaluation.

### 5. Training labels (no hand labeling)

- Alerts: candidates equal (after normalization) to the parsed `server` or
  server-shaped `instance` are positive; other candidates are negative. Alert
  text is templated, so remove the `Server` and `Instance` label words before
  extracting context features, or use alerts for token features only. Measure
  both and keep the better one.
- Human tickets: candidates matching one of the ticket's own `dw_servers`, or a
  directory name for the ticket's client, are positive; other candidates are
  negative. These give realistic context.
- Report counts of positives and negatives by source. If negatives swamp
  positives, use `class_weight="balanced"` or sample negatives, and say which.

### 6. Model

- Logistic regression (`scikit-learn`) on the combined sparse features, and
  `HistGradientBoostingClassifier` on the dense features (shape, pattern,
  client profile) as a comparison. Report both; default to the better one by
  average precision.
- Output a probability per candidate. The threshold is a CLI option, with a
  default chosen from the evaluation (state the rule, for example best F1 on a
  validation split of the training file).

### 7. Evaluation

- **Unseen-name test:** split by server name (group split): hold out about 20%
  of normalized host names, remove them from the directory and from training,
  and measure recall on their mentions in the test file. This is the main
  measure of detecting new servers. A random split overstates results.
- **Standard test:** train on train_tickets.jsonl, score test_tickets.jsonl.
- Report precision, recall and F1 at several thresholds, and average precision,
  separately for alerts and human tickets, plus candidate-generation recall.
- Report the most common false-positive patterns as compressed shapes and
  counts (for example `L2 D4`), never as raw text.
- Write the report to `data/reports/server-detector-<timestamp>.md` and print a
  short summary of figures only.

### 8. Output for a ticket

```python
detect_servers(ticket: dict, directory: ServerDirectory, model) -> list[dict]
# each: {"text": raw span, "start": int, "end": int, "normalized": host[\\instance],
#        "status": "known" | "possible_new" | "typo_of_known",
#        "matches": [known names], "score": float, "evidence": short quote}
```

Store it on the ticket only when asked (for example `--write` adds
`detected_servers` to a copy of the ticket file); never change the source file.

## Deliverables

- `server_detector.py`: directory classes, candidates, features, model, and
  `detect_servers`.
- A CLI with `train` (fit and save the model to `data/models/server_detector.joblib`),
  `evaluate` (both tests, report) and `detect` (run over a ticket file, write
  results to a new .jsonl). Options: `--train`, `--test`, `--threshold`,
  `--seed`, `--holdout-share`.
- Viewer support: show `detected_servers` in its own card, marking known,
  possible new and typo, when a ticket has them.
- Tests in `tests/test_server_detector.py` with made-up data covering:
  tokenization of `HOST\INST`, FQDN, `host,1433`, `tcp:`, connection strings and
  UNC paths; candidate filtering; directory lookup and normalization; client
  profiles, sibling and typo classification; the grouped holdout leaving no
  held-out name in training; and an end-to-end fit on a small synthetic set.
- README.md section describing the module, the commands, the evaluation and the
  `ServerDirectory` interface. Add joblib to requirements.txt only if it is not
  already installed with scikit-learn, and pin any new dependency.

## Acceptance

- All tests pass (`python -m pytest`), with no network access.
- `python server_detector.py evaluate` runs on the exports and prints figures
  only.
- The report states, for alerts and human tickets separately: candidate recall,
  precision and recall at the chosen threshold, and recall on held-out server
  names.
- Nothing under data/ is committed. Commit only when asked.

## Open questions to raise, not resolve

- How the CMDB API identifies a client (Zendesk organization id, Keystone id, or
  name) and whether it returns instances as `HOST\INSTANCE` or separate fields.
- Whether database names should be detected as a separate kind; for now they
  are negatives unless they are also server names.
