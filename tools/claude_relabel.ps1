<#
.SYNOPSIS
Relabels Claude's server-name label sets through the Claude Code CLI, one fresh session per batch.

.DESCRIPTION
For each label set, writes labeling batches for the same tickets
(python -m tools.claude_pack labels --from-labels), sends each batch to `claude -p` with
prompts\claude-labeling-prompt.md as the system prompt, checks the reply against its batch
(claude_pack check-reply), and retries a reply that is unusable. Every call is a new session with no
tools, MCP servers, settings or saved history, so nothing carries over from one batch to the next.

Finished batches are kept, so running the script again resumes where it stopped. When every batch of
a set is done, the set's label file is backed up once (<name>_sqlserver_only.jsonl), replaced by the
new labels (claude_pack import-labels), and the saved model is scored against them
(server_labels evaluate).

Ticket text is masked as it is for Claude Desktop, and is sent to Claude. Batches and replies stay
under data\claude_pack\relabel, which is gitignored. The script prints counts only.

.EXAMPLE
.\tools\claude_relabel.ps1 -Limit 1
Labels one batch, to try it.

.EXAMPLE
.\tools\claude_relabel.ps1
Labels every remaining batch of both sets, then imports and scores each set.

.EXAMPLE
.\tools\claude_relabel.ps1 -Set holdout -Fresh
Starts the holdout set again from new batches, discarding its earlier replies.

.EXAMPLE
.\tools\claude_relabel.ps1 -Set rest
Labels every ticket no set covers yet into new label files (tools\claude_label_all.ps1 runs this).
#>
[CmdletBinding()]
param(
    # Which label set: the test file sample, the training holdout, or both ("all"); or the tickets no
    # set covers yet, in the training file, the test file, or both ("rest")
    [ValidateSet("all", "test", "holdout", "rest", "train-rest", "test-rest")] [string]$Set = "all",
    [string]$Model = "claude-opus-5-5",
    [ValidateSet("low", "medium", "high", "xhigh", "max")] [string]$Effort = "high",
    # Calls per batch before giving up on it for this run
    [int]$MaxAttempts = 3,
    # Batches to send this run; 0 sends all that remain
    [int]$Limit = 0,
    # Write new batches and discard earlier replies for the chosen sets
    [switch]$Fresh,
    [string]$Claude = "claude"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Prompt = Join-Path $Root "prompts\claude-labeling-prompt.md"
$Data = Join-Path $Root "data"
# Relabel sets label the tickets of an existing label file again; rest sets label every other ticket of
# a ticket file into a new label file. Only test-file sets are scored: the rest of the training file is
# training data.
$Sets = [ordered]@{
    "test"       = @{ Labels = "labels\claude_spans.jsonl"; Tickets = "test_tickets.jsonl"; Score = $true }
    "holdout"    = @{ Labels = "labels\claude_spans_holdout.jsonl"; Tickets = "train_tickets.jsonl"; Score = $true }
    "train-rest" = @{ Labels = "labels\claude_spans_train_rest.jsonl"; Tickets = "train_tickets.jsonl"; AllOf = "train" }
    "test-rest"  = @{ Labels = "labels\claude_spans_test_rest.jsonl"; Tickets = "test_tickets.jsonl"; AllOf = "test"; Score = $true }
}
$Groups = @{ "all" = @("test", "holdout"); "rest" = @("train-rest", "test-rest") }
# The relabel sets' files and their backups, whose tickets the rest sets leave out
$Labeled = @("claude_spans", "claude_spans_holdout") | ForEach-Object {
    Join-Path $Data "labels\$_.jsonl"; Join-Path $Data "labels\${_}_sqlserver_only.jsonl" }

# Windows PowerShell 5.1 drops an empty argument to a native program; '""' reaches it as an empty string
if ($PSVersionTable.PSVersion -ge [version]"7.3") { $Empty = "" } else { $Empty = '""' }
# Text to and from claude and python is UTF-8
$Utf8 = New-Object System.Text.UTF8Encoding $false
$OutputEncoding = $Utf8
[Console]::OutputEncoding = $Utf8
$env:PYTHONIOENCODING = "utf-8"

function Invoke-Python {
    & $Python -m @args
    if ($LASTEXITCODE -ne 0) { throw "python -m $($args -join ' ') failed with exit code $LASTEXITCODE" }
}

if (-not (Test-Path $Python)) { throw "No virtual environment at $Python" }
if (-not (Get-Command $Claude -ErrorAction SilentlyContinue)) { throw "The Claude Code CLI ($Claude) is not on PATH" }

Push-Location $Root
try {
    if ($Groups.ContainsKey($Set)) { $names = $Groups[$Set] } else { $names = @($Set) }
    $sent = 0
    $failed = 0  # batches in a row without a usable reply
    $stop = $false
    foreach ($name in $names) {
        if ($stop) { break }
        $labels = Join-Path $Data $Sets[$name].Labels
        $tickets = Join-Path $Data $Sets[$name].Tickets
        $backup = $labels -replace '\.jsonl$', '_sqlserver_only.jsonl'
        $work = Join-Path $Data "claude_pack\relabel\$name"
        $batchDir = Join-Path $work "batches"
        $replyDir = Join-Path $work "replies"
        $rowDir = Join-Path $work "rows"

        $allOf = $Sets[$name].AllOf
        if (-not $allOf) {
            # The tickets to label are those of the original label file, kept as the backup once replaced
            if (Test-Path $backup) { $source = $backup } else { $source = $labels }
            if (-not (Test-Path $source)) { Write-Warning "${name}: no label file at $source; skipped"; continue }
        }
        if ($Fresh -and (Test-Path $work)) { Remove-Item -Recurse -Force $work }
        if (-not (Get-ChildItem $batchDir -Filter "batch_*.jsonl" -ErrorAction SilentlyContinue)) {
            if ($allOf) {
                $except = @($Labeled | Where-Object { Test-Path $_ })
                Invoke-Python tools.claude_pack labels --all-of $allOf --except-labels $except --out-dir $batchDir
            } else {
                Invoke-Python tools.claude_pack labels --from-labels $source --out-dir $batchDir
            }
        }
        New-Item -ItemType Directory -Force $replyDir, $rowDir | Out-Null
        $batches = @(Get-ChildItem $batchDir -Filter "batch_*.jsonl" | Sort-Object Name)

        $i = 0
        foreach ($batch in $batches) {
            $i++
            $num = $batch.BaseName -replace '^batch_', ''
            $rows = Join-Path $rowDir "labels_$num.jsonl"
            if (Test-Path $rows) { continue }
            if ($Limit -gt 0 -and $sent -ge $Limit) { break }
            $sent++
            $text = [IO.File]::ReadAllText($batch.FullName, $Utf8)
            $count = @($text -split "`n" | Where-Object { $_.Trim() }).Count
            $message = "Label every ticket in this batch of $count tickets, as the instructions say. " +
                       "The batch is JSONL, one ticket per line:`n`n$text"
            $ok = $false
            for ($attempt = 1; $attempt -le $MaxAttempts -and -not $ok; $attempt++) {
                Write-Host "$name batch $i of $($batches.Count) ($count tickets), attempt $attempt"
                $reply = Join-Path $replyDir "reply_${num}_$attempt.txt"
                $global:LASTEXITCODE = 0  # a stand-in script for -Claude sets no exit code
                $out =$message | & $Claude -p --model $Model --effort $Effort --system-prompt-file $Prompt `
                    --tools $Empty --setting-sources $Empty --strict-mcp-config --no-session-persistence `
                    --disable-slash-commands --output-format text
                $code = $LASTEXITCODE
                [IO.File]::WriteAllText($reply, ($out | Out-String), $Utf8)
                if ($code -ne 0) {
                    # Often a usage limit or an outage: wait longer after each failure
                    Write-Host "  claude exited with code $code; waiting $(60 * $attempt) seconds"
                    Start-Sleep -Seconds (60 * $attempt)
                    continue
                }
                # check-reply prints its counts, or why the reply is unusable, and exits 1 if it is
                $check = & $Python -m tools.claude_pack check-reply $reply --batch $batch.FullName --out $rows
                if ($LASTEXITCODE -eq 0) { Write-Host "  $check"; $ok = $true }
            }
            if ($ok) { $failed = 0; continue }
            Write-Warning "$name batch ${i}: no usable reply after $MaxAttempts attempts; run again to retry it"
            $failed++
            if ($failed -ge 3) {
                Write-Warning "Three batches in a row failed, perhaps a usage limit; stopping. Run again later to resume."
                $stop = $true
                break
            }
        }

        $done = @(Get-ChildItem $rowDir -Filter "labels_*.jsonl" | Sort-Object Name)
        if ($done.Count -lt $batches.Count) {
            Write-Host "${name}: $($done.Count) of $($batches.Count) batches labeled; run again to continue"
            continue
        }
        if (-not $allOf -and -not (Test-Path $backup)) {
            Copy-Item $labels $backup
            Write-Host "Kept the SQL Server-only labels as $backup"
        }
        # Imported into a new file first, so the label file is replaced only by a complete import
        $new = "$labels.new"
        if (Test-Path $new) { Remove-Item $new }
        Invoke-Python tools.claude_pack import-labels @($done.FullName) --out $new
        Move-Item -Force $new $labels
        if ($Sets[$name].Score) { Invoke-Python tools.server_labels evaluate --labels $labels --tickets $tickets }
    }
}
finally {
    Pop-Location
}
