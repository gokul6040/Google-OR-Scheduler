"""Main deploy safety: upgrade before start. Never contacts Azure, Zoho or WhatsApp."""

from datetime import date
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text

from flow_test.service import FlowTest
from main_workflow import release
from tests.test_workflow_concurrency import target  # noqa: F401 - pytest fixture


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
FAKE_SECRET = "postgresql://fake-user:SECRET-PASSWORD@db.invalid/fake"


def test_safe_reason_keeps_own_messages_and_hides_connection_details():
    assert release.safe_reason("Traceback ...\nValueError: Refusing to upgrade an unrecognized test store\n") == \
        "Refusing to upgrade an unrecognized test store"
    leaked = "sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection to server at \"db\" failed"
    assert release.safe_reason(leaked) == "OperationalError"
    assert release.safe_reason("ValueError: bad postgresql://user:pw@host/db") == "ValueError"
    assert release.safe_reason("") == "no details"


@pytest.mark.parametrize("value,message", [
    ("", "did not supply"), ("sqlite:///local.db", "PostgreSQL"), ("not a url at all://::", None)])
def test_database_url_requires_postgresql_and_never_echoes_it(monkeypatch, value, message):
    monkeypatch.setenv(release.URL_VARIABLE, value)
    with pytest.raises(release.ReleaseStopped) as caught:
        release.database_url()
    assert value not in str(caught.value) or not value
    if message:
        assert message in str(caught.value)


@pytest.mark.parametrize("arguments", [["--check", "--upgrade"], ["--stop", "--upgrade"], []])
def test_release_command_rejects_incomplete_requests(arguments):
    result = subprocess.run([sys.executable, "-m", "main_workflow.release", *arguments], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2


@pytest.mark.skipif(not POWERSHELL, reason="Release tooling uses Windows PowerShell for Azure commands")
def test_main_release_upgrades_an_old_store_without_sending(target, tmp_path, legacy_flow_scenario):  # noqa: F811
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
    flow.engine.dispose()
    # Make it look like a store from before this release, in a non-live (preview) mode
    # that the WhatsApp release would refuse, with old settings-based push tracking.
    with engine.begin() as connection:
        for table in ("flow_test_offer_appointments", "flow_test_export_items", "flow_test_previews"):
            connection.execute(text(f"DROP TABLE flow_test.{table}"))
        connection.execute(text("ALTER TABLE flow_test.flow_test_offers DROP COLUMN visit_date"))
        connection.execute(text("UPDATE flow_test.flow_test_settings SET value='preview' WHERE key='mode'"))
        connection.execute(text("INSERT INTO flow_test.flow_test_settings (key, value) VALUES (:key, :value)"),
                           {"key": "zoho_scheduler_export:item:999001",
                            "value": json.dumps({"therapyDate": "07-01-2030", "fingerprint": "fp-1", "recordId": "12345"})})
        jobs_before = connection.execute(text("SELECT id, status, attempts FROM flow_test.flow_test_jobs ORDER BY id")).all()
    assert jobs_before and all(status == "QUEUED" for _, status, _ in jobs_before)

    calls = tmp_path / "azure-calls.log"
    driver = tmp_path / "fake-azure.ps1"
    driver.write_text(r'''param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
[IO.File]::AppendAllText($env:FAKE_AZURE_LOG, ($Arguments -join ' ') + "`n")
if ($Arguments[0] -ne 'containerapp') { exit 7 }
if ($Arguments[1] -eq 'revision' -and $Arguments[2] -eq 'deactivate') { exit 0 }
if ($Arguments[1] -eq 'replica' -and $Arguments[2] -eq 'list') { Write-Output '[]'; exit 0 }
exit 8
''', encoding="ascii")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps([{"app": "fictional-app", "revision": "fictional-app--old"}]), encoding="utf-8")
    environment = os.environ.copy()
    environment.update({release.URL_VARIABLE: target, "FAKE_AZURE_LOG": str(calls)})

    def helper(*arguments):
        return subprocess.run([sys.executable, "-m", "main_workflow.release", *arguments], cwd=ROOT,
                              env=environment, capture_output=True, encoding="utf-8", timeout=300)

    def stop(backup):
        return helper("--stop", "--upgrade", "--backup", str(backup), "--revisions", str(inventory),
                      "--az", str(driver), "--resource-group", "fictional-rg", "--subscription", "fictional-sub")

    try:
        check = helper("--check")
        assert check.returncode == 0, check.stdout + check.stderr
        before = json.loads(check.stdout.strip().splitlines()[-1])
        assert before["upgradeNeeded"] is True
        assert {"performanceStorageMissing", "workflowTablesMissing"} <= set(before["pending"])

        first = stop(tmp_path / "backup-1.json")
        assert first.returncode == 0, first.stdout + first.stderr
        assert json.loads(first.stdout.strip().splitlines()[-1]) == {
            "stopped": ["fictional-app--old"], "upgraded": True, "messagesSent": 0}
        assert "containerapp revision deactivate --name fictional-app --revision fictional-app--old" in calls.read_text()
        saved = json.loads((tmp_path / "backup-1.json").read_text(encoding="utf-8"))
        assert saved["schema"] == "flow_test" and len(saved["tables"]["flow_test_jobs"]) == len(jobs_before)

        after = helper("--check")
        assert json.loads(after.stdout.strip().splitlines()[-1])["upgradeNeeded"] is False
        with engine.connect() as connection:
            assert connection.execute(text("SELECT record_id, therapy_date FROM flow_test.flow_test_export_items "
                                           "WHERE appointment_id=999001")).one() == ("12345", date(2030, 1, 7))
            assert connection.scalar(text("SELECT count(*) FROM flow_test.flow_test_settings "
                                          "WHERE key LIKE 'zoho_scheduler_export:item:%'")) == 0
            assert connection.scalar(text("SELECT count(*) FROM flow_test.flow_test_offers WHERE visit_date IS NULL")) == 0
            assert connection.execute(text("SELECT id, status, attempts FROM flow_test.flow_test_jobs ORDER BY id")).all() == jobs_before
            assert connection.scalar(text("SELECT value FROM flow_test.flow_test_settings WHERE key='mode'")) == "preview"

        second = stop(tmp_path / "backup-2.json")  # Safe to repeat.
        assert second.returncode == 0, second.stdout + second.stderr
        assert target not in first.stdout + first.stderr + second.stdout + second.stderr
    finally:
        engine.dispose()


# --- deploy_main.ps1 orchestration with a fake Azure and a fake release helper ---

APP_QUERY = {"image": "old-image", "command": None, "args": None, "fqdn": "fictional.invalid", "mainSettings": [
    {"name": "FLOW_TEST_DATABASE_URL", "secretRef": "flow-test-db-url"},
    {"name": "FLOW_TEST_DATABASE_SCHEMA", "value": "flow_test"},
    {"name": "FLOW_TEST_LIVE", "value": "false"}]}
WORKER = {"tags": {"therapy-whatsapp-release": "orscheduler"},
          "properties": {"template": {"containers": [{"image": "old-worker-image"}]}}}
FAKE_HELPER = r'''import json, os, pathlib, sys
here = pathlib.Path(__file__).parent
args = sys.argv[1:]
assert args[:2] == ["-m", "main_workflow.release"], args
args = args[2:]
def log(entry):
    with (here / "calls.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
log({"call": "helper", "args": args, "urlOk": os.environ.get("MAIN_RELEASE_DATABASE_URL") == os.environ["FAKE_EXPECTED_URL"]})
flag = here / "upgraded.flag"
if args[0] == "--check":
    needed = os.environ["FAKE_UPGRADE_NEEDED"] == "1" and not flag.exists()
    print(json.dumps({"upgradeNeeded": needed, "pending": ["performanceStorageMissing"] if needed else [], "details": {}}))
else:
    inventory = json.loads(pathlib.Path(args[args.index("--revisions") + 1]).read_text(encoding="utf-8-sig"))
    log({"call": "inventory", "items": inventory})
    if os.environ.get("FAKE_FAIL_STOP") == "1":
        sys.exit("Release stopped: fake failure. No messages were sent.")
    if "--upgrade" in args:
        flag.write_text("1")
    print(json.dumps({"upgraded": "--upgrade" in args}))
'''
FAKE_AZURE = r'''function Invoke-Azure {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AzArguments)
    [IO.File]::AppendAllText((Join-Path $PSScriptRoot 'calls.log'), (ConvertTo-Json -Compress -InputObject @{call='az'; args=@($AzArguments)}) + "`n", (New-Object Text.UTF8Encoding($false)))
    $name = if ($AzArguments -contains '--name') { $AzArguments[[Array]::IndexOf($AzArguments, '--name') + 1] } else { '' }
    if ($AzArguments[0] -eq 'acr') { return }
    if ($AzArguments[1] -eq 'list') { return $script:fakeAppList }
    if ($AzArguments[1] -eq 'secret') { return ('[{"name":"other","value":"x"},{"name":"flow-test-db-url","value":"' + $env:FAKE_EXPECTED_URL + '"}]') }
    if ($AzArguments[1] -eq 'revision') {
        if ($env:FAKE_WORKER_STOPPED -eq '1' -and $name -eq 'orscheduler-whatsapp-worker') { return '' }
        return ($name + '--old')
    }
    if ($AzArguments[1] -eq 'update') {
        $script:fakeImage[$name] = $AzArguments[[Array]::IndexOf($AzArguments, '--image') + 1]
        $script:fakeLatest[$name] = $name + '--' + $AzArguments[[Array]::IndexOf($AzArguments, '--revision-suffix') + 1]
        return
    }
    if ($AzArguments[1] -eq 'show') {
        if ($AzArguments -contains '--query') {
            if ($AzArguments[[Array]::IndexOf($AzArguments, '--query') + 1] -like '*mainSettings*') { return $script:fakeAppQuery }
            $state = if ($env:FAKE_FAIL_APP -eq '1' -and $name -eq 'orscheduler') { 'Failed' } else { 'Succeeded' }
            return (ConvertTo-Json -InputObject @{image=$script:fakeImage[$name]; latest=$script:fakeLatest[$name]; ready=$script:fakeLatest[$name]; state=$state})
        }
        return $script:fakeWorkerJson
    }
    throw 'Unexpected fake Azure call'
}

'''


def run_deploy_main(tmp_path, *, upgrade_needed, worker, fail_stop=False, fail_app=False, worker_stopped=False,
                    arguments=()):
    for name in ("Dockerfile", ".dockerignore", "requirements.txt", "web/app.js", "main_workflow/mount.py",
                 "main_workflow/release.py", "services/whatsapp_release.py"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("", encoding="ascii")
    (tmp_path / "app-query.json").write_text(json.dumps(APP_QUERY), encoding="ascii")
    (tmp_path / "worker.json").write_text(json.dumps(WORKER), encoding="ascii")
    (tmp_path / "fake_release.py").write_text(FAKE_HELPER, encoding="ascii")
    (tmp_path / "fake-helper.cmd").write_text(f'@echo off\n"{sys.executable}" "{tmp_path / "fake_release.py"}" %*\n', encoding="ascii")
    source = (ROOT / "deploy_main.ps1").read_text(encoding="utf-8")
    begin, end = source.index("function Invoke-Azure {"), source.index("function Set-ReleaseEnvironment {")
    source = source[:begin] + FAKE_AZURE + source[end:]
    original = '$python = Join-Path $PSScriptRoot ".venv\\Scripts\\python.exe"'
    assert original in source
    source = source.replace(original, '$python = Join-Path $PSScriptRoot "fake-helper.cmd"')
    apps = "orscheduler\norscheduler-whatsapp-worker" if worker else "orscheduler"
    prefix = ("\nfunction az { $global:LASTEXITCODE = 0; 'WhatsApp worker ready; replies have a separate processing lane.' }\n"
              "function Start-Sleep { param([int]$Seconds) }\n"
              "function Invoke-WebRequest { param($Uri, [switch]$UseBasicParsing, $TimeoutSec) "
              "[pscustomobject]@{StatusCode=200; Content='{\"dataSource\": \"test\", \"requiresAccessKey\": false}'} }\n"
              "$script:fakeAppQuery = Get-Content (Join-Path $PSScriptRoot 'app-query.json') -Raw\n"
              "$script:fakeWorkerJson = Get-Content (Join-Path $PSScriptRoot 'worker.json') -Raw\n"
              f"$script:fakeAppList = '{apps}'\n"
              "$script:fakeImage = @{}\n$script:fakeLatest = @{}\n")
    insertion = source.index("Set-StrictMode -Version Latest")
    script = tmp_path / "deploy-main-test.ps1"
    script.write_text(source[:insertion] + prefix + source[insertion:], encoding="utf-8-sig")
    environment = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
    config = tmp_path / "empty-azure-config"
    config.mkdir()
    environment.update(FAKE_EXPECTED_URL=FAKE_SECRET, FAKE_UPGRADE_NEEDED="1" if upgrade_needed else "0",
                       FAKE_FAIL_STOP="1" if fail_stop else "0", FAKE_FAIL_APP="1" if fail_app else "0",
                       FAKE_WORKER_STOPPED="1" if worker_stopped else "0",
                       AZURE_CONFIG_DIR=str(config))  # Even a stray real az call has no login.
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(script), *arguments],
                            capture_output=True, encoding="utf-8", timeout=120, env=environment)
    log = tmp_path / "calls.log"
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return result, calls


def _event(entry):
    if entry["call"] == "helper":
        return "check" if entry["args"][0] == "--check" else ("stop+upgrade" if "--upgrade" in entry["args"] else "stop")
    if entry["call"] == "az" and entry["args"][:2] == ["containerapp", "update"]:
        return "update:" + entry["args"][entry["args"].index("--name") + 1]
    return None


@pytest.mark.skipif(not POWERSHELL, reason="Windows deployment command")
@pytest.mark.parametrize("upgrade_needed,worker,expected,stopped", [
    (True, True, ["check", "stop+upgrade", "update:orscheduler", "update:orscheduler-whatsapp-worker"],
     ["orscheduler--old", "orscheduler-whatsapp-worker--old"]),
    (True, False, ["check", "stop+upgrade", "update:orscheduler"], ["orscheduler--old"]),
    # No upgrade: the app rolls over with no downtime, then the worker is swapped.
    (False, True, ["check", "update:orscheduler", "stop", "update:orscheduler-whatsapp-worker"],
     ["orscheduler-whatsapp-worker--old"]),
    (False, False, ["check", "update:orscheduler"], []),
])
def test_deploy_main_upgrades_before_the_new_version_starts(tmp_path, upgrade_needed, worker, expected, stopped):
    result, calls = run_deploy_main(tmp_path, upgrade_needed=upgrade_needed, worker=worker)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert [event for event in map(_event, calls) if event] == expected
    assert [item["revision"] for entry in calls if entry["call"] == "inventory" for item in entry["items"]] == stopped
    assert all(entry["urlOk"] for entry in calls if entry["call"] == "helper")
    assert "SECRET-PASSWORD" not in output
    updates = [entry["args"] for entry in calls if _event(entry) and _event(entry).startswith("update:")]
    assert all("--revision-suffix" in args for args in updates)
    images = {args[args.index("--image") + 1] for args in updates}
    assert len(images) == 1 and images != {"old-image"}  # App and worker run the same new image.
    assert ("Database upgrade: applied and verified" if upgrade_needed else "Database upgrade: not needed") in output


@pytest.mark.skipif(not POWERSHELL, reason="Windows deployment command")
@pytest.mark.parametrize("upgrade_needed", [True, False])
def test_deploy_main_never_starts_a_worker_that_was_stopped(tmp_path, upgrade_needed):
    result, calls = run_deploy_main(tmp_path, upgrade_needed=upgrade_needed, worker=True, worker_stopped=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "update:orscheduler-whatsapp-worker" not in [_event(entry) for entry in calls]
    assert all(item["app"] == "orscheduler" for entry in calls if entry["call"] == "inventory" for item in entry["items"])
    assert "WhatsApp worker: left stopped" in output and "az containerapp update --name orscheduler-whatsapp-worker" in output


@pytest.mark.skipif(not POWERSHELL, reason="Windows deployment command")
def test_deploy_main_failed_stop_explains_how_to_restore_the_old_version(tmp_path):
    result, calls = run_deploy_main(tmp_path, upgrade_needed=True, worker=True, fail_stop=True)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert not [entry for entry in calls if _event(entry) and _event(entry).startswith("update:")]
    assert "This run did not upgrade the database" in output
    assert "az containerapp revision activate --name orscheduler --revision orscheduler--old" in output
    assert "SECRET-PASSWORD" not in output


@pytest.mark.skipif(not POWERSHELL, reason="Windows deployment command")
def test_deploy_main_failure_after_upgrade_forbids_restarting_old_version(tmp_path):
    result, _ = run_deploy_main(tmp_path, upgrade_needed=True, worker=False, fail_app=True)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "The database upgrade has been applied. Do not restart the old revisions" in output
    assert "revision activate" not in output


@pytest.mark.skipif(not POWERSHELL, reason="Windows deployment command")
def test_deploy_main_build_only_reads_no_secret_and_changes_nothing(tmp_path):
    result, calls = run_deploy_main(tmp_path, upgrade_needed=True, worker=True, arguments=("-BuildOnly",))
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [entry for entry in calls if entry["call"] == "helper"]
    assert not [entry for entry in calls if entry["call"] == "az" and entry["args"][1:2] in (["secret"], ["update"])]


@pytest.mark.skipif(not POWERSHELL, reason="Windows deployment command")
@pytest.mark.parametrize("report,exit_code,outcome", [
    ('{"upgradeNeeded": true, "pending": ["performanceStorageMissing"]}', 0, "blocked"),
    ('{"upgradeNeeded": false, "pending": []}', 0, "allowed"),
    ("", 1, "blocked"),
])
def test_test_workspace_deploy_refuses_before_the_upgrade(tmp_path, report, exit_code, outcome):
    helper = tmp_path / "fake_check.py"
    helper.write_text("import os, sys\n"
                      "assert sys.argv[1:] == ['-m', 'main_workflow.release', '--check']\n"
                      "assert os.environ['MAIN_RELEASE_DATABASE_URL'] == 'postgresql://fake/db'\n"
                      f"print({report!r})\nsys.exit({exit_code})\n", encoding="ascii")
    (tmp_path / "fake-python.cmd").write_text(f'@echo off\n"{sys.executable}" "{helper}" %*\n', encoding="ascii")
    driver = tmp_path / "guard.ps1"
    driver.write_text(r'''param($scriptPath, $python)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Script syntax error' }
$definition = $ast.Find({param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-StoreUpgraded'
}, $true)
Invoke-Expression $definition.Extent.Text
[Environment]::SetEnvironmentVariable('MAIN_RELEASE_DATABASE_URL', 'unchanged', 'Process')
try { Assert-StoreUpgraded -Python $python -DatabaseUrl 'postgresql://fake/db'; $outcome = 'allowed' } catch { $outcome = 'blocked' }
Write-Output ($outcome + '|' + [Environment]::GetEnvironmentVariable('MAIN_RELEASE_DATABASE_URL', 'Process'))
''', encoding="ascii")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(driver),
                             str(ROOT / "flow_test" / "deploy_azure.ps1"), str(tmp_path / "fake-python.cmd")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == f"{outcome}|unchanged"
