[CmdletBinding()]
param(
    [string]$GodotExe = $env:GODOT_EXE,
    [Parameter(Mandatory = $true)]
    [string]$PostgresImage,
    [Parameter(Mandatory = $true)]
    [string]$SandboxImage,
    [string]$Model = 'deepseek-v4-flash',
    [string]$Provider = 'deepseek',
    [string]$UpstreamEndpoint = 'https://api.deepseek.com/chat/completions',
    [long]$MinimumFreeMemoryBytes = 1073741824,
    [long]$MinimumFreeDiskBytes = 4294967296,
    [int]$TotalDeadlineSeconds = 720,
    [switch]$EnableWorldPresentation = $true,
    [switch]$EnableSkillPatch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($EnableSkillPatch -and -not $EnableWorldPresentation) {
    throw 'Formal Skill Patch acceptance requires -EnableWorldPresentation.'
}

function Test-DigestPinnedImage([string]$Value) {
    return $Value -cmatch '^[a-z0-9][a-z0-9._/-]*(?::[a-z0-9][a-z0-9._-]*)?@sha256:[a-f0-9]{64}$'
}

if ($env:WALNUT_INT1_REAL_PROVIDER_E2E -ne 'true') {
    Write-Output 'INT1_REAL_PROVIDER_E2E_NOT_LIVE {"status":"NOT_LIVE","reason":"explicit opt-in is absent"}'
    exit 2
}
if (-not (Test-DigestPinnedImage $PostgresImage) -or -not (Test-DigestPinnedImage $SandboxImage)) {
    Write-Output 'INT1_REAL_PROVIDER_E2E_NOT_LIVE {"status":"NOT_LIVE","reason":"PostgreSQL and Sandbox images must be digest-pinned"}'
    exit 2
}
$hasDirectKey = -not [string]::IsNullOrWhiteSpace($env:WALNUT_LLM_UPSTREAM_API_KEY)
$hasKeyFile = -not [string]::IsNullOrWhiteSpace($env:WALNUT_LLM_UPSTREAM_API_KEY_FILE)
if ($hasDirectKey -eq $hasKeyFile) {
    Write-Output 'INT1_REAL_PROVIDER_E2E_NOT_LIVE {"status":"NOT_LIVE","reason":"set exactly one upstream key source"}'
    exit 2
}

$harness = Join-Path $PSScriptRoot 'run-int1-local-diagnostic.ps1'
$generationLimitName = 'WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS'
$generationLimit = if ($EnableSkillPatch) { 32 } else { 24 }
$originalGenerationLimit = [Environment]::GetEnvironmentVariable($generationLimitName, 'Process')
$harnessExitCode = 1
try {
    # This billable gate, and no general relay default, opts into the hard cap.
    # M1 retains 24; the six-Turn M2 chain is bounded at 32 including its
    # one-generation, zero-tool Patch proposal branch.
    [Environment]::SetEnvironmentVariable($generationLimitName, [string]$generationLimit, 'Process')
    $harnessArguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $harness,
        '-GodotExe', $GodotExe,
        '-PostgresImage', $PostgresImage,
        '-SandboxImage', $SandboxImage,
        '-MinimumFreeMemoryBytes', [string]$MinimumFreeMemoryBytes,
        '-MinimumFreeDiskBytes', [string]$MinimumFreeDiskBytes,
        '-TotalDeadlineSeconds', [string]$TotalDeadlineSeconds,
        '-RealProvider',
        '-RealProviderName', $Provider,
        '-RealProviderModel', $Model,
        '-RealProviderEndpoint', $UpstreamEndpoint
    )
    if ($EnableWorldPresentation) {
        $harnessArguments += '-EnableWorldPresentation'
    }
    if ($EnableSkillPatch) {
        $harnessArguments += '-EnableSkillPatch'
    }
    & powershell.exe @harnessArguments
    $harnessExitCode = $LASTEXITCODE
}
finally {
    [Environment]::SetEnvironmentVariable(
        $generationLimitName,
        $originalGenerationLimit,
        'Process'
    )
}
exit $harnessExitCode
