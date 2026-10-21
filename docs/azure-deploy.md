# Deploying the Ticket Analysis API to Azure Container Apps

Step-by-step `az` CLI instructions in PowerShell. They have not been run; review names, SKUs and
network rules with the platform owners first. The settings are described in docs/api.md.

What gets created:

| Resource | Purpose |
| --- | --- |
| Resource group | Everything below |
| Log Analytics workspace | Container logs (the API writes JSON lines without ticket text) |
| Container Registry | The API image, built from this repository |
| Storage account, private Blob container | Model versions (`models/<version>/`) and directory snapshots (`directory/`); client data |
| Key Vault | API keys and the Keystone CMDB credential |
| Container Apps environment | Hosts the app and the job |
| Container app (system-assigned identity) | The API, HTTPS ingress only, at least one replica |
| Container Apps job (system-assigned identity) | The daily directory refresh |

## 0. Names

```powershell
$rg = "rg-ticket-analysis"
$location = "eastus2"            # no region constraint; pick the one closest to Keystone
$acr = "crticketanalysis"        # globally unique, letters and digits
$sa = "stticketanalysis"         # globally unique, letters and digits
$container = "ticket-analysis"
$kv = "kv-ticket-analysis"       # globally unique
$law = "log-ticket-analysis"
$env = "cae-ticket-analysis"
$app = "ca-ticket-analysis"
$job = "caj-directory-refresh"
$modelVersion = "2026-10-17"     # the models/<version>/ folder to serve
az login
az account set --subscription "<subscription id>"
```

## 1. Resource group, Log Analytics and Container Registry

```powershell
az group create --name $rg --location $location
az monitor log-analytics workspace create --resource-group $rg --workspace-name $law --location $location
$lawId = az monitor log-analytics workspace show -g $rg -n $law --query customerId -o tsv
$lawKey = az monitor log-analytics workspace get-shared-keys -g $rg -n $law --query primarySharedKey -o tsv

az acr create --resource-group $rg --name $acr --sku Standard --admin-enabled false
# Builds in Azure from this folder; .dockerignore keeps data/, .env, tools/ and tests/ out of the context
az acr build --registry $acr --image "ticket-analysis-api:1.0.0" .
```

## 2. Storage for model versions and directory snapshots

The account allows no public access and no shared keys, so only Entra identities with a role on the
container can read it.

```powershell
az storage account create --resource-group $rg --name $sa --location $location --sku Standard_ZRS `
  --kind StorageV2 --allow-blob-public-access false --allow-shared-key-access false --min-tls-version TLS1_2 `
  --https-only true
$saId = az storage account show -g $rg -n $sa --query id -o tsv
$containerScope = "$saId/blobServices/default/containers/$container"
# Through the management plane: needs no Blob data role, and shared keys are off
az storage container-rm create --resource-group $rg --storage-account $sa --name $container
```

Uploading needs Storage Blob Data Contributor on the container for whoever uploads. Grant it to a named
operator (or a deployment identity) for the upload and remove it afterwards, so that standing access
stays with the app and the job only. Role assignments take a few minutes to apply; if an upload is refused,
wait and repeat it.

```powershell
$me = az ad signed-in-user show --query id -o tsv
az role assignment create --assignee $me --role "Storage Blob Data Contributor" --scope $containerScope

# A detector saved before the package split must be converted first: python -m tools.convert_model
az storage blob upload --auth-mode login --account-name $sa --container-name $container `
  --name "models/$modelVersion/server_detector.joblib" --file data/models/server_detector.joblib
az storage blob upload --auth-mode login --account-name $sa --container-name $container `
  --name "models/$modelVersion/ticket_types.joblib" --file data/models/ticket_types.joblib
az storage blob upload --auth-mode login --account-name $sa --container-name $container `
  --name "models/$modelVersion/taxonomy_notes.md" --file data/claude_pack/returned/taxonomy_notes.md

# Hashes for MODEL_SHA256, so the API refuses any other file under these names
$h1 = (Get-FileHash data/models/server_detector.joblib -Algorithm SHA256).Hash.ToLower()
$h2 = (Get-FileHash data/models/ticket_types.joblib -Algorithm SHA256).Hash.ToLower()
$modelSha = "server_detector.joblib=$h1,ticket_types.joblib=$h2"

az role assignment delete --assignee $me --role "Storage Blob Data Contributor" --scope $containerScope
```

## 3. Key Vault

```powershell
az keyvault create --resource-group $rg --name $kv --location $location --enable-rbac-authorization true
$kvId = az keyvault show -g $rg -n $kv --query id -o tsv
az role assignment create --assignee $me --role "Key Vault Secrets Officer" --scope $kvId
# Several keys, comma-separated, allow rotation; generate each with a password manager or:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
az keyvault secret set --vault-name $kv --name api-keys --value "<key for Keystone>,<key for other callers>"
az keyvault secret set --vault-name $kv --name cmdb-api-key --value "<Keystone CMDB credential, once known>"
$apiKeysUri = az keyvault secret show --vault-name $kv --name api-keys --query id -o tsv
$cmdbKeyUri = az keyvault secret show --vault-name $kv --name cmdb-api-key --query id -o tsv
```

Use the secret URIs without a version (strip the last path segment) if the app should pick up a rotated
secret on its next revision without a configuration change.

## 4. Container Apps environment

```powershell
az extension add --name containerapp --upgrade
az containerapp env create --resource-group $rg --name $env --location $location `
  --logs-workspace-id $lawId --logs-workspace-key $lawKey
```

## 5. The API app

Create the app with a public placeholder image and a system-assigned identity, grant the identity its
roles, then point it at the private image.

```powershell
az containerapp create --resource-group $rg --name $app --environment $env --system-assigned `
  --image mcr.microsoft.com/k8se/quickstart:latest --ingress external --target-port 8000 `
  --min-replicas 1 --max-replicas 3 --cpu 1.0 --memory 2.0Gi
$appPrincipal = az containerapp show -g $rg -n $app --query identity.principalId -o tsv
$acrId = az acr show -n $acr --query id -o tsv
az role assignment create --assignee $appPrincipal --role AcrPull --scope $acrId
az role assignment create --assignee $appPrincipal --role "Key Vault Secrets User" --scope $kvId
az role assignment create --assignee $appPrincipal --role "Storage Blob Data Reader" --scope $containerScope

# Role assignments take a few minutes to apply. Until they do, the image pull or the model download can
# be refused; the API retries its model load (10 s, doubling to 5 minutes), and /ready stays 503 meanwhile.
az containerapp registry set --resource-group $rg --name $app --server "$acr.azurecr.io" --identity system
az containerapp secret set --resource-group $rg --name $app `
  --secrets "api-keys=keyvaultref:$apiKeysUri,identityref:system"
az containerapp update --resource-group $rg --name $app --image "$acr.azurecr.io/ticket-analysis-api:1.0.0" `
  --set-env-vars "AUTH_MODE=api_key" "API_KEYS=secretref:api-keys" `
    "MODEL_STORE=https://$sa.blob.core.windows.net/$container" "MODEL_VERSION=$modelVersion" `
    "MODEL_SHA256=$modelSha" "MODEL_DIR=/models" `
    "DIRECTORY_STORE=https://$sa.blob.core.windows.net/$container/directory" "DIRECTORY_REFRESH_MINUTES=60" `
    "WORKERS=4" "LOG_FORMAT=json" "LOG_LEVEL=INFO" "MCP_ENABLED=true" "ENABLE_DOCS=false"
```

Ingress is HTTPS only: Container Apps terminates TLS and redirects HTTP; confirm with
`az containerapp ingress show -g $rg -n $app --query allowInsecure` (false). Restrict callers further with
`az containerapp ingress access-restriction set` (Keystone's egress addresses) if the app stays public,
or use an internal environment on a virtual network.

### Probes

The CLI has no probe flags; apply them with YAML. The startup probe allows a few minutes for the model
download; readiness keeps traffic away until both models and the directory are loaded; liveness only
checks the process.

```powershell
az containerapp show -g $rg -n $app -o yaml > app.yaml
```

Add under `properties.template.containers[0]`:

```yaml
probes:
  - type: Startup
    httpGet: {path: /health, port: 8000}
    periodSeconds: 5
    failureThreshold: 60
  - type: Readiness
    httpGet: {path: /ready, port: 8000}
    periodSeconds: 10
    failureThreshold: 3
  - type: Liveness
    httpGet: {path: /health, port: 8000}
    periodSeconds: 30
    failureThreshold: 3
```

```powershell
az containerapp update -g $rg -n $app --yaml app.yaml
Remove-Item app.yaml
```

The minimum of one replica avoids cold starts, which would otherwise include downloading and loading
the models (about 15 MB of files, a few seconds to load) on the first request.

## 6. The directory refresh job

A scheduled job runs `python -m api.refresh_directory` daily from the same image. It reads the detector
model (for its alert names) and the CMDB, and writes `directory/` snapshots, so it needs write access
to the container.

```powershell
az containerapp job create --resource-group $rg --name $job --environment $env --mi-system-assigned `
  --trigger-type Schedule --cron-expression "0 5 * * *" --replica-timeout 1800 --replica-retry-limit 1 `
  --image mcr.microsoft.com/k8se/quickstart:latest --cpu 0.5 --memory 1.0Gi
$jobPrincipal = az containerapp job show -g $rg -n $job --query identity.principalId -o tsv
az role assignment create --assignee $jobPrincipal --role AcrPull --scope $acrId
az role assignment create --assignee $jobPrincipal --role "Key Vault Secrets User" --scope $kvId
az role assignment create --assignee $jobPrincipal --role "Storage Blob Data Contributor" --scope $containerScope

az containerapp job registry set -g $rg -n $job --server "$acr.azurecr.io" --identity system
az containerapp job secret set -g $rg -n $job --secrets "cmdb-api-key=keyvaultref:$cmdbKeyUri,identityref:system"
az containerapp job update -g $rg -n $job --image "$acr.azurecr.io/ticket-analysis-api:1.0.0" `
  --command "python" --args "-m" "api.refresh_directory" `
  --set-env-vars "MODEL_STORE=https://$sa.blob.core.windows.net/$container" "MODEL_VERSION=$modelVersion" `
    "MODEL_SHA256=$modelSha" "MODEL_DIR=/models" `
    "DIRECTORY_STORE=https://$sa.blob.core.windows.net/$container/directory" "DIRECTORY_KEEP=14" `
    "DIRECTORY_MIN_SHARE=0.9" "DIRECTORY_INCLUDE_DW=true" "CMDB_SOURCE=none" "LOG_FORMAT=json"
az containerapp job start -g $rg -n $job   # a first run now
```

`CMDB_SOURCE=none` publishes the model's alert and DW2.0 names until the Keystone API is known; then set
`CMDB_SOURCE=keystone`, `CMDB_BASE_URL` and `CMDB_API_KEY=secretref:cmdb-api-key` (or an Entra token for
the job's identity, if Keystone accepts one). A run that is not published (empty, or under 90% of the
previous snapshot's records) exits 1 and shows as failed; alert on failed job executions in Log
Analytics. The job's role covers the whole container; to keep it out of `models/`, add an ABAC condition
to its role assignment that limits writes to blobs whose path starts with `directory/`.

## 7. Check the deployment

```powershell
$fqdn = az containerapp show -g $rg -n $app --query properties.configuration.ingress.fqdn -o tsv
Invoke-RestMethod "https://$fqdn/health"
Invoke-RestMethod "https://$fqdn/ready"
$headers = @{ "X-API-Key" = "<key>" }
$body = @{ text = "The REPORTS instance on FAKESQL01 is slow."; subject = "Slow reports"; zendeskOrgId = 999001 } |
  ConvertTo-Json
Invoke-RestMethod -Method Post "https://$fqdn/v1/detect" -Headers $headers -ContentType "application/json" -Body $body
```

Logs: `az containerapp logs show -g $rg -n $app --follow`, or query `ContainerAppConsoleLogs_CL` in the
workspace. Request lines carry method, path, status, latency, request id, text length and whether an org
id was given, never the text.

## 8. Rolling out and rolling back a model

Upload the new files to `models/<new version>/` (step 2), then change the setting; Container Apps
starts a new revision and moves traffic once it is ready:

```powershell
az containerapp update -g $rg -n $app --set-env-vars "MODEL_VERSION=<new version>" "MODEL_SHA256=<new hashes>"
az containerapp job update -g $rg -n $job --set-env-vars "MODEL_VERSION=<new version>" "MODEL_SHA256=<new hashes>"
```

Roll back by setting the previous version again. Callers see the change only in `models` in each
response; `/v1` does not change.

## 9. Where scheduled retraining would plug in

`python -m tools.retrain` does the training, scoring and gating (README, Scheduled retraining). In
Azure it would run as a second scheduled Container Apps job (cron `0 3 1 1,4,7,10 *`) from an image
that includes `tools/`, after a step that exports the latest tickets and labels from DW2.0 into its
own storage, with `--store https://$sa.blob.core.windows.net/$container --serving-dir <a copy of
models/<MODEL_VERSION>/>`. It uploads a new `models/<date>/` folder only when the metrics are not
worse than the serving version's, and prints the MODEL_VERSION and MODEL_SHA256 values. Promotion
stays a reviewed change of `MODEL_VERSION` (step 8). The job needs the training data, so it needs
its own identity and data access review; the export step is not built.

## 10. If the auth decision goes another way

- **Entra ID:** register an app for the API with an app role (for example `Tickets.Analyze`), assign the
  role to Keystone's identity and other callers, then set `AUTH_MODE=entra`, `ENTRA_TENANT_ID`,
  `ENTRA_AUDIENCE` (the app ID URI and the client id) and `ENTRA_REQUIRED_ROLE`. No code change.
- **API Management:** put APIM in front of the app (the app on an internal environment, or ingress
  restricted to APIM's addresses). APIM can validate Entra tokens or subscription keys, rate-limit per
  caller and import `docs/openapi.yaml` as the API definition. The app then keeps `AUTH_MODE=api_key`
  with a key only APIM holds, or `entra` with APIM forwarding the caller's token.
