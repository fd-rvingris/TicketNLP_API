/*
  TEST ticket export from DW2.0.

  Returns up to @Alerts automated monitoring alerts and up to @Human tickets
  written by people, created in the last @TestDays days. dw-export-train.sql
  takes only older tickets, so the two sets never share a ticket and the model
  is measured on tickets newer than any it learned from. Keep @TestDays the same
  in both files.

  For a small sample to label by hand, set @Alerts = 30 and @Human = 70.

  The sample is fixed by @Seed: running the query again returns the same tickets.
  Change @Seed for a different sample. If fewer tickets match than requested,
  the query returns all that match; check the row count in the Messages tab.

  Column names match what load_export.py reads. See docs/dw-ticket-schema.md.

  How to run and save (SSMS on the pool server):
  1. Tools > Options > Query Results > SQL Server > Results to Grid: turn on
     "Quote strings containing list separators when saving .csv results" and
     "Retain CR/LF on copy or save", and make sure "Discard results after
     execution" is off. Open a new query window afterward.
  2. Results to Grid (Ctrl+D), run (F5), right-click the grid, Save Results As,
     CSV, named test_tickets.csv.
  3. The file contains client ticket text. Keep it in the PoC's data\ folder,
     which git ignores, then run:
       python load_export.py data\test_tickets.csv --output data\test_tickets.jsonl

  Before relying on the results:
  - Run against production DW2.0, not FDReportingDev, unless the maintainers
    confirm the dev copy is current.
  - The alert subject patterns and the excluded categories come from a first look
    at the data; check them against current values.
  - organization_id assumes DimClient.SourceID is the Zendesk organization id
    when SourceSystem = 'Zendesk'.
*/

DECLARE @TestDays int = 90;        -- must match dw-export-train.sql
DECLARE @Alerts int = 1000;
DECLARE @Human int = 1000;
DECLARE @Seed varchar(20) = 'test-1';

DECLARE @Today date = CAST(GETDATE() AS date);
DECLARE @From date = DATEADD(day, -@TestDays, @Today);
DECLARE @To date = DATEADD(day, 1, @Today);

WITH base AS (
    SELECT f.FactTickets_SK,
           HASHBYTES('SHA2_256', @Seed + CAST(f.TicketID AS varchar(20))) AS sort_key,
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
    s.is_alert                                         AS is_alert
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
ORDER BY f.TicketID;
