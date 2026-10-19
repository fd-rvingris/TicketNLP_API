<#
.SYNOPSIS
Labels every ticket that has no Claude server-name label yet, through the Claude Code CLI.

.DESCRIPTION
Runs tools\claude_relabel.ps1 -Set rest: all tickets of data\train_tickets.jsonl and
data\test_tickets.jsonl except the 400 in the relabel sets (about 11,600 tickets, some 460 batches of
25), with the same prompt, checks, retries and resume. The labels go to new files:
data\labels\claude_spans_train_rest.jsonl (training data) and claude_spans_test_rest.jsonl (scored
against the saved model when done). The run stops after three failed batches in a row, as when a
usage limit is reached; run it again later to resume. Run the 400-ticket relabel first, so its
tickets are left out.

.EXAMPLE
.\tools\claude_label_all.ps1 -Limit 20
Labels the next 20 batches.

.EXAMPLE
.\tools\claude_label_all.ps1
Labels every remaining batch, then imports each finished set.
#>
[CmdletBinding()]
param(
    [ValidateSet("rest", "train-rest", "test-rest")] [string]$Set = "rest",
    [string]$Model = "claude-opus-5-5",
    [ValidateSet("low", "medium", "high", "xhigh", "max")] [string]$Effort = "high",
    [int]$MaxAttempts = 3,
    [int]$Limit = 0,
    [switch]$Fresh,
    [string]$Claude = "claude"
)

& (Join-Path $PSScriptRoot "claude_relabel.ps1") -Set $Set -Model $Model -Effort $Effort -MaxAttempts $MaxAttempts `
    -Limit $Limit -Fresh:$Fresh -Claude $Claude
