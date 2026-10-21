# Priorities

Updated 2026-10-19.

## High

- Update the Label tab's instructions to the all-engines scope (SQL Server, MySQL and MariaDB, PostgreSQL, Oracle, MongoDB, cloud databases), so DBA and Claude labels are comparable. This takes minutes and comes before any DBA labels server names.
- Build the type and intent labeling mode for DBAs, with a fixed share of DBA labels held back as the honest test set and the rest used for training.
- Have a DBA spot-check about 30 of Claude's relabeled server tickets, including alerts and non-SQL Server tickets, because every figure now rests on those labels.
- Load the CMDB export with its non-SQL Server hosts and Oracle SIDs. Then settle the disputed DEV and UAT instance detections against it before changing `instance:known_name` or `instance:name_before_instance`.
- Run the bulk labeling of the remaining 11,600 tickets with `tools/claude_label_all.ps1`. It can run over several days and resumes after a stop.
- Review and merge the `api-and-all-engine-labels` branch.

## Medium

- Retrain the server detector once the bulk labels are in. The 2026-10-19 retrain was held back by the gate: host F1 on the test set fell 2.5 points, though instance F1 rose on both sets. It is kept as `data/models/server_detector.retrained-2026-10-19.joblib`.
- Update build prompts 1, 3 and 7 with the new scope and baseline figures, and retire prompt 2, which prompt 7 replaces.
- Decide whether the alert `collector` field counts as a server.
- After the bulk labels arrive: train on hand labels (prompt 7, step 1), add name-swap augmentation (prompt 1), then run the model comparison (prompt 3).
- Grow the `ha_terms` rule into one subject keyword table per type, keeping only terms the bulk labels show are at least 90% precise.
- Schedule the quarterly retrain (`python -m tools.retrain`, see the README) and build the DW2.0 export step it needs.
- Deploy the API to Azure (`docs/azure-deploy.md`) when ready.

## Low

- Add an availability group subtype under `replication_ha_dr`, or detect availability group and listener names.
- Measure a `Host=` connection-string rule (217 tickets use that form).
- Add the Keystone feedback endpoint for DBA type corrections.
- Build the transformer and ensemble (the later steps of prompt 7), only if the comparison shows the simpler models have stopped improving.

## Done on 2026-10-19

- Relabeled Claude's 400 server-name tickets with the all-engines scope through `tools/claude_relabel.ps1`; host precision rose from 66% to 84% on the test set and from 65% to 76% on the holdout.
- Added the `master_host`, database port and Oracle SID and service name rules to the server detector.
- Added the `ha_terms` type rule for availability group, replication and failover subjects (30 fires, 96.7% precision).
- Added the Alert labels tab to the detector UI.
- Built `tools/retrain.py`, which trains both models, gates them against the serving version and publishes a dated version.
