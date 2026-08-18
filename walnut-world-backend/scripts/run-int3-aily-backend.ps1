[CmdletBinding()]
param(
    [ValidateSet('Start', 'IssueCredential', 'Status', 'Stop')]
    [string]$Action = 'Status',
    [ValidateRange(1024, 65535)]
    [int]$BackendPort = 8790,
    [ValidateRange(60, 86400)]
    [int]$TokenLifetimeSeconds = 28800,
    [string]$PostgresContainer = 'walnut-int3-postgres',
    [string]$TenantId = 'tenant_yaya',
    [ValidateRange(1024, 65535)]
    [int]$RelayPort = 8081,
    [string]$LlmUpstreamKeyFile = $env:WALNUT_LLM_UPSTREAM_API_KEY_FILE,
    [switch]$GatewayOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    Add-Type -AssemblyName System.Security
}

$script:ProtocolVersion = '2025-06-18'
$script:ActorId = 'feishu_teacher_int3_aily'
$script:Issuer = 'walnut-int3-demo'
$script:Audience = 'walnut-feishu-aily'
$script:TeacherRoles = @(
    'learner:read',
    'class-insights:read',
    'evidence:read'
)
$script:DashboardUrl =
    'https://larkcommunity.feishu.cn/base/Q6V9biulZaezaHskZqacYm2JnHe?table=blkK3ldZA0pePRGb'
$script:TeacherWorkspaceUrl =
    'https://dcniaqwtmoca.feishuapp.com/app/app_17c6bc5hz7d'

$backendRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $backendRoot '.venv\Scripts\python.exe'
$bundledAgentPath = Join-Path $backendRoot 'agent'
$legacyAgentPath = Join-Path (Split-Path -Parent $backendRoot) 'agent'
if (Test-Path -LiteralPath (Join-Path $bundledAgentPath 'contracts\manifest.json') -PathType Leaf) {
    $agentPath = (Resolve-Path -LiteralPath $bundledAgentPath).Path
}
elseif (Test-Path -LiteralPath (Join-Path $legacyAgentPath 'contracts\manifest.json') -PathType Leaf) {
    $agentPath = (Resolve-Path -LiteralPath $legacyAgentPath).Path
}
else {
    throw "Agent workspace was not found at $bundledAgentPath or $legacyAgentPath"
}
$localApplicationData = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::LocalApplicationData
)
if ([string]::IsNullOrWhiteSpace($localApplicationData)) {
    throw 'LocalApplicationData is unavailable; refusing to persist runtime credentials.'
}
$runtimeRoot = Join-Path $localApplicationData 'WalnutWorld\int3-aily-backend'
$activeStatePath = Join-Path $runtimeRoot 'active.json'

function Get-LayeredEnvironmentValue {
    param([Parameter(Mandatory)][string]$Name)

    foreach ($target in @('Process', 'User', 'Machine')) {
        $value = [Environment]::GetEnvironmentVariable($Name, $target)
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value
        }
    }
    return $null
}

function ConvertTo-Base64Url {
    param([Parameter(Mandatory)][byte[]]$Bytes)

    return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-RandomBase64Url {
    param([ValidateRange(32, 512)][int]$ByteCount = 48)

    $bytes = [byte[]]::new($ByteCount)
    $rng = $null
    try {
        $rng = [System.Security.Cryptography.RNGCryptoServiceProvider]::new()
        $rng.GetBytes($bytes)
        return ConvertTo-Base64Url -Bytes $bytes
    }
    finally {
        if ($null -ne $rng) { $rng.Dispose() }
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Protect-Text {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Path
    )

    $plain = [Text.Encoding]::UTF8.GetBytes($Value)
    $protected = $null
    try {
        $protected = [Security.Cryptography.ProtectedData]::Protect(
            $plain,
            $null,
            [Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        [IO.File]::WriteAllBytes($Path, $protected)
    }
    finally {
        if ($null -ne $plain) {
            [Array]::Clear($plain, 0, $plain.Length)
        }
        if ($null -ne $protected) {
            [Array]::Clear($protected, 0, $protected.Length)
        }
    }
}

function Unprotect-Text {
    param([Parameter(Mandatory)][string]$Path)

    $protected = [IO.File]::ReadAllBytes($Path)
    $plain = $null
    try {
        $plain = [Security.Cryptography.ProtectedData]::Unprotect(
            $protected,
            $null,
            [Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        return [Text.Encoding]::UTF8.GetString($plain)
    }
    finally {
        if ($null -ne $protected) {
            [Array]::Clear($protected, 0, $protected.Length)
        }
        if ($null -ne $plain) {
            [Array]::Clear($plain, 0, $plain.Length)
        }
    }
}

function Protect-RunDirectory {
    param([Parameter(Mandatory)][string]$Path)

    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            $currentUser,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            $propagation,
            $allow
        )
    )
    $acl.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            $system,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            $propagation,
            $allow
        )
    )
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory)][object]$Value,
        [Parameter(Mandatory)][string]$Path
    )

    $json = $Value | ConvertTo-Json -Depth 8 -Compress
    [IO.File]::WriteAllText($Path, $json, [Text.UTF8Encoding]::new($false))
}

function Read-ActiveState {
    if (-not (Test-Path -LiteralPath $activeStatePath -PathType Leaf)) {
        throw 'No active INT3 Aily Backend runtime exists.'
    }
    $json = Get-Content -LiteralPath $activeStatePath -Raw -Encoding utf8
    if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {
        return $json | ConvertFrom-Json -DateKind String
    }
    return $json | ConvertFrom-Json
}

function Assert-SafeRunDirectory {
    param([Parameter(Mandatory)][string]$Path)

    $resolvedRoot = [IO.Path]::GetFullPath($runtimeRoot).TrimEnd('\') + '\'
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    if (-not $resolvedPath.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Runtime state points outside the scoped INT3 runtime root.'
    }
    if ([IO.Path]::GetFileName($resolvedPath) -notmatch '^run-[a-f0-9]{32}$') {
        throw 'Runtime directory name is not an INT3 run identifier.'
    }
    return $resolvedPath
}

function Get-ExpectedBackendProcess {
    param([Parameter(Mandatory)][object]$State)

    $processId = [int]$State.backend_pid
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
    if (
        $null -eq $cim -or
        $cim.CommandLine -notlike '*uvicorn*walnut_backend.main:app*' -or
        $cim.CommandLine -notlike "*--port $([int]$State.backend_port)*"
    ) {
        throw 'Recorded PID is not the scoped INT3 Backend process; refusing to operate on it.'
    }
    $recordedStart = [DateTimeOffset]::Parse([string]$State.process_started_at).UtcDateTime
    if ([Math]::Abs(($process.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 2) {
        throw 'Recorded PID start time changed; refusing to operate on a reused PID.'
    }
    return $process
}

function Get-ExpectedRuntimeChild {
    param(
        [Parameter(Mandatory)][object]$State,
        [Parameter(Mandatory)][string]$PidKey,
        [Parameter(Mandatory)][string]$StartedAtKey,
        [Parameter(Mandatory)][string]$CommandMarker
    )

    if ($State.PSObject.Properties.Name -notcontains $PidKey -or $null -eq $State.$PidKey) {
        return $null
    }
    if (
        $State.PSObject.Properties.Name -notcontains $StartedAtKey -or
        [string]::IsNullOrWhiteSpace([string]$State.$StartedAtKey)
    ) {
        throw "Runtime state has $PidKey without its start-time identity; refusing to stop it."
    }
    $processId = [int]$State.$PidKey
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
    if ($null -eq $cim -or $cim.CommandLine -notlike "*$CommandMarker*") {
        throw "Recorded $PidKey is not the scoped INT3 runtime child; refusing to stop it."
    }
    $recordedStart = [DateTimeOffset]::Parse([string]$State.$StartedAtKey).UtcDateTime
    if ([Math]::Abs(($process.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 2) {
        throw "Recorded $PidKey start time changed; refusing to stop a reused PID."
    }
    return $process
}

function Stop-ProcessAndWait {
    param([AllowNull()][System.Diagnostics.Process]$Process)

    if ($null -eq $Process -or $Process.HasExited) {
        return
    }
    Stop-Process -Id $Process.Id -Force -ErrorAction Stop
    if (-not $Process.WaitForExit(10000)) {
        throw "Process $($Process.Id) did not exit within 10 seconds."
    }
}

function Get-BackendListenerProcessId {
    param(
        [Parameter(Mandatory)][int]$LauncherProcessId,
        [Parameter(Mandatory)][int]$Port
    )

    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalAddress '127.0.0.1' -LocalPort $Port `
            -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -ne 1) {
        throw "Expected exactly one Backend listener on 127.0.0.1:$Port."
    }
    $listenerProcessId = [int]$listeners[0].OwningProcess
    if ($listenerProcessId -eq $LauncherProcessId) {
        return $listenerProcessId
    }
    $listener = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerProcessId"
    if (
        $null -eq $listener -or
        [int]$listener.ParentProcessId -ne $LauncherProcessId -or
        $listener.CommandLine -notlike '*uvicorn*walnut_backend.main:app*' -or
        $listener.CommandLine -notlike "*--port $Port*"
    ) {
        throw 'Backend listener is not the direct child of the scoped launcher process.'
    }
    return $listenerProcessId
}

function Get-PostgresDatabaseUrl {
    param(
        [Parameter(Mandatory)][string]$Container,
        [Parameter(Mandatory)][int]$ExpectedHostPort
    )

    $explicit = Get-LayeredEnvironmentValue -Name 'WALNUT_DATABASE_URL'
    if ($null -ne $explicit) {
        return $explicit
    }

    $rawInspect = & docker inspect $Container 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($rawInspect -join ''))) {
        throw "PostgreSQL container '$Container' is unavailable and WALNUT_DATABASE_URL is unset."
    }
    try {
        $inspect = @($rawInspect | ConvertFrom-Json)
    }
    catch {
        throw 'The scoped PostgreSQL container metadata is malformed.'
    }
    if ($inspect.Count -ne 1 -or -not [bool]$inspect[0].State.Running) {
        throw "PostgreSQL container '$Container' is not running."
    }
    $bindings = @($inspect[0].NetworkSettings.Ports.'5432/tcp')
    if (
        $bindings.Count -ne 1 -or
        [string]$bindings[0].HostIp -ne '127.0.0.1' -or
        [int]$bindings[0].HostPort -ne $ExpectedHostPort
    ) {
        throw "PostgreSQL container must bind only 127.0.0.1:$ExpectedHostPort."
    }
    $containerEnvironment = @{}
    foreach ($entry in @($inspect[0].Config.Env)) {
        $name, $value = ([string]$entry).Split('=', 2)
        $containerEnvironment[$name] = $value
    }
    $user = if ($containerEnvironment.ContainsKey('POSTGRES_USER')) {
        [string]$containerEnvironment.POSTGRES_USER
    }
    else {
        'postgres'
    }
    $database = if ($containerEnvironment.ContainsKey('POSTGRES_DB')) {
        [string]$containerEnvironment.POSTGRES_DB
    }
    else {
        $user
    }
    $password = if ($containerEnvironment.ContainsKey('POSTGRES_PASSWORD')) {
        [string]$containerEnvironment.POSTGRES_PASSWORD
    }
    else {
        $null
    }
    $hostAuthMethod = if ($containerEnvironment.ContainsKey('POSTGRES_HOST_AUTH_METHOD')) {
        [string]$containerEnvironment.POSTGRES_HOST_AUTH_METHOD
    }
    else {
        $null
    }
    $encodedUser = [Uri]::EscapeDataString($user)
    $encodedDatabase = [Uri]::EscapeDataString($database)
    if ([string]::IsNullOrEmpty($password)) {
        if ($hostAuthMethod -cne 'trust') {
            throw 'The scoped PostgreSQL container has neither password nor explicit trust mode.'
        }
        # This is the pre-existing local demo database. Its only published binding
        # was closed above to 127.0.0.1, so trust never becomes a network ingress.
        return "postgresql://${encodedUser}@127.0.0.1:${ExpectedHostPort}/${encodedDatabase}"
    }
    $encodedPassword = [Uri]::EscapeDataString($password)
    return "postgresql://${encodedUser}:${encodedPassword}@127.0.0.1:${ExpectedHostPort}/${encodedDatabase}"
}

function New-TeacherAuthorization {
    param(
        [Parameter(Mandatory)][string]$HmacSecret,
        [Parameter(Mandatory)][string]$Tenant,
        [Parameter(Mandatory)][int]$LifetimeSeconds
    )

    $issuedAt = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $header = [ordered]@{ alg = 'HS256'; typ = 'JWT' } |
        ConvertTo-Json -Compress
    $claims = [ordered]@{
        iss = $script:Issuer
        aud = $script:Audience
        sub = $script:ActorId
        tenant_id = $Tenant
        actor_id = $script:ActorId
        actor_type = 'teacher'
        roles = [string[]]$script:TeacherRoles
        iat = $issuedAt
        nbf = $issuedAt
        exp = $issuedAt + $LifetimeSeconds
    } | ConvertTo-Json -Compress
    $encodedHeader = ConvertTo-Base64Url -Bytes ([Text.Encoding]::UTF8.GetBytes($header))
    $encodedClaims = ConvertTo-Base64Url -Bytes ([Text.Encoding]::UTF8.GetBytes($claims))
    $signingInput = "$encodedHeader.$encodedClaims"
    $secretBytes = [Text.Encoding]::UTF8.GetBytes($HmacSecret)
    $signingBytes = [Text.Encoding]::ASCII.GetBytes($signingInput)
    $hmac = [Security.Cryptography.HMACSHA256]::new($secretBytes)
    $signature = $null
    try {
        $signature = $hmac.ComputeHash($signingBytes)
        $token = "$signingInput.$(ConvertTo-Base64Url -Bytes $signature)"
    }
    finally {
        $hmac.Dispose()
        [Array]::Clear($secretBytes, 0, $secretBytes.Length)
        [Array]::Clear($signingBytes, 0, $signingBytes.Length)
        if ($null -ne $signature) {
            [Array]::Clear($signature, 0, $signature.Length)
        }
    }
    return [pscustomobject]@{
        authorization = "Bearer $token"
        issued_at = [DateTimeOffset]::FromUnixTimeSeconds($issuedAt).ToString('o')
        expires_at = [DateTimeOffset]::FromUnixTimeSeconds(
            $issuedAt + $LifetimeSeconds
        ).ToString('o')
        lifetime_seconds = $LifetimeSeconds
    }
}

function Save-TeacherCredential {
    param(
        [Parameter(Mandatory)][string]$RunDirectory,
        [Parameter(Mandatory)][object]$Credential,
        [Parameter(Mandatory)][string]$Tenant
    )

    Protect-Text -Value ([string]$Credential.authorization) -Path (
        Join-Path $RunDirectory 'teacher-authorization.dpapi'
    )
    Write-JsonFile -Path (Join-Path $RunDirectory 'credential-metadata.json') -Value (
        [ordered]@{
            actor_id = $script:ActorId
            actor_type = 'teacher'
            tenant_id = $Tenant
            roles = [string[]]$script:TeacherRoles
            issued_at = $Credential.issued_at
            expires_at = $Credential.expires_at
            lifetime_seconds = $Credential.lifetime_seconds
            protected_for = 'CURRENT_WINDOWS_USER_DPAPI'
        }
    )
}

function Invoke-McpRequest {
    param(
        [Parameter(Mandatory)][int]$Port,
        [AllowNull()][string]$Authorization,
        [Parameter(Mandatory)][object]$Body,
        [switch]$SkipHttpErrorCheck
    )

    $headers = @{ Accept = 'application/json' }
    if (-not [string]::IsNullOrEmpty($Authorization)) {
        $headers.Authorization = $Authorization
    }
    if ([string]$Body.method -ne 'initialize') {
        $headers.'MCP-Protocol-Version' = $script:ProtocolVersion
    }
    $invokeParameters = @{
        UseBasicParsing = $true
        Method = 'Post'
        Uri = "http://127.0.0.1:$Port/integrations/feishu/v1/mcp"
        Headers = $headers
        ContentType = 'application/json'
        Body = ($Body | ConvertTo-Json -Depth 12 -Compress)
    }
    $availableParameters = (Get-Command Invoke-WebRequest).Parameters
    if ($availableParameters.ContainsKey('NoProxy')) {
        $invokeParameters.NoProxy = $true
    }
    if ($availableParameters.ContainsKey('ConnectionTimeoutSeconds')) {
        $invokeParameters.ConnectionTimeoutSeconds = 1
        $invokeParameters.OperationTimeoutSeconds = 2
    }
    else {
        $invokeParameters.TimeoutSec = 2
    }
    if ($availableParameters.ContainsKey('SkipHttpErrorCheck')) {
        $invokeParameters.SkipHttpErrorCheck = [bool]$SkipHttpErrorCheck
    }
    try {
        return Invoke-WebRequest @invokeParameters
    }
    catch {
        if (-not $SkipHttpErrorCheck -or $null -eq $_.Exception.Response) {
            throw
        }
        $response = $_.Exception.Response
        $content = [string]$_.ErrorDetails.Message
        if ([string]::IsNullOrWhiteSpace($content)) {
            $content = '{}'
        }
        return [pscustomobject]@{
            StatusCode = [int]$response.StatusCode
            Content = $content
        }
    }
}

function Assert-BackendCredential {
    param(
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$Authorization,
        [ValidateRange(1, 60)][int]$Attempts = 1
    )

    $initialize = [ordered]@{
        jsonrpc = '2.0'
        id = 'runtime-initialize'
        method = 'initialize'
        params = [ordered]@{
            protocolVersion = $script:ProtocolVersion
            capabilities = @{}
            clientInfo = @{ name = 'int3-secure-runtime'; version = '1.0.0' }
        }
    }
    $lastError = $null
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            $response = Invoke-McpRequest -Port $Port -Authorization $Authorization -Body $initialize
            $payload = $response.Content | ConvertFrom-Json
            if (
                [int]$response.StatusCode -ne 200 -or
                [string]$payload.result.protocolVersion -ne $script:ProtocolVersion
            ) {
                throw 'Backend did not negotiate the locked MCP protocol.'
            }
            $list = Invoke-McpRequest -Port $Port -Authorization $Authorization -Body (
                [ordered]@{ jsonrpc = '2.0'; id = 'runtime-tools'; method = 'tools/list'; params = @{} }
            )
            $listPayload = $list.Content | ConvertFrom-Json
            $actualTools = @($listPayload.result.tools | ForEach-Object { [string]$_.name } | Sort-Object)
            $expectedTools = @(
                'get_evidence_summary_and_links',
                'query_class_common_issues',
                'query_learner_progress'
            )
            if (($actualTools -join "`n") -cne ($expectedTools -join "`n")) {
                throw 'Backend did not expose exactly the three locked teacher tools.'
            }
            $denied = Invoke-McpRequest -Port $Port -Authorization $null -Body $initialize `
                -SkipHttpErrorCheck
            if ([int]$denied.StatusCode -ne 401) {
                throw 'Backend did not reject an unauthenticated MCP request.'
            }
            return
        }
        catch {
            $lastError = $_
            if ($attempt -lt $Attempts) {
                Start-Sleep -Milliseconds 500
            }
        }
    }
    throw $lastError
}

function Remove-ScopedRuntime {
    param(
        [Parameter(Mandatory)][string]$RunDirectory,
        [switch]$RemoveActiveState
    )

    $safeDirectory = Assert-SafeRunDirectory -Path $RunDirectory
    for ($attempt = 1; $attempt -le 40; $attempt++) {
        if (-not (Test-Path -LiteralPath $safeDirectory -PathType Container)) {
            break
        }
        try {
            Remove-Item -LiteralPath $safeDirectory -Recurse -Force -ErrorAction Stop
        }
        catch {
            if ($attempt -eq 40) {
                throw
            }
            Start-Sleep -Milliseconds 250
        }
    }
    if ($RemoveActiveState -and (Test-Path -LiteralPath $activeStatePath -PathType Leaf)) {
        Remove-Item -LiteralPath $activeStatePath -Force -ErrorAction Stop
    }
}

function Start-BackendRuntime {
    if (Test-Path -LiteralPath $activeStatePath -PathType Leaf) {
        $existing = Read-ActiveState
        if ($null -ne (Get-ExpectedBackendProcess -State $existing)) {
            throw 'An INT3 Aily Backend runtime is already active; stop it before starting another.'
        }
        throw 'A stale active runtime record exists; inspect it before removing any scoped state.'
    }
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "Backend Python runtime is missing at $pythonPath."
    }
    if (Get-NetTCPConnection -State Listen -LocalPort $BackendPort -ErrorAction SilentlyContinue) {
        throw "Backend port $BackendPort is already listening."
    }
    if (-not (Test-Path -LiteralPath $runtimeRoot -PathType Container)) {
        New-Item -ItemType Directory -Path $runtimeRoot | Out-Null
    }
    $runDirectory = Join-Path $runtimeRoot "run-$([Guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $runDirectory | Out-Null
    Protect-RunDirectory -Path $runDirectory

    $backendProcess = $null
    $relayProcess = $null
    $workerProcess = $null
    $learnerProcess = $null
    try {
        & $pythonPath (Join-Path $backendRoot 'scripts\verify_contract_release.py') `
            --agent-repo $agentPath *> $null
        if ($LASTEXITCODE -ne 0) {
            throw 'Agent contract byte-pin verification failed.'
        }
        $databaseUrl = Get-PostgresDatabaseUrl -Container $PostgresContainer `
            -ExpectedHostPort 55432
        $pseudonymSecret = Get-LayeredEnvironmentValue -Name 'WALNUT_FEISHU_PSEUDONYM_SECRET'
        if ($null -eq $pseudonymSecret -or $pseudonymSecret.Length -lt 32) {
            throw 'WALNUT_FEISHU_PSEUDONYM_SECRET is unavailable or invalid.'
        }
        $hmacSecret = New-RandomBase64Url
        Protect-Text -Value $hmacSecret -Path (Join-Path $runDirectory 'auth-hmac.dpapi')
        $credential = New-TeacherAuthorization -HmacSecret $hmacSecret -Tenant $TenantId `
            -LifetimeSeconds $TokenLifetimeSeconds
        Save-TeacherCredential -RunDirectory $runDirectory -Credential $credential -Tenant $TenantId

        if (-not $GatewayOnly) {
            if ([string]::IsNullOrWhiteSpace($LlmUpstreamKeyFile)) {
                $LlmUpstreamKeyFile = Join-Path (
                    [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
                ) '.walnut-secrets\deepseek-v4-flash.key'
            }
            if (-not (Test-Path -LiteralPath $LlmUpstreamKeyFile -PathType Leaf)) {
                throw "LLM upstream key file is missing at $LlmUpstreamKeyFile. Configure WALNUT_LLM_UPSTREAM_API_KEY_FILE or pass -LlmUpstreamKeyFile."
            }
        }
        $relaySecret = New-RandomBase64Url
        $workerId = 'walnut-int3-worker'
        $learnerWorkerId = 'walnut-int3-learner'
        $childEnvironment = @{
            WALNUT_DATABASE_URL = $databaseUrl
            WALNUT_CONTRACT_PATH = $agentPath
            WALNUT_DEVELOPMENT_AUTH = 'false'
            WALNUT_AUTH_HMAC_SECRET = $hmacSecret
            WALNUT_AUTH_ISSUER = $script:Issuer
            WALNUT_AUTH_AUDIENCE = $script:Audience
            WALNUT_AUTH_CLOCK_SKEW_SECONDS = '0'
            WALNUT_AUTH_MAXIMUM_LIFETIME_SECONDS = [string]$TokenLifetimeSeconds
            WALNUT_FEISHU_PSEUDONYM_SECRET = $pseudonymSecret
            WALNUT_FEISHU_MCP_DASHBOARD_URL = $script:DashboardUrl
            WALNUT_FEISHU_MCP_TEACHER_WORKSPACE_URL = $script:TeacherWorkspaceUrl
            WALNUT_ENABLE_REALTIME_WSS = 'false'
            WALNUT_ENABLE_CLIENT_EVENT_BATCH = 'false'
            WALNUT_ENABLE_WORLD_PRESENTATION = 'false'
            WALNUT_ENABLE_SKILL_PATCH = 'false'
            WALNUT_LLM_UPSTREAM_API_KEY = ''
            WALNUT_LLM_UPSTREAM_API_KEY_FILE = ''
            DEEPSEEK_API_KEY = ''
            OPENAI_API_KEY = ''
            PYTHONPATH = (Join-Path $backendRoot 'src')
            PYTHONUTF8 = '1'
            WALNUT_LLM_RELAY_ENDPOINT = "http://127.0.0.1:$RelayPort"
            WALNUT_LLM_RELAY_API_KEY = $relaySecret
            WALNUT_LLM_RELAY_ALLOW_INSECURE_LOCALHOST = 'true'
            WALNUT_LLM_RELAY_REQUIRED_RETENTION_SECONDS = '604800'
            WALNUT_LLM_RELAY_MAX_RESPONSE_BYTES = '2097152'
            WALNUT_LLM_RELAY_CAPABILITY_TIMEOUT_MS = '5000'
            WALNUT_LLM_RELAY_SERVER_API_KEY = $relaySecret
            WALNUT_LLM_RELAY_BIND_HOST = '127.0.0.1'
            WALNUT_LLM_RELAY_BIND_PORT = [string]$RelayPort
            WALNUT_LLM_UPSTREAM_ENDPOINT = 'https://api.deepseek.com/chat/completions'
            WALNUT_LLM_PROVIDER = 'deepseek'
            WALNUT_LLM_MODEL = 'deepseek-v4-flash'
            WALNUT_LLM_RESPONSE_FORMAT = 'json_object'
            WALNUT_LLM_THINKING_MODE = 'disabled'
            WALNUT_SANDBOX_IMAGE = 'gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c'
            WALNUT_DOCKER_EXECUTABLE = 'docker'
            WALNUT_SANDBOX_CPU_MS = '1000'
            WALNUT_SANDBOX_WALL_MS = '15000'
            WALNUT_SANDBOX_MEMORY_BYTES = '536870912'
            WALNUT_SANDBOX_MAX_PROCESSES = '64'
            WALNUT_SANDBOX_MAX_OUTPUT_BYTES = '65536'
            WALNUT_TENANT_ID = $TenantId
            WALNUT_PROMPT_VERSION = 'int1-prompt-v1'
            WALNUT_TEACHING_SPEC_VERSION = 'agent-teaching-v1'
            WALNUT_WORLD_RULES_VERSION = 'farm-rules-1'
            WALNUT_WORLD_CONTENT_VERSION = '1.0.0'
            WALNUT_WORLD_SUCCESS_SCORE = '8'
            WALNUT_WORLD_WATERING_EXPECTED_UNITS = '2,1,1,0,0,2,0,1'
            WALNUT_RUNTIME_ROOT = $runtimeRoot
            WALNUT_WORKER_ID = $workerId
            WALNUT_WORKER_LEASE_SECONDS = '120'
            WALNUT_WORKER_IDLE_POLL_SECONDS = '0.1'
            WALNUT_LEARNER_WORKER_ID = $learnerWorkerId
            WALNUT_LEARNER_WORKER_LEASE_SECONDS = '120'
            WALNUT_LEARNER_WORKER_IDLE_POLL_SECONDS = '0.1'
            WALNUT_LEARNER_WORKER_MAXIMUM_ATTEMPTS = '5'
        }
        $stdoutPath = Join-Path $runDirectory 'backend.stdout.log'
        $stderrPath = Join-Path $runDirectory 'backend.stderr.log'
        $relayStdoutPath = Join-Path $runDirectory 'relay.stdout.log'
        $relayStderrPath = Join-Path $runDirectory 'relay.stderr.log'
        $workerStdoutPath = Join-Path $runDirectory 'worker.stdout.log'
        $workerStderrPath = Join-Path $runDirectory 'worker.stderr.log'
        $learnerStdoutPath = Join-Path $runDirectory 'learner-worker.stdout.log'
        $learnerStderrPath = Join-Path $runDirectory 'learner-worker.stderr.log'
        # Windows PowerShell 5.1 Start-Process has no -Environment; set the child
        # variables on this process (children inherit) and restore them after.
        $savedChildEnvironment = @{}
        foreach ($key in $childEnvironment.Keys) {
            $savedChildEnvironment[[string]$key] = [Environment]::GetEnvironmentVariable([string]$key)
            [Environment]::SetEnvironmentVariable([string]$key, [string]$childEnvironment[$key])
        }
        try {
            $backendProcess = Start-Process -FilePath $pythonPath -ArgumentList @(
                '-m',
                'uvicorn',
                'walnut_backend.main:app',
                '--host',
                '127.0.0.1',
                '--port',
                [string]$BackendPort,
                '--no-access-log',
                '--log-level',
                'warning'
            ) -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
            if (-not $GatewayOnly) {
                try {
                    [Environment]::SetEnvironmentVariable(
                        'WALNUT_LLM_UPSTREAM_API_KEY_FILE', $LlmUpstreamKeyFile
                    )
                    $relayProcess = Start-Process -FilePath $pythonPath `
                        -ArgumentList @('-m', 'walnut_backend.llm_relay.main') `
                        -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru `
                        -RedirectStandardOutput $relayStdoutPath `
                        -RedirectStandardError $relayStderrPath
                }
                finally {
                    [Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY_FILE', '')
                }
                $workerProcess = Start-Process -FilePath $pythonPath `
                    -ArgumentList @('-m', 'walnut_backend.worker_main') `
                    -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru `
                    -RedirectStandardOutput $workerStdoutPath `
                    -RedirectStandardError $workerStderrPath
                $learnerProcess = Start-Process -FilePath $pythonPath `
                    -ArgumentList @('-m', 'walnut_backend.learner_worker_main') `
                    -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru `
                    -RedirectStandardOutput $learnerStdoutPath `
                    -RedirectStandardError $learnerStderrPath
            }
        }
        finally {
            foreach ($key in $savedChildEnvironment.Keys) {
                [Environment]::SetEnvironmentVariable([string]$key, $savedChildEnvironment[$key])
            }
        }

        Assert-BackendCredential -Port $BackendPort `
            -Authorization ([string]$credential.authorization) -Attempts 40
        if (-not $GatewayOnly) {
            Start-Sleep -Seconds 1
            if ($relayProcess.HasExited -or $workerProcess.HasExited) {
                throw 'LLM relay or workflow worker exited during startup; inspect relay/worker logs.'
            }
        }
        $process = Get-Process -Id $backendProcess.Id
        $listenerProcessId = Get-BackendListenerProcessId `
            -LauncherProcessId $backendProcess.Id -Port $BackendPort
        $state = [ordered]@{
            runtime_version = '1.0.0'
            run_directory = $runDirectory
            backend_pid = $backendProcess.Id
            backend_listener_pid = $listenerProcessId
            backend_port = $BackendPort
            relay_pid = if ($null -ne $relayProcess) { $relayProcess.Id } else { $null }
            relay_started_at = if ($null -ne $relayProcess) { $relayProcess.StartTime.ToUniversalTime().ToString('o') } else { $null }
            worker_pid = if ($null -ne $workerProcess) { $workerProcess.Id } else { $null }
            worker_started_at = if ($null -ne $workerProcess) { $workerProcess.StartTime.ToUniversalTime().ToString('o') } else { $null }
            learner_pid = if ($null -ne $learnerProcess -and -not $learnerProcess.HasExited) { $learnerProcess.Id } else { $null }
            learner_started_at = if ($null -ne $learnerProcess -and -not $learnerProcess.HasExited) { $learnerProcess.StartTime.ToUniversalTime().ToString('o') } else { $null }
            process_started_at = $process.StartTime.ToUniversalTime().ToString('o')
            runtime_mode = if ($GatewayOnly) { 'GATEWAY_ONLY' } else { 'FULL_STACK' }
            provider_started = -not $GatewayOnly
            tenant_id = $TenantId
            actor_id = $script:ActorId
            actor_type = 'teacher'
            roles = [string[]]$script:TeacherRoles
            issuer = $script:Issuer
            audience = $script:Audience
            development_auth = $false
            maximum_jwt_lifetime_seconds = $TokenLifetimeSeconds
            credential_expires_at = $credential.expires_at
            mcp_path = '/integrations/feishu/v1/mcp'
            created_at = [DateTimeOffset]::UtcNow.ToString('o')
        }
        Write-JsonFile -Value $state -Path (Join-Path $runDirectory 'runtime.json')
        Write-JsonFile -Value $state -Path $activeStatePath
        [pscustomobject]@{
            status = 'RUNNING'
            backend_pid = $backendProcess.Id
            backend_endpoint = "http://127.0.0.1:$BackendPort/integrations/feishu/v1/mcp"
            production_auth = $true
            tenant_id = $TenantId
            actor_type = 'teacher'
            roles = [string[]]$script:TeacherRoles
            token_lifetime_seconds = $TokenLifetimeSeconds
            credential_expires_at = $credential.expires_at
            credential_storage = 'CURRENT_WINDOWS_USER_DPAPI'
            authorization_echoed = $false
            provider_started = -not $GatewayOnly
            runtime_mode = if ($GatewayOnly) { 'GATEWAY_ONLY' } else { 'FULL_STACK' }
            relay_pid = if ($null -ne $relayProcess) { $relayProcess.Id } else { $null }
            worker_pid = if ($null -ne $workerProcess) { $workerProcess.Id } else { $null }
            learner_pid = if ($null -ne $learnerProcess -and -not $learnerProcess.HasExited) { $learnerProcess.Id } else { $null }
            database_exposure = 'LOOPBACK_ONLY_EXISTING_DEMO_CONTAINER'
        }
    }
    catch {
        foreach ($candidate in @($backendProcess, $relayProcess, $workerProcess, $learnerProcess)) {
            if ($null -ne $candidate -and -not $candidate.HasExited) {
                Stop-Process -Id $candidate.Id -Force -ErrorAction SilentlyContinue
            }
        }
        if (Test-Path -LiteralPath $runDirectory -PathType Container) {
            Remove-ScopedRuntime -RunDirectory $runDirectory
        }
        throw
    }
    finally {
        $databaseUrl = $null
        $pseudonymSecret = $null
        $hmacSecret = $null
        $credential = $null
    }
}

function Issue-BackendCredential {
    $state = Read-ActiveState
    $process = Get-ExpectedBackendProcess -State $state
    if ($null -eq $process) {
        throw 'The recorded INT3 Backend process is no longer running.'
    }
    $runDirectory = Assert-SafeRunDirectory -Path ([string]$state.run_directory)
    $secretPath = Join-Path $runDirectory 'auth-hmac.dpapi'
    $hmacSecret = Unprotect-Text -Path $secretPath
    try {
        $credential = New-TeacherAuthorization -HmacSecret $hmacSecret `
            -Tenant ([string]$state.tenant_id) -LifetimeSeconds $TokenLifetimeSeconds
        Assert-BackendCredential -Port ([int]$state.backend_port) `
            -Authorization ([string]$credential.authorization)
        Save-TeacherCredential -RunDirectory $runDirectory -Credential $credential `
            -Tenant ([string]$state.tenant_id)
        $state.credential_expires_at = $credential.expires_at
        Write-JsonFile -Value $state -Path (Join-Path $runDirectory 'runtime.json')
        Write-JsonFile -Value $state -Path $activeStatePath
        [pscustomobject]@{
            status = 'CREDENTIAL_ISSUED'
            credential_expires_at = $credential.expires_at
            token_lifetime_seconds = $TokenLifetimeSeconds
            tenant_id = $state.tenant_id
            actor_type = $state.actor_type
            roles = @($state.roles)
            credential_storage = 'CURRENT_WINDOWS_USER_DPAPI'
            authorization_echoed = $false
        }
    }
    finally {
        $hmacSecret = $null
        $credential = $null
    }
}

function Show-BackendStatus {
    if (-not (Test-Path -LiteralPath $activeStatePath -PathType Leaf)) {
        return [pscustomobject]@{ status = 'STOPPED' }
    }
    $state = Read-ActiveState
    $process = Get-ExpectedBackendProcess -State $state
    $runDirectory = Assert-SafeRunDirectory -Path ([string]$state.run_directory)
    $credentialPath = Join-Path $runDirectory 'teacher-authorization.dpapi'
    return [pscustomobject]@{
        status = if ($null -eq $process) { 'STALE' } else { 'RUNNING' }
        backend_pid = $state.backend_pid
        backend_endpoint = "http://127.0.0.1:$($state.backend_port)$($state.mcp_path)"
        production_auth = -not [bool]$state.development_auth
        tenant_id = $state.tenant_id
        actor_type = $state.actor_type
        roles = @($state.roles)
        maximum_jwt_lifetime_seconds = $state.maximum_jwt_lifetime_seconds
        credential_expires_at = $state.credential_expires_at
        credential_is_dpapi_protected = (Test-Path -LiteralPath $credentialPath -PathType Leaf)
        authorization_echoed = $false
        provider_started = if ($state.PSObject.Properties.Name -contains 'provider_started') {
            [bool]$state.provider_started
        }
        else {
            $false
        }
        runtime_mode = if ($state.PSObject.Properties.Name -contains 'runtime_mode') {
            [string]$state.runtime_mode
        }
        else {
            'LEGACY_GATEWAY_ONLY'
        }
        database_exposure = 'LOOPBACK_ONLY_EXISTING_DEMO_CONTAINER'
    }
}

function Stop-BackendRuntime {
    $state = Read-ActiveState
    $runDirectory = Assert-SafeRunDirectory -Path ([string]$state.run_directory)
    $process = Get-ExpectedBackendProcess -State $state
    $listenerProcessId = if ($state.PSObject.Properties.Name -contains 'backend_listener_pid') {
        [int]$state.backend_listener_pid
    }
    else {
        Get-BackendListenerProcessId -LauncherProcessId ([int]$state.backend_pid) `
            -Port ([int]$state.backend_port)
    }
    $listenerProcess = if ($listenerProcessId -ne [int]$state.backend_pid) {
        Get-Process -Id $listenerProcessId -ErrorAction SilentlyContinue
    }
    else {
        $null
    }
    $relayProcess = Get-ExpectedRuntimeChild -State $state -PidKey 'relay_pid' `
        -StartedAtKey 'relay_started_at' -CommandMarker 'walnut_backend.llm_relay.main'
    $workerProcess = Get-ExpectedRuntimeChild -State $state -PidKey 'worker_pid' `
        -StartedAtKey 'worker_started_at' -CommandMarker 'walnut_backend.worker_main'
    $learnerProcess = Get-ExpectedRuntimeChild -State $state -PidKey 'learner_pid' `
        -StartedAtKey 'learner_started_at' -CommandMarker 'walnut_backend.learner_worker_main'

    Stop-ProcessAndWait -Process $learnerProcess
    Stop-ProcessAndWait -Process $workerProcess
    Stop-ProcessAndWait -Process $relayProcess
    Stop-ProcessAndWait -Process $listenerProcess
    Stop-ProcessAndWait -Process $process
    Remove-ScopedRuntime -RunDirectory $runDirectory -RemoveActiveState
    return [pscustomobject]@{
        status = 'STOPPED'
        backend_pid = $state.backend_pid
        scoped_runtime_files_removed = $true
        recoverability_note = 'Normal deletion completed; storage-level forensic recovery was not assessed.'
    }
}

switch ($Action) {
    'Start' {
        Start-BackendRuntime
    }
    'IssueCredential' {
        Issue-BackendCredential
    }
    'Status' {
        Show-BackendStatus
    }
    'Stop' {
        Stop-BackendRuntime
    }
}
