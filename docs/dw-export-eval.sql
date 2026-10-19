/*
  EVALUATION ticket export from DW2.0: tickets never pulled before, for testing
  the taxonomy (ticket_analysis/ticket_types.py) and the server detector on unseen data.

  Returns up to @Alerts automated monitoring alerts and up to @Human tickets
  written by people, created from @From (the first day after the training
  window) up to and including today. It leaves out the tickets
  dw-export-test.sql returned, by recomputing that query's seeded sample, and
  dw-export-train.sql only took tickets before @From. So no ticket in this
  export is in train_tickets.jsonl or test_tickets.jsonl.

  If DW2.0 rows changed since the first exports (a ticket merged, a comment
  added), the recomputed test sample can differ slightly, so load the file with
  --exclude as below: it drops any ticket already in the earlier files and says
  how many it dropped.

  The sample is fixed by @Seed and @Today: running the query again returns the
  same tickets. Set @Today to a fixed date after the first run so a rerun
  matches; change @Seed for a different sample. If fewer tickets match than
  requested, the query returns all that match; check the row count in the
  Messages tab.

  Column names match what tools/load_export.py reads, and are the same as in
  dw-export-test.sql. See docs/dw-ticket-schema.md.

  How to run and save (SSMS on the pool server):
  1. Tools > Options > Query Results > SQL Server > Results to Grid: turn on
     "Quote strings containing list separators when saving .csv results" and
     "Retain CR/LF on copy or save", and make sure "Discard results after
     execution" is off. Open a new query window afterward.
  2. Results to Grid (Ctrl+D), run (F5), right-click the grid, Save Results As,
     CSV, named eval_tickets.csv.
  3. The file contains client ticket text. Keep it in the PoC's data\ folder,
     which git ignores, then run:
       python -m tools.load_export data\eval_tickets.csv --output data\eval_tickets.jsonl --exclude data\train_tickets.jsonl data\test_tickets.jsonl
       python -m tools.alert_parser data\eval_tickets.jsonl

  Before relying on the results, the notes in dw-export-test.sql apply: run
  against production DW2.0, and organization_id assumes DimClient.SourceID is
  the Zendesk organization id when SourceSystem = 'Zendesk'.
*/

DECLARE @Alerts int = 500;
DECLARE @Human int = 500;
DECLARE @Seed varchar(20) = 'eval-1';

-- The window: from the day after the training window to today. Replace
-- CAST(GETDATE() AS date) with the first run's date to make a rerun repeat it.
DECLARE @Today date = CAST(GETDATE() AS date);
DECLARE @From date = '2026-07-18';
DECLARE @To date = DATEADD(day, 1, @Today);

-- The earlier test export, exactly as dw-export-test.sql chose it, to leave out
DECLARE @TestSeed varchar(20) = 'test-1';
DECLARE @TestAlerts int = 1000;
DECLARE @TestHuman int = 1000;
DECLARE @TestFrom date = '2026-07-18';
DECLARE @TestTo date = '2026-10-17';

WITH eligible AS (
    -- The filters dw-export-test.sql and dw-export-train.sql use
    SELECT f.FactTickets_SK, f.TicketID, f.CreatedDate_SK,
           CASE WHEN f.Subject LIKE '%Alert #%'
                  OR f.Subject LIKE '[[]FIRING%'
                  OR f.Subject LIKE 'Volume Capacity%'
                  OR f.Subject LIKE 'Backup Exception Report%'
                THEN 1 ELSE 0 END AS is_alert
    FROM dbo.FactTickets f
    WHERE f.CreatedDate_SK >= @From
      AND f.CreatedDate_SK <  @To
      AND ISNULL(f.Merged, 0) = 0
      -- Internal time-tracking, meeting and training tickets, not client requests
      AND ISNULL(f.Category, '') NOT IN ('Meeting', 'Information Request', 'Client Required Training')
      -- The description is the first comment; skip tickets without one
      AND EXISTS (SELECT 1 FROM dbo.FactTicketComments tc
                  WHERE tc.TicketID = f.TicketID AND LEN(tc.CommentText) > 0)
),
earlier AS (
    -- dw-export-test.sql's sample, recomputed with its seed, window and sizes
    SELECT FactTickets_SK FROM (
        SELECT TOP (@TestAlerts) FactTickets_SK FROM eligible
        WHERE is_alert = 1 AND CreatedDate_SK >= @TestFrom AND CreatedDate_SK < @TestTo
        ORDER BY HASHBYTES('SHA2_256', @TestSeed + CAST(TicketID AS varchar(20)))) a
    UNION ALL
    SELECT FactTickets_SK FROM (
        SELECT TOP (@TestHuman) FactTickets_SK FROM eligible
        WHERE is_alert = 0 AND CreatedDate_SK >= @TestFrom AND CreatedDate_SK < @TestTo
        ORDER BY HASHBYTES('SHA2_256', @TestSeed + CAST(TicketID AS varchar(20)))) h
),
base AS (
    SELECT e.FactTickets_SK, e.is_alert,
           HASHBYTES('SHA2_256', @Seed + CAST(e.TicketID AS varchar(20))) AS sort_key
    FROM eligible e
    WHERE e.FactTickets_SK NOT IN (SELECT FactTickets_SK FROM earlier)
),
sample AS (
    SELECT * FROM (SELECT TOP (@Alerts) FactTickets_SK, is_alert FROM base WHERE is_alert = 1 ORDER BY sort_key) a
    UNION ALL
    SELECT * FROM (SELECT TOP (@Human) FactTickets_SK, is_alert FROM base WHERE is_alert = 0 ORDER BY sort_key) h
)
SELECT
    f.TicketID                                         AS ticket_id,
    ISNULL(f.Subject, '')                              AS subject,
    ISNULL(d.CommentText, '')                          AS description,
    ISNULL(CASE WHEN c.SourceSystem = 'Zendesk' THEN c.SourceID END, '') AS organization_id,
    ISNULL(c.Name, '')                                 AS organization_name,
    CONVERT(varchar(10), f.CreatedDate_SK, 23)         AS created_at,
    ISNULL(a.TicketType, '')                           AS [type],
    ISNULL(a.Subtype, '')                              AS [sub type],
    ISNULL(f.Category, '')                             AS [category],
    ISNULL(cat.Names, '')                              AS [categories],
    ISNULL(srv.Names, '')                              AS dw_servers,
    s.is_alert                                         AS is_alert,
    ISNULL(CAST(f.Assignee_SK AS varchar(20)), '')     AS assignee_key,
    ISNULL(sa.Keys, '')                                AS status_assignee_keys,
    ISNULL(f.Team, '')                                 AS team,
    ISNULL(c.Pod, '')                                  AS client_pod,
    ISNULL(c.PrimaryDBA, '')                           AS client_primary_dba,
    ISNULL(f.Platform, '')                             AS dw_platform,
    ISNULL(a.Severity, '')                             AS dw_severity,
    ISNULL(a.Priority, '')                             AS dw_priority,
    ISNULL(a.Impact, '')                               AS dw_impact,
    ISNULL(a.Complexity, '')                           AS dw_complexity
FROM sample s
JOIN dbo.FactTickets f              ON f.FactTickets_SK = s.FactTickets_SK
LEFT JOIN dbo.DimClient c           ON c.Client_SK = f.Client_SK
LEFT JOIN dbo.DimTicketAttributes a ON a.TicketAttributes_SK = f.TicketAttributes_SK
OUTER APPLY (SELECT TOP (1) tc.CommentText FROM dbo.FactTicketComments tc
             WHERE tc.TicketID = f.TicketID ORDER BY tc.CommentDate, tc.CommentID) d
OUTER APPLY (SELECT STRING_AGG(NULLIF(LTRIM(dc.CategoryName), ''), '; ') AS Names
             FROM dbo.TicketCategory tcat JOIN dbo.DimTicketCategory dc ON dc.Category_SK = tcat.Category_SK
             WHERE tcat.FactTickets_SK = f.FactTickets_SK) cat
OUTER APPLY (SELECT STRING_AGG(NULLIF(LTRIM(ds.ServerName), ''), '; ') AS Names
             FROM dbo.TicketServer ts JOIN dbo.DimTicketServer ds ON ds.TicketServer_SK = ts.TicketServer_SK
             WHERE ts.FactTickets_SK = f.FactTickets_SK) srv
OUTER APPLY (SELECT STRING_AGG(CAST(x.Assignee_SK AS varchar(20)), '; ') WITHIN GROUP (ORDER BY x.Assignee_SK) AS Keys
             FROM (SELECT DISTINCT st.Assignee_SK FROM dbo.FactTicketStatus st
                   WHERE st.FactTickets_SK = f.FactTickets_SK AND st.Assignee_SK IS NOT NULL) x) sa
ORDER BY f.TicketID;
