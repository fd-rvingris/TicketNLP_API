# Example requests for the Ticket Analysis API with Invoke-RestMethod (Windows PowerShell 5.1 or PowerShell 7).
# Start the API first: python -m api --local   (or --auth api_key, or docker compose up), then:
#   .\docs\api-examples.ps1                        # no auth
#   .\docs\api-examples.ps1 -ApiKey <key>          # AUTH_MODE=api_key
# Made-up text only. Prints a short line per request.
param(
    [string]$Base = "http://127.0.0.1:8000",
    [string]$ApiKey = ""
)

$headers = @{}
if ($ApiKey) { $headers["X-API-Key"] = $ApiKey }

function Send([string]$Method, [string]$Path, $Body = $null, [hashtable]$Extra = @{}, [string]$ContentType = "application/json") {
    $h = $headers.Clone()
    foreach ($k in $Extra.Keys) { $h[$k] = $Extra[$k] }
    $params = @{ Method = $Method; Uri = "$Base$Path"; Headers = $h }
    if ($null -ne $Body) {
        $params.ContentType = $ContentType
        $params.Body = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Depth 5 -Compress }
    }
    try {
        return Invoke-RestMethod @params
    } catch {
        $status = $_.Exception.Response.StatusCode.value__
        Write-Host "  $Method $Path -> $status $($_.ErrorDetails.Message)"
        return $null
    }
}

Write-Host "Probes"
Send GET "/health" | ConvertTo-Json -Compress
Send GET "/ready" | ConvertTo-Json -Compress

Write-Host "Detect with subject and zendeskOrgId"
$r = Send POST "/v1/detect" @{
    text = "The REPORTS instance on FAKESQL01 is slow since this morning. FAKESQL02\FINANCE is fine."
    subject = "Slow reports"; zendeskOrgId = 999001; ticketId = "12345"
} @{ "X-Request-Id" = "example-detect-1" }
if ($r) { Write-Host "  client known: $($r.client.known); entities: $($r.entities | ConvertTo-Json -Compress)" }

Write-Host "Detect, text only (no client's names apply)"
$r = Send POST "/v1/detect" @{ text = "Please check FAKESQL01 and FAKESQL01\REPORTS today." }
if ($r) { $r.detections | ForEach-Object { Write-Host "  $($_.key) $($_.value) $($_.status)" } }

Write-Host "Classify the access request email"
$email = "Hi Fortified Data Support,`r`n`r`nCould you please help me get READ access for the schema below, " +
    "Server=FAKESQL01, Database=FAKE_DB, for the execution user CORP\svc-fake-batch. Please approve.`r`n`r`n" +
    "Thanks,`r`nJane Doe`r`nData Engineer | Fake Co"
$r = Send POST "/v1/classify" @{ text = $email; subject = "READ access request" }
if ($r) { Write-Host "  $($r.primaryType) from $($r.typeSource), intent $($r.intent), uncertain $($r.uncertain)" }

Write-Host "Classify without a subject"
$r = Send POST "/v1/classify" @{ text = "Disk space is low on the data drive of FAKESQL01, the volume is nearly full." }
if ($r) { Write-Host "  $($r.primaryType) from $($r.typeSource), uncertain $($r.uncertain) $($r.uncertainReason)" }

Write-Host "Analyze a collector alert"
$alert = "AlertId 123456`nAlert FD - SQL Agent Job Failure`nClient Name Fake Co`nServer FAKESQL01`n" +
    "Platform Microsoft SQL Server`nInstance Job: Nightly Load, Category: Data`nMessage Error: The job failed.`n" +
    "Severity Warning`nLevel Failure`nPreviousLevel OK`nOpen Date 07/21/2026 17:00:00 UTC`n"
$r = Send POST "/v1/analyze" @{ text = $alert; subject = "Fake Co - Alert #42 - FD - SQL Agent Job Failure"; zendeskOrgId = "999001" }
if ($r) { Write-Host "  alert $($r.detection.isAlert); $($r.taxonomy.primaryType) from $($r.taxonomy.typeSource); models $($r.models | ConvertTo-Json -Compress)" }

Write-Host "Catalog"
$r = Send GET "/v1/catalog"
if ($r) { Write-Host "  $($r.types.Count) types, $($r.intents.Count) intents, statuses: $((($r.detectionFields | Where-Object name -eq 'status').values.value) -join ', ')" }

Write-Host "Auth failure (401 when the API runs with AUTH_MODE=api_key)"
$saved = $headers
$headers = @{ "X-API-Key" = "fake-wrong-key-0000000000" }
$null = Send POST "/v1/detect" @{ text = "FAKESQL01 is down." }
$headers = $saved

Write-Host "Validation failure: no text (422)"
$null = Send POST "/v1/detect" @{ subject = "No text" }

Write-Host "Oversized body (413)"
$null = Send POST "/v1/detect" ('{"text": "' + ("x" * 1100000) + '"}')

Write-Host "Not JSON (415)"
$null = Send POST "/v1/detect" "FAKESQL01 is down." @{} "text/plain"
