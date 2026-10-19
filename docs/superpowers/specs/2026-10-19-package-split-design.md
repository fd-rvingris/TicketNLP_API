# Package Split and Documentation Update: Design

Date: 2026-10-19. Branch: server-detector.

## Why

This repository is no longer a throwaway proof of concept. It will become a
ticket analysis API hosted in Azure (docs/prompt-6-ticket-api.md). Today it is
about 7,000 lines of flat scripts at the repository root, with inference,
training, evaluation, labeling and LLM-run code mixed together:

- common.py (753 lines) imports anthropic, python-dotenv and requests, and
  every inference module imports common.py.
- schema.py imports anthropic only to build the structured-output schema.
- ticket_types.py imports its feature function from baseline.py, a training
  script.
- server_detector.py (1,848 lines) holds both the detector and its training
  and evaluation harness.

The README and PLAN.md still describe the project as throwaway.

## Goals

1. A package, `ticket_analysis`, that holds only what loading a model and
   analyzing a ticket needs, and that imports nothing from the tools and none
   of anthropic, dotenv or requests.
2. Every other script moves to `tools/` and runs as `python -m tools.<name>`.
3. No behavior changes: the same inputs give the same outputs, reports and
   saved files.
4. The README, PLAN.md and the docs still in use describe the new layout,
   commands and direction.

Not in scope: the API itself (prompt 6), moving data or model files out of
data/, retraining, CI, a pyproject.toml or installed entry points.

## Layout

```
ticket_analysis/
  __init__.py
  paths.py           REPO_ROOT, DATA_DIR
  jsonl.py           read_jsonl, write_jsonl, append_jsonl, now_iso
  tickets.py         make_ticket and its field helpers (_flag, _server_list, _dw_fields, _staff)
  masking.py         mask_secrets, the secret patterns, prepare_ticket_text
  names.py           ParsedName, parse_server_name, Instance, Cmdb, load_cmdb,
                     client_server_hints, match_mention, normalize_ws
  taxonomy.py        TAXONOMY, TAXONOMY_VERSION, TicketType, Intent, Confidence,
                     MentionKind, Mention, TicketClassification
  taxonomy.json      moved from the root
  alert_parser.py    unchanged
  text_clean.py      clean_text, removed_chars and the patterns
  type_rules.py      unchanged
  server_detector.py ServerDirectory, LocalServerDirectory, CmdbApiDirectory,
                     NameProfile, Knowledge, build_stop_words, tokenizing,
                     candidates, rules, add_features, _Linear, _Boosted,
                     ServerModel, decide, detect_servers, pairing, load_model
  ticket_types.py    features, build_model, load_alert_map, uncertainty,
                     classify, load_model

tools/
  __init__.py
  common.py          load_env, merge_tickets, Prompt, load_prompt, render_user_message,
                     split_by_evidence, the Anthropic client and request code,
                     classify_ticket, ZendeskClient, labels.csv, metrics, formatting
  schema.py          OUTPUT_SCHEMA (anthropic.transform_schema of TicketClassification)
  fetch_tickets.py, load_export.py, classify.py, report.py, compare.py,
  view_tickets.py, viewer_template.html, split_alerts.py, baseline.py,
  claude_pack.py, server_labels.py, alert_layouts.py
  text_clean.py      measure and the measuring command
  server_detector.py gold_names, label_of, synthetic_instance, make_examples,
                     fit_config, select_config, scoring and evaluation,
                     render_report, train / evaluate / detect commands
  ticket_types.py    read_labels, current_labels, train, load_or_train,
                     summary, prompt_rules, the train command
  detector_ui.py, detector_ui.html
  convert_model.py   one-off: rewrites a saved detector model for the new module path

prompts/, docs/, samples/, tests/   stay at the root
taxonomy_v0.json                    stays at the root (history only)
```

The function lists are the intended placement. Where a function is used by
both sides it goes in the package; where only tools use it, it goes in tools.
Final placement is decided by that rule during implementation.

## Rules

- `ticket_analysis` never imports from `tools`, anthropic, dotenv or requests.
- Code moves without changes except import lines and path constants.
  Files are moved with `git mv` where a file keeps its role, so history follows.
- `ServerModel` keeps its `fit` method in the package; the class is not split.
- Paths: `ticket_analysis/paths.py` defines `REPO_ROOT` (the parent of the
  package folder) and `DATA_DIR = REPO_ROOT / "data"`. Every module that used
  `Path(__file__).parent` for the repo root uses these instead. Files that sit
  next to their module (taxonomy.json, detector_ui.html,
  viewer_template.html) keep `Path(__file__).parent`.
- `load_env()` keeps reading `REPO_ROOT / ".env"`.
- No compatibility aliases for the old module names.

## The saved detector model

data/models/server_detector.joblib stores `server_detector.ServerModel`,
`server_detector._Linear` and `server_detector.LocalServerDirectory` by module
path. The type model stores no project classes and needs no change.

`python -m tools.convert_model [paths]` loads each file with
`sys.modules["server_detector"]` pointing at `ticket_analysis.server_detector`,
saves it back, and keeps the original as `<name>.before-package.joblib`.
The weights, thresholds and directory are identical, so no retraining is
needed. Default path: data/models/server_detector.joblib. The other
`*.before-*.joblib` backups are not converted; they load only through the
same script.

## Dependencies

- requirements.txt: what the package needs (pydantic, scikit-learn, which
  brings numpy, scipy and joblib).
- requirements-tools.txt: `-r requirements.txt` plus anthropic, python-dotenv,
  requests and pytest.
- Versions stay as they are pinned today.

## Documentation

- README.md: replace the throwaway framing with the current direction (a
  ticket analysis service that will be exposed as an Azure-hosted API; the
  tools are the local training, evaluation and labeling workflow). Add a short
  layout section. Rewrite every command to `python -m tools.<name>`, and the
  setup step to install requirements-tools.txt. Update "Files that carry
  forward" for the new taxonomy.json location.
- PLAN.md: add a dated note at the top that the throwaway decision is
  superseded and point to the README; the rest stays as the original plan.
- Docs that are instructions for work not yet done get the new commands and
  file paths: prompts 1, 2, 3 and 6, docs/alert-parser.md,
  docs/claude-taxonomy-prompt.md, docs/recommendations-review.md and the
  comments in docs/dw-export-*.sql.
- Historical records stay as written: BUILD_PROMPT.md, HANDOFF-2.md,
  docs/server-detector-prompt.md, prompts 4 and 5.
- The Claude Desktop prompt.md files under data/ are generated and are not
  touched.

## Tests

- Tests stay in tests/ and keep their names. Imports change to the new
  modules; assertions do not change.
- tests/test_server_detector.py runs `python -m tools.server_detector train`
  instead of the script path. tests/test_detector_ui.py reads the source of
  the package detector module.
- New: a test that imports every `ticket_analysis` module in a subprocess
  with anthropic, dotenv, requests and `tools` blocked in `sys.modules`, and
  fails if any import is attempted.
- New: a test for convert_model.py on a small model trained under the old
  module name in a temporary folder.

## Verification

Before the move, on the current code, record:

1. `python server_detector.py detect --tickets data/test_tickets.jsonl` output.
2. The type model's `classify()` output for every ticket in
   data/test_tickets_human.jsonl.
3. `python baseline.py --labels data/claude_pack/returned/taxonomy_sample_labels.jsonl`
   console metrics.

After the move and the model conversion, the same three must be identical,
and the full suite must pass with `-W error::SyntaxWarning`. The detector UI
must start and analyze a made-up text. Outputs stay under data/ or the
scratchpad and are compared by script; no ticket text is printed.

## Commits

On branch server-detector, after the user agrees: one commit for the move and
imports, one for the docs. Nothing under data/ is committed.
