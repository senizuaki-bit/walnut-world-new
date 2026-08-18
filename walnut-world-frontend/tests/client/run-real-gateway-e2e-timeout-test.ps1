[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$runner = Join-Path $projectPath 'scripts\run-real-gateway-e2e.ps1'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ("walnut-real-gateway-timeout-{0}" -f [guid]::NewGuid().ToString('N'))
$fakeGodot = Join-Path $testRoot 'fake-godot.exe'
$fakeExitGodot = Join-Path $testRoot 'fake-godot-exit.exe'
$fakeTaskKill = Join-Path $testRoot 'fake-taskkill-nonzero.exe'
$childPidPath = Join-Path $testRoot 'child.pid'
$nonzeroChildPidPath = Join-Path $testRoot 'nonzero-child.pid'
$fingerprintPath = Join-Path $testRoot 'phase1.json'
$previousBaseUrl = $env:YAYA_API_BASE_URL
$previousAuthToken = $env:YAYA_AUTH_TOKEN
$previousChildPidPath = $env:YAYA_TIMEOUT_TEST_CHILD_PID
$previousExitCode = $env:YAYA_EXIT_TEST_CODE
$previousExitFingerprint = $env:YAYA_EXIT_TEST_FINGERPRINT
$childPid = 0
$nonzeroChildPid = 0
$nonzeroRoot = $null

try {
    [void](New-Item -ItemType Directory -Path $testRoot)
    $fakeSource = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;

public static class FakeGodotTimeoutProcess
{
    public static void Main(string[] args)
    {
        var child = Process.Start(new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments = "-NoProfile -NonInteractive -Command \"Start-Sleep -Seconds 60\"",
            UseShellExecute = false,
            CreateNoWindow = true,
        });
        File.WriteAllText(Environment.GetEnvironmentVariable("YAYA_TIMEOUT_TEST_CHILD_PID"), child.Id.ToString());
        Console.WriteLine("FAKE_GODOT_TIMEOUT_DIAGNOSTIC");
        Console.Out.Flush();
        Thread.Sleep(TimeSpan.FromSeconds(60));
    }
}
'@
    Add-Type -TypeDefinition $fakeSource -Language CSharp -OutputAssembly $fakeGodot -OutputType ConsoleApplication
    $fakeExitSource = @'
using System;

public static class FakeGodotExitProcess
{
    public static int Main(string[] args)
    {
        var exitCode = Int32.Parse(Environment.GetEnvironmentVariable("YAYA_EXIT_TEST_CODE"));
        Console.WriteLine("FAKE_GODOT_EXIT_{0}_DIAGNOSTIC", exitCode);
        if (exitCode == 0)
        {
            Console.WriteLine(
                "REAL_GATEWAY_CHAIN_E2E_PASS " +
                Environment.GetEnvironmentVariable("YAYA_EXIT_TEST_FINGERPRINT")
            );
        }
        Console.Out.Flush();
        return exitCode;
    }
}
'@
    Add-Type -TypeDefinition $fakeExitSource -Language CSharp -OutputAssembly $fakeExitGodot -OutputType ConsoleApplication
    $fakeTaskKillSource = @'
using System;
using System.Diagnostics;

public static class FakeTaskKillNonzero
{
    public static int Main(string[] args)
    {
        for (var index = 0; index + 1 < args.Length; index++)
        {
            if (args[index] == "/PID")
            {
                Process.GetProcessById(Int32.Parse(args[index + 1])).Kill();
                return 9;
            }
        }
        return 10;
    }
}
'@
    Add-Type -TypeDefinition $fakeTaskKillSource -Language CSharp -OutputAssembly $fakeTaskKill -OutputType ConsoleApplication
    $env:YAYA_API_BASE_URL = 'http://127.0.0.1:8790'
    $env:YAYA_AUTH_TOKEN = 'offline-timeout-test-token'
    $env:YAYA_TIMEOUT_TEST_CHILD_PID = $childPidPath

    $captured = @()
    $failedAsExpected = $false
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    try {
        & $runner `
            -GodotExe $fakeGodot `
            -TotalDeadlineSeconds 2 `
            -ResourceDeadlineSeconds 1 `
            -InteractionDeadlineSeconds 1 `
            -Phase1FingerprintPath $fingerprintPath `
            -ResetPersistence *>&1 | ForEach-Object { $captured += [string]$_ }
    }
    catch {
        $captured += [string]$_
        $failedAsExpected = $true
    }
    finally {
        $stopwatch.Stop()
    }

    if (-not $failedAsExpected) {
        throw 'The fake Godot process unexpectedly escaped the external process deadline.'
    }
    $capturedText = $captured -join "`n"
    if ($capturedText -notmatch 'FAKE_GODOT_TIMEOUT_DIAGNOSTIC') {
        throw 'The external timeout did not preserve the spawned process diagnostic output.'
    }
    if ($capturedText -notmatch 'exceeded external process deadline of 7 seconds; the exact spawned process tree was terminated') {
        throw "The external timeout did not fail with its exact bounded process-tree diagnostic.`n$capturedText"
    }
    if ($stopwatch.Elapsed.TotalSeconds -gt 15) {
        throw "The 7-second external timeout returned too late ($($stopwatch.Elapsed.TotalSeconds) seconds)."
    }
    if (-not (Test-Path -LiteralPath $childPidPath -PathType Leaf)) {
        throw 'The fake Godot process did not prove that it spawned a descendant.'
    }
    $childPid = [int](Get-Content -LiteralPath $childPidPath -Raw)
    if ($null -ne (Get-Process -Id $childPid -ErrorAction SilentlyContinue)) {
        throw 'The external timeout left the exact spawned Godot descendant alive.'
    }

    # Load only the process-tree helper so a fake terminator can prove that a
    # non-zero tree result is never masked merely because the root exited.
    $parseTokens = $null
    $parseErrors = $null
    $runnerAst = [Management.Automation.Language.Parser]::ParseFile(
        $runner,
        [ref]$parseTokens,
        [ref]$parseErrors
    )
    if ($parseErrors.Count -ne 0) {
        throw 'The formal runner could not be parsed for focused process-tree coverage.'
    }
    $treeHelperAst = $runnerAst.Find({
        param($node)
        return (
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq 'Stop-VerifiedSpawnedProcessTree'
        )
    }, $true)
    if ($null -eq $treeHelperAst) {
        throw 'The formal runner exposes no verified spawned-process-tree helper.'
    }
    Invoke-Expression $treeHelperAst.Extent.Text

    $env:YAYA_TIMEOUT_TEST_CHILD_PID = $nonzeroChildPidPath
    $nonzeroStdout = Join-Path $testRoot 'nonzero-root.stdout.log'
    $nonzeroStderr = Join-Path $testRoot 'nonzero-root.stderr.log'
    $nonzeroRoot = Start-Process -FilePath $fakeGodot `
        -WindowStyle Hidden `
        -PassThru `
        -RedirectStandardOutput $nonzeroStdout `
        -RedirectStandardError $nonzeroStderr
    $childDeadline = [DateTime]::UtcNow.AddSeconds(5)
    while (-not (Test-Path -LiteralPath $nonzeroChildPidPath -PathType Leaf) -and [DateTime]::UtcNow -lt $childDeadline) {
        Start-Sleep -Milliseconds 50
    }
    if (-not (Test-Path -LiteralPath $nonzeroChildPidPath -PathType Leaf)) {
        throw 'The non-zero taskkill regression fake did not spawn its descendant.'
    }
    $nonzeroChildPid = [int](Get-Content -LiteralPath $nonzeroChildPidPath -Raw)
    $nonzeroFailure = ''
    try {
        Stop-VerifiedSpawnedProcessTree -Process $nonzeroRoot -TaskKillPath $fakeTaskKill
    }
    catch {
        $nonzeroFailure = $_.Exception.Message
    }
    if ($nonzeroFailure -notmatch 'taskkill.exe failed to terminate the exact spawned Godot process tree \(exit 9\)') {
        throw "A non-zero process-tree termination result was masked: $nonzeroFailure"
    }
    if (-not $nonzeroRoot.HasExited) {
        throw 'The non-zero taskkill fake did not establish the root-exited branch.'
    }
    if ($null -eq (Get-Process -Id $nonzeroChildPid -ErrorAction SilentlyContinue)) {
        throw 'The non-zero taskkill fake unexpectedly terminated the descendant needed to prove the partial-tree boundary.'
    }

    $hashA = 'a' * 64
    $hashB = 'b' * 64
    $hashC = 'c' * 64
    $validFingerprint = [ordered]@{
        phase1_fingerprint_schema = '1.0.0'
        api_store_closure = [ordered]@{
            draft_cas_performed = $true
            failure_chain_closed = $true
            correction_draft_cas_performed = $true
            patch_decision_performed = $false
            second_build_performed = $true
            build_performed = $true
            run_closed = $true
        }
        persistence_identity = '0123456789abcdef'
        starter_draft_revision = 1
        failure_draft_revision = 2
        saved_draft_revision = 3
        starter_workspace_revision = 1
        failure_workspace_revision = 2
        saved_workspace_revision = 3
        final_workspace_revision = 3
        final_workspace_sha256 = $hashA
        failure_draft_source_sha256 = $hashA
        failure_draft_sha256 = $hashB
        draft_source_sha256 = $hashB
        draft_sha256 = $hashC
        failure_build_source_sha256 = $hashA
        build_source_sha256 = $hashB
        active_skill_tuple_sha256 = $hashC
        active_skill_tuple = @{ registry_revision = 2 }
        failure_reason = 'TASK_INCOMPLETE'
        build_ids = @('build_1', 'build_2')
        activation_ids = @('activation_1', 'activation_2')
        turn_ids = @('turn_1', 'turn_2', 'turn_3', 'turn_4')
        command_ids = @('command_1', 'command_2', 'command_3', 'command_4')
        run_ids = @('run_1', 'run_2', 'run_3', 'run_4')
        interaction_ids = @('interaction_1', 'interaction_2', 'interaction_3', 'interaction_4')
        evidence_ids = @('evidence_1', 'evidence_2', 'evidence_3', 'evidence_4', 'evidence_5')
        evidence_count = 5
        interaction_roles = @('teaching_agent', 'teaching_agent', 'bug_agent', 'book_agent')
        command_statuses = @('REJECTED', 'REJECTED', 'REJECTED', 'APPLIED')
        run_statuses = @('REJECTED', 'REJECTED', 'REJECTED', 'SUCCEEDED')
        interaction_role = 'book_agent'
        interaction_sequence = 4
        transport_attempt_audit = [ordered]@{
            total_started = 12
            total_completed = 12
            method_counts = @{ GET = 1; POST = 9; PUT = 2; PATCH = 0; DELETE = 0 }
            operation_counts = @{
                create_agent_session = 1
                upsert_product_skill_draft = 2
                submit_skill_build = 2
                activate_skill_version = 2
                submit_agent_turn = 4
                record_product_patch_decision = 0
            }
        }
        persistence_sha256 = $hashA
        live_pending_response_loss = @{ status = 'NOT_PROVEN' }
        ui_display = @{
            crop_adaptive_watering_demo = $true
            crop_agent_bridge = $true
            run_button = $true
            content_draft_interaction_snapshot = $true
        }
        persistence_reset_performed = $true
        persistence_reset_residual_count = 0
    } | ConvertTo-Json -Depth 10 -Compress

    $successFingerprintPath = Join-Path $testRoot 'exit-zero-phase1.json'
    $env:YAYA_EXIT_TEST_CODE = '0'
    $env:YAYA_EXIT_TEST_FINGERPRINT = $validFingerprint
    $successCaptured = @()
    try {
        & $runner `
            -GodotExe $fakeExitGodot `
            -TotalDeadlineSeconds 2 `
            -ResourceDeadlineSeconds 1 `
            -InteractionDeadlineSeconds 1 `
            -Phase1FingerprintPath $successFingerprintPath `
            -ResetPersistence *>&1 | ForEach-Object { $successCaptured += [string]$_ }
    }
    catch {
        $successCaptured += [string]$_
        throw "A short fake Godot exit 0 was not accepted by the formal runner.`n$($successCaptured -join "`n")"
    }
    $successCapturedText = $successCaptured -join "`n"
    if ($successCapturedText -notmatch 'FAKE_GODOT_EXIT_0_DIAGNOSTIC') {
        throw 'The formal runner did not preserve output from a short fake Godot exit 0.'
    }
    if (-not (Test-Path -LiteralPath $successFingerprintPath -PathType Leaf)) {
        throw 'The formal runner did not complete its validated exit-0 path.'
    }

    $nonzeroFingerprintPath = Join-Path $testRoot 'exit-23-phase1.json'
    $env:YAYA_EXIT_TEST_CODE = '23'
    $nonzeroCaptured = @()
    $nonzeroFailedAsExpected = $false
    try {
        & $runner `
            -GodotExe $fakeExitGodot `
            -TotalDeadlineSeconds 2 `
            -ResourceDeadlineSeconds 1 `
            -InteractionDeadlineSeconds 1 `
            -Phase1FingerprintPath $nonzeroFingerprintPath `
            -ResetPersistence *>&1 | ForEach-Object { $nonzeroCaptured += [string]$_ }
    }
    catch {
        $nonzeroCaptured += [string]$_
        $nonzeroFailedAsExpected = $true
    }
    $nonzeroCapturedText = $nonzeroCaptured -join "`n"
    if (-not $nonzeroFailedAsExpected) {
        throw 'A short fake Godot non-zero exit unexpectedly passed the formal runner.'
    }
    if ($nonzeroCapturedText -notmatch 'FAKE_GODOT_EXIT_23_DIAGNOSTIC') {
        throw 'The formal runner did not preserve output from a short fake Godot non-zero exit.'
    }
    if ($nonzeroCapturedText -notmatch 'Real Gateway Godot E2E failed with exit code 23\.') {
        throw "The formal runner did not fail loudly with the exact fake Godot exit code 23.`n$nonzeroCapturedText"
    }

    Write-Host 'RUN_REAL_GATEWAY_E2E_TIMEOUT_TEST_PASS'
}
finally {
    $env:YAYA_API_BASE_URL = $previousBaseUrl
    $env:YAYA_AUTH_TOKEN = $previousAuthToken
    $env:YAYA_TIMEOUT_TEST_CHILD_PID = $previousChildPidPath
    $env:YAYA_EXIT_TEST_CODE = $previousExitCode
    $env:YAYA_EXIT_TEST_FINGERPRINT = $previousExitFingerprint
    if ($childPid -gt 0) {
        Stop-Process -Id $childPid -Force -ErrorAction SilentlyContinue
    }
    if ($nonzeroChildPid -gt 0) {
        Stop-Process -Id $nonzeroChildPid -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $nonzeroRoot -and -not $nonzeroRoot.HasExited) {
        Stop-Process -Id $nonzeroRoot.Id -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $testRoot -PathType Container) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
