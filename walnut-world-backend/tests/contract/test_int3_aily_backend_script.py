"""Static Windows gates for the persistent INT3 Gateway launcher."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND_ROOT / "scripts" / "run-int3-aily-backend.ps1"


def test_int3_aily_launcher_powershell_syntax_is_valid() -> None:
    environment = os.environ.copy()
    environment["WALNUT_TEST_INT3_AILY_SCRIPT"] = str(SCRIPT)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$errors=$null; "
                "[Management.Automation.Language.Parser]::ParseFile("
                "$env:WALNUT_TEST_INT3_AILY_SCRIPT,[ref]$null,[ref]$errors)|Out-Null; "
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


def test_gateway_only_mode_is_explicit_and_does_not_require_a_provider_key() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "[switch]$GatewayOnly" in script
    assert "if (-not $GatewayOnly)" in script
    assert "runtime_mode = if ($GatewayOnly) { 'GATEWAY_ONLY' } else { 'FULL_STACK' }" in script
    assert "provider_started = -not $GatewayOnly" in script
    assert "WALNUT_LLM_UPSTREAM_API_KEY = ''" in script
    assert "WALNUT_LLM_UPSTREAM_API_KEY_FILE = ''" in script
    assert (
        "[Environment]::SetEnvironmentVariable(\n"
        "                        'WALNUT_LLM_UPSTREAM_API_KEY_FILE', $LlmUpstreamKeyFile"
    ) in script
    assert script.index("if (-not $GatewayOnly) {") < script.index(
        "LLM upstream key file is missing"
    )
    assert "Join-Path $agentPath 'walnut-llm-api-key.key'" not in script
    assert "Get-Content -LiteralPath $LlmUpstreamKeyFile" not in script


def test_stop_waits_for_owned_processes_and_retries_only_the_scoped_run_directory() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    for required in (
        "Assert-SafeRunDirectory -Path $RunDirectory",
        "for ($attempt = 1; $attempt -le 40; $attempt++)",
        "Start-Sleep -Milliseconds 250",
        "function Get-ExpectedRuntimeChild",
        "Recorded $PidKey start time changed",
        "function Stop-ProcessAndWait",
        "$Process.WaitForExit(10000)",
        "Stop-ProcessAndWait -Process $learnerProcess",
        "Stop-ProcessAndWait -Process $workerProcess",
        "Stop-ProcessAndWait -Process $relayProcess",
        "Stop-ProcessAndWait -Process $listenerProcess",
        "Stop-ProcessAndWait -Process $process",
    ):
        assert required in script

    safe = script.index("$safeDirectory = Assert-SafeRunDirectory -Path $RunDirectory")
    retry = script.index("for ($attempt = 1; $attempt -le 40; $attempt++)", safe)
    removal = script.index("Remove-Item -LiteralPath $safeDirectory", retry)
    assert safe < retry < removal


def test_windows_powershell_51_compatibility_is_kept_at_the_http_and_dpapi_edges() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    for required in (
        "Add-Type -AssemblyName System.Security",
        "(Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')",
        "(Get-Command Invoke-WebRequest).Parameters",
        "if ($availableParameters.ContainsKey('NoProxy'))",
        "if ($availableParameters.ContainsKey('SkipHttpErrorCheck'))",
        "$content = [string]$_.ErrorDetails.Message",
        "$invokeParameters.TimeoutSec = 2",
    ):
        assert required in script
