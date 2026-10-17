# Handoff 2: what is left to do (2026-10-17)

This follows HANDOFF.md (2026-10-16), which still holds for the detector
background, the taxonomy track and the rules. This note covers what changed
since, what is waiting on the user, and the remaining work in order.

## Where things stand

Branch `server-detector`. The last commit is aa74484. **Everything below is
uncommitted.** Ask the user before committing; never push to main without
asking.

`docs/claude-labeling-prompt.md` was already modified (40 lines removed)
before this session. It is the user's change; leave it alone unless they say
otherwise.

### Done since HANDOFF.md

- **Step 1 of HANDOFF.md** (untouched test set) is done:
  `data/labels/claude_spans_holdout.jsonl` exists, and those tickets are kept
  out of training (commit 8208fce).
- **Prompt 4** (docs/prompt-4-alert-parsers.md) is implemented:
  - `alert_layouts.py inventory`: groups alerts by template, writes a masked
    report to data/reports/alert-layouts-*.md and masked examples to
    data/alert_layouts/.
  - `alert_parser.py`: a `Layout` registry with six layouts (collector_table,
    backup_report, grafana, volume_capacity, collector_markdown,
    collector_labeled). Every layout returns spans and is recognised by its
    body as well as its subject. `SERVER_FIELDS` is the single list of
    server-bearing fields, read by `server_detector.ticket_names()`.
    `python alert_parser.py coverage <files>` prints per-layout coverage.
  - docs/alert-parser.md and README.md describe the layouts, the registry and
    both commands.
- **Alert types tab** in the detector UI (detector_ui.py, detector_ui.html):
  one example per alert type (layout plus alert name) from the train and test
  files, 117 types, opened in the normal result view with fields highlighted.
  `--alert-tickets` picks the source files; pass none to turn it off.
- **Alert type list**: DW2.0 has no alert-type column, so the user decided the
  types in the existing ticket files are the list. The full list is in
  data/reports/alert-types-20261017-083625.md.

### Files added or changed

| File | Change |
| --- | --- |
| alert_layouts.py | new: layout inventory and masking |
| alert_parser.py | rewritten around the layout registry |
| server_detector.py | `ticket_names()` reads `SERVER_FIELDS`; "HOST, 1433" read as HOST,1433 |
| detector_ui.py, detector_ui.html | spans for every layout; Alert types tab |
| tests/test_alert_layouts.py, tests/test_alert_inventory.py | new |
| tests/test_alert_parser.py, tests/test_detector_ui.py | see "Test changes" |
| docs/alert-parser.md, README.md | updated |
| docs/dw-alert-types.sql | written, never run; the user switched to the ticket files instead. Delete unless the user wants it. |

**Test changes to existing tests:** only the new `layout` key in exact-match
tests, the layout name `collector_table` (was `table`), and the field
`environment_code` renamed to `collector` (user decision). 230 tests pass.

### Data state (data/, gitignored)

- The six ticket files were refreshed with the new parser. Backups:
  `*.before-layouts.jsonl`.
- The model was retrained (`--kind logreg --alert-context strip_labels`).
  Training picked thresholds host 0.29, instance 0.90. The previous model is
  `models/server_detector.before-layouts.joblib`.
- Training log: data/reports/train-layouts.log.

### Current figures

Alert coverage (6,000 alerts): 99.9% recognised, 99.1% with a server (was
98%). Volume Capacity went from 0% to 100% with a server.

Host class against Claude's labels (both sets are human tickets only):

| Label set | Before | After (threshold 0.29) | After, threshold 0.42 |
| --- | --- | --- | --- |
| claude_spans (test) | 60.1% P / 89.8% R | 57.4% / 89.4% | 60.0% / 89.4% |
| claude_spans_holdout | 54.8% / 89.2% | 51.8% / 89.2% | 55.0% / 88.8% |

Validation AP host went from 93.3% to 94.4%. At the trained threshold, human
precision falls about 3 points, outside the one-point limit in prompt 4; at
0.42 it is within a point.

## Decisions waiting on the user

1. **Host threshold.** Recommended: retrain with `--host-threshold 0.42` so
   human-ticket precision stays within a point. Not yet asked to apply it.
2. **Grafana `instance` as a server field.** Claude labeled the 12 labeled
   tickets that are Grafana alerts as having no servers, so the detector's 48
   `host:alert_field` detections there count as wrong. `target` is clearly a
   server (93 of 94 are known DW2.0 servers); `instance` looks like the
   exporter. Options: drop `instance` from `SERVER_FIELDS` for the grafana
   layout, or relabel those tickets.
3. **535 alert-shaped tickets flagged human by DW2.0** (333 collector, 185
   Grafana, 16 Volume Capacity template, 1 backup). Body recognition makes
   them alerts in the detector (`is_alert_ticket`), so they are labeled from
   their alert fields and move to the alerts segment of reports. Confirm this
   is wanted; the alternative is to recognise by body only for pasted text.
4. **Commit.** All of the above is uncommitted.
5. **docs/dw-alert-types.sql**: keep or delete.

## Next steps, in order

1. **Apply the threshold and Grafana decisions**, retrain if needed, and
   rescore both label sets:
   `python server_labels.py evaluate --labels data/labels/claude_spans.jsonl` and
   `--labels data/labels/claude_spans_holdout.jsonl --tickets data/train_tickets.jsonl`.
2. **Commit** the prompt 4 work and the Alert types tab, once the user agrees.
3. **Write and run prompt 5: a parser per alert type.** The user asked for this
   as a new prompt; write it as docs/prompt-5-alert-type-parsers.md in the
   format of prompt 4. Scope to cover:
   - Today every type parses at the layout level (server, client, severity,
     dates), but the detail inside the message is free text: the job name in
     SQL Agent Job Failure, drive and free space in Low Disk Space, blocking
     duration, AG name and node in the Availability Group alerts, latency in
     replication alerts, and so on. One parser per alert type for those
     message fields, with spans, using the same span contract
     (`{field, start, end, in, row?}`).
   - An alias map for near-duplicate type names ("SQL Agent Job Failure",
     "FD - SQL Agent Job Failure", "SQL Server Agent Job Failed"; "SQL Server
     Is Offline", "SQL Server Offline or Unresponsive", "SQL Server in
     Unresponsive"; the "(Critical)" variants), so each type has one canonical
     name plus its variants.
   - "Custom Alert" (281 alerts, 6 clients) is a catch-all; its meaning is in
     the message, so it needs its own sub-typing or is skipped by decision.
   - The 41 Grafana alert names seen at one or two clients are set per
     client; decide whether to parse them generically (labels and
     annotations) rather than per name.
   - Use the Alert types tab as the review surface: its "Fields" and "With a
     server" columns should show progress after a restart.
   - Per-type coverage in `alert_parser.py coverage`, and a test per type with
     a made-up example in its exact shape.
   - The same data handling rules as prompt 4: masked shapes only, made-up
     names in tests, no network.
4. **Remaining steps from HANDOFF.md**, unchanged and not started:
   - Step 2, the recommendations in docs/recommendations-review.md (none of
     the exclusions, structural rules, tiers or entity summary are in the code
     yet).
   - Prompts 1 to 3 (augmentation, transformer tagger, comparison). The user
     runs these in separate sessions.
   - The taxonomy track: the digest is built (data/claude_pack/taxonomy/) but
     the user has not returned a taxonomy.
   - Policy questions H1 to H5.

## Rules and pitfalls added this session

- Everything in HANDOFF.md "Rules and pitfalls" still applies (no ticket text
  in the conversation, Write or Edit for files with backslashes, tests with
  `-W error::SyntaxWarning`).
- **After a parser change:** back up the ticket files, run
  `python alert_parser.py data/train_tickets.jsonl data/test_tickets.jsonl
  data/train_tickets_alerts.jsonl data/test_tickets_alerts.jsonl
  data/train_tickets_human.jsonl data/test_tickets_human.jsonl`, then retrain
  if server fields changed. The ticket files store `alert_fields` as parsed
  when written; the Alert types tab re-parses at startup.
- **Any ticket with alert fields is an alert** to the detector
  (`is_alert_ticket`), which changes its training labels and its report
  segment. Widening body recognition therefore changes human-ticket results;
  check `alert_parser.py coverage`'s "Human tickets matched" column.
- **Both Claude label sets contain only human tickets**, so they cannot
  measure alert improvements. Use training validation AP and coverage for
  alerts, or label some alerts.
- **Training picks thresholds by validation F1**, and more alert positives
  pulled the host threshold down. Pass `--host-threshold` when the human
  precision limit matters. Do not let training choose the model kind.
- **Masking in alert_layouts.py** keeps a word only if it appears in alerts of
  eight or more organizations and is not part of an organization, server,
  client, environment or collector name, or a greeting or sign-off. Staff
  first names slipped through an organization count alone.
- Restart the UI after changes: `python detector_ui.py` (it serves
  127.0.0.1:8765; Zendesk lookups need ZENDESK_EMAIL and ZENDESK_API_TOKEN in
  .env, which are currently missing).
- This file is not gitignored (HANDOFF.md is). It contains no ticket text;
  decide with the user whether to commit it or add it to .gitignore.
