
## Prompt

You are labeling IT support tickets for a managed SQL Server database
services company. The goal is a gold-standard test set for a program that
finds SQL Server host and instance names in ticket text. Precision matters
as much as recall: mark what is truly there, and nothing else.

Each attached file is JSONL, one ticket per line, with `ticket_id`, `kind`
(human or alert), `client`, `subject`, `description` and `truncated`. Text
shown as [REDACTED] was a secret and is not a name.

For every ticket, find every SQL Server host and instance name in the subject
and the description, and classify each as one kind:

- `host`: a server name written on its own. Includes FQDNs
  (`db01.corp.local`; return the full text as written), an IP address used as
  a server, the host part of `host,1433` or `tcp:host` (return just the host),
  a UNC host (`\\fileserver01\share`: return `fileserver01`), and a server named
  in a connection string (`Server=` or `Data Source=`). Include Windows servers
  the SQL Server runs on or that the ticket is about (cluster nodes, AG
  replicas, collector machines).
- `host_instance`: a host and a named instance written together with a
  backslash, such as `SQLPRD01\REPORTS`. Return the whole text including the
  backslash (and without any `,port` suffix).
- `bare_instance`: a named instance written without its host, such as "the
  REPORTS instance", or `MSSQL$REPORTS` / `SQLAgent$REPORTS` (return just
  `REPORTS`). If the ticket makes clear which host it runs on, give that host
  in `host`.

Do not mark: database names, logins and accounts (`CORP\jsmith`,
`NT SERVICE\MSSQLSERVER`), file paths and folders, website URLs and email
domains, job, step or alert names, application names, generic words
("the server", "the instance", "managed instance", "default instance"),
company names, or people. A word that is only an instance name in some
contexts (PROD, REPORTS) counts only where the text uses it as an instance.
When you cannot tell whether a token is a server, leave it out and say why in
`notes`.

Return one JSON object per ticket, in the order given, as JSONL inside a
single code block, and nothing else after the code block:

```
{"ticket_id": "123", "status": "done", "mentions": [{"text": "SQLPRD01\\REPORTS", "kind": "host_instance", "field": "description"}, {"text": "REPORTS", "kind": "bare_instance", "field": "subject", "host": "SQLPRD01"}], "notes": ""}
{"ticket_id": "124", "status": "done", "mentions": [], "notes": "No servers mentioned."}
{"ticket_id": "125", "status": "skipped", "mentions": [], "notes": "Text is unreadable."}
```

Rules for the output:

- `text` is copied exactly from the ticket, character for character, as it
  appears (same case). Escape backslashes as JSON requires (`\\`).
- List each distinct name once per field; the import marks every occurrence of
  it, so do not repeat a name. If the same name appears with different case,
  list each spelling.
- `field` is `subject` or `description`.
- `status` is `done` (including when there are no servers, with an empty
  `mentions` list) or `skipped` only when you genuinely cannot judge the
  ticket.
- Every ticket in the file must have exactly one line, with its `ticket_id`
  unchanged.

Before answering, check each mention against the text once more: does it
appear exactly as written, and is it really a SQL Server host or instance?
