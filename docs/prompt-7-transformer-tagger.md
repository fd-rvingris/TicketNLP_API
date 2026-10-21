# Build Prompt 7: Transformer Tagger, Ensemble and Learning from Hand Labels

Use this prompt in a new Claude Code session opened in C:\src\ticket-classifier-poc. It supersedes
docs/prompt-2-transformer-tagger.md, which was written before the package split and the API, and
before the plan below was agreed. Prompt 3 (docs/prompt-3-model-comparison.md) still compares the
detectors afterwards; this prompt gives it two more candidates (the ensemble, and each model trained
with hand labels).

## The plan, and where this prompt fits

The server detector finds SQL Server hosts, HOST\INSTANCE names and bare instance names in ticket
text without an LLM. It is a pipeline, and only one step of it is a learned model:

1. Candidates: a tokenizer that keeps `\ . - _ , : $` inside tokens, and a filter.
2. Dictionary pass: the client's confirmed known names, from the known-names directory.
3. Rules for unambiguous wording (each match scores at least `RULE_SCORE`).
4. **Scoring: logistic regression over hand-built features (character n-grams, shape, three words of
   context on each side, the client's naming convention).**
5. Pairing of bare instances with hosts, then status against the directory (known, new instance,
   possible new host, typo of a known name and so on).

The agreed direction:

- **A transformer replaces or joins step 4 only.** Candidates, dictionary, rules, pairing and statuses
  stay as they are. The transformer's advantage is context: attention sees the whole passage (a
  "Server" column header above a name, a sentence about a failover two lines earlier), where the
  current features see three words on each side.
- **Both models can run together as an ensemble.** Their raw scores are not on the same scale
  (fine-tuned transformers are usually overconfident), so they are never compared directly: each is
  calibrated first, then combined, and their agreement is reported as a signal.
- **The detector learns from DBA labels over time.** Today, hand labels (data/labels/server_spans.jsonl
  from the Label tab, Claude's span labels) only score the model; training uses labels derived from
  alert fields, dw_servers and the directory, which only ever mark names already known. Hand labels
  are complete for their ticket (every unmarked candidate is not a server), so they are the best
  training data we have. Both models should train on them, with a permanent holdout kept out.
- **Confirmed names feed the directory** (a DBA-marked name the directory does not know becomes a
  known name at the next refresh). That is a separate, later prompt; do not build it here, but do not
  make it harder.
- Evidence decides. Every addition is kept only if it improves precision or recall on hand-labeled
  data. Prompt 3 picks the default; the API keeps serving the current model until then.

## Read first

README.md (sections "Server and instance detector", "Hand labeling by several DBAs", "Known-names
directory"), docs/prompt-2-transformer-tagger.md (the earlier design; reuse what still fits),
docs/prompt-3-model-comparison.md, docs/prompt-1-name-swap-augmentation.md, docs/api.md,
ticket_analysis/server_detector.py (`ticket_candidates()`, `ServerModel`, `ServerModel.scores()`,
`detect_servers()`, `RULE_SCORE`, `load_model()`), tools/server_detector.py (`gold_names()`,
`label_of()`, `make_examples()`, `fit_config()`, `best_f1_threshold()`, `evaluate()`,
`pairing_accuracy()`, the holdout exclusion of labeled tickets), tools/server_labels.py
(`load_labels()`, `primary_labels()`, `normalize_span()`, `evaluate_labels()`, `agreement()`),
api/service.py (how the API loads the detector), and the tests of each.

Check what exists before relying on names in this prompt. In particular, check whether prompt 1
(name-swap augmentation and the probe set) was built. If it was, use `--augment` and the probe eval
as prompt 2 describes. If not, build without them, and say so in the report; do not build prompt 1
here.

## Data handling rules (strict)

- data/ holds real client ticket text and is gitignored. Never print or paste ticket text, client
  names, people's names or server names from data/ into the conversation. Print counts, rates and
  compressed shapes (`L6 D2`) only.
- Model files contain client data (names in the directory, vocabulary from tickets). They stay under
  data/models/ and never go into git or the Docker image.
- Ask the user before installing packages or downloading model weights. Downloading public weights
  is the only network use. Ticket text never leaves the machine.
- Tests run with sockets blocked (tests/conftest.py), use made-up names only (FAKESQL01, Fake Co,
  REPORTS) and pass without the downloaded weights: build a tiny randomly initialized model locally,
  or skip with a clear reason.
- Do not commit. Commit only when asked.

## Environment notes

- Python is .venv\Scripts\python.exe (3.14). Run tests with
  `.venv\Scripts\python.exe -m pytest -q -W error::SyntaxWarning`.
- Check that pinned torch and transformers versions support Python 3.14 on Windows before installing.
  If they do not, stop and propose an alternative (for example a separate 3.12 virtual environment
  for training only) instead of downgrading the project. Use the CPU build of PyTorch unless the
  user says a GPU is available.
- Put the new dependencies in requirements-transformer.txt (`-r requirements.txt` plus exact pins),
  included from requirements-tools.txt. Do not add them to requirements.txt or requirements-api.txt:
  the API image stays as it is until prompt 3 chooses a model.
- The Bash tool is Git Bash; its heredocs collapse `\\` to `\`. Write files containing backslashes
  with the Write or Edit tool.
- Run training in the background with `python -u`. Time a small subset first and tell the user the
  estimate before a long run.
- Saved models must load from other programs: define every class a saved model needs in the
  ticket_analysis package, never in a tools/ script. `ticket_analysis` never imports from tools/.
  Import torch and transformers lazily inside the transformer module, so the package and the API
  still import without them.

## Task

### 1. Hand labels as training data (both models)

Build this first; it is useful without the transformer.

- A loader in the package or tools (wherever `make_examples()` can reach it) that reads the hand-label
  files, takes one label set per ticket (`primary_labels()`; latest row per labeler), and turns spans
  into character spans per field. Rows with status `skipped` are ignored; `done` with no spans means
  the ticket has no servers.
- **Permanent holdout.** A fixed share of hand-labeled tickets (`--hand-holdout`, default 0.3) is never
  trained on, chosen by a stable hash of the ticket id so a ticket's side never changes as labels
  grow. data/labels/claude_spans_holdout.jsonl is holdout in full. All hand-label scoring uses only
  the holdout side and reports its size.
- **No leakage.** A hand-labeled ticket used for training is removed from every test set
  (the test file, the unseen-host and unseen-instance tests). Report how many were removed.
- **Labels from hand spans.** For a hand-labeled training ticket, its spans replace the derived labels
  for that ticket: a candidate overlapping a hand span takes its class, every other candidate is
  'none'. A hand span with no candidate counts as a candidate-generation miss; report how many and
  their compressed shapes, since those are names steps 1 and 2 cannot reach.
- **Weight.** Hand-labeled examples count more than derived ones (`--hand-weight`, default 3), through
  `sample_weight` for logistic regression and oversampling for the transformer. Choose the default on
  the validation split and report the alternatives tried.
- `python -m tools.server_detector train --hand-labels FILE ...` retrains the current model with them;
  without the flag, training is unchanged.

### 2. Transformer tagger

Follow docs/prompt-2-transformer-tagger.md for the model, labels, windows and integration, with these
changes:

- Model: `microsoft/deberta-v3-small` by default, `distilroberta-base` if tokenizer dependencies are a
  problem. State which was used and why.
- Labels: BIO tags (HOST, HOSTINST, INST) built from the same derived labels as the current model plus
  the hand labels from step 1, mapped to sub-word tokens with the offset mapping.
- Integration through a narrow hook: `detect_servers()` calls `model.scores_for_ticket(ticket, cands)`
  when the model has it, else `model.scores(cands)`. A candidate's score is the maximum token
  probability of its class over its character span. Candidates, rules, thresholds, pairing and
  statuses then work unchanged. `ServerModel` must behave exactly as before (existing tests pass
  unchanged).
- Thresholds from the validation split, as `best_f1_threshold()` chooses them (rule matches excluded).
- Save to data/models/server_detector_transformer/ (weights, tokenizer, thresholds, settings) so
  tools/detector_ui.py and tools/server_labels.py load it with `--model-path`.
- Modules: ticket_analysis/transformer_detector.py (model class, scoring, loading) and
  tools/transformer_detector.py (`train`, `evaluate`, with --train, --test, --seed, --epochs,
  --max-steps, --window, --stride, --model-name, --hand-labels, --hand-weight, and --augment if
  prompt 1 exists).

### 3. Ensemble

`EnsembleModel` in the package, holding a `ServerModel` and a transformer model, usable anywhere a
model is (`scores_for_ticket()`), saved and loaded like the others.

- **Calibration.** On the validation split, fit a calibrator per model and class (isotonic regression,
  or Platt scaling when there are fewer than a few hundred positives; state which) that maps raw
  score to observed precision. Report each model's calibration before and after (reliability table,
  Brier score).
- **Combination.** The ensemble score is a weighted average of the calibrated scores, the weight
  chosen on the validation split. Thresholds are chosen on the combined score as for the other
  models. Never pick "whichever model is more confident".
- **Agreement.** Each detection carries an `agreement` value: `both` (each model alone would have
  detected it), `one` (only one would) or `rule` (a rule or the dictionary decided). Add it to the
  detection dict as an extra key; do not change the API's response models or docs/openapi.yaml in
  this prompt. Report precision on the hand-label holdout for `both` and for `one` separately; if
  `one` is much less precise, that is the case for marking those detections for DBA review later.

### 4. Evaluation and report

`python -m tools.transformer_detector evaluate` runs, with the same seeds and held-out names as
`tools.server_detector evaluate`:

- the standard test, the unseen-host test and the unseen-instance test (each holdout needs its own
  fine-tune; if that is too slow on CPU, offer fewer epochs for the holdouts and report them as such);
- the hand-label holdout: precision, recall, and recall on names the directory did not know, the
  measure the derived labels cannot give;
- the probe set, if prompt 1 built it;

for four detectors: logistic regression as today, logistic regression with hand labels, the
transformer with hand labels, and the ensemble. Also report training time, model size on disk, and
prediction time per ticket on CPU (median and 95th percentile). Prompt 3's decision rule requires
under one second per ticket; say whether each detector meets it.

Write data/reports/transformer-detector-<timestamp>.md and print figures only. Read the results
honestly: note sample sizes, and say where a difference could be a labeling artifact (derived labels
understate precision in alerts and for rule-found instances).

## Out of scope

- Changing the default model in the detector UI, tools/server_labels.py or the API (prompt 3 does that).
- Changing the API image, requirements-api.txt or the API's response contract. If the transformer or
  the ensemble wins in prompt 3, a later prompt decides how the API serves it (PyTorch in the image,
  or an ONNX Runtime export to keep the image small) and adds `agreement` to the response.
- Adding DBA-confirmed names to the known-names directory.
- Scheduled retraining (described in docs/azure-deploy.md as future work).

## Tests

Made-up tickets and names only, no network, no downloaded weights:

- hand spans to examples: overlap gives the span's class, other candidates 'none', a span without a
  candidate is counted as a miss; `skipped` rows ignored; `done` with no spans gives all 'none';
- the holdout split is stable as labels are added, the Claude holdout file is always holdout, and
  hand-labeled training tickets are removed from the test sets;
- `--hand-weight` reaches `sample_weight`; training without `--hand-labels` gives the same model as
  before;
- span to BIO and back, and window merging in the overlaps;
- the `scores_for_ticket` hook, with `ServerModel` results unchanged;
- calibration is monotonic and the ensemble's `agreement` values are set as defined;
- the transformer model and the ensemble save, load, and run through `detect_servers()` and
  detector_ui with `--model-path`;
- the package and the API import without torch installed.

## Deliverables

- The hand-label training path for the current model, ticket_analysis/transformer_detector.py,
  tools/transformer_detector.py, `EnsembleModel`, the `scores_for_ticket` hook,
  requirements-transformer.txt, trained models under data/models/, tests, and README sections
  (training with hand labels, the transformer, the ensemble, how to load each).
- A short report to the user, figures only: the four detectors side by side on the hand-label
  holdout and the unseen-name tests, cost per ticket, what the agreement split shows, how many hand
  labels were used and held out, and how many more labels would make the differences meaningful.
