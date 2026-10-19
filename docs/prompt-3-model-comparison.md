# Build Prompt 3 of 3: Compare the Three Detectors and Choose a Default

Use this prompt in a new Claude Code session opened in C:\src\ticket-classifier-poc,
after prompts 1 and 2 (docs/prompt-1-name-swap-augmentation.md and
docs/prompt-2-transformer-tagger.md) are done.

## Goal

Decide, on evidence, which server detector to use by default:

1. **Baseline:** logistic regression with rules, no augmentation
   (`python -m tools.server_detector train --augment 0`).
2. **Augmented:** logistic regression with rules and name-swap augmentation
   (prompt 1).
3. **Transformer:** the fine-tuned token tagger with rules (prompt 2).

The question that matters most is how well each finds server and instance names
it has never seen, without flooding DBAs with false detections.

## Read first

README.md, the three prompts in docs/, and the latest reports in data/reports/
(server-detector-*, transformer-detector-*, server-labels-*). Confirm what
prompts 1 and 2 actually built before using the names below.

## Data handling rules (strict)

- Never print or paste ticket text, client names or server names from data/
  into the conversation; figures and compressed shapes only.
- No network access, except what prompt 2 already installed.
- Do not commit. Commit only when asked.

## Environment notes

- Python is .venv\Scripts\python.exe; the Bash tool is Git Bash, whose heredocs
  collapse `\\` to `\`, so write backslash-heavy files with Write or Edit.
- Run long jobs in the background with `python -u`.

## Task

### 1. One comparison script

Add `tools/compare_detectors.py` that, for each of the three models, trains on
data/train_tickets.jsonl (or loads an already trained model when its settings
match) and measures, with the same seed and the same held-out names for all
three:

| Measure | Source |
| --- | --- |
| Standard test: host and instance AP, precision and recall at each model's threshold, alerts and human separately | `evaluate()` and its transformer equivalent |
| Unseen-host recall and unseen-instance recall (bare and HOST\INSTANCE, unique vs shared names) | the grouped holdouts |
| Probe set precision and recall per class, rule vs model | prompt 1's probe eval |
| Hand labels, if any: precision, recall, recall on names the directory did not know, labeler agreement | `server_labels.evaluate_labels()` |
| Bare-instance pairing accuracy | `pairing_accuracy()` and the hand labels |
| Cost: training time, model size, prediction time per ticket (median and 95th percentile) | timed in the script |

Write data/reports/model-comparison-<timestamp>.md with one table per measure,
models as columns, and print a short summary of figures only.

### 2. Read the results honestly

- Derived labels understate precision in alerts (they count only the alert's
  own server) and for rule-found instances; say where a difference between
  models could be a labeling artifact rather than a real difference.
- Held-out hosts often share prefixes with siblings left in training; weight
  the probe set and the hand labels (new names) more for "unseen names".
- Note sample sizes; a difference on 3 held-out bare instances means nothing.
- Report the five most common false-positive and false-negative shapes per
  model on the probe set (compressed patterns, never text).

### 3. Recommend and set the default

Decision rule, stated in the report before looking at the winner:
the default is the model with the best recall on unseen names (probe set, and
hand-labeled new names when there are at least 50), provided its probe and
hand-label precision is at least 80% and its prediction time is under one
second per ticket on this machine. Ties go to the cheaper model.

Then make that model the one `tools/detector_ui.py` and `tools/server_labels.py` load by
default, update README.md (which model is the default, why, and how to switch),
and restart nothing that the user is running without asking.

## Deliverables

- tools/compare_detectors.py, its tests (made-up data, a fast path with tiny models),
  the comparison report, the default switched if the evidence supports it,
  README.md updated.
- A one-page summary for the user: the recommendation first, then the figures
  that support it, the caveats, and what more hand labels would settle.
