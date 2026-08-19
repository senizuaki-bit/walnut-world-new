[CmdletBinding()]
param(
    [ValidateSet('Start', 'Status', 'Stop')]
    [string]$Action = 'Start',
    [string]$PostgresImage = 'postgres:16.9-alpine@sha256:7c688148e5e156d0e86df7ba8ae5a05a2386aaec1e2ad8e6d11bdf10504b1fb7',
    [string]$SandboxImage = 'gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c',
    [string]$UpstreamEndpoint = 'https://api.deepseek.com/chat/completions',
    [string]$Model = 'deepseek-v4-flash',
    [string]$Provider = 'deepseek',
    [int]$TokenLifetimeSeconds = 7200,
    [string]$PythonExe = '',
    [string]$GodotExe = $env:GODOT_EXE,
    [string]$UpstreamKeyFile = $env:WALNUT_LLM_UPSTREAM_API_KEY_FILE
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
if ($PSVersionTable.PSEdition -eq 'Desktop') {
    Add-Type -AssemblyName System.Security
}

$backendRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $backendRoot
$frontendContainerRoot = Join-Path $workspaceRoot 'walnut-world-frontend'
$nestedFrontendRoot = Join-Path $frontendContainerRoot 'walnut-world-frontend'
if (Test-Path -LiteralPath (Join-Path $nestedFrontendRoot 'project.godot') -PathType Leaf) {
    $frontendRoot = (Resolve-Path -LiteralPath $nestedFrontendRoot).Path
}
else {
    $frontendRoot = $frontendContainerRoot
}
$bundledAgentRoot = Join-Path $backendRoot 'agent'
$legacyAgentRoot = Join-Path $workspaceRoot 'agent'
if (Test-Path -LiteralPath (Join-Path $bundledAgentRoot 'contracts\manifest.json') -PathType Leaf) {
    $agentRoot = (Resolve-Path -LiteralPath $bundledAgentRoot).Path
}
elseif (Test-Path -LiteralPath (Join-Path $legacyAgentRoot 'contracts\manifest.json') -PathType Leaf) {
    $agentRoot = (Resolve-Path -LiteralPath $legacyAgentRoot).Path
}
else {
    $agentRoot = $null
}
if ([string]::IsNullOrWhiteSpace($GodotExe)) {
    $GodotExe = Join-Path $workspaceRoot 'tools\godot-4.5.2\Godot_v4.5.2-stable_win64.exe'
}
$backendPython = Join-Path $backendRoot '.venv\Scripts\python.exe'
if (-not [string]::IsNullOrWhiteSpace($PythonExe)) {
    $backendPython = $PythonExe
}
if ([string]::IsNullOrWhiteSpace($UpstreamKeyFile)) {
    $UpstreamKeyFile = Join-Path (
        [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    ) '.walnut-secrets\deepseek-v4-flash.key'
}

$postgresName = 'walnut-play-postgres'
$postgresPort = 55433
$relayPort = 20999
$gatewayPort = 8790
$postgresVolume = 'walnut-play-pgdata'
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'WalnutWorld\persistent-play'
$statePath = Join-Path $runtimeRoot 'state.json'

function New-RandomHex([int]$ByteCount = 32) {
    $bytes = [byte[]]::new($ByteCount)
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
        return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $rng.Dispose()
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function ConvertTo-Base64Url([byte[]]$Bytes) {
    return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-StudentJwt {
    param([string]$Secret, [string]$Issuer, [string]$Audience, [int]$LifetimeSeconds)
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $header = [ordered]@{ alg = 'HS256'; typ = 'JWT' } | ConvertTo-Json -Compress
    $claims = [ordered]@{
        iss = $Issuer; aud = $Audience; sub = 'student_0001'; tenant_id = 'tenant_yaya'
        actor_id = 'student_0001'; actor_type = 'student'; roles = @('game:player')
        iat = $now; nbf = $now; exp = $now + $LifetimeSeconds
    } | ConvertTo-Json -Compress
    $unsigned = "$(ConvertTo-Base64Url ([Text.Encoding]::UTF8.GetBytes($header))).$(ConvertTo-Base64Url ([Text.Encoding]::UTF8.GetBytes($claims)))"
    $hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($Secret))
    try {
        $signature = $hmac.ComputeHash([Text.Encoding]::ASCII.GetBytes($unsigned))
        return "$unsigned.$(ConvertTo-Base64Url $signature)"
    }
    finally { $hmac.Dispose() }
}

function Protect-RunDirectory {
    param([Parameter(Mandatory)][string]$Path)

    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $accessSection = [Security.AccessControl.AccessControlSections]::Access
    $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $directory = [IO.DirectoryInfo]::new($Path)
    if ($PSVersionTable.PSEdition -eq 'Desktop') {
        $acl = [IO.Directory]::GetAccessControl($Path, $accessSection)
    }
    else {
        $acl = [IO.FileSystemAclExtensions]::GetAccessControl($directory, $accessSection)
    }

    $expectedSidValues = @($currentUser.Value, $system.Value)
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    $isExact = $acl.AreAccessRulesProtected -and $rules.Count -eq $expectedSidValues.Count
    if ($isExact) {
        foreach ($sidValue in $expectedSidValues) {
            $matchingRules = @($rules | Where-Object {
                $_.IdentityReference.Value -ceq $sidValue -and
                -not $_.IsInherited -and
                $_.AccessControlType -eq $allow -and
                $_.FileSystemRights -eq [Security.AccessControl.FileSystemRights]::FullControl -and
                $_.InheritanceFlags -eq $inheritance -and
                $_.PropagationFlags -eq $propagation
            })
            if ($matchingRules.Count -ne 1) {
                $isExact = $false
                break
            }
        }
    }
    if ($isExact) {
        return
    }

    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @(
        $acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])
    )) {
        [void]$acl.RemoveAccessRuleSpecific($rule)
    }
    foreach ($sid in @($currentUser, $system)) {
        $acl.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                $sid,
                [Security.AccessControl.FileSystemRights]::FullControl,
                $inheritance,
                $propagation,
                $allow
            )
        )
    }
    # The descriptor was loaded with Access only, so this persists the DACL only.
    if ($PSVersionTable.PSEdition -eq 'Desktop') {
        [IO.Directory]::SetAccessControl($Path, $acl)
    }
    else {
        [IO.FileSystemAclExtensions]::SetAccessControl($directory, $acl)
    }
}

function Read-PersistentState {
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        return $null
    }
    $json = [IO.File]::ReadAllText($statePath, [Text.Encoding]::UTF8)
    if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {
        return $json | ConvertFrom-Json -DateKind String
    }
    return $json | ConvertFrom-Json
}

function Write-PersistentState {
    param([Parameter(Mandatory)][object]$State)

    $json = $State | ConvertTo-Json -Depth 8 -Compress
    [IO.File]::WriteAllText($statePath, $json, [Text.UTF8Encoding]::new($false))
}

function Set-StateValue {
    param(
        [Parameter(Mandatory)][object]$State,
        [Parameter(Mandatory)][string]$Name,
        [AllowNull()][object]$Value
    )

    if ($State.PSObject.Properties.Name -contains $Name) {
        $State.$Name = $Value
    }
    else {
        $State | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Get-RecordedProcess {
    param(
        [Parameter(Mandatory)][object]$State,
        [Parameter(Mandatory)][string]$PidName,
        [Parameter(Mandatory)][string]$StartedAtName,
        [Parameter(Mandatory)][string]$CommandMarker
    )

    if (
        $State.PSObject.Properties.Name -notcontains $PidName -or
        $null -eq $State.$PidName
    ) {
        return $null
    }
    if (
        $State.PSObject.Properties.Name -notcontains $StartedAtName -or
        [string]::IsNullOrWhiteSpace([string]$State.$StartedAtName)
    ) {
        throw "Recorded process '$PidName' has no start-time identity."
    }
    $processId = [int]$State.$PidName
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
    if ($null -eq $cim -or [string]$cim.CommandLine -notlike $CommandMarker) {
        throw "Recorded PID $processId is not the scoped '$PidName' process."
    }
    $recordedStart = [DateTimeOffset]::Parse([string]$State.$StartedAtName).UtcDateTime
    if ([Math]::Abs(($process.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 2) {
        throw "Recorded PID $processId start time changed; refusing a reused PID."
    }
    return $process
}

function Get-RecordedListenerProcess {
    param(
        [AllowNull()][System.Diagnostics.Process]$Launcher,
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$CommandMarker
    )

    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalAddress '127.0.0.1' -LocalPort $Port `
            -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -eq 0) {
        return $null
    }
    if ($listeners.Count -ne 1 -or $null -eq $Launcher) {
        throw "Port $Port has a competing or unrecorded listener."
    }
    $listenerProcessId = [int]$listeners[0].OwningProcess
    if ($listenerProcessId -eq $Launcher.Id) {
        return $Launcher
    }
    $listener = Get-Process -Id $listenerProcessId -ErrorAction SilentlyContinue
    $listenerCim = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerProcessId"
    if (
        $null -eq $listener -or
        $null -eq $listenerCim -or
        [int]$listenerCim.ParentProcessId -ne $Launcher.Id -or
        [string]$listenerCim.CommandLine -notlike $CommandMarker
    ) {
        throw "Port $Port listener is not the recorded launcher or its direct child."
    }
    return $listener
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

function Start-ProviderBlindBackendChild {
    param(
        [Parameter(Mandatory)][string[]]$ChildArguments,
        [Parameter(Mandatory)][string]$StandardOutputPath,
        [Parameter(Mandatory)][string]$StandardErrorPath
    )

    $previousDirectKey = [Environment]::GetEnvironmentVariable(
        'WALNUT_LLM_UPSTREAM_API_KEY', 'Process'
    )
    $previousKeyFile = [Environment]::GetEnvironmentVariable(
        'WALNUT_LLM_UPSTREAM_API_KEY_FILE', 'Process'
    )
    try {
        [Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY', $null, 'Process')
        [Environment]::SetEnvironmentVariable(
            'WALNUT_LLM_UPSTREAM_API_KEY_FILE', $null, 'Process'
        )
        return Start-Process -FilePath $backendPython -ArgumentList $ChildArguments `
            -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $StandardOutputPath `
            -RedirectStandardError $StandardErrorPath
    }
    finally {
        [Environment]::SetEnvironmentVariable(
            'WALNUT_LLM_UPSTREAM_API_KEY', $previousDirectKey, 'Process'
        )
        [Environment]::SetEnvironmentVariable(
            'WALNUT_LLM_UPSTREAM_API_KEY_FILE', $previousKeyFile, 'Process'
        )
    }
}

function Test-StateHasRecordedCoreIdentity {
    param([Parameter(Mandatory)][object]$State)

    foreach ($name in @('relay_pid', 'gateway_pid', 'worker_pid', 'learner_pid')) {
        if ($State.PSObject.Properties.Name -contains $name -and $null -ne $State.$name) {
            return $true
        }
    }
    return $false
}

function Test-ScopedPostgresRunning {
    if ($null -eq (Get-Command docker -CommandType Application -ErrorAction SilentlyContinue)) {
        return $false
    }
    $raw = & docker inspect $postgresName 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($raw -join ''))) {
        return $false
    }
    $items = @($raw | ConvertFrom-Json)
    if ($items.Count -ne 1 -or [string]$items[0].Name -cne "/$postgresName") {
        throw "PostgreSQL container name does not match the scoped persistent-play authority."
    }
    return [bool]$items[0].State.Running
}

function Get-PersistentProcesses {
    param([Parameter(Mandatory)][object]$State)

    return [ordered]@{
        relay = Get-RecordedProcess -State $State -PidName 'relay_pid' `
            -StartedAtName 'relay_started_at' -CommandMarker '*walnut_backend.llm_relay.main*'
        gateway = Get-RecordedProcess -State $State -PidName 'gateway_pid' `
            -StartedAtName 'gateway_started_at' -CommandMarker '*uvicorn*walnut_backend.main:app*'
        worker = Get-RecordedProcess -State $State -PidName 'worker_pid' `
            -StartedAtName 'worker_started_at' -CommandMarker '*walnut_backend.worker_main*'
        learner = Get-RecordedProcess -State $State -PidName 'learner_pid' `
            -StartedAtName 'learner_started_at' -CommandMarker '*walnut_backend.learner_worker_main*'
        game = Get-RecordedProcess -State $State -PidName 'game_pid' `
            -StartedAtName 'game_started_at' -CommandMarker '*--path*'
    }
}

function Show-PersistentStatus {
    param([AllowNull()][object]$State)

    if ($null -eq $State) {
        [pscustomobject]@{
            status = 'STOPPED'
            state = 'ABSENT'
            postgres_running = Test-ScopedPostgresRunning
            relay_running = $false
            gateway_running = $false
            worker_running = $false
            learner_running = $false
            game_running = $false
        } | ConvertTo-Json -Compress
        return
    }
    $processes = Get-PersistentProcesses -State $State
    $relayListener = Get-RecordedListenerProcess -Launcher $processes.relay `
        -Port $relayPort -CommandMarker '*walnut_backend.llm_relay.main*'
    $gatewayListener = Get-RecordedListenerProcess -Launcher $processes.gateway `
        -Port $gatewayPort -CommandMarker '*uvicorn*walnut_backend.main:app*'
    $coreHealthy = (
        $null -ne $processes.relay -and $null -ne $relayListener -and
        $null -ne $processes.gateway -and $null -ne $gatewayListener -and
        $null -ne $processes.worker -and $null -ne $processes.learner
    )
    $anyCore = (Test-StateHasRecordedCoreIdentity -State $State) -or (
        $null -ne $processes.relay -or $null -ne $processes.gateway -or
        $null -ne $processes.worker -or $null -ne $processes.learner
    )
    [pscustomobject]@{
        status = if ($coreHealthy) { 'RUNNING' } elseif ($anyCore) { 'DEGRADED' } else { 'STOPPED' }
        state = 'PRESENT'
        postgres_running = Test-ScopedPostgresRunning
        relay_running = $null -ne $relayListener
        gateway_running = $null -ne $gatewayListener
        worker_running = $null -ne $processes.worker
        learner_running = $null -ne $processes.learner
        game_running = $null -ne $processes.game
        provider_key_source = 'WALNUT_LLM_UPSTREAM_API_KEY_FILE'
    } | ConvertTo-Json -Compress
}

function Stop-PersistentPlay {
    param([AllowNull()][object]$State)

    if ($null -eq $State) {
        '{"status":"STOPPED","already_stopped":true}'
        return
    }
    $processes = Get-PersistentProcesses -State $State
    $relayListener = Get-RecordedListenerProcess -Launcher $processes.relay `
        -Port $relayPort -CommandMarker '*walnut_backend.llm_relay.main*'
    $gatewayListener = Get-RecordedListenerProcess -Launcher $processes.gateway `
        -Port $gatewayPort -CommandMarker '*uvicorn*walnut_backend.main:app*'
    Stop-ProcessAndWait -Process $processes.game
    Stop-ProcessAndWait -Process $processes.learner
    Stop-ProcessAndWait -Process $processes.worker
    if ($null -ne $gatewayListener -and $gatewayListener.Id -ne $processes.gateway.Id) {
        Stop-ProcessAndWait -Process $gatewayListener
    }
    Stop-ProcessAndWait -Process $processes.gateway
    if ($null -ne $relayListener -and $relayListener.Id -ne $processes.relay.Id) {
        Stop-ProcessAndWait -Process $relayListener
    }
    Stop-ProcessAndWait -Process $processes.relay
    if (Test-ScopedPostgresRunning) {
        & docker stop $postgresName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to stop scoped PostgreSQL container '$postgresName'."
        }
    }
    foreach ($name in @(
        'relay_pid', 'relay_started_at', 'gateway_pid', 'gateway_started_at',
        'worker_pid', 'worker_started_at', 'learner_pid', 'learner_started_at',
        'game_pid', 'game_started_at'
    )) {
        Set-StateValue -State $State -Name $name -Value $null
    }
    Set-StateValue -State $State -Name 'stopped_at' -Value ([DateTimeOffset]::UtcNow.ToString('o'))
    Write-PersistentState -State $State
    '{"status":"STOPPED","already_stopped":false,"volume_preserved":true}'
}

$state = Read-PersistentState

if ($Action -eq 'Status') {
    Show-PersistentStatus -State $state
    exit 0
}
if ($Action -eq 'Stop') {
    if (Test-Path -LiteralPath $runtimeRoot -PathType Container) {
        Protect-RunDirectory -Path $runtimeRoot
    }
    Stop-PersistentPlay -State $state
    exit 0
}

if (-not (Test-Path -LiteralPath $backendPython -PathType Leaf)) { throw "Backend Python missing: $backendPython" }
if (-not (Test-Path -LiteralPath $GodotExe -PathType Leaf)) { throw "Godot missing: $GodotExe" }
if (-not (Test-Path -LiteralPath $UpstreamKeyFile -PathType Leaf)) { throw "Provider key file missing: $UpstreamKeyFile" }
if ($null -eq $agentRoot) { throw "Agent workspace was not found at $bundledAgentRoot or $legacyAgentRoot" }
if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot 'project.godot') -PathType Leaf)) {
    throw "Godot project missing: $frontendRoot"
}

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
Protect-RunDirectory -Path $runtimeRoot
New-Item -ItemType Directory -Path (Join-Path $runtimeRoot 'sandbox-results') -Force | Out-Null

if ($null -eq $state) {
    $state = [pscustomobject][ordered]@{
        postgres_name = $postgresName
        postgres_port = $postgresPort
        relay_port = $relayPort
        database_password = New-RandomHex
        relay_secret = New-RandomHex
        auth_secret = New-RandomHex
        feishu_pseudonym_secret = New-RandomHex
    }
    Write-PersistentState -State $state
}
elseif ($state.PSObject.Properties.Name -notcontains 'feishu_pseudonym_secret') {
    $state | Add-Member -NotePropertyName feishu_pseudonym_secret -NotePropertyValue (New-RandomHex)
    Write-PersistentState -State $state
}

$databasePassword = [string]$state.database_password
$relaySecret = [string]$state.relay_secret
$authSecret = [string]$state.auth_secret
$feishuPseudonymSecret = [string]$state.feishu_pseudonym_secret

if (
    [string]$state.postgres_name -cne $postgresName -or
    [int]$state.postgres_port -ne $postgresPort -or
    [int]$state.relay_port -ne $relayPort
) {
    throw 'Persistent-play state does not match the scoped container and ports.'
}
$existingProcesses = Get-PersistentProcesses -State $state
$existingRelayListener = Get-RecordedListenerProcess -Launcher $existingProcesses.relay `
    -Port $relayPort -CommandMarker '*walnut_backend.llm_relay.main*'
$existingGatewayListener = Get-RecordedListenerProcess -Launcher $existingProcesses.gateway `
    -Port $gatewayPort -CommandMarker '*uvicorn*walnut_backend.main:app*'
$coreHealthy = (
    $null -ne $existingProcesses.relay -and $null -ne $existingRelayListener -and
    $null -ne $existingProcesses.gateway -and $null -ne $existingGatewayListener -and
    $null -ne $existingProcesses.worker -and $null -ne $existingProcesses.learner
)
$hasRecordedCore = Test-StateHasRecordedCoreIdentity -State $state
$anyCore = $hasRecordedCore -or (
    $null -ne $existingProcesses.relay -or $null -ne $existingRelayListener -or
    $null -ne $existingProcesses.gateway -or $null -ne $existingGatewayListener -or
    $null -ne $existingProcesses.worker -or $null -ne $existingProcesses.learner
)
if ($anyCore -and -not $coreHealthy) {
    throw 'Persistent-play runtime is incomplete; use -Action Stop before restarting it.'
}
if ($null -ne $existingProcesses.game -and -not $coreHealthy) {
    throw 'A recorded Godot process exists without a healthy persistent-play runtime.'
}
if ($coreHealthy -and $null -ne $existingProcesses.game) {
    Write-Output 'PERSISTENT_PLAY_ALREADY_RUNNING'
    Show-PersistentStatus -State $state
    exit 0
}

Write-Output "PERSISTENT_PLAY_STATE postgres_port=$postgresPort relay_port=$relayPort"

# ---- 1. PostgreSQL ----
$postgresStartedThisInvocation = $false
$pgRunning = docker ps --format '{{.Names}}' 2>$null | Select-String -Quiet ("^$postgresName$")
if (-not $pgRunning) {
    $pgExists = docker ps -a --format '{{.Names}}' 2>$null | Select-String -Quiet ("^$postgresName$")
    if (-not $pgExists) {
        docker volume create $postgresVolume | Out-Null
        docker run -d --name $postgresName `
            --publish "127.0.0.1:${postgresPort}:5432" `
            --mount "type=volume,source=$postgresVolume,target=/var/lib/postgresql/data" `
            --env POSTGRES_DB=walnut_int1 --env POSTGRES_USER=walnut `
            --env "POSTGRES_PASSWORD=$databasePassword" `
            --health-cmd '"pg_isready -U walnut -d walnut_int1"' `
            --health-interval 1s --health-timeout 3s --health-retries 30 `
            $PostgresImage | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create scoped PostgreSQL container '$postgresName'."
        }
        $postgresStartedThisInvocation = $true
        Write-Output "PERSISTENT_PLAY_POSTGRES created"
    }
    else {
        docker start $postgresName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to start scoped PostgreSQL container '$postgresName'."
        }
        $postgresStartedThisInvocation = $true
        Write-Output "PERSISTENT_PLAY_POSTGRES started existing"
    }
}
$pgHealthy = $false
foreach ($i in 1..60) {
    docker exec $postgresName pg_isready -U walnut -d walnut_int1 *> $null
    if ($LASTEXITCODE -eq 0) { $pgHealthy = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $pgHealthy) { throw "PostgreSQL '$postgresName' did not become healthy." }
Write-Output "PERSISTENT_PLAY_POSTGRES healthy"

# ---- 2. Shared environment ----
$env:PYTHONPATH = (Join-Path $backendRoot 'src') + ';' + (Join-Path $agentRoot 'python')
$env:WALNUT_DATABASE_URL = "postgresql+asyncpg://walnut:$databasePassword@127.0.0.1:$postgresPort/walnut_int1"
$env:WALNUT_CONTRACT_PATH = $agentRoot
$env:WALNUT_CONTRACT_RELEASE_PATH = Join-Path $backendRoot 'contract-release.json'
$env:WALNUT_RUNTIME_ROOT = $runtimeRoot
$env:WALNUT_INT1_E2E_SEED = 'true'
$env:WALNUT_ENABLE_WORLD_PRESENTATION = 'true'
$env:WALNUT_ENABLE_SKILL_PATCH = 'true'
$env:WALNUT_DEVELOPMENT_AUTH = 'false'
$env:WALNUT_AUTH_HMAC_SECRET = $authSecret
$env:WALNUT_FEISHU_PSEUDONYM_SECRET = $feishuPseudonymSecret
$env:WALNUT_FEISHU_MCP_DASHBOARD_URL = 'https://larkcommunity.feishu.cn/base/Q6V9biulZaezaHskZqacYm2JnHe?table=blkK3ldZA0pePRGb'
$env:WALNUT_FEISHU_MCP_TEACHER_WORKSPACE_URL = 'https://dcniaqwtmoca.feishuapp.com/app/app_17c6bc5hz7d'
$env:WALNUT_AUTH_ISSUER = 'walnut-int1-local-diagnostic'
$env:WALNUT_AUTH_AUDIENCE = 'walnut-game-client'
$env:WALNUT_AUTH_CLOCK_SKEW_SECONDS = '5'
$env:WALNUT_AUTH_MAXIMUM_LIFETIME_SECONDS = [string]$TokenLifetimeSeconds
$env:WALNUT_TENANT_ID = 'tenant_yaya'
$env:WALNUT_WORKER_ID = 'persistent-play-worker'
$env:WALNUT_DOCKER_EXECUTABLE = 'docker'
$env:WALNUT_SANDBOX_IMAGE = $SandboxImage
$env:WALNUT_SANDBOX_CPU_MS = '1000'
$env:WALNUT_SANDBOX_WALL_MS = '15000'
$env:WALNUT_SANDBOX_MEMORY_BYTES = '536870912'
$env:WALNUT_SANDBOX_MAX_PROCESSES = '64'
$env:WALNUT_SANDBOX_MAX_OUTPUT_BYTES = '65536'
$env:WALNUT_LLM_RELAY_ENDPOINT = "http://127.0.0.1:$relayPort"
$env:WALNUT_LLM_RELAY_API_KEY = $relaySecret
$env:WALNUT_LLM_RELAY_ALLOW_INSECURE_LOCALHOST = 'true'
$env:WALNUT_LLM_RELAY_REQUIRED_RETENTION_SECONDS = '604800'
$env:WALNUT_LLM_RELAY_MAX_RESPONSE_BYTES = '2097152'
$env:WALNUT_LLM_RELAY_CAPABILITY_TIMEOUT_MS = '5000'
$env:WALNUT_LLM_PROVIDER = $Provider
$env:WALNUT_LLM_MODEL = $Model
$env:WALNUT_LLM_RESPONSE_FORMAT = 'json_object'
$env:WALNUT_LLM_THINKING_MODE = 'disabled'
$env:WALNUT_PROMPT_VERSION = 'int1-prompt-v1'
$env:WALNUT_TEACHING_SPEC_VERSION = 'agent-teaching-v1'
$env:WALNUT_WORLD_RULES_VERSION = 'farm-rules-1'
$env:WALNUT_WORLD_CONTENT_VERSION = '1.0.0'
$env:WALNUT_WORLD_SUCCESS_SCORE = '8'
$env:WALNUT_WORLD_WATERING_EXPECTED_UNITS = '2,1,1,0,0,2,0,1'
$env:WALNUT_INT1_TASK_MODE = 'watering'
$env:WALNUT_WORKER_LEASE_SECONDS = '120'
$env:WALNUT_WORKER_IDLE_POLL_SECONDS = '0.1'
$env:WALNUT_LEARNER_WORKER_ID = 'persistent-play-learner'
$env:WALNUT_LEARNER_WORKER_LEASE_SECONDS = '120'
$env:WALNUT_LEARNER_WORKER_IDLE_POLL_SECONDS = '0.1'
$env:WALNUT_LEARNER_WORKER_MAXIMUM_ATTEMPTS = '5'

# ---- 3. Migrate + seed, then independently prove current watering authority ----
Push-Location $backendRoot
try {
    & $backendPython -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Alembic failed with exit code $LASTEXITCODE." }
    Write-Output "PERSISTENT_PLAY_MIGRATED"
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $seedOutput = (& $backendPython -m walnut_backend.int1_e2e_authority 2>&1 | Out-String).Trim()
        $seedExit = $LASTEXITCODE
    }
    catch {
        $seedExit = 1
        $seedOutput = $_.Exception.Message
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    $seedWasRefused = $seedExit -ne 0 -and $seedOutput -match 'SEED_REFUSED'
    if ($seedExit -eq 0) {
        try {
            $seed = $seedOutput | ConvertFrom-Json
        }
        catch {
            throw 'Authority seed returned non-JSON output; sensitive output withheld.'
        }
        if ([string]$seed.status -cne 'SEEDED') {
            throw 'Authority seed returned an invalid status; sensitive output withheld.'
        }
    }
    elseif (-not $seedWasRefused) {
        throw "Authority seed failed with exit code $seedExit; output withheld."
    }
    $seed = $null
    $seedOutput = $null

    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $authorityOutput = (
            & $backendPython -m walnut_backend.persistent_play_authority 2>&1 | Out-String
        ).Trim()
        $authorityExit = $LASTEXITCODE
    }
    catch {
        $authorityExit = 1
        $authorityOutput = ''
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($authorityExit -ne 0) {
        $authorityReason = 'VERIFIER_EXECUTION_FAILED'
        if ($authorityOutput -match 'PERSISTENT_WATERING_AUTHORITY_INVALID code=([A-Z0-9_]+)') {
            $authorityReason = $Matches[1]
        }
        $authorityOutput = $null
        throw "Current watering authority verification failed: $authorityReason"
    }
    try {
        $authority = $authorityOutput | ConvertFrom-Json
    }
    catch {
        $authorityOutput = $null
        throw 'Current watering authority verifier returned invalid output.'
    }
    $authorityOutput = $null
    if (
        [string]$authority.status -cne 'CURRENT_WATERING_AUTHORITY_VALID' -or
        [int]$authority.authority_rows -ne 7 -or
        [bool]$authority.read_only -ne $true
    ) {
        throw 'Current watering authority verifier returned an invalid proof.'
    }
    if ($seedWasRefused) {
        Write-Output 'PERSISTENT_PLAY_SEEDED already-seeded (current watering authority verified)'
    }
    else {
        Write-Output 'PERSISTENT_PLAY_SEEDED status=SEEDED authority=current-watering-verified'
    }
}
catch {
    if ($postgresStartedThisInvocation -and (Test-ScopedPostgresRunning)) {
        & docker stop $postgresName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Authority setup failed and scoped PostgreSQL cleanup also failed."
        }
    }
    throw
}
finally {
    $seedOutput = $null
    $authorityOutput = $null
    Pop-Location
}

# ---- 4. Recoverable relay (real Provider, file-only credential injection) ----
$relayProcess = $existingProcesses.relay
if ($null -eq $relayProcess) {
    $env:WALNUT_LLM_RELAY_SERVER_API_KEY = $relaySecret
    $env:WALNUT_LLM_UPSTREAM_ENDPOINT = $UpstreamEndpoint
    $env:WALNUT_LLM_RELAY_BIND_HOST = '127.0.0.1'
    $env:WALNUT_LLM_RELAY_BIND_PORT = [string]$relayPort
    $relayLog = Join-Path $runtimeRoot 'relay.stdout.log'
    $relayErr = Join-Path $runtimeRoot 'relay.stderr.log'
    $previousDirectKey = [Environment]::GetEnvironmentVariable(
        'WALNUT_LLM_UPSTREAM_API_KEY', 'Process'
    )
    $previousKeyFile = [Environment]::GetEnvironmentVariable(
        'WALNUT_LLM_UPSTREAM_API_KEY_FILE', 'Process'
    )
    try {
        [Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY', $null, 'Process')
        [Environment]::SetEnvironmentVariable(
            'WALNUT_LLM_UPSTREAM_API_KEY_FILE',
            [IO.Path]::GetFullPath($UpstreamKeyFile),
            'Process'
        )
        $relayProcess = Start-Process -FilePath $backendPython `
            -ArgumentList @('-m', 'walnut_backend.llm_relay.main') `
            -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $relayLog -RedirectStandardError $relayErr
    }
    finally {
        [Environment]::SetEnvironmentVariable(
            'WALNUT_LLM_UPSTREAM_API_KEY', $previousDirectKey, 'Process'
        )
        [Environment]::SetEnvironmentVariable(
            'WALNUT_LLM_UPSTREAM_API_KEY_FILE', $previousKeyFile, 'Process'
        )
    }
    foreach ($i in 1..30) {
        if (Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $relayPort -ErrorAction SilentlyContinue) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not (Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $relayPort -ErrorAction SilentlyContinue)) {
        throw "Recoverable relay did not start on 127.0.0.1:$relayPort."
    }
    if ($relayProcess.HasExited) {
        throw "Recoverable relay exited during startup with code $($relayProcess.ExitCode)."
    }
    Write-Output "PERSISTENT_PLAY_RELAY started"
}

# ---- 5. Gateway + worker + learner worker ----
$gatewayProcess = $existingProcesses.gateway
$workerProcess = $existingProcesses.worker
$learnerProcess = $existingProcesses.learner
if ($null -eq $gatewayProcess) {
    $gatewayProcess = Start-ProviderBlindBackendChild `
        -ChildArguments @(
            '-m', 'uvicorn', 'walnut_backend.main:app',
            '--host', '127.0.0.1', '--port', [string]$gatewayPort
        ) `
        -StandardOutputPath (Join-Path $runtimeRoot 'gateway.stdout.log') `
        -StandardErrorPath (Join-Path $runtimeRoot 'gateway.stderr.log')
}
if ($null -eq $workerProcess) {
    $workerProcess = Start-ProviderBlindBackendChild `
        -ChildArguments @('-m', 'walnut_backend.worker_main') `
        -StandardOutputPath (Join-Path $runtimeRoot 'worker.stdout.log') `
        -StandardErrorPath (Join-Path $runtimeRoot 'worker.stderr.log')
}
if ($null -eq $learnerProcess) {
    $learnerProcess = Start-ProviderBlindBackendChild `
        -ChildArguments @('-m', 'walnut_backend.learner_worker_main') `
        -StandardOutputPath (Join-Path $runtimeRoot 'learner-worker.stdout.log') `
        -StandardErrorPath (Join-Path $runtimeRoot 'learner-worker.stderr.log')
}
if ($null -eq $existingGatewayListener) {
    foreach ($i in 1..45) {
        if (Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $gatewayPort -ErrorAction SilentlyContinue) { break }
        Start-Sleep -Seconds 1
    }
    if (-not (Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $gatewayPort -ErrorAction SilentlyContinue)) {
        throw "Gateway did not start on 127.0.0.1:$gatewayPort."
    }
    Write-Output "PERSISTENT_PLAY_GATEWAY ready"
}
Start-Sleep -Seconds 1
foreach ($component in @(
    [pscustomobject]@{ Name = 'relay'; Process = $relayProcess },
    [pscustomobject]@{ Name = 'gateway'; Process = $gatewayProcess },
    [pscustomobject]@{ Name = 'worker'; Process = $workerProcess },
    [pscustomobject]@{ Name = 'learner'; Process = $learnerProcess }
)) {
    if ($null -eq $component.Process -or $component.Process.HasExited) {
        throw "Persistent-play $($component.Name) process is not healthy after startup."
    }
}
[void](Get-RecordedListenerProcess -Launcher $relayProcess -Port $relayPort `
    -CommandMarker '*walnut_backend.llm_relay.main*')
[void](Get-RecordedListenerProcess -Launcher $gatewayProcess -Port $gatewayPort `
    -CommandMarker '*uvicorn*walnut_backend.main:app*')

Set-StateValue -State $state -Name 'runtime_version' -Value '1.0.0'
Set-StateValue -State $state -Name 'provider_started' -Value $true
Set-StateValue -State $state -Name 'relay_pid' -Value $relayProcess.Id
Set-StateValue -State $state -Name 'relay_started_at' `
    -Value $relayProcess.StartTime.ToUniversalTime().ToString('o')
Set-StateValue -State $state -Name 'gateway_pid' -Value $gatewayProcess.Id
Set-StateValue -State $state -Name 'gateway_started_at' `
    -Value $gatewayProcess.StartTime.ToUniversalTime().ToString('o')
Set-StateValue -State $state -Name 'worker_pid' -Value $workerProcess.Id
Set-StateValue -State $state -Name 'worker_started_at' `
    -Value $workerProcess.StartTime.ToUniversalTime().ToString('o')
Set-StateValue -State $state -Name 'learner_pid' -Value $learnerProcess.Id
Set-StateValue -State $state -Name 'learner_started_at' `
    -Value $learnerProcess.StartTime.ToUniversalTime().ToString('o')
Set-StateValue -State $state -Name 'started_at' -Value ([DateTimeOffset]::UtcNow.ToString('o'))
Write-PersistentState -State $state

# ---- 6. Launch visible game ----
$studentToken = New-StudentJwt -Secret $authSecret -Issuer 'walnut-int1-local-diagnostic' -Audience 'walnut-game-client' -LifetimeSeconds $TokenLifetimeSeconds
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $GodotExe
$start.Arguments = "--path `"$frontendRoot`""
$start.WorkingDirectory = $frontendRoot
$start.UseShellExecute = $false
foreach ($secretName in @(
    'WALNUT_DATABASE_URL',
    'WALNUT_AUTH_HMAC_SECRET',
    'WALNUT_FEISHU_PSEUDONYM_SECRET',
    'WALNUT_LLM_RELAY_API_KEY',
    'WALNUT_LLM_RELAY_SERVER_API_KEY',
    'WALNUT_LLM_UPSTREAM_API_KEY',
    'WALNUT_LLM_UPSTREAM_API_KEY_FILE'
)) {
    $start.EnvironmentVariables.Remove($secretName) | Out-Null
}
$start.EnvironmentVariables['YAYA_API_BASE_URL'] = "http://127.0.0.1:$gatewayPort"
$start.EnvironmentVariables['YAYA_AUTH_TOKEN'] = $studentToken
$game = [Diagnostics.Process]::Start($start)
Start-Sleep -Seconds 2
if ($game.HasExited) { throw "Godot exited during launch with code $($game.ExitCode)." }
Set-StateValue -State $state -Name 'game_pid' -Value $game.Id
Set-StateValue -State $state -Name 'game_started_at' `
    -Value $game.StartTime.ToUniversalTime().ToString('o')
Set-StateValue -State $state -Name 'godot_executable' -Value ([IO.Path]::GetFullPath($GodotExe))
Set-StateValue -State $state -Name 'frontend_root' -Value ([IO.Path]::GetFullPath($frontendRoot))
Write-PersistentState -State $state
$studentToken = $null
Write-Output "PERSISTENT_PLAY_GAME_STARTED pid=$($game.Id)"
Write-Output "PERSISTENT_PLAY_READY gateway=$gatewayPort relay=$relayPort token_lifetime=$TokenLifetimeSeconds"
Show-PersistentStatus -State $state
