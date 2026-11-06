"""Release tooling checks never contact Azure or send real WhatsApp messages."""

from datetime import date, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text

from flow_test.service import FlowTest
from services.whatsapp_release import encode_value, read_revision_inventory
from tests.test_workflow_concurrency import target


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


def test_release_backup_serializes_dates_without_changing_values():
    value = {"date": date(2030, 1, 7), "instant": datetime(2030, 1, 7, 8, 30), "status": "UNKNOWN"}
    assert json.loads(json.dumps(value, default=encode_value)) == {
        "date": "2030-01-07", "instant": "2030-01-07T08:30:00", "status": "UNKNOWN"}
    with pytest.raises(TypeError):
        encode_value(object())


def test_wrapped_powershell_revision_is_rejected_before_release(tmp_path):
    path = tmp_path / "bad-revisions.json"
    path.write_text(json.dumps([{"app": "orscheduler", "revision": {
        "value": ["orscheduler--courses-20261028-145716"], "Count": 1}}]), encoding="utf-8")
    with pytest.raises(ValueError, match="no sender was stopped"):
        read_revision_inventory(str(path))


@pytest.mark.skipif(not POWERSHELL, reason="Windows deployment command")
@pytest.mark.parametrize("names", [[], ["orscheduler--test"], ["orscheduler--test", "orscheduler--other"]])
def test_native_tsv_revision_names_stay_plain_strings(tmp_path, names):
    driver, output = tmp_path / "names.ps1", tmp_path / "names.json"
    driver.write_text(r'''param($source, $target, $python, $names)
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
foreach ($name in @('Write-ProtectedJson','Read-AzureStringList')) {
    $function=$ast.Find({param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    },$true)
    Invoke-Expression $function.Extent.Text
}
$active=@(Read-AzureStringList (& $python -c "import sys; print(sys.argv[1].replace(',',chr(10)))" $names))
$inventory=@()
foreach ($revision in $active) { $inventory += @{app='orscheduler'; revision=[string]$revision} }
Write-ProtectedJson $target @($inventory)
''', encoding="ascii")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(driver),
                             str(ROOT / "deploy_whatsapp.ps1"), str(output), sys.executable, ",".join(names)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert read_revision_inventory(str(output)) == [{"app": "orscheduler", "revision": name} for name in names]


@pytest.mark.skipif(not POWERSHELL, reason="Windows deployment command")
def test_powershell_release_parses_and_preserves_single_revision_array(tmp_path):
    output = tmp_path / "revisions.json"
    driver = tmp_path / "validate.ps1"
    driver.write_text(r'''param($source, $target)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Deployment script syntax error' }
$writer = $ast.Find({param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Write-ProtectedJson'
}, $true)
Invoke-Expression $writer.Extent.Text
Write-ProtectedJson $target @(@{app='fictional-app'; revision='fictional-revision'})
''', encoding="ascii")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(driver),
                             str(ROOT / "deploy_whatsapp.ps1"), str(output)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == [{"app": "fictional-app", "revision": "fictional-revision"}]


@pytest.mark.skipif(not POWERSHELL, reason="Windows deployment encoding")
def test_release_streams_unicode_python_output_in_windows_powershell(tmp_path):
    driver = tmp_path / "encoding.ps1"
    driver.write_text(r'''param($source, $python)
$ErrorActionPreference = 'Stop'
$savedEnvironment = @{}
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Deployment script syntax error' }
foreach ($name in @('Set-ReleaseEnvironment', 'Enable-Utf8Output')) {
    $function = $ast.Find({param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    Invoke-Expression $function.Extent.Text
}
Enable-Utf8Output
$line = & $python -c "import sys; print(str(sys.flags.utf8_mode) + '|' + sys.stdout.encoding + '|' + chr(0x0D2E) + chr(0x0D32) + chr(0x0D2F))"
if ($LASTEXITCODE -ne 0) { throw 'Unicode native output failed' }
[Console]::WriteLine($line)
''', encoding="ascii")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(driver),
                             str(ROOT / "deploy_whatsapp.ps1"), sys.executable],
                            capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1|utf-8|\u0d2e\u0d32\u0d2f"


@pytest.mark.skipif(not POWERSHELL, reason="Windows deployment command")
@pytest.mark.parametrize("existing_worker", [False, True])
def test_entire_powershell_release_with_fake_azure_and_fake_database_helper(tmp_path, existing_worker):
    """Exercise the orchestration/ARM JSON without any cloud or DB mutation."""
    for name in ("services/whatsapp_worker.py", "services/whatsapp_dispatch.py", "services/whatsapp_release.py", "Dockerfile"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("", encoding="ascii")
    env = [{"name": name, "value": value} for name, value in {
        "MAIN_WORKFLOW_ENABLED": "true", "MAIN_WORKFLOW_DATA_SOURCE": "test", "MAIN_WORKFLOW_LIVE": "true",
        "MAIN_WORKFLOW_TEST_DATABASE_SCHEMA": "flow_test", "WHATSAPP_PHONE_NUMBER_ID": "123456789",
        "FLOW_TEST_MESSAGE_MODE": "session", "FLOW_TEST_ADMIN_TOKEN": "fake-admin"
    }.items()]
    env += [{"name": name, "secretRef": ref} for name, ref in {
        "MAIN_WORKFLOW_TEST_DATABASE_URL": "queue-db", "THERAPY_SCHEDULER_DATABASE_URL": "admin-db",
        "WHATSAPP_ACCESS_TOKEN": "fake-whatsapp"}.items()]
    fixture = {"location": "fictional-region", "tags": {"therapy-whatsapp-release": "orscheduler"}, "properties": {
        "environmentId": "fictional-environment", "workloadProfileName": "Consumption",
        "latestRevisionName": "ready", "latestReadyRevisionName": "ready", "provisioningState": "Succeeded",
        "configuration": {"ingress": {"fqdn": "fictional.invalid"}, "registries": [{
            "identity": "", "server": "orscheduler0488dadb.azurecr.io", "username": "fictional-user", "passwordSecretRef": "registry-password"}]},
        "template": {"containers": [{"env": env, "image": "old-image", "resources": {"cpu": 0.5, "memory": "1Gi"}}]}}}
    (tmp_path / "fixture.json").write_text(json.dumps(fixture), encoding="ascii")
    fake_helper = tmp_path / "fake-helper.py"
    fake_helper.write_text('''import json, pathlib, sys
if '--revisions' in sys.argv:
    data=json.loads(pathlib.Path(sys.argv[sys.argv.index('--revisions')+1]).read_text())
    assert isinstance(data,list) and all(isinstance(row['revision'],str) for row in data)
    pathlib.Path(__file__).with_name('captured-inventory.json').write_text(json.dumps(data))
''', encoding="ascii")
    (tmp_path / "fake-helper.cmd").write_text(f'@echo off\n"{sys.executable}" "{fake_helper}" %*\n', encoding="ascii")
    source = (ROOT / "deploy_whatsapp.ps1").read_text(encoding="utf-8")
    begin, end = source.index("function Invoke-Azure {"), source.index("function Set-ReleaseEnvironment {")
    fake = r'''function Invoke-Azure {
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
if ($Arguments[0] -eq 'acr') { return ('sha256:' + ('b' * 64)) }
if ($Arguments[1] -eq 'list') { return $script:fakeAppList }
if ($Arguments[1] -eq 'secret') { return '[{"name":"queue-db","value":"fake-queue"},{"name":"admin-db","value":"fake-admin"},{"name":"fake-whatsapp","value":"fake-token"},{"name":"registry-password","value":"fake-registry"}]' }
if ($Arguments[1] -eq 'revision') { return ($Arguments[[Array]::IndexOf($Arguments,'--name')+1] + '--old') }
if ($Arguments[1] -eq 'show') {
    $appName=$Arguments[[Array]::IndexOf($Arguments,'--name')+1]
    $doc=ConvertFrom-Json (ConvertTo-Json -InputObject $script:fakeFixture -Depth 40)
    if ($appName -eq 'orscheduler-whatsapp-worker') { $doc.properties.configuration.ingress=$null }
    if ($script:fakeImage) { $doc.properties.template.containers[0].image=$script:fakeImage }
    if ($Arguments -contains '--query') { return (ConvertTo-Json -InputObject @{state='Succeeded'; latest='ready'; ready='ready'; image=$script:fakeImage}) }
    return (ConvertTo-Json -InputObject $doc -Depth 40)
}
if ($Arguments -contains '--image') { $script:fakeImage=$Arguments[[Array]::IndexOf($Arguments,'--image')+1]; return }
if ($Arguments -contains '--yaml') {
    $document=$Arguments[[Array]::IndexOf($Arguments,'--yaml')+1]
    Copy-Item -LiteralPath $document -Destination (Join-Path $PSScriptRoot 'captured-worker.json')
    return
}
throw 'Unexpected fake Azure call'
}

'''
    source = source[:begin] + fake + source[end:]
    source = source.replace("$python = Join-Path $PSScriptRoot '.venv\\Scripts\\python.exe'",
                            "$python = Join-Path $PSScriptRoot 'fake-helper.cmd'")
    source = source.replace("    $health = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 30",
                            "    $health = [pscustomobject]@{StatusCode=200}")
    log_line = next(line for line in source.splitlines() if "$logs = & az containerapp logs show" in line)
    source = source.replace(log_line, "        $logs = 'WhatsApp worker ready; replies have a separate processing lane'; $global:LASTEXITCODE=0")
    # Prefix follows param(), so the original script still binds -UseImage.
    prefix = "\nfunction az {}\n$script:fakeFixture=Get-Content (Join-Path $PSScriptRoot 'fixture.json') -Raw | ConvertFrom-Json\n$script:fakeImage=$null\n"
    prefix += "$script:fakeAppList='orscheduler" + ("`norscheduler-whatsapp-worker" if existing_worker else "") + "'\n"
    # Real TSV has newline characters, not literal PowerShell escape sequences.
    prefix = prefix.replace("orscheduler`norscheduler", "orscheduler\norscheduler")
    insertion = source.index("Set-StrictMode -Version Latest")
    source = source[:insertion] + prefix + source[insertion:]
    script = tmp_path / "deploy-test.ps1"
    script.write_text(source, encoding="utf-8-sig")
    environment = os.environ.copy()
    # The parent tool shell can be PS7; let Windows PowerShell locate its own
    # Security module instead of inheriting incompatible PS7 module paths.
    for key in list(environment):
        if key.upper() == "PSMODULEPATH":
            environment.pop(key)
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script), "-UseImage",
                             "orscheduler0488dadb.azurecr.io/therapy-scheduler:fake"],
                            capture_output=True, encoding="utf-8", timeout=60, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    worker = json.loads((tmp_path / "captured-worker.json").read_text())
    container = worker["properties"]["template"]["containers"][0]
    assert container["command"] == ["python", "-m", "services.whatsapp_worker"]
    assert container["args"] == ["--workspace", "main"]
    assert isinstance(container["env"], list) and all(isinstance(item, dict) and "name" in item for item in container["env"])
    assert {item["name"]: item for item in container["env"]}["WHATSAPP_DISPATCH_DATABASE_URL"]["secretRef"] == "queue-db"
    inventory = json.loads((tmp_path / "captured-inventory.json").read_text())
    assert all(isinstance(item["revision"], str) for item in inventory)
    assert len(inventory) == (2 if existing_worker else 1)
    assert not list((tmp_path / ".codex_tmp").rglob("worker-private.json"))


@pytest.mark.skipif(not POWERSHELL, reason="Windows deployment command")
def test_release_backup_migration_and_control_provisioning_against_disposable_pg(target, tmp_path, legacy_flow_scenario):
    if target.startswith("sqlite"):
        pytest.skip("Release tooling intentionally requires PostgreSQL")
    engine = create_engine(target)
    with engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA flow_test"))
    flow = FlowTest(target, live=True, database_schema="flow_test", scenario=legacy_flow_scenario,
                    recipients={"TEST-A": "919999999991", "TEST-B": "919999999992", "TEST-C": "919999999993"},
                    send=lambda *_: pytest.fail("Release must not send"))
    flow.message_mode = "template"
    flow.generate(date(2030, 1, 7))
    flow.publish(date(2030, 1, 7))
    driver = tmp_path / "fake-azure.ps1"
    driver.write_text(r'''param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
if ($Arguments[0] -ne 'containerapp') { exit 7 }
if ($Arguments[1] -eq 'revision' -and $Arguments[2] -eq 'deactivate') { exit 0 }
if ($Arguments[1] -eq 'replica' -and $Arguments[2] -eq 'list') { Write-Output '[]'; exit 0 }
exit 8
''', encoding="ascii")
    inventory, backup = tmp_path / "inventory.json", tmp_path / "backup.json"
    inventory.write_text(json.dumps([{"app": "fictional-app", "revision": "fictional-app--revision"}]), encoding="utf-8")
    environment = os.environ.copy()
    environment.update(WHATSAPP_RELEASE_QUEUE_URL=target, WHATSAPP_RELEASE_ADMIN_URL=target)
    try:
        result = subprocess.run([sys.executable, "-m", "services.whatsapp_release", "--backup", str(backup),
                                 "--revisions", str(inventory), "--az", str(driver), "--resource-group", "fictional-rg",
                                 "--subscription", "fictional-subscription"], cwd=ROOT, env=environment,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        saved = json.loads(backup.read_text(encoding="utf-8"))
        assert saved["schema"] == "flow_test"
        assert len(saved["tables"]["flow_test_jobs"]) == 3
        assert all(job["status"] == "QUEUED" and job["attempts"] == 0 for job in saved["tables"]["flow_test_jobs"])
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM whatsapp_dispatch.whatsapp_dispatch_state")) >= 1
        assert all(job["status"] == "QUEUED" for job in flow.state(date(2030, 1, 7))["jobs"])
    finally:
        flow.engine.dispose()
        engine.dispose()
