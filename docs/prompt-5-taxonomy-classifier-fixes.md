# Build Prompt 5: Make the Local Taxonomy Classifier Robust

Use this prompt in a new Claude Code session opened in C:\src\ticket-classifier-poc.
It is independent of prompts 1 to 4.

## Goal

`ticket_types.py` applies the ticket taxonomy (taxonomy.json, v1-draft) without
an LLM: alerts take their type from the alert-name table in the taxonomy notes,
and every other ticket is scored by TF-IDF and logistic regression trained on
Claude's 600 labeled human tickets. Cross-validated, it reaches 73.3% type
accuracy and 83.2% intent accuracy, but it fails in a predictable way on short
requests wrapped in email noise.

Example (made up, but the same shape as a real failure): a client asks
"Could you please help me get READ access for the schema below, Server=FAKESQL01,
Database=FAKE_DB, for the execution user CORP\svc-fake-batch. Please approve."
The email also carries an external-sender CAUTION banner, an inline image link,
a signature, and a confidentiality footer ("Any disclosure, copying,
distribution..."). With no subject, the model scored database_refresh 17%,
access_security 14%, deployment 13%, and picked database_refresh. The words
"access", "read", "user" and "approve" pushed the right way, but many small
pushes from the footer, "PROD", "database", the support mailbox name and a
person's first name outweighed them. The correct type is access_security
(staff_access is only for Fortified Data's own accounts).

Make three changes, measure each, and keep only what helps:

1. Strip email boilerplate before scoring, in training and in classification.
2. Add a keyword rule layer ahead of the model for unambiguous wording.
3. Flag low-confidence results so a near-tie is visibly uncertain.

## Read first

README.md (sections "Non-LLM baseline" and "Trying the model by hand"),
taxonomy.json, prompts/classify_v2.md (the primary-type rules and tie-breaks),
ticket_types.py, baseline.py (`features()`, `build_model()`, `run_labeled()`,
`main_labeled()`), detector_ui.py (`taxonomy()`, `classify_text()`) and
detector_ui.html (the Classify ticket and How types are chosen tabs), and
tests/test_ticket_types.py. The taxonomy notes are in
data/claude_pack/returned/taxonomy_notes.md: read the sections "How to choose
the primary type" and "Hardest distinctions" only, because other sections quote
ticket subjects.

## Data handling rules (strict)

- data/ holds real client ticket text and is gitignored. Never print, paste or
  read ticket text, client names, people's names or server names from data/
  into the conversation. Scripts that inspect data print counts, rates, label
  ids and feature names only. Feature names can contain names (the model learned
  a client's domain and a person's first name), so when listing influential
  features, print only those that are in a vocabulary of taxonomy and support
  words you define, and count the rest.
- No network access. Tests must pass with sockets blocked (tests/conftest.py).
- Tests and docs use made-up names only (FAKESQL01, Fake Co, CORP\svc-fake).
- Do not commit. Commit only when asked.

## Environment notes

- Python is .venv\Scripts\python.exe (3.14). Run tests with
  `.venv\Scripts\python.exe -m pytest -q -W error::SyntaxWarning`.
- The Bash tool is Git Bash, and its heredocs collapse `\\` to `\`. Write any
  file containing backslashes (regexes, CORP\user, test strings) with the Write
  or Edit tool, never with a shell heredoc or `python - <<EOF`.
- Ticket text uses \r\n line endings and sometimes non-breaking spaces; use
  `[^\S\r\n]` for horizontal space in patterns.
- The baseline on Claude's labels:
  `python baseline.py --labels data\claude_pack\returned\taxonomy_sample_labels.jsonl`
  (5-fold stratified cross-validation, a few seconds). Its last report is in
  data/reports/baseline-labels-*.md; record the current numbers before changing
  anything.
- `python ticket_types.py train` refits the saved model
  (data/models/ticket_types.joblib). The detector UI may be running on
  127.0.0.1:8765; restart it after changes (`python detector_ui.py`), because it
  loads the model and routes at startup.

## Task

### 1. Strip email boilerplate

Add `clean_text(subject, description) -> (subject, description)` in a new
module (for example text_clean.py) and use it in both places the model sees
text: `baseline.features()` (so cross-validation trains and scores on cleaned
text) and `ticket_types.classify()`. The saved model and the cross-validation
must apply the same cleaning, or the measurement is meaningless.

Remove, with conservative patterns:
- external-sender banners ("CAUTION: This email originated from outside...",
  "[EXTERNAL]", "This message came from outside your organization");
- confidentiality and disclaimer footers (from a line like "This message is
  intended only for", "CONFIDENTIALITY NOTICE", "This e-mail and any
  attachments" to the end of that paragraph or the text);
- image and attachment links (Markdown images `![](...)`, `[cid:...]`,
  "image001.png") and bare URLs;
- quoted reply history (from a line "From: ... Sent: ..." or "On ... wrote:"
  to the end), and greeting and sign-off lines ("Hi team,", "Thanks,",
  "Regards,") when they stand alone;
- the support mailbox name ("Fortified Data Support") and @-mentions of people.

Rules: never remove a line that also contains a server-shaped token, a SQL
keyword or an error number; never return empty text when the input had a
request (fall back to the uncleaned text). Keep the server detector unchanged:
it reads raw text and must not use this cleaning.

Measure how much text the cleaning removes across the ticket files (share of
tickets touched, median characters removed, per pattern), printing counts only.

### 2. Keyword rule layer

Add rules that run before the model in `ticket_types.classify()` and return a
type with source `rule:<name>`, the way the server detector's rules work
(server_detector.py `_rule()`). Start with access, the confirmed failure:

- access_security: a grant, read, write or access request for a login, user,
  role, service account or schema ("READ access", "grant access",
  "db_datareader", "db_owner", "add the login", "permission to"), unless the
  account is a Fortified Data staff account or the text is a scheduled log-on
  task, which are staff_access.

Then consider a small number of others only where the taxonomy definitions and
tie-breaks make the wording unambiguous (for example Validation Results reports
are always outage_availability; a "[Log on to ...]" keep-alive task is
staff_access). Derive rules from taxonomy.json and prompts/classify_v2.md, not
from reading tickets. Each rule must be a pure function of the cleaned text with
a test.

Measure every rule against Claude's 600 labels: how often it fires and its
precision. Drop or tighten any rule under 90% precision. Then measure the whole
classifier (rules first, model for the rest) with the same 5-fold
cross-validation; the rules must not be fitted to the labels they are scored on.

### 3. Flag low confidence

The model's top probability and the gap to the second are available in
`classify()`. Add `uncertain: bool` and a short `uncertain_reason` to its
result when the top type's probability is below a threshold or within a small
margin of the second (start with p < 0.30 or gap < 0.05, then choose the values
from the cross-validated predictions: report accuracy for confident and
uncertain tickets and the share flagged, and pick thresholds where the
uncertain group is clearly worse). Rule and alert-table results are not flagged
by probability; alert-table entries whose name does not decide the type already
carry a note.

In the Classify ticket tab, show an uncertain result with a visible marker and
the reason ("close call between database_refresh and access_security; add a
subject or check by hand"), and suggest adding a subject when none was given,
since the subject carries a lot of weight.

### 4. Tests, docs and measurement

- Tests: cleaning (each pattern, the never-remove rules, the empty fallback,
  \r\n text), each keyword rule (fires and does not fire, the staff_access
  exception), the uncertain flag, and the made-up example above classified as
  access_security with or without a subject.
- Update README.md, the How types are chosen tab (a step for cleaning, a step
  for rules, the uncertain flag) and the accuracy figures shown there.
- Report a before/after table from `baseline.py --labels` (accuracy and macro
  F1 for type and intent, all, high confidence, medium or low) with each change
  added in turn: cleaning, then rules, then both. Also report per-class F1 for
  access_security, staff_access, database_refresh and deployment.

## Acceptance

- The made-up example is classified access_security, with and without a
  subject.
- Type accuracy and macro F1 on the 600 labels do not drop with any kept
  change, and access_security F1 improves. Revert any change that lowers
  overall accuracy and say so.
- Every rule kept has at least 90% precision against Claude's labels.
- The uncertain flag marks a group whose accuracy is clearly below the rest,
  with the thresholds and the measured split documented.
- The server detector's results are unchanged (it does not use the cleaning).
- All tests pass; nothing is committed.
- Finish with a short report: what changed, the before/after table, the rules
  kept and dropped with their precision, the chosen uncertainty thresholds, and
  what to do next (more labels for the weak types are the likely next step).
