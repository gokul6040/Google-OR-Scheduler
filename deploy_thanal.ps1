[CmdletBinding(DefaultParameterSetName = 'Deploy')]
param(
    [Parameter(ParameterSetName = 'Check')][switch]$CheckOnly,
    [Parameter(ParameterSetName = 'Build')][switch]$BuildOnly,
    [string]$Roster = '.codex_tmp/iqra-roster/decisions.json'
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create the project .venv and install requirements.txt first.' }
$mode = if ($CheckOnly) { '--check' } elseif ($BuildOnly) { '--build-only' } else { '--deploy' }
Push-Location $PSScriptRoot
try {
    & $python -m main_workflow.thanal_release $mode --roster $Roster
    if ($LASTEXITCODE -ne 0) { throw 'Thanal release stopped. Follow the last status message; no messaging worker was enabled.' }
} finally { Pop-Location }
