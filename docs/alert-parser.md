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

## Coverage on the DW2.0 exports (12,000 tickets, 6,000 alerts)

| Family | Alerts | Most-filled fields |
| --- | --- | --- |
| collector | 5,361 | alert_name 98%, severity 91%, instance 76%, server and platform 57% |
| firing | 203 | alert_name, severity, instance, job, summary 100% |
| backup_exception_report | 412 | family only |
| volume_capacity | 24 | family only |

No human ticket matched an alert format.
