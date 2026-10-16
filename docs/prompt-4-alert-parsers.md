# Build Prompt 4: A Parser for Every Alert Layout

Use this prompt in a new Claude Code session opened in C:\src\ticket-classifier-poc.
It is independent of prompts 1 to 3 and can run before or alongside them.

## Goal

Alert tickets are machine-generated from a small number of fixed templates. Each
template should be parsed exactly, with every value (client, environment,
server, instance, alert type, dates, databases and so on) extracted as a field
and located in the text so the detector UI can highlight it. Server names found
this way are certain; they should not depend on the server detector's model.

The work has two parts: find every alert layout in the data, then write one
parser per layout, behind a registry, with a coverage report showing which
alerts are parsed and which are not.

## Read first

README.md, docs/alert-parser.md (the current parser and its coverage table),
alert_parser.py, tests/test_alert_parser.py, and in server_detector.py:
`ticket_names()`, `is_alert_ticket()`, and the `host:alert_field` rule in
`ticket_candidates()`. In detector_ui.py and detector_ui.html: the
`alert_fields` and `alert_spans` parts of `analyze()`, and `highlighted()`.

## Data handling rules (strict)

- data/ holds real client ticket text and is gitignored. Never print, paste or
  read ticket text, client names or server names from data/ into the
  conversation. Scripts that inspect data print counts, rates and masked
  shapes only: keep a word only when it is a known template word, and replace
  everything else with `server_detector.compress()` (SQL01 becomes L3 D2).
  Alert names and Grafana alert names can contain client names; mask them too.
- No network access. Tests must pass with sockets blocked (tests/conftest.py).
- Tests use made-up names only (FAKESQL01, Fake Co).
- Do not commit. Commit only when asked.

## Environment notes (learned the hard way)

- Python is .venv\Scripts\python.exe (3.14). Run tests with
  `.venv\Scripts\python.exe -m pytest -q -W error::SyntaxWarning`.
- The Bash tool is Git Bash, and its heredocs collapse `\\` to `\`. Write any
  file containing backslashes (HOST\INSTANCE, regexes, test strings) with the
  Write or Edit tool, never with a shell heredoc or `python - <<EOF`.
- Alert text uses \r\n line endings and sometimes non-breaking spaces. In
  patterns, use `[^\S\r\n]` for horizontal space (not `[ \t]` or a literal
  space) and allow `\r?` before `$` with re.M.
- Ticket files store `alert_fields` as parsed when they were built. After a
  parser change, back up the files and run
  `python alert_parser.py data/train_tickets.jsonl data/test_tickets.jsonl
  data/train_tickets_alerts.jsonl data/test_tickets_alerts.jsonl
  data/train_tickets_human.jsonl data/test_tickets_human.jsonl`.
- When the server fields change for many alerts, retrain with
  `python -u server_detector.py train --kind logreg --alert-context strip_labels`
  in the background (about 2 minutes), then score with
  `python server_labels.py evaluate --labels data/labels/claude_spans.jsonl` and
  `--labels data/labels/claude_spans_holdout.jsonl --tickets data/train_tickets.jsonl`.
  Do not let training choose the model kind; it picked a worse one before.
- The detector UI may be running on 127.0.0.1:8765; restart it after changes
  (`python detector_ui.py`).

## What exists

| Layout | How it is recognised | Parser | Alerts (of 6,000) |
| --- | --- | --- | --- |
| Collector, labeled lines (AlertId, Alert, Client Name, Server, Platform, Instance, Message, Severity, Open Date ...) | "Alert #" in the subject | `parse_labeled_lines` with `COLLECTOR_LABELS`; server, platform and hosts also read from a "ServerName: X (platform) Host(s): X" message | 5,279 |
| Backup Exception Report | subject "Backup Exception Report from server X", or the `DATABASENAME BACKUP TYPE` header | `parse_backup_report`, `backup_spans` | 407 |
| Grafana | subject starts with "[FIRING" | `parse_labeled_lines` with `FIRING_LABELS` | 203 |
| Collector, table (Client Name Environment Name Server Alert Type Open Date (UTC) Message, one row, then "Label: value" lines) | the header line | `parse_table`, `table_spans` | 68 |
| Volume Capacity | subject starts with "Volume Capacity" | collector labels; finds no server | 24 |
| Unrecognised | | none | about 19 |

Only the table and backup layouts return spans (positions) today. The
collector labeled layout and Grafana return values only.

## Task

### 1. Layout inventory

Write `alert_layouts.py` with a `layout_signature(ticket)` function and an
`inventory` command. The signature is the ordered list of template labels and
header lines in the description (values removed), plus the subject's template
words, so alerts from the same template share a signature. Group all alerts in
the train and test files by signature, merge signatures that differ only in
optional lines, and write data/reports/alert-layouts-<timestamp>.md with one
row per layout:

- layout id, alert count, families and alert names seen (masked);
- whether the current parser recognises it, and the share with a server parsed;
- one example per layout, fully masked (template words kept, every value
  replaced by its compressed shape).

Also write a masked example file per layout to data/alert_layouts/ so the user
can share it with the collector owners or with Claude Desktop. Print counts
only.

Stop after the inventory and show the user the layout table (counts and
masked signatures only). Ask which layouts to build parsers for, and whether
anyone has explained unclear fields (for example the value in brackets after
the environment name in the table layout: environment code or a machine name).

### 2. A parser per layout

Restructure alert_parser.py around a registry:

```python
@dataclass
class Layout:
    name: str                       # "collector_labeled", "collector_table", "backup_report", ...
    family: str                     # the existing family value, kept for compatibility
    matches: Callable[[str, str], bool]          # (subject, description) -> bool
    spans: Callable[[str, str], list[dict]]      # {field, start, end, in: subject|description, row?}
```

- `parse_alert(subject, description)` tries layouts in a fixed order and builds
  the fields from the spans, as `parse_table` does now; it returns the same
  dict shape as today plus `layout`. `alert_spans(subject, description)`
  returns the spans for the UI.
- Every layout returns spans, including the collector labeled layout and
  Grafana. Field names stay as they are today (server, instance, platform,
  alert_name, client_name, open_date ...) so the detector and the taxonomy
  digest keep working. Repeated rows carry a `row` number; summary fields
  (databases, backup_rows) are derived from them.
- Server-bearing fields are listed in one place (a `SERVER_FIELDS` constant),
  and `server_detector.ticket_names()` reads that list instead of naming
  fields itself.
- Recognise layouts by their body as well as their subject, so text pasted
  without its subject is parsed.
- Keep the existing tests passing; their expectations are the contract.

### 3. Tests

For each layout: one made-up example in its exact shape (\r\n endings,
non-breaking spaces where the real template has them), a test of every field
value, a test that each span's text equals its value, and the variants seen in
the inventory (empty columns, missing optional lines, extra columns). Add a
test that no human-style text ("Server is slow", "Client Name: Fake") is
recognised as an alert.

### 4. Coverage and integration

- Extend `alert_parser.py` with a `coverage` command that prints, per layout:
  alerts, parsed, with a server, and the most-filled fields. Add the table to
  docs/alert-parser.md, replacing the current coverage section.
- In detector_ui.html, use `alert_spans` for every layout; keep one colour per
  field group (client, environment, alert type, date and other, server).
- Refresh the ticket files, retrain as described above, and report the label
  scores before and after. Alerts are what should improve; human-ticket scores
  should stay within a point.

## Acceptance

- Every layout in the inventory with 10 or more alerts has a parser, or the
  user has decided to skip it.
- At least 99% of alerts are recognised, and the share with a server is
  reported per layout, with the reason for each layout below 95%.
- All tests pass. docs/alert-parser.md and README.md describe the layouts, the
  registry and both commands.
- Final message to the user: the coverage table, the label scores before and
  after, the layouts skipped and why, and any field whose meaning is still
  unclear.
