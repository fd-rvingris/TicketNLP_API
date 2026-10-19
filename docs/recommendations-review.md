# Review of Recommendations A1 to H5 Against the Current Server Detector

Prepared 2026-10-16 for the reviewer who proposed recommendations A1 to H5, and
for the project team.

Each item was checked against the code (`server_detector.py`, `claude_pack.py`,
`server_labels.py`, since split into ticket_analysis/ and tools/) and by running made-up strings shaped like the reviewer's
examples through the saved model, with no client selected. "Probe" below refers
to that run.

I do not have the reviewer's gold set. The only gold labels I can measure
against are the 200 Claude-labeled human test tickets in
`data/labels/claude_spans.jsonl`. On those, the detector's host precision is
59.6%, recall 89.8%, and recall on names the directory did not know 71.6%.

## 1. Already implemented

| ID | Status detail | Reason or Plan |
| --- | --- | --- |
| A5 | The rule `host:host_instance` matches HOST\INSTANCE inside brackets, because brackets are not token characters. The probe detected the pair from "Validation Results for Server [HOST\INST]". | No title-specific rule is needed. The general backslash rule covers it. |
| A10 | `SERVICE_RE` and the rule `instance:service_suffix` detect MSSQL$ and SQLAgent$ names. The probe found FINANCE in SQLAgent$FINANCE. | Broader than recommended: it also covers SQLServerAgent$, ReportServer$, MSSQLFDLauncher$, MSOLAP$ and similar names. |
| G1 | The tokenizer keeps HOST\INSTANCE as one token, which becomes one `host_instance` candidate. The Claude-label importer claims longer names first (`spans_from_mentions`). | No change needed. |
| G2 | Candidates are whole tokens, so a bare instance never matches inside a longer name. The probe marked only the full SSIS host, not the shorter name inside it. | No change needed. |

## 2. Partially implemented

| ID | Status detail | Reason or Plan |
| --- | --- | --- |
| A1 | Single backslash: works. Doubled backslash: **broken.** `token_parts` treats a doubled backslash as a path separator. In the probe, `FAKECL\\PROCMAIN` produced PROCMAIN as a host and missed FAKECL. The host pattern also accepts names starting with a digit, which A1's pattern does not. | **Will fix.** A doubled backslash inside a token (not at the start, where it means a UNC path) will be read as one separator. Tested with both forms and with a real UNC path. |
| A2 | A colon port of 1433 or 1434 fires `host:port`, so the probe's `:1433` SPN host was detected. The `MSSQLSvc/` prefix is not recognized, and with a named-instance suffix the host was missed; only the suffix was found, by the model (0.84). | **Will add** an SPN rule: the FQDN becomes a host, and a non-numeric suffix becomes an instance paired with that host. Tested with the port and instance forms. |
| A3 | The bracket CI form was found by the model only (0.78). The "SQL IP Address 1 (NAME)" form was missed. | **Will add** a rule for "Created on CI:" and "Created on Node:" with brackets, and for "SQL IP Address N (NAME)". |
| A6 | Found by the model (0.81); no rule. | **Will add** a rule: a name followed by CLUSTER_NODE on the same row is a host. |
| A7 | "DB Server: name" fires `host:after_server_word`, but only when the name contains a digit. "Server/Instance name:" works through the HOST\INSTANCE rule. Being after a label is a model feature only. The "Host Name column" form is not handled. | **Will add** a labeled-field rule for Server:, DB Server:, Server Name: and Server/Instance name:, keeping the digit-or-known-name condition (see H5). **Not adding** "Host Name" columns: in blocking reports those are client machines, which conflicts with D6. |
| A8 | The UNC rule exists (`host:unc_path`) but has no space guard. The probe confirmed the D4 false positive: "\\\\Fortified Data Services, LLC\\..." produced a host. On the Claude labels this rule was correct 1 time in 3. A8's own pattern would still accept it, because it allows whitespace after the name. | **Will fix.** A UNC host is rejected when more words and then a backslash follow on the same line (a space in the first path segment). Tested with both of the reviewer's examples. |
| B1 | The model uses shape, client-profile and context-word features, and its scores are thresholded. Shape alone never fires a rule; the server-word rule needs a digit plus server, host, vm, box, machine or node nearby. Patch, reboot, RDP, connect to, backup and decommission are model context only. | **Keep in the model rather than a rule**, as the reviewer suggests. Covered further by G4. |
| B3 | Dotted names are normalized to their first label, but the probe missed "fakeinvsql5.investran" entirely. HOST\\db\\schema paths are split and the host found (model 0.90). A single HOST\\dbname would be read as HOST\INSTANCE, which is wrong. | **Will add** a rule for a server followed by a database: when the right-hand part of a backslash pair has lower-case and underscore patterns typical of database names, or is longer than 16 characters, it is not an instance and the left side is kept as a host. Without the reviewer's gold set this is heuristic. |
| C1 | `NameProfile` learns per-client prefixes and patterns from the known-names directory, used as model features and in `host:near_known_name`. Not learned from the gold set. | **Will not learn from the gold set:** it would leak the test labels into the detector, and every figure measured afterwards would be inflated. If the reviewer's lists are added, they should go into `data/instances.csv` as directory data, kept separate from the evaluation labels. |
| C2 | `instance:known_name` fires for the client's known instance names written in upper case. Instance vocabulary is a model feature, and pairing uses the client's known pairs. The source is `dw_servers` and alert fields only. | Same as C1. The upper-case condition stays: lower-case instance words in running text were mostly ordinary words. |
| D1 | Indirect only. The HOST\INSTANCE rule needs a digit in the host part, a known host or a connection string; the probe ignored FAKEDOM\svcs_devportal1 and NT SERVICE\MSSQLSERVER. A domain name containing a digit, like the reviewer's first example, would still fire. | **Will add** an exclusion for NT AUTHORITY and NT SERVICE, and a per-client domain list learned from the training text: left-hand names that repeatedly appear with account-like right-hand names (svc_, s_, or first-name and last-name forms). Tested with digit and digit-free domains. |
| D3 | No explicit rule. Job names are single tokens because underscores are token characters, and the probe detected nothing in the job-name example. | **Will verify** on the Claude labels before writing a rule. If job-name false positives appear, a quoted-name and "Job Name" context exclusion will be added. |
| D4 | Exists: email parts (`@`), file extensions (`FILE_EXTS`), drive-letter paths (split at the colon), a `localhost` exclusion, and SQL-style ports only. URL context is only a model feature (`in_url`). The UNC-with-space gap is A8. | **Will fix** A8, and will turn hosts of `http(s)://` addresses from a feature into an exclusion unless they are a known host. |
| D5 | MSSQLSERVER is stored as the host alone (`DEFAULT_INSTANCE`), as `match_mention()` assumes. Placeholders were not detected in the probe, but only because of their shape, not by rule. | **Will add** explicit exclusions for SQLSERVERAGENT, braced placeholders such as {ServerName}, and a short list of example names. |
| E1 | Three of the four forms exist: backslash, service name, and "<name> instance" or "instance <name>". The SPN form is in A2. "Access in P2" is not handled. | **Will add** the SPN form (A2). **Will not add** "access in P2": it needs a client instance dictionary that does not exist yet, and "in <word>" is too common to use without one. |
| G3 | Each detection's `host` and `instance` are normalized to lower case, so spellings group by value, but detections are listed per mention, not per entity. | **Will add** an entity summary per ticket (one row per normalized name, with mention count and spellings) to the output and the UI. Low effort, presentation only. |
| G4 | Effectively two tiers: rule matches score 0.95, and model matches use a threshold. Below the threshold, candidates are dropped, not routed. | **Will add** a `tier` field: high for structural rules, medium for context and known-name rules, low for model-only detections above the threshold, and `review` for model scores between about 0.2 and the threshold. The UI shows review items separately. |
| G5 | Per-rule precision exists in the server_labels report ("Detections by source") and in the evaluate report's rules table. Per-rule recall does not. | **Will add** per-rule recall: the share of labeled spans each rule hits, and the share only that rule hits. |

## 3. Skipped or rejected

| ID | Status detail | Reason or Plan |
| --- | --- | --- |
| D6 | Not implemented. Blocking-report host names are only excluded if the model happens to score them low. | **Deferred.** It needs the client_host label (F2) and a decision on H policy. Without that label, it cannot be measured, and the "Host Name" column means different things in different reports (A7 versus D6). |
| D7 | No platform filter. The probe did not detect a MySQL exporter host, because its port is not SQL-style and the model scored it low. | **Deferred** pending H1. If non-SQL Server hosts are out of scope, this becomes a platform tag (MySQL, MariaDB, PostgreSQL, Oracle, exporter ports) rather than a silent filter, so mixed tickets are not lost. |
| F1 | Out of scope for the server detector. The client comes from the Zendesk organization on the ticket. | **Separate task.** Client-name extraction and alias normalization should be its own module with its own labels. |
| F2 | No client_host label exists. | **Deferred.** Adding a label kind changes the Label tab, the Claude labeling prompt and all scoring, so it should wait for the D6 and H decisions. |
| C1 (gold-set part) | See C1 in group 2. | **Rejected** in its gold-set form, because of evaluation leakage. The directory-based form is already implemented. |

## 4. New, will implement

| ID | Status detail | Reason or Plan |
| --- | --- | --- |
| A4 | Not implemented; the probe missed "Physical Memory for <HOST>". | **Will add** a rule limited to ticket subjects: "for <NAME>" at the end of the subject, where the name has a digit. "For X" is too common in body text to use there. Tested with alert subjects and with ordinary "waiting for approval" sentences. |
| A9 | Not implemented; the probe missed an Azure SQL endpoint. | **Will add** a high-tier rule for `*.database.windows.net` that keeps the full FQDN as the display text, with the server label as the host. |
| B2 | IP addresses are host candidates, but no rule fires; the probe missed "RDP to <IP>". | **Will add** a medium-tier rule: an IP within three words of RDP, SQL box, DB Server:, connect to or server. Tested with version-number and log-line decoys. |
| D2 | Not implemented; the probe detected an AG name as a host (model 0.68). | **Will add** an exclusion for names inside "availability group '...'" and names directly before "AG". The suffix-only form (ending in AG with no other signal) becomes low tier and review, not dropped, because some real hosts may end in AG. |
| D8 | Not implemented; the probe detected "P2" in "P2 Portal" as a bare instance (model 0.92). | **Will add** an exclusion: an instance word followed by Portal, App, Application, Tool or Site is not an instance. |

## 5. Policy questions (H)

| ID | Current behavior | Recommended decision |
| --- | --- | --- |
| H1 | No platform distinction. Any database host can be detected if its name or context looks like a server. | Out of scope for the host label, but tag the platform (D7). The company manages SQL Server, so non-SQL hosts should not count as misses, and they should not be dropped silently either. |
| H2 | FQDNs are normalized to their first label, so a DNS alias such as `coda.dev.db...` becomes "coda", which loses its meaning. Linked server names are detected only by shape. | Count both as hosts. Change normalization to keep the full FQDN when the first label alone is generic or ambiguous, so aliases resolve correctly against the CMDB. |
| H3 | Not detected: names inside job or step names are part of a longer token. | Do not count them as host mentions. If wanted, report them as low tier for review. |
| H4 | Listed servers are detected if their shape, a rule or a known name supports it. | Count every server in a migration or decommission list. A list is strong server context, and DBAs need those references even for non-SQL hosts, with the platform tagged per H1. |
| H5 | The model decides. "Server: Procmain" fires no rule, because the name has no digit. | Route these to the `review` tier (G4) rather than forcing a decision. In labeling, mark them with a note. A client instance dictionary (C2) can resolve the "Server: Procmain" case to a bare instance. |

## Top three by expected gain

1. **An exclusion layer: D1, D2, D8, D4's URL hosts and the A8 space guard.**
   Precision is the detector's weakness (59.6%, and the model alone is right 47%
   of the time). Each probe failure in this group was a false positive. The A8
   fix alone removes a rule that was right 1 time in 3 on the Claude labels.
2. **Confidence tiers with a review route (G4).** This turns the low-precision
   model detections into review items instead of errors, and gives DBAs a short
   queue for the ambiguous cases in H5. It raises the precision of what is
   reported without losing recall.
3. **Structural fixes: A1, A2 and A9.** A1 is a current bug that produces a
   wrong host and misses the real one. A2 and A9 are unambiguous forms the
   detector misses entirely. They are cheap and safe, and they add recall on
   exactly the names an unknown-name detector should never miss.

## Risks in the recommendations

- **A7 versus D6.** "Host Name" means a SQL server in long-running query reports
  and a client machine in blocking reports. A rule for either will misfire on
  the other without report-type detection.
- **B3 and D2's suffix rule can reduce recall.** "A backslash followed by a known
  database is not an instance" needs a database dictionary, and real instances
  are sometimes named like databases. A name ending in AG with no other signal
  will occasionally be a real host. Both belong in the review tier, not in hard
  exclusions.
- **C1 and C2 learned from the gold set** would make every later measurement
  against that gold set optimistic.
- **D1's domain list,** if learned automatically, could absorb a NetBIOS domain
  that is also a server name. It should be reviewable per client.
- **E1's "access in P2" and A4's "for X"** match very common phrasing and will
  reduce precision unless kept as narrow as proposed.
- **Many rules increase overlap and conflicts.** Conflicts need a fixed
  precedence: exclusions first, then structural rules, then context rules, then
  the model. That order should be tested.

## Gaps the recommendations missed

- **Directory quality.** The largest false-positive source measured so far was
  `dw_servers` containing digit-free abbreviations and words, which made the
  known-host rule wrong 84% of the time on those names. It now requires a digit.
  Cleaning the directory itself is still needed, and it matters doubly once the
  CMDB API replaces it.
- **Typos and siblings.** Already handled: `near_match` reports `typo_of_known`
  (one edit, with swaps counting as one) and `sibling_of` (only trailing digits
  differ). Statuses and bare-instance pairing to a host are not covered by the
  recommendations either.
- **Templated alert text.** Already handled: the label words of alert templates
  are removed from the context (`strip_labels`), and instance rules skip alert
  template lines.
- **Measuring unseen names.** The recommendations measure against a gold set,
  but say nothing about names the system has never seen. The detector's
  evaluation already holds out 20% of hosts and of instance names, and the
  hand-label report splits recall into known and new names. The new-name recall
  (71.6%) is the figure that predicts performance on new clients.
- **Secrets.** Ticket text shown to labelers or sent to Claude is masked; the
  recommendations do not mention it.
- **Labeler agreement.** Gold-set quality is not measured. The Label tab
  double-labels 15% of tickets to report agreement; the reviewer's gold set
  should get the same check.

All "will implement" items are proposals; none has been built yet. Each would
be measured against the Claude labels and the probe strings above, and against
the reviewer's gold set if it is shared.
