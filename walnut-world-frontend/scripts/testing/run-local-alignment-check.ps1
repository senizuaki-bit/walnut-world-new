[CmdletBinding()]
param(
    [string]$GodotExe = $env:GODOT_EXE,
    [string]$FingerprintPath,
    [switch]$CurrentSessionSmoke,
    [switch]$SnapshotOnly,
    [switch]$RecoveryOnly,
    [switch]$EnableWorldPresentation,
    [switch]$EnableSkillPatch
)

$ErrorActionPreference = 'Stop'
# Test-only credential bridge for the existing local persistent-play service.
# Never print or persist the signing key or the generated student token.
$runtimeStatePath = Join-Path $env:LOCALAPPDATA 'WalnutWorld/persistent-play/state.json'
$runtimeState = Get-Content -LiteralPath $runtimeStatePath -Raw | ConvertFrom-Json
function ConvertTo-UrlBase64([byte[]]$Bytes) {
    [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$header = @{alg='HS256';typ='JWT'} | ConvertTo-Json -Compress
$claims = @{
    iss='walnut-int1-local-diagnostic';aud='walnut-game-client'
    sub='student_0001';tenant_id='tenant_yaya';actor_id='student_0001'
    actor_type='student';roles=@('game:player');iat=$now;nbf=$now;exp=$now+3600
} | ConvertTo-Json -Compress
$unsigned = (ConvertTo-UrlBase64 ([Text.Encoding]::UTF8.GetBytes($header))) + '.' + (ConvertTo-UrlBase64 ([Text.Encoding]::UTF8.GetBytes($claims)))
$signer = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes([string]$runtimeState.auth_secret))
$previousToken = $env:YAYA_AUTH_TOKEN
$previousBase = $env:YAYA_API_BASE_URL
$alignmentEnvironment = @{}
foreach ($name in @('WALNUT_LIVE_ALIGNMENT', 'WALNUT_LIVE_ALIGNMENT_MODE', 'WALNUT_LIVE_ALIGNMENT_FINGERPRINT')) {
    $alignmentEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $env:YAYA_AUTH_TOKEN = $unsigned + '.' + (ConvertTo-UrlBase64 ($signer.ComputeHash([Text.Encoding]::ASCII.GetBytes($unsigned))))
    $env:YAYA_API_BASE_URL = 'http://127.0.0.1:8790'
    if ([string]::IsNullOrWhiteSpace($GodotExe)) {
        $GodotExe = (Get-Command Godot_v4.7.1-stable_win64_console.exe).Source
    }
    if ($CurrentSessionSmoke) {
        $env:WALNUT_LIVE_ALIGNMENT = '1'
        $env:WALNUT_LIVE_ALIGNMENT_MODE = if ($SnapshotOnly) { 'snapshot' } elseif ($RecoveryOnly) { 'recovery' } else { 'full' }
        $env:WALNUT_LIVE_ALIGNMENT_FINGERPRINT = $FingerprintPath
        & $GodotExe --headless --path (Join-Path $PSScriptRoot '../..') --script res://scripts/testing/current_session_live_test.gd
        if ($LASTEXITCODE -ne 0) { throw 'Current-session live validation failed.' }
        return
    }
    if ([string]::IsNullOrWhiteSpace($FingerprintPath)) { throw 'FingerprintPath is required for fresh-authority acceptance.' }
    $arguments = @{
        GodotExe=$GodotExe;TotalDeadlineSeconds=1200;ResourceDeadlineSeconds=360
        InteractionDeadlineSeconds=180;Phase1FingerprintPath=$FingerprintPath
        EnableWorldPresentation=$EnableWorldPresentation;EnableSkillPatch=$EnableSkillPatch
    }
    if ($RecoveryOnly) {
        $arguments.RecoveryOnly = $true
        $arguments.CleanupPersistence = $true
    } else {
        $arguments.ResetPersistence = $true
    }
    & (Join-Path $PSScriptRoot '../run-real-gateway-e2e.ps1') @arguments
} finally {
    $env:YAYA_AUTH_TOKEN = $previousToken
    $env:YAYA_API_BASE_URL = $previousBase
    foreach ($name in $alignmentEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $alignmentEnvironment[$name], 'Process')
    }
    $signer.Dispose()
    $runtimeState = $null
}
