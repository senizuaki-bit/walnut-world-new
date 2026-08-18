[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 18792,
    [string]$PythonExe
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    Add-Type -AssemblyName System.Security
}
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$BackendPort = 8790
$LoopbackAddress = [System.Net.IPAddress]::Loopback
$RuntimeRoot = Join-Path (
    [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
) "WalnutWorld\int3-aily-backend"
$ActiveStatePath = Join-Path $RuntimeRoot "active.json"
$ExpectedRoles = @("learner:read", "class-insights:read", "evidence:read")

if ($Port -eq $BackendPort) {
    throw "Proxy port must differ from the fixed Backend port $BackendPort."
}

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = Join-Path $RepositoryRoot ".venv\Scripts\python.exe"
}
$PythonExe = [System.IO.Path]::GetFullPath($PythonExe)
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Repository Python runtime was not found: $PythonExe"
}

function Test-LoopbackListener {
    param(
        [Parameter(Mandatory = $true)]
        [int]$TargetPort
    )

    $Client = [System.Net.Sockets.TcpClient]::new()
    try {
        $ConnectTask = $Client.ConnectAsync($LoopbackAddress, $TargetPort)
        if (-not $ConnectTask.Wait(1000)) {
            return $false
        }
        return $Client.Connected
    }
    catch {
        return $false
    }
    finally {
        $Client.Dispose()
    }
}

if (-not (Test-LoopbackListener -TargetPort $BackendPort)) {
    throw "Authoritative Backend is not listening on 127.0.0.1:$BackendPort."
}
if (Test-LoopbackListener -TargetPort $Port) {
    throw "Refusing to reuse occupied loopback proxy port 127.0.0.1:$Port."
}

if (-not (Test-Path -LiteralPath $ActiveStatePath -PathType Leaf)) {
    throw "The production-authenticated INT3 Backend runtime is not active."
}
$RuntimeState = Get-Content -LiteralPath $ActiveStatePath -Raw -Encoding utf8 |
    ConvertFrom-Json -DateKind String
$ResolvedRuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd("\") + "\"
$RunDirectory = [IO.Path]::GetFullPath([string]$RuntimeState.run_directory)
if (
    -not $RunDirectory.StartsWith($ResolvedRuntimeRoot, [StringComparison]::OrdinalIgnoreCase) -or
    [IO.Path]::GetFileName($RunDirectory) -notmatch '^run-[a-f0-9]{32}$'
) {
    throw "Backend runtime state points outside its scoped credential directory."
}
if (
    [int]$RuntimeState.backend_port -ne $BackendPort -or
    [bool]$RuntimeState.development_auth -or
    [string]$RuntimeState.tenant_id -cne "tenant_yaya" -or
    [string]$RuntimeState.actor_type -cne "teacher" -or
    [int]$RuntimeState.maximum_jwt_lifetime_seconds -ne 900
) {
    throw "Backend runtime identity or production-authentication policy is invalid."
}
$ActualRoles = @($RuntimeState.roles | ForEach-Object { [string]$_ } | Sort-Object)
$SortedExpectedRoles = @($ExpectedRoles | Sort-Object)
if (($ActualRoles -join "`n") -cne ($SortedExpectedRoles -join "`n")) {
    throw "Backend runtime does not have exactly the three teacher read scopes."
}
$BackendProcessId = [int]$RuntimeState.backend_pid
$BackendListenerProcessId = if (
    $RuntimeState.PSObject.Properties.Name -contains 'backend_listener_pid'
) {
    [int]$RuntimeState.backend_listener_pid
}
else {
    $BackendProcessId
}
$BackendListener = Get-NetTCPConnection -State Listen -LocalAddress "127.0.0.1" `
    -LocalPort $BackendPort -ErrorAction SilentlyContinue
if ($null -eq $BackendListener -or $BackendListener.OwningProcess -ne $BackendListenerProcessId) {
    throw "Backend runtime state does not own 127.0.0.1:$BackendPort."
}
$BackendProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $BackendProcessId"
if (
    $null -eq $BackendProcess -or
    $BackendProcess.CommandLine -notlike '*uvicorn*walnut_backend.main:app*' -or
    $BackendProcess.CommandLine -notlike "*--port $BackendPort*"
) {
    throw "Backend runtime PID is not the expected authoritative Gateway process."
}
$ListenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $BackendListenerProcessId"
if (
    $null -eq $ListenerProcess -or
    (
        $BackendListenerProcessId -ne $BackendProcessId -and
        [int]$ListenerProcess.ParentProcessId -ne $BackendProcessId
    ) -or
    $ListenerProcess.CommandLine -notlike '*uvicorn*walnut_backend.main:app*'
) {
    throw "Backend listener is not the scoped authoritative Gateway process."
}

$ProtectedHmacPath = Join-Path $RunDirectory "auth-hmac.dpapi"
if (-not (Test-Path -LiteralPath $ProtectedHmacPath -PathType Leaf)) {
    throw "The DPAPI-protected Backend signing key is unavailable."
}

function Get-OrCreateStableCapabilityPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProtectedPath
    )

    $protectedBytes = $null
    $plainBytes = $null
    $newBytes = $null
    try {
        if (Test-Path -LiteralPath $ProtectedPath -PathType Leaf) {
            $protectedBytes = [IO.File]::ReadAllBytes($ProtectedPath)
            $plainBytes = [Security.Cryptography.ProtectedData]::Unprotect(
                $protectedBytes,
                $null,
                [Security.Cryptography.DataProtectionScope]::CurrentUser
            )
            $existingPath = [Text.Encoding]::UTF8.GetString($plainBytes)
            if ($existingPath -notmatch '^/mcp/[A-Za-z0-9_-]{43,128}$') {
                throw "The protected MCP capability path is malformed."
            }
            return $existingPath
        }

        $newBytes = [byte[]]::new(48)
        [Security.Cryptography.RandomNumberGenerator]::Fill($newBytes)
        $newPath = "/mcp/$([Convert]::ToBase64String($newBytes).TrimEnd("=").Replace("+", "-").Replace("/", "_"))"
        $encodedPath = [Text.Encoding]::UTF8.GetBytes($newPath)
        try {
            $protectedPathBytes = [Security.Cryptography.ProtectedData]::Protect(
                $encodedPath,
                $null,
                [Security.Cryptography.DataProtectionScope]::CurrentUser
            )
            try {
                [IO.File]::WriteAllBytes($ProtectedPath, $protectedPathBytes)
            }
            finally {
                [Array]::Clear($protectedPathBytes, 0, $protectedPathBytes.Length)
            }
        }
        finally {
            [Array]::Clear($encodedPath, 0, $encodedPath.Length)
        }
        return $newPath
    }
    finally {
        if ($null -ne $protectedBytes) {
            [Array]::Clear($protectedBytes, 0, $protectedBytes.Length)
        }
        if ($null -ne $plainBytes) {
            [Array]::Clear($plainBytes, 0, $plainBytes.Length)
        }
        if ($null -ne $newBytes) {
            [Array]::Clear($newBytes, 0, $newBytes.Length)
        }
    }
}

$ProtectedBytes = [IO.File]::ReadAllBytes($ProtectedHmacPath)
$PlainBytes = $null
$HmacSecret = $null
$Capability = $null
$CapabilityPath = $null
$PreviousHmacSecret = $null
$PreviousCapability = $null
$PreviousIssuer = $null
$PreviousAudience = $null
$PreviousTenantId = $null
$PreviousActorId = $null
try {
    $PlainBytes = [Security.Cryptography.ProtectedData]::Unprotect(
        $ProtectedBytes,
        $null,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $HmacSecret = [Text.Encoding]::UTF8.GetString($PlainBytes)
    if ([string]::IsNullOrWhiteSpace($HmacSecret)) {
        throw "The protected Backend signing key is malformed."
    }
    $CapabilityPath = Get-OrCreateStableCapabilityPath -ProtectedPath (
        Join-Path $RunDirectory "edge-capability-path.dpapi"
    )

    $Entrypoint = Join-Path $PSScriptRoot "int3_mcp_edge_proxy.py"
    $PreviousHmacSecret = [Environment]::GetEnvironmentVariable(
        "WALNUT_INT3_EDGE_HMAC_SECRET",
        "Process"
    )
    $PreviousCapability = [Environment]::GetEnvironmentVariable(
        "WALNUT_INT3_EDGE_CAPABILITY_PATH",
        "Process"
    )
    $PreviousIssuer = [Environment]::GetEnvironmentVariable(
        "WALNUT_INT3_EDGE_ISSUER",
        "Process"
    )
    $PreviousAudience = [Environment]::GetEnvironmentVariable(
        "WALNUT_INT3_EDGE_AUDIENCE",
        "Process"
    )
    $PreviousTenantId = [Environment]::GetEnvironmentVariable(
        "WALNUT_INT3_EDGE_TENANT_ID",
        "Process"
    )
    $PreviousActorId = [Environment]::GetEnvironmentVariable(
        "WALNUT_INT3_EDGE_ACTOR_ID",
        "Process"
    )
    [Environment]::SetEnvironmentVariable(
        "WALNUT_INT3_EDGE_HMAC_SECRET",
        $HmacSecret,
        "Process"
    )
    [Environment]::SetEnvironmentVariable(
        "WALNUT_INT3_EDGE_CAPABILITY_PATH",
        $CapabilityPath,
        "Process"
    )
    [Environment]::SetEnvironmentVariable(
        "WALNUT_INT3_EDGE_ISSUER",
        [string]$RuntimeState.issuer,
        "Process"
    )
    [Environment]::SetEnvironmentVariable(
        "WALNUT_INT3_EDGE_AUDIENCE",
        [string]$RuntimeState.audience,
        "Process"
    )
    [Environment]::SetEnvironmentVariable(
        "WALNUT_INT3_EDGE_TENANT_ID",
        "tenant_yaya",
        "Process"
    )
    [Environment]::SetEnvironmentVariable(
        "WALNUT_INT3_EDGE_ACTOR_ID",
        [string]$RuntimeState.actor_id,
        "Process"
    )
    Write-Output "INT3_EDGE_CAPABILITY_PATH=$CapabilityPath"
    & $PythonExe -u $Entrypoint --port $Port
    $NativeExitCode = $LASTEXITCODE
    if ($NativeExitCode -ne 0) {
        throw "INT3 MCP edge proxy exited with native exit code $NativeExitCode."
    }
}
finally {
    if ($null -ne $PreviousHmacSecret) {
        [Environment]::SetEnvironmentVariable(
            "WALNUT_INT3_EDGE_HMAC_SECRET",
            $PreviousHmacSecret,
            "Process"
        )
    }
    else {
        [Environment]::SetEnvironmentVariable("WALNUT_INT3_EDGE_HMAC_SECRET", $null, "Process")
    }
    if ($null -ne $PreviousCapability) {
        [Environment]::SetEnvironmentVariable(
            "WALNUT_INT3_EDGE_CAPABILITY_PATH",
            $PreviousCapability,
            "Process"
        )
    }
    else {
        [Environment]::SetEnvironmentVariable("WALNUT_INT3_EDGE_CAPABILITY_PATH", $null, "Process")
    }
    if ($null -ne $PreviousIssuer) {
        [Environment]::SetEnvironmentVariable(
            "WALNUT_INT3_EDGE_ISSUER",
            $PreviousIssuer,
            "Process"
        )
    }
    else {
        [Environment]::SetEnvironmentVariable("WALNUT_INT3_EDGE_ISSUER", $null, "Process")
    }
    if ($null -ne $PreviousAudience) {
        [Environment]::SetEnvironmentVariable(
            "WALNUT_INT3_EDGE_AUDIENCE",
            $PreviousAudience,
            "Process"
        )
    }
    else {
        [Environment]::SetEnvironmentVariable("WALNUT_INT3_EDGE_AUDIENCE", $null, "Process")
    }
    if ($null -ne $PreviousTenantId) {
        [Environment]::SetEnvironmentVariable(
            "WALNUT_INT3_EDGE_TENANT_ID",
            $PreviousTenantId,
            "Process"
        )
    }
    else {
        [Environment]::SetEnvironmentVariable("WALNUT_INT3_EDGE_TENANT_ID", $null, "Process")
    }
    if ($null -ne $PreviousActorId) {
        [Environment]::SetEnvironmentVariable(
            "WALNUT_INT3_EDGE_ACTOR_ID",
            $PreviousActorId,
            "Process"
        )
    }
    else {
        [Environment]::SetEnvironmentVariable("WALNUT_INT3_EDGE_ACTOR_ID", $null, "Process")
    }
    if ($null -ne $ProtectedBytes) {
        [Array]::Clear($ProtectedBytes, 0, $ProtectedBytes.Length)
    }
    if ($null -ne $PlainBytes) {
        [Array]::Clear($PlainBytes, 0, $PlainBytes.Length)
    }
    $HmacSecret = $null
    $Capability = $null
    $CapabilityPath = $null
}
