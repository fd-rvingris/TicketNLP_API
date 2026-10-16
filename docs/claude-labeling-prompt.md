# Server Labeling in Claude Desktop

Claude labels the hosts, HOST\INSTANCE names and bare instance names in ticket
batches, so the server detector can be measured on real tickets without waiting
for DBAs or an API key.

## How to run it

1. Prepare the batches (already done once; rerun to change the sample):

       python claude_pack.py labels                    # 200 human test tickets, same sample as the DBA Label tab
       python claude_pack.py labels --train-human 300  # optionally add training tickets

   This writes data/claude_pack/labels/batch_001.jsonl and onward, about 25
   tickets each, with secrets masked.

2. In Claude Desktop, create a Project and paste everything under "Prompt"
   below into its instructions. Use a strong model (Opus).

3. For each batch, start a new chat in the project, attach one batch file, and
   send: `Label this batch.` Long chats degrade, so use one chat per batch.

4. Save Claude's reply as data/claude_pack/returned/labels_001.jsonl (matching
   the batch number). Save only the JSONL lines, without the code fence.

5. Import and score:

       python claude_pack.py import-labels data/claude_pack/returned/labels_*.jsonl --dry-run
       python claude_pack.py import-labels data/claude_pack/returned/labels_*.jsonl
       python server_labels.py evaluate --labels data/labels/claude_spans.jsonl

   The import finds every occurrence of each returned name in the original
   ticket text and reports names it could not find. Claude's labels go to
   data/labels/claude_spans.jsonl, separate from the DBA labels. When DBAs label
   the same tickets in the Label tab, comparing the two files shows how far
   Claude's labels can be trusted.

Check with your organization that client ticket text may be used in Claude
Desktop before uploading. The batches are masked for passwords, keys and
tokens, but still contain client names, server names and ticket content.

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
