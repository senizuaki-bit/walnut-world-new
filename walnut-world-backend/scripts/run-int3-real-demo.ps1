[CmdletBinding()]
param(
    [string]$GodotExe = $env:GODOT_EXE,
    [string]$RuntimeParent = (Join-Path $env:SystemDrive 'w3'),
    [string]$ProviderConfigPath = (Join-Path (
        [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    ) 'WalnutINT3\secrets\provider.env'),
    [string]$PostgresContainer = 'walnut-int3-postgres',
    [ValidateRange(1024, 65535)]
    [int]$PostgresPort = 55432,
    [ValidateRange(1024, 65535)]
    [int]$RelayPort = 18791,
    [ValidateRange(120, 840)]
    [int]$TotalDeadlineSeconds = 600,
    [ValidateRange(30, 300)]
    [int]$ResourceDeadlineSeconds = 180,
    [ValidateRange(15, 180)]
    [int]$InteractionDeadlineSeconds = 90
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:TenantId = 'tenant_yaya'
$script:StudentId = 'student_0001'
$script:GatewayPort = 8790
$script:Provider = 'deepseek'
$script:Model = 'deepseek-v4-flash'
$script:UpstreamEndpoint = 'https://api.deepseek.com/chat/completions'
$script:SandboxImage = 'gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c'
$script:ExpectedContractVersion = '0.4.0'
$script:ExpectedBackendRuntimeVersion = '1.0.0'
$script:RelayProtocol = 'YAYA_RECOVERABLE_LLM_V1'
$script:MaximumWindowsPath = 259

$backendRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = Split-Path -Parent $backendRoot
$frontendRoot = Join-Path $workspaceRoot 'walnut-world-frontend'
$agentRoot = Join-Path $workspaceRoot 'agent'
$backendPython = Join-Path $backendRoot '.venv\Scripts\python.exe'
$frontendRunner = Join-Path $frontendRoot 'scripts\run-real-gateway-e2e.ps1'
$contractVerifier = Join-Path $backendRoot 'scripts\verify_contract_release.py'
$localApplicationData = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::LocalApplicationData
)
$backendStatePath = Join-Path $localApplicationData 'WalnutWorld\int3-aily-backend\active.json'
$runId = [Guid]::NewGuid().ToString('N').Substring(0, 8)
$runDirectory = [IO.Path]::GetFullPath((Join-Path $RuntimeParent $runId))
$phase1FingerprintPath = Join-Path $runDirectory 'p.json'
$providerKeyPath = Join-Path $runDirectory 'k'
$ownedProcesses = [Collections.Generic.List[object]]::new()
$providerKey = $null
$studentAuthorization = $null
$relaySecret = $null
$databaseUrl = $null
$runSucceeded = $false

$environmentNames = @(
    'PYTHONPATH',
    'PYTHONUTF8',
    'WALNUT_DATABASE_URL',
    'WALNUT_CONTRACT_PATH',
    'WALNUT_RUNTIME_ROOT',
    'WALNUT_AUTH_HMAC_SECRET',
    'WALNUT_FEISHU_PSEUDONYM_SECRET',
    'POSTGRES_PASSWORD',
    'WALNUT_TENANT_ID',
    'WALNUT_WORKER_ID',
    'WALNUT_LEARNER_WORKER_ID',
    'WALNUT_DOCKER_EXECUTABLE',
    'WALNUT_SANDBOX_IMAGE',
    'WALNUT_SANDBOX_CPU_MS',
    'WALNUT_SANDBOX_WALL_MS',
    'WALNUT_SANDBOX_MEMORY_BYTES',
    'WALNUT_SANDBOX_MAX_PROCESSES',
    'WALNUT_SANDBOX_MAX_OUTPUT_BYTES',
    'WALNUT_ENABLE_WORLD_PRESENTATION',
    'WALNUT_ENABLE_SKILL_PATCH',
    'WALNUT_LLM_PROVIDER',
    'WALNUT_LLM_MODEL',
    'WALNUT_LLM_RESPONSE_FORMAT',
    'WALNUT_LLM_THINKING_MODE',
    'WALNUT_LLM_RELAY_ENDPOINT',
    'WALNUT_LLM_RELAY_API_KEY',
    'WALNUT_LLM_RELAY_ALLOW_INSECURE_LOCALHOST',
    'WALNUT_LLM_RELAY_REQUIRED_RETENTION_SECONDS',
    'WALNUT_LLM_RELAY_MAX_RESPONSE_BYTES',
    'WALNUT_LLM_RELAY_CAPABILITY_TIMEOUT_MS',
    'WALNUT_LLM_RELAY_SERVER_API_KEY',
    'WALNUT_LLM_RELAY_BIND_HOST',
    'WALNUT_LLM_RELAY_BIND_PORT',
    'WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS',
    'WALNUT_LLM_UPSTREAM_ENDPOINT',
    'WALNUT_LLM_UPSTREAM_TIMEOUT_MS',
    'WALNUT_LLM_UPSTREAM_API_KEY',
    'WALNUT_LLM_UPSTREAM_API_KEY_FILE',
    'WALNUT_LLM_API_KEY',
    'WALNUT_PROMPT_VERSION',
    'WALNUT_TEACHING_SPEC_VERSION',
    'WALNUT_WORLD_RULES_VERSION',
    'WALNUT_WORLD_CONTENT_VERSION',
    'WALNUT_WORLD_SUCCESS_SCORE',
    'WALNUT_WORKER_LEASE_SECONDS',
    'WALNUT_WORKER_IDLE_POLL_SECONDS',
    'WALNUT_LEARNER_WORKER_LEASE_SECONDS',
    'WALNUT_LEARNER_WORKER_IDLE_POLL_SECONDS',
    'WALNUT_LEARNER_WORKER_MAXIMUM_ATTEMPTS',
    'DEEPSEEK_API_KEY',
    'OPENAI_API_KEY',
    'YAYA_API_BASE_URL',
    'YAYA_AUTH_TOKEN',
    'GODOT_EXE'
)
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
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

function Assert-SecretFileAcl {
    param([Parameter(Mandatory)][string]$Path)

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (
        -not $item.PSIsContainer -and
        (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)
    ) {
        $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
        $raw = [Security.AccessControl.RawSecurityDescriptor]::new(
            $acl.GetSecurityDescriptorBinaryForm(),
            0
        )
        $daclPresent = (
            $raw.ControlFlags -band
            [Security.AccessControl.ControlFlags]::DiscretionaryAclPresent
        ) -ne 0
        if (-not $daclPresent -or $null -eq $raw.DiscretionaryAcl) {
            throw 'Provider credential file has no fail-closed DACL.'
        }
        $broadSids = @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545')
        $readData = [int64][Security.AccessControl.FileSystemRights]::ReadData
        $rules = $acl.GetAccessRules(
            $true,
            $true,
            [Security.Principal.SecurityIdentifier]
        )
        foreach ($rule in $rules) {
            if (
                $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                $broadSids -contains [string]$rule.IdentityReference.Value -and
                (([int64]$rule.FileSystemRights -band $readData) -ne 0)
            ) {
                throw 'Provider credential file grants broad read access.'
            }
        }
        return
    }
    throw 'Provider credential path must be a regular non-reparse file.'
}

function Read-ProviderKey {
    param([Parameter(Mandatory)][string]$Path)

    $fullPath = [IO.Path]::GetFullPath($Path)
    Assert-SecretFileAcl -Path $fullPath
    $itemBefore = Get-Item -LiteralPath $fullPath -Force
    if ($itemBefore.Length -gt 4098) {
        throw 'Provider credential file is too large.'
    }
    $bytes = [IO.File]::ReadAllBytes($fullPath)
    try {
        $itemAfter = Get-Item -LiteralPath $fullPath -Force
        if (
            $itemAfter.Length -ne $itemBefore.Length -or
            $itemAfter.LastWriteTimeUtc -ne $itemBefore.LastWriteTimeUtc
        ) {
            throw 'Provider credential file changed while it was read.'
        }
        $text = [Text.UTF8Encoding]::new($false, $true).GetString($bytes)
        $text = $text.TrimEnd("`r", "`n")
        $match = [regex]::Match(
            $text,
            '\AWALNUT_LLM_API_KEY=(?<secret>[^\s=]{16,4096})\z',
            [Text.RegularExpressions.RegexOptions]::CultureInvariant
        )
        if (-not $match.Success) {
            throw 'Provider credential file must contain exactly WALNUT_LLM_API_KEY=<secret>.'
        }
        return [string]$match.Groups['secret'].Value
    }
    finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
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
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Write-TemporaryProviderKey {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Value
    )

    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Value)
    try {
        $stream = [IO.FileStream]::new(
            $Path,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally {
            $stream.Dispose()
        }
        Assert-SecretFileAcl -Path $Path
    }
    finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Assert-ShortRuntimePath {
    param([Parameter(Mandatory)][string]$Path)

    if ($Path -notmatch '^[A-Za-z]:\\') {
        throw 'Runtime path must be an absolute local Windows drive path.'
    }
    $repositoryPrefix = [IO.Path]::GetFullPath($workspaceRoot).TrimEnd('\') + '\'
    $candidatePrefix = [IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
    if ($candidatePrefix.StartsWith($repositoryPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Runtime path must stay outside the three frozen project repositories.'
    }
    $hash = 'f' * 64
    $knownPaths = @(
        (Join-Path $Path "build-workspaces\build-$hash\receipts\$hash.json"),
        (Join-Path $Path "sandbox-results\ff\$hash.launch.json"),
        (Join-Path $Path "sandbox-results\ff\$hash.json"),
        (Join-Path $Path "artifacts\ff\$hash"),
        (Join-Path $Path 'p.json'),
        (Join-Path $Path 'learner.stderr.log')
    )
    $longest = ($knownPaths | Sort-Object Length -Descending | Select-Object -First 1)
    if ($longest.Length -gt $script:MaximumWindowsPath) {
        throw "Runtime path would create a known receipt path of $($longest.Length) characters."
    }
    return [pscustomobject]@{
        longest_known_path_characters = [int]$longest.Length
        maximum_allowed_characters = [int]$script:MaximumWindowsPath
    }
}

function Get-ExactBackendState {
    if (-not (Test-Path -LiteralPath $backendStatePath -PathType Leaf)) {
        throw 'The reusable INT3 production Backend has no active runtime record.'
    }
    $state = Get-Content -LiteralPath $backendStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        [string]$state.runtime_version -cne $script:ExpectedBackendRuntimeVersion -or
        [int]$state.backend_port -ne $script:GatewayPort -or
        [string]$state.tenant_id -cne $script:TenantId -or
        [bool]$state.development_auth -ne $false -or
        [int]$state.maximum_jwt_lifetime_seconds -lt ($TotalDeadlineSeconds + 60)
    ) {
        throw 'The recorded Backend runtime/version/auth profile is not the INT3 production authority.'
    }
    $expectedRuntimeRoot = [IO.Path]::GetFullPath((Join-Path (
        $localApplicationData
    ) 'WalnutWorld\int3-aily-backend')).TrimEnd('\') + '\'
    $stateRunDirectory = [IO.Path]::GetFullPath([string]$state.run_directory)
    if (-not $stateRunDirectory.StartsWith(
        $expectedRuntimeRoot,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Backend state points outside its scoped runtime root.'
    }
    $launcher = Get-Process -Id ([int]$state.backend_pid) -ErrorAction Stop
    $launcherCim = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$state.backend_pid)"
    $recordedStart = [DateTimeOffset]::Parse([string]$state.process_started_at).UtcDateTime
    if (
        $null -eq $launcherCim -or
        $launcherCim.CommandLine -notlike '*uvicorn*walnut_backend.main:app*' -or
        $launcherCim.CommandLine -notlike "*--port $($script:GatewayPort)*" -or
        [Math]::Abs(($launcher.StartTime.ToUniversalTime() - $recordedStart).TotalSeconds) -gt 2
    ) {
        throw 'Backend launcher PID no longer matches the recorded production process.'
    }
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $script:GatewayPort `
        -ErrorAction SilentlyContinue)
    if (
        $listeners.Count -ne 1 -or
        [string]$listeners[0].LocalAddress -cne '127.0.0.1' -or
        [int]$listeners[0].OwningProcess -ne [int]$state.backend_listener_pid
    ) {
        throw 'Port 8790 is not owned by the exact recorded production Backend listener.'
    }
    $listenerCim = Get-CimInstance Win32_Process -Filter (
        "ProcessId=$([int]$state.backend_listener_pid)"
    )
    if (
        $null -eq $listenerCim -or
        [int]$listenerCim.ParentProcessId -ne [int]$state.backend_pid -or
        $listenerCim.CommandLine -notlike '*uvicorn*walnut_backend.main:app*'
    ) {
        throw 'Backend listener is not the recorded launcher child.'
    }
    return $state
}

function Get-DatabaseUrl {
    $raw = & docker inspect $PostgresContainer 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($raw -join ''))) {
        throw 'The scoped INT3 PostgreSQL container is unavailable.'
    }
    $items = @($raw | ConvertFrom-Json)
    if (
        $items.Count -ne 1 -or
        [string]$items[0].Name -cne "/$PostgresContainer" -or
        [string]$items[0].Id -cnotmatch '^[a-f0-9]{64}$' -or
        -not [bool]$items[0].State.Running
    ) {
        throw 'The scoped INT3 PostgreSQL container is not uniquely running.'
    }
    $bindings = @($items[0].NetworkSettings.Ports.'5432/tcp')
    if (
        $bindings.Count -ne 1 -or
        [string]$bindings[0].HostIp -cne '127.0.0.1' -or
        [int]$bindings[0].HostPort -ne $PostgresPort
    ) {
        throw 'INT3 PostgreSQL must have exactly the expected loopback binding.'
    }
    $values = @{}
    foreach ($entry in @($items[0].Config.Env)) {
        $name, $value = ([string]$entry).Split('=', 2)
        $values[$name] = $value
    }
    $user = if ($values.ContainsKey('POSTGRES_USER')) { [string]$values.POSTGRES_USER } else { 'postgres' }
    $database = if ($values.ContainsKey('POSTGRES_DB')) { [string]$values.POSTGRES_DB } else { $user }
    $password = if ($values.ContainsKey('POSTGRES_PASSWORD')) { [string]$values.POSTGRES_PASSWORD } else { $null }
    if ([string]::IsNullOrEmpty($password)) {
        if (-not $values.ContainsKey('POSTGRES_HOST_AUTH_METHOD') -or [string]$values.POSTGRES_HOST_AUTH_METHOD -cne 'trust') {
            throw 'The scoped PostgreSQL container has no usable local authentication authority.'
        }
        return "postgresql://$([Uri]::EscapeDataString($user))@127.0.0.1:$PostgresPort/$([Uri]::EscapeDataString($database))"
    }
    return "postgresql://$([Uri]::EscapeDataString($user)):$([Uri]::EscapeDataString($password))@127.0.0.1:$PostgresPort/$([Uri]::EscapeDataString($database))"
}

function Invoke-DatabaseProbe {
    param([ValidateSet('baseline', 'result')][string]$Mode)

    $probe = @'
import asyncio
import json
import os

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text

from walnut_backend.adapters.postgres.models import Base
from walnut_backend.adapters.postgres.session import create_session_factory

ALLOWED = {
    "product_content_units", "world_snapshots", "learner_profiles",
    "agent_profiles", "build_policies", "launch_authorities", "registry_heads",
}

async def main():
    mode = os.environ["WALNUT_INT3_DEMO_PROBE_MODE"]
    sessions = create_session_factory(os.environ["WALNUT_DATABASE_URL"])
    try:
        async with sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ ONLY"))
            cfg = Config(os.path.join(os.environ["WALNUT_INT3_BACKEND_ROOT"], "alembic.ini"))
            cfg.set_main_option("script_location", os.path.join(os.environ["WALNUT_INT3_BACKEND_ROOT"], "migrations"))
            expected_head = ScriptDirectory.from_config(cfg).get_current_head()
            actual_heads = tuple(await session.scalars(text("SELECT version_num FROM alembic_version")))
            if actual_heads != (expected_head,):
                raise RuntimeError("database migration head differs from this Backend checkout")
            counts = {
                table.name: int(await session.scalar(select(func.count()).select_from(table)) or 0)
                for table in Base.metadata.sorted_tables
            }
            if mode == "baseline":
                expected = {name: (1 if name in ALLOWED else 0) for name in counts}
                expected["audit_records"] = counts.get("audit_records", 0)
                if counts != expected:
                    drift = {name: value for name, value in counts.items() if value != expected[name]}
                    raise RuntimeError("database is not the seven-row authority baseline: " + json.dumps(drift, sort_keys=True))
                exact = bool(await session.scalar(text("""
                    SELECT
                      (SELECT count(*) FROM product_content_units WHERE tenant_id='tenant_yaya' AND unit_id='YAYA_FARM_001' AND version='1.0.0') = 1 AND
                      (SELECT count(*) FROM world_snapshots WHERE tenant_id='tenant_yaya' AND world_id='world_watering_0001' AND actor_id='student_0001' AND revision=0) = 1 AND
                      (SELECT count(*) FROM learner_profiles WHERE tenant_id='tenant_yaya' AND learner_id='student_0001' AND actor_id='student_0001') = 1 AND
                      (SELECT count(*) FROM agent_profiles WHERE tenant_id='tenant_yaya' AND agent_profile_id='agent_profile_build_e2e_0001') = 1 AND
                      (SELECT count(*) FROM build_policies WHERE tenant_id='tenant_yaya' AND build_policy_id='build_policy_e2e_0001' AND active IS TRUE) = 1 AND
                      (SELECT count(*) FROM launch_authorities WHERE tenant_id='tenant_yaya' AND authority_id='authority_build_e2e_0001' AND active IS TRUE) = 1 AND
                      (SELECT count(*) FROM registry_heads WHERE tenant_id='tenant_yaya' AND actor_id='student_0001' AND revision=0) = 1
                """)))
                if not exact:
                    raise RuntimeError("authority baseline identifiers or revisions drifted")
                result = {"status": "BASELINE_OK", "audit_records": counts.get("audit_records", 0), "authority_rows": 7}
            else:
                result = dict((await session.execute(text("""
                    SELECT
                      (SELECT count(*) FROM game_runs WHERE tenant_id='tenant_yaya') AS game_runs,
                      (SELECT count(*) FROM game_evidence WHERE tenant_id='tenant_yaya') AS game_evidence,
                      (SELECT count(*) FROM learner_projection_jobs WHERE tenant_id='tenant_yaya') AS learner_jobs,
                      (SELECT count(*) FROM learner_projection_jobs WHERE tenant_id='tenant_yaya' AND status='SUCCEEDED') AS learner_jobs_succeeded,
                      (SELECT count(*) FROM learner_profiles WHERE tenant_id='tenant_yaya' AND learner_id='student_0001') AS learner_profiles,
                      (SELECT count(*) FROM recoverable_llm_dispatches) AS relay_dispatches,
                      (SELECT count(*) FROM recoverable_llm_dispatches WHERE state='SUCCEEDED' AND generation_count=1 AND provider='deepseek' AND model='deepseek-v4-flash') AS relay_dispatches_succeeded
                """))).mappings().one())
                if (
                    result["game_runs"] < 1 or result["game_evidence"] < 1 or
                    result["learner_jobs"] < 1 or
                    result["learner_jobs_succeeded"] != result["learner_jobs"] or
                    result["learner_profiles"] != 1 or
                    result["relay_dispatches"] < 1 or
                    result["relay_dispatches_succeeded"] != result["relay_dispatches"]
                ):
                    raise RuntimeError("phase 1 did not close Run/Evidence/Learner/Provider authority")
                result = {"status": "RESULT_OK", **result}
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    finally:
        await sessions.kw["bind"].dispose()

asyncio.run(main())
'@
    $oldMode = [Environment]::GetEnvironmentVariable('WALNUT_INT3_DEMO_PROBE_MODE', 'Process')
    $oldRoot = [Environment]::GetEnvironmentVariable('WALNUT_INT3_BACKEND_ROOT', 'Process')
    try {
        $env:WALNUT_INT3_DEMO_PROBE_MODE = $Mode
        $env:WALNUT_INT3_BACKEND_ROOT = $backendRoot
        $output = (& $backendPython -c $probe 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Read-only database $Mode probe failed."
        }
        return $output | ConvertFrom-Json
    }
    finally {
        [Environment]::SetEnvironmentVariable('WALNUT_INT3_DEMO_PROBE_MODE', $oldMode, 'Process')
        [Environment]::SetEnvironmentVariable('WALNUT_INT3_BACKEND_ROOT', $oldRoot, 'Process')
    }
}

function New-StudentAuthorization {
    param(
        [Parameter(Mandatory)][string]$HmacSecret,
        [Parameter(Mandatory)][object]$BackendState
    )

    $issuedAt = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $lifetime = $TotalDeadlineSeconds + 60
    $header = [ordered]@{ alg = 'HS256'; typ = 'JWT' } | ConvertTo-Json -Compress
    $claims = [ordered]@{
        iss = [string]$BackendState.issuer
        aud = [string]$BackendState.audience
        sub = $script:StudentId
        tenant_id = $script:TenantId
        actor_id = $script:StudentId
        actor_type = 'student'
        roles = @('game:player')
        iat = $issuedAt
        nbf = $issuedAt
        exp = $issuedAt + $lifetime
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
        return "Bearer $signingInput.$(ConvertTo-Base64Url -Bytes $signature)"
    }
    finally {
        $hmac.Dispose()
        [Array]::Clear($secretBytes, 0, $secretBytes.Length)
        [Array]::Clear($signingBytes, 0, $signingBytes.Length)
        if ($null -ne $signature) { [Array]::Clear($signature, 0, $signature.Length) }
    }
}

function Unprotect-BackendHmacSecret {
    param([Parameter(Mandatory)][object]$BackendState)

    $path = Join-Path ([string]$BackendState.run_directory) 'auth-hmac.dpapi'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'Backend runtime HMAC authority is unavailable.'
    }
    $protected = [IO.File]::ReadAllBytes($path)
    $plain = $null
    try {
        $plain = [Security.Cryptography.ProtectedData]::Unprotect(
            $protected,
            $null,
            [Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        return [Text.UTF8Encoding]::new($false, $true).GetString($plain)
    }
    finally {
        [Array]::Clear($protected, 0, $protected.Length)
        if ($null -ne $plain) { [Array]::Clear($plain, 0, $plain.Length) }
    }
}

function Assert-NoCompetingRuntime {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $RelayPort `
        -ErrorAction SilentlyContinue)
    if ($listeners.Count -ne 0) {
        throw "Relay port $RelayPort is already in use."
    }
    $patterns = @(
        'walnut_backend.llm_relay.main',
        'walnut_backend.worker_main',
        'walnut_backend.learner_worker_main'
    )
    $competitors = @(Get-CimInstance Win32_Process | Where-Object {
        $line = [string]$_.CommandLine
        -not [string]::IsNullOrWhiteSpace($line) -and
        @($patterns | Where-Object { $line.Contains($_) }).Count -ne 0
    })
    if ($competitors.Count -ne 0) {
        throw 'A relay/workflow/learner runtime already exists; refusing competing job claims.'
    }
}

function Start-OwnedProcess {
    param(
        [Parameter(Mandatory)][string]$Role,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$StdoutPath,
        [Parameter(Mandatory)][string]$StderrPath,
        [string]$FilePath = $backendPython,
        [string]$WorkingDirectory = $backendRoot
    )

    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath
    [void]$process.Handle
    $ownedProcesses.Add([pscustomobject]@{
        role = $Role
        process = $process
        process_id = [int]$process.Id
        started_at = $process.StartTime.ToUniversalTime()
    })
    return $process
}

function Assert-OwnedProcessLive {
    param([Parameter(Mandatory)][Diagnostics.Process]$Process)

    $Process.Refresh()
    if ($Process.HasExited) {
        throw "Owned process $($Process.Id) exited during startup."
    }
}

function Wait-RelayReady {
    param(
        [Parameter(Mandatory)][Diagnostics.Process]$Process,
        [Parameter(Mandatory)][string]$Authorization
    )

    $headers = @{
        Authorization = "Bearer $Authorization"
        'X-Yaya-Llm-Protocol' = $script:RelayProtocol
    }
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        Assert-OwnedProcessLive -Process $Process
        try {
            $capabilities = Invoke-RestMethod -NoProxy -Method Get `
                -Uri "http://127.0.0.1:$RelayPort/v1/llm/capabilities" `
                -Headers $headers -ConnectionTimeoutSeconds 1 -OperationTimeoutSeconds 2
            if (
                [string]$capabilities.protocol -cne $script:RelayProtocol -or
                [bool]$capabilities.atomic_put_by_dispatch_id -ne $true -or
                [bool]$capabilities.linearizable_get -ne $true -or
                [int]$capabilities.max_generation_count -ne 1
            ) {
                throw 'Relay capability document drifted.'
            }
            return
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    throw 'Private relay did not reach its exact capability contract before the deadline.'
}

function Stop-OwnedProcesses {
    $failures = [Collections.Generic.List[string]]::new()
    $reverseOwned = [object[]]$ownedProcesses.ToArray()
    [Array]::Reverse($reverseOwned)
    foreach ($owned in $reverseOwned) {
        try {
            $process = $owned.process
            $process.Refresh()
            if (-not $process.HasExited) {
                $live = Get-Process -Id ([int]$owned.process_id) -ErrorAction Stop
                if ($live.StartTime.ToUniversalTime() -ne [datetime]$owned.started_at) {
                    throw "PID $($owned.process_id) was reused; it was not stopped."
                }
                $children = @(Get-CimInstance Win32_Process -Filter (
                    "ParentProcessId=$([int]$owned.process_id)"
                ))
                foreach ($child in $children) {
                    Stop-Process -Id ([int]$child.ProcessId) -Force -ErrorAction Stop
                }
                $process.Refresh()
                if (-not $process.HasExited) {
                    Stop-Process -Id ([int]$owned.process_id) -Force -ErrorAction Stop
                }
                [void]$process.WaitForExit(10000)
            }
        }
        catch {
            $failures.Add("$($owned.role): $($_.Exception.Message)")
        }
    }
    if ($failures.Count -ne 0) {
        throw "Owned process cleanup failed: $($failures -join ' | ')"
    }
}

try {
    if ($PSVersionTable.PSVersion.Major -lt 7) {
        throw 'Run this orchestrator with PowerShell 7; it starts the compatible frontend runner separately.'
    }
    if ($ResourceDeadlineSeconds -ge $TotalDeadlineSeconds -or $InteractionDeadlineSeconds -ge $TotalDeadlineSeconds) {
        throw 'Resource and interaction deadlines must be smaller than the total deadline.'
    }
    foreach ($path in @($backendPython, $frontendRunner, $contractVerifier)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required executable/script is unavailable: $path"
        }
    }
    if ([string]::IsNullOrWhiteSpace($GodotExe) -or -not (Test-Path -LiteralPath $GodotExe -PathType Leaf)) {
        throw 'Pass the existing Godot 4.5.2 console executable with -GodotExe.'
    }
    $godotVersion = (& $GodotExe --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $godotVersion -notmatch '^4\.5\.2\.stable') {
        throw 'Godot executable is not the pinned 4.5.2 stable runtime.'
    }
    & $backendPython $contractVerifier --agent-repo $agentRoot *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'Agent contract release byte-pin verification failed.'
    }
    $sandboxOs = (& docker image inspect $script:SandboxImage --format '{{.Os}}' 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $sandboxOs -cne 'linux') {
        throw 'Pinned Sandbox image is unavailable locally; this script never pulls it.'
    }
    $backendState = Get-ExactBackendState
    Assert-NoCompetingRuntime
    $databaseUrl = Get-DatabaseUrl
    $env:WALNUT_AUTH_HMAC_SECRET = $null
    $env:WALNUT_FEISHU_PSEUDONYM_SECRET = $null
    $env:POSTGRES_PASSWORD = $null
    $env:WALNUT_LLM_API_KEY = $null
    $env:YAYA_AUTH_TOKEN = $null
    $env:PYTHONPATH = (Join-Path $backendRoot 'src') + [IO.Path]::PathSeparator + (
        Join-Path $agentRoot 'python'
    )
    $env:PYTHONUTF8 = '1'
    $env:WALNUT_DATABASE_URL = $databaseUrl
    $baseline = Invoke-DatabaseProbe -Mode baseline

    $runtimeParentFull = [IO.Path]::GetFullPath($RuntimeParent)
    $pathBudget = Assert-ShortRuntimePath -Path $runDirectory
    if (Test-Path -LiteralPath $runtimeParentFull) {
        $parentItem = Get-Item -LiteralPath $runtimeParentFull -Force
        if (-not $parentItem.PSIsContainer -or (($parentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw 'Runtime parent must be a real non-reparse directory.'
        }
    }
    else {
        New-Item -ItemType Directory -Path $runtimeParentFull | Out-Null
    }
    if (Test-Path -LiteralPath $runDirectory) {
        throw 'Random short runtime directory already exists.'
    }
    New-Item -ItemType Directory -Path $runDirectory | Out-Null
    Protect-RunDirectory -Path $runDirectory

    $providerKey = Read-ProviderKey -Path $ProviderConfigPath
    Write-TemporaryProviderKey -Path $providerKeyPath -Value $providerKey
    $providerKey = $null
    $relaySecret = New-RandomBase64Url

    $env:WALNUT_LLM_PROVIDER = $script:Provider
    $env:WALNUT_LLM_MODEL = $script:Model
    $env:WALNUT_LLM_UPSTREAM_ENDPOINT = $script:UpstreamEndpoint
    $env:WALNUT_LLM_UPSTREAM_TIMEOUT_MS = '120000'
    $env:WALNUT_LLM_UPSTREAM_API_KEY = $null
    $env:WALNUT_LLM_UPSTREAM_API_KEY_FILE = $providerKeyPath
    $env:DEEPSEEK_API_KEY = $null
    $env:OPENAI_API_KEY = $null
    $env:WALNUT_LLM_RELAY_SERVER_API_KEY = $relaySecret
    $env:WALNUT_LLM_RELAY_BIND_HOST = '127.0.0.1'
    $env:WALNUT_LLM_RELAY_BIND_PORT = [string]$RelayPort
    $env:WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS = '24'
    $relayProcess = Start-OwnedProcess -Role relay `
        -Arguments @('-m', 'walnut_backend.llm_relay.main') `
        -StdoutPath (Join-Path $runDirectory 'relay.stdout.log') `
        -StderrPath (Join-Path $runDirectory 'relay.stderr.log')
    Wait-RelayReady -Process $relayProcess -Authorization $relaySecret
    $env:WALNUT_LLM_UPSTREAM_API_KEY_FILE = $null
    Remove-Item -LiteralPath $providerKeyPath -Force

    $env:WALNUT_CONTRACT_PATH = $agentRoot
    $env:WALNUT_RUNTIME_ROOT = $runDirectory
    $env:WALNUT_TENANT_ID = $script:TenantId
    $env:WALNUT_WORKER_ID = "int3-demo-workflow-$runId"
    $env:WALNUT_LEARNER_WORKER_ID = "int3-demo-learner-$runId"
    $env:WALNUT_DOCKER_EXECUTABLE = 'docker'
    $env:WALNUT_SANDBOX_IMAGE = $script:SandboxImage
    $env:WALNUT_SANDBOX_CPU_MS = '1000'
    $env:WALNUT_SANDBOX_WALL_MS = '15000'
    $env:WALNUT_SANDBOX_MEMORY_BYTES = '536870912'
    $env:WALNUT_SANDBOX_MAX_PROCESSES = '64'
    $env:WALNUT_SANDBOX_MAX_OUTPUT_BYTES = '65536'
    $env:WALNUT_ENABLE_WORLD_PRESENTATION = 'false'
    $env:WALNUT_ENABLE_SKILL_PATCH = 'false'
    $env:WALNUT_LLM_RELAY_ENDPOINT = "http://127.0.0.1:$RelayPort"
    $env:WALNUT_LLM_RELAY_API_KEY = $relaySecret
    $env:WALNUT_LLM_RELAY_ALLOW_INSECURE_LOCALHOST = 'true'
    $env:WALNUT_LLM_RELAY_REQUIRED_RETENTION_SECONDS = '604800'
    $env:WALNUT_LLM_RELAY_MAX_RESPONSE_BYTES = '2097152'
    $env:WALNUT_LLM_RELAY_CAPABILITY_TIMEOUT_MS = '5000'
    $env:WALNUT_LLM_RESPONSE_FORMAT = 'json_object'
    $env:WALNUT_LLM_THINKING_MODE = 'disabled'
    $env:WALNUT_PROMPT_VERSION = 'int1-prompt-v1'
    $env:WALNUT_TEACHING_SPEC_VERSION = 'agent-teaching-v1'
    $env:WALNUT_WORLD_RULES_VERSION = 'farm-rules-1'
    $env:WALNUT_WORLD_CONTENT_VERSION = '1.0.0'
    $env:WALNUT_WORLD_SUCCESS_SCORE = '8'
    $env:WALNUT_WORKER_LEASE_SECONDS = '120'
    $env:WALNUT_WORKER_IDLE_POLL_SECONDS = '0.1'
    $env:WALNUT_LEARNER_WORKER_LEASE_SECONDS = '120'
    $env:WALNUT_LEARNER_WORKER_IDLE_POLL_SECONDS = '0.1'
    $env:WALNUT_LEARNER_WORKER_MAXIMUM_ATTEMPTS = '5'
    $env:WALNUT_LLM_RELAY_SERVER_API_KEY = $null
    $env:WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS = $null

    $workflowProcess = Start-OwnedProcess -Role workflow `
        -Arguments @('-m', 'walnut_backend.worker_main') `
        -StdoutPath (Join-Path $runDirectory 'workflow.stdout.log') `
        -StderrPath (Join-Path $runDirectory 'workflow.stderr.log')
    # The learner projector never calls the relay. It must not inherit that bearer.
    $env:WALNUT_LLM_RELAY_API_KEY = $null
    $learnerProcess = Start-OwnedProcess -Role learner `
        -Arguments @('-m', 'walnut_backend.learner_worker_main') `
        -StdoutPath (Join-Path $runDirectory 'learner.stdout.log') `
        -StderrPath (Join-Path $runDirectory 'learner.stderr.log')
    Start-Sleep -Seconds 2
    Assert-OwnedProcessLive -Process $workflowProcess
    Assert-OwnedProcessLive -Process $learnerProcess

    $backendHmac = Unprotect-BackendHmacSecret -BackendState $backendState
    try {
        $studentAuthorization = New-StudentAuthorization -HmacSecret $backendHmac `
            -BackendState $backendState
    }
    finally {
        $backendHmac = $null
    }
    $headers = @{
        Authorization = $studentAuthorization
        'X-Request-Id' = "req_int3_demo_$runId"
        'X-Trace-Id' = "trace_int3_demo_$runId"
        'X-Correlation-Id' = "corr_int3_demo_$runId"
        'X-Schema-Version' = '1.0.0'
    }
    try {
        $bootstrap = Invoke-RestMethod -NoProxy -Method Get `
            -Uri "http://127.0.0.1:$($script:GatewayPort)/v1/student-bootstrap" `
            -Headers $headers -ConnectionTimeoutSeconds 2 -OperationTimeoutSeconds 10
    }
    catch {
        throw 'Existing Backend rejected the in-memory student authority.'
    }
    if ([string]$bootstrap.contract_version -cne $script:ExpectedContractVersion) {
        throw 'Existing Backend did not return the pinned student bootstrap contract.'
    }
    $baselineAfterServices = Invoke-DatabaseProbe -Mode baseline

    # Only the student HTTP authority crosses into the frozen frontend process.
    # The already-started service children retain their own environment copies.
    $env:WALNUT_DATABASE_URL = $null
    $env:WALNUT_LLM_RELAY_API_KEY = $null
    $env:YAYA_API_BASE_URL = "http://127.0.0.1:$($script:GatewayPort)"
    $env:YAYA_AUTH_TOKEN = $studentAuthorization
    $env:GODOT_EXE = $GodotExe
    $runnerArguments = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $frontendRunner,
        '-GodotExe', $GodotExe,
        '-TotalDeadlineSeconds', [string]$TotalDeadlineSeconds,
        '-ResourceDeadlineSeconds', [string]$ResourceDeadlineSeconds,
        '-InteractionDeadlineSeconds', [string]$InteractionDeadlineSeconds,
        '-Phase1FingerprintPath', $phase1FingerprintPath,
        '-ResetPersistence'
    )
    $runner = Start-OwnedProcess -Role frontend-phase1 -FilePath 'powershell.exe' `
        -WorkingDirectory $frontendRoot -Arguments $runnerArguments `
        -StdoutPath (Join-Path $runDirectory 'phase1.stdout.log') `
        -StderrPath (Join-Path $runDirectory 'phase1.stderr.log')
    $env:YAYA_AUTH_TOKEN = $null
    $studentAuthorization = $null
    $runnerWaitMilliseconds = ([long]$TotalDeadlineSeconds + 20) * 1000
    if ($runnerWaitMilliseconds -gt [int]::MaxValue -or -not $runner.WaitForExit([int]$runnerWaitMilliseconds)) {
        throw 'Frontend Phase 1 exceeded its bounded external deadline.'
    }
    $runner.WaitForExit()
    if ([int]$runner.ExitCode -ne 0) {
        throw "Frontend Phase 1 failed; inspect the scoped logs in $runDirectory."
    }
    if (-not (Test-Path -LiteralPath $phase1FingerprintPath -PathType Leaf)) {
        throw 'Frontend Phase 1 returned success without its authority fingerprint.'
    }
    $env:WALNUT_DATABASE_URL = $databaseUrl
    $result = Invoke-DatabaseProbe -Mode result
    $fingerprintSha256 = (Get-FileHash -LiteralPath $phase1FingerprintPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $runSucceeded = $true
    $summary = [ordered]@{
        status = 'PASS'
        classification = 'INT3_REAL_PROVIDER_PHASE1'
        backend_reused = $true
        backend_port = $script:GatewayPort
        relay_port = $RelayPort
        provider = $script:Provider
        model = $script:Model
        database_preserved_for_feishu_sync = $true
        baseline = $baseline
        baseline_after_services = $baselineAfterServices
        result = $result
        runtime_directory = $runDirectory
        longest_known_path_characters = $pathBudget.longest_known_path_characters
        phase1_fingerprint_sha256 = $fingerprintSha256
        provider_key_echoed = $false
        student_jwt_persisted = $false
    }
    Write-Output ('INT3_REAL_DEMO_PASS ' + ($summary | ConvertTo-Json -Compress -Depth 8))
}
finally {
    $studentAuthorization = $null
    $providerKey = $null
    $relaySecret = $null
    $databaseUrl = $null
    [Environment]::SetEnvironmentVariable('YAYA_AUTH_TOKEN', $null, 'Process')
    [Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY', $null, 'Process')
    [Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY_FILE', $null, 'Process')
    $credentialCleanupError = $null
    if (Test-Path -LiteralPath $providerKeyPath -PathType Leaf) {
        try {
            Remove-Item -LiteralPath $providerKeyPath -Force -ErrorAction Stop
        }
        catch {
            $credentialCleanupError = [InvalidOperationException]::new(
                'Temporary Provider credential cleanup failed.'
            )
        }
    }
    $cleanupError = $null
    try {
        Stop-OwnedProcesses
    }
    catch {
        $cleanupError = $_
    }
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
    if ($null -ne $credentialCleanupError) {
        throw $credentialCleanupError
    }
    if ($null -ne $cleanupError) {
        throw $cleanupError
    }
    if (-not $runSucceeded -and (Test-Path -LiteralPath $runDirectory -PathType Container)) {
        Write-Output "INT3_REAL_DEMO_LOGS $runDirectory"
    }
}
