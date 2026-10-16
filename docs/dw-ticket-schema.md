# DW2.0 Ticket Schema

The DW2.0 tables that hold Zendesk tickets, their comments, categories, servers and
clients, as exported from `FDReportingDev` on 2026-10-16. The export contained no
foreign keys, so the relationships below are inferred from column names. Confirm
them with the DW2.0 maintainers before relying on them.

Rendered version: [DW2.0 Ticket Schema artifact](https://claude.ai/artifact/Y7Q3tTUPZ8sVboe475m7Z2?sk=y4wzU1OFXidkl0AifJkxHg)

Legend: `PK` primary or surrogate key, `FK` inferred reference. Stereotypes give
each table's role: fact, dimension, bridge or control.

```mermaid
classDiagram
  direction LR

  class FactTickets {
    <<fact>>
    PK int FactTickets_SK
    bigint TicketID
    FK bigint ParentTicketID
    FK int Client_SK
    FK int TicketAttributes_SK
    FK int Assignee_SK
    FK int Submitter_SK
    FK int Requester_SK
    date CreatedDate_SK
    int CreatedTime_SK
    date FirstReplyDate_SK
    int FirstReplyTime_SK
    date CloseDate_SK
    int CloseTime_SK
    varchar Subject
    varchar URL
    varchar Category
    varchar Platform
    varchar Channel
    varchar Team
    varchar ResolutionCode
    varchar ResolutionNotes
    varchar PartnerTicketNumber
    varchar PartnerParentTicket
    int CalendarReplyTimeMinutes
    int CalendarFirstResolutionTimeMinutes
    int CalendarFullResolutionTimeMinutes
    int BusinessReplyTimeMinutes
    int BusinessFirstResolutionTimeMinutes
    int BusinessFullResolutionTimeMinutes
    tinyint Closed
    tinyint Merged
    tinyint CSIncident
    tinyint Reopens
    tinyint Replies
    tinyint CreatedBusinessHours
    bit SurveyOffered
    varchar SourceSystem
    varchar SourceID
    datetime DWModifiedDate
  }

  class FactTicketComments {
    <<fact>>
    PK int FactTicketComments_SK
    FK int TicketID
    bigint CommentID
    FK int Contact_SK
    datetime CommentDate
    bit IsPublic
    nvarchar_max CommentText
    varchar SourceSystem
    varchar SourceID
    datetime DWModifiedDate
  }

  class FactTicketStatus {
    <<fact>>
    FK int FactTickets_SK
    varchar status
    FK int Assignee_SK
    datetime starttime
    datetime endtime
    varchar SourceSystem
    varchar SourceID
    datetime DWModifiedDate
  }

  class DimClient {
    <<dimension>>
    PK int Client_SK
    varchar Name
    varchar Domain
    varchar Industry
    varchar ClientStatus
    varchar Pod
    varchar PrimaryDBA
    varchar SAM
    varchar PartnerParent
    varchar SlackChannel
    varchar HS_CSM_Sentiment
    money AnnualRevenue
    varchar Address1
    varchar Address2
    varchar City
    varchar State
    varchar Zip
    varchar Country
    varchar Phone
    varchar Website
    varchar Timezone
    time BusinessStartTimeUTC
    time BusinessEndTimeUTC
    varchar SourceSystem
    varchar SourceID
    varchar SourceName
    bit SourceActive
    datetime DWValidFrom
    datetime DWValidTo
    bit DWCurrentRecord
  }

  class DimTicketAttributes {
    <<dimension>>
    PK int TicketAttributes_SK
    varchar TicketType
    varchar ServiceOffering
    varchar Subtype
    varchar Priority
    varchar Severity
    varchar Complexity
    varchar Impact
    tinyint PrioritySort
    tinyint SeveritySort
    tinyint ImpactSort
  }

  class TicketCategory {
    <<bridge>>
    FK int FactTickets_SK
    FK int Category_SK
    decimal HoursWeight
  }

  class DimTicketCategory {
    <<dimension>>
    PK int Category_SK
    varchar CategoryValue
    varchar CategoryName
    tinyint Level
  }

  class TicketServer {
    <<bridge>>
    FK int FactTickets_SK
    FK int TicketServer_SK
    decimal HoursWeight
  }

  class DimTicketServer {
    <<dimension>>
    PK int TicketServer_SK
    varchar ServerName
  }

  class ClientMapping {
    <<control>>
    PK int Mapping_SK
    varchar MasterName
    varchar SourceSystem
    varchar SourceID
    varchar SourceName
    bit ReviewedFlag
    datetime DWCreatedDate
    datetime DWModifiedDate
  }

  class TicketTypeCategoryMapping {
    <<control>>
    bigint TicketTypeId
    varchar TicketTypeName
    varchar TicketTypeValue
    bigint CategoryFieldId
  }

  DimClient "1" -- "*" FactTickets : Client_SK
  DimTicketAttributes "1" -- "*" FactTickets : TicketAttributes_SK
  FactTickets "1" -- "*" FactTicketComments : TicketID
  FactTickets "1" -- "*" FactTicketStatus : FactTickets_SK
  FactTickets "0..1" -- "*" FactTickets : ParentTicketID
  FactTickets "1" -- "*" TicketCategory : FactTickets_SK
  DimTicketCategory "1" -- "*" TicketCategory : Category_SK
  FactTickets "1" -- "*" TicketServer : FactTickets_SK
  DimTicketServer "1" -- "*" TicketServer : TicketServer_SK
  ClientMapping ..> DimClient : SourceSystem + SourceID
  TicketTypeCategoryMapping ..> DimTicketAttributes : TicketType values
```

`ClientMapping` and `TicketTypeCategoryMapping` are in the `control` schema; all
other tables are in `dbo`.

## Reading the relationships

- Solid lines are joins implied by matching key names. Dotted lines are looser
  lookups through source system values, not keys.
- `FactTicketComments` joins on `TicketID`, not the surrogate key, and stores it
  as `int` where `FactTickets` uses `bigint`.
- `DimClient` keeps history (`DWValidFrom`, `DWValidTo`, `DWCurrentRecord`), so a
  ticket points at the client record that was current when the ticket loaded.
- `TicketCategory` and `TicketServer` carry `HoursWeight`, which suggests they
  come from time entries rather than from the ticket itself.
- Assignee, submitter, requester, contact and date keys point to dimensions that
  were not in this export, so they are not drawn.

## What the classifier export uses

- `FactTickets`: `TicketID`, `Subject`, `CreatedDate_SK`, `Category`, and `Merged`
  as a filter.
- `FactTicketComments`: the earliest `CommentText` per ticket, used as the
  description (in Zendesk the description is the first comment).
- `DimClient`: `Name`, plus `SourceID` when `SourceSystem` is Zendesk (the Zendesk
  organization id).
- `DimTicketAttributes`: `TicketType` and `Subtype` as weak labels.
- `DimTicketCategory` through `TicketCategory`: category names as weak labels.
- `DimTicketServer` through `TicketServer`: server names, kept as `dw_servers`, a
  candidate check on the model's server mentions.
