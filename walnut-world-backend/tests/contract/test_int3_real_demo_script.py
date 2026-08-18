"""Static and non-live unit gates for the INT3 real-demo orchestrator."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND_ROOT / "scripts" / "run-int3-real-demo.ps1"


def test_int3_demo_script_powershell_syntax_is_valid_without_execution() -> None:
    environment = os.environ.copy()
    environment["WALNUT_TEST_INT3_DEMO_SCRIPT"] = str(SCRIPT)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$errors=$null; "
                "[Management.Automation.Language.Parser]::ParseFile("
                "$env:WALNUT_TEST_INT3_DEMO_SCRIPT,[ref]$null,[ref]$errors)|Out-Null; "
                "if($errors.Count){$errors|ForEach-Object{$_.ToString()};exit 1}"
            ),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_int3_demo_allows_bounded_provider_interaction_recovery_over_three_minutes() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "[ValidateRange(15, 300)]\n    [int]$InteractionDeadlineSeconds" in script
    assert "$InteractionDeadlineSeconds -ge $TotalDeadlineSeconds" in script


def test_int3_demo_worker_uses_the_pinned_watering_world_rules() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "'WALNUT_WORLD_WATERING_EXPECTED_UNITS'" in script
    assert (
        "$env:WALNUT_WORLD_WATERING_EXPECTED_UNITS = '2,1,1,0,0,2,0,1'"
        in script
    )
    worker_start = script.index("Start-OwnedProcess -Role workflow")
    assert script.index("$env:WALNUT_WORLD_WATERING_EXPECTED_UNITS =", 0, worker_start) > 0


def test_backend_state_timestamp_remains_an_exact_utc_string_in_powershell_7() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    state_reader = script.split("function Get-ExactBackendState", 1)[1].split(
        "function Get-DatabaseUrl", 1
    )[0]

    assert "ConvertFrom-Json -DateKind String" in state_reader
    assert state_reader.index("ConvertFrom-Json -DateKind String") < state_reader.index(
        "[DateTimeOffset]::Parse([string]$state.process_started_at)"
    )

    completed = subprocess.run(
        [
            "pwsh.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$raw='{\"process_started_at\":\"2026-08-17T09:00:00.1234567Z\"}';"
                "$state=$raw|ConvertFrom-Json -DateKind String;"
                "if($state.process_started_at -isnot [string]){exit 2};"
                "if($state.process_started_at -cne '2026-08-17T09:00:00.1234567Z'){exit 3};"
                "$parsed=[DateTimeOffset]::Parse([string]$state.process_started_at);"
                "if($parsed.Offset -ne [TimeSpan]::Zero){exit 4}"
            ),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_student_jwt_is_raw_for_godot_and_prefixed_once_for_gateway_preflight() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    jwt_function = script.split("function New-StudentAuthorization", 1)[1].split(
        "function Unprotect-BackendHmacSecret", 1
    )[0]

    assert 'return "$signingInput.$(ConvertTo-Base64Url -Bytes $signature)"' in jwt_function
    assert 'return "Bearer $signingInput.' not in jwt_function
    assert 'Authorization = "Bearer $studentAuthorization"' in script
    assert "$env:YAYA_AUTH_TOKEN = $studentAuthorization" in script
    assert '$env:YAYA_AUTH_TOKEN = "Bearer $studentAuthorization"' not in script


def test_int3_demo_reuses_gateway_and_starts_only_private_runtime_children() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    for required in (
        "$script:GatewayPort = 8790",
        "$script:ExpectedBackendRuntimeVersion = '1.0.0'",
        "$backendStatePath",
        "Get-ExactBackendState",
        "backend_listener_pid",
        "process_started_at",
        "development_auth",
        "Assert-NoCompetingRuntime",
        "walnut_backend.llm_relay.main",
        "walnut_backend.worker_main",
        "walnut_backend.learner_worker_main",
        "$RelayPort = 18791",
        "Start-OwnedProcess -Role relay",
        "Start-OwnedProcess -Role workflow",
        "Start-OwnedProcess -Role learner",
        "Stop-OwnedProcesses",
        "PID $($owned.process_id) was reused",
    ):
        assert required in script

    assert "walnut_backend.main:app" not in script.split("function Get-ExactBackendState", 1)[0]
    assert "Start-OwnedProcess -Role gateway" not in script
    assert "docker run" not in script.lower()
    assert "alembic upgrade" not in script.lower()
    assert "seed_int1_e2e_authority" not in script


def test_int3_demo_has_read_only_exact_baseline_and_preserves_result_database() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    for required in (
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ ONLY",
        '"product_content_units", "world_snapshots", "learner_profiles"',
        '"agent_profiles", "build_policies", "launch_authorities", "registry_heads"',
        'expected["audit_records"] = counts.get("audit_records", 0)',
        "database is not the seven-row authority baseline",
        "unit_id='YAYA_FARM_001'",
        "world_id='world_crop_watering_0001'",
        "learner_id='student_0001'",
        "recoverable_llm_dispatches",
        "game_runs",
        "game_evidence",
        "learner_projection_jobs",
        "database_preserved_for_feishu_sync = $true",
    ):
        assert required in script

    assert "DELETE FROM" not in script.upper()
    assert "UPDATE " not in script.upper()
    assert "INSERT INTO" not in script.upper()
    assert "DROP " not in script.upper()
    assert "Remove-Item -LiteralPath $runDirectory -Recurse" not in script


def test_int3_demo_secrets_are_file_scoped_and_student_jwt_is_memory_only() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    for required in (
        "Assert-SecretFileAcl",
        "WALNUT_LLM_API_KEY=<secret>",
        "Write-TemporaryProviderKey",
        "$env:WALNUT_LLM_UPSTREAM_API_KEY_FILE = $providerKeyPath",
        "$env:WALNUT_LLM_UPSTREAM_API_KEY = $null",
        "Remove-Item -LiteralPath $providerKeyPath -Force",
        "Unprotect-BackendHmacSecret",
        "New-StudentAuthorization",
        "$env:YAYA_AUTH_TOKEN = $studentAuthorization",
        "$env:YAYA_AUTH_TOKEN = $null",
        "provider_key_echoed = $false",
        "student_jwt_persisted = $false",
    ):
        assert required in script

    assert "sk-" not in script
    assert "authorization = $studentAuthorization" not in script
    assert "WriteAllText($studentAuthorization" not in script
    runner_arguments = script.split("$runnerArguments = @(", 1)[1].split("\n    )", 1)[0]
    assert "YAYA_AUTH_TOKEN" not in runner_arguments
    assert "studentAuthorization" not in runner_arguments
    frontend_start = script.index("Start-OwnedProcess -Role frontend-phase1")
    assert script.index("$env:WALNUT_DATABASE_URL = $null") < frontend_start
    assert script.index("$env:WALNUT_LLM_RELAY_API_KEY = $null", frontend_start - 1000) < frontend_start
    assert script.index("$env:YAYA_AUTH_TOKEN = $null", frontend_start) > frontend_start
    assert script.index("$env:WALNUT_DATABASE_URL = $databaseUrl", frontend_start) > frontend_start


def test_int3_demo_invokes_only_frontend_phase1_with_short_receipt_guard() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    for required in (
        "run-real-gateway-e2e.ps1",
        "'-Phase1FingerprintPath', $phase1FingerprintPath",
        "'-ResetPersistence'",
        "build-workspaces\\build-$hash\\receipts\\$hash.json",
        "sandbox-results\\ff\\$hash.launch.json",
        "$script:MaximumWindowsPath = 259",
        "Assert-ShortRuntimePath -Path $runDirectory",
        "longest_known_path_characters",
    ):
        assert required in script

    assert "'-RecoveryOnly'" not in script
    assert "'-EnableSkillPatch'" not in script
    assert "'-EnableWorldPresentation'" not in script
    assert script.index("Assert-ShortRuntimePath -Path $runDirectory") < script.index(
        "New-Item -ItemType Directory -Path $runDirectory"
    )


def test_short_runtime_path_budget_executes_on_windows_powershell_51(tmp_path: Path) -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    function_text = (
        "function Assert-ShortRuntimePath"
        + script.split("function Assert-ShortRuntimePath", 1)[1].split(
            "function Get-ExactBackendState", 1
        )[0]
    )
    probe = tmp_path / "int3-path-budget-probe.ps1"
    probe.write_text(
        "$script:MaximumWindowsPath=259\n"
        "$workspaceRoot='C:\\repo'\n"
        + function_text
        + r"""
$result = Assert-ShortRuntimePath -Path 'C:\w3\12345678'
if ($result.longest_known_path_characters -ge 260) { throw 'short path was not short' }
try {
    Assert-ShortRuntimePath -Path 'C:\repo\runtime' | Out-Null
    throw 'repository-contained runtime was accepted'
}
catch {
    if ($_.Exception.Message -notlike '*outside*repositories*') { throw }
}
try {
    Assert-ShortRuntimePath -Path ('C:\w3\' + ('x' * 150)) | Out-Null
    throw 'overlong runtime was accepted'
}
catch {
    if ($_.Exception.Message -notlike '*known receipt path*') { throw }
}
Write-Output 'INT3_SHORT_PATH_BUDGET_PASS'
""",
        encoding="utf-8-sig",
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "INT3_SHORT_PATH_BUDGET_PASS" in completed.stdout


def test_provider_env_parser_accepts_only_the_acl_controlled_single_key(
    tmp_path: Path,
) -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    parser_functions = (
        "function Assert-SecretFileAcl"
        + script.split("function Assert-SecretFileAcl", 1)[1].split(
            "function Protect-RunDirectory", 1
        )[0]
    )
    protect_function = (
        "function Protect-RunDirectory"
        + script.split("function Protect-RunDirectory", 1)[1].split(
            "function Write-TemporaryProviderKey", 1
        )[0]
    )
    probe = tmp_path / "int3-provider-parser-probe.ps1"
    probe.write_text(
        parser_functions
        + protect_function
        + r"""
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$root=Join-Path $env:TEMP ('int3-provider-parser-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
try {
    Protect-RunDirectory -Path $root
    $path=Join-Path $root 'provider.env'
    $fake='not-a-real-provider-key-0123456789'
    [IO.File]::WriteAllText(
        $path,
        ('WALNUT_LLM_API_KEY=' + $fake + "`n"),
        [Text.UTF8Encoding]::new($false)
    )
    $parsed=Read-ProviderKey -Path $path
    if ($parsed -cne $fake) { throw 'single key did not parse' }
    $parsed=$null
    [IO.File]::WriteAllText(
        $path,
        ('WALNUT_LLM_API_KEY=' + $fake + "`nEXTRA=value`n"),
        [Text.UTF8Encoding]::new($false)
    )
    try {
        Read-ProviderKey -Path $path | Out-Null
        throw 'multi-line provider file was accepted'
    }
    catch {
        if ($_.Exception.Message -notlike '*exactly WALNUT_LLM_API_KEY*') { throw }
    }
    Write-Output 'INT3_PROVIDER_PARSER_PASS'
}
finally {
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
""",
        encoding="utf-8-sig",
    )
    completed = subprocess.run(
        [
            "pwsh.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "INT3_PROVIDER_PARSER_PASS" in completed.stdout
    assert "not-a-real-provider-key" not in completed.stdout
