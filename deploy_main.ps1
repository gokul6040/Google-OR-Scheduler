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
$workerName = "orscheduler-whatsapp-worker"
$registry = "orscheduler0488dadb"
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$tag = "main-patient-picker-$stamp"
$image = "$registry.azurecr.io/therapy-scheduler:$tag"
# Every run creates new, named revisions, so a stopped app or worker always
# starts again, even when -UseCurrentImage keeps the same image.
$revisionSuffix = "m" + ($stamp -replace '-', '')
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$savedEnvironment = @{}
$releaseDirectory = $null
$stopped = @()
$appStopped = $false

function Invoke-Azure {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AzArguments)
    & az @AzArguments --subscription $subscriptionId --only-show-errors
    if ($LASTEXITCODE -ne 0) {
        throw "Azure command failed. Deployment stopped."
    }
}

function Set-ReleaseEnvironment {
    param([string]$Name, [string]$Value)
    if (-not $savedEnvironment.ContainsKey($Name)) {
        $savedEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

function Write-ProtectedJson {
    param([string]$Path, $Document)
    $json = ConvertTo-Json -InputObject $Document -Depth 50
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding($false)))
}

function Read-AzureStringList {
    param([string[]]$Lines)
    # Native TSV lines give plain names, including zero or one result.
    foreach ($line in (($Lines -join "`n") -split '\r?\n')) {
        $name = $line.Trim()
        if ($name) { Write-Output ([string]$name) }
    }
}

function Get-ActiveRevisions {
    param([string]$Name)
    $active = @(Read-AzureStringList (Invoke-Azure containerapp revision list --name $Name --resource-group $resourceGroup --query "[?properties.active].name" --output tsv))
    foreach ($revision in $active) {
        if ($revision -notmatch ('^' + [regex]::Escape($Name) + '--[a-z0-9-]+$')) {
            throw "Azure returned an invalid revision name; nothing was stopped."
        }
        Write-Output @{app = [string]$Name; revision = [string]$revision}
    }
}

function Test-StoreUpgrade {
    # Read-only. Returns upgradeNeeded, pending and details from the release helper.
    $output = & $python -m main_workflow.release --check
    if ($LASTEXITCODE -ne 0) { throw "The database check failed; see the message above." }
    return (($output -join "`n") | ConvertFrom-Json)
}

function Stop-OldRevisions {
    # Waits for running sends (and Zoho pushes), saves a database backup, stops the
    # named apps' active revisions and, with -Upgrade, upgrades the database.
    param([string[]]$Names, [switch]$Upgrade)
    if (-not $script:releaseDirectory) {
        $script:releaseDirectory = Join-Path $PSScriptRoot ".codex_tmp\main-release-$stamp"
        New-Item -ItemType Directory -Path $script:releaseDirectory | Out-Null
        # The backup holds clinic data: restrict the folder to the current Windows user first.
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $security = New-Object Security.AccessControl.DirectorySecurity
        $security.SetAccessRuleProtection($true, $false)
        $security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")))
        Set-Acl -LiteralPath $script:releaseDirectory -AclObject $security
        [IO.File]::WriteAllText((Join-Path $script:releaseDirectory "azure-driver.ps1"), @'
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
& az @Arguments
exit $LASTEXITCODE
'@, (New-Object Text.UTF8Encoding($false)))
    }
    $candidates = @(foreach ($name in $Names) { Get-ActiveRevisions $name })
    $label = ($Names -join "-")
    $inventory = Join-Path $script:releaseDirectory "old-revisions-$label.json"
    Write-ProtectedJson $inventory @($candidates)
    $helperArguments = @("--stop", "--backup", (Join-Path $script:releaseDirectory "database-backup-$label.json"),
        "--revisions", $inventory, "--az", (Join-Path $script:releaseDirectory "azure-driver.ps1"),
        "--resource-group", $resourceGroup, "--subscription", $subscriptionId)
    if ($Upgrade) { $helperArguments += "--upgrade" }
    # Recorded before the helper runs: it may stop some revisions and then fail.
    $script:stopped += $candidates
    if ($Upgrade) { $script:appStopped = $true }
    & $python -m main_workflow.release @helperArguments | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Release preparation stopped; see the message above." }
}

function Wait-NewRevision {
    param([string]$Name, [string]$ExpectedImage, [string]$PreviousImage)
    $expected = "$Name--$revisionSuffix"
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        $state = (Invoke-Azure containerapp show --name $Name --resource-group $resourceGroup --query "{image:properties.template.containers[0].image,latest:properties.latestRevisionName,ready:properties.latestReadyRevisionName,state:properties.provisioningState}" --output json) | ConvertFrom-Json
        if ($state.state -eq "Failed") {
            throw "Azure reported a failed deployment of $Name. Inspect its revision logs. Previous image: $PreviousImage"
        }
        if ($state.image -eq $ExpectedImage -and $state.state -eq "Succeeded" -and $state.latest -eq $expected -and $state.ready -eq $expected) {
            return $state
        }
        Write-Host "Waiting for the new $Name revision ($attempt/60)..."
        Start-Sleep -Seconds 5
    }
    throw "The new $Name revision was not ready within five minutes. Inspect Azure before retrying. Previous image: $PreviousImage"
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Install it and run az login first."
}

Push-Location -LiteralPath $PSScriptRoot
try {
    foreach ($path in @("Dockerfile", ".dockerignore", "requirements.txt", "web/app.js", "main_workflow/mount.py",
                        "main_workflow/release.py", "services/whatsapp_release.py")) {
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
        # Secret-backed settings may carry only secretRef, with no value property.
        if ($null -ne $setting) {
            $settings[$setting.name] = if ($setting.PSObject.Properties["value"]) { $setting.value } else { $null }
        }
    }
    $testDatabase = @($before.mainSettings | Where-Object { $_.name -eq "FLOW_TEST_DATABASE_URL" })
    if ($testDatabase.Count -ne 1 -or -not $testDatabase[0].secretRef -or $settings["FLOW_TEST_DATABASE_SCHEMA"] -ne "flow_test") {
        throw "The existing flow_test database secret/schema is missing. No database was created or changed."
    }
    $testLive = if ($settings["FLOW_TEST_LIVE"] -eq "true") { "true" } else { "false" }

    $check = $null
    $worker = $null
    $workerActive = $false
    if (-not $BuildOnly) {
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
            throw "Install requirements into .venv before deploying. Nothing was changed."
        }
        # The separate WhatsApp worker (made by deploy_whatsapp.ps1) moves to the same
        # version, so the app and worker never run different code.
        $apps = @(Read-AzureStringList (Invoke-Azure containerapp list --resource-group $resourceGroup --query "[].name" --output tsv))
        if ($apps -contains $workerName) {
            $worker = (Invoke-Azure containerapp show --name $workerName --resource-group $resourceGroup --output json) | ConvertFrom-Json
            $owner = $null
            if ($worker.PSObject.Properties["tags"] -and $worker.tags -and $worker.tags.PSObject.Properties["therapy-whatsapp-release"]) {
                $owner = $worker.tags."therapy-whatsapp-release"
            }
            if ($owner -ne $appName) { throw "The worker name belongs to an unrecognized resource. Nothing was changed." }
            # A worker someone stopped on purpose (to pause sending) is never started here.
            $workerActive = @(Get-ActiveRevisions $workerName).Count -gt 0
            if (-not $workerActive) {
                Write-Warning "The WhatsApp worker is stopped. This deploy leaves it stopped and does not change it."
            }
        }
        # The database URL stays in this process's memory and environment only; it is never printed.
        $secretRows = (Invoke-Azure containerapp secret list --name $appName --resource-group $resourceGroup --show-values --output json) | ConvertFrom-Json
        $databaseUrl = $null
        foreach ($row in @($secretRows)) {
            if ($row.name -eq $testDatabase[0].secretRef -and $row.PSObject.Properties["value"]) { $databaseUrl = [string]$row.value }
        }
        $secretRows = $null
        if (-not $databaseUrl) { throw "Could not read the test database secret. Nothing was changed." }
        Set-ReleaseEnvironment "MAIN_RELEASE_DATABASE_URL" $databaseUrl
        $databaseUrl = $null
        Set-ReleaseEnvironment "PYTHONUTF8" "1"
        Set-ReleaseEnvironment "PYTHONIOENCODING" "utf-8"
        Write-Host "Checking whether the database needs this release's upgrade (read-only)."
        $check = Test-StoreUpgrade
        if ($check.upgradeNeeded) {
            Write-Host "Upgrade needed ($(@($check.pending) -join ', ')). The old version will be stopped before the upgrade."
        } else {
            Write-Host "The database is already upgraded; the app updates without downtime."
        }
    }

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

    if ($check.upgradeNeeded) {
        Write-Host "Waiting for running sends and any Zoho push to finish, backing up the database, stopping the old app$(if ($workerActive) { ' and worker' }), then upgrading. The website is down from here until the new version is ready."
        Write-Host "If it waits here for more than a few minutes, press Ctrl+C: nothing is stopped until the backup message appears."
        $toStop = @($appName)
        if ($workerActive) { $toStop += $workerName }
        Stop-OldRevisions $toStop -Upgrade
    }

    Write-Host "Configuring the main / page to use the EXISTING test database (flow_test)."
    Write-Host "Preserving the existing test messaging mode: live=$testLive"
    Invoke-Azure containerapp update --name $appName --resource-group $resourceGroup --image $image --revision-suffix $revisionSuffix --set-env-vars `
        "MAIN_WORKFLOW_ENABLED=true" "MAIN_WORKFLOW_DATA_SOURCE=test" `
        "MAIN_WORKFLOW_REQUIRE_ACCESS_KEY=false" `
        "MAIN_WORKFLOW_TEST_DATABASE_URL=secretref:$($testDatabase[0].secretRef)" `
        "MAIN_WORKFLOW_TEST_DATABASE_SCHEMA=flow_test" "MAIN_WORKFLOW_LOCAL_NO_AUTH=false" `
        "MAIN_WORKFLOW_REMOTE_SENDER=false" "MAIN_WORKFLOW_LIVE=$testLive" --output none
    $revision = Wait-NewRevision $appName $image $before.image

    if ($workerActive) {
        if (-not $check.upgradeNeeded) {
            Write-Host "Waiting for running sends to finish, then stopping the WhatsApp worker so the old and new worker never run together. The website stays up."
            Stop-OldRevisions @($workerName)
        }
        Write-Host "Starting the WhatsApp worker on the same version. Its own settings are unchanged."
        Invoke-Azure containerapp update --name $workerName --resource-group $resourceGroup --image $image --revision-suffix $revisionSuffix --output none
        $workerRevision = Wait-NewRevision $workerName $image $worker.properties.template.containers[0].image
        $workerReady = $false
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            try {
                $logs = & az containerapp logs show --name $workerName --resource-group $resourceGroup --subscription $subscriptionId --revision $workerRevision.latest --type console --tail 50 --format json --only-show-errors 2>$null
                $logsRead = ($LASTEXITCODE -eq 0)
            } catch {
                $logs = @()
                $logsRead = $false
            }
            if ($logsRead -and ($logs -join "`n") -match 'WhatsApp worker ready; replies have a separate processing lane') {
                $workerReady = $true
                break
            }
            Start-Sleep -Seconds 2
        }
        if (-not $workerReady) { throw "The new worker's start-up was not confirmed in its logs. Inspect $workerName before declaring the rollout complete." }
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
    if ($check.upgradeNeeded) { Write-Host "Database upgrade: applied and verified" } else { Write-Host "Database upgrade: not needed" }
    if ($workerActive) {
        Write-Host "WhatsApp worker: running the same image (ready revision: $($workerRevision.ready))"
    } elseif ($worker) {
        Write-Host "WhatsApp worker: left stopped, still on its old image. Do not restart its old revision." -ForegroundColor Yellow
        Write-Host "  When sending should resume, start it on this version with:"
        Write-Host "  az containerapp update --name $workerName --resource-group $resourceGroup --subscription $subscriptionId --image $image --revision-suffix w$($stamp -replace '-', '')"
    } else {
        Write-Host "WhatsApp worker: none found"
    }
    if ($releaseDirectory) { Write-Host "Database backup and stopped revisions: $releaseDirectory" }
    Write-Host "Main page: no access key. The separate /test interface still requires its key."
    Write-Host "Image: $image"
    Write-Host "Ready revision: $($revision.ready)"
    Write-Host "Previous image: $($before.image)"
    Write-Host "Press Ctrl+F5 in your browser, then check the cancelled-session patient picker."
}
catch {
    $failure = $_
    if (@($stopped).Count) {
        Write-Host ""
        Write-Host "The deployment stopped after it began stopping the old version." -ForegroundColor Yellow
        if ($releaseDirectory) { Write-Host "Database backup and revision list: $releaseDirectory" }
        foreach ($item in $stopped) { Write-Host "  May be stopped: $($item.app) / $($item.revision)" }
        $upgraded = $false  # Only the worker was stopped: this run did not change the database.
        if ($appStopped) {
            try {
                $after = Test-StoreUpgrade
                $upgraded = -not [bool]$after.upgradeNeeded
            } catch {
                $upgraded = $null
            }
        }
        if ($upgraded -eq $false) {
            Write-Host "This run did not upgrade the database. To bring the old version back unchanged, run:"
            foreach ($item in $stopped) {
                Write-Host "  az containerapp revision activate --name $($item.app) --revision $($item.revision) --resource-group $resourceGroup --subscription $subscriptionId"
            }
            Write-Host "Or fix the problem and run this script again."
        } elseif ($upgraded -eq $true) {
            Write-Host "The database upgrade has been applied. Do not restart the old revisions: the old version cannot read the upgraded Zoho push tracking and could create duplicate Zoho records. Fix the problem and run this script again; the upgrade is safe to repeat."
        } else {
            Write-Host "Could not confirm whether the database was upgraded. Do not restart the old revisions; run this script again (the upgrade is safe to repeat)."
        }
    }
    throw $failure
}
finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
    $databaseUrl = $null
    $secretRows = $null
    Pop-Location
}
