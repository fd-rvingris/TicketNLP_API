## Prompt

You are labeling IT support tickets for a managed database services company.
Most clients run SQL Server, but the company also manages MySQL, MariaDB,
PostgreSQL, Oracle, MongoDB and cloud databases (Amazon RDS and Aurora, Azure
SQL). The goal is a gold-standard test set for a program that finds database
server host and instance names in ticket text, for every database engine.
Precision matters as much as recall: mark what is truly there, and nothing
else.

The tickets are JSONL, one ticket per line, with `ticket_id`, `kind` (human
or alert), `client`, `subject`, `description` and `truncated`. Text shown as
[REDACTED] or masked with `*` was a secret and is not a name.

For every ticket, find every database server host and instance name in the
subject and the description, whatever the engine, and classify each as one
kind:

- `host`: a server name written on its own. Includes FQDNs
  (`db01.corp.local`; return the full text as written), cloud database
  endpoints (`orders.abc123.us-east-1.rds.amazonaws.com`,
  `sales.database.windows.net`; return the full text), an IP address used as a
  server, the host part of `host,1433`, `tcp:host` or `host:port` with any
  port (`pg01:5432`, `mysql02:3306`, `ora03:1521`,
  `prod-db-1.example.net:9104`; return just the host), a UNC host
  (`\\fileserver01\share`: return `fileserver01`), and a server named in a
  connection string or URI (`Server=`, `Data Source=`, `Host=`, `host=`,
  `jdbc:mysql://host`, `postgres://host`, `@host:1521`). Include the Windows or
  Linux servers a database runs on or that the ticket is about (cluster nodes,
  AG and replication replicas and primaries, `master_host` values, collector
  machines).
- `host_instance`: a host and a SQL Server named instance written together
  with a backslash, such as `SQLPRD01\REPORTS`. Return the whole text
  including the backslash (and without any `,port` suffix).
- `bare_instance`: an instance name written without its host. For SQL Server,
  a named instance such as "the REPORTS instance", or `MSSQL$REPORTS` /
  `SQLAgent$REPORTS` (return just `REPORTS`). For Oracle, a SID or service
  name such as `ORCL` or `PRODCDB` (`SID=ORCL`, `SERVICE_NAME=PRODCDB`, the
  `/PRODCDB` in `host:1521/PRODCDB`; return just the name). If the ticket
  makes clear which host it runs on, give that host in `host`. MySQL, MariaDB,
  PostgreSQL and MongoDB have no named instances: mark only their hosts.

Do not mark: database names (including PostgreSQL and MySQL databases and
schemas), logins and accounts (`CORP\jsmith`, `NT SERVICE\MSSQLSERVER`), file
paths and folders, website URLs and email domains, job, step, alert and
exporter job names (`job = cb_prod_db1_slave4`), application names, product
and engine names ("MariaDB", "Postgres"), generic words ("the server", "the
instance", "managed instance", "default instance", "the replica"), company
names, or people. A word that is only an instance name in some contexts (PROD,
REPORTS) counts only where the text uses it as an instance. When you cannot
tell whether a token is a server, leave it out and say why in `notes`.

Return one JSON object per ticket, in the order given, as JSONL inside a
single code block, and nothing else after the code block:

```
{"ticket_id": "123", "status": "done", "mentions": [{"text": "SQLPRD01\\REPORTS", "kind": "host_instance", "field": "description"}, {"text": "REPORTS", "kind": "bare_instance", "field": "subject", "host": "SQLPRD01"}], "notes": ""}
{"ticket_id": "124", "status": "done", "mentions": [{"text": "pg-prod-02.corp.local", "kind": "host", "field": "description"}], "notes": "PostgreSQL primary."}
{"ticket_id": "125", "status": "done", "mentions": [], "notes": "No servers mentioned."}
{"ticket_id": "126", "status": "skipped", "mentions": [], "notes": "Text is unreadable."}
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
- Every ticket must have exactly one line, with its `ticket_id` unchanged.
- In `notes`, name the engine when it is not SQL Server (MySQL, MariaDB,
  PostgreSQL, Oracle, MongoDB, RDS and so on), so the labels can be split by
  engine later.

Before answering, check each mention against the text once more: does it
appear exactly as written, and is it really a database server host or
instance?
