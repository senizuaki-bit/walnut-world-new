[CmdletBinding()]
param(
    [ValidateSet('Start', 'IssueCredential', 'Status', 'Stop')]
    [string]$Action = 'Status',
    [ValidateRange(1024, 65535)]
    [int]$BackendPort = 8790,
    [ValidateRange(60, 900)]
    [int]$TokenLifetimeSeconds = 900,
    [string]$PostgresContainer = 'walnut-int3-postgres',
    [string]$TenantId = 'tenant_yaya'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

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
$agentPath = (Resolve-Path (Join-Path $backendRoot '..\agent')).Path
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
    try {
        [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        return ConvertTo-Base64Url -Bytes $bytes
    }
    finally {
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
    return Get-Content -LiteralPath $activeStatePath -Raw -Encoding utf8 |
        ConvertFrom-Json -DateKind String
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
    return Invoke-WebRequest -NoProxy -UseBasicParsing -Method Post `
        -Uri "http://127.0.0.1:$Port/integrations/feishu/v1/mcp" `
        -Headers $headers -ContentType 'application/json' `
        -Body ($Body | ConvertTo-Json -Depth 12 -Compress) `
        -ConnectionTimeoutSeconds 1 -OperationTimeoutSeconds 2 `
        -SkipHttpErrorCheck:$SkipHttpErrorCheck
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
    if (Test-Path -LiteralPath $safeDirectory -PathType Container) {
        Remove-Item -LiteralPath $safeDirectory -Recurse -Force
    }
    if ($RemoveActiveState -and (Test-Path -LiteralPath $activeStatePath -PathType Leaf)) {
        Remove-Item -LiteralPath $activeStatePath -Force
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

        $childEnvironment = @{
            WALNUT_DATABASE_URL = $databaseUrl
            WALNUT_CONTRACT_PATH = $agentPath
            WALNUT_DEVELOPMENT_AUTH = 'false'
            WALNUT_AUTH_HMAC_SECRET = $hmacSecret
            WALNUT_AUTH_ISSUER = $script:Issuer
            WALNUT_AUTH_AUDIENCE = $script:Audience
            WALNUT_AUTH_CLOCK_SKEW_SECONDS = '0'
            WALNUT_AUTH_MAXIMUM_LIFETIME_SECONDS = '900'
            WALNUT_FEISHU_PSEUDONYM_SECRET = $pseudonymSecret
            WALNUT_FEISHU_MCP_DASHBOARD_URL = $script:DashboardUrl
            WALNUT_FEISHU_MCP_TEACHER_WORKSPACE_URL = $script:TeacherWorkspaceUrl
            WALNUT_ENABLE_REALTIME_WSS = 'false'
            WALNUT_ENABLE_CLIENT_EVENT_BATCH = 'false'
            WALNUT_ENABLE_WORLD_PRESENTATION = 'false'
            WALNUT_ENABLE_SKILL_PATCH = 'false'
            WALNUT_LLM_UPSTREAM_API_KEY = ''
            DEEPSEEK_API_KEY = ''
            OPENAI_API_KEY = ''
            PYTHONPATH = (Join-Path $backendRoot 'src')
            PYTHONUTF8 = '1'
        }
        $stdoutPath = Join-Path $runDirectory 'backend.stdout.log'
        $stderrPath = Join-Path $runDirectory 'backend.stderr.log'
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
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath `
            -Environment $childEnvironment

        Assert-BackendCredential -Port $BackendPort `
            -Authorization ([string]$credential.authorization) -Attempts 40
        $process = Get-Process -Id $backendProcess.Id
        $listenerProcessId = Get-BackendListenerProcessId `
            -LauncherProcessId $backendProcess.Id -Port $BackendPort
        $state = [ordered]@{
            runtime_version = '1.0.0'
            run_directory = $runDirectory
            backend_pid = $backendProcess.Id
            backend_listener_pid = $listenerProcessId
            backend_port = $BackendPort
            process_started_at = $process.StartTime.ToUniversalTime().ToString('o')
            tenant_id = $TenantId
            actor_id = $script:ActorId
            actor_type = 'teacher'
            roles = [string[]]$script:TeacherRoles
            issuer = $script:Issuer
            audience = $script:Audience
            development_auth = $false
            maximum_jwt_lifetime_seconds = 900
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
            provider_started = $false
            database_exposure = 'LOOPBACK_ONLY_EXISTING_DEMO_CONTAINER'
        }
    }
    catch {
        if ($null -ne $backendProcess -and -not $backendProcess.HasExited) {
            Stop-Process -Id $backendProcess.Id -Force -ErrorAction SilentlyContinue
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
        provider_started = $false
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
    if ($listenerProcessId -ne [int]$state.backend_pid) {
        Stop-Process -Id $listenerProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($null -ne $process) {
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit(10000)
    }
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
