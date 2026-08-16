[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$backendRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$workspaceRoot = Split-Path -Parent $backendRoot
$frontendRoot = Join-Path $workspaceRoot 'walnut-world-frontend'
$agentRoot = Join-Path $workspaceRoot 'agent'
$python = Join-Path $backendRoot '.venv\Scripts\python.exe'
$frontendRunner = Join-Path $frontendRoot 'scripts\run-real-gateway-e2e.ps1'
$godot = Join-Path $workspaceRoot 'tools\godot-4.5.2\Godot_v4.5.2-stable_win64_console.exe'
$providerEnvPath = 'C:\Users\HP\AppData\Local\WalnutINT3\secrets\provider.env'
$activeStatePath = 'C:\Users\HP\AppData\Local\WalnutWorld\int3-aily-backend\active.json'
$runId = [Guid]::NewGuid().ToString('N').Substring(0, 8)
$runRoot = "C:\w3\$runId"
$runtimeRoot = Join-Path $runRoot 'r'
$logRoot = Join-Path $runRoot 'l'
$fingerprintPath = Join-Path $runRoot 'f.json'
$providerKeyPath = Join-Path $runRoot 'p.key'
$started = [Collections.Generic.List[Diagnostics.Process]]::new()
$stopwatch = [Diagnostics.Stopwatch]::StartNew()

function ConvertTo-Base64Url([byte[]]$Bytes) {
    [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-RandomHex([int]$Bytes = 32) {
    $buffer = [byte[]]::new($Bytes)
    try {
        [Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
        return [Convert]::ToHexString($buffer).ToLowerInvariant()
    }
    finally { [Array]::Clear($buffer, 0, $buffer.Length) }
}

function New-StudentJwt([string]$Secret, [object]$State) {
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $header = @{ alg = 'HS256'; typ = 'JWT' } | ConvertTo-Json -Compress
    $claims = [ordered]@{
        iss = [string]$State.issuer
        aud = [string]$State.audience
        sub = 'student_0001'
        tenant_id = [string]$State.tenant_id
        actor_id = 'student_0001'
        actor_type = 'student'
        roles = @('game:player')
        iat = $now
        nbf = $now
        exp = $now + 840
    } | ConvertTo-Json -Compress
    $head = ConvertTo-Base64Url ([Text.Encoding]::UTF8.GetBytes($header))
    $body = ConvertTo-Base64Url ([Text.Encoding]::UTF8.GetBytes($claims))
    $input = "$head.$body"
    $secretBytes = [Text.Encoding]::UTF8.GetBytes($Secret)
    $inputBytes = [Text.Encoding]::ASCII.GetBytes($input)
    $hmac = [Security.Cryptography.HMACSHA256]::new($secretBytes)
    try { return "$input.$(ConvertTo-Base64Url ($hmac.ComputeHash($inputBytes)))" }
    finally {
        $hmac.Dispose()
        [Array]::Clear($secretBytes, 0, $secretBytes.Length)
        [Array]::Clear($inputBytes, 0, $inputBytes.Length)
    }
}

function Protect-Directory([string]$Path) {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    foreach ($sid in @($current, $system)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance, $propagation, $allow
        ))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Protect-File([string]$Path) {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $allow = [Security.AccessControl.AccessControlType]::Allow
    foreach ($sid in @($current, $system)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, [Security.AccessControl.FileSystemRights]::FullControl, $allow
        ))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Get-DatabaseUrl {
    $inspect = @((& docker inspect walnut-int3-postgres) | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or $inspect.Count -ne 1 -or -not $inspect[0].State.Running) {
        throw 'walnut-int3-postgres is unavailable'
    }
    $values = @{}
    foreach ($entry in @($inspect[0].Config.Env)) {
        $name, $value = ([string]$entry).Split('=', 2)
        $values[$name] = $value
    }
    $user = if ($values.ContainsKey('POSTGRES_USER')) { [string]$values['POSTGRES_USER'] } else { '' }
    $database = if ($values.ContainsKey('POSTGRES_DB')) { [string]$values['POSTGRES_DB'] } else { '' }
    $password = if ($values.ContainsKey('POSTGRES_PASSWORD')) { [string]$values['POSTGRES_PASSWORD'] } else { '' }
    if ([string]::IsNullOrWhiteSpace($user) -or [string]::IsNullOrWhiteSpace($database)) {
        throw 'PostgreSQL authority metadata is incomplete'
    }
    $u = [Uri]::EscapeDataString($user)
    $d = [Uri]::EscapeDataString($database)
    if ([string]::IsNullOrEmpty($password)) {
        if (-not $values.ContainsKey('POSTGRES_HOST_AUTH_METHOD') -or [string]$values['POSTGRES_HOST_AUTH_METHOD'] -cne 'trust') { throw 'PostgreSQL password is unavailable' }
        return "postgresql+asyncpg://${u}@127.0.0.1:55432/${d}"
    }
    $p = [Uri]::EscapeDataString($password)
    return "postgresql+asyncpg://${u}:${p}@127.0.0.1:55432/${d}"
}

function Wait-Port([int]$Port, [int]$Seconds = 30) {
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($Seconds)
    do {
        if (Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $Port -ErrorAction SilentlyContinue) { return }
        Start-Sleep -Milliseconds 200
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw "port $Port did not become ready"
}

function Stop-Owned([Diagnostics.Process]$Process) {
    if ($null -eq $Process -or $Process.HasExited) { return }
    $expected = $Process.StartTime.ToUniversalTime()
    $live = Get-Process -Id $Process.Id -ErrorAction Stop
    if ($live.StartTime.ToUniversalTime() -ne $expected) { throw 'PID identity changed before cleanup' }
    Stop-Process -Id $Process.Id -Force
    [void]$Process.WaitForExit(10000)
}

try {
    foreach ($path in @($python, $frontendRunner, $godot, $providerEnvPath, $activeStatePath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "required file missing: $path" }
    }
    if (Get-NetTCPConnection -State Listen -LocalPort 18791 -ErrorAction SilentlyContinue) { throw 'port 18791 is occupied' }
    $backendListener = @(Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort 8790 -ErrorAction Stop)
    if ($backendListener.Count -ne 1) { throw 'production Backend 8790 is not a single loopback listener' }
    $pre = ((& docker exec walnut-int3-postgres psql -U walnut -d walnut_int3 -X -Atc "SELECT (SELECT count(*) FROM game_runs WHERE tenant_id='tenant_yaya')||'|'||(SELECT count(*) FROM game_evidence WHERE tenant_id='tenant_yaya')||'|'||(SELECT count(*) FROM recoverable_llm_dispatches)||'|'||(SELECT count(*) FROM commands WHERE tenant_id='tenant_yaya')||'|'||(SELECT count(*) FROM agent_sessions WHERE tenant_id='tenant_yaya');") | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $pre -ne '0|0|0|0|0') { throw "DB prestate is not empty: $pre" }

    New-Item -ItemType Directory -Path $runtimeRoot, $logRoot -Force | Out-Null
    Protect-Directory $runRoot
    $longestReceipt = Join-Path $runtimeRoot ("build-workspaces\build-" + ('a' * 64) + "\receipts\" + ('b' * 64) + '.json')
    if ($longestReceipt.Length -ge 260) { throw "receipt path remains too long: $($longestReceipt.Length)" }
    Write-Output "PHASE1_PREFLIGHT_OK run=$runId receipt_path_length=$($longestReceipt.Length) backend_pid=$($backendListener[0].OwningProcess)"

    $providerFile = Get-Item -LiteralPath $providerEnvPath -Force
    if (($providerFile.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $providerFile.Length -gt 4098) { throw 'provider.env is unsafe' }
    $providerText = [IO.File]::ReadAllText($providerFile.FullName, [Text.UTF8Encoding]::new($false, $true))
    $matches = [regex]::Matches($providerText, '(?m)^WALNUT_LLM_API_KEY=([^\r\n]+)$')
    if ($matches.Count -ne 1) { throw 'provider.env must contain exactly one WALNUT_LLM_API_KEY' }
    $providerKey = [string]$matches[0].Groups[1].Value
    if ($providerKey.Length -lt 8 -or $providerKey -match '\s') { throw 'provider key is invalid' }
    $providerText = $null

    $state = Get-Content -LiteralPath $activeStatePath -Raw -Encoding utf8 | ConvertFrom-Json
    $dpapiPath = Join-Path ([string]$state.run_directory) 'auth-hmac.dpapi'
    $protected = [IO.File]::ReadAllBytes($dpapiPath)
    $plain = [Security.Cryptography.ProtectedData]::Unprotect($protected, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    try { $hmacSecret = [Text.Encoding]::UTF8.GetString($plain) }
    finally { [Array]::Clear($protected, 0, $protected.Length); [Array]::Clear($plain, 0, $plain.Length) }
    $studentJwt = New-StudentJwt $hmacSecret $state
    $hmacSecret = $null
    $headers = @{ Authorization = "Bearer $studentJwt"; 'X-Request-Id' = "req_pre_$runId"; 'X-Trace-Id' = "trace_pre_$runId"; 'X-Correlation-Id' = "corr_pre_$runId"; 'X-Schema-Version' = '1.0.0' }
    $bootstrap = Invoke-RestMethod -Uri 'http://127.0.0.1:8790/v1/student-bootstrap' -Method Get -Headers $headers -TimeoutSec 30
    if ([string]$bootstrap.contract_version -ne '0.4.0') { throw 'student bootstrap production auth failed' }

    $databaseUrl = Get-DatabaseUrl
    $relaySecret = New-RandomHex
    $pathValue = [Environment]::GetEnvironmentVariable('PATH', 'Process')
    $pythonPathValue = (Join-Path $backendRoot 'src') + [IO.Path]::PathSeparator + (Join-Path $agentRoot 'python')
    $relayEnvironment = @{
        PATH = $pathValue; PYTHONPATH = $pythonPathValue; PYTHONUTF8 = '1'
        WALNUT_DATABASE_URL = $databaseUrl
        WALNUT_LLM_RELAY_SERVER_API_KEY = $relaySecret
        WALNUT_LLM_UPSTREAM_API_KEY = $providerKey
        WALNUT_LLM_PROVIDER = 'deepseek'; WALNUT_LLM_MODEL = 'deepseek-v4-flash'
        WALNUT_LLM_UPSTREAM_ENDPOINT = 'https://api.deepseek.com/chat/completions'
        WALNUT_LLM_RELAY_BIND_HOST = '127.0.0.1'; WALNUT_LLM_RELAY_BIND_PORT = '18791'
        WALNUT_LLM_RELAY_RESULT_RETENTION_SECONDS = '604800'; WALNUT_LLM_RELAY_MAX_RESPONSE_BYTES = '2097152'
        WALNUT_LLM_UPSTREAM_TIMEOUT_MS = '120000'; WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS = '24'
    }
    $relay = Start-Process -FilePath $python -ArgumentList @('-m','walnut_backend.llm_relay.main') -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'relay.out') -RedirectStandardError (Join-Path $logRoot 'relay.err') -Environment $relayEnvironment
    $providerKey = $null
    $started.Add($relay)
    Wait-Port 18791 30
    if ($relay.HasExited) { throw 'relay exited during startup' }

    $workerEnvironment = @{
        PATH = $pathValue; PYTHONPATH = $pythonPathValue; PYTHONUTF8 = '1'
        WALNUT_DATABASE_URL = $databaseUrl; WALNUT_TENANT_ID = 'tenant_yaya'
        WALNUT_RUNTIME_ROOT = $runtimeRoot; WALNUT_WORKER_ID = "wf-$runId"
        WALNUT_DOCKER_EXECUTABLE = 'docker'
        WALNUT_SANDBOX_IMAGE = 'gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c'
        WALNUT_SANDBOX_CPU_MS = '1000'; WALNUT_SANDBOX_WALL_MS = '15000'; WALNUT_SANDBOX_MEMORY_BYTES = '536870912'
        WALNUT_SANDBOX_MAX_PROCESSES = '64'; WALNUT_SANDBOX_MAX_OUTPUT_BYTES = '65536'
        WALNUT_LLM_RELAY_ENDPOINT = 'http://127.0.0.1:18791'; WALNUT_LLM_RELAY_API_KEY = $relaySecret
        WALNUT_LLM_RELAY_ALLOW_INSECURE_LOCALHOST = 'true'; WALNUT_LLM_RELAY_REQUIRED_RETENTION_SECONDS = '604800'
        WALNUT_LLM_RELAY_MAX_RESPONSE_BYTES = '2097152'; WALNUT_LLM_RELAY_CAPABILITY_TIMEOUT_MS = '5000'
        WALNUT_LLM_PROVIDER = 'deepseek'; WALNUT_LLM_MODEL = 'deepseek-v4-flash'; WALNUT_LLM_RESPONSE_FORMAT = 'json_object'; WALNUT_LLM_THINKING_MODE = 'disabled'
        WALNUT_PROMPT_VERSION = 'int1-prompt-v1'; WALNUT_TEACHING_SPEC_VERSION = 'agent-teaching-v1'
        WALNUT_WORLD_RULES_VERSION = 'farm-rules-1'; WALNUT_WORLD_CONTENT_VERSION = '1.0.0'; WALNUT_WORLD_SUCCESS_SCORE = '8'
        WALNUT_ENABLE_WORLD_PRESENTATION = 'false'; WALNUT_ENABLE_SKILL_PATCH = 'false'
        WALNUT_WORKER_LEASE_SECONDS = '120'; WALNUT_WORKER_IDLE_POLL_SECONDS = '0.1'
    }
    $worker = Start-Process -FilePath $python -ArgumentList @('-m','walnut_backend.worker_main') -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'worker.out') -RedirectStandardError (Join-Path $logRoot 'worker.err') -Environment $workerEnvironment
    $started.Add($worker)
    $learnerEnvironment = @{
        PATH = $pathValue; PYTHONPATH = $pythonPathValue; PYTHONUTF8 = '1'; WALNUT_DATABASE_URL = $databaseUrl; WALNUT_TENANT_ID = 'tenant_yaya'
        WALNUT_LEARNER_WORKER_ID = "lr-$runId"; WALNUT_LEARNER_WORKER_LEASE_SECONDS = '120'; WALNUT_LEARNER_WORKER_IDLE_POLL_SECONDS = '0.1'; WALNUT_LEARNER_WORKER_MAXIMUM_ATTEMPTS = '5'
    }
    $learner = Start-Process -FilePath $python -ArgumentList @('-m','walnut_backend.learner_worker_main') -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'learner.out') -RedirectStandardError (Join-Path $logRoot 'learner.err') -Environment $learnerEnvironment
    $started.Add($learner)
    Start-Sleep -Seconds 2
    if ($relay.HasExited -or $worker.HasExited -or $learner.HasExited) { throw 'one of relay/workflow/learner exited during startup' }
    Write-Output "PHASE1_PROCESSES relay=$($relay.Id) workflow=$($worker.Id) learner=$($learner.Id)"

    $godotEnvironment = @{ YAYA_API_BASE_URL = 'http://127.0.0.1:8790'; YAYA_AUTH_TOKEN = $studentJwt; GODOT_EXE = $godot }
    $godotArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$frontendRunner,'-GodotExe',$godot,'-TotalDeadlineSeconds','600','-ResourceDeadlineSeconds','180','-InteractionDeadlineSeconds','90','-Phase1FingerprintPath',$fingerprintPath,'-ResetPersistence')
    $godotProcess = Start-Process -FilePath 'powershell.exe' -ArgumentList $godotArgs -WorkingDirectory $frontendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'godot.out') -RedirectStandardError (Join-Path $logRoot 'godot.err') -Environment $godotEnvironment
    Write-Output "PHASE1_GODOT_STARTED pid=$($godotProcess.Id)"
    $godotProcess.WaitForExit()
    $godotProcess.Refresh()
    $studentJwt = $null
    $godotLines = @(@(Get-Content -LiteralPath (Join-Path $logRoot 'godot.out') -ErrorAction SilentlyContinue), @(Get-Content -LiteralPath (Join-Path $logRoot 'godot.err') -ErrorAction SilentlyContinue)) | ForEach-Object { [string]$_ }
    $passLines = @($godotLines | Where-Object { $_.StartsWith('REAL_GATEWAY_CHAIN_E2E_PASS ', [StringComparison]::Ordinal) })
    if ($godotProcess.ExitCode -ne 0 -or $passLines.Count -ne 1) {
        $godotLines | Select-Object -Last 80 | ForEach-Object { Write-Output $_ }
        throw "Godot phase1 failed exit=$($godotProcess.ExitCode) pass_lines=$($passLines.Count)"
    }
    Write-Output $passLines[0]

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(90)
    do {
        $closure = ((& docker exec walnut-int3-postgres psql -U walnut -d walnut_int3 -X -Atc "SELECT count(*)||'|'||count(*) FILTER (WHERE status='SUCCEEDED' AND lease_owner IS NULL AND completed_at IS NOT NULL) FROM learner_projection_jobs WHERE tenant_id='tenant_yaya';") | Out-String).Trim()
        if ($closure -eq '4|4') { break }
        Start-Sleep -Milliseconds 500
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    if ($closure -ne '4|4') { throw "learner projection closure timed out: $closure" }
    $summary = ((& docker exec walnut-int3-postgres psql -U walnut -d walnut_int3 -X -Atc "SELECT json_build_object('runs',(SELECT count(*) FROM game_runs WHERE tenant_id='tenant_yaya'),'failed_runs',(SELECT count(*) FROM game_runs WHERE tenant_id='tenant_yaya' AND run_json->>'status'='REJECTED'),'successful_runs',(SELECT count(*) FROM game_runs WHERE tenant_id='tenant_yaya' AND run_json->>'status'='SUCCEEDED'),'evidence',(SELECT count(*) FROM game_evidence WHERE tenant_id='tenant_yaya'),'interactions',(SELECT count(*) FROM product_agent_interactions WHERE tenant_id='tenant_yaya'),'learner_jobs',(SELECT count(*) FROM learner_projection_jobs WHERE tenant_id='tenant_yaya'),'learner_revision',(SELECT revision FROM learner_profiles WHERE tenant_id='tenant_yaya'),'commands',(SELECT count(*) FROM commands WHERE tenant_id='tenant_yaya'),'relay_dispatches',(SELECT count(*) FROM recoverable_llm_dispatches),'relay_succeeded',(SELECT count(*) FROM recoverable_llm_dispatches WHERE state='SUCCEEDED'),'max_generation',(SELECT COALESCE(max(generation_count),0) FROM recoverable_llm_dispatches));") | Out-String).Trim()
    $parsed = $summary | ConvertFrom-Json
    if ([int]$parsed.runs -ne 4 -or [int]$parsed.failed_runs -ne 3 -or [int]$parsed.successful_runs -ne 1 -or [int]$parsed.evidence -ne 11 -or [int]$parsed.interactions -ne 4 -or [int]$parsed.learner_jobs -ne 4 -or [int]$parsed.learner_revision -ne 4 -or [int]$parsed.commands -ne 9 -or [int]$parsed.relay_dispatches -lt 12 -or [int]$parsed.relay_dispatches -gt 24 -or [int]$parsed.relay_succeeded -ne [int]$parsed.relay_dispatches -or [int]$parsed.max_generation -ne 1) { throw "authority closure mismatch: $summary" }
    Write-Output "PHASE1_AUTHORITY_CLOSED $summary"
    Write-Output "PHASE1_COMPLETE run=$runId elapsed_seconds=$([math]::Round($stopwatch.Elapsed.TotalSeconds,3)) logs=$logRoot"
}
finally {
    for ($index = $started.Count - 1; $index -ge 0; $index--) {
        try { Stop-Owned $started[$index] } catch { Write-Output "TEMP_PROCESS_CLEANUP_WARNING pid=$($started[$index].Id)" }
    }
    if (Test-Path -LiteralPath $providerKeyPath -PathType Leaf) { Remove-Item -LiteralPath $providerKeyPath -Force }
    Write-Output "PHASE1_TEMP_PROCESSES_STOPPED count=$($started.Count)"
}
