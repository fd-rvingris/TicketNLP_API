# Build Prompt 2 of 3: Transformer Token Tagger

Use this prompt in a new Claude Code session opened in C:\src\ticket-classifier-poc,
after prompt 1 (docs/prompt-1-name-swap-augmentation.md) is done. Prompt 3
compares the models.

## Goal

Add a second detector model: a small pretrained transformer fine-tuned to tag
host, HOST\INSTANCE and bare-instance spans in ticket text. Attention lets every
token use the whole passage, for example linking a "Server" column header to the
name on the next line, where the current model sees only three words on each
side. Sub-word tokenization lets it judge unseen names by shape.

It must plug into the existing pipeline unchanged: the same candidates, rules,
pairing, statuses, `detect_servers()`, the UI (detector_ui.py), hand-label
scoring (server_labels.py) and the probe set from prompt 1.

## Read first

README.md, docs/server-detector-prompt.md, docs/prompt-1-name-swap-augmentation.md,
server_detector.py, server_labels.py, detector_ui.py and their tests. Check what
prompt 1 actually built (augmentation flag, generator, probe eval) before
relying on the names below.

## Data handling rules (strict)

- data/ holds real client ticket text and is gitignored. Never print or paste
  ticket text, client names or server names from data/ into the conversation.
  Print counts, rates and compressed shapes only.
- Ask the user before installing packages or downloading model weights. Pin
  exact versions in requirements.txt (a separate optional file such as
  requirements-transformer.txt is fine). Use the CPU build of PyTorch unless
  the user says a GPU is available. Ticket text never leaves the machine;
  downloading public weights is the only network use, and only with approval.
- Tests pass with sockets blocked and without the downloaded model (skip with a
  clear reason, or use a tiny randomly initialized model built locally).
- Do not commit. Commit only when asked.

## Environment notes

- Python is .venv\Scripts\python.exe (3.14). Check that the pinned torch and
  transformers versions support Python 3.14 on Windows before installing; if
  not, tell the user and propose an alternative (for example a separate 3.12
  virtual environment) instead of downgrading the project.
- The Bash tool is Git Bash; its heredocs collapse `\\` to `\`. Write files
  containing backslashes with the Write or Edit tool.
- Run training in the background with `python -u`; it can take an hour or more
  on CPU. Estimate time on a small subset first and tell the user.
- Saved models must load from other programs: run scripts through the imported
  module (see the end of server_detector.py).

## Design

### Model

Default to `microsoft/deberta-v3-small` (good for token classification); fall
back to `distilroberta-base` if the tokenizer dependencies are a problem. State
which was used and why.

### Labels

BIO tags over sub-word tokens: B/I-HOST, B/I-HOSTINST, B/I-INST, O. Build
character spans from the same derived labels the current model uses
(`gold_names()`, `label_of()` over `ticket_candidates()`), including the
synthetic bare instances and prompt 1's name-swap augmentation, then map spans
to tokens with the tokenizer's offset mapping. Report counts per tag and source.
Sample tickets without any gold span down to a stated ratio.

### Long text

Subject plus the first `max_chars` (20,000) description characters, split into
overlapping windows (for example 256 tokens, stride 64). At prediction time,
merge window outputs by averaging token probabilities in the overlaps.

### Integration

Add `TransformerModel` (new module, for example transformer_detector.py) that
gives the existing pipeline what it needs. Preferred: score each existing
candidate by the maximum token probability of its class over the candidate's
character span, so `ticket_candidates()`, the rules (still at least
`RULE_SCORE`), thresholds, pairing and statuses work as today. That needs the
ticket text, so add a narrow hook: `detect_servers()` calls
`model.scores_for_ticket(ticket, cands)` when the model has it, else
`model.scores(cands)`. Keep `ServerModel` working as before.

Thresholds come from a validation split, chosen as `best_f1_threshold()` does
(rule matches excluded). Save to data/models/server_detector_transformer/
(weights, tokenizer, thresholds, the directory and settings) so detector_ui.py
and server_labels.py can load it with `--model-path`.

### Training and evaluation

- `python transformer_detector.py train` and `evaluate`, with --train, --test,
  --seed, --epochs, --max-steps, --window, --stride, --augment, --model-name.
- evaluate runs the same standard test, unseen-host test and unseen-instance
  test as server_detector.evaluate(), with the same seeds and held-out names,
  so prompt 3 can compare like with like. Each holdout needs its own
  fine-tune; if that is too slow on CPU, say so and offer fewer epochs for the
  holdout runs, reported as such.
- Also run prompt 1's probe eval and, if labels exist, server_labels evaluate.
- Report training time, model size on disk, and prediction time per ticket.
- Write data/reports/transformer-detector-<timestamp>.md and print figures only.

## Deliverables

- transformer_detector.py, the `scores_for_ticket` hook, pinned optional
  requirements, a saved model under data/models/, tests (span to BIO and back,
  window merging, the hook, loading in detector_ui), README section.
- A short summary of figures only, against the prompt 1 results.
