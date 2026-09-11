"""Static safety gates for the persistent real-Provider visual launcher."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND_ROOT / "scripts" / "start-persistent-play.ps1"


def test_stop_tolerates_exit_race_but_does_not_hide_real_stop_failure():
    environment = {**os.environ, "WALNUT_TEST_PERSISTENT_PLAY_SCRIPT": str(SCRIPT)}
    command = r"""
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($env:WALNUT_TEST_PERSISTENT_PLAY_SCRIPT,[ref]$null,[ref]$null)
$definition = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-ProcessAndWait'}, $true)
Invoke-Expression $definition.Extent.Text
foreach ($race in @($true, $false)) {
    $script:testProcess = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 20' -WindowStyle Hidden -PassThru
    function Stop-Process {
        param($Id, $InputObject, [switch]$Force, $ErrorAction)
        if ($race) { $script:testProcess.Kill(); $script:testProcess.WaitForExit() }
        throw 'TEST_STOP_FAILURE'
    }
    $threw = $false
    try { Stop-ProcessAndWait -Process $script:testProcess } catch { $threw = $true }
    finally { if (-not $script:testProcess.HasExited) { $script:testProcess.Kill(); $script:testProcess.WaitForExit() } }
    if ($threw -eq $race) { throw 'STOP_RACE_HANDLING_FAILED' }
}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "filename",
    [
        "start-persistent-play.ps1",
        "setup-play.ps1",
        "runtime-preflight.ps1",
        "voice-environment.ps1",
    ],
)
def test_persistent_play_powershell_syntax_is_valid_without_execution(filename) -> None:
    environment = os.environ.copy()
    environment["WALNUT_TEST_PERSISTENT_PLAY_SCRIPT"] = str(SCRIPT.with_name(filename))
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$errors=$null; "
                "[Management.Automation.Language.Parser]::ParseFile("
                "$env:WALNUT_TEST_PERSISTENT_PLAY_SCRIPT,[ref]$null,[ref]$errors)|Out-Null; "
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


def test_persistent_play_supports_current_and_legacy_workspace_layouts() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    for required in (
        "[ValidateSet('Start', 'Status', 'Stop', 'Check')]",
        "$nestedFrontendRoot = Join-Path $frontendContainerRoot 'walnut-world-frontend'",
        "Join-Path $nestedFrontendRoot 'project.godot'",
        "$bundledAgentRoot = Join-Path $backendRoot 'agent'",
        "$legacyAgentRoot = Join-Path $workspaceRoot 'agent'",
        "tools\\godot-4.7.1\\Godot_v4.7.1-stable_win64.exe",
        "Godot project missing: $frontendRoot",
    ):
        assert required in script


def test_provider_credential_is_injected_by_validated_file_only() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    relay_start = script.split(
        "# ---- 4. Recoverable relay (real Provider, file-only credential injection) ----", 1
    )[1].split("# ---- 5. Gateway + worker + learner worker ----", 1)[0]

    assert "Get-Content -LiteralPath $UpstreamKeyFile" not in script
    assert "$upstreamKey" not in script
    assert "$env:WALNUT_LLM_UPSTREAM_API_KEY =" not in script
    assert "Clear-ProcessEnvironmentVariable -Name 'WALNUT_LLM_UPSTREAM_API_KEY'" in relay_start
    assert "'WALNUT_LLM_UPSTREAM_API_KEY_FILE'," in relay_start
    assert "[IO.Path]::GetFullPath($UpstreamKeyFile)" in relay_start
    assert "Start-ProviderBlindBackendChild" in script
    assert "provider_key_source = 'WALNUT_LLM_UPSTREAM_API_KEY_FILE'" in script

    godot_start = script.split("# ---- 6. Launch visible game ----", 1)[1]
    for secret_name in (
        "WALNUT_DATABASE_URL",
        "WALNUT_AUTH_HMAC_SECRET",
        "WALNUT_FEISHU_PSEUDONYM_SECRET",
        "WALNUT_LLM_RELAY_API_KEY",
        "WALNUT_LLM_RELAY_SERVER_API_KEY",
        "WALNUT_LLM_UPSTREAM_API_KEY",
        "WALNUT_LLM_UPSTREAM_API_KEY_FILE",
    ):
        assert f"'{secret_name}'" in godot_start
    assert "$start.EnvironmentVariables.Remove($secretName)" in godot_start


def test_status_and_stop_require_exact_process_identity_and_preserve_data() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    for required in (
        "if ($Action -eq 'Status')",
        "if ($Action -eq 'Stop')",
        "function Get-RecordedProcess",
        "Get-CimInstance Win32_Process",
        "[DateTimeOffset]::Parse([string]$State.$StartedAtName)",
        "Recorded PID $processId start time changed",
        "function Get-RecordedListenerProcess",
        "listener is not the recorded launcher or its direct child",
        "function Stop-ProcessAndWait",
        "$Process.WaitForExit(10000)",
        "Stop-ProcessAndWait -Process $processes.learner",
        "Stop-ProcessAndWait -Process $processes.worker",
        "Stop-ProcessAndWait -Process $processes.gateway",
        "Stop-ProcessAndWait -Process $processes.relay",
        "& docker stop $postgresName",
        '"volume_preserved":true',
        "ConvertFrom-Json -DateKind String",
    ):
        assert required in script


def test_run_directory_acl_is_idempotent_and_persists_only_the_dacl() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    protect = (
        "function Protect-RunDirectory"
        + script.split("function Protect-RunDirectory", 1)[1].split(
            "function Read-PersistentState", 1
        )[0]
    )

    for required in (
        "[Security.AccessControl.AccessControlSections]::Access",
        "$acl.AreAccessRulesProtected",
        "$rules.Count -eq $expectedSidValues.Count",
        "-not $_.IsInherited",
        "[Security.AccessControl.FileSystemRights]::FullControl",
        "$matchingRules.Count -ne 1",
        "[void]$acl.RemoveAccessRuleSpecific($rule)",
        "[IO.Directory]::SetAccessControl($Path, $acl)",
        "[IO.FileSystemAclExtensions]::SetAccessControl($directory, $acl)",
    ):
        assert required in protect

    fast_return = protect.index("if ($isExact) {\n        return")
    desktop_write = protect.index("[IO.Directory]::SetAccessControl($Path, $acl)")
    core_write = protect.index("[IO.FileSystemAclExtensions]::SetAccessControl($directory, $acl)")
    assert fast_return < desktop_write
    assert fast_return < core_write
    assert "Set-Acl" not in protect


def test_workers_are_recorded_health_checked_and_duplicate_start_is_blocked() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    for required in (
        "Test-StateHasRecordedCoreIdentity -State $state",
        "Persistent-play runtime is incomplete; use -Action Stop before restarting it.",
        "PERSISTENT_PLAY_ALREADY_RUNNING",
        "Start-ProviderBlindBackendChild",
        "'-m', 'walnut_backend.worker_main'",
        "'-m', 'walnut_backend.learner_worker_main'",
        "Persistent-play $($component.Name) process is not healthy after startup.",
        "Set-StateValue -State $state -Name 'worker_pid'",
        "Set-StateValue -State $state -Name 'worker_started_at'",
        "Set-StateValue -State $state -Name 'learner_pid'",
        "Set-StateValue -State $state -Name 'learner_started_at'",
        "Protect-RunDirectory -Path $runtimeRoot",
    ):
        assert required in script


def test_configuration_validation_precedes_state_mutation_and_reuse():
    script = SCRIPT.read_text(encoding="utf-8")
    assert script.index("-m walnut_backend.voice_preflight") < script.index(
        "$state = Read-PersistentState"
    )
    assert script.index("Voice configuration changed or was not verified") < script.index(
        "PERSISTENT_PLAY_ALREADY_RUNNING"
    )
    assert (
        "-Name 'voice_configuration_sha256' -Value $voiceConfiguration.configuration_sha256"
        in script
    )
    assert script.index("-m walnut_backend.play_preflight") < script.index(
        "$state = Read-PersistentState"
    )
    assert script.index("Test-WalnutRuntime -GodotExe") < script.index(
        "$state = Read-PersistentState"
    )


def test_separate_instance_cannot_use_player_ports_or_state():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "An isolated instance requires three dedicated non-player ports." in script
    assert '"walnut-$InstanceName-postgres"' in script
    assert '"walnut-$InstanceName-pgdata"' in script
    assert '"WalnutWorld\\$InstanceName"' in script
    assert "if ($NoGame)" in script


def test_seed_refusal_requires_independent_current_watering_authority_proof() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    authority_setup = script.split(
        "# ---- 3. Migrate + seed, then independently prove current watering authority ----",
        1,
    )[1].split(
        "# ---- 4. Recoverable relay (real Provider, file-only credential injection) ----",
        1,
    )[0]

    assert "-m walnut_backend.persistent_play_authority" in authority_setup
    assert "PERSISTENT_WATERING_AUTHORITY_INVALID code=([A-Z0-9_]+)" in authority_setup
    assert "Current watering authority verification failed: $authorityReason" in authority_setup
    assert "CURRENT_WATERING_AUTHORITY_VALID" in authority_setup
    assert "[int]$authority.authority_rows -ne 7" in authority_setup
    assert "[bool]$authority.read_only -ne $true" in authority_setup
    assert "already-seeded (current watering authority verified)" in authority_setup
    assert "already-seeded (authority exists)" not in script
    assert "Authority seed failed: $seedOutput" not in script
    assert "sensitive output withheld" in authority_setup
    assert "$seedOutput = $null" in authority_setup


def test_authority_failure_stops_only_postgres_started_by_this_invocation() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    authority_setup = script.split(
        "# ---- 3. Migrate + seed, then independently prove current watering authority ----",
        1,
    )[1].split(
        "# ---- 4. Recoverable relay (real Provider, file-only credential injection) ----",
        1,
    )[0]

    assert "$postgresStartedThisInvocation = $false" in script
    assert script.count("$postgresStartedThisInvocation = $true") == 2
    assert (
        "if ($postgresStartedThisInvocation -and (Test-ScopedPostgresRunning))" in authority_setup
    )
    assert "& docker stop $postgresName" in authority_setup
