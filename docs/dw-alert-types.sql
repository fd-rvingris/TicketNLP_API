/*
  Every alert type in DW2.0, with counts.

  DW2.0 has no alert-type column; the type is in the ticket text. This query
  recognises each alert layout the way alert_parser.py does (docs/alert-parser.md)
  and reads the alert name from it:

  - collector_labeled, collector_markdown: the "Alert <name>" line of the first
    comment (the next line in the older vertical layout), else the subject
    segment after "Alert #<n> - ".
  - collector_table: the subject segment after "Alert #<n> - ".
  - grafana: the first "alertname = <name>" line.
  - backup_report, volume_capacity: the layout's fixed name.

  Tickets that match no layout (human tickets) are left out. Replies to alerts
  ("RE: ... Alert #") count only when their first comment is an alert body.
  The description is the first comment, as in dw-export-train.sql; only its
  first 4,000 characters are read.

  Output: one row per layout and alert type, with the number of alerts, clients,
  first and last date, and one example ticket id.
*/

DECLARE @YearsBack int = 3;
DECLARE @To date = CAST(GETDATE() AS date);
DECLARE @From date = DATEADD(year, -@YearsBack, @To);

WITH t AS (
    SELECT f.TicketID, f.Subject, f.Client_SK, f.CreatedDate_SK,
           -- One line ending (LF) so line searches work for CRLF and LF text; leading LF so the
           -- first line can be found like any other
           CHAR(10) + REPLACE(ISNULL(d.CommentText, ''), CHAR(13), CHAR(10)) AS body
    FROM dbo.FactTickets f
    OUTER APPLY (SELECT TOP (1) LEFT(tc.CommentText, 4000) AS CommentText
                 FROM dbo.FactTicketComments tc
                 WHERE tc.TicketID = f.TicketID
                 ORDER BY tc.CommentDate, tc.CommentID) d
    WHERE f.CreatedDate_SK >= @From
      AND f.CreatedDate_SK <  DATEADD(day, 1, @To)
      AND ISNULL(f.Merged, 0) = 0
),
layout AS (
    SELECT t.*,
           CASE
               WHEN t.body LIKE '%Client Name%Environment Name%Server%Alert Type%Date (UTC)%Message%'
                   THEN 'collector_table'
               WHEN t.Subject LIKE 'Backup Exception Report%' OR t.body LIKE '%DATABASENAME%BACKUP TYPE%'
                   THEN 'backup_report'
               WHEN t.Subject LIKE '[[]FIRING%' OR t.body LIKE '%' + CHAR(10) + 'alertname =%'
                   THEN 'grafana'
               WHEN t.Subject LIKE '%Volume Capacity%' OR t.body LIKE '%Resource:% - Source:%'
                   THEN 'volume_capacity'
               WHEN t.body LIKE '%* *AlertId*:%'
                   THEN 'collector_markdown'
               WHEN (t.Subject LIKE '%Alert #%' AND t.Subject NOT LIKE 'RE:%' AND t.Subject NOT LIKE 'FW%:%')
                    OR t.body LIKE '%' + CHAR(10) + 'AlertId %'
                   THEN 'collector_labeled'
           END AS layout
    FROM t
),
named AS (
    SELECT l.*,
           CASE l.layout
               WHEN 'backup_report' THEN 'Backup Exception Report'
               WHEN 'volume_capacity' THEN 'Volume Capacity'
               WHEN 'grafana' THEN gf.name
               WHEN 'collector_markdown' THEN md.name
               WHEN 'collector_labeled' THEN COALESCE(NULLIF(al.name, ''), NULLIF(av.name, ''), sj.name)
               WHEN 'collector_table' THEN sj.name
           END AS alert_type
    FROM layout l
    -- "Alert <name>" line ("Alert Collection errors" gives "Collection errors")
    CROSS APPLY (SELECT CHARINDEX(CHAR(10) + 'Alert ', l.body) AS p) pa
    CROSS APPLY (SELECT CASE WHEN pa.p > 0 THEN LTRIM(RTRIM(SUBSTRING(l.body, pa.p + 7,
                     CHARINDEX(CHAR(10), l.body + CHAR(10), pa.p + 7) - (pa.p + 7)))) END AS name) al
    -- Older vertical layout: "Alert" alone on a line, the name on the next line
    CROSS APPLY (SELECT CHARINDEX(CHAR(10) + 'Alert' + CHAR(10), l.body) AS p) pv
    CROSS APPLY (SELECT CASE WHEN pv.p > 0 THEN LTRIM(RTRIM(SUBSTRING(l.body, pv.p + 7,
                     CHARINDEX(CHAR(10), l.body + CHAR(10), pv.p + 7) - (pv.p + 7)))) END AS name) av
    -- Grafana: the first "alertname = <name>" line
    CROSS APPLY (SELECT CHARINDEX(CHAR(10) + 'alertname =', l.body) AS p) pg
    CROSS APPLY (SELECT CASE WHEN pg.p > 0 THEN LTRIM(RTRIM(SUBSTRING(l.body, pg.p + 12,
                     CHARINDEX(CHAR(10), l.body + CHAR(10), pg.p + 12) - (pg.p + 12)))) END AS name) gf
    -- Markdown: the value between "*Alert*:" and the next " * *"
    CROSS APPLY (SELECT CHARINDEX('*Alert*:', l.body) AS p) pm
    CROSS APPLY (SELECT CASE WHEN pm.p > 0 THEN LTRIM(RTRIM(SUBSTRING(l.body, pm.p + 8,
                     CHARINDEX(' * *', l.body + ' * *', pm.p + 8) - (pm.p + 8)))) END AS name) md
    -- Subject: the segment after "Alert #<digits> - ", up to the next " - "
    CROSS APPLY (SELECT CASE WHEN CHARINDEX('Alert #', l.Subject) > 0
                     THEN SUBSTRING(l.Subject, CHARINDEX('Alert #', l.Subject) + 7, 400) END AS after_hash) s1
    CROSS APPLY (SELECT LTRIM(STUFF(s1.after_hash, 1, PATINDEX('%[^0-9]%', s1.after_hash + 'x') - 1, '')) AS r) s2
    CROSS APPLY (SELECT CASE WHEN s2.r LIKE '-%' THEN LTRIM(SUBSTRING(s2.r, 2, 400)) ELSE s2.r END AS r) s3
    CROSS APPLY (SELECT LEFT(s3.r, CHARINDEX(' - ', s3.r + ' - ') - 1) AS name) sj
    WHERE l.layout IS NOT NULL
)
SELECT layout,
       ISNULL(NULLIF(LEFT(alert_type, 200), ''), '(not found)') AS alert_type,
       COUNT(*)                                                AS alerts,
       COUNT(DISTINCT Client_SK)                               AS clients,
       MIN(CreatedDate_SK)                                     AS first_seen,
       MAX(CreatedDate_SK)                                     AS last_seen,
       MIN(TicketID)                                           AS example_ticket_id
FROM named
GROUP BY layout, ISNULL(NULLIF(LEFT(alert_type, 200), ''), '(not found)')
ORDER BY layout, alerts DESC;
