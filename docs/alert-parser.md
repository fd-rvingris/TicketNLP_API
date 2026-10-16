# Alert Parser

How [alert_parser.py](../alert_parser.py) turns an alert ticket into structured
fields. It runs inside `make_ticket` in common.py, so every ticket loaded by
`load_export.py` (or fetched by `fetch_tickets.py`) gets an `alert_fields` value.
The fields are for review, filtering and analysis; they are never sent to the
model.

## Overview

```mermaid
flowchart TD
    A([Ticket loaded: subject and description]) --> B{"Subject contains<br/>'Alert #'?"}
    B -- yes --> C[Family: collector<br/>use collector labels]
    B -- no --> D{"Subject starts with<br/>'[FIRING'?"}
    D -- yes --> E[Family: firing<br/>use Grafana labels]
    D -- no --> F{"Subject starts with<br/>'Backup Exception Report'<br/>or 'Volume Capacity'?"}
    F -- yes --> G[/"alert_fields = family only<br/>(no labeled lines to read)"/]
    F -- no --> H[/alert_fields = null<br/>not a known alert format/]
    C --> P[[Read labeled lines<br/>see below]]
    E --> P
    P --> R[/"alert_fields = family + parsed fields<br/>e.g. alert_name, severity, server, platform"/]
```

## Reading labeled lines

Each line is trimmed and checked against the family's labels, longest first, so
`Alert Collection` is not read as `Alert` and `PreviousLevel` is not read as
`Level`. Collector lines are `Label value`; Grafana lines are `label value` or
`label = value`.

```mermaid
flowchart TD
    S([Next line of the description]) --> E1{Blank?}
    E1 -- yes --> S
    E1 -- no --> L{Starts with a<br/>known label?}
    L -- no --> CT{"Inside a free-text field?<br/>(message, description,<br/>recommendations, summary)"}
    CT -- yes --> AP[Append the line<br/>to that field] --> S
    CT -- no --> IG[Ignore the line] --> S
    L -- yes --> V{Value after<br/>the label?}
    V -- no --> NX{"Is the next non-blank<br/>line unlabeled?"}
    NX -- yes --> TK["Take it as the value<br/>(older vertical layout)"] --> DUP
    NX -- no --> EMPTY[Field is empty] --> DUP
    V -- yes --> DUP{Field already<br/>has a value?}
    DUP -- yes --> KEEP[Keep the first value<br/>stop any free-text continuation] --> S
    DUP -- no --> SET{Value empty?}
    SET -- yes --> S
    SET -- no --> STORE[Store the field] --> FT{Free-text field?}
    FT -- yes --> MARK[Following unlabeled lines<br/>continue this field] --> S
    FT -- no --> S
```

## Labels and the fields they fill

| Family | Label in the text | Field |
| --- | --- | --- |
| collector | AlertId | alert_id |
| collector | Alert, Alert Collection | alert_name |
| collector | Client Name | client_name |
| collector | Server, Platform, Instance | server, platform, instance |
| collector | Collector Machine, Alias, Keyword | collector, alias, keyword |
| collector | Severity, Level, PreviousLevel / Previous Level | severity, level, previous_level |
| collector | Open Date, OK Collections, Failures | open_date, ok_collections, failures |
| collector | Message, Description, Recommendations | message, description, recommendations (free text) |
| firing | alertname, severity, instance, job, env | alert_name, severity, instance, job, env |
| firing | dimension_DBInstanceIdentifier | db_instance |
| firing | summary, description | summary, description (free text) |

## Collector table layout

Some collector alerts ("Collection errors", "No recent monitoring runs") put
their values in a table instead of labeled lines. They are recognised by the
header line, whatever the subject, so the layout is also parsed when the text is
pasted without its alert subject:

```
Client Name Environment Name Server Alert Type Open Date (UTC) Message
FAKECO - Zeta (123) ZetaAzureEast (Q9XAB12P) Q9ZZPAP07K Collection errors 2025-01-14 15:30:46 UTC ServerName: Q9ZZPAP07K (SQL Server)
Host(s): Q9ZZPAP07K
Database:
Error Type: SQL Server is unresponsive
First Occurrence: 2025-01-14 15:21:00
Last Occurrence: 2025-01-14 15:26:00
Possible Solutions: Please make sure the server is up and running
```

| Part | Field |
| --- | --- |
| Client name and the number in brackets | client_name, client_id |
| Environment name and the code in brackets | environment, environment_code |
| Server column | server (from `ServerName:` in the message when the column is empty) |
| Alert type | alert_name |
| Open date, message | open_date, message |
| Brackets after `ServerName:` | platform (SQL Server, Windows Machine) |
| A "Label: value" message, and every "Label: value" line after the row | the label in snake case: `Host(s):` gives hosts, `Error Type:` error_type, `Minutes Delay:` minutes_delay, `Threshold:` threshold |

Labels with no value (such as an empty `Database:`) are left out. These fields
also carry `layout: table`. `table_spans()` returns where each value sits in the
description, and the detector UI highlights them (client, environment, alert
type, date and other fields in separate colours) and lists them in an "Alert
fields" panel. The server detector treats the server
and every name in `hosts` as the alert's own servers: they label training
examples, fill the known-names directory, and fire the `host:alert_field` rule
wherever they appear in the text.

The ticket files keep the fields parsed when they were built. After a parser
change, refresh them with `python alert_parser.py data/train_tickets.jsonl
data/test_tickets.jsonl` (rewrites `alert_fields` in place).

In the 12,000-ticket sample, 66 training alerts use this layout (none in the
test file); 62 have a server.

## Collection-error alerts in the labeled layout

"Collection errors" alerts in the labeled layout leave `Server` and `Platform`
empty and flatten the details into the message ("ServerName: HOST (SQL Server)
Host(s): HOST Database: ..."). The server, platform and hosts are taken from the
message when the labeled fields are empty. The line "Alert Collection errors"
is the alert "Collection errors", not the label "Alert Collection".

## Backup Exception Report

Recognised by the subject ("Backup Exception Report from server HOST") or by the
`DATABASENAME BACKUP TYPE ...` header line.

| Part | Field |
| --- | --- |
| Subject "from server HOST" | server |
| Heading with "SERVER" ("PRODUCTION SERVER: PLEASE INVESTIGATE.") | environment_note |
| Table rows: database, type, latest backup date, hours, minutes | databases and backup_types (comma-separated, no repeats), backup_rows, max_hours_without |
| Availability Group variant's "LATEST BACKUP SERVER NAME" column | backup_servers |
| Parameter lines (@HoursForFull, @MinutesForDiff, @MinutesForLog, @DeviceType, Report Creation Time) | hoursforfull, minutesfordiff, minutesforlog, devicetype, report_creation_time |

Rows are read from the date outwards, so database names may contain spaces.
`backup_spans()` gives the position of every value, per row, for highlighting.

## Server coverage after these layouts (12,000-ticket sample, 6,000 alerts)

| Family | Alerts | With a server before | With a server now |
| --- | --- | --- | --- |
| collector, labeled layout | 5,295 | 58% | 99% |
| backup_exception_report | 412 | 0% | 100% |
| firing | 203 | 100% | 100% |
| collector, table layout | 66 | 0% | 94% |
| volume_capacity | 24 | 0% | 0% |
| All alerts | 6,000 | 56% | 98% |

## Coverage on the DW2.0 exports (12,000 tickets, 6,000 alerts)

| Family | Alerts | Most-filled fields |
| --- | --- | --- |
| collector | 5,361 | alert_name 98%, severity 91%, instance 76%, server and platform 57% |
| firing | 203 | alert_name, severity, instance, job, summary 100% |
| backup_exception_report | 412 | family only |
| volume_capacity | 24 | family only |

No human ticket matched an alert format.
