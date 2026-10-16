# Ticket Classifier PoC: Plan

Prepared 2026-10-15. A standalone proof of concept, run before and separately from the Keystone integration work. `README.md` describes how to use the PoC, and `BUILD_PROMPT.md` is the prompt that builds it in Claude Code.

## Context and Goal

Before committing days of Keystone work, we want evidence on four questions, using real tickets:

1. Does a single LLM call classify ticket type and intent accurately enough to be useful?
2. Does it reliably pick out the servers and instances mentioned in the ticket?
3. How often do those names match the CMDB?
4. What does each ticket cost, and how long does it take?

The PoC is throwaway code. Four things carry forward into the Keystone integration plan (a separate document, `keystoneAIModelUtilityPlan.md` in the Keystone repo): the **taxonomy**, the **prompt**, the **output schema**, and the **hand-labeled results**.

**Decisions:**
- **Language and runtime:** Python, on the Windows machine. Python 3.14 is already installed at `C:\Python314`.
- **Location:** a separate folder, `C:\src\ticket-classifier-poc`, with its own git repository. It is outside Keystone and outside any OneDrive-synced folder, because it will hold client ticket data.
- **Ticket sources:** both a read-only Zendesk fetch and a local export file.
- **Provider:** the Anthropic API.

## Plan

**Design, kept deliberately small:**
- **Six scripts, one shared module:**
  - `fetch_tickets.py` pulls tickets from Zendesk.
  - `load_export.py` normalizes a CSV or JSON export.
  - `classify.py` calls the model.
  - `report.py` produces metrics and a labeling sheet.
  - `compare.py` compares two runs.
  - `common.py` holds the shared code.
- **Plain files, no database:** JSONL for tickets and results, CSV for CMDB instances and human labels. Labeling happens in Excel, so no UI is built.
- **Portable artifacts:** `taxonomy.json`, `prompts/classify_v1.md`, and the Pydantic schema generated from the taxonomy are the pieces that move to Keystone. The schema is generated so the enums can never drift from the taxonomy.
- **Same rules as the target design, so results transfer:**
  - one ticket per call;
  - structured output with closed enums plus `unknown`;
  - every extracted mention carries an evidence quote, checked against the ticket text;
  - CMDB matching done deterministically in code, never by the model;
  - secrets masked before sending;
  - no tools.
- **Zendesk is read-only:** the code issues only GET requests.
- **Model:** default `claude-opus-5-5` at `effort: low`. `--model` switches to `claude-sonnet-5-5` or `claude-haiku-5-5` for comparison. The model decision is made from the comparison results.

**Timebox (one person, about half a day):**

| Hour | Activity | Output |
| --- | --- | --- |
| 0 to 1 | Run the build prompt in Claude Code; set up the virtual environment and `.env`; fetch about 100 recent tickets; export a CMDB instance CSV. | `data/tickets.jsonl`, `data/instances.csv` |
| 1 to 2 | Classify 20 tickets, read the output, adjust the taxonomy and prompt (v1 to v2). | Prompt v2 |
| 2 to 3 | Classify all 100; fill in about 50 rows of the labeling sheet in Excel (roughly 30 to 40 minutes). | `data/labels.csv` |
| 3 to 4 | Run the report; rerun on a second model; compare the two runs. | `data/reports/*.md` |

**Success criteria (proposed; adjust before running):**

| Measure | Target | Why it matters |
| --- | --- | --- |
| Type accuracy on labeled tickets | at least 80% | Below this, release 1 shadow mode would teach us little. |
| Intent accuracy on labeled tickets | at least 75% | Intent is more subjective, so the bar is lower. |
| `unknown` rate | at most 15% | Shows whether the taxonomy covers the real tickets. |
| Evidence quotes found in the ticket text | at least 95% | Shows whether extraction is trustworthy. |
| Mention precision (human judged) | at least 90% | Wrong servers would mislead later automation. |
| CMDB match rate for real mentions | measured, no target | Evidence for or against adding alias or FQDN fields to the CMDB. |
| Cost per 1,000 tickets, p95 latency | measured | Inputs to the model choice and to the Keystone plan's performance and cost requirements (NFR-15, NFR-17). |

**Possible outcomes:**
- **Go:** the targets are met. Move to Keystone R0 and R1, carrying the taxonomy, prompt and labels forward.
- **Iterate:** accuracy is close. Revise the taxonomy or prompt and run again.
- **Stop:** the classes cannot be separated reliably even with a revised taxonomy. Record why.

**Data handling:**
- `.env` and `data/` are gitignored.
- No ticket text is printed to the console beyond the ticket ID and subject.
- The `data/` folder is deleted when the PoC ends, unless the labels are moved into Keystone release 1.
- Sending ticket content to the provider is approved in general. Confirm the Anthropic org's data retention setting before the first run.

**Verification:**
- `python -m pytest`: unit tests for secret masking, evidence quote validation, CMDB matching, and the schema generated from the taxonomy. The tests make no network calls.
- A dry run (`--dry-run`) prints the rendered prompt for one ticket without calling the API.
- A smoke run on 3 tickets returns schema-valid JSONL with token counts.
