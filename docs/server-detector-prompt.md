# Build Prompt: Server and Instance Detector

Use this prompt in a Claude Code session opened in C:\src\ticket-classifier-poc.

---

## Task

Build a local, non-LLM module that finds SQL Server **hosts** and **named
instances** in free ticket text, including instance names written on their own
("the REPORTS instance", "restart SSRS"). For each mention it reports:

- the kind: `host`, `host_instance` (written as `HOST\INSTANCE`) or
  `bare_instance` (an instance name without its host);
- the host and instance it resolves to, where possible;
- whether it is known (in the known-names directory), a new instance on a known
  host, a possible new host, a possible new instance, or a typo of a known name.

It must detect hosts and instances it has never seen. Known `HOST\INSTANCE`
names are the main source for learning what instance names look like.

Read README.md, docs/alert-parser.md and docs/dw-ticket-schema.md first. README.md
is the specification and must stay accurate when you finish.

## Data handling rules (strict)

- data/ holds real client ticket text and is gitignored. Never print, paste or
  read ticket text (subjects, descriptions, names, emails, server or instance
  names from tickets) into the conversation. Scripts that check data print
  counts, rates and shapes only.
- Nothing in this task calls a network service. The CMDB API does not exist yet;
  see "Known-names directory" below.
- Tests use made-up tickets, hosts and instances only, and must pass with no
  network access (tests/conftest.py blocks sockets).
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
  entry when one exists. About 52% of collector `instance` values are
  `HOST\INSTANCE`; about 14% are not servers at all (for example `Job: ...`), so
  only server-shaped values count.
- `common.py`: `parse_server_name()` normalizes a reference (lower case, drops
  `tcp:`, splits `HOST,PORT` and `HOST\INSTANCE`, strips the domain from an FQDN)
  into host, instance and port; `load_cmdb()` and `match_mention()` match names
  against `data/instances.csv` (client_name, zendesk_organization_id,
  server_name, environment) with host-level and instance-level rules.
- `baseline.py`: the scikit-learn 1.9.1 style to follow (pinned in
  requirements.txt).

## Design

### 1. Known-names directory

Create an interface so the CMDB API can replace the local sources later with no
change to the detector:

```python
class ServerDirectory(Protocol):
    def hosts(self, client_key: str | None = None) -> list[str]: ...           # normalized hosts
    def instances(self, client_key: str | None = None) -> list[tuple[str, str]]: ...  # (host, instance)
    def instance_names(self) -> Counter[str]: ...      # instance name -> number of hosts using it
    def hosts_with_instance(self, instance: str, client_key: str | None) -> list[str]: ...
    def lookup(self, host: str | None, instance: str | None, client_key: str | None) -> dict: ...
```

- `LocalServerDirectory` builds the directory from: server-shaped values in
  `alert_fields`, `dw_servers`, and `data/instances.csv` when present. Split
  every `HOST\INSTANCE` with `parse_server_name()` and record both the host and
  the (host, instance) pair. Key clients by Zendesk organization id, falling
  back to organization name, as `Cmdb.for_ticket` does.
- `lookup` returns the status for a host, a pair or a bare instance, using the
  same host-level and instance-level rules as `match_mention()`.
- `CmdbApiDirectory` is a stub with the same methods that raises
  `NotImplementedError` and documents the expected API shape. Do not call any
  service.
- Report directory size as counts only: clients, hosts, (host, instance) pairs,
  distinct instance names, and how many instance names occur on more than one
  host or client (for example `PROD` or `SSRS`).

### 2. Candidate generation

Split text into tokens on whitespace and punctuation, but keep `\ . - _ , :`
inside a token so `HOST\INST`, `db01.corp.local`, `host,1433` and `tcp:host`
survive. Also extract values from `Server=`, `Data Source=` and `Address=` in
connection strings, and the host in UNC paths (`\\host\share`).

Two candidate rules, both generous, because a mention missed here cannot be
recovered later:

- **Host candidates:** 3 to 63 characters, at least one letter, not a common
  English word (small built-in stop list plus frequent words from human
  tickets), not a pure date, a ticket number, or a KB or error code pattern.
- **Instance candidates:** SQL Server instance names are short (up to 16
  characters) and are often ordinary words (`REPORTS`, `PROD`, `SHAREPOINT`),
  so the stop list must not remove them. Keep a token of 2 to 16 characters
  with a letter if any of these holds:
  - it is a known instance name in the directory, for any client;
  - it is within 3 tokens of an instance cue: `instance`, `named instance`,
    `sql instance`, `\`, `service`, `MSSQL$` (as in `MSSQL$REPORTS`),
    `SQLAgent$`;
  - it looks like an instance name by shape: all upper case, or an upper-case
    word with digits (`SQL2019`, `PRD01`).

Report candidate recall for hosts and for instances separately on the
evaluation set.

### 3. Features: what a host or instance looks like

From the token:
- character n-grams 2 to 5 (TF-IDF, `analyzer="char_wb"`)
- shape: uppercase ratio, digit count, ends in digits, mixes letters and digits,
  length, contains `\`, `.`, `-`, `_` or `,port`, is an IP address
- a compressed pattern such as `AAUTDB01` -> `L4 L2 D2` (one-hot)
- instance vocabulary: how many hosts and clients in the directory use this
  token as an instance name (log scale), and whether it is a known instance of
  the ticket's own client. Common instance names such as `PROD` score high
  here even without a host.
- whether it appears as an `MSSQL$` or `SQLAgent$` service suffix

From the context:
- the 3 lowercase tokens on each side (bag of words, positions not kept)
- flags for: inside a connection string, inside a UNC path, after a
  `Label:` or `Label value` line, next to an instance cue word
- whether a host candidate appears in the same sentence or within 10 tokens

### 4. Features: the client's naming convention

From the directory, build a profile per client for hosts and for instances
separately: common prefixes (first 3 to 5 characters), common compressed
patterns, and the list of known names. Features per candidate:
- starts with one of its own client's common prefixes
- matches one of its own client's common patterns
- edit distance to the nearest known name of the same client, normalized by length
- matches another client's convention but not its own

After scoring, classify near matches:
- distance 1 to 2 where only trailing digits differ (`AAUTDB03` vs `AAUTDB01`):
  possible new sibling host or instance
- distance 1 with the same pattern otherwise (`AAUTBD01` vs `AAUTDB01`): likely
  typo; report as the known name with `typo_of`

Profiles must be built from training data only during evaluation.

### 5. Training labels (no hand labeling)

Every candidate gets one of three labels: `host`, `instance` or `none`. A
`HOST\INSTANCE` token is labeled `host` (it carries both parts); the `instance`
label is for bare instance names.

- Alerts: candidates equal (after normalization) to the parsed `server`, or to
  the host part of a server-shaped `instance`, are `host`. Other candidates are
  `none`. Alert text is templated, so remove the `Server` and `Instance` label
  words before extracting context features, or use alerts for token features
  only. Measure both and keep the better one.
- Human tickets: candidates matching one of the ticket's own `dw_servers` or a
  directory host for the ticket's client are `host`. A token matching a known
  instance name of the ticket's own client, and not part of a `HOST\INSTANCE`
  token, is `instance`. Other candidates are `none`.
- Synthetic bare instances: for each known `HOST\INSTANCE` mention in training
  text, also create an `instance` example from the instance part with the host
  removed from its context, so the model learns instance names without needing
  many natural bare mentions. Keep these out of every evaluation set and report
  how many were added.
- Report counts per label and source. If `none` swamps the others, use
  `class_weight="balanced"` or sample negatives, and say which.

### 6. Model

- One multiclass model over `host`, `instance` and `none`: logistic regression
  (`scikit-learn`) on the combined sparse features, and
  `HistGradientBoostingClassifier` on the dense features (shape, pattern,
  vocabulary, client profile) as a comparison. Report both; default to the
  better one by macro average precision over `host` and `instance`.
- If a single model does poorly on bare instances, try a separate binary
  instance model on instance candidates only, and report both.
- Output class probabilities per candidate. Thresholds for `host` and
  `instance` are CLI options, with defaults chosen from the evaluation (state
  the rule, for example best F1 on a validation split of the training file).

### 7. Pairing a bare instance with its host

For each `bare_instance` detection, in this order:

1. The nearest host detection in the same ticket (same sentence first, then
   within 10 tokens, then anywhere in the ticket), if the directory either has
   that pair or has no instances for that host yet.
2. Otherwise, the ticket client's hosts that have this instance in the
   directory: one host is `resolved`; several are `ambiguous` (list them).
3. Otherwise the host is unknown.

Statuses for each detection:

| Status | Meaning |
| --- | --- |
| `known` | host, or host and instance, in the directory |
| `new_instance_on_known_host` | host known, instance not known on it |
| `possible_new_host` | host not in the directory, scored as a host |
| `possible_new_instance` | bare instance not known for the client, host unknown or not paired |
| `ambiguous` | bare instance known on several of the client's hosts |
| `typo_of_known` | near match to a known name, see section 4 |

### 8. Evaluation

- **Unseen-host test:** group split by normalized host: hold out about 20% of
  hosts, remove them from the directory and from training, and measure recall
  on their mentions in the test file.
- **Unseen-instance test:** group split by instance name: hold out about 20% of
  instance names (all hosts that use them), remove them from the directory and
  from training, and measure recall on their bare and `HOST\INSTANCE` mentions.
  Report separately for instance names unique to one client and for names
  shared across clients, since shared names such as `PROD` are easier.
- **Standard test:** train on train_tickets.jsonl, score test_tickets.jsonl.
- **Pairing accuracy:** for bare instances whose true host is known from the
  same ticket's `dw_servers`, the share paired with the correct host, ambiguous,
  and unpaired.
- Report precision, recall and F1 at several thresholds, and average precision,
  per class (`host`, `instance`) and separately for alerts and human tickets,
  plus candidate-generation recall for each class.
- Report the most common false-positive patterns as compressed shapes and
  counts (for example `L2 D4`), never as raw text. For instances, also report
  how many false positives are common English words.
- Write the report to `data/reports/server-detector-<timestamp>.md` and print a
  short summary of figures only.

### 9. Output for a ticket

```python
detect_servers(ticket: dict, directory: ServerDirectory, model) -> list[dict]
# each: {"text": raw span, "start": int, "end": int,
#        "kind": "host" | "host_instance" | "bare_instance",
#        "host": normalized host or None, "instance": normalized instance or None,
#        "status": see section 7, "candidates": [hosts, when ambiguous],
#        "typo_of": known name or None,
#        "score": float, "evidence": short quote}
```

Store it on the ticket only when asked (for example `--write` adds
`detected_servers` to a copy of the ticket file); never change the source file.

## Deliverables

- `server_detector.py`: directory classes, candidates, features, model,
  pairing and `detect_servers`.
- A CLI with `train` (fit and save the model to `data/models/server_detector.joblib`),
  `evaluate` (all tests, report) and `detect` (run over a ticket file, write
  results to a new .jsonl). Options: `--train`, `--test`, `--host-threshold`,
  `--instance-threshold`, `--seed`, `--holdout-share`.
- Viewer support: show `detected_servers` in its own card with kind, host,
  instance and status, when a ticket has them.
- Tests in `tests/test_server_detector.py` with made-up data covering:
  - tokenization of `HOST\INST`, FQDN, `host,1433`, `tcp:`, `MSSQL$INST`,
    connection strings and UNC paths;
  - host and instance candidate rules, including instance names that are
    common words (`REPORTS`) surviving next to an instance cue;
  - directory building, splitting `HOST\INSTANCE`, the instance vocabulary and
    lookup statuses;
  - client profiles, sibling and typo classification;
  - pairing: nearest host in the ticket, a single client host, ambiguous, and
    unpaired;
  - both grouped holdouts leaving no held-out host or instance name in training
    or in the directory;
  - an end-to-end fit on a small synthetic set that finds a bare instance it
    only saw inside `HOST\INSTANCE` during training.
- README.md section describing the module, the commands, the evaluation and the
  `ServerDirectory` interface. Add joblib to requirements.txt only if it is not
  already installed with scikit-learn, and pin any new dependency.

## Acceptance

- All tests pass (`python -m pytest`), with no network access.
- `python server_detector.py evaluate` runs on the exports and prints figures
  only.
- The report states, for alerts and human tickets separately and for hosts and
  instances separately: candidate recall, precision and recall at the chosen
  thresholds, recall on held-out names, and bare-instance pairing accuracy.
- Nothing under data/ is committed. Commit only when asked.

## Open questions to raise, not resolve

- How the CMDB API identifies a client (Zendesk organization id, Keystone id, or
  name) and whether it returns instances as `HOST\INSTANCE` or separate fields.
- Whether the default instance (`MSSQLSERVER`) should be stored as a host-level
  record, as `match_mention()` assumes today.
- Whether database names should be detected as a separate kind; for now they
  are `none` unless they are also host or instance names.
- Whether there are enough natural bare-instance mentions in human tickets to
  evaluate on; if fewer than about 50, say so and recommend a hand-labeled
  sample.
