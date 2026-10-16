# Ticket Type Taxonomy in Claude Desktop

Claude proposes a ticket type and intent taxonomy from a digest of every ticket
in the exports (training and test, alerts and human), to replace the v0 draft
in taxonomy.json.

## How to run it

1. Prepare the digest (already done once):

       python claude_pack.py taxonomy

   This writes data/claude_pack/taxonomy/ (about 1.1 MB, secrets masked):

   | File | Contents |
   | --- | --- |
   | human_subjects.tsv | every human ticket's subject with its Zendesk type, category and sub type (6,000 rows) |
   | alert_names.tsv | alert tickets counted by family, alert name and category (alerts are templated, so counts replace text) |
   | human_sample.jsonl | 600 human tickets with the first 800 description characters, spread across Zendesk categories |
   | label_counts.json | counts of every Zendesk type and category, for alerts and human tickets |
   | taxonomy_v0.json | the current draft taxonomy |

2. In Claude Desktop, start a new chat with a strong model (Opus), attach all
   five files directly to the message (not as project knowledge, so they are
   read in full rather than searched), and paste everything under "Prompt"
   below. If Claude says the files are too large, rerun with
   `python claude_pack.py taxonomy --sample 300 --sample-chars 500`.

3. Claude returns three files. Save them as:

   - data/claude_pack/returned/taxonomy_v1.json
   - data/claude_pack/returned/taxonomy_sample_labels.jsonl
   - data/claude_pack/returned/taxonomy_notes.md

4. Check the taxonomy's shape:

       python claude_pack.py check-taxonomy data/claude_pack/returned/taxonomy_v1.json

   Review it with the team before it replaces taxonomy.json. A new taxonomy
   version changes classify.py's prompt and schema, so earlier runs are no
   longer comparable.

Check with your organization that client ticket text may be used in Claude
Desktop before uploading.

## Prompt

You are designing the ticket taxonomy for a managed SQL Server database
services company. Its support desk receives two kinds of tickets: automated
monitoring alerts (from the company's collector, Grafana, backup exception
reports and volume capacity checks) and tickets written by clients and staff.
The taxonomy will be used by an LLM classifier, by reporting, and later by
routing, so each ticket must fit one primary type, and two people reading the
same ticket should pick the same type.

Attached is a digest of all 12,000 tickets in the current exports:
`human_subjects.tsv` (every human ticket's subject with its current Zendesk
type, category and sub type), `alert_names.tsv` (alert tickets counted by
family, alert name and category), `human_sample.jsonl` (600 human tickets with
the start of the description), `label_counts.json` (label counts) and
`taxonomy_v0.json` (the current draft). Text shown as [REDACTED] was a secret.

Work in this order:

1. Read all of it. Use the subjects and alert counts to see the whole
   population and the sample to understand what tickets actually ask for.
   Note where the Zendesk categories are inconsistent, overlapping or too
   coarse.
2. Propose types and intents. Type is what the ticket is about (backup,
   performance, access, ...); intent is what the requester wants done (fix an
   incident, make a change, answer a question, ...). Start from v0 and keep its
   ids where the meaning is unchanged. Aim for 10 to 20 types and 5 to 10
   intents; each must be clearly distinct, cover a meaningful share of tickets,
   and have a definition that says what is in and what is out (name the nearest
   other type when they could be confused). Keep `unknown` in both lists. Say
   how alerts map: every alert name in `alert_names.tsv` must map to one type.
3. Test it: assign every ticket in `human_sample.jsonl` a primary type,
   optional secondary types, an intent and a confidence (high, medium, low).
   Then revise definitions where you hesitated, and redo the assignments
   affected.

Return three files, each in its own code block, with the file name on the line
before the block:

**taxonomy_v1.json**, in exactly this shape (ids are lower snake_case):

```
{"version": "v1-draft",
 "types": [{"id": "backup_restore", "label": "Backup and restore", "definition": "..."}],
 "intents": [{"id": "fix_incident", "label": "Fix an incident", "definition": "..."}]}
```

**taxonomy_sample_labels.jsonl**, one line per ticket in human_sample.jsonl:

```
{"ticket_id": "123", "primary_type": "performance", "secondary_types": [], "intent": "fix_incident", "confidence": "high"}
```

**taxonomy_notes.md**: the changes from v0 and why; a table mapping each
current Zendesk category to the new types (with rough ticket counts); a table
mapping each alert name to a type; the share of sampled tickets per type and
intent; the types you found hardest to tell apart, with an example subject for
each confusion; and open questions for the team.

Do not invent tickets or counts. Base every count on the attached files, and
say when a figure is an estimate.
