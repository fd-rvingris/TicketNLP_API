# Package Split and Documentation Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the flat scripts into a `ticket_analysis` package (inference only) and a `tools` folder (everything else, run as `python -m tools.<name>`), with no behavior change, and update the docs for the new layout and direction.

**Architecture:** Foundation modules are extracted from common.py and schema.py first, with the root files re-exporting them so every script keeps working. Then each inference module moves into the package and its training, evaluation or command code moves to tools/. Last, the remaining scripts move to tools/, the temporary re-exports are removed, and before and after outputs are compared.

**Tech Stack:** Python 3.14, pytest 9.1.1, scikit-learn 1.9.1, pydantic 2.14.1, joblib.

**Spec:** docs/superpowers/specs/2026-10-19-package-split-design.md

## Global Constraints

- `ticket_analysis` never imports from `tools`, anthropic, dotenv or requests.
- Code moves without changes except import lines, path constants and command strings in messages and docstrings (`python X.py` becomes `python -m tools.X`).
- Use `git mv` when a file keeps its role, so history follows.
- `REPO_ROOT` is the parent of the package folder; `DATA_DIR = REPO_ROOT / "data"`. Files that sit next to their module (taxonomy.json, detector_ui.html, viewer_template.html) keep `Path(__file__).parent`.
- No compatibility aliases for old module names.
- Run every test command as `.venv/Scripts/python.exe -m pytest -q -W error::SyntaxWarning` from the repository root. Baseline: 272 passed.
- Never print ticket text. Outputs that contain ticket text stay under data/.
- Nothing under data/ or .env is committed. Commit only with the user's approval (see each task's last step).
- Use the Write or Edit tool, not shell heredocs, for files that contain backslashes.

## Review Focus

1. Running a moved command with `python -m tools.X` from the repository root: it must find data/, .env, prompts/ and its HTML template exactly as before. Covered by Task 7's smoke runs and the path assertions in Task 2.
2. Loading the existing detector model after the move: must work only after conversion, with identical results. Covered by Task 5's convert test and Task 8's comparison.
3. A model trained after the move must store `ticket_analysis.server_detector.ServerModel`, never `__main__` or `tools.server_detector`. Covered by the updated subprocess test in Task 5.
4. Importing the package in a clean interpreter must not pull in anthropic, dotenv, requests or tools, including through lazy imports inside functions. Covered by Task 2's isolation test, which Task 3 extends to call the lazy paths.
5. The detector UI's Classify, Label and Alert types tabs still load their models and label files. Covered by the existing tests/test_detector_ui.py and Task 8's UI start.

---

### Task 1: Record the outputs of the current code

No code changes. These files are what Task 8 compares against.

**Files:**
- Create: `C:\Users\RYANVI~1\AppData\Local\Temp\claude\c--src-ticket-classifier-poc\b9cfcf7e-12aa-4f7c-acf9-1d810f81dc34\scratchpad\split_check.py`
- Output: `data/verify/package-split/before/` (gitignored)

**Interfaces:**
- Produces: `split_check.py types <out.jsonl>` and `split_check.py compare <before_dir> <after_dir>`, used by Task 8.

- [ ] **Step 1: Write the check script**

```python
"""Record and compare outputs for the package split. Prints counts only, never ticket text.

  split_check.py types OUT.jsonl        type and intent of every human test ticket
  split_check.py compare BEFORE AFTER   compare the files recorded in two folders
"""
import json
import sys
from pathlib import Path

REPO = Path(r"C:\src\ticket-classifier-poc")
sys.path.insert(0, str(REPO))


def types(out: Path) -> None:
    import joblib
    try:
        from ticket_analysis import ticket_types as tt
        from ticket_analysis.jsonl import read_jsonl
    except ImportError:
        import ticket_types as tt
        from common import read_jsonl
    bundle = joblib.load(REPO / "data" / "models" / "ticket_types.joblib")
    alert_map = tt.load_alert_map()
    rows = []
    for t in read_jsonl(REPO / "data" / "test_tickets_human.jsonl"):
        r = tt.classify(t.get("subject") or "", t.get("description") or "", bundle, alert_map)
        rows.append({"ticket_id": t["ticket_id"], "primary_type": r["primary_type"], "type_source": r["type_source"],
                     "type_candidates": r["type_candidates"], "intent": r["intent"],
                     "intent_candidates": r["intent_candidates"], "uncertain": r["uncertain"]})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(f"{len(rows):,} tickets classified")


def baseline_lines(path: Path) -> list[str]:
    # the report path and generation time differ between runs
    return [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if "Report" not in ln and "data\\reports" not in ln and "data/reports" not in ln]


def compare(before: Path, after: Path) -> None:
    ok = True
    for name in ("detect.jsonl", "types.jsonl"):
        same = (before / name).read_bytes() == (after / name).read_bytes()
        ok &= same
        print(f"{name}: {'identical' if same else 'DIFFERENT'}")
    a, b = baseline_lines(before / "baseline.txt"), baseline_lines(after / "baseline.txt")
    differing = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
    ok &= differing == 0
    print(f"baseline.txt: {'identical' if not differing else f'{differing} lines differ'}")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:]
    types(Path(rest[0])) if cmd == "types" else compare(Path(rest[0]), Path(rest[1]))
```

- [ ] **Step 2: Record the three outputs**

```bash
cd /c/src/ticket-classifier-poc
B=data/verify/package-split/before; mkdir -p $B
.venv/Scripts/python.exe server_detector.py detect --tickets data/test_tickets.jsonl --output $B/detect.jsonl
.venv/Scripts/python.exe "$SCRATCH/split_check.py" types $B/types.jsonl
.venv/Scripts/python.exe baseline.py --labels data/claude_pack/returned/taxonomy_sample_labels.jsonl > $B/baseline.txt
```

`$SCRATCH` is the scratchpad path above. Expected: detect prints ticket and detection counts; types prints a ticket count; baseline.txt has metrics only. Check that `grep -c . $B/baseline.txt` is above 0.

- [ ] **Step 3: Confirm the baseline suite**

Run: `.venv/Scripts/python.exe -m pytest -q -W error::SyntaxWarning`
Expected: 272 passed.

---

### Task 2: Foundation modules and the import isolation test

**Files:**
- Create: `ticket_analysis/__init__.py`, `ticket_analysis/paths.py`, `ticket_analysis/jsonl.py`, `ticket_analysis/tickets.py`, `ticket_analysis/masking.py`, `ticket_analysis/names.py`, `ticket_analysis/taxonomy.py`
- Move: `taxonomy.json` to `ticket_analysis/taxonomy.json` (git mv)
- Modify: `common.py` (remove moved code, re-export it), `schema.py` (keep only OUTPUT_SCHEMA, re-export the rest)
- Test: `tests/test_package_imports.py`

**Interfaces:**
- Produces:
  - `ticket_analysis.paths`: `REPO_ROOT: Path`, `DATA_DIR: Path`, `INSTANCES_PATH: Path`
  - `ticket_analysis.jsonl`: `now_iso() -> str`, `read_jsonl(path: Path) -> list[dict]`, `write_jsonl(path: Path, rows: list[dict]) -> None`, `append_jsonl(path: Path, row: dict) -> None`
  - `ticket_analysis.tickets`: `STAFF_FIELDS`, `DW_FIELDS`, `_flag`, `_server_list`, `_dw_fields`, `_staff`, `make_ticket(...)` (same signature as today)
  - `ticket_analysis.masking`: `REDACTED`, `_SECRET_PATTERNS`, `mask_secrets(text) -> tuple[str, int]`, `_PROMPT_TAGS`, `PROMPT_TAG_REPLACEMENT`, `prepare_ticket_text(ticket) -> tuple[str, str, int]`
  - `ticket_analysis.names`: `ParsedName`, `parse_server_name(name) -> ParsedName | None`, `Instance`, `Cmdb`, `load_cmdb(path=INSTANCES_PATH) -> Cmdb`, `match_mention(text, instances) -> dict`
  - `ticket_analysis.taxonomy`: `TAXONOMY_PATH`, `TAXONOMY`, `TAXONOMY_VERSION`, `TicketType`, `Intent`, `MAX_SECONDARY_TYPES`, `MAX_MENTIONS`, `Confidence`, `MentionKind`, `Mention`, `TicketClassification`

- [ ] **Step 1: Write the failing isolation test**

`tests/test_package_imports.py`:

```python
"""The package is what the API ships, so it must load without the tools or the LLM and Zendesk clients."""
import pkgutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BLOCKED = ("anthropic", "dotenv", "requests", "tools")
EXPECTED = {"paths", "jsonl", "tickets", "masking", "names", "taxonomy"}


def package_modules() -> list[str]:
    import ticket_analysis
    return sorted(m.name for m in pkgutil.iter_modules(ticket_analysis.__path__))


def test_package_has_the_foundation_modules():
    assert EXPECTED <= set(package_modules())


def test_package_imports_nothing_from_tools_or_the_api_clients():
    code = ("import importlib, sys\n"
            f"for name in {BLOCKED!r}:\n"
            "    sys.modules[name] = None  # any import of these raises ImportError\n"
            f"for name in {package_modules()!r}:\n"
            "    importlib.import_module('ticket_analysis.' + name)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_paths_point_at_the_repository():
    from ticket_analysis.paths import DATA_DIR, INSTANCES_PATH, REPO_ROOT as root
    assert root == REPO_ROOT.resolve()
    assert DATA_DIR == root / "data" and INSTANCES_PATH == root / "data" / "instances.csv"


def test_taxonomy_file_ships_with_the_package():
    from ticket_analysis.taxonomy import TAXONOMY_PATH
    assert TAXONOMY_PATH.parent.name == "ticket_analysis" and TAXONOMY_PATH.exists()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_package_imports.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ticket_analysis'`.

- [ ] **Step 3: Create the foundation modules**

`ticket_analysis/__init__.py`:

```python
"""Ticket analysis: server and instance detection and taxonomy classification for Fortified Data
Zendesk tickets. Holds only what loading a model and analyzing a ticket needs; training, evaluation,
labeling and the LLM workflow are in tools/."""
```

`ticket_analysis/paths.py`:

```python
"""Repository locations. The package sits one level below the repository root."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
INSTANCES_PATH = DATA_DIR / "instances.csv"
```

`ticket_analysis/jsonl.py`: a module docstring ("JSONL storage and timestamps."), `from __future__ import annotations`, `import json`, `from datetime import datetime, timezone`, `from pathlib import Path`, then `now_iso`, `read_jsonl`, `write_jsonl` and `append_jsonl` cut verbatim from common.py lines 65-98.

`ticket_analysis/tickets.py`: docstring ("The normalized ticket record shared by every loader."), `from __future__ import annotations`, `from alert_parser import parse_alert` (alert_parser is still at the root until Task 3, which makes this import relative; the isolation test passes meanwhile because alert_parser imports nothing blocked). Then cut verbatim from common.py lines 101-155: `_flag`, `_server_list`, `STAFF_FIELDS`, the DW_FIELDS comment and constant, `_dw_fields`, `_staff`, `make_ticket`.

`ticket_analysis/masking.py`: docstring ("Secret masking applied before any text leaves the machine, and the delimiter-tag guard for prompts."), `import re`, then common.py lines 170-207 verbatim (`REDACTED` through `prepare_ticket_text`).

`ticket_analysis/names.py`: docstring ("Server name normalization and the CMDB instance export."), `from __future__ import annotations`, `import csv`, `import re`, `from dataclasses import dataclass`, `from pathlib import Path`, `from .paths import INSTANCES_PATH`, then common.py lines 254-320 (`_IPV4` through `load_cmdb`) and 327-346 (`match_mention`) verbatim.

`ticket_analysis/taxonomy.py`: `git mv taxonomy.json ticket_analysis/taxonomy.json`, then `git mv schema.py ticket_analysis/taxonomy.py` and edit the moved file: docstring first line becomes "Taxonomy and output schema for one classified ticket, generated from taxonomy.json."; delete `import anthropic`; delete the last three lines (the OUTPUT_SCHEMA comment and assignment). `TAXONOMY_PATH = Path(__file__).parent / "taxonomy.json"` stays as is.

- [ ] **Step 4: Recreate schema.py and trim common.py**

New root `schema.py` (temporary until Task 7 moves it to tools/schema.py):

```python
"""Structured-output schema for the LLM classifier, built from the package's taxonomy models."""

import anthropic

from ticket_analysis.taxonomy import (MAX_MENTIONS, MAX_SECONDARY_TYPES, TAXONOMY, TAXONOMY_PATH,  # noqa: F401
                                      TAXONOMY_VERSION, Confidence, Intent, Mention, MentionKind,
                                      TicketClassification, TicketType)

# JSON schema in the form the structured outputs API accepts (unsupported
# constraints such as maxItems are moved into descriptions by the SDK).
OUTPUT_SCHEMA = anthropic.transform_schema(TicketClassification)
```

In common.py: delete the code moved in Step 3 (lines 65-155 except `merge_tickets` and `load_env`; lines 170-207; lines 254-320 and 327-346), keep `merge_tickets`, `client_server_hints`, `normalize_ws`, `split_by_evidence` and everything from `Prompt` and `FatalAPIError` on. Replace `ROOT = Path(__file__).parent` and `DATA_DIR = ROOT / "data"` with imports, keep the other path constants, and add the re-exports below the existing imports:

```python
from ticket_analysis.jsonl import append_jsonl, now_iso, read_jsonl, write_jsonl  # noqa: F401
from ticket_analysis.masking import (_PROMPT_TAGS, _SECRET_PATTERNS, PROMPT_TAG_REPLACEMENT, REDACTED,  # noqa: F401
                                     mask_secrets, prepare_ticket_text)
from ticket_analysis.names import Cmdb, Instance, ParsedName, load_cmdb, match_mention, parse_server_name  # noqa: F401
from ticket_analysis.paths import DATA_DIR, INSTANCES_PATH, REPO_ROOT as ROOT  # noqa: F401
from ticket_analysis.tickets import DW_FIELDS, STAFF_FIELDS, _flag, make_ticket  # noqa: F401
```

and remove the now-unused `from alert_parser import parse_alert` and `INSTANCES_PATH = ...` lines. Keep `load_env()` reading `ROOT / ".env"`.

- [ ] **Step 5: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q -W error::SyntaxWarning`
Expected: 276 passed (272 plus the four new tests). If `test_taxonomy_file_ships_with_the_package` fails, check that tests/test_schema.py still imports `TAXONOMY_PATH` from schema (re-exported) and passes.

- [ ] **Step 6: Commit (after the user approves committing)**

```bash
git add ticket_analysis tests/test_package_imports.py common.py schema.py
git commit -m "Extract the package foundation from common.py and schema.py"
```

---

### Task 3: Move alert_parser, type_rules and text_clean into the package

**Files:**
- Move: `alert_parser.py` to `ticket_analysis/alert_parser.py`, `type_rules.py` to `ticket_analysis/type_rules.py`, `text_clean.py` to `ticket_analysis/text_clean.py` (git mv)
- Create: `tools/__init__.py`, `tools/alert_parser.py` (re-parse and coverage commands), `tools/text_clean.py` (measuring command)
- Modify: `ticket_analysis/tickets.py` (relative import), importers listed in Step 4, `tests/test_package_imports.py`
- Test: `tests/test_alert_layouts.py`, `tests/test_alert_parser.py`, `tests/test_text_clean.py`, `tests/test_type_rules.py`

**Interfaces:**
- Consumes: `ticket_analysis.jsonl.read_jsonl`, `write_jsonl` (Task 2)
- Produces: `ticket_analysis.alert_parser` (everything up to and including `parse_alert`), `ticket_analysis.type_rules` (unchanged), `ticket_analysis.text_clean` (`PATTERNS`, `protected`, `clean_text`, `removed_chars`), `tools.alert_parser` (`refresh_file`, `coverage`, `render_coverage`, `main`), `tools.text_clean` (`TICKET_FILES`, `measure`, `main`)

- [ ] **Step 1: Extend the isolation test to the moved modules**

In tests/test_package_imports.py change `EXPECTED` to:

```python
EXPECTED = {"paths", "jsonl", "tickets", "masking", "names", "taxonomy", "alert_parser", "type_rules", "text_clean"}
```

and add a test that runs the lazy code paths with the blocked modules:

```python
def test_parsing_and_cleaning_run_without_the_tools():
    code = ("import sys\n"
            f"for name in {BLOCKED!r}:\n"
            "    sys.modules[name] = None\n"
            "from ticket_analysis.tickets import make_ticket\n"
            "from ticket_analysis.text_clean import clean_text\n"
            "t = make_ticket('1', '[FIRING:1] Made Up Alert', 'Hello,\\nserver FAKESQL01 is down.\\nThanks')\n"
            "print(clean_text(t['subject'], t['description'])[1])\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_package_imports.py -q`
Expected: FAIL; `test_package_has_the_foundation_modules` reports the missing modules.

- [ ] **Step 3: Move the files and split their command code**

```bash
git mv alert_parser.py ticket_analysis/alert_parser.py
git mv type_rules.py ticket_analysis/type_rules.py
git mv text_clean.py ticket_analysis/text_clean.py
```

`tools/__init__.py`:

```python
"""Local tools: data loading, the LLM run workflow, training, evaluation, labeling and the review UIs.
Run each as python -m tools.<name> from the repository root."""
```

ticket_analysis/alert_parser.py: cut the `# --- Ticket files ---` section through the end of the file (from line 578: `refresh_file`, `coverage`, `render_coverage` and the `__main__` block) and paste it into `tools/alert_parser.py`. Remove the command lines from the module docstring (lines 20-21) and paste them into the tools docstring with `python -m tools.alert_parser`.

tools/alert_parser.py gets this header, and the pasted functions lose their function-level imports in favour of it (`coverage` used `from server_detector import ticket_names`; it now imports from tools.server_detector's core counterpart once Task 5 lands, so for this task keep `from server_detector import ticket_names` inside `coverage` and change it in Task 5):

```python
"""Commands for alert_parser:

    python -m tools.alert_parser TICKETS.jsonl [...]            re-parse alert_fields in place
    python -m tools.alert_parser coverage TICKETS.jsonl [...]   per-layout coverage (counts only)
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

from common import md_table
from ticket_analysis.alert_parser import LAYOUTS, parse_alert
from ticket_analysis.jsonl import read_jsonl, write_jsonl
```

and its `__main__` block becomes a `main()` with the usage text changed to `python -m tools.alert_parser ...`, followed by `if __name__ == "__main__": main()`.

ticket_analysis/text_clean.py: cut the `# --- Measurement ---` section through the end (`measure`, `main`, `__main__`) into `tools/text_clean.py`; also move `TICKET_FILES` there. Replace `from common import DATA_DIR, fmt_pct, read_jsonl` with nothing (the core part uses none of them; check with grep). tools/text_clean.py header:

```python
"""Measure what text_clean removes from the ticket files, per pattern, as counts only.

    python -m tools.text_clean [TICKETS.jsonl ...]
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

from common import fmt_pct
from ticket_analysis.jsonl import read_jsonl
from ticket_analysis.paths import DATA_DIR
from ticket_analysis.text_clean import PATTERNS, removed_chars

TICKET_FILES = [DATA_DIR / "train_tickets.jsonl", DATA_DIR / "test_tickets.jsonl"]
```

In ticket_analysis/tickets.py change `from alert_parser import parse_alert` to `from .alert_parser import parse_alert`.

- [ ] **Step 4: Update the importers**

| File | Old | New |
| --- | --- | --- |
| alert_layouts.py | `from alert_parser import COLLECTOR_LABELS, FIRING_LABELS, parse_alert` | `from ticket_analysis.alert_parser import COLLECTOR_LABELS, FIRING_LABELS, parse_alert` |
| baseline.py | `from text_clean import clean_text`, `from type_rules import RULES, apply_rules` | `from ticket_analysis.text_clean import clean_text`, `from ticket_analysis.type_rules import RULES, apply_rules` |
| claude_pack.py | `from text_clean import clean_text` | `from ticket_analysis.text_clean import clean_text` |
| detector_ui.py | `from alert_parser import alert_spans, parse_alert` | `from ticket_analysis.alert_parser import alert_spans, parse_alert` |
| server_detector.py | `from alert_parser import COLLECTOR_LABELS, FIRING_LABELS, SERVER_FIELDS` | `from ticket_analysis.alert_parser import COLLECTOR_LABELS, FIRING_LABELS, SERVER_FIELDS` |
| ticket_types.py | `from alert_parser import parse_alert`, `from text_clean import clean_text`, `from type_rules import apply_rules` | the `ticket_analysis.` forms |
| tests/test_alert_layouts.py | `from alert_parser import LAYOUTS, SERVER_FIELDS, alert_spans, coverage, match_layout, parse_alert, span_value` | `from ticket_analysis.alert_parser import LAYOUTS, SERVER_FIELDS, alert_spans, match_layout, parse_alert, span_value` and `from tools.alert_parser import coverage` |
| tests/test_alert_parser.py | `from alert_parser import alert_family, parse_alert` | `from ticket_analysis.alert_parser import alert_family, parse_alert` |
| tests/test_text_clean.py | `from text_clean import PATTERNS, clean_text, protected, removed_chars` | `from ticket_analysis.text_clean import ...` (same names) |
| tests/test_type_rules.py | `import type_rules` or `from type_rules import ...` | the `ticket_analysis.type_rules` form |

Then run `grep -rnE "^(from|import) (alert_parser|type_rules|text_clean)\b|from (alert_parser|text_clean|type_rules) import" --include=*.py .` and fix anything left (function-level imports included).

- [ ] **Step 5: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q -W error::SyntaxWarning`
Expected: 277 passed.

Run: `.venv/Scripts/python.exe -m tools.alert_parser coverage data/test_tickets.jsonl` and `.venv/Scripts/python.exe -m tools.text_clean data/test_tickets.jsonl`
Expected: a coverage table and per-pattern counts, as before.

- [ ] **Step 6: Commit (after the user approves committing)**

```bash
git add -A ticket_analysis tools tests alert_layouts.py baseline.py claude_pack.py detector_ui.py server_detector.py ticket_types.py
git commit -m "Move the alert parser, type rules and text cleaning into the package"
```

---

### Task 4: Split ticket_types

**Files:**
- Move: `ticket_types.py` to `ticket_analysis/ticket_types.py` (git mv)
- Create: `tools/ticket_types.py`
- Modify: `baseline.py` (import `features` from the package), `claude_pack.py`, `detector_ui.py`, `tests/test_ticket_types.py`, `tests/test_detector_ui.py`, `tests/test_package_imports.py`

**Interfaces:**
- Consumes: `ticket_analysis.alert_parser.parse_alert`, `text_clean.clean_text`, `type_rules.apply_rules`, `taxonomy.TAXONOMY_VERSION`, `paths.DATA_DIR`
- Produces:
  - `ticket_analysis.ticket_types`: `RETURNED_DIR`, `NOTES_PATH`, `MODEL_PATH`, `ALERT_FAMILIES`, `MAX_CHARS`, `TOP_K`, `MODEL_CLEAN`, `UNCERTAIN_P`, `UNCERTAIN_GAP`, `RESOLVED_RE`, `features(tickets, max_chars, clean=False) -> np.ndarray`, `load_alert_map(path=NOTES_PATH) -> dict`, `load_model(path=MODEL_PATH) -> dict`, `uncertainty(candidates, has_subject=True) -> tuple[bool, str]`, `classify(subject, text, bundle, alert_map) -> dict`
  - `tools.ticket_types`: `LABELS_PATH`, `CLAUDE_TYPES_PATH`, `LABEL_PATHS`, `PROMPT_PATH`, `TICKET_FILES`, `TARGETS`, `train(tickets, labels, seed=0) -> dict`, `current_labels(paths=LABEL_PATHS) -> dict`, `load_or_train(...) -> dict | None`, `prompt_rules(path=PROMPT_PATH) -> list[str]`, `summary(bundle, alert_map) -> dict`, `main(argv=None)`

- [ ] **Step 1: Extend the isolation test**

Add `"ticket_types"` to `EXPECTED` and this test:

```python
def test_type_classification_runs_without_the_tools():
    code = ("import sys\n"
            f"for name in {BLOCKED!r}:\n"
            "    sys.modules[name] = None\n"
            "from ticket_analysis.ticket_types import features\n"
            "print(features([{'subject': 'Made up', 'description': 'text'}], 100).shape)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "(1, 2)"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_package_imports.py -q`
Expected: FAIL (ticket_types missing from the package).

- [ ] **Step 3: Move and split**

`git mv ticket_types.py ticket_analysis/ticket_types.py`. In the package file:

- Imports become `import joblib`, `import numpy as np`, `from .alert_parser import parse_alert`, `from .paths import DATA_DIR`, `from .taxonomy import TAXONOMY_VERSION`, `from .text_clean import clean_text`, `from .type_rules import apply_rules`. Drop argparse, Counter and the baseline, common and schema imports.
- Paste `features()` verbatim from baseline.py lines 104-115 under the `# --- Model ---` heading.
- Keep `RETURNED_DIR`, `NOTES_PATH`, `MODEL_PATH`, `ALERT_FAMILIES`, `MAX_CHARS`, `TOP_K`, the MODEL_CLEAN comment and constant, the UNCERTAIN comment and constants, `RESOLVED_RE`, `load_alert_map`, `_top`, `uncertainty`, `_rule_or_model`, `classify`.
- Add after `features`:

```python
def load_model(path: Path = MODEL_PATH) -> dict:
    """The saved type and intent model bundle (see tools.ticket_types.train)."""
    return joblib.load(path)
```

- Cut into tools/ticket_types.py: the label path constants (`LABELS_PATH`, the CLAUDE_TYPES comment and `CLAUDE_TYPES_PATH`, `LABEL_PATHS`), `PROMPT_PATH`, `TICKET_FILES`, `TARGETS`, `train`, `current_labels`, `load_or_train`, the `# --- Taxonomy summary for the page ---` section (`prompt_rules`, `summary`), and `main`.
- Rewrite the package docstring to describe classification only, and move the "Commands: train ..." paragraph to the tools docstring as `python -m tools.ticket_types train`.

tools/ticket_types.py header:

```python
"""Train the taxonomy classifier and build the detector UI's taxonomy summary.

    python -m tools.ticket_types train [--labels FILE ...] [--model-path PATH]

Fits on Claude's taxonomy sample labels plus later Claude Desktop rounds (LABEL_PATHS) and saves
data/models/ticket_types.joblib. Prints counts only, never ticket text. Nothing leaves this machine.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import joblib

from baseline import build_model, read_labels
from common import read_jsonl
from ticket_analysis.jsonl import now_iso
from ticket_analysis.paths import DATA_DIR, REPO_ROOT
from ticket_analysis.taxonomy import TAXONOMY, TAXONOMY_VERSION
from ticket_analysis.ticket_types import MAX_CHARS, MODEL_CLEAN, MODEL_PATH, RETURNED_DIR, features, load_alert_map

LABELS_PATH = RETURNED_DIR / "taxonomy_sample_labels.jsonl"
```

followed by the cut constants with `PROMPT_PATH = REPO_ROOT / "prompts" / "classify_v2.md"`, then the cut functions. In `load_or_train`, `bundle = joblib.load(model_path)` stays as is.

In baseline.py, delete `features()` and add `from ticket_analysis.ticket_types import features` (baseline keeps `build_model` and `read_labels`).

- [ ] **Step 4: Update the importers**

| File | Change |
| --- | --- |
| claude_pack.py | `from baseline import features, read_labels` becomes `from baseline import read_labels` plus `from ticket_analysis.ticket_types import features`; `from ticket_types import CLAUDE_TYPES_PATH, LABEL_PATHS, load_or_train, prompt_rules` becomes `from tools.ticket_types import ...` (same names) |
| detector_ui.py | `import ticket_types as tt` becomes `import ticket_analysis.ticket_types as tt` plus `import tools.ticket_types as ttt`; `tt.LABELS_PATH`, `tt.load_or_train`, `tt.summary` become `ttt.`; `tt.MODEL_PATH`, `tt.TAXONOMY_VERSION`, `tt.classify` and `tt.load_alert_map` stay (the package module imports TAXONOMY_VERSION, so it resolves) |
| tests/test_ticket_types.py | `import ticket_types as tt` becomes `import ticket_analysis.ticket_types as tt` and `import tools.ticket_types as ttt`; `tt.train`, `tt.current_labels`, `tt.load_or_train`, `tt.summary`, `tt.TAXONOMY` become `ttt.` (keep `tt.classify`, `tt.load_alert_map`, `tt.uncertainty`, `tt.TAXONOMY_VERSION`) |
| tests/test_detector_ui.py | `tt.train` becomes `ttt.train`, with `import tools.ticket_types as ttt` |

`tt.TAXONOMY` in tests: add `TAXONOMY` to tools.ticket_types's imports (already in the header above), so `ttt.TAXONOMY` works.

Run `grep -rnE "import ticket_types|from ticket_types|baseline import .*features" --include=*.py .` and fix anything left.

- [ ] **Step 5: Run the full suite and the training command**

Run: `.venv/Scripts/python.exe -m pytest -q -W error::SyntaxWarning`
Expected: 278 passed.

Run: `.venv/Scripts/python.exe -m tools.ticket_types train --model-path data/verify/package-split/ticket_types-check.joblib`
Expected: "Ticket types: trained on N labeled tickets ..." and an alert-table count. This writes a scratch model only; data/models/ticket_types.joblib is not touched.

- [ ] **Step 6: Commit (after the user approves committing)**

```bash
git add -A ticket_analysis tools tests baseline.py claude_pack.py detector_ui.py
git commit -m "Split the taxonomy classifier into the package and its training tool"
```

---

### Task 5: Split server_detector and convert the saved model

**Files:**
- Move: `server_detector.py` to `ticket_analysis/server_detector.py` (git mv)
- Create: `tools/server_detector.py`, `tools/convert_model.py`
- Modify: `alert_layouts.py`, `claude_pack.py`, `detector_ui.py`, `server_labels.py`, `tools/alert_parser.py`, `tests/test_server_detector.py`, `tests/test_detector_ui.py`, `tests/test_server_labels.py`, `tests/test_alert_layouts.py`, `tests/test_package_imports.py`
- Test: `tests/test_convert_model.py`

**Interfaces:**
- Consumes: `ticket_analysis.names.ParsedName`, `parse_server_name`; `ticket_analysis.paths.DATA_DIR`, `INSTANCES_PATH`; `ticket_analysis.alert_parser.COLLECTOR_LABELS`, `FIRING_LABELS`, `SERVER_FIELDS`
- Produces:
  - `ticket_analysis.server_detector`: everything in today's file except the lines listed below for tools, plus `load_model(path: Path = MODEL_PATH) -> dict`
  - `tools.server_detector`: `HOLDOUT_LABELS_PATH`, `CLASSES`, `DEFAULT_NEG_RATIO`, `DEFAULT_CLIENT_DROPOUT`, `ALERT_CONTEXT_MODES`, `MODEL_KINDS`, `gold_names`, `label_of`, `synthetic_instance`, `make_examples`, `split_validation`, `holdout_names`, `score_tickets`, `_occurrences`, `average_precision`, `best_f1_threshold`, `fit_config`, `select_config`, `SEGMENTS`, `REPORT_THRESHOLDS`, `_seg`, `prf`, `recall_of`, `false_positive_shapes`, `held_out_recall`, `pairing_accuracy`, `evaluate`, `_frac`, `_num`, `render_report`, `print_summary`, `parse_args`, `without_labeled`, `cmd_train`, `cmd_evaluate`, `cmd_detect`, `main`
  - `tools.convert_model`: `OLD_MODULE = "server_detector"`, `BACKUP_SUFFIX = ".before-package"`, `convert(path: Path) -> Path` (returns the backup path), `main(argv=None)`

The boundary was computed from the code (a name-reference closure from `detect_servers`, `ServerModel`, the directories, `ticket_candidates`, `add_features` and the other names the UI and tests use): nothing on the inference path references the tools-side names above.

- [ ] **Step 1: Write the failing convert test**

`tests/test_convert_model.py`:

```python
import subprocess
import sys
from pathlib import Path

import joblib

import ticket_analysis.server_detector as sd
from tools.convert_model import BACKUP_SUFFIX, OLD_MODULE, convert

REPO_ROOT = Path(__file__).parent.parent
LOAD = ("import joblib, sys; b = joblib.load(sys.argv[1]); m = b['model']; "
        "print(type(m).__module__, type(m).__name__, m.host_threshold)")


def old_style_model(path: Path, monkeypatch) -> None:
    """A model file saved before the package existed: its classes are stored as server_detector.*"""
    with monkeypatch.context() as m:
        m.setattr(sd.ServerModel, "__module__", OLD_MODULE)
        m.setitem(sys.modules, OLD_MODULE, sd)
        joblib.dump({"model": sd.ServerModel(host_threshold=0.42), "trained_on": "made_up.jsonl"}, path)


def load_elsewhere(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", LOAD, str(path)], cwd=REPO_ROOT, capture_output=True, text=True)


def test_an_old_model_does_not_load_after_the_move(tmp_path, monkeypatch):
    path = tmp_path / "server_detector.joblib"
    old_style_model(path, monkeypatch)
    out = load_elsewhere(path)
    assert out.returncode != 0 and "server_detector" in out.stderr


def test_convert_rewrites_the_module_path_and_keeps_the_model(tmp_path, monkeypatch):
    path = tmp_path / "server_detector.joblib"
    old_style_model(path, monkeypatch)
    backup = convert(path)
    assert backup == tmp_path / f"server_detector{BACKUP_SUFFIX}.joblib" and backup.exists()
    out = load_elsewhere(path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["ticket_analysis.server_detector", "ServerModel", "0.42"]
    assert OLD_MODULE not in sys.modules or sys.modules[OLD_MODULE] is not sd


def test_convert_refuses_a_second_run(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / "server_detector.joblib"
    old_style_model(path, monkeypatch)
    convert(path)
    with pytest.raises(SystemExit, match="already converted"):
        convert(path)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_convert_model.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ticket_analysis.server_detector'`.

- [ ] **Step 3: Move and split server_detector**

`git mv server_detector.py ticket_analysis/server_detector.py`, then in the package file:

- Imports: drop `argparse`, `datetime`, `from sklearn.metrics import average_precision_score, precision_recall_curve` (check with grep that no core function uses them; move them to tools if so), and replace the two local import lines with:

```python
from .alert_parser import COLLECTOR_LABELS, FIRING_LABELS, SERVER_FIELDS
from .names import ParsedName, parse_server_name
from .paths import DATA_DIR, INSTANCES_PATH
```

- Cut to tools/server_detector.py, verbatim and in order: the constants `HOLDOUT_LABELS_PATH`, `CLASSES`, `DEFAULT_NEG_RATIO` (and its comment), `DEFAULT_CLIENT_DROPOUT` (and its comment), `ALERT_CONTEXT_MODES`, `MODEL_KINDS`; today's lines 912-984 (`gold_names` through `make_examples`); and today's line 1250 to the end (`split_validation` through `main` and the `__main__` block). Before cutting, check with `grep -n "time\." ticket_analysis/server_detector.py` whether `time` is still used in the core part; move the import if not.
- Add after `detect_servers`'s helpers:

```python
def load_model(path: Path = MODEL_PATH) -> dict:
    """The saved detector bundle: {model, directory, trained_on, created_at, config}."""
    return joblib.load(path)
```

- Move the "Commands: ..." paragraph of the module docstring to tools/server_detector.py, with `python -m tools.server_detector train|evaluate|detect`.

tools/server_detector.py header:

```python
"""Train, evaluate and run the server and instance detector (ticket_analysis.server_detector).

    python -m tools.server_detector train      fit and save data/models/server_detector.joblib
    python -m tools.server_detector evaluate   all tests, Markdown report under data/reports/
    python -m tools.server_detector detect     detections for a ticket file, written to a new .jsonl

Prints counts and rates only, never ticket text. Nothing leaves this machine; the CMDB API is a stub.
"""

from __future__ import annotations

import argparse
import random
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
from sklearn.metrics import average_precision_score, precision_recall_curve

from common import REPORTS_DIR, fmt_pct, md_table
from ticket_analysis.jsonl import now_iso, read_jsonl, write_jsonl
from ticket_analysis.paths import DATA_DIR, INSTANCES_PATH
from ticket_analysis.server_detector import *  # noqa: F401,F403 -- the harness uses most of the detector
from ticket_analysis.server_detector import (_after_label, _candidate, _evidence, _mark_host_nearby,  # noqa: F401
                                             _prep_dense, _rule)
```

The star import is a scaffold so the pasted code runs unchanged; a star import skips underscore names, hence the second line. Before committing, replace both lines with one explicit import of the names the file uses: list them with `grep -oE "\b[A-Za-z_][A-Za-z0-9_]*\b" tools/server_detector.py | sort -u` intersected with `dir(ticket_analysis.server_detector)`. Likewise drop any header import the cut code does not use (`random` and `defaultdict` are listed only in case it does).

In `cmd_detect`, replace `bundle = joblib.load(args.model_path)` with `bundle = load_model(args.model_path)`, and change the message `Run: python server_detector.py train` to `Run: python -m tools.server_detector train`.

- [ ] **Step 4: Write tools/convert_model.py**

```python
"""Rewrite saved server detector models for the ticket_analysis package.

Models saved before the package existed store their classes as server_detector.ServerModel and the
like, which no longer imports. This loads each file with that name mapped to
ticket_analysis.server_detector and saves it back, keeping the original as
<name>.before-package.joblib. The weights, thresholds and directory are unchanged.

    python -m tools.convert_model [MODEL.joblib ...]   default: data/models/server_detector.joblib
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import joblib

import ticket_analysis.server_detector as sd

OLD_MODULE = "server_detector"
BACKUP_SUFFIX = ".before-package"


def convert(path: Path) -> Path:
    """Convert one model file in place and return the backup's path."""
    backup = path.with_name(path.stem + BACKUP_SUFFIX + path.suffix)
    if backup.exists():
        raise SystemExit(f"{backup} exists, so {path} was already converted")
    previous = sys.modules.get(OLD_MODULE)
    sys.modules[OLD_MODULE] = sd
    try:
        bundle = joblib.load(path)
    finally:
        if previous is None:
            del sys.modules[OLD_MODULE]
        else:
            sys.modules[OLD_MODULE] = previous
    shutil.copy2(path, backup)
    joblib.dump(bundle, path)
    return backup


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("models", nargs="*", type=Path, default=[sd.MODEL_PATH])
    for path in p.parse_args(argv).models:
        print(f"{path}: converted; original kept as {convert(path)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Update the importers and tests**

| File | Change |
| --- | --- |
| alert_layouts.py | `from server_detector import BUILTIN_STOP, compress, is_alert_ticket, ticket_names` becomes `from ticket_analysis.server_detector import ...` |
| tools/alert_parser.py | inside `coverage`, `from server_detector import ticket_names` becomes a top-level `from ticket_analysis.server_detector import ticket_names` |
| claude_pack.py | `import server_detector as sd` becomes `import ticket_analysis.server_detector as sd` |
| server_labels.py | the same; `joblib.load(args.model_path)` becomes `sd.load_model(args.model_path)`; message `python server_detector.py train` becomes `python -m tools.server_detector train` |
| detector_ui.py | the same import; `joblib.load(model_path)` at line 484 becomes `sd.load_model(model_path)`; the two `python server_detector.py train` strings become `python -m tools.server_detector train` |
| tests/test_alert_layouts.py | `from server_detector import ticket_names` becomes `from ticket_analysis.server_detector import ticket_names` |
| tests/test_server_labels.py, tests/test_detector_ui.py | `import server_detector as sd` becomes `import ticket_analysis.server_detector as sd` |
| tests/test_server_detector.py | `import server_detector as sd` becomes `import ticket_analysis.server_detector as sd` plus `import tools.server_detector as sdt`; the training-side calls become `sdt.`: `sdt.make_examples`, `sdt.fit_config`, `sdt.holdout_names`, `sdt.best_f1_threshold`, `sdt.without_labeled` (find them all with `grep -noE "sd\.[a-z_]+" tests/test_server_detector.py` and compare against the tools list in Interfaces) |

In tests/test_server_detector.py `test_model_saved_by_the_script_loads_in_another_program`, change the command to `[sys.executable, "-m", "tools.server_detector", "train", ...]` with `cwd=Path(__file__).parent.parent`, and the expected output to `["ticket_analysis.server_detector", "ServerModel"]`. Update its docstring to "Running the training command must save the package's classes, not __main__.ServerModel."

Add `"server_detector"` to `EXPECTED` in tests/test_package_imports.py.

Run `grep -rnE "import server_detector|from server_detector" --include=*.py .` and fix anything left.

- [ ] **Step 6: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest -q -W error::SyntaxWarning`
Expected: 281 passed (278 plus the three convert tests).

- [ ] **Step 7: Convert the saved model**

Run: `.venv/Scripts/python.exe -m tools.convert_model`
Expected: `...server_detector.joblib: converted; original kept as ...server_detector.before-package.joblib`.

Then: `.venv/Scripts/python.exe -c "import ticket_analysis.server_detector as sd; b = sd.load_model(); print(type(b['model']).__module__, b['model'].host_threshold, b['model'].instance_threshold)"`
Expected: `ticket_analysis.server_detector 0.29 0.9` (the thresholds recorded in HANDOFF-2).

- [ ] **Step 8: Commit (after the user approves committing)**

```bash
git add -A ticket_analysis tools tests alert_layouts.py claude_pack.py detector_ui.py server_labels.py
git commit -m "Split the server detector into the package and its training tool"
```

---

### Task 6: Dependencies

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-tools.txt`

- [ ] **Step 1: Split the requirements**

`requirements.txt`:

```
# What the ticket_analysis package needs (the API image installs only this file).
pydantic==2.14.1
scikit-learn==1.9.1
```

`requirements-tools.txt`:

```
# The tools and tests: python -m tools.<name> and pytest.
-r requirements.txt
anthropic==1.15.0
python-dotenv==1.2.4
requests==2.34.2
pytest==9.1.1
```

- [ ] **Step 2: Check the package's requirements are complete**

Run in a throwaway venv in the scratchpad:

```bash
V="$SCRATCH/venv-core"
/c/Python314/python.exe -m venv "$V"
"$V/Scripts/python.exe" -m pip install -q -r requirements.txt
cd /c/src/ticket-classifier-poc && "$V/Scripts/python.exe" -c "import ticket_analysis.server_detector, ticket_analysis.ticket_types, ticket_analysis.tickets, ticket_analysis.masking; print('ok')"
```

Expected: `ok`. If an import fails for a package not listed (for example numpy or scipy imported directly), it arrives with scikit-learn; anything else gets added to requirements.txt with the version from `.venv/Scripts/python.exe -m pip show <name>`.

- [ ] **Step 3: Commit (after the user approves committing)**

```bash
git add requirements.txt requirements-tools.txt
git commit -m "Split the requirements into the package and the tools"
```

---

### Task 7: Move the remaining scripts into tools/

**Files:**
- Move (git mv to tools/): `common.py`, `schema.py`, `fetch_tickets.py`, `load_export.py`, `classify.py`, `report.py`, `compare.py`, `view_tickets.py`, `viewer_template.html`, `split_alerts.py`, `baseline.py`, `claude_pack.py`, `server_labels.py`, `alert_layouts.py`, `detector_ui.py`, `detector_ui.html`
- Modify: every moved file's imports; `tools/common.py` (drop the re-exports); all tests that import them

**Interfaces:**
- Produces: `tools.common` without the Task 2 re-exports. Callers import foundation names from `ticket_analysis.*` directly.

- [ ] **Step 1: Move the files**

```bash
cd /c/src/ticket-classifier-poc
for f in common schema fetch_tickets load_export classify report compare view_tickets split_alerts baseline claude_pack server_labels alert_layouts detector_ui; do git mv $f.py tools/$f.py; done
git mv viewer_template.html tools/viewer_template.html
git mv detector_ui.html tools/detector_ui.html
ls *.py   # expected: no .py files left at the root
```

- [ ] **Step 2: Rewrite the imports**

Rules, applied file by file in tools/ and tests/:

- `from common import X, Y` splits by where each name now lives: `read_jsonl`, `write_jsonl`, `append_jsonl`, `now_iso` from `ticket_analysis.jsonl`; `make_ticket`, `_flag`, `DW_FIELDS`, `STAFF_FIELDS` from `ticket_analysis.tickets`; `mask_secrets`, `prepare_ticket_text`, `_SECRET_PATTERNS`, `_PROMPT_TAGS`, `PROMPT_TAG_REPLACEMENT` from `ticket_analysis.masking`; `ParsedName`, `parse_server_name`, `Instance`, `Cmdb`, `load_cmdb`, `match_mention` from `ticket_analysis.names`; `DATA_DIR`, `INSTANCES_PATH` from `ticket_analysis.paths`; everything else from `tools.common`.
- `from schema import OUTPUT_SCHEMA` becomes `from tools.schema import OUTPUT_SCHEMA`; every other schema name comes from `ticket_analysis.taxonomy`.
- `from baseline import`, `from load_export import`, `from fetch_tickets import`, `import server_labels as sl` become the `tools.` forms.
- In tools/common.py, delete the Task 2 re-export block and import only what common.py itself uses (`now_iso`, `read_jsonl`, `write_jsonl`, `append_jsonl`, `prepare_ticket_text` if used, `parse_server_name`, `Instance`, `Cmdb`, `TAXONOMY`, `TAXONOMY_VERSION`, `TicketClassification` and so on; let the test suite and `python -c "import tools.common"` tell you). `ROOT` becomes `from ticket_analysis.paths import DATA_DIR, INSTANCES_PATH, REPO_ROOT` with `PROMPTS_DIR = REPO_ROOT / "prompts"` and `load_env()` reading `REPO_ROOT / ".env"`.
- In tools/claude_pack.py line 358, `(Path(__file__).parent / "taxonomy.json")` becomes `TAXONOMY_PATH` from `ticket_analysis.taxonomy`.
- Earlier tasks' temporary `from common import` lines in tools/alert_parser.py, tools/text_clean.py, tools/ticket_types.py and tools/server_detector.py become `from tools.common import` (or the package module, by the rule above).
- tests/test_schema.py: `from schema import OUTPUT_SCHEMA, TAXONOMY, TAXONOMY_PATH, Intent, TicketClassification, TicketType` becomes `from tools.schema import OUTPUT_SCHEMA` plus `from ticket_analysis.taxonomy import TAXONOMY, TAXONOMY_PATH, Intent, TicketClassification, TicketType`. tests/test_quotes.py: `Mention` from `ticket_analysis.taxonomy`.

Then: `grep -rnE "^\s*(from|import) (common|schema|baseline|load_export|fetch_tickets|server_labels|view_tickets|split_alerts|report|compare|classify|claude_pack|alert_layouts|detector_ui)\b" --include=*.py .`
Expected: no output.

- [ ] **Step 3: Update command strings in code**

Run `grep -rnE "python [a-z_]+\.py" --include=*.py --include=*.html tools ticket_analysis` and change each to `python -m tools.<name>` (`python baseline.py --labels` in tools/detector_ui.html becomes `python -m tools.baseline --labels`). Strings only; no logic.

- [ ] **Step 4: Run the suite**

Run: `.venv/Scripts/python.exe -m pytest -q -W error::SyntaxWarning`
Expected: 281 passed. tests/test_viewer.py's Node check runs if Node is installed and must pass, since `view_tickets.py` still embeds the patterns from `ticket_analysis.masking`.

- [ ] **Step 5: Smoke-run each command's help**

```bash
for m in fetch_tickets load_export classify report compare view_tickets split_alerts baseline claude_pack server_labels alert_layouts detector_ui server_detector ticket_types text_clean convert_model; do
  .venv/Scripts/python.exe -m tools.$m --help > /dev/null 2>&1 && echo "ok $m" || echo "FAIL $m"; done
.venv/Scripts/python.exe -m tools.alert_parser 2>&1 | head -1   # prints its usage line
.venv/Scripts/python.exe -m tools.classify --limit 1 --dry-run --tickets samples/sample_tickets.jsonl
```

Expected: `ok` for every module; the usage line; the dry run prints the system prompt and writes data/dry-run/<id>.txt (no API call).

- [ ] **Step 6: Commit (after the user approves committing)**

```bash
git add -A tools tests ticket_analysis
git commit -m "Move the remaining scripts into tools and drop the temporary re-exports"
```

---

### Task 8: Verify that nothing changed

**Files:**
- Output: `data/verify/package-split/after/`

- [ ] **Step 1: Record the outputs from the new layout**

```bash
cd /c/src/ticket-classifier-poc
A=data/verify/package-split/after; mkdir -p $A
.venv/Scripts/python.exe -m tools.server_detector detect --tickets data/test_tickets.jsonl --output $A/detect.jsonl
.venv/Scripts/python.exe "$SCRATCH/split_check.py" types $A/types.jsonl
.venv/Scripts/python.exe -m tools.baseline --labels data/claude_pack/returned/taxonomy_sample_labels.jsonl > $A/baseline.txt
```

- [ ] **Step 2: Compare**

Run: `.venv/Scripts/python.exe "$SCRATCH/split_check.py" compare data/verify/package-split/before data/verify/package-split/after`
Expected: `detect.jsonl: identical`, `types.jsonl: identical`, `baseline.txt: identical`, exit code 0.

If detect.jsonl differs, the model conversion or a moved constant is wrong: diff the two files with a script that prints only ticket ids and field names, never text.

- [ ] **Step 3: Start the UI and analyze made-up text**

Run in the background: `.venv/Scripts/python.exe -m tools.detector_ui --port 8766`
Then:

```bash
curl -s -X POST http://127.0.0.1:8766/api/analyze -H "Content-Type: application/json" \
  -d '{"subject": "Made up", "text": "Please restart FAKESQL01\\REPORTS today."}' | head -c 300
```

Expected: JSON with a detection for FAKESQL01. Check the route name in tools/detector_ui.py first (`grep -n "def do_POST\|/api/" tools/detector_ui.py`) and use the one it serves. Stop the server afterwards.

- [ ] **Step 4: Full suite one last time**

Run: `.venv/Scripts/python.exe -m pytest -q -W error::SyntaxWarning`
Expected: 281 passed.

---

### Task 9: Documentation

**Files:**
- Modify: `README.md`, `PLAN.md`, `docs/prompt-1-name-swap-augmentation.md`, `docs/prompt-2-transformer-tagger.md`, `docs/prompt-3-model-comparison.md`, `docs/prompt-6-ticket-api.md`, `docs/alert-parser.md`, `docs/claude-taxonomy-prompt.md`, `docs/recommendations-review.md`, `docs/dw-export-eval.sql`, `docs/dw-export-test.sql`, `docs/dw-export-train.sql`
- Leave as written: `BUILD_PROMPT.md`, `HANDOFF-2.md`, `docs/server-detector-prompt.md`, `docs/prompt-4-alert-parsers.md`, `docs/prompt-5-taxonomy-classifier-fixes.md`, `docs/superpowers/`

- [ ] **Step 1: README framing and layout**

Replace the title and first paragraph with:

```markdown
# Ticket Analysis

Classifies Fortified Data Zendesk tickets (type, intent) and finds the SQL Server hosts and
instances they mention, matched against the known names of each client. It started as a proof of
concept and is being developed into a ticket analysis API hosted in Azure
(docs/prompt-6-ticket-api.md). Today it runs locally: the server detector and the taxonomy
classifier work without an LLM, and an LLM classifier path measures what a single Claude call
can do.

## Layout

| Folder | Holds |
| --- | --- |
| ticket_analysis/ | What the API ships: alert parsing, masking, text cleaning, server detection, type classification, the taxonomy. It never imports from tools/. |
| tools/ | Data loading, the LLM run workflow, training, evaluation, labeling and the review pages. Run each as `python -m tools.<name>` from the repository root. |
| prompts/ | LLM prompt versions. |
| docs/ | Design notes, build prompts and export queries. |
| tests/ | pytest; no network access. |
```

Change "What it does" to describe both paths (local models and the LLM run), not only the LLM run.

- [ ] **Step 2: README commands and setup**

- Setup: `pip install -r requirements-tools.txt` (with a line saying requirements.txt alone is what the package needs).
- Every `python X.py` becomes `python -m tools.X`, including `python alert_parser.py` (now `python -m tools.alert_parser`) and `python text_clean.py` (`python -m tools.text_clean`). Keep Windows paths with backslashes in the PowerShell examples.
- Add `python -m tools.convert_model` to the server detector section: one sentence on when it is needed (a model saved before the package split).
- File references: `server_detector.py` becomes `ticket_analysis/server_detector.py` (or `tools/server_detector.py` where the text is about training or evaluation), and likewise for ticket_types.py, text_clean.py, type_rules.py, alert_parser.py, common.py (masking now in ticket_analysis/masking.py), schema.py (ticket_analysis/taxonomy.py and tools/schema.py), view_tickets.py and detector_ui.py.
- "Files that carry forward to Keystone": taxonomy.json is now ticket_analysis/taxonomy.json; schema.py becomes ticket_analysis/taxonomy.py (models) and tools/schema.py (the API form).
- Data handling: replace "Delete data/ when the PoC ends unless the labels move into Keystone." with "Keep data/ only on approved machines; the labels and models in it contain client data and move only into approved storage."

Check: `grep -nE "python [a-z_]+\.py|throwaway|proof of concept|PoC" README.md`
Expected: no `python X.py` forms; "proof of concept" appears only in the history sentence of the first paragraph.

- [ ] **Step 3: PLAN.md note**

Insert below the title line:

```markdown
> **Update 2026-10-19:** this project is no longer throwaway. It is being developed into the ticket
> analysis API (docs/prompt-6-ticket-api.md); the code is split into the ticket_analysis package and
> tools/. README.md describes the current layout and commands. The plan below is kept as written.
```

- [ ] **Step 4: Docs for work not yet done**

In each file in the Modify list after PLAN.md: rewrite `python X.py` commands to `python -m tools.X`, and file references to their new paths by the rule in Step 2. In docs/prompt-6-ticket-api.md, also update the "existing code" list (around lines 34-38): `detector_ui.py` becomes `tools/detector_ui.py`; `server_detector.py` becomes `ticket_analysis/server_detector.py`; `common.py (load_cmdb(), parse_server_name())` becomes `ticket_analysis/names.py`; `ticket_types.py (classify(), load_alert_map())` becomes `ticket_analysis/ticket_types.py` and `summary()` is in `tools/ticket_types.py`; `schema.py (TAXONOMY)` becomes `ticket_analysis/taxonomy.py`; `common.py (make_ticket(), mask_secrets())` becomes `ticket_analysis/tickets.py` and `ticket_analysis/masking.py`. Add one sentence to its Goal: the API imports only `ticket_analysis` and installs only requirements.txt.

Check: `grep -nE "python [a-z_]+\.py" README.md docs/prompt-1*.md docs/prompt-2*.md docs/prompt-3*.md docs/prompt-6*.md docs/alert-parser.md docs/claude-taxonomy-prompt.md docs/recommendations-review.md docs/dw-export-*.sql`
Expected: no output.

- [ ] **Step 5: Commit (after the user approves committing)**

```bash
git add README.md PLAN.md docs
git commit -m "Describe the package layout and the move from proof of concept to API"
```
