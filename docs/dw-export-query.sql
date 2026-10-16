/*
  Ticket sample export from DW2.0 for the ticket classifier PoC.

  Returns 100 tickets from the last 90 days: 30 automated monitoring alerts and
  70 tickets written by people, sampled separately so alerts do not dominate the
  accuracy figures. Column names match what load_export.py reads. See
  docs/dw-ticket-schema.md for the tables.

  How to run and save (SSMS on the pool server):
  1. Tools > Options > Query Results > SQL Server > Results to Grid: turn on
     "Quote strings containing list separators when saving .csv results" and
     "Retain CR/LF on copy or save", and make sure "Discard results after
     execution" is off. Open a new query window afterward.
  2. Results to Grid (Ctrl+D), run (F5), right-click the grid, Save Results As, CSV.
  3. The file contains client ticket text. Keep it in the PoC's data\ folder,
     which git ignores, then run: python load_export.py data\<file>.csv

  Before relying on the results:
  - Run against production DW2.0, not FDReportingDev, unless the maintainers
    confirm the dev copy is current.
  - Check the alert patterns and the excluded categories against the current
    DimTicketCategory values; both lists come from a first look at the data.
  - organization_id assumes DimClient.SourceID is the Zendesk organization id
    when SourceSystem = 'Zendesk'.
*/

DECLARE @Days int = 90;
DECLARE @Alerts int = 30;
DECLARE @Human int = 70;

WITH base AS (
    SELECT f.FactTickets_SK,
           CASE WHEN f.Subject LIKE '%Alert #%'
                  OR f.Subject LIKE '[[]FIRING%'
                  OR f.Subject LIKE 'Volume Capacity%'
                  OR f.Subject LIKE 'Backup Exception Report%'
                THEN 1 ELSE 0 END AS is_alert
    FROM dbo.FactTickets f
    WHERE f.CreatedDate_SK >= DATEADD(day, -@Days, CAST(GETDATE() AS date))
      AND ISNULL(f.Merged, 0) = 0
      -- Internal time-tracking, meeting and training tickets, not client requests
      AND ISNULL(f.Category, '') NOT IN ('Meeting', 'Information Request', 'Client Required Training')
      -- The description is the first comment; skip tickets without one
      AND EXISTS (SELECT 1 FROM dbo.FactTicketComments tc
                  WHERE tc.TicketID = f.TicketID AND LEN(tc.CommentText) > 0)
),
sample AS (
    SELECT * FROM (SELECT TOP (@Alerts) FactTickets_SK, is_alert FROM base WHERE is_alert = 1 ORDER BY NEWID()) a
    UNION ALL
    SELECT * FROM (SELECT TOP (@Human) FactTickets_SK, is_alert FROM base WHERE is_alert = 0 ORDER BY NEWID()) h
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
