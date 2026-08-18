[CmdletBinding()]
param(
    [string]$GodotExe = $env:GODOT_EXE
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($GodotExe)) {
    $bundledCandidates = @(
        (Join-Path (Split-Path -Parent $projectPath) 'tools\godot-4.5.2\Godot_v4.5.2-stable_win64_console.exe'),
        (Join-Path $projectPath '..\..\tools\godot-4.5.2\Godot_v4.5.2-stable_win64_console.exe')
    )
    foreach ($bundledCandidate in $bundledCandidates) {
        if (Test-Path -LiteralPath $bundledCandidate -PathType Leaf) {
            $GodotExe = (Resolve-Path -LiteralPath $bundledCandidate).Path
            break
        }
    }
}
if ([string]::IsNullOrWhiteSpace($GodotExe) -or -not (Test-Path -LiteralPath $GodotExe)) {
    throw 'Set GODOT_EXE or pass -GodotExe with the Godot 4.5.2 console executable.'
}

$realOptInTests = @(
    'tests/client/real_gateway_chain_e2e_test.gd',
    'tests/client/real_gateway_chain_recovery_e2e_test.gd'
)
$nonTestHelpers = @(
    'tests/level_demo/capture_horizontal_watering_demo.gd',
    'tests/level_demo/capture_crop_adaptive_demo.gd'
)
$allScripts = @(Get-ChildItem -LiteralPath (Join-Path $projectPath 'tests') -Recurse -File -Filter '*.gd' | Sort-Object FullName)
$offlineTests = @()
$realOptInMatches = @{}
foreach ($path in $realOptInTests) {
    $realOptInMatches[$path] = 0
}
foreach ($script in $allScripts) {
    $relative = $script.FullName.Substring($projectPath.Length + 1).Replace('\', '/')
    if ($realOptInTests -contains $relative) {
        $realOptInMatches[$relative] += 1
        continue
    }
    if ($nonTestHelpers -contains $relative) {
        continue
    }
    $offlineTests += [pscustomobject]@{ Path = $script.FullName; Resource = "res://$relative" }
}
foreach ($path in $realOptInTests) {
    if ($realOptInMatches[$path] -ne 1) {
        throw "Expected exactly one real opt-in test at $path; found $($realOptInMatches[$path])."
    }
}

$agentRepositoryEnvironmentName = 'YAYA_AGENT_REPOSITORY_ROOT'
$previousAgentRepositoryRoot = [Environment]::GetEnvironmentVariable($agentRepositoryEnvironmentName, 'Process')
$agentRepositoryCandidates = if ([string]::IsNullOrWhiteSpace($previousAgentRepositoryRoot)) {
    @(
        (Join-Path $projectPath '..\agent'),
        (Join-Path $projectPath '..\..\agent'),
        (Join-Path $projectPath '..\..\walnut-world-backend\agent')
    )
}
else {
    @($previousAgentRepositoryRoot)
}
$resolvedAgentRepositoryRoot = $null
foreach ($candidate in $agentRepositoryCandidates) {
    $candidatePath = if ([IO.Path]::IsPathRooted($candidate)) {
        [IO.Path]::GetFullPath($candidate)
    }
    else {
        [IO.Path]::GetFullPath((Join-Path $projectPath $candidate))
    }
    if (Test-Path -LiteralPath (Join-Path $candidatePath 'contracts\manifest.json') -PathType Leaf) {
        $resolvedAgentRepositoryRoot = $candidatePath
        break
    }
}
if ($null -eq $resolvedAgentRepositoryRoot) {
    throw 'Agent contracts are unavailable. Set YAYA_AGENT_REPOSITORY_ROOT or provide the sibling/combined workspace Agent repository.'
}
[Environment]::SetEnvironmentVariable($agentRepositoryEnvironmentName, $resolvedAgentRepositoryRoot, 'Process')

try {
$realReport = @($realOptInTests | ForEach-Object {
    [ordered]@{
        path = $_
        suite = 'REAL_GATEWAY_OPT_IN'
        executed = $false
        status = 'EXCLUDED_NOT_RUN'
        runner = 'scripts/run-real-gateway-e2e.ps1'
    }
})
Write-Host ("REAL_OPT_IN_TEST_REPORT " + ($realReport | ConvertTo-Json -Compress))

$passed = 0
$failures = @()
foreach ($test in $offlineTests) {
    Write-Host "OFFLINE_TEST_START $($test.Resource)"
    # Windows PowerShell promotes native stderr to ErrorRecord.  Collect it as
    # test evidence without letting one process bypass the aggregate summary.
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $GodotExe --headless --path $projectPath --script $test.Resource 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = 'Stop'
    }
    $output | ForEach-Object { Write-Host ([string]$_) }
    if ($exitCode -eq 0) {
        $passed += 1
        Write-Host "OFFLINE_TEST_PASS $($test.Resource)"
    }
    else {
        $failures += [pscustomobject]@{ path = $test.Resource; exit_code = $exitCode }
        Write-Host "OFFLINE_TEST_FAIL $($test.Resource) exit_code=$exitCode"
    }
}

$summary = [ordered]@{
    suite = 'OFFLINE'
    discovered = $offlineTests.Count
    executed = $offlineTests.Count
    passed = $passed
    failed = $failures.Count
    skipped = 0
    real_opt_in = $realReport
}
Write-Host ("OFFLINE_TEST_SUMMARY " + ($summary | ConvertTo-Json -Compress -Depth 4))
if ($failures.Count -ne 0) {
    throw "$($failures.Count) offline Godot test(s) failed."
}
}
finally {
    [Environment]::SetEnvironmentVariable($agentRepositoryEnvironmentName, $previousAgentRepositoryRoot, 'Process')
}
