[CmdletBinding()]
param(
    [switch]$BuildOnly,
    [switch]$UseCurrentImage
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($BuildOnly -and $UseCurrentImage) { throw "Choose BuildOnly or UseCurrentImage, not both." }

$subscriptionId = "0488dadb-6bca-435f-bee2-0f8977fc646f"
$resourceGroup = "TherapyScheduler"
$appName = "orscheduler"
$registry = "orscheduler0488dadb"
$tag = "main-patient-picker-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$image = "$registry.azurecr.io/therapy-scheduler:$tag"

function Invoke-Azure {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AzArguments)
    & az @AzArguments --subscription $subscriptionId --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw "Azure command failed. Deployment stopped."
    }
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Install it and run az login first."
}

Push-Location -LiteralPath $PSScriptRoot
try {
    foreach ($path in @("Dockerfile", ".dockerignore", "requirements.txt", "web/app.js", "main_workflow/mount.py")) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required build file is missing: $path"
        }
    }

    Write-Host "Checking main application: $appName / $resourceGroup"
    $query = "{image:properties.template.containers[0].image,command:properties.template.containers[0].command,args:properties.template.containers[0].args,fqdn:properties.configuration.ingress.fqdn,mainSettings:properties.template.containers[0].env[?name=='FLOW_TEST_DATABASE_URL' || name=='FLOW_TEST_DATABASE_SCHEMA' || name=='FLOW_TEST_LIVE']}"
    $before = (Invoke-Azure containerapp show --name $appName --resource-group $resourceGroup --query $query --output json) | ConvertFrom-Json
    $launch = (@($before.command) + @($before.args)) -join " "
    if ($launch -match '--test-data|--test-database|flow_test[./]app|--response-demo') {
        throw "The selected app has a test/demo launch command. Its configuration was not changed."
    }
    $settings = @{}
    foreach ($setting in @($before.mainSettings)) {
        if ($null -ne $setting) { $settings[$setting.name] = $setting.value }
    }
    $testDatabase = @($before.mainSettings | Where-Object { $_.name -eq "FLOW_TEST_DATABASE_URL" })
    if ($testDatabase.Count -ne 1 -or -not $testDatabase[0].secretRef -or $settings["FLOW_TEST_DATABASE_SCHEMA"] -ne "flow_test") {
        throw "The existing flow_test database secret/schema is missing. No database was created or changed."
    }
    $testLive = if ($settings["FLOW_TEST_LIVE"] -eq "true") { "true" } else { "false" }

    if ($UseCurrentImage) {
        $image = $before.image
        Write-Host "Reusing the deployed image: $image"
    } else {
        Write-Host "Building and pushing $image in Azure (Docker Desktop is not required)."
        Invoke-Azure acr build --registry $registry --platform linux/amd64 --image "therapy-scheduler:$tag" .
    }
    if ($BuildOnly) {
        Write-Host "Image built and pushed: $image"
        Write-Host "BuildOnly selected; the running application was not updated."
        return
    }

    Write-Host "Configuring the main / page to use the EXISTING test database (flow_test)."
    Write-Host "Preserving the existing test messaging mode: live=$testLive"
    Invoke-Azure containerapp update --name $appName --resource-group $resourceGroup --image $image --set-env-vars `
        "MAIN_WORKFLOW_ENABLED=true" "MAIN_WORKFLOW_DATA_SOURCE=test" `
        "MAIN_WORKFLOW_REQUIRE_ACCESS_KEY=false" `
        "MAIN_WORKFLOW_TEST_DATABASE_URL=secretref:$($testDatabase[0].secretRef)" `
        "MAIN_WORKFLOW_TEST_DATABASE_SCHEMA=flow_test" "MAIN_WORKFLOW_LOCAL_NO_AUTH=false" `
        "MAIN_WORKFLOW_REMOTE_SENDER=false" "MAIN_WORKFLOW_LIVE=$testLive" --output none

    $ready = $false
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        $revision = (Invoke-Azure containerapp show --name $appName --resource-group $resourceGroup --query "{image:properties.template.containers[0].image,latest:properties.latestRevisionName,ready:properties.latestReadyRevisionName,state:properties.provisioningState}" --output json) | ConvertFrom-Json
        if ($revision.state -eq "Failed") {
            throw "Azure reported a failed deployment. Inspect Container App revision logs. Previous image: $($before.image)"
        }
        if ($revision.image -eq $image -and $revision.state -eq "Succeeded" -and $revision.latest -and $revision.latest -eq $revision.ready) {
            $ready = $true
            break
        }
        Write-Host "Waiting for the new revision ($attempt/60)..."
        Start-Sleep -Seconds 5
    }
    if (-not $ready) {
        throw "The new revision was not ready within five minutes. Inspect Azure before retrying. Previous image: $($before.image)"
    }

    $baseUrl = "https://$($before.fqdn)"
    # Workflow APIs require an operations key. Check public serving/configuration
    # after Azure readiness without retrieving a secret or weakening API auth.
    $health = Invoke-WebRequest -Uri "$baseUrl/" -UseBasicParsing -TimeoutSec 30
    if ($health.StatusCode -ne 200) { throw "The main page check failed. Inspect the new revision logs." }
    $browserConfig = (Invoke-WebRequest -Uri "$baseUrl/main-workflow-config.js" -UseBasicParsing -TimeoutSec 30).Content
    if ($browserConfig -notmatch '"dataSource"\s*:\s*"test"') {
        throw "The main page has not confirmed the test database configuration. Inspect the revision before continuing."
    }
    if ($browserConfig -notmatch '"requiresAccessKey"\s*:\s*false') {
        throw "The main page still requires an access key. Build/deploy the latest source without UseCurrentImage."
    }

    Write-Host ""
    Write-Host "Main application deployed: $baseUrl/" -ForegroundColor Green
    Write-Host "Database: existing test database, flow_test schema"
    Write-Host "Main page: no access key. The separate /test interface still requires its key."
    Write-Host "Image: $image"
    Write-Host "Ready revision: $($revision.ready)"
    Write-Host "Previous image: $($before.image)"
    Write-Host "Press Ctrl+F5 in your browser, then check the cancelled-session patient picker."
}
finally {
    Pop-Location
}
