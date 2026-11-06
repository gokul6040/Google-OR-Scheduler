[CmdletBinding()]
param(
    [switch]$ValidateOnly,
    [ValidatePattern('^orscheduler0488dadb\.azurecr\.io/therapy-scheduler(?::[A-Za-z0-9_.-]+|@sha256:[a-f0-9]{64})$')]
    [string]$UseImage
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$subscription = '0488dadb-6bca-435f-bee2-0f8977fc646f'
$resourceGroup = 'TherapyScheduler'
$webName = 'orscheduler'
$workerName = 'orscheduler-whatsapp-worker'
$registry = 'orscheduler0488dadb'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$secretFile = $null
$savedEnvironment = @{}
$savedConsoleEncoding = [Console]::OutputEncoding
$savedOutputEncoding = $OutputEncoding

function Invoke-Azure {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $result = & az @Arguments --subscription $subscription --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw 'Azure command failed; deployment stopped. Check Azure resource status before retrying.' }
    return $result
}

function Set-ReleaseEnvironment {
    param([string]$Name, [string]$Value)
    if (-not $savedEnvironment.ContainsKey($Name)) {
        $savedEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
    }
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

function Write-ProtectedJson {
    param([string]$Path, $Document)
    $json = ConvertTo-Json -InputObject $Document -Depth 50
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding($false)))
}

function Read-AzureStringList {
    param([string[]]$Lines)
    # Windows PowerShell 5 can retain array metadata around ConvertFrom-Json
    # results. Native TSV lines give us plain names, including zero/one results.
    foreach ($line in (($Lines -join "`n") -split '\r?\n')) {
        $name = $line.Trim()
        if ($name) { Write-Output ([string]$name) }
    }
}

function Get-ReleaseRevisions {
    param([string[]]$Names)
    foreach ($name in $Names) {
        $active = @(Read-AzureStringList (Invoke-Azure containerapp revision list --name $name --resource-group $resourceGroup --query '[?properties.active].name' --output tsv))
        foreach ($revision in $active) {
            if ($revision -notmatch ('^' + [regex]::Escape($name) + '--[a-z0-9-]+$')) { throw 'Azure returned an invalid revision name; no sender was stopped.' }
            Write-Output @{app = [string]$name; revision = [string]$revision}
        }
    }
}

function Enable-Utf8Output {
    # Azure CLI streams Unicode ACR build logs through Python/colorama. The
    # Windows legacy code page cannot encode some package/build output.
    Set-ReleaseEnvironment 'PYTHONUTF8' '1'
    Set-ReleaseEnvironment 'PYTHONIOENCODING' 'utf-8'
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
    $script:OutputEncoding = [Console]::OutputEncoding
}

function Wait-Ready {
    param([string]$Name, [string]$Image)
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        $state = Invoke-Azure containerapp show --name $Name --resource-group $resourceGroup --query '{latest:properties.latestRevisionName,ready:properties.latestReadyRevisionName,state:properties.provisioningState,image:properties.template.containers[0].image}' --output json | ConvertFrom-Json
        if ($state.state -eq 'Failed') { throw "Azure provisioning failed for $Name." }
        if ($state.state -eq 'Succeeded' -and $state.latest -and $state.latest -eq $state.ready -and $state.image -eq $Image) { return }
        Start-Sleep -Seconds 3
    }
    throw "Readiness was not confirmed for $Name; inspect Azure before retrying."
}

Push-Location -LiteralPath $PSScriptRoot
try {
    Enable-Utf8Output
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Install Azure CLI and run az login first.' }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Install requirements into .venv before deploying.' }
    foreach ($file in @('services/whatsapp_worker.py', 'services/whatsapp_dispatch.py', 'services/whatsapp_release.py', 'Dockerfile')) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required source is missing: $file" }
    }
    $app = Invoke-Azure containerapp show --name $webName --resource-group $resourceGroup --output json | ConvertFrom-Json
    if (@($app.properties.template.containers).Count -ne 1) { throw 'Review the multi-container web configuration before deploying.' }
    $environment = @{}
    foreach ($item in $app.properties.template.containers[0].env) { $environment[$item.name] = $item }
    if ($environment['MAIN_WORKFLOW_ENABLED'].value -ne 'true' -or $environment['MAIN_WORKFLOW_DATA_SOURCE'].value -ne 'test' -or
        $environment['MAIN_WORKFLOW_LIVE'].value -ne 'true' -or $environment['MAIN_WORKFLOW_TEST_DATABASE_SCHEMA'].value -ne 'flow_test') {
        throw 'This release targets only the verified live main app using the retained flow_test queue.'
    }
    $queueRef = $environment['MAIN_WORKFLOW_TEST_DATABASE_URL'].secretRef
    $adminRef = $environment['THERAPY_SCHEDULER_DATABASE_URL'].secretRef
    if (-not $queueRef -or -not $adminRef) { throw 'Existing queue and provisioning database secret references are required.' }
    $apps = @(Read-AzureStringList (Invoke-Azure containerapp list --resource-group $resourceGroup --query '[].name' --output tsv))
    $existingWorker = $null
    if ($apps -contains $workerName) {
        $existingWorker = Invoke-Azure containerapp show --name $workerName --resource-group $resourceGroup --output json | ConvertFrom-Json
        $tagProperty = $existingWorker.tags.PSObject.Properties['therapy-whatsapp-release']
        if (-not $tagProperty -or $tagProperty.Value -ne $webName) { throw 'The worker name already belongs to an unrecognized resource; nothing was overwritten.' }
        if ($existingWorker.properties.configuration.PSObject.Properties['ingress'] -and $existingWorker.properties.configuration.ingress) { throw 'The existing worker has ingress enabled; review it before deploying.' }
    }
    $releaseApps = @($webName)
    if ($existingWorker) { $releaseApps += $workerName }
    $revisions = @(Get-ReleaseRevisions $releaseApps)
    if ($ValidateOnly) {
        Write-Host "Validated target: $webName (live flow_test queue), independent worker: $workerName, rate: 5/second."
        Write-Host 'Validation only: no build, migration, secret retrieval or cloud changes performed.'
        return
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $releaseDirectory = Join-Path $PSScriptRoot ".codex_tmp\whatsapp-release-$stamp"
    New-Item -ItemType Directory -Path $releaseDirectory | Out-Null
    # Backups contain clinic data. Restrict this ignored local directory to the
    # current Windows identity before writing any configuration or row data.
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $security = New-Object Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    Set-Acl -LiteralPath $releaseDirectory -AclObject $security
    Write-ProtectedJson (Join-Path $releaseDirectory 'previous-web.json') $app
    if ($existingWorker) { Write-ProtectedJson (Join-Path $releaseDirectory 'previous-worker.json') $existingWorker }
    # Keep secret values in process memory and the short-lived protected ARM
    # document only; never interpolate them into native command arguments.
    $secretRows = Invoke-Azure containerapp secret list --name $webName --resource-group $resourceGroup --show-values --output json | ConvertFrom-Json
    $secretsByName = @{}
    foreach ($item in $secretRows) {
        if ($item.PSObject.Properties['value']) { $secretsByName[$item.name] = $item.value }
    }
    if (-not $secretsByName[$queueRef] -or -not $secretsByName[$adminRef]) { throw 'Unable to resolve database secrets; no sender was stopped.' }
    Set-ReleaseEnvironment 'WHATSAPP_RELEASE_QUEUE_URL' $secretsByName[$queueRef]
    Set-ReleaseEnvironment 'WHATSAPP_RELEASE_ADMIN_URL' $secretsByName[$adminRef]
    & $python -m services.whatsapp_release --check
    if ($LASTEXITCODE -ne 0) { throw 'Database preflight failed; the running app was not stopped.' }
    if ($UseImage) {
        $reference = $UseImage.Substring("$registry.azurecr.io/".Length)
        $digest = Invoke-Azure acr repository show --name $registry --image $reference --query digest --output tsv
        if ($digest -notmatch '^sha256:[a-f0-9]{64}$') { throw 'The existing image digest could not be verified; deployment stopped before draining.' }
        $image = "$registry.azurecr.io/therapy-scheduler@$digest"
        Write-Host "Using verified existing image: $image"
    } else {
        $tag = "whatsapp-worker-$stamp"
        $image = "$registry.azurecr.io/therapy-scheduler:$tag"
        Write-Host "Building $image in Azure. The running app remains available during the build."
        Invoke-Azure acr build --registry $registry --platform linux/amd64 --image "therapy-scheduler:$tag" . | Out-Host
    }

    # Refresh the read-only inventory after the build, in case revisions changed.
    $revisions = @(Get-ReleaseRevisions $releaseApps)
    $inventoryPath = Join-Path $releaseDirectory 'old-revisions.json'
    Write-ProtectedJson $inventoryPath @($revisions)
    $driver = Join-Path $releaseDirectory 'azure-driver.ps1'
    [IO.File]::WriteAllText($driver, @'
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
& az @Arguments
exit $LASTEXITCODE
'@, (New-Object Text.UTF8Encoding($false)))
    $workerEnvironment = @{}
    foreach ($name in $environment.Keys) {
        if ($name -match '^(MAIN_WORKFLOW_|WHATSAPP_)' -or
            ($name -match '^FLOW_TEST_' -and $name -notmatch '^FLOW_TEST_ZOHO_')) {
            $workerEnvironment[$name] = $environment[$name]
        }
    }
    foreach ($entry in @{
        WHATSAPP_WORKER_MODE = 'external'; WHATSAPP_SENDS_PER_SECOND = '5';
        WHATSAPP_OUTAGE_RECIPIENTS = '3'; WHATSAPP_OUTAGE_WINDOW_SECONDS = '60';
        WHATSAPP_OUTAGE_PAUSE_SECONDS = '30'; MAIN_WORKFLOW_REMOTE_SENDER = 'false'
    }.GetEnumerator()) {
        $workerEnvironment[$entry.Key] = @{name = $entry.Key; value = $entry.Value}
    }
    $workerEnvironment['WHATSAPP_DISPATCH_DATABASE_URL'] = @{name = 'WHATSAPP_DISPATCH_DATABASE_URL'; secretRef = $queueRef}
    $requiredSecrets = @($workerEnvironment.Values | Where-Object { $_.PSObject.Properties['secretRef'] -and $_.secretRef } | ForEach-Object { $_.secretRef })
    # Hashtable-backed override entries are handled separately for PS5.
    $requiredSecrets += $queueRef
    $registries = @($app.properties.configuration.registries)
    foreach ($entry in $registries) {
        if ($entry.identity -and $entry.identity -ne '') { throw 'Managed-identity registry auth needs an explicit worker identity; no identity was copied automatically.' }
        $requiredSecrets += $entry.passwordSecretRef
    }
    $workerSecrets = @()
    foreach ($name in @($requiredSecrets | Sort-Object -Unique)) {
        if (-not $secretsByName[$name]) { throw 'A required worker secret could not be resolved; the protected backup is retained.' }
        $workerSecrets += @{name = $name; value = $secretsByName[$name]}
    }
    $resources = $app.properties.template.containers[0].resources
    $worker = @{
        name = $workerName; type = 'Microsoft.App/containerApps'; location = $app.location;
        tags = @{'therapy-whatsapp-release' = $webName};
        properties = @{
            environmentId = $app.properties.environmentId;
            workloadProfileName = $app.properties.workloadProfileName;
            configuration = @{activeRevisionsMode = 'Single'; registries = $registries; secrets = $workerSecrets};
            template = @{
                terminationGracePeriodSeconds = 60;
                containers = @(@{name = 'whatsapp-worker'; image = $image;
                    command = @('python', '-m', 'services.whatsapp_worker'); args = @('--workspace', 'main');
                    env = @($workerEnvironment.Values); resources = @{cpu = $resources.cpu; memory = $resources.memory}});
                scale = @{minReplicas = 1; maxReplicas = 1}
            }
        }
    }
    $secretFile = Join-Path $releaseDirectory 'worker-private.json'
    Write-ProtectedJson $secretFile $worker
    Write-Host 'Draining the old sender, backing up the queue and applying additive setup. Brief website downtime begins here.'
    & $python -m services.whatsapp_release --backup (Join-Path $releaseDirectory 'queue-backup.json') --revisions $inventoryPath --az $driver --resource-group $resourceGroup --subscription $subscription
    if ($LASTEXITCODE -ne 0) { throw "Release preparation failed. Keep senders stopped and inspect $releaseDirectory plus Azure revision status before recovery." }

    Invoke-Azure containerapp update --name $webName --resource-group $resourceGroup --image $image --set-env-vars `
        'WHATSAPP_WORKER_MODE=external' 'WHATSAPP_SENDS_PER_SECOND=5' 'MAIN_WORKFLOW_REMOTE_SENDER=false' `
        "WHATSAPP_DISPATCH_DATABASE_URL=secretref:$queueRef" --output none | Out-Null
    Wait-Ready $webName $image
    if ($existingWorker) {
        Invoke-Azure containerapp update --name $workerName --resource-group $resourceGroup --yaml $secretFile --output none | Out-Null
    } else {
        Invoke-Azure containerapp create --name $workerName --resource-group $resourceGroup --yaml $secretFile --output none | Out-Null
    }
    Wait-Ready $workerName $image
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $logs = & az containerapp logs show --name $workerName --resource-group $resourceGroup --subscription $subscription --type console --tail 50 --format json --only-show-errors 2>$null
        if ($LASTEXITCODE -eq 0 -and ($logs -join "`n") -match 'WhatsApp worker ready; replies have a separate processing lane') { $ready = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw 'Worker startup was not confirmed in its logs. Inspect the new worker before declaring rollout complete.' }
    $url = "https://$($app.properties.configuration.ingress.fqdn)/"
    $health = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 30
    if ($health.StatusCode -ne 200) { throw 'The website health check failed.' }
    Write-Host "Deployed: $url" -ForegroundColor Green
    Write-Host "Independent worker: $workerName; shared send rate: 5/second. Pending eligible messages can now send."
    Write-Host "Backup/config snapshots: $releaseDirectory"
    Write-Host 'Cloud startup verified. A consented send/reply/restart check is still required for live delivery verification.'
}
finally {
    if ($secretFile -and (Test-Path -LiteralPath $secretFile)) { Remove-Item -LiteralPath $secretFile -Force }
    foreach ($name in $savedEnvironment.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process') }
    [Console]::OutputEncoding = $savedConsoleEncoding
    $OutputEncoding = $savedOutputEncoding
    $secretRows = $null; $secretsByName = $null; $workerSecrets = $null; $worker = $null
    Pop-Location
}
