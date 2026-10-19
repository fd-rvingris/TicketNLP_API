# Build Prompt 1 of 3: Name-Swap Augmentation and a Shared Probe Set

Use this prompt in a new Claude Code session opened in C:\src\ticket-classifier-poc.
Prompts 2 (transformer tagger) and 3 (comparison) follow in separate sessions and
depend on what this one builds, so keep the interfaces named here.

## Goal

The server detector (`ticket_analysis/server_detector.py`) finds hosts well when it already knows
the name, and poorly when it does not. The cause is the training labels: every
positive example is a name already in the known-names directory (alert fields,
dw_servers, data/instances.csv), so the model learns "familiar name means host"
instead of "this context and shape mean host".

Fix the training data, not the model: train on real ticket text in which the
known server and instance names are replaced by generated names that have never
been seen. The context stays real; the names are new, so the model can only
succeed by learning context and shape.

Also build a shared probe set of made-up tickets with marked spans, used by all
three prompts to measure detection of unseen names the same way.

## Read first

README.md (the specification; keep it accurate), docs/server-detector-prompt.md
(the original build prompt), docs/alert-parser.md, and ticket_analysis/server_detector.py,
tools/server_detector.py, tools/server_labels.py, tools/detector_ui.py with their tests.

## Data handling rules (strict)

- data/ holds real client ticket text and is gitignored. Never print, paste or
  read ticket text, client names or server names from data/ into the
  conversation. Scripts that inspect data print counts, rates and compressed
  shapes (server_detector.compress) only.
- No network access. Tests must pass with sockets blocked (tests/conftest.py).
- Tests and the probe set use made-up names only.
- Do not commit. Commit only when asked.

## Environment notes (learned the hard way)

- Python is .venv\Scripts\python.exe (3.14). Run tests with
  `.venv\Scripts\python.exe -m pytest -q -W error::SyntaxWarning`.
- The Bash tool is Git Bash, and its heredocs collapse `\\` to `\`. Write any
  file or script containing backslashes (HOST\INSTANCE, UNC paths, regexes)
  with the Write or Edit tool, never with a shell heredoc or `python - <<EOF`.
- Long jobs (train, evaluate take 4 to 8 minutes) run in the background with
  `python -u` so their output is not buffered.
- Model classes live in the ticket_analysis package, so pickled models
  resolve as `ticket_analysis.server_detector.ServerModel`; keep any class a
  saved model needs there, never in a tools/ script run as `__main__`.

## What exists

- Candidates: `ticket_candidates()` tokenizes, keeps host and instance
  candidates, and assigns rule names (`_rule()`); rule matches score
  `RULE_SCORE` (0.95) in `ServerModel.scores()`.
- Labels: `gold_names()` and `label_of()` derive labels; `synthetic_instance()`
  adds bare-instance examples from HOST\INSTANCE mentions; `make_examples()`
  builds training examples with negative sampling (`neg_ratio`) and client
  dropout (`client_dropout`, 30% of tickets get no client features).
- Features: `add_features()`; client profile features are leave-one-out
  (`NameProfile`, `Knowledge.profile_features`, similarity is 0 when the client
  has no other names).
- Model: `ServerModel` with `_Linear` (logistic regression on character 2 to 5
  grams, context words and dense features) or `_Boosted`. `fit_config()` fits;
  `select_config()` picks model kind, alert-context mode and thresholds on a
  20% validation split; `best_f1_threshold()` ignores rule matches.
- Evaluation: `evaluate()` runs the standard test, an unseen-host test and an
  unseen-instance test (grouped holdouts via `holdout_names()` and
  `LocalServerDirectory.without()`), pairing accuracy, a rules table, and writes
  data/reports/server-detector-<timestamp>.md.
- Hand labels: tools/server_labels.py and the Label tab in tools/detector_ui.py. DBAs may
  have saved labels in data/labels/server_spans.jsonl; `python -m tools.server_labels
  evaluate` scores the saved model against them.

## Baseline to beat (report server-detector-20261016-140635.md)

| Measure | Alerts | Human |
| --- | --- | --- |
| Host precision / recall (derived labels) | 38.1% / 96.2% | 82.2% / 79.5% |
| Held-out host recall | 89.5% (340/380) | 80.9% (258/319) |
| Held-out HOST\INSTANCE detected | 100% (19/19) | 100% (29/29) |
| Held-out bare instance | none | 0 of 3 |

Validation AP: host 83.3%, instance 33.5%. Alert precision and instance AP are
understated: alert labels count only the alert's own server, and the rules find
instances the derived labels do not know. Held-out hosts often share a prefix
with siblings left in training, so held-out recall overstates how well truly
novel names are found. The probe set below is the stricter check.

## Task

### 1. Shared probe set

Create `tests/fixtures/probe_tickets.jsonl`: 40 to 60 made-up tickets written
the way DBAs and the collector write (both human emails and alert layouts,
including table-style rows such as "Client Name ... Server ... Message" with
values on the next line). Use invented hosts and instances that follow varied
conventions (`QRX-SQLZ-07`, `wdcsqlp07`, `abc-sql09.corp.local`,
`HOST\INSTANCE`, `host,1433`, UNC paths, MSSQL$NAME, upper and lower case, bare
instances that are ordinary words such as REPORTS or PROD), plus decoys that
must not be detected (logins like CORP\jsmith, database names, file paths,
URLs, KB numbers, "managed instance"). Each line is a ticket in the
tools/load_export.py shape plus `gold_spans`: a list of
`{field, start, end, kind}` with kind host, host_instance or bare_instance.
Write a validator test that checks every span's offsets match its text.

Add `probe_eval(model, directory, path)` (in tools/server_detector.py or a small new
module in tools/) returning precision and recall per class on the probe set, using the
same overlap matching as `server_labels.evaluate_labels`, with rule and model
detections reported separately. Prompt 3 calls it for every model.

### 2. Name generator

`generate_name(kind, rng, *, like=None, avoid=set())`: a new host or instance
name. With `like`, keep the compressed pattern and separators but replace
letters and digits (`aautdb01` -> `kqmrxp47`); without it, sample from a mix
of realistic conventions (site or client prefix, role such as sql, db, prd,
rpt, environment letter, two or three digits, hyphens or underscores, FQDN
suffix sometimes). Instance names are short, mostly upper case, and include
ordinary words. Never return a name in `avoid` (all directory names, all
held-out names, all names already used in the ticket).

### 3. Augmented training tickets

`augment_ticket(ticket, mapping_rng, directory)`: replace every occurrence of
each gold host and gold instance name in the subject and description, case
insensitively, preserving the original casing style (upper, lower, title),
with one generated name per original name (consistent within the ticket).
Update `dw_servers` and `alert_fields` with the same mapping so
`gold_names()` labels the new names. Keep the client, so the case "a new host
at a known client" is learned; client dropout still applies.

Wire it into `make_examples()` with `augment: int = 0` (copies per ticket that
has at least one gold mention) and expose `--augment N` on train and evaluate.
In the holdout experiments, generated names must avoid held-out names, and
held-out names must not appear in augmented text. Report how many augmented
tickets and examples were added, by label and source.

### 4. Measure

Run evaluate with `--augment 0` and with the best of 1 and 2, and the probe
eval for both. Keep augmentation on by default only if held-out recall and
probe recall improve and the standard-test host AP drops by no more than 2
points. State the rule and the result in the report.

## Deliverables

- tools/server_detector.py changes (generator, augmentation, probe eval, CLI flag),
  tests/fixtures/probe_tickets.jsonl, tests for the generator (pattern kept,
  avoid list honored, seeded), augmentation (all occurrences replaced, casing
  kept, labels follow the new names, no held-out name leaks), and probe eval.
- README.md section on augmentation and the probe set.
- A retrained data/models/server_detector.joblib if augmentation is kept on.
- A short summary of figures only: baseline vs augmented on the standard test,
  both holdouts, the probe set and (if any exist) hand labels.
