[CmdletBinding()]
param(
    [switch]$NoStartBackend,
    [ValidateRange(0, 86400)]
    [int]$TokenLifetimeSeconds = 0,
    [string]$GodotExe = $env:GODOT_EXE
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    Add-Type -AssemblyName System.Security
}

$backendRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = Split-Path -Parent $backendRoot
$frontendContainerRoot = Join-Path $workspaceRoot 'walnut-world-frontend'
$nestedFrontendRoot = Join-Path $frontendContainerRoot 'walnut-world-frontend'
if (Test-Path -LiteralPath (Join-Path $nestedFrontendRoot 'project.godot') -PathType Leaf) {
    $frontendRoot = (Resolve-Path -LiteralPath $nestedFrontendRoot).Path
}
else {
    $frontendRoot = $frontendContainerRoot
}
if ([string]::IsNullOrWhiteSpace($GodotExe)) {
    $GodotExe = Join-Path $workspaceRoot 'tools\godot-4.5.2\Godot_v4.5.2-stable_win64.exe'
}
$backendLauncher = Join-Path $PSScriptRoot 'run-int3-aily-backend.ps1'
$statePath = Join-Path $env:LOCALAPPDATA 'WalnutWorld\int3-aily-backend\active.json'

function ConvertTo-Base64Url([byte[]]$Bytes) {
    [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

if (-not (Test-Path -LiteralPath $GodotExe -PathType Leaf)) {
    throw "Godot executable not found: $GodotExe"
}
if (-not (Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort 8790 -ErrorAction SilentlyContinue)) {
    if ($NoStartBackend) { throw 'Backend 8790 is not running.' }
    if ($TokenLifetimeSeconds -gt 0) {
        & $backendLauncher -Action Start -TokenLifetimeSeconds $TokenLifetimeSeconds | Out-Null
    }
    else {
        & $backendLauncher -Action Start | Out-Null
    }
}
if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    throw 'Backend runtime state is unavailable; start the INT3 Backend first.'
}

$cipher = $null
$plain = $null
$secretBytes = $null
$inputBytes = $null
$signature = $null
$hmac = $null
try {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $keyPath = Join-Path ([string]$state.run_directory) 'auth-hmac.dpapi'
    $cipher = [IO.File]::ReadAllBytes($keyPath)
    $plain = [Security.Cryptography.ProtectedData]::Unprotect(
        $cipher, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $secret = [Text.Encoding]::UTF8.GetString($plain)
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $maximumLifetimeSeconds = 900
    if ($state.PSObject.Properties.Name -contains 'maximum_jwt_lifetime_seconds') {
        $maximumLifetimeSeconds = [int]$state.maximum_jwt_lifetime_seconds
    }
    if ($TokenLifetimeSeconds -gt 0) {
        if ($TokenLifetimeSeconds -gt $maximumLifetimeSeconds) {
            throw "Requested student token lifetime ${TokenLifetimeSeconds}s exceeds the running Gateway maximum ${maximumLifetimeSeconds}s. Stop the backend and start it with -TokenLifetimeSeconds $TokenLifetimeSeconds."
        }
        $gameTokenLifetimeSeconds = $TokenLifetimeSeconds
    }
    else {
        $gameTokenLifetimeSeconds = [Math]::Max(60, $maximumLifetimeSeconds - 60)
    }
    $header = ConvertTo-Base64Url ([Text.Encoding]::UTF8.GetBytes((@{ alg = 'HS256'; typ = 'JWT' } | ConvertTo-Json -Compress)))
    $claims = [ordered]@{
        iss = [string]$state.issuer; aud = [string]$state.audience
        sub = 'student_0001'; tenant_id = 'tenant_yaya'; actor_id = 'student_0001'
        actor_type = 'student'; roles = @('game:player'); iat = $now; nbf = $now; exp = $now + $gameTokenLifetimeSeconds
    } | ConvertTo-Json -Compress
    $payload = ConvertTo-Base64Url ([Text.Encoding]::UTF8.GetBytes($claims))
    $unsigned = "$header.$payload"
    $secretBytes = [Text.Encoding]::UTF8.GetBytes($secret)
    $inputBytes = [Text.Encoding]::ASCII.GetBytes($unsigned)
    $hmac = [Security.Cryptography.HMACSHA256]::new($secretBytes)
    $signature = $hmac.ComputeHash($inputBytes)
    $token = "$unsigned.$(ConvertTo-Base64Url $signature)"

    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $GodotExe
    $start.Arguments = "--path `"$frontendRoot`""
    $start.WorkingDirectory = $frontendRoot
    $start.UseShellExecute = $false
    $start.EnvironmentVariables['YAYA_API_BASE_URL'] = 'http://127.0.0.1:8790'
    $start.EnvironmentVariables['YAYA_AUTH_TOKEN'] = $token
    $game = [Diagnostics.Process]::Start($start)
    Start-Sleep -Seconds 2
    if ($game.HasExited) { throw "Godot exited during launch with code $($game.ExitCode)." }
    Write-Output "INT3_GAME_STARTED pid=$($game.Id) token_lifetime=$gameTokenLifetimeSeconds"
}
finally {
    if ($null -ne $hmac) { $hmac.Dispose() }
    foreach ($bytes in @($cipher, $plain, $secretBytes, $inputBytes, $signature)) {
        if ($null -ne $bytes) { [Array]::Clear($bytes, 0, $bytes.Length) }
    }
    $token = $null
    $secret = $null
    $unsigned = $null
}
