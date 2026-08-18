[CmdletBinding()]
param(
    [string]$GodotExe = $env:GODOT_EXE,
    [string]$PostgresImage = 'postgres:16.9-alpine@sha256:7c688148e5e156d0e86df7ba8ae5a05a2386aaec1e2ad8e6d11bdf10504b1fb7',
    [string]$SandboxImage = 'gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c',
    [long]$MinimumFreeMemoryBytes = 1073741824,
    [long]$MinimumFreeDiskBytes = 4294967296,
    [int]$TotalDeadlineSeconds = 600,
    [switch]$EnableWorldPresentation = $true,
    [switch]$EnableSkillPatch,
    [switch]$RealProvider,
    [string]$RealProviderName = 'deepseek',
    [string]$RealProviderModel = 'deepseek-v4-flash',
    [string]$RealProviderEndpoint = 'https://api.deepseek.com/chat/completions'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($EnableSkillPatch -and -not $EnableWorldPresentation) {
    throw 'Skill Patch requires the authoritative World presentation flag.'
}

function Test-DigestPinnedImage([string]$Value) {
    return $Value -cmatch '^[a-z0-9][a-z0-9._/-]*(?::[a-z0-9][a-z0-9._-]*)?@sha256:[a-f0-9]{64}$'
}

$backendRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$workspaceParent = Split-Path -Parent $backendRoot
$frontendContainerRoot = Join-Path $workspaceParent 'walnut-world-frontend'
$nestedFrontendRoot = Join-Path $frontendContainerRoot 'walnut-world-frontend'
if (Test-Path -LiteralPath (Join-Path $nestedFrontendRoot 'project.godot') -PathType Leaf) {
    $frontendRoot = (Resolve-Path -LiteralPath $nestedFrontendRoot).Path
}
else {
    $frontendRoot = $frontendContainerRoot
}
$bundledAgentRoot = Join-Path $backendRoot 'agent'
$legacyAgentRoot = Join-Path $workspaceParent 'agent'
if (Test-Path -LiteralPath (Join-Path $bundledAgentRoot 'contracts\manifest.json') -PathType Leaf) {
    $agentRoot = (Resolve-Path -LiteralPath $bundledAgentRoot).Path
}
else {
    $agentRoot = $legacyAgentRoot
}
$backendPython = Join-Path $backendRoot '.venv\Scripts\python.exe'
$relayScript = Join-Path $PSScriptRoot 'int1_recoverable_relay.py'
$realProviderFaultProxyScript = Join-Path $PSScriptRoot 'int1_real_provider_fault_proxy.py'
$frontendRunner = Join-Path $frontendRoot 'scripts\run-real-gateway-e2e.ps1'
$runId = [Guid]::NewGuid().ToString('N')
$runRoot = Join-Path ([IO.Path]::GetTempPath()) ("walnut-int1-local-diagnostic\run-$runId")
$phase1GodotFingerprintPath = Join-Path $runRoot 'godot-phase1-authority-fingerprint.json'
$runtimeRoot = Join-Path ([IO.Path]::GetTempPath()) ("wi1-$($runId.Substring(0, 10))")
$postgresName = "walnut-int1-pg-$($runId.Substring(0, 12))"
$postgresVolumeName = "walnut-int1-pgdata-$($runId.Substring(0, 12))"
$postgresResourceOwner = 'run-int1-local-diagnostic'
$startedProcesses = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$postgresCreated = $false
$postgresStarted = $false
$postgresId = $null
$postgresVolumeAbsentBeforeCreate = $false
$postgresVolumeCreated = $false
$stopwatch = [Diagnostics.Stopwatch]::StartNew()
$classification = if ($RealProvider) { 'REAL_PROVIDER_PRIVATE_DURABLE_RELAY' } else { 'DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER' }
$int1E2eTokenLifetimeSeconds = 1800
$int1E2eTransitionBudgetSeconds = 300
$requiredInt1E2eTokenLifetimeSeconds = `
    ([long]$TotalDeadlineSeconds * 2) + $int1E2eTransitionBudgetSeconds
$expectedRealProviderGenerationLimit = if ($EnableSkillPatch) { 32 } else { 24 }
$originalUpstreamKey = [Environment]::GetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY', 'Process')
$originalUpstreamKeyFile = [Environment]::GetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY_FILE', 'Process')
$realProviderUpstreamKey = $null
$dockerBaseline = $null
$pendingPassResult = $null
$runFailure = $null
$cleanupFailure = $null
$durableFailureState = $null
$dockerBaselineAfterCleanup = $null

function Restore-UpstreamKeyEnvironment {
    [Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY', $originalUpstreamKey, 'Process')
    [Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY_FILE', $originalUpstreamKeyFile, 'Process')
}

function Clear-UpstreamKeyEnvironment {
    [Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY', $null, 'Process')
    [Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY_FILE', $null, 'Process')
}

function Get-DockerResourceAuthority(
    [ValidateSet('container', 'volume')]
    [string]$Kind,
    [string]$Name,
    [string]$DockerExecutable = 'docker.exe'
) {
    if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$') {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace($DockerExecutable)) {
        return $null
    }

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $DockerExecutable
    $startInfo.Arguments = "$Kind inspect $Name"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            return $null
        }
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if (
            $process.ExitCode -ne 0 -or
            -not [string]::IsNullOrWhiteSpace($stderr) -or
            [string]::IsNullOrWhiteSpace($stdout)
        ) {
            return $null
        }
        try {
            $decodedItems = $stdout | ConvertFrom-Json -ErrorAction Stop
            if ($decodedItems -is [Array]) {
                $items = @($decodedItems.GetEnumerator())
            }
            else {
                $items = @($decodedItems)
            }
        }
        catch {
            return $null
        }
        if ($items.Count -ne 1) {
            return $null
        }
        $resource = $items[0]
        if ($Kind -eq 'container') {
            $identity = [string]$resource.Id
            $labels = $resource.Config.Labels
            if ($identity -cnotmatch '^[a-f0-9]{64}$') {
                return $null
            }
            if ($Name -cmatch '^[a-f0-9]{64}$') {
                if ($identity -cne $Name) {
                    return $null
                }
            }
            else {
                $nameProperty = $resource.PSObject.Properties['Name']
                if (
                    $null -eq $nameProperty -or
                    ([string]$nameProperty.Value).TrimStart('/') -cne $Name
                ) {
                    return $null
                }
            }
        }
        else {
            $identity = [string]$resource.Name
            $labels = $resource.Labels
            if ($identity -cne $Name) {
                return $null
            }
        }
        if ($null -eq $labels) {
            return $null
        }
        $runProperty = $labels.PSObject.Properties['walnut.int1.run_id']
        $ownerProperty = $labels.PSObject.Properties['walnut.int1.owner']
        if ($null -eq $runProperty -or $null -eq $ownerProperty) {
            return $null
        }
        $resourceRunId = [string]$runProperty.Value
        $resourceOwner = [string]$ownerProperty.Value
        if ($resourceRunId -notmatch '^[a-f0-9]{32}$' -or [string]::IsNullOrWhiteSpace($resourceOwner)) {
            return $null
        }
        return "$identity|$resourceRunId|$resourceOwner"
    }
    catch {
        return $null
    }
    finally {
        $process.Dispose()
    }
}

function Test-DockerContainerAbsent(
    [string]$ContainerId,
    [string]$DockerExecutable = 'docker.exe'
) {
    if ($ContainerId -cnotmatch '^[a-f0-9]{64}$') {
        throw 'PostgreSQL container identity is not a bounded Docker identity.'
    }
    if ([string]::IsNullOrWhiteSpace($DockerExecutable)) {
        throw 'Docker executable path is empty.'
    }

    $probeId = [Guid]::NewGuid().ToString('N')
    $probeRoot = Join-Path ([IO.Path]::GetTempPath()) 'walnut-int1-container-probes'
    $stdoutPath = Join-Path $probeRoot "$probeId.stdout.log"
    $stderrPath = Join-Path $probeRoot "$probeId.stderr.log"
    New-Item -ItemType Directory -Path $probeRoot -Force | Out-Null

    try {
        $probe = Start-Process -FilePath $DockerExecutable `
            -ArgumentList @('container', 'inspect', '--format', '{{.Id}}', $ContainerId) `
            -WindowStyle Hidden `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        $stdout = if (Test-Path -LiteralPath $stdoutPath) {
            ([IO.File]::ReadAllText($stdoutPath)).Trim()
        }
        else {
            ''
        }
        $stderr = if (Test-Path -LiteralPath $stderrPath) {
            ([IO.File]::ReadAllText($stderrPath)).Trim()
        }
        else {
            ''
        }

        if ($probe.ExitCode -eq 0) {
            if ($stdout -cne $ContainerId -or -not [string]::IsNullOrWhiteSpace($stderr)) {
                throw 'Docker container inspect returned malformed existing-container authority.'
            }
            return $false
        }

        $expectedAbsentErrors = @(
            "Error: No such object: $ContainerId",
            "Error: No such container: $ContainerId",
            "Error response from daemon: No such container: $ContainerId"
        )
        if (
            $probe.ExitCode -eq 1 -and
            [string]::IsNullOrWhiteSpace($stdout) -and
            $expectedAbsentErrors -ccontains $stderr
        ) {
            return $true
        }

        throw "Could not prove the PostgreSQL container was absent after cleanup (Docker exit code $($probe.ExitCode))."
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-DockerNativeCapture(
    [string]$DockerExecutable,
    [string[]]$Arguments,
    [string]$Operation
) {
    if ([string]::IsNullOrWhiteSpace($DockerExecutable)) {
        throw 'Docker executable path is empty.'
    }
    if (
        $Arguments.Count -eq 0 -or
        @($Arguments | Where-Object { [string]::IsNullOrWhiteSpace($_) }).Count -ne 0 -or
        [string]::IsNullOrWhiteSpace($Operation)
    ) {
        throw 'Docker native invocation authority is malformed.'
    }

    $captureId = [Guid]::NewGuid().ToString('N')
    $captureRoot = Join-Path ([IO.Path]::GetTempPath()) 'walnut-int1-docker-native-captures'
    $stdoutPath = Join-Path $captureRoot "$captureId.stdout.log"
    $stderrPath = Join-Path $captureRoot "$captureId.stderr.log"
    New-Item -ItemType Directory -Path $captureRoot -Force | Out-Null

    try {
        $process = Start-Process -FilePath $DockerExecutable `
            -ArgumentList $Arguments `
            -WindowStyle Hidden `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        try {
            $exitCode = [int]$process.ExitCode
        }
        finally {
            $process.Dispose()
        }
        $strictUtf8 = [Text.UTF8Encoding]::new($false, $true)
        try {
            $stdout = if (Test-Path -LiteralPath $stdoutPath) {
                $strictUtf8.GetString([IO.File]::ReadAllBytes($stdoutPath))
            }
            else {
                ''
            }
            $stderr = if (Test-Path -LiteralPath $stderrPath) {
                $strictUtf8.GetString([IO.File]::ReadAllBytes($stderrPath))
            }
            else {
                ''
            }
        }
        catch [Text.DecoderFallbackException] {
            throw "Docker native $Operation output is not strict UTF-8."
        }
        return [PSCustomObject][ordered]@{
            operation = $Operation
            exit_code = $exitCode
            stdout = $stdout
            stderr = $stderr
        }
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function New-OwnedPostgresVolume(
    [string]$Name,
    [string]$RunId,
    [string]$Owner,
    [ref]$Created,
    [string]$DockerExecutable = 'docker.exe'
) {
    $Created.Value = $false
    $expectedName = if ($RunId -cmatch '^[a-f0-9]{32}$') {
        "walnut-int1-pgdata-$($RunId.Substring(0, 12))"
    }
    else {
        ''
    }
    if (
        $Name -cne $expectedName -or
        $Owner -cne 'run-int1-local-diagnostic'
    ) {
        throw 'Disposable PostgreSQL volume creation authority is malformed.'
    }

    $creation = Invoke-DockerNativeCapture `
        $DockerExecutable `
        @(
            'volume', 'create',
            '--label', "walnut.int1.run_id=$RunId",
            '--label', "walnut.int1.owner=$Owner",
            $Name
        ) `
        'create-owned-postgres-volume'
    $createdName = ([string]$creation.stdout).Trim()
    if ([int]$creation.exit_code -eq 0) {
        # An exit-zero create may already have committed the volume even when
        # CLI presentation bytes are malformed.  Capture cleanup authority
        # before validating stdout/stderr or performing the first inspect.
        $Created.Value = $true
    }
    if (
        [int]$creation.exit_code -ne 0 -or
        $createdName -cne $Name -or
        -not [string]::IsNullOrWhiteSpace([string]$creation.stderr)
    ) {
        throw 'Failed to create the exact disposable PostgreSQL data volume.'
    }

    $authority = Get-DockerResourceAuthority 'volume' $Name $DockerExecutable
    if ($authority -cne "$Name|$RunId|$Owner") {
        throw 'Created PostgreSQL data volume does not have the exact run ownership labels.'
    }
    return $Name
}

function Get-ExactOwnedPostgresContainerId(
    [string]$Name,
    [string]$RunId,
    [string]$Owner,
    [string]$DockerExecutable = 'docker.exe'
) {
    $authority = Get-DockerResourceAuthority 'container' $Name $DockerExecutable
    if ([string]::IsNullOrWhiteSpace([string]$authority)) {
        return $null
    }
    $parts = @(([string]$authority) -csplit '\|', 3)
    if (
        $parts.Count -ne 3 -or
        [string]$parts[0] -cnotmatch '^[a-f0-9]{64}$' -or
        [string]$parts[1] -cne $RunId -or
        [string]$parts[2] -cne $Owner
    ) {
        return $null
    }
    return [string]$parts[0]
}

function Start-OwnedPostgresContainer(
    [string]$Name,
    [string]$RunId,
    [string]$Owner,
    [string[]]$Arguments,
    [ref]$Created,
    [ref]$Started,
    [ref]$ContainerId,
    [string]$DockerExecutable = 'docker.exe'
) {
    $Created.Value = $false
    $Started.Value = $false
    $ContainerId.Value = $null
    $expectedName = if ($RunId -cmatch '^[a-f0-9]{32}$') {
        "walnut-int1-pg-$($RunId.Substring(0, 12))"
    }
    else {
        ''
    }
    if (
        $Name -cne $expectedName -or
        $Owner -cne 'run-int1-local-diagnostic' -or
        $Arguments.Count -eq 0 -or
        [string]$Arguments[0] -cne 'run'
    ) {
        throw 'Disposable PostgreSQL container creation authority is malformed.'
    }

    # A failed `docker run` can still leave a created, stopped container.  Mark
    # cleanup required before invoking it; cleanup will rediscover only this
    # random exact name and will never mutate without exact labels/full ID.
    $Created.Value = $true
    $launch = Invoke-DockerNativeCapture `
        $DockerExecutable `
        $Arguments `
        'create-and-start-owned-postgres-container'
    $reportedId = ([string]$launch.stdout).Trim()
    if ($reportedId -cmatch '^[a-f0-9]{64}$') {
        $ContainerId.Value = $reportedId
    }

    $ownedId = Get-ExactOwnedPostgresContainerId $Name $RunId $Owner $DockerExecutable
    if ([string]::IsNullOrWhiteSpace($ownedId)) {
        throw 'Created PostgreSQL container does not have the exact random name, full identity, and run ownership labels.'
    }
    if (
        -not [string]::IsNullOrWhiteSpace($reportedId) -and
        $reportedId -cne $ownedId
    ) {
        throw 'Created PostgreSQL container identity disagrees with the exact name authority.'
    }
    $ContainerId.Value = $ownedId
    if (
        [int]$launch.exit_code -ne 0 -or
        $reportedId -cnotmatch '^[a-f0-9]{64}$' -or
        -not [string]::IsNullOrWhiteSpace([string]$launch.stderr)
    ) {
        throw 'Failed to start fresh disposable PostgreSQL.'
    }
    $Started.Value = $true
    return $ownedId
}

function Remove-OwnedPostgresContainer(
    [bool]$Created,
    [string]$Name,
    [string]$ContainerId,
    [string]$RunId,
    [string]$Owner,
    [string]$DockerExecutable = 'docker.exe'
) {
    if (-not $Created) {
        return
    }
    $expectedName = if ($RunId -cmatch '^[a-f0-9]{32}$') {
        "walnut-int1-pg-$($RunId.Substring(0, 12))"
    }
    else {
        ''
    }
    if (
        $Name -cne $expectedName -or
        $Owner -cne 'run-int1-local-diagnostic' -or
        (
            -not [string]::IsNullOrWhiteSpace($ContainerId) -and
            $ContainerId -cnotmatch '^[a-f0-9]{64}$'
        )
    ) {
        throw 'Captured PostgreSQL container cleanup authority is malformed.'
    }
    $ownedId = Get-ExactOwnedPostgresContainerId $Name $RunId $Owner $DockerExecutable
    if (
        [string]::IsNullOrWhiteSpace($ownedId) -or
        (
            -not [string]::IsNullOrWhiteSpace($ContainerId) -and
            $ContainerId -cne $ownedId
        )
    ) {
        throw 'Refusing to remove a PostgreSQL container without exact captured ownership.'
    }
    $removal = Invoke-DockerNativeCapture `
        $DockerExecutable `
        @('rm', '--force', $ownedId) `
        'remove-owned-postgres-container'
    $removalText = ([string]$removal.stdout).Trim()
    $removeExitCode = [int]$removal.exit_code
    if (
        $removeExitCode -ne 0 -or
        $removalText -cne $ownedId -or
        -not [string]::IsNullOrWhiteSpace([string]$removal.stderr)
    ) {
        throw "Failed to remove the exact owned PostgreSQL container (Docker exit $removeExitCode)."
    }
    if (-not (Test-DockerContainerAbsent $ownedId $DockerExecutable)) {
        throw 'Owned PostgreSQL container still exists after Docker reported successful removal.'
    }
}

function Remove-OwnedPostgresVolume(
    [bool]$Created,
    [bool]$AbsentBeforeCreate,
    [string]$Name,
    [string]$RunId,
    [string]$Owner,
    [string]$DockerExecutable = 'docker.exe'
) {
    if (-not $Created) {
        return
    }
    $expectedName = if ($RunId -cmatch '^[a-f0-9]{32}$') {
        "walnut-int1-pgdata-$($RunId.Substring(0, 12))"
    }
    else {
        ''
    }
    if (
        -not $AbsentBeforeCreate -or
        $Name -cne $expectedName -or
        $Owner -cne 'run-int1-local-diagnostic'
    ) {
        throw 'Captured PostgreSQL volume cleanup authority is malformed.'
    }
    $authority = Get-DockerResourceAuthority 'volume' $Name $DockerExecutable
    if ($null -eq $authority -or $authority -cne "$Name|$RunId|$Owner") {
        throw 'Refusing to remove a PostgreSQL volume without exact captured ownership.'
    }
    $removal = Invoke-DockerNativeCapture `
        $DockerExecutable `
        @('volume', 'rm', '--force', $Name) `
        'remove-owned-postgres-volume'
    $removalText = ([string]$removal.stdout).Trim()
    $removeExitCode = [int]$removal.exit_code
    if (
        $removeExitCode -ne 0 -or
        $removalText -cne $Name -or
        -not [string]::IsNullOrWhiteSpace([string]$removal.stderr)
    ) {
        throw "Failed to remove the exact owned PostgreSQL volume (Docker exit $removeExitCode)."
    }
    if (-not (Test-DockerVolumeAbsent $Name $DockerExecutable)) {
        throw 'Owned PostgreSQL volume still exists after Docker reported successful removal.'
    }
}

function Test-DockerVolumeAbsent(
    [string]$Name,
    [string]$DockerExecutable = 'docker.exe'
) {
    if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$') {
        throw 'PostgreSQL data volume name is not a bounded Docker volume name.'
    }
    if ([string]::IsNullOrWhiteSpace($DockerExecutable)) {
        throw 'Docker executable path is empty.'
    }

    $probeId = [Guid]::NewGuid().ToString('N')
    $probeRoot = Join-Path ([IO.Path]::GetTempPath()) 'walnut-int1-volume-probes'
    $stdoutPath = Join-Path $probeRoot "$probeId.stdout.log"
    $stderrPath = Join-Path $probeRoot "$probeId.stderr.log"
    New-Item -ItemType Directory -Path $probeRoot -Force | Out-Null

    try {
        $probe = Start-Process -FilePath $DockerExecutable `
            -ArgumentList @('volume', 'inspect', '--format', '{{.Name}}', $Name) `
            -WindowStyle Hidden `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        $stdout = if (Test-Path -LiteralPath $stdoutPath) {
            ([IO.File]::ReadAllText($stdoutPath)).Trim()
        }
        else {
            ''
        }
        $stderr = if (Test-Path -LiteralPath $stderrPath) {
            ([IO.File]::ReadAllText($stderrPath)).Trim()
        }
        else {
            ''
        }

        if ($probe.ExitCode -eq 0) {
            if ($stdout -cne $Name -or -not [string]::IsNullOrWhiteSpace($stderr)) {
                throw 'Docker volume inspect returned malformed existing-volume authority.'
            }
            return $false
        }

        $expectedAbsentError = "Error response from daemon: get ${Name}: no such volume"
        if (
            $probe.ExitCode -eq 1 -and
            [string]::IsNullOrWhiteSpace($stdout) -and
            $stderr -ceq $expectedAbsentError
        ) {
            return $true
        }

        throw "Could not prove the PostgreSQL data volume was absent before creation (Docker exit code $($probe.ExitCode))."
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Assert-ProviderSecretText([string]$Value) {
    if ($null -eq $Value -or $Value.Length -lt 8 -or $Value.Length -gt 4096) {
        throw 'Provider key must be bounded non-whitespace text.'
    }
    foreach ($character in $Value.ToCharArray()) {
        if ([char]::IsControl($character) -or [char]::IsWhiteSpace($character)) {
            throw 'Provider key must be bounded non-whitespace text.'
        }
    }
}

function Read-WindowsAclControlledProviderKey([string]$FileName) {
    if ([string]::IsNullOrWhiteSpace($FileName)) {
        throw 'Provider key file path is empty.'
    }
    $item = Get-Item -LiteralPath $FileName -Force -ErrorAction Stop
    if (
        -not ($item -is [System.IO.FileInfo]) -or
        (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
    ) {
        throw 'Provider key file must be a regular non-reparse file.'
    }
    if ([long]$item.Length -gt 4098) {
        throw 'Provider key file is too large.'
    }
    $acl = Get-Acl -LiteralPath $item.FullName -ErrorAction Stop
    $raw = [System.Security.AccessControl.RawSecurityDescriptor]::new(
        $acl.GetSecurityDescriptorBinaryForm(),
        0
    )
    $daclPresent = (
        $raw.ControlFlags -band
        [System.Security.AccessControl.ControlFlags]::DiscretionaryAclPresent
    ) -ne 0
    if (-not $daclPresent -or $null -eq $raw.DiscretionaryAcl) {
        throw 'Provider key file DACL is absent or null.'
    }
    $rules = $acl.GetAccessRules(
        $true,
        $true,
        [System.Security.Principal.SecurityIdentifier]
    )
    $broadSids = @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545')
    $readData = [int64][System.Security.AccessControl.FileSystemRights]::ReadData
    foreach ($rule in $rules) {
        if (
            $rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow -and
            $broadSids -contains [string]$rule.IdentityReference.Value -and
            (([int64]$rule.FileSystemRights -band $readData) -ne 0)
        ) {
            throw 'Provider key file ACL grants read data to a broad Windows identity.'
        }
    }
    $bytes = [System.IO.File]::ReadAllBytes($item.FullName)
    $after = Get-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
    if (
        -not ($after -is [System.IO.FileInfo]) -or
        (($after.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) -or
        [long]$after.Length -ne [long]$item.Length -or
        $after.LastWriteTimeUtc.Ticks -ne $item.LastWriteTimeUtc.Ticks -or
        $bytes.Length -gt 4098
    ) {
        throw 'Provider key file changed during validation.'
    }
    $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
    $value = $strictUtf8.GetString($bytes)
    if ($value.EndsWith("`r`n", [StringComparison]::Ordinal)) {
        $value = $value.Substring(0, $value.Length - 2)
    }
    elseif (
        $value.EndsWith("`n", [StringComparison]::Ordinal) -or
        $value.EndsWith("`r", [StringComparison]::Ordinal)
    ) {
        $value = $value.Substring(0, $value.Length - 1)
    }
    Assert-ProviderSecretText $value
    return $value
}

function Start-PrivateRealProviderRelay([string]$StandardOutputPath, [string]$StandardErrorPath) {
    Assert-ProviderSecretText $realProviderUpstreamKey
    [Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY_FILE', $null, 'Process')
    [Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY', $realProviderUpstreamKey, 'Process')
    try {
        return Start-Process -FilePath $backendPython `
            -ArgumentList @('-m', 'walnut_backend.llm_relay.main') `
            -WorkingDirectory $backendRoot `
            -WindowStyle Hidden `
            -PassThru `
            -RedirectStandardOutput $StandardOutputPath `
            -RedirectStandardError $StandardErrorPath
    }
    finally {
        Clear-UpstreamKeyEnvironment
    }
}

function Get-FreeTcpPort {
    param([int[]]$ExcludedPorts = @())

    # Windows assigns outbound source ports from its dynamic range.  Binding to
    # port 0 and releasing it therefore returns a candidate the OS may
    # immediately reuse.  Select from a fixed non-dynamic range instead; the
    # exclusive loopback bind below also rejects reserved/excluded ports.
    $minimumPort = 20000
    $maximumPort = 45000
    $maximumCandidateCount = 512
    $rangeSize = [uint32]($maximumPort - $minimumPort + 1)
    $excluded = [Collections.Generic.HashSet[int]]::new()
    foreach ($port in $ExcludedPorts) {
        [void]$excluded.Add($port)
    }
    $tested = [Collections.Generic.HashSet[int]]::new()
    $randomBytes = [byte[]]::new(4)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        for ($attempt = 0; $attempt -lt $maximumCandidateCount; $attempt++) {
            $generator.GetBytes($randomBytes)
            $randomValue = [BitConverter]::ToUInt32($randomBytes, 0)
            $candidate = $minimumPort + [int]($randomValue % $rangeSize)
            if ($excluded.Contains($candidate) -or -not $tested.Add($candidate)) {
                continue
            }

            $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $candidate)
            try {
                $listener.ExclusiveAddressUse = $true
                $listener.Start()
                return $candidate
            }
            catch [Net.Sockets.SocketException] {
            }
            finally {
                $listener.Stop()
            }
        }
    }
    finally {
        $generator.Dispose()
    }
    throw "No available loopback TCP port was found in $minimumPort..$maximumPort after $maximumCandidateCount candidates."
}

function Test-LocalTcpPortAvailable([int]$Port) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        $listener.Stop()
    }
}

function Wait-LocalPort([int]$Port, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $client = [Net.Sockets.TcpClient]::new()
        try {
            $task = $client.ConnectAsync('127.0.0.1', $Port)
            if ($task.Wait(250) -and $client.Connected) {
                return
            }
        }
        catch {
        }
        finally {
            $client.Dispose()
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Timed out waiting for localhost port $Port."
}

function Get-ListeningProcessIds([int]$Port) {
    try {
        return @(
            Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
                ForEach-Object { [int]$_.OwningProcess } |
                Sort-Object -Unique
        )
    }
    catch [Microsoft.PowerShell.Cmdletization.Cim.CimJobException] {
        return @()
    }
}

function Get-BackendGatewayProcessIds {
    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                -not [string]::IsNullOrWhiteSpace([string]$_.CommandLine) -and
                [string]$_.CommandLine -like '*walnut_backend.main:app*'
            } |
            ForEach-Object { [int]$_.ProcessId } |
            Sort-Object -Unique
    )
}

function Test-ProcessDescendsFrom([int]$ProcessId, [int]$ExpectedAncestorId) {
    $current = $ProcessId
    $visited = [Collections.Generic.HashSet[int]]::new()
    for ($depth = 0; $depth -lt 16; $depth++) {
        if ($current -eq $ExpectedAncestorId) {
            return $true
        }
        if (-not $visited.Add($current)) {
            return $false
        }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $current" -ErrorAction SilentlyContinue
        if ($null -eq $process -or [int]$process.ParentProcessId -le 0) {
            return $false
        }
        $current = [int]$process.ParentProcessId
    }
    return $false
}

function Assert-SingleGatewayListener([int]$Port, [int]$ExpectedLauncherProcessId, [string]$Phase) {
    $owners = @(Get-ListeningProcessIds $Port)
    if ($owners.Count -ne 1) {
        throw "$Phase did not have exactly one Gateway listener on port $Port (owners=$($owners -join ','))."
    }
    $listenerProcessId = [int]$owners[0]
    $gatewayProcesses = @(Get-BackendGatewayProcessIds)
    if ($gatewayProcesses.Count -lt 1 -or $listenerProcessId -notin $gatewayProcesses) {
        throw "$Phase did not have one serving Backend Gateway process (processes=$($gatewayProcesses -join ','))."
    }
    # uv-managed virtual environments on Windows may keep a launcher process
    # and execute the interpreter as its child.  The socket must belong to the
    # sole Gateway process and that process must descend from the exact launcher
    # returned by Start-Process; requiring PID equality rejects this legitimate
    # topology and confuses the launcher with the serving process.
    if (-not (Test-ProcessDescendsFrom $listenerProcessId $ExpectedLauncherProcessId)) {
        throw "$Phase Gateway listener PID $listenerProcessId did not descend from launcher PID $ExpectedLauncherProcessId."
    }
    $unrelatedGatewayProcesses = @(
        $gatewayProcesses |
            Where-Object { -not (Test-ProcessDescendsFrom ([int]$_) $ExpectedLauncherProcessId) }
    )
    if ($unrelatedGatewayProcesses.Count -ne 0) {
        throw "$Phase found unrelated Backend Gateway processes (processes=$($unrelatedGatewayProcesses -join ','))."
    }
    return [ordered]@{
        phase = $Phase
        port = $Port
        listener_count = $owners.Count
        launcher_process_id = $ExpectedLauncherProcessId
        owning_process_id = $listenerProcessId
        backend_gateway_process_count = $gatewayProcesses.Count
    }
}

function Wait-LocalPortClosed([int]$Port, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (@(Get-ListeningProcessIds $Port).Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Timed out waiting for localhost port $Port to have no listener."
}

function Stop-TestProcess([System.Diagnostics.Process]$Process, [string]$Name) {
    if ($Process.HasExited) {
        throw "$Name exited before the explicit restart boundary."
    }
    Stop-Process -Id $Process.Id -Force
    if (-not $Process.WaitForExit(5000)) {
        throw "$Name PID $($Process.Id) did not terminate at the restart boundary."
    }
}

function Wait-PostgresHealthy([string]$ContainerName, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $health = (& docker inspect --format '{{.State.Health.Status}}' $ContainerName 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $health -eq 'healthy') {
            return
        }
        if ($health -eq 'unhealthy') {
            throw 'Disposable PostgreSQL became unhealthy.'
        }
        Start-Sleep -Milliseconds 500
    }
    throw 'Timed out waiting for disposable PostgreSQL.'
}

function Get-PostgresClientProcessIds([int]$Port) {
    try {
        return @(
            Get-NetTCPConnection -State Established -RemotePort $Port -ErrorAction Stop |
                Where-Object {
                    [string]$_.RemoteAddress -in @(
                        '127.0.0.1',
                        '::1',
                        '::ffff:127.0.0.1'
                    )
                } |
                ForEach-Object { [int]$_.OwningProcess } |
                Sort-Object -Unique
        )
    }
    catch [Microsoft.PowerShell.Cmdletization.Cim.CimJobException] {
        return @()
    }
}

function Wait-ServicePostgresConnections(
    [int]$Port,
    [System.Collections.IDictionary]$ServiceLauncherProcessIds,
    [int]$TimeoutSeconds
) {
    $observed = [ordered]@{}
    foreach ($entry in $ServiceLauncherProcessIds.GetEnumerator()) {
        $observed[[string]$entry.Key] = $null
    }
    $lastClientProcessIds = @()
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $lastClientProcessIds = @(Get-PostgresClientProcessIds $Port)
        foreach ($entry in $ServiceLauncherProcessIds.GetEnumerator()) {
            $serviceName = [string]$entry.Key
            if ($null -ne $observed[$serviceName]) {
                continue
            }
            $launcherProcessId = [int]$entry.Value
            $matchingProcessIds = @(
                $lastClientProcessIds |
                    Where-Object { Test-ProcessDescendsFrom ([int]$_) $launcherProcessId }
            )
            if ($matchingProcessIds.Count -gt 0) {
                $observed[$serviceName] = [int]$matchingProcessIds[0]
            }
        }
        $pending = @($observed.GetEnumerator() | Where-Object { $null -eq $_.Value })
        if ($pending.Count -eq 0) {
            return [ordered]@{
                postgres_port = $Port
                service_client_process_ids = $observed
                final_established_client_process_ids = $lastClientProcessIds
            }
        }
        Start-Sleep -Milliseconds 100
    }
    $missing = @(
        $observed.GetEnumerator() |
            Where-Object { $null -eq $_.Value } |
            ForEach-Object { [string]$_.Key }
    )
    throw "Timed out waiting for PostgreSQL reconnections from: $($missing -join ',')."
}

function New-RandomHex([int]$ByteCount) {
    $bytes = [byte[]]::new($ByteCount)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
        return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $generator.Dispose()
    }
}

function Get-Sha256([string]$Text) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))
        return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $hasher.Dispose()
    }
}

function Get-DockerProjectionProperty(
    [object]$Value,
    [string]$Name,
    [object]$DefaultValue = $null
) {
    if ($null -eq $Value) {
        return $DefaultValue
    }
    if ($Value -is [Collections.IDictionary]) {
        if ($Value.Contains($Name)) {
            return $Value[$Name]
        }
        return $DefaultValue
    }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $DefaultValue
    }
    return $property.Value
}

function ConvertTo-DockerCanonicalStringMap([object]$Value) {
    $entries = [ordered]@{}
    if ($null -eq $Value) {
        return $entries
    }
    if ($Value -is [Collections.IDictionary]) {
        $names = @($Value.Keys | ForEach-Object { [string]$_ } | Sort-Object)
        foreach ($name in $names) {
            $item = $Value[$name]
            $entries[$name] = if ($null -eq $item) { $null } else { [string]$item }
        }
        return $entries
    }
    foreach ($property in @($Value.PSObject.Properties | Sort-Object Name)) {
        $item = $property.Value
        $entries[[string]$property.Name] = if ($null -eq $item) { $null } else { [string]$item }
    }
    return $entries
}

function Get-DockerRunningIds([string]$DockerExecutable) {
    $capture = Invoke-DockerNativeCapture `
        $DockerExecutable `
        @('ps', '--quiet', '--no-trunc') `
        'capture-running-container-ids'
    $text = ([string]$capture.stdout).Trim()
    $exitCode = [int]$capture.exit_code
    if ($exitCode -ne 0 -or -not [string]::IsNullOrWhiteSpace([string]$capture.stderr)) {
        throw "Could not enumerate the complete running Docker container baseline (exit $exitCode)."
    }
    if ([string]::IsNullOrWhiteSpace($text)) {
        return @()
    }
    $ids = @(
        $text -split '\r?\n' |
            ForEach-Object { $_.Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if (
        @($ids | Where-Object { $_ -cnotmatch '^[a-f0-9]{64}$' }).Count -ne 0 -or
        @($ids | Sort-Object -Unique).Count -ne $ids.Count
    ) {
        throw 'Docker returned a malformed, truncated, or duplicate running container identity.'
    }
    return @($ids | Sort-Object)
}

function Get-DockerRunningBaseline([string]$DockerExecutable = 'docker.exe') {
    if ([string]::IsNullOrWhiteSpace($DockerExecutable)) {
        throw 'Docker executable path is empty.'
    }
    $firstIds = @(Get-DockerRunningIds $DockerExecutable)
    $resources = @()
    if ($firstIds.Count -ne 0) {
        $inspectArguments = @('container', 'inspect') + @($firstIds)
        $inspectCapture = Invoke-DockerNativeCapture `
            $DockerExecutable `
            $inspectArguments `
            'capture-running-container-projection'
        $inspectText = ([string]$inspectCapture.stdout).Trim()
        $inspectExitCode = [int]$inspectCapture.exit_code
        if (
            $inspectExitCode -ne 0 -or
            -not [string]::IsNullOrWhiteSpace([string]$inspectCapture.stderr) -or
            [string]::IsNullOrWhiteSpace($inspectText)
        ) {
            throw "Could not inspect the complete running Docker container baseline (exit $inspectExitCode)."
        }
        try {
            $decodedResources = $inspectText | ConvertFrom-Json -ErrorAction Stop
            if ($decodedResources -is [Array]) {
                $resources = @($decodedResources.GetEnumerator())
            }
            else {
                $resources = @($decodedResources)
            }
        }
        catch {
            throw 'Docker returned malformed running container inspection JSON.'
        }
        $inspectedIds = @(
            $resources |
                ForEach-Object { [string](Get-DockerProjectionProperty $_ 'Id' '') } |
                Sort-Object
        )
        if (
            $resources.Count -ne $firstIds.Count -or
            @($inspectedIds | Where-Object { $_ -cnotmatch '^[a-f0-9]{64}$' }).Count -ne 0 -or
            (($inspectedIds -join "`n") -cne ($firstIds -join "`n"))
        ) {
            $invalidInspectedIdCount = @(
                $inspectedIds | Where-Object { $_ -cnotmatch '^[a-f0-9]{64}$' }
            ).Count
            $identitySetEqual = ($inspectedIds -join "`n") -ceq ($firstIds -join "`n")
            throw "Docker inspection did not close the exact full running container identity set (listed=$($firstIds.Count), inspected=$($resources.Count), inspected_ids=$($inspectedIds.Count), invalid=$invalidInspectedIdCount, equal=$identitySetEqual, listed_value=$($firstIds -join ','), inspected_value=$($inspectedIds -join ','))."
        }
    }

    $projection = @(
        $resources |
            Sort-Object { [string](Get-DockerProjectionProperty $_ 'Id' '') } |
            ForEach-Object {
                $resource = $_
                $config = Get-DockerProjectionProperty $resource 'Config' $null
                $state = Get-DockerProjectionProperty $resource 'State' $null
                $networkSettings = Get-DockerProjectionProperty $resource 'NetworkSettings' $null

                $mountProjection = @(
                    @(Get-DockerProjectionProperty $resource 'Mounts' @()) |
                        Where-Object { $null -ne $_ } |
                        ForEach-Object {
                            [ordered]@{
                                type = [string](Get-DockerProjectionProperty $_ 'Type' '')
                                name = [string](Get-DockerProjectionProperty $_ 'Name' '')
                                source = [string](Get-DockerProjectionProperty $_ 'Source' '')
                                destination = [string](Get-DockerProjectionProperty $_ 'Destination' '')
                                driver = [string](Get-DockerProjectionProperty $_ 'Driver' '')
                                mode = [string](Get-DockerProjectionProperty $_ 'Mode' '')
                                read_write = [bool](Get-DockerProjectionProperty $_ 'RW' $false)
                                propagation = [string](Get-DockerProjectionProperty $_ 'Propagation' '')
                                consistency = [string](Get-DockerProjectionProperty $_ 'Consistency' '')
                            }
                        } |
                        Sort-Object {
                            ConvertTo-Json -InputObject $_ -Compress -Depth 6
                        }
                )

                $publishedPortProjection = @()
                $ports = Get-DockerProjectionProperty $networkSettings 'Ports' $null
                if ($null -ne $ports) {
                    foreach ($portProperty in @($ports.PSObject.Properties | Sort-Object Name)) {
                        foreach ($binding in @($portProperty.Value)) {
                            if ($null -eq $binding) {
                                continue
                            }
                            $hostPortText = [string](Get-DockerProjectionProperty $binding 'HostPort' '')
                            if ($hostPortText -cnotmatch '^[0-9]{1,5}$') {
                                throw 'Docker returned a malformed published host port.'
                            }
                            $hostPort = [int]$hostPortText
                            if ($hostPort -lt 1 -or $hostPort -gt 65535) {
                                throw 'Docker returned an out-of-range published host port.'
                            }
                            $publishedPortProjection += [ordered]@{
                                container_port = [string]$portProperty.Name
                                host_ip = [string](Get-DockerProjectionProperty $binding 'HostIp' '')
                                host_port = $hostPort
                            }
                        }
                    }
                }
                $publishedPortProjection = @(
                    $publishedPortProjection |
                        Sort-Object container_port, host_ip, host_port
                )

                $networkProjection = @()
                $networks = Get-DockerProjectionProperty $networkSettings 'Networks' $null
                if ($null -ne $networks) {
                    foreach ($networkProperty in @($networks.PSObject.Properties | Sort-Object Name)) {
                        $endpoint = $networkProperty.Value
                        $ipamConfig = Get-DockerProjectionProperty $endpoint 'IPAMConfig' $null
                        $ipamProjection = if ($null -eq $ipamConfig) {
                            $null
                        }
                        else {
                            [ordered]@{
                                ipv4_address = [string](Get-DockerProjectionProperty $ipamConfig 'IPv4Address' '')
                                ipv6_address = [string](Get-DockerProjectionProperty $ipamConfig 'IPv6Address' '')
                                link_local_ips = @(
                                    @(Get-DockerProjectionProperty $ipamConfig 'LinkLocalIPs' @()) |
                                        Where-Object { $null -ne $_ } |
                                        ForEach-Object { [string]$_ } |
                                        Sort-Object
                                )
                            }
                        }
                        $networkProjection += [ordered]@{
                            network_name = [string]$networkProperty.Name
                            ipam_config = $ipamProjection
                            network_id = [string](Get-DockerProjectionProperty $endpoint 'NetworkID' '')
                            endpoint_id = [string](Get-DockerProjectionProperty $endpoint 'EndpointID' '')
                            gateway = [string](Get-DockerProjectionProperty $endpoint 'Gateway' '')
                            ip_address = [string](Get-DockerProjectionProperty $endpoint 'IPAddress' '')
                            ip_prefix_length = [int](Get-DockerProjectionProperty $endpoint 'IPPrefixLen' 0)
                            ipv6_gateway = [string](Get-DockerProjectionProperty $endpoint 'IPv6Gateway' '')
                            global_ipv6_address = [string](Get-DockerProjectionProperty $endpoint 'GlobalIPv6Address' '')
                            global_ipv6_prefix_length = [int](Get-DockerProjectionProperty $endpoint 'GlobalIPv6PrefixLen' 0)
                            mac_address = [string](Get-DockerProjectionProperty $endpoint 'MacAddress' '')
                            gateway_priority = [int](Get-DockerProjectionProperty $endpoint 'GwPriority' 0)
                            aliases = @(
                                @(Get-DockerProjectionProperty $endpoint 'Aliases' @()) |
                                    Where-Object { $null -ne $_ } |
                                    ForEach-Object { [string]$_ } |
                                    Sort-Object
                            )
                            dns_names = @(
                                @(Get-DockerProjectionProperty $endpoint 'DNSNames' @()) |
                                    Where-Object { $null -ne $_ } |
                                    ForEach-Object { [string]$_ } |
                                    Sort-Object
                            )
                            driver_options = ConvertTo-DockerCanonicalStringMap (
                                Get-DockerProjectionProperty $endpoint 'DriverOpts' $null
                            )
                            links = @(
                                @(Get-DockerProjectionProperty $endpoint 'Links' @()) |
                                    Where-Object { $null -ne $_ } |
                                    ForEach-Object { [string]$_ } |
                                    Sort-Object
                            )
                        }
                    }
                }

                $running = [bool](Get-DockerProjectionProperty $state 'Running' $false)
                if (-not $running) {
                    throw 'A docker ps identity was not running in its captured inspection state.'
                }
                $startedAt = [string](Get-DockerProjectionProperty $state 'StartedAt' '')
                $health = Get-DockerProjectionProperty $state 'Health' $null
                $healthProjection = if ($null -eq $health) {
                    $null
                }
                else {
                    [ordered]@{
                        status = [string](Get-DockerProjectionProperty $health 'Status' '')
                        failing_streak = [int](Get-DockerProjectionProperty $health 'FailingStreak' 0)
                    }
                }
                [ordered]@{
                    id = [string](Get-DockerProjectionProperty $resource 'Id' '')
                    name = [string](Get-DockerProjectionProperty $resource 'Name' '')
                    normalized_name = ([string](Get-DockerProjectionProperty $resource 'Name' '')).TrimStart('/')
                    image_id = [string](Get-DockerProjectionProperty $resource 'Image' '')
                    config_image = [string](Get-DockerProjectionProperty $config 'Image' '')
                    labels = ConvertTo-DockerCanonicalStringMap (
                        Get-DockerProjectionProperty $config 'Labels' $null
                    )
                    path = [string](Get-DockerProjectionProperty $resource 'Path' '')
                    args = @(
                        @(Get-DockerProjectionProperty $resource 'Args' @()) |
                            Where-Object { $null -ne $_ } |
                            ForEach-Object { [string]$_ }
                    )
                    mounts = $mountProjection
                    published_ports = $publishedPortProjection
                    network_endpoints = @($networkProjection)
                    state = [ordered]@{
                        status = [string](Get-DockerProjectionProperty $state 'Status' '')
                        running = $running
                        paused = [bool](Get-DockerProjectionProperty $state 'Paused' $false)
                        restarting = [bool](Get-DockerProjectionProperty $state 'Restarting' $false)
                        oom_killed = [bool](Get-DockerProjectionProperty $state 'OOMKilled' $false)
                        dead = [bool](Get-DockerProjectionProperty $state 'Dead' $false)
                        pid = [int64](Get-DockerProjectionProperty $state 'Pid' 0)
                        exit_code = [int](Get-DockerProjectionProperty $state 'ExitCode' 0)
                        error = [string](Get-DockerProjectionProperty $state 'Error' '')
                        started_at = $startedAt
                        finished_at = [string](Get-DockerProjectionProperty $state 'FinishedAt' '')
                        health = $healthProjection
                    }
                    restart_count = [int](Get-DockerProjectionProperty $resource 'RestartCount' 0)
                    started_at = $startedAt
                }
            }
    )

    $secondIds = @(Get-DockerRunningIds $DockerExecutable)
    if (($secondIds -join "`n") -cne ($firstIds -join "`n")) {
        throw 'Running Docker container identities changed while the baseline was captured.'
    }
    $canonicalJson = ConvertTo-Json -InputObject @($projection) -Compress -Depth 30
    $utf8 = [Text.UTF8Encoding]::new($false, $true)
    $canonicalBytes = $utf8.GetBytes($canonicalJson)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $sha256 = ([BitConverter]::ToString($hasher.ComputeHash($canonicalBytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $hasher.Dispose()
    }
    return [PSCustomObject][ordered]@{
        ids = @($firstIds)
        count = $firstIds.Count
        projection = @($projection)
        canonical_json = $canonicalJson
        canonical_utf8_base64 = [Convert]::ToBase64String($canonicalBytes)
        sha256 = $sha256
        all_running = $true
    }
}

function Assert-NoDiagnosticDockerConflict(
    [object]$Baseline,
    [string]$OwnedContainerName,
    [string]$OwnedLabelValue,
    [int[]]$SelectedPorts
) {
    $reasons = [Collections.Generic.List[string]]::new()
    $selected = [Collections.Generic.HashSet[int]]::new()
    foreach ($port in @($SelectedPorts)) {
        if ($port -lt 1 -or $port -gt 65535) {
            throw 'Selected diagnostic port is out of range.'
        }
        [void]$selected.Add([int]$port)
    }
    foreach ($resource in @($Baseline.projection)) {
        $identity = [string]$resource.id
        if ([string]$resource.normalized_name -ceq $OwnedContainerName) {
            $reasons.Add("name:$identity")
        }
        $ownerLabel = Get-DockerProjectionProperty $resource.labels 'walnut.int1.owner' $null
        if ($null -ne $ownerLabel -and [string]$ownerLabel -ceq $OwnedLabelValue) {
            $reasons.Add("owner:$identity")
        }
        foreach ($binding in @($resource.published_ports)) {
            if ($selected.Contains([int]$binding.host_port)) {
                $reasons.Add("port:$identity`:$([int]$binding.host_port)")
            }
        }
    }
    if ($reasons.Count -ne 0) {
        throw "Running Docker baseline conflicts with this diagnostic: $($reasons -join ',')."
    }
}

function Assert-DockerBaselineRestored(
    [object]$Baseline,
    [string]$DockerExecutable = 'docker.exe'
) {
    $current = Get-DockerRunningBaseline $DockerExecutable
    if (
        $Baseline.all_running -ne $true -or
        $current.all_running -ne $true -or
        [int]$current.count -ne [int]$Baseline.count -or
        (($current.ids -join "`n") -cne ($Baseline.ids -join "`n")) -or
        [string]$current.canonical_json -cne [string]$Baseline.canonical_json -or
        [string]$current.canonical_utf8_base64 -cne [string]$Baseline.canonical_utf8_base64 -or
        [string]$current.sha256 -cne [string]$Baseline.sha256
    ) {
        throw 'Running Docker baseline changed during the diagnostic or cleanup.'
    }
    return $current
}

function Get-DirectoryFingerprint([string]$Root, [string]$Label) {
    $resolvedRoot = [IO.Path]::GetFullPath($Root)
    if (-not (Test-Path -LiteralPath $resolvedRoot -PathType Container)) {
        throw "$Label root is unavailable: $resolvedRoot"
    }
    $files = @(
        Get-ChildItem -LiteralPath $resolvedRoot -Recurse -File |
            Sort-Object FullName
    )
    $entries = @($files | ForEach-Object {
        $resolvedFile = [IO.Path]::GetFullPath($_.FullName)
        if (-not $resolvedFile.StartsWith($resolvedRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "$Label fingerprint escaped its root: $resolvedFile"
        }
        [ordered]@{
            relative_path = $resolvedFile.Substring($resolvedRoot.Length + 1).Replace('\', '/')
            length = [long]$_.Length
            sha256 = (Get-FileHash -LiteralPath $resolvedFile -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
    $json = $entries | ConvertTo-Json -Compress -Depth 4
    return [ordered]@{
        receipt_count = $files.Count
        entries_sha256 = Get-Sha256 $json
        entries = $entries
    }
}

function ConvertFrom-DatabaseFingerprintTransport([string]$EncodedText) {
    $compact = $EncodedText.Trim()
    if (
        [string]::IsNullOrWhiteSpace($compact) -or
        $compact -notmatch '^[A-Za-z0-9+/]*={0,2}$'
    ) {
        throw 'Disposable database fingerprint transport is not canonical base64.'
    }
    try {
        $bytes = [Convert]::FromBase64String($compact)
    }
    catch [FormatException] {
        throw 'Disposable database fingerprint transport is not canonical base64.'
    }
    if ([Convert]::ToBase64String($bytes) -cne $compact) {
        throw 'Disposable database fingerprint transport is not canonical base64.'
    }
    try {
        $utf8 = [Text.UTF8Encoding]::new($false, $true)
        $text = $utf8.GetString($bytes)
    }
    catch [Text.DecoderFallbackException] {
        throw 'Disposable database fingerprint transport is not strict UTF-8.'
    }
    try {
        $value = $text | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw 'Disposable database fingerprint transport does not contain one JSON object.'
    }
    if (
        $null -eq $value -or
        $value.GetType().FullName -cne 'System.Management.Automation.PSCustomObject'
    ) {
        throw 'Disposable database fingerprint transport does not contain one JSON object.'
    }
    return $value
}

function Invoke-DatabaseFingerprint([string]$ContainerName, [string]$Sql) {
    $statement = $Sql.Trim()
    if ($statement.EndsWith(';', [StringComparison]::Ordinal)) {
        $statement = $statement.Substring(0, $statement.Length - 1).TrimEnd()
    }
    if ([string]::IsNullOrWhiteSpace($statement) -or $statement.Contains(';')) {
        throw 'Disposable database fingerprint must be exactly one SQL statement.'
    }
    # Native stdout in Windows PowerShell can be decoded through the active
    # legacy code page.  UTF-8 CJK bytes immediately before an escaped quote
    # can consume its backslash under CP936 and turn valid JSON into invalid
    # text.  PostgreSQL therefore emits a single ASCII-only base64 cell; only
    # after capture do we decode strict UTF-8 and parse JSON.
    $transportSql = "SELECT replace(encode(convert_to((`n$statement`n)::text, 'UTF8'), 'base64'), E'\n', '');"
    $stderrPath = Join-Path ([IO.Path]::GetTempPath()) (
        "walnut-int1-psql-$([Guid]::NewGuid().ToString('N')).stderr.log"
    )
    try {
        $encodedText = (& docker exec $ContainerName psql -U walnut -d walnut_int1 --tuples-only --no-align --no-psqlrc --quiet --set=ON_ERROR_STOP=1 --command $transportSql 2> $stderrPath | Out-String).Trim()
        $exitCode = $LASTEXITCODE
    }
    finally {
        if (Test-Path -LiteralPath $stderrPath) {
            [IO.File]::Delete($stderrPath)
        }
    }
    if ($exitCode -ne 0) {
        throw "Failed to read the disposable database diagnostic fingerprint (psql exit $exitCode)."
    }
    return ConvertFrom-DatabaseFingerprintTransport $encodedText
}

function Wait-LearnerProjectionClosure(
    [string]$ContainerName,
    [int]$ExpectedCount,
    [int]$TimeoutSeconds
) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $state = Invoke-DatabaseFingerprint $ContainerName @"
SELECT json_build_object(
  'total', (SELECT count(*) FROM learner_projection_jobs WHERE tenant_id='tenant_yaya'),
  'succeeded', (SELECT count(*) FROM learner_projection_jobs WHERE tenant_id='tenant_yaya' AND status='SUCCEEDED'),
  'terminal_closed', (SELECT count(*) FROM learner_projection_jobs WHERE tenant_id='tenant_yaya' AND status='SUCCEEDED' AND lease_owner IS NULL AND lease_expires_at IS NULL AND next_attempt_at IS NULL AND result_sha256 ~ '^[a-f0-9]{64}$' AND result_json IS NOT NULL AND last_error_json IS NULL AND completed_at IS NOT NULL)
)::text;
"@
        if (
            [int]$state.total -eq $ExpectedCount -and
            [int]$state.succeeded -eq $ExpectedCount -and
            [int]$state.terminal_closed -eq $ExpectedCount
        ) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for $ExpectedCount terminal learner projections."
}

function ConvertTo-StableJson([object]$Value) {
    return $Value | ConvertTo-Json -Compress -Depth 30
}

function Get-JsonIntProperty([object]$Value, [string]$Name) {
    if ($null -eq $Value) {
        return 0
    }
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return 0
    }
    return [int]$property.Value
}

function Get-RelaySideEffectFingerprint([object]$Statistics, [bool]$IsRealProvider) {
    $projection = (ConvertTo-StableJson $Statistics) | ConvertFrom-Json
    if (-not $IsRealProvider) {
        $capabilityProperty = $projection.PSObject.Properties['capability_gets']
        if ($null -eq $capabilityProperty -or [int]$capabilityProperty.Value -lt 0) {
            throw 'Deterministic relay statistics have no valid capability probe counter.'
        }
        # A restarted workflow worker must prove the relay contract before it
        # may claim work.  That GET is read-only operational evidence, not a
        # Provider generation or business side effect, so compare every other
        # relay statistic byte while accounting for this probe separately.
        $projection.PSObject.Properties.Remove('capability_gets')
    }
    return $projection
}

function New-RestartFingerprintComparison([string]$BeforeJson, [string]$AfterJson) {
    $unchanged = $BeforeJson -ceq $AfterJson
    $firstDifferentProperty = $null
    if (-not $unchanged) {
        $before = $BeforeJson | ConvertFrom-Json
        $after = $AfterJson | ConvertFrom-Json
        $propertyNames = @(
            @($before.PSObject.Properties.Name)
            @($after.PSObject.Properties.Name)
        ) | Sort-Object -Unique
        foreach ($name in $propertyNames) {
            $beforeProperty = $before.PSObject.Properties[$name]
            $afterProperty = $after.PSObject.Properties[$name]
            if (
                $null -eq $beforeProperty -or
                $null -eq $afterProperty -or
                (ConvertTo-StableJson $beforeProperty.Value) -cne (ConvertTo-StableJson $afterProperty.Value)
            ) {
                $firstDifferentProperty = [string]$name
                break
            }
        }
        if ($null -eq $firstDifferentProperty) {
            $firstDifferentProperty = '_serialized_order'
        }
    }
    return [ordered]@{
        unchanged = $unchanged
        before_sha256 = Get-Sha256 $BeforeJson
        after_sha256 = Get-Sha256 $AfterJson
        first_different_property = $firstDifferentProperty
    }
}

function Get-GatewayRequestAudit([string]$LogPath, [int]$TimeoutSeconds = 5) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $entries = @()
    $requestPattern = '"(?<method>[A-Z]+)\s+(?<target>\S+)\s+HTTP/(?<version>[0-9.]+)"\s+(?<status>[0-9]{3})'
    do {
        $entries = @(
            if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
                foreach ($line in @(Get-Content -LiteralPath $LogPath -ErrorAction Stop)) {
                    $match = [regex]::Match([string]$line, $requestPattern)
                    if ($match.Success) {
                        [ordered]@{
                            method = $match.Groups['method'].Value
                            target = $match.Groups['target'].Value
                            http_version = $match.Groups['version'].Value
                            status = [int]$match.Groups['status'].Value
                        }
                    }
                }
            }
        )
        if ($entries.Count -gt 0) {
            break
        }
        Start-Sleep -Milliseconds 50
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($entries.Count -eq 0) {
        throw 'Recovery-only Gateway access log contains no parseable HTTP request records.'
    }
    $unsafe = @($entries | Where-Object { $_.method -notin @('GET', 'HEAD', 'OPTIONS') })
    if ($unsafe.Count -ne 0) {
        $methods = @($unsafe | ForEach-Object { [string]$_.method } | Sort-Object -Unique)
        throw "Recovery-only Godot process issued mutating or unexpected HTTP methods: $($methods -join ',')."
    }
    $getCount = @($entries | Where-Object { $_.method -eq 'GET' }).Count
    if ($getCount -lt 8) {
        throw "Recovery-only Gateway access log contains only $getCount GET requests; the canonical read path is incomplete."
    }
    $methodCounts = [ordered]@{}
    foreach ($method in @($entries | ForEach-Object { [string]$_.method } | Sort-Object -Unique)) {
        $methodCounts[$method] = @($entries | Where-Object { $_.method -eq $method }).Count
    }
    $requestJson = $entries | ConvertTo-Json -Compress -Depth 4
    return [ordered]@{
        request_count = $entries.Count
        get_count = $getCount
        mutating_method_count = $unsafe.Count
        method_counts = $methodCounts
        request_set_sha256 = Get-Sha256 $requestJson
    }
}

function Write-NotLive([string]$Reason, [object]$Preflight) {
    Restore-UpstreamKeyEnvironment
    $value = [ordered]@{
        classification = $classification
        status = 'NOT_LIVE'
        reason = $Reason
        preflight = $Preflight
    }
    Write-Output ("INT1_LOCAL_DIAGNOSTIC_NOT_LIVE " + ($value | ConvertTo-Json -Compress -Depth 8))
}

if (
    $TotalDeadlineSeconds -le 180 -or
    $requiredInt1E2eTokenLifetimeSeconds -gt $int1E2eTokenLifetimeSeconds
) {
    Write-NotLive 'TotalDeadlineSeconds exceeds the bounded formal two-phase token lifetime budget.' ([ordered]@{
        total_deadline_seconds = $TotalDeadlineSeconds
        transition_budget_seconds = $int1E2eTransitionBudgetSeconds
        required_token_lifetime_seconds = $requiredInt1E2eTokenLifetimeSeconds
        token_lifetime_seconds = $int1E2eTokenLifetimeSeconds
    })
    exit 2
}

if ($RealProvider) {
    if ($env:WALNUT_INT1_REAL_PROVIDER_E2E -ne 'true') {
        Write-NotLive 'Set WALNUT_INT1_REAL_PROVIDER_E2E=true for the explicit billable live gate.' $null
        exit 2
    }
    if (
        $env:WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS -cne
        [string]$expectedRealProviderGenerationLimit
    ) {
        Write-NotLive "Use the real-Provider wrapper with its bounded $expectedRealProviderGenerationLimit-generation pre-billing limit." $null
        exit 2
    }
    $hasDirectUpstreamKey = -not [string]::IsNullOrWhiteSpace($originalUpstreamKey)
    $hasUpstreamKeyFile = -not [string]::IsNullOrWhiteSpace($originalUpstreamKeyFile)
    if ($hasDirectUpstreamKey -eq $hasUpstreamKeyFile) {
        Write-NotLive 'Set exactly one upstream Provider key source.' $null
        exit 2
    }
    try {
        if ($hasUpstreamKeyFile) {
            $realProviderUpstreamKey = Read-WindowsAclControlledProviderKey $originalUpstreamKeyFile
        }
        else {
            Assert-ProviderSecretText $originalUpstreamKey
            $realProviderUpstreamKey = $originalUpstreamKey
        }
    }
    catch {
        Write-NotLive 'The upstream Provider key source failed secure validation.' $null
        exit 2
    }
    Clear-UpstreamKeyEnvironment
}

foreach ($requiredPath in @($backendPython, $relayScript, $realProviderFaultProxyScript, $frontendRunner, $agentRoot)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        Write-NotLive "Required path is unavailable: $requiredPath" $null
        exit 2
    }
}
if ([string]::IsNullOrWhiteSpace($GodotExe)) {
    $GodotExe = Join-Path $workspaceParent 'tools\godot-4.5.2\Godot_v4.5.2-stable_win64_console.exe'
}
if (-not (Test-Path -LiteralPath $GodotExe)) {
    Write-NotLive 'Pinned Godot 4.5.2 executable is unavailable.' $null
    exit 2
}

$dockerVersion = (& docker version --format '{{.Server.Version}}' 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    Write-NotLive 'Docker daemon is unavailable.' @{ docker = $dockerVersion }
    exit 2
}
$operatingSystem = Get-CimInstance Win32_OperatingSystem
$freeMemoryBytes = [long]$operatingSystem.FreePhysicalMemory * 1024
$driveName = ([IO.Path]::GetPathRoot($backendRoot)).Substring(0, 1)
$freeDiskBytes = [long](Get-PSDrive -Name $driveName).Free
$dockerBaseline = Get-DockerRunningBaseline
$containerCount = [int]$dockerBaseline.count
$hostGatewayProcessCount = @(Get-BackendGatewayProcessIds).Count
$gatewayPort = 8790
$gatewayPortAvailable = Test-LocalTcpPortAvailable $gatewayPort
$relayPort = Get-FreeTcpPort
$privateRelayPort = Get-FreeTcpPort -ExcludedPorts @($relayPort)
$postgresPort = Get-FreeTcpPort -ExcludedPorts @($relayPort, $privateRelayPort)
$dockerConflictReason = $null
try {
    Assert-NoDiagnosticDockerConflict `
        $dockerBaseline `
        $postgresName `
        $postgresResourceOwner `
        @($gatewayPort, $relayPort, $privateRelayPort, $postgresPort)
}
catch {
    $dockerConflictReason = $_.Exception.Message
}
$postgresImageDigestPinned = Test-DigestPinnedImage $PostgresImage
$sandboxImageDigestPinned = Test-DigestPinnedImage $SandboxImage
$postgresImagePresent = $false
$sandboxImagePresent = $false
if ($postgresImageDigestPinned) {
    & docker image inspect $PostgresImage *> $null
    $postgresImagePresent = $LASTEXITCODE -eq 0
}
if ($sandboxImageDigestPinned) {
    & docker image inspect $SandboxImage *> $null
    $sandboxImagePresent = $LASTEXITCODE -eq 0
}
$preflight = [ordered]@{
    docker_server_version = $dockerVersion
    running_container_count = $containerCount
    running_container_ids = @($dockerBaseline.ids)
    running_container_baseline_sha256 = [string]$dockerBaseline.sha256
    running_container_baseline_canonical_utf8_base64 = [string]$dockerBaseline.canonical_utf8_base64
    running_container_all_running = [bool]$dockerBaseline.all_running
    running_container_conflict = $dockerConflictReason
    host_gateway_process_count = $hostGatewayProcessCount
    free_memory_bytes = $freeMemoryBytes
    free_disk_bytes = $freeDiskBytes
    postgres_image = $PostgresImage
    postgres_image_digest_pinned = $postgresImageDigestPinned
    postgres_image_present = $postgresImagePresent
    sandbox_image = $SandboxImage
    sandbox_image_digest_pinned = $sandboxImageDigestPinned
    sandbox_image_present = $sandboxImagePresent
    gateway_loopback_port = $gatewayPort
    gateway_loopback_port_available = $gatewayPortAvailable
}
Write-Output ("INT1_LOCAL_DIAGNOSTIC_PREFLIGHT " + ($preflight | ConvertTo-Json -Compress))
if (-not $postgresImageDigestPinned -or -not $sandboxImageDigestPinned) {
    Write-NotLive 'PostgreSQL and Sandbox images must use exact name@sha256:64hex identities.' $preflight
    exit 2
}
if ($freeMemoryBytes -lt $MinimumFreeMemoryBytes) {
    Write-NotLive 'Free physical memory is below the diagnostic safety threshold.' $preflight
    exit 2
}
if ($null -ne $dockerConflictReason) {
    Write-NotLive $dockerConflictReason $preflight
    exit 2
}
if ($hostGatewayProcessCount -ne 0) {
    Write-NotLive 'The diagnostic requires no preexisting Backend Gateway process.' $preflight
    exit 2
}
if ($freeDiskBytes -lt $MinimumFreeDiskBytes) {
    Write-NotLive 'Free disk is below the diagnostic safety threshold.' $preflight
    exit 2
}
if (-not $postgresImagePresent -or -not $sandboxImagePresent) {
    Write-NotLive 'Required local Docker image is absent; this harness never pulls implicitly.' $preflight
    exit 2
}
if (-not $gatewayPortAvailable) {
    Write-NotLive 'The contract-declared loopback Gateway port 8790 is already in use.' $preflight
    exit 2
}

$relaySecret = New-RandomHex 32
$databasePassword = New-RandomHex 24
$authSecret = New-RandomHex 32
$environmentNames = @(
    'PYTHONPATH',
    'WALNUT_DATABASE_URL',
    'WALNUT_CONTRACT_PATH',
    'WALNUT_CONTRACT_RELEASE_PATH',
    'WALNUT_RUNTIME_ROOT',
    'WALNUT_INT1_E2E_SEED',
    'WALNUT_ENABLE_WORLD_PRESENTATION',
    'WALNUT_ENABLE_SKILL_PATCH',
    'WALNUT_DEVELOPMENT_AUTH',
    'WALNUT_AUTH_HMAC_SECRET',
    'WALNUT_AUTH_ISSUER',
    'WALNUT_AUTH_AUDIENCE',
    'WALNUT_AUTH_CLOCK_SKEW_SECONDS',
    'WALNUT_AUTH_MAXIMUM_LIFETIME_SECONDS',
    'WALNUT_TENANT_ID',
    'WALNUT_WORKER_ID',
    'WALNUT_DOCKER_EXECUTABLE',
    'WALNUT_SANDBOX_IMAGE',
    'WALNUT_SANDBOX_CPU_MS',
    'WALNUT_SANDBOX_WALL_MS',
    'WALNUT_SANDBOX_MEMORY_BYTES',
    'WALNUT_SANDBOX_MAX_PROCESSES',
    'WALNUT_SANDBOX_MAX_OUTPUT_BYTES',
    'WALNUT_LLM_RELAY_ENDPOINT',
    'WALNUT_LLM_RELAY_API_KEY',
    'WALNUT_LLM_RELAY_ALLOW_INSECURE_LOCALHOST',
    'WALNUT_LLM_RELAY_REQUIRED_RETENTION_SECONDS',
    'WALNUT_LLM_RELAY_MAX_RESPONSE_BYTES',
    'WALNUT_LLM_RELAY_CAPABILITY_TIMEOUT_MS',
    'WALNUT_LLM_PROVIDER',
    'WALNUT_LLM_MODEL',
    'WALNUT_LLM_RESPONSE_FORMAT',
    'WALNUT_LLM_THINKING_MODE',
    'WALNUT_LLM_RELAY_SERVER_API_KEY',
    'WALNUT_LLM_UPSTREAM_API_KEY',
    'WALNUT_LLM_UPSTREAM_API_KEY_FILE',
    'WALNUT_LLM_UPSTREAM_ENDPOINT',
    'WALNUT_LLM_RELAY_BIND_HOST',
    'WALNUT_LLM_RELAY_BIND_PORT',
    'WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS',
    'WALNUT_INT1_RELAY_API_KEY',
    'WALNUT_INT1_RELAY_PROVIDER',
    'WALNUT_INT1_RELAY_MODEL',
    'WALNUT_INT1_RELAY_DROP_FIRST_PUT_ACK',
    'WALNUT_INT1_RELAY_FAIL_FIRST_RECONCILE',
    'WALNUT_PROMPT_VERSION',
    'WALNUT_TEACHING_SPEC_VERSION',
    'WALNUT_WORLD_RULES_VERSION',
    'WALNUT_WORLD_CONTENT_VERSION',
    'WALNUT_WORLD_SUCCESS_SCORE',
    'WALNUT_WORKER_LEASE_SECONDS',
    'WALNUT_WORKER_IDLE_POLL_SECONDS',
    'WALNUT_LEARNER_WORKER_ID',
    'WALNUT_LEARNER_WORKER_LEASE_SECONDS',
    'WALNUT_LEARNER_WORKER_IDLE_POLL_SECONDS',
    'WALNUT_LEARNER_WORKER_MAXIMUM_ATTEMPTS',
    'YAYA_API_BASE_URL',
    'YAYA_AUTH_TOKEN',
    'GODOT_EXE'
)
$previousEnvironment = @{}
foreach ($name in $environmentNames) {
    if ($name -eq 'WALNUT_LLM_UPSTREAM_API_KEY') {
        $previousEnvironment[$name] = $originalUpstreamKey
    }
    elseif ($name -eq 'WALNUT_LLM_UPSTREAM_API_KEY_FILE') {
        $previousEnvironment[$name] = $originalUpstreamKeyFile
    }
    else {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
}

try {
    New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $runtimeRoot 'sandbox-results') -Force | Out-Null
    if (Test-Path -LiteralPath $phase1GodotFingerprintPath) {
        throw 'The run-scoped phase-1 Godot authority fingerprint path was not fresh.'
    }

    $env:PYTHONPATH = (Join-Path $backendRoot 'src') + [IO.Path]::PathSeparator + (Join-Path $agentRoot 'python')
    if (-not $RealProvider) {
        $env:WALNUT_INT1_RELAY_API_KEY = $relaySecret
        $env:WALNUT_INT1_RELAY_PROVIDER = 'int1-local-relay'
        $env:WALNUT_INT1_RELAY_MODEL = 'int1-local-model-v1'
        $env:WALNUT_INT1_RELAY_DROP_FIRST_PUT_ACK = 'true'
        $env:WALNUT_INT1_RELAY_FAIL_FIRST_RECONCILE = 'true'
        $relayProcess = Start-Process -FilePath $backendPython -ArgumentList @($relayScript, '--port', [string]$relayPort) -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runRoot 'relay.stdout.log') -RedirectStandardError (Join-Path $runRoot 'relay.stderr.log')
        $startedProcesses.Add($relayProcess)
        Wait-LocalPort $relayPort 15
        Write-Output 'INT1_LOCAL_DIAGNOSTIC_MILESTONE fixture-relay-ready'
    }

    if (-not (Test-DockerVolumeAbsent $postgresVolumeName)) {
        throw 'Refusing to reuse a preexisting PostgreSQL data volume.'
    }
    $postgresVolumeAbsentBeforeCreate = $true
    $createdPostgresVolume = New-OwnedPostgresVolume `
        $postgresVolumeName `
        $runId `
        $postgresResourceOwner `
        ([ref]$postgresVolumeCreated)
    $postgresArguments = @(
        'run', '--detach', '--name', $postgresName,
        '--label', "walnut.int1.run_id=$runId",
        '--label', "walnut.int1.owner=$postgresResourceOwner",
        '--publish', "127.0.0.1:${postgresPort}:5432",
        '--mount', "type=volume,source=$postgresVolumeName,target=/var/lib/postgresql/data",
        '--env', 'POSTGRES_DB=walnut_int1',
        '--env', 'POSTGRES_USER=walnut',
        '--env', "POSTGRES_PASSWORD=$databasePassword",
        '--health-cmd', '"pg_isready -U walnut -d walnut_int1"',
        '--health-interval', '1s', '--health-timeout', '3s', '--health-retries', '30',
        $PostgresImage
    )
    $postgresId = Start-OwnedPostgresContainer `
        $postgresName `
        $runId `
        $postgresResourceOwner `
        $postgresArguments `
        ([ref]$postgresCreated) `
        ([ref]$postgresStarted) `
        ([ref]$postgresId)
    Wait-PostgresHealthy $postgresId 45
    Write-Output 'INT1_LOCAL_DIAGNOSTIC_MILESTONE fresh-postgres-ready'

    $env:WALNUT_DATABASE_URL = "postgresql+asyncpg://walnut:$databasePassword@127.0.0.1:$postgresPort/walnut_int1"
    $env:WALNUT_CONTRACT_PATH = $agentRoot
    $env:WALNUT_CONTRACT_RELEASE_PATH = Join-Path $backendRoot 'contract-release.json'
    $env:WALNUT_RUNTIME_ROOT = $runtimeRoot
    $env:WALNUT_INT1_E2E_SEED = 'true'
    $env:WALNUT_ENABLE_WORLD_PRESENTATION = if ($EnableWorldPresentation) { 'true' } else { 'false' }
    $env:WALNUT_ENABLE_SKILL_PATCH = if ($EnableSkillPatch) { 'true' } else { 'false' }
    $env:WALNUT_DEVELOPMENT_AUTH = 'false'
    $env:WALNUT_AUTH_HMAC_SECRET = $authSecret
    $env:WALNUT_AUTH_ISSUER = 'walnut-int1-local-diagnostic'
    $env:WALNUT_AUTH_AUDIENCE = 'walnut-game-client'
    $env:WALNUT_AUTH_CLOCK_SKEW_SECONDS = '5'
    $env:WALNUT_AUTH_MAXIMUM_LIFETIME_SECONDS = [string]$int1E2eTokenLifetimeSeconds
    $env:WALNUT_TENANT_ID = 'tenant_yaya'
    $phase1WorkerId = "int1-local-phase1-$($runId.Substring(0, 12))"
    $env:WALNUT_WORKER_ID = $phase1WorkerId
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
    $env:WALNUT_LLM_PROVIDER = if ($RealProvider) { $RealProviderName } else { 'int1-local-relay' }
    $env:WALNUT_LLM_MODEL = if ($RealProvider) { $RealProviderModel } else { 'int1-local-model-v1' }
    $env:WALNUT_LLM_RESPONSE_FORMAT = 'json_object'
    $env:WALNUT_LLM_THINKING_MODE = 'disabled'
    $env:WALNUT_PROMPT_VERSION = 'int1-prompt-v1'
    $env:WALNUT_TEACHING_SPEC_VERSION = 'agent-teaching-v1'
    $env:WALNUT_WORLD_RULES_VERSION = 'farm-rules-1'
    $env:WALNUT_WORLD_CONTENT_VERSION = '1.0.0'
    $env:WALNUT_WORLD_SUCCESS_SCORE = '8'
    $env:WALNUT_WORKER_LEASE_SECONDS = '120'
    $env:WALNUT_WORKER_IDLE_POLL_SECONDS = '0.1'
    $phase1LearnerWorkerId = "int1-learner-phase1-$($runId.Substring(0, 12))"
    $env:WALNUT_LEARNER_WORKER_ID = $phase1LearnerWorkerId
    $env:WALNUT_LEARNER_WORKER_LEASE_SECONDS = '120'
    $env:WALNUT_LEARNER_WORKER_IDLE_POLL_SECONDS = '0.1'
    $env:WALNUT_LEARNER_WORKER_MAXIMUM_ATTEMPTS = '5'

    $featureGateText = (& $backendPython -c "import json; from walnut_backend.bootstrap import Settings; from walnut_backend.worker_main import WorkerSettings; print(json.dumps({'gateway_skill_patch_enabled': Settings.from_env().skill_patch_enabled, 'worker_skill_patch_enabled': WorkerSettings.from_env().skill_patch_enabled}, separators=(',', ':')))" | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not read the independently default-closed Gateway and worker feature gates.'
    }
    $featureGates = $featureGateText | ConvertFrom-Json
    if (
        $featureGates.gateway_skill_patch_enabled -ne [bool]$EnableSkillPatch -or
        $featureGates.worker_skill_patch_enabled -ne [bool]$EnableSkillPatch
    ) {
        throw 'Gateway and workflow worker did not resolve the exact selected Skill Patch flag.'
    }
    Write-Output (
        'INT1_LOCAL_DIAGNOSTIC_FEATURE_GATES ' +
        ($featureGates | ConvertTo-Json -Compress)
    )

    Push-Location $backendRoot
    try {
        & $backendPython -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) {
            throw "Alembic failed with exit code $LASTEXITCODE."
        }
        $seedText = (& $backendPython -m walnut_backend.int1_e2e_authority | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Authority seed failed with exit code $LASTEXITCODE."
        }
        $seed = $seedText | ConvertFrom-Json
        if ($seed.status -ne 'SEEDED' -or [string]::IsNullOrWhiteSpace([string]$seed.authorization)) {
            throw 'Authority seed returned an invalid handoff.'
        }
    }
    finally {
        Pop-Location
    }
    Write-Output 'INT1_LOCAL_DIAGNOSTIC_MILESTONE migrated-and-authority-seeded'

    if ($RealProvider) {
        $env:WALNUT_LLM_RELAY_SERVER_API_KEY = $relaySecret
        $env:WALNUT_LLM_UPSTREAM_ENDPOINT = $RealProviderEndpoint
        $env:WALNUT_LLM_RELAY_BIND_HOST = '127.0.0.1'
        $env:WALNUT_LLM_RELAY_BIND_PORT = [string]$privateRelayPort
        $relayProcess = Start-PrivateRealProviderRelay `
            (Join-Path $runRoot 'relay.stdout.log') `
            (Join-Path $runRoot 'relay.stderr.log')
        $startedProcesses.Add($relayProcess)
        Wait-LocalPort $privateRelayPort 15
        if ($relayProcess.HasExited) {
            throw 'Private real-Provider relay exited during startup.'
        }
        $faultProxyProcess = Start-Process -FilePath $backendPython `
            -ArgumentList @(
                $realProviderFaultProxyScript,
                '--port', [string]$relayPort,
                '--upstream-port', [string]$privateRelayPort
            ) `
            -WorkingDirectory $backendRoot `
            -WindowStyle Hidden `
            -PassThru `
            -RedirectStandardOutput (Join-Path $runRoot 'relay-fault-proxy.stdout.log') `
            -RedirectStandardError (Join-Path $runRoot 'relay-fault-proxy.stderr.log')
        $startedProcesses.Add($faultProxyProcess)
        Wait-LocalPort $relayPort 15
        if ($faultProxyProcess.HasExited) {
            throw 'Real-Provider response-loss proxy exited during startup.'
        }
        Write-Output 'INT1_LOCAL_DIAGNOSTIC_MILESTONE real-provider-relay-and-response-loss-proxy-ready'
    }

    $phase1GatewayStdout = Join-Path $runRoot 'gateway.stdout.log'
    $gatewayProcess = Start-Process -FilePath $backendPython -ArgumentList @('-m', 'uvicorn', 'walnut_backend.main:app', '--host', '127.0.0.1', '--port', [string]$gatewayPort) -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $phase1GatewayStdout -RedirectStandardError (Join-Path $runRoot 'gateway.stderr.log')
    $startedProcesses.Add($gatewayProcess)
    $workerProcess = Start-Process -FilePath $backendPython -ArgumentList @('-m', 'walnut_backend.worker_main') -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runRoot 'worker.stdout.log') -RedirectStandardError (Join-Path $runRoot 'worker.stderr.log')
    $startedProcesses.Add($workerProcess)
    $learnerProcess = Start-Process -FilePath $backendPython -ArgumentList @('-m', 'walnut_backend.learner_worker_main') -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runRoot 'learner-worker.stdout.log') -RedirectStandardError (Join-Path $runRoot 'learner-worker.stderr.log')
    $startedProcesses.Add($learnerProcess)
    Wait-LocalPort $gatewayPort 45
    if ($gatewayProcess.HasExited -or $workerProcess.HasExited -or $learnerProcess.HasExited) {
        throw 'Gateway, workflow worker, or learner worker exited during startup.'
    }
    $phase1GatewayPid = [int]$gatewayProcess.Id
    $phase1WorkerPid = [int]$workerProcess.Id
    $phase1LearnerWorkerPid = [int]$learnerProcess.Id
    $phase1Listener = Assert-SingleGatewayListener $gatewayPort $phase1GatewayPid 'phase1'

    $rawAuthorization = [string]$seed.authorization
    if (-not $rawAuthorization.StartsWith('Bearer ', [StringComparison]::Ordinal)) {
        throw 'Authority handoff did not contain a Bearer authorization value.'
    }
    $headers = @{
        Authorization = $rawAuthorization
        'X-Request-Id' = "req_int1_preflight_$($runId.Substring(0, 16))"
        'X-Trace-Id' = "trace_int1_preflight_$($runId.Substring(0, 16))"
        'X-Correlation-Id' = "corr_int1_preflight_$($runId.Substring(0, 16))"
        'X-Schema-Version' = '1.0.0'
    }
    $bootstrap = Invoke-RestMethod -Uri "http://127.0.0.1:$gatewayPort/v1/student-bootstrap" -Method Get -Headers $headers -TimeoutSec 30
    if ($bootstrap.contract_version -ne '0.4.0') {
        throw 'Gateway bootstrap preflight did not return StudentBootstrap v2.'
    }
    Write-Output 'INT1_LOCAL_DIAGNOSTIC_MILESTONE gateway-worker-ready'

    $env:YAYA_API_BASE_URL = "http://127.0.0.1:$gatewayPort"
    $env:YAYA_AUTH_TOKEN = $rawAuthorization.Substring(7)
    $env:GODOT_EXE = $GodotExe
    $phase1GodotStdout = Join-Path $runRoot 'godot-phase1.stdout.log'
    $phase1GodotStderr = Join-Path $runRoot 'godot-phase1.stderr.log'
    $phase1FrontendArguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $frontendRunner,
        '-GodotExe', $GodotExe,
        '-TotalDeadlineSeconds', [string]$TotalDeadlineSeconds,
        '-ResourceDeadlineSeconds', '180',
        '-InteractionDeadlineSeconds', '90',
        '-Phase1FingerprintPath', $phase1GodotFingerprintPath,
        '-ResetPersistence'
    )
    if ($EnableWorldPresentation) {
        $phase1FrontendArguments += '-EnableWorldPresentation'
    }
    if ($EnableSkillPatch) {
        $phase1FrontendArguments += '-EnableSkillPatch'
    }
    $godotProcess = Start-Process -FilePath 'powershell.exe' -ArgumentList $phase1FrontendArguments `
        -WorkingDirectory $frontendRoot -WindowStyle Hidden -Wait -PassThru `
        -RedirectStandardOutput $phase1GodotStdout -RedirectStandardError $phase1GodotStderr
    $godotExitCode = $godotProcess.ExitCode
    $godotLines = @(
        @(Get-Content -LiteralPath $phase1GodotStdout -ErrorAction SilentlyContinue)
        @(Get-Content -LiteralPath $phase1GodotStderr -ErrorAction SilentlyContinue)
    ) | ForEach-Object { [string]$_ }
    $godotLines | ForEach-Object { Write-Output $_ }
    if ($godotExitCode -ne 0) {
        throw "Godot cross-repository diagnostic failed with exit code $godotExitCode."
    }
    $passPrefix = 'REAL_GATEWAY_CHAIN_E2E_PASS '
    $passLines = @($godotLines | Where-Object { $_.StartsWith($passPrefix, [StringComparison]::Ordinal) })
    if ($passLines.Count -ne 1) {
        throw 'Godot diagnostic emitted no unique structured PASS fingerprint.'
    }
    $godotFingerprint = $passLines[0].Substring($passPrefix.Length) | ConvertFrom-Json
    $expectedRelayGenerationCount = if ($EnableSkillPatch) { 16 } else { 12 }
    $expectedTurnCount = if ($EnableSkillPatch) { 6 } else { 4 }
    $expectedRunCount = if ($EnableSkillPatch) { 5 } else { 4 }
    $expectedLearnerCount = if ($EnableSkillPatch) { 5 } else { 4 }
    $expectedFrontendPostCount = if ($EnableSkillPatch) { 12 } else { 9 }
    $expectedFrontendPutCount = if ($EnableSkillPatch) { 1 } else { 2 }
    $expectedSessionCommandCount = 1
    $expectedBuildCommandCount = 2
    $expectedActivationCommandCount = 2
    $expectedFailureRunCount = if ($EnableSkillPatch) { 4 } else { 3 }
    $expectedRejectedCommandCount = if ($EnableSkillPatch) { 4 } else { 3 }
    # Every accepted Session, Build, Activation, and Turn owns exactly one
    # terminal Command and one durable command receipt.  Derive totals from
    # those public operations so the M2 six-Turn path cannot omit Session.
    $expectedCommandCount = $expectedSessionCommandCount + $expectedBuildCommandCount + $expectedActivationCommandCount + $expectedTurnCount
    $expectedAppliedCommandCount = $expectedCommandCount - $expectedRejectedCommandCount
    $expectedSandboxReceiptCount = if ($EnableSkillPatch) { 10 } else { 8 }
    $expectedProviderResultMinimum = if ($EnableSkillPatch) { 11 } else { 8 }
    $expectedProductReceiptCount = if ($EnableSkillPatch) { 1 } else { 2 }
    $expectedEvidenceCount = if ($EnableSkillPatch) { 13 } else { 11 }
    # One committed World transaction advances the authoritative World stream
    # once.  Its eight student-visible actions are separate presentation rows.
    $expectedWorldCommitEventCount = 1
    $expectedWorldPresentationEventCount = 8
    $expectedPatchAuthorityCount = if ($EnableSkillPatch) { 1 } else { 0 }
    $expectedAssistedAuthorityCount = if ($EnableSkillPatch) { 1 } else { 0 }
    $expectedFailureCountSequence = if ($EnableSkillPatch) { '[1, 2, 3, 4]' } else { '[1, 2, 3]' }
    $expectedInteractionRoleSequence = if ($EnableSkillPatch) {
        '["teaching_agent", "teaching_agent", "bug_agent", "bug_agent", "teaching_agent", "book_agent"]'
    }
    else {
        '["teaching_agent", "teaching_agent", "bug_agent", "book_agent"]'
    }
    if ($EnableSkillPatch) {
        if (
            $godotFingerprint.skill_patch.enabled -ne $true -or
            [string]$godotFingerprint.skill_patch.status -ne 'PUBLIC_UI_CHAIN_CLOSED' -or
            $godotFingerprint.skill_patch.backend_authority_fingerprint_required -ne $true -or
            [int]$godotFingerprint.skill_patch.expected_transport_counts.POST -ne 12 -or
            [int]$godotFingerprint.skill_patch.expected_transport_counts.PUT -ne 1 -or
            [int]$godotFingerprint.skill_patch.expected_backend_counts.turns -ne 6 -or
            [int]$godotFingerprint.skill_patch.expected_backend_counts.runs -ne 5 -or
            [int]$godotFingerprint.skill_patch.expected_backend_counts.learner_jobs -ne 5 -or
            $godotFingerprint.skill_patch.public_terminal_run_get_validated_learner_projection -ne $true -or
            [string]$godotFingerprint.skill_patch.public_chain_sha256 -notmatch '^[a-f0-9]{64}$'
        ) {
            throw 'Godot phase-1 fingerprint did not close the exact formal M2 public UI chain.'
        }
    }
    $phase1TransportAudit = $godotFingerprint.transport_attempt_audit
    if (
        (Get-JsonIntProperty $phase1TransportAudit.method_counts 'POST') -ne $expectedFrontendPostCount -or
        (Get-JsonIntProperty $phase1TransportAudit.method_counts 'PUT') -ne $expectedFrontendPutCount -or
        (Get-JsonIntProperty $phase1TransportAudit.method_counts 'PATCH') -ne 0 -or
        (Get-JsonIntProperty $phase1TransportAudit.method_counts 'DELETE') -ne 0
    ) {
        throw 'Godot formal transport counts differ from the exact selected M1/M2 chain.'
    }
    if ($godotFingerprint.persistence_reset_performed -ne $true) {
        throw 'Phase-1 Godot process did not reset its exact test-only persistence identity.'
    }
    if (-not (Test-Path -LiteralPath $phase1GodotFingerprintPath -PathType Leaf)) {
        throw 'Phase-1 Godot process did not persist its run-scoped authority fingerprint.'
    }
    $phase1GodotFingerprintFile = Get-Item -LiteralPath $phase1GodotFingerprintPath
    if (
        $phase1GodotFingerprintFile.Length -le 0 -or
        ($phase1GodotFingerprintFile.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw 'The run-scoped phase-1 Godot authority fingerprint is empty or unsafe.'
    }
    Wait-LearnerProjectionClosure $postgresName $expectedLearnerCount 60
    if ($EnableSkillPatch) {
        Write-Output 'INT1_LOCAL_DIAGNOSTIC_MILESTONE five-learner-projections-terminal'
    }
    else {
        Write-Output 'INT1_LOCAL_DIAGNOSTIC_MILESTONE four-learner-projections-terminal'
    }

    $snapshotHeaders = $headers.Clone()
    $snapshotHeaders['X-Request-Id'] = "req_int1_snapshot_$($runId.Substring(0, 16))"
    $snapshotHeaders['X-Trace-Id'] = "trace_int1_snapshot_$($runId.Substring(0, 16))"
    $snapshotHeaders['X-Correlation-Id'] = "corr_int1_snapshot_$($runId.Substring(0, 16))"
    $worldId = [string]$bootstrap.world.world_id
    $worldSnapshot = Invoke-RestMethod -Uri "http://127.0.0.1:$gatewayPort/v1/worlds/$worldId/snapshot" -Method Get -Headers $snapshotHeaders -TimeoutSec 30
    if ([string]$worldSnapshot.state_hash -notmatch '^[a-f0-9]{64}$') {
        throw 'Gateway returned an invalid world state hash.'
    }

    $relayHeaders = @{
        Authorization = "Bearer $relaySecret"
        'X-Yaya-Llm-Protocol' = 'YAYA_RECOVERABLE_LLM_V1'
        Accept = 'application/json'
    }
    if ($RealProvider) {
        $relayStats = Invoke-RestMethod -Uri "http://127.0.0.1:$privateRelayPort/__private__/llm-relay/statistics" -Method Get -Headers $relayHeaders -TimeoutSec 10
        $relayFaultStats = Invoke-RestMethod -Uri "http://127.0.0.1:$relayPort/__int1_real_provider_fault_proxy__/statistics" -Method Get -Headers $relayHeaders -TimeoutSec 10
        $faultDispatchRows = @(
            $relayStats.dispatches |
                Where-Object { [string]$_.dispatch_id -eq [string]$relayFaultStats.fault_dispatch_id }
        )
        $unexpectedGenerationRows = @(
            $relayStats.dispatches |
                Where-Object { [int]$_.generation_count -ne 1 }
        )
        if (
            [int]$relayStats.unique_dispatches -lt $expectedRelayGenerationCount -or
            [int]$relayStats.unique_dispatches -gt $expectedRealProviderGenerationLimit -or
            [int]$relayStats.total_generations -ne [int]$relayStats.unique_dispatches -or
            [int]$relayStats.max_generation_count -ne 1 -or
            [int]$relayStats.states.SUCCEEDED -ne [int]$relayStats.unique_dispatches -or
            $unexpectedGenerationRows.Count -ne 0
        ) {
            throw 'Private relay statistics do not prove one successful Provider generation per dispatch.'
        }
        $verificationText = (& $backendPython (Join-Path $PSScriptRoot 'verify_real_provider_relay.py') --database-url $env:WALNUT_DATABASE_URL 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $verificationText.StartsWith('INT1_REAL_PROVIDER_RELAY_PASS ', [StringComparison]::Ordinal)) {
            throw 'Private relay database verification did not prove source=provider, degraded=false.'
        }
        if (
            [string]$relayFaultStats.classification -ne 'REAL_PROVIDER_RESPONSE_LOSS_PROXY_TEST_ONLY' -or
            [int]$relayFaultStats.acknowledgement_drops -ne 1 -or
            [int]$relayFaultStats.reconcile_unavailable_attempted -ne 1 -or
            [int]$relayFaultStats.reconcile_unavailable_delivered -ne 1 -or
            $relayFaultStats.terminal_before_drop -ne $true -or
            [string]$relayFaultStats.terminal_state -ne 'SUCCEEDED' -or
            [int]$relayFaultStats.terminal_generation_count -ne 1 -or
            $relayFaultStats.recovered_same_dispatch -ne $true -or
            [string]$relayFaultStats.recovered_dispatch_id -ne [string]$relayFaultStats.fault_dispatch_id -or
            [int]$relayFaultStats.recovered_generation_count -ne 1 -or
            $faultDispatchRows.Count -ne 1 -or
            [string]$faultDispatchRows[0].state -ne 'SUCCEEDED' -or
            [int]$faultDispatchRows[0].generation_count -ne 1
        ) {
            throw 'Real-Provider fault proxy does not prove one lost terminal acknowledgement and same-dispatch generation-one recovery.'
        }
        $terminalDispatchId = [string]$relayStats.dispatches[0].dispatch_id
        if ($relayProcess.HasExited -or $faultProxyProcess.HasExited) {
            throw 'Private relay or persistent response-loss proxy exited before restart verification.'
        }
        Stop-Process -Id $relayProcess.Id -Force
        $relayProcess.WaitForExit(5000) | Out-Null
        $relayProcess = Start-PrivateRealProviderRelay `
            (Join-Path $runRoot 'relay-restarted.stdout.log') `
            (Join-Path $runRoot 'relay-restarted.stderr.log')
        $startedProcesses.Add($relayProcess)
        Wait-LocalPort $privateRelayPort 15
        $recoveredProvider = Invoke-RestMethod -Uri "http://127.0.0.1:$relayPort/v1/llm/dispatches/$terminalDispatchId" -Method Get -Headers $relayHeaders -TimeoutSec 10
        if ($recoveredProvider.state -ne 'SUCCEEDED' -or [int]$recoveredProvider.generation_count -ne 1) {
            throw 'Persistent proxy GET after private-relay restart did not recover the terminal Provider resource.'
        }
    }
    else {
        $relayStats = Invoke-RestMethod -Uri "http://127.0.0.1:$relayPort/__int1_diagnostic__/stats" -Method Get -Headers $relayHeaders -TimeoutSec 10
        $unexpectedGenerationRows = @(
            $relayStats.dispatches |
                Where-Object { [int]$_.generation_count -ne 1 }
        )
        if (
            $relayStats.classification -ne 'DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER' -or
            [int]$relayStats.acknowledgement_drops -ne 1 -or
            [int]$relayStats.reconcile_unavailable -ne 1 -or
            [int]$relayStats.reconcile_gets -ne 2 -or
            [int]$relayStats.dispatch_puts -ne $expectedRelayGenerationCount -or
            [int]$relayStats.unique_dispatches -ne $expectedRelayGenerationCount -or
            [int]$relayStats.total_generations -ne [int]$relayStats.unique_dispatches -or
            [int]$relayStats.max_generation_count -ne 1 -or
            $unexpectedGenerationRows.Count -ne 0
        ) {
            throw 'Relay statistics do not prove lost-ACK retry recovery with one generation per dispatch.'
        }
    }

    $databaseSql = @"
SELECT (jsonb_build_object(
  'build_job_attempt', COALESCE((SELECT max(attempt) FROM workflow_jobs WHERE tenant_id='tenant_yaya' AND operation='CREATE_SKILL_BUILD'), 0),
  'build_job_fencing_token', COALESCE((SELECT max(fencing_token) FROM workflow_jobs WHERE tenant_id='tenant_yaya' AND operation='CREATE_SKILL_BUILD'), 0),
  'build_job_status', COALESCE((SELECT max(status) FROM workflow_jobs WHERE tenant_id='tenant_yaya' AND operation='CREATE_SKILL_BUILD'), 'ABSENT'),
  'turn_job_attempt', COALESCE((SELECT max(attempt) FROM workflow_jobs WHERE tenant_id='tenant_yaya' AND operation='EXECUTE_AGENT_TURN'), 0),
  'turn_job_fencing_token', COALESCE((SELECT max(fencing_token) FROM workflow_jobs WHERE tenant_id='tenant_yaya' AND operation='EXECUTE_AGENT_TURN'), 0),
  'turn_job_status', COALESCE((SELECT max(status) FROM workflow_jobs WHERE tenant_id='tenant_yaya' AND operation='EXECUTE_AGENT_TURN'), 'ABSENT'),
  'turn_worker_failure_receipts', (SELECT count(*) FROM job_step_receipts r JOIN workflow_jobs w ON w.tenant_id=r.tenant_id AND w.job_id=r.job_id WHERE w.tenant_id='tenant_yaya' AND w.operation='EXECUTE_AGENT_TURN' AND r.step_name LIKE 'WORKER_FAILURE_%'),
  'turn_worker_reconcile_receipts', (SELECT count(*) FROM job_step_receipts r JOIN workflow_jobs w ON w.tenant_id=r.tenant_id AND w.job_id=r.job_id WHERE w.tenant_id='tenant_yaya' AND w.operation='EXECUTE_AGENT_TURN' AND r.step_name LIKE 'WORKER_RECONCILE_%'),
  'provider_dispatch_receipts', (SELECT count(*) FROM job_step_receipts r JOIN workflow_jobs w ON w.tenant_id=r.tenant_id AND w.job_id=r.job_id WHERE w.tenant_id='tenant_yaya' AND w.operation='EXECUTE_AGENT_TURN' AND r.step_name LIKE '%PROVIDER_DISPATCH_%'),
  'provider_result_receipts', (SELECT count(*) FROM job_step_receipts r JOIN workflow_jobs w ON w.tenant_id=r.tenant_id AND w.job_id=r.job_id WHERE w.tenant_id='tenant_yaya' AND w.operation='EXECUTE_AGENT_TURN' AND r.step_name LIKE '%PROVIDER_RESULT_%'),
  'sandbox_dispatch_receipts', (SELECT count(*) FROM job_step_receipts WHERE tenant_id='tenant_yaya' AND step_name='SANDBOX_DISPATCHED'),
  'sandbox_result_receipts', (SELECT count(*) FROM job_step_receipts WHERE tenant_id='tenant_yaya' AND step_name='SKILL_INVOKED'),
  'build_certification_receipts', (SELECT count(*) FROM job_step_receipts WHERE tenant_id='tenant_yaya' AND step_name='BUILD_CERTIFIED'),
  'session_count', (SELECT count(*) FROM agent_sessions WHERE tenant_id='tenant_yaya'),
  'workspace_count', (SELECT count(*) FROM product_workspaces WHERE tenant_id='tenant_yaya'),
  'draft_count', (SELECT count(*) FROM product_skill_drafts WHERE tenant_id='tenant_yaya'),
  'build_count', (SELECT count(*) FROM skill_builds WHERE tenant_id='tenant_yaya'),
  'artifact_count', (SELECT count(*) FROM skill_artifacts WHERE tenant_id='tenant_yaya'),
  'certification_count', (SELECT count(*) FROM skill_certifications WHERE tenant_id='tenant_yaya'),
  'activation_count', (SELECT count(*) FROM skill_activations WHERE tenant_id='tenant_yaya'),
  'turn_count', (SELECT count(*) FROM agent_turns WHERE tenant_id='tenant_yaya'),
  'run_count', (SELECT count(*) FROM game_runs WHERE tenant_id='tenant_yaya'),
  'world_event_count', (SELECT count(*) FROM domain_events WHERE tenant_id='tenant_yaya' AND stream_id LIKE 'world:%'),
  'evidence_count', (SELECT count(*) FROM game_evidence WHERE tenant_id='tenant_yaya'),
  'interaction_count', (SELECT count(*) FROM product_agent_interactions WHERE tenant_id='tenant_yaya'),
  'learner_projection_count', (SELECT count(*) FROM learner_projection_jobs WHERE tenant_id='tenant_yaya'),
  'learner_projection_succeeded', (SELECT count(*) FROM learner_projection_jobs WHERE tenant_id='tenant_yaya' AND status='SUCCEEDED'),
  'learner_revision', COALESCE((SELECT max((profile_json->>'revision')::bigint) FROM learner_profiles WHERE tenant_id='tenant_yaya'), 0),
  'learner_profile_sha256', COALESCE((SELECT max(profile_sha256) FROM learner_profiles WHERE tenant_id='tenant_yaya'), ''),
  'terminal_command_count', (SELECT count(*) FROM commands WHERE tenant_id='tenant_yaya' AND terminal),
  'product_receipt_count', (SELECT count(*) FROM product_idempotency_receipts WHERE tenant_id='tenant_yaya'),
  'command_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(command_row) ORDER BY command_row.command_id)::text FROM commands AS command_row WHERE command_row.tenant_id='tenant_yaya'), '[]'),
  'workflow_status_material', COALESCE((SELECT jsonb_agg(to_jsonb(workflow_row) ORDER BY workflow_row.job_id)::text FROM workflow_jobs AS workflow_row WHERE workflow_row.tenant_id='tenant_yaya'), '[]'),
  'receipt_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(receipt_row) ORDER BY receipt_row.receipt_id)::text FROM job_step_receipts AS receipt_row WHERE receipt_row.tenant_id='tenant_yaya'), '[]'),
  'product_receipt_material', COALESCE((SELECT jsonb_agg(to_jsonb(product_receipt_row) ORDER BY product_receipt_row.receipt_id)::text FROM product_idempotency_receipts AS product_receipt_row WHERE product_receipt_row.tenant_id='tenant_yaya'), '[]'),
  'session_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(session_row) ORDER BY session_row.session_id)::text FROM agent_sessions AS session_row WHERE session_row.tenant_id='tenant_yaya'), '[]'),
  'workspace_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(workspace_row) ORDER BY workspace_row.workspace_id)::text FROM product_workspaces AS workspace_row WHERE workspace_row.tenant_id='tenant_yaya'), '[]'),
  'draft_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(draft_row) ORDER BY draft_row.draft_row_id)::text FROM product_skill_drafts AS draft_row WHERE draft_row.tenant_id='tenant_yaya'), '[]'),
  'build_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(build_row) ORDER BY build_row.build_id)::text FROM skill_builds AS build_row WHERE build_row.tenant_id='tenant_yaya'), '[]'),
  'artifact_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(artifact_row) ORDER BY artifact_row.tenant_id, artifact_row.artifact_sha256)::text FROM skill_artifacts AS artifact_row WHERE artifact_row.tenant_id='tenant_yaya'), '[]'),
  'certification_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(certification_row) ORDER BY certification_row.certification_id)::text FROM skill_certifications AS certification_row WHERE certification_row.tenant_id='tenant_yaya'), '[]'),
  'registry_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(registry_row) ORDER BY registry_row.tenant_id, registry_row.actor_id, registry_row.content_hash, registry_row.world_id, registry_row.agent_profile_id, registry_row.revision)::text FROM registry_entries AS registry_row WHERE registry_row.tenant_id='tenant_yaya'), '[]'),
  'activation_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(activation_row) ORDER BY activation_row.activation_id)::text FROM skill_activations AS activation_row WHERE activation_row.tenant_id='tenant_yaya'), '[]'),
  'turn_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(turn_row) ORDER BY turn_row.turn_row_id)::text FROM agent_turns AS turn_row WHERE turn_row.tenant_id='tenant_yaya'), '[]'),
  'run_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(run_row) ORDER BY run_row.run_id)::text FROM game_runs AS run_row WHERE run_row.tenant_id='tenant_yaya'), '[]'),
  'world_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(world_snapshot_row) ORDER BY world_snapshot_row.tenant_id, world_snapshot_row.world_id)::text FROM world_snapshots AS world_snapshot_row WHERE world_snapshot_row.tenant_id='tenant_yaya'), '[]'),
  'world_event_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(world_event_row) ORDER BY world_event_row.event_id)::text FROM domain_events AS world_event_row WHERE world_event_row.tenant_id='tenant_yaya' AND world_event_row.stream_id LIKE 'world:%'), '[]'),
  'evidence_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(evidence_row) ORDER BY evidence_row.evidence_id)::text FROM game_evidence AS evidence_row WHERE evidence_row.tenant_id='tenant_yaya'), '[]'),
  'interaction_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(interaction_row) ORDER BY interaction_row.interaction_row_id)::text FROM product_agent_interactions AS interaction_row WHERE interaction_row.tenant_id='tenant_yaya'), '[]'),
  'learner_projection_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(learner_job_row) ORDER BY learner_job_row.job_id)::text FROM learner_projection_jobs AS learner_job_row WHERE learner_job_row.tenant_id='tenant_yaya'), '[]')
) || jsonb_build_object(
  'command_count', (SELECT count(*) FROM commands WHERE tenant_id='tenant_yaya'),
  'applied_terminal_command_count', (SELECT count(*) FROM commands WHERE tenant_id='tenant_yaya' AND terminal AND status='APPLIED'),
  'rejected_terminal_command_count', (SELECT count(*) FROM commands WHERE tenant_id='tenant_yaya' AND terminal AND status='REJECTED'),
  'session_command_count', (SELECT count(*) FROM commands WHERE tenant_id='tenant_yaya' AND command_type='CREATE_AGENT_SESSION'),
  'build_command_count', (SELECT count(*) FROM commands WHERE tenant_id='tenant_yaya' AND command_type='CREATE_SKILL_BUILD'),
  'activation_command_count', (SELECT count(*) FROM commands WHERE tenant_id='tenant_yaya' AND command_type='ACTIVATE_SKILL_VERSION'),
  'turn_command_count', (SELECT count(*) FROM commands WHERE tenant_id='tenant_yaya' AND command_type='EXECUTE_AGENT_TURN'),
  'registry_revision', COALESCE((SELECT max(revision) FROM registry_entries WHERE tenant_id='tenant_yaya'), 0),
  'failure_run_count', (SELECT count(*) FROM game_runs WHERE tenant_id='tenant_yaya' AND run_json->>'status'='REJECTED' AND run_json->'sandbox'->>'status'='SUCCEEDED' AND jsonb_array_length(run_json->'sandbox'->'action_intents')=7 AND run_json->'world_application'->>'status'='REJECTED' AND run_json->'world_application'->'receipt'='null'::jsonb AND run_json->'world_application'->'failure'->>'code'='WORLD_RULE_REJECTED' AND run_json->'world_application'->'failure'->'details'->>'reason'='TASK_INCOMPLETE'),
  'successful_run_count', (SELECT count(*) FROM game_runs WHERE tenant_id='tenant_yaya' AND run_json->>'status'='SUCCEEDED' AND run_json->'sandbox'->>'status'='SUCCEEDED' AND run_json->'world_application'->>'status'='COMMITTED'),
  'same_failure_key_count', (SELECT count(*) FROM job_step_receipts r JOIN workflow_jobs w ON w.tenant_id=r.tenant_id AND w.job_id=r.job_id WHERE w.tenant_id='tenant_yaya' AND w.operation='EXECUTE_AGENT_TURN' AND r.step_name='OUTCOME_DERIVED' AND r.receipt_json->'event'->>'event_type'='run_failed' AND r.receipt_json->'event'->>'failure_key'='task_incomplete'),
  'distinct_failed_failure_keys', (SELECT count(DISTINCT r.receipt_json->'event'->>'failure_key') FROM job_step_receipts r JOIN workflow_jobs w ON w.tenant_id=r.tenant_id AND w.job_id=r.job_id WHERE w.tenant_id='tenant_yaya' AND w.operation='EXECUTE_AGENT_TURN' AND r.step_name='OUTCOME_DERIVED' AND r.receipt_json->'event'->>'event_type'='run_failed'),
  'failure_count_sequence', COALESCE((SELECT jsonb_agg((r.receipt_json->'event'->>'failure_count')::int ORDER BY t.turn_sequence)::text FROM job_step_receipts r JOIN workflow_jobs w ON w.tenant_id=r.tenant_id AND w.job_id=r.job_id JOIN agent_turns t ON t.tenant_id=w.tenant_id AND t.command_id=w.command_id WHERE w.tenant_id='tenant_yaya' AND w.operation='EXECUTE_AGENT_TURN' AND r.step_name='OUTCOME_DERIVED' AND r.receipt_json->'event'->>'event_type'='run_failed'), '[]'),
  'interaction_role_sequence', COALESCE((SELECT jsonb_agg(interaction_json->>'role' ORDER BY sequence)::text FROM product_agent_interactions WHERE tenant_id='tenant_yaya'), '[]'),
  'learner_profile_count', (SELECT count(*) FROM learner_profiles WHERE tenant_id='tenant_yaya'),
  'learner_profile_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(learner_profile_row) ORDER BY learner_profile_row.tenant_id, learner_profile_row.learner_id)::text FROM learner_profiles AS learner_profile_row WHERE learner_profile_row.tenant_id='tenant_yaya'), '[]'),
  'learner_projection_terminal_closed', (SELECT count(*) FROM learner_projection_jobs WHERE tenant_id='tenant_yaya' AND status='SUCCEEDED' AND attempt >= 1 AND fencing_token >= 1 AND lease_owner IS NULL AND lease_expires_at IS NULL AND next_attempt_at IS NULL AND result_sha256 ~ '^[a-f0-9]{64}$' AND result_json IS NOT NULL AND last_error_json IS NULL AND completed_at IS NOT NULL),
  'domain_event_count', (SELECT count(*) FROM domain_events WHERE tenant_id='tenant_yaya'),
  'non_world_event_count', (SELECT count(*) FROM domain_events WHERE tenant_id='tenant_yaya' AND stream_id NOT LIKE 'world:%'),
  'domain_event_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(event_row) ORDER BY event_row.event_id)::text FROM domain_events AS event_row WHERE event_row.tenant_id='tenant_yaya'), '[]'),
  'event_stream_count', (SELECT count(*) FROM world_streams WHERE tenant_id='tenant_yaya'),
  'non_world_event_stream_count', (SELECT count(*) FROM world_streams WHERE tenant_id='tenant_yaya' AND world_id IS NULL),
  'event_stream_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(stream_row) ORDER BY stream_row.tenant_id, stream_row.stream_id)::text FROM world_streams AS stream_row WHERE stream_row.tenant_id='tenant_yaya'), '[]'),
  'presentation_stream_count', (SELECT count(*) FROM world_presentation_streams WHERE tenant_id='tenant_yaya'),
  'presentation_event_count', (SELECT count(*) FROM world_presentation_events WHERE tenant_id='tenant_yaya'),
  'presentation_commit_count', (SELECT count(DISTINCT commit_id) FROM world_presentation_events WHERE tenant_id='tenant_yaya'),
  'presentation_last_sequence', COALESCE((SELECT max(last_sequence) FROM world_presentation_streams WHERE tenant_id='tenant_yaya'), 0),
  'presentation_gap_count', (SELECT count(*) FROM world_presentation_streams WHERE tenant_id='tenant_yaya' AND gap_world_revision IS NOT NULL),
  'presentation_stream_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(presentation_stream_row) ORDER BY presentation_stream_row.tenant_id, presentation_stream_row.stream_id)::text FROM world_presentation_streams AS presentation_stream_row WHERE presentation_stream_row.tenant_id='tenant_yaya'), '[]'),
  'presentation_event_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(presentation_event_row) ORDER BY presentation_event_row.event_id)::text FROM world_presentation_events AS presentation_event_row WHERE presentation_event_row.tenant_id='tenant_yaya'), '[]'),
  'presentation_event_id_sequence', COALESCE((SELECT jsonb_agg(event_id ORDER BY sequence)::text FROM world_presentation_events WHERE tenant_id='tenant_yaya'), '[]'),
  'relay_dispatch_count', (SELECT count(*) FROM recoverable_llm_dispatches),
  'relay_dispatch_set_material', COALESCE((
    SELECT jsonb_agg(to_jsonb(relay_material) ORDER BY relay_material.dispatch_id)::text
    FROM (
      SELECT dispatch_id, request_sha256, context_sha256, completion_sha256,
             provider, model, request_body_sha256, state, generation_count,
             dispatch_started_at, upstream_deadline_at, response_http_status,
             response_content_type, response_body_sha256, failure_code,
             failure_retryable, terminal_at, expires_at, created_at, updated_at,
             octet_length(request_body) AS request_body_length,
             octet_length(response_body) AS response_body_length
      FROM recoverable_llm_dispatches
    ) AS relay_material
  ), '[]')
) || jsonb_build_object(
  'draft_revision_count', (SELECT count(*) FROM product_skill_draft_revisions WHERE tenant_id='tenant_yaya'),
  'draft_revision_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(draft_revision_row) ORDER BY draft_revision_row.draft_revision_row_id)::text FROM product_skill_draft_revisions AS draft_revision_row WHERE draft_revision_row.tenant_id='tenant_yaya'), '[]'),
  'patch_request_count', (SELECT count(*) FROM product_skill_patch_requests WHERE tenant_id='tenant_yaya'),
  'patch_request_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(patch_request_row) ORDER BY patch_request_row.request_id)::text FROM product_skill_patch_requests AS patch_request_row WHERE patch_request_row.tenant_id='tenant_yaya'), '[]'),
  'patch_proposal_count', (SELECT count(*) FROM product_skill_patch_proposals WHERE tenant_id='tenant_yaya'),
  'patch_proposal_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(patch_proposal_row) ORDER BY patch_proposal_row.patch_id)::text FROM product_skill_patch_proposals AS patch_proposal_row WHERE patch_proposal_row.tenant_id='tenant_yaya'), '[]'),
  'patch_evidence_count', (SELECT count(*) FROM product_skill_patch_evidence AS patch_evidence_row JOIN product_skill_patch_proposals AS patch_evidence_owner ON patch_evidence_owner.patch_id=patch_evidence_row.patch_id WHERE patch_evidence_owner.tenant_id='tenant_yaya'),
  'patch_evidence_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(patch_evidence_row) ORDER BY patch_evidence_row.patch_id, patch_evidence_row.evidence_id)::text FROM product_skill_patch_evidence AS patch_evidence_row JOIN product_skill_patch_proposals AS patch_evidence_owner ON patch_evidence_owner.patch_id=patch_evidence_row.patch_id WHERE patch_evidence_owner.tenant_id='tenant_yaya'), '[]'),
  'patch_decision_count', (SELECT count(*) FROM product_skill_patch_decisions WHERE tenant_id='tenant_yaya'),
  'patch_decision_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(patch_decision_row) ORDER BY patch_decision_row.decision_id)::text FROM product_skill_patch_decisions AS patch_decision_row WHERE patch_decision_row.tenant_id='tenant_yaya'), '[]'),
  'draft_assistance_count', (SELECT count(*) FROM product_draft_revision_assistance AS draft_assistance_count_row JOIN product_skill_draft_revisions AS draft_assistance_owner ON draft_assistance_owner.draft_revision_row_id=draft_assistance_count_row.draft_revision_row_id WHERE draft_assistance_owner.tenant_id='tenant_yaya'),
  'draft_assistance_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(draft_assistance_row) ORDER BY draft_assistance_row.draft_revision_row_id)::text FROM product_draft_revision_assistance AS draft_assistance_row JOIN product_skill_draft_revisions AS draft_assistance_owner ON draft_assistance_owner.draft_revision_row_id=draft_assistance_row.draft_revision_row_id WHERE draft_assistance_owner.tenant_id='tenant_yaya'), '[]'),
  'patch_decision_receipt_count', (SELECT count(*) FROM product_patch_decision_receipts WHERE tenant_id='tenant_yaya'),
  'patch_decision_receipt_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(patch_receipt_row) ORDER BY patch_receipt_row.receipt_id)::text FROM product_patch_decision_receipts AS patch_receipt_row WHERE patch_receipt_row.tenant_id='tenant_yaya'), '[]'),
  'build_provenance_count', (SELECT count(*) FROM skill_build_provenance WHERE tenant_id='tenant_yaya'),
  'build_provenance_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(build_provenance_row) ORDER BY build_provenance_row.build_id)::text FROM skill_build_provenance AS build_provenance_row WHERE build_provenance_row.tenant_id='tenant_yaya'), '[]'),
  'certification_provenance_count', (SELECT count(*) FROM skill_certification_provenance WHERE tenant_id='tenant_yaya'),
  'certification_provenance_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(certification_provenance_row) ORDER BY certification_provenance_row.certification_id)::text FROM skill_certification_provenance AS certification_provenance_row WHERE certification_provenance_row.tenant_id='tenant_yaya'), '[]'),
  'activation_provenance_count', (SELECT count(*) FROM skill_activation_provenance WHERE tenant_id='tenant_yaya'),
  'activation_provenance_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(activation_provenance_row) ORDER BY activation_provenance_row.activation_id)::text FROM skill_activation_provenance AS activation_provenance_row WHERE activation_provenance_row.tenant_id='tenant_yaya'), '[]'),
  'run_provenance_count', (SELECT count(*) FROM skill_run_provenance WHERE tenant_id='tenant_yaya'),
  'run_provenance_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(run_provenance_row) ORDER BY run_provenance_row.run_id)::text FROM skill_run_provenance AS run_provenance_row WHERE run_provenance_row.tenant_id='tenant_yaya'), '[]'),
  'assisted_build_count', (SELECT count(*) FROM skill_build_provenance WHERE tenant_id='tenant_yaya' AND assistance_authority='SKILL_PATCH'),
  'assisted_run_count', (SELECT count(*) FROM skill_run_provenance WHERE tenant_id='tenant_yaya' AND assistance_authority='SKILL_PATCH'),
  'command_receipt_count', (SELECT count(*) FROM idempotency_receipts WHERE tenant_id='tenant_yaya'),
  'command_receipt_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(command_receipt_row) ORDER BY command_receipt_row.receipt_id)::text FROM idempotency_receipts AS command_receipt_row WHERE command_receipt_row.tenant_id='tenant_yaya'), '[]'),
  'job_step_receipt_count', (SELECT count(*) FROM job_step_receipts WHERE tenant_id='tenant_yaya'),
  'registry_entry_count', (SELECT count(*) FROM registry_entries WHERE tenant_id='tenant_yaya'),
  'world_snapshot_count', (SELECT count(*) FROM world_snapshots WHERE tenant_id='tenant_yaya'),
  'build_terminal_authority_count', (SELECT count(*) FROM skill_build_terminal_authority WHERE tenant_id='tenant_yaya'),
  'build_terminal_certified_count', (SELECT count(*) FROM skill_build_terminal_authority WHERE tenant_id='tenant_yaya' AND terminal_status='CERTIFIED'),
  'build_terminal_authority_set_material', COALESCE((SELECT jsonb_agg(to_jsonb(build_terminal_authority_row) ORDER BY build_terminal_authority_row.build_id)::text FROM skill_build_terminal_authority AS build_terminal_authority_row WHERE build_terminal_authority_row.tenant_id='tenant_yaya'), '[]')
))::text;
"@
    $databaseFingerprint = Invoke-DatabaseFingerprint $postgresName $databaseSql
    $presentationDatabaseEventIds = @(([string]$databaseFingerprint.presentation_event_id_sequence | ConvertFrom-Json))
    $expectedTurnJobAttempt = 2
    $providerDispatchCount = [int]$databaseFingerprint.provider_dispatch_receipts
    $providerResultCount = [int]$databaseFingerprint.provider_result_receipts
    $providerReceiptClosureInvalid = if ($RealProvider) {
        $providerDispatchCount -lt $expectedRelayGenerationCount -or
        $providerDispatchCount -gt $expectedRealProviderGenerationLimit -or
        $providerResultCount -lt $expectedProviderResultMinimum -or
        $providerResultCount -gt $providerDispatchCount
    }
    else {
        $providerDispatchCount -ne $expectedRelayGenerationCount -or
        $providerResultCount -ne $expectedRelayGenerationCount
    }
    if (
        [int]$databaseFingerprint.build_job_attempt -ne 1 -or
        [int]$databaseFingerprint.build_job_fencing_token -ne 1 -or
        [string]$databaseFingerprint.build_job_status -ne 'SUCCEEDED' -or
        [int]$databaseFingerprint.turn_job_attempt -lt $expectedTurnJobAttempt -or
        [int]$databaseFingerprint.turn_worker_reconcile_receipts -lt 1 -or
        [int]$databaseFingerprint.turn_worker_failure_receipts -ne 0 -or
        [string]$databaseFingerprint.turn_job_status -ne 'SUCCEEDED' -or
        $providerReceiptClosureInvalid -or
        [int]$databaseFingerprint.sandbox_dispatch_receipts -ne $expectedRunCount -or
        [int]$databaseFingerprint.sandbox_result_receipts -ne $expectedRunCount -or
        [int]$databaseFingerprint.build_certification_receipts -ne 2 -or
        [int]$databaseFingerprint.session_count -ne 1 -or
        [int]$databaseFingerprint.workspace_count -ne 1 -or
        [int]$databaseFingerprint.draft_count -ne 1 -or
        [int]$databaseFingerprint.draft_revision_count -ne 3 -or
        [int]$databaseFingerprint.patch_request_count -ne $expectedPatchAuthorityCount -or
        [int]$databaseFingerprint.patch_proposal_count -ne $expectedPatchAuthorityCount -or
        [int]$databaseFingerprint.patch_evidence_count -ne $expectedPatchAuthorityCount -or
        [int]$databaseFingerprint.patch_decision_count -ne $expectedPatchAuthorityCount -or
        [int]$databaseFingerprint.draft_assistance_count -ne $expectedPatchAuthorityCount -or
        [int]$databaseFingerprint.patch_decision_receipt_count -ne $expectedPatchAuthorityCount -or
        [int]$databaseFingerprint.build_count -ne 2 -or
        [int]$databaseFingerprint.build_provenance_count -ne 2 -or
        [int]$databaseFingerprint.build_terminal_authority_count -ne 2 -or
        [int]$databaseFingerprint.build_terminal_certified_count -ne 2 -or
        [int]$databaseFingerprint.artifact_count -ne 2 -or
        [int]$databaseFingerprint.certification_count -ne 2 -or
        [int]$databaseFingerprint.certification_provenance_count -ne 2 -or
        [int]$databaseFingerprint.activation_count -ne 2 -or
        [int]$databaseFingerprint.activation_provenance_count -ne 2 -or
        [int]$databaseFingerprint.registry_revision -ne 2 -or
        [int]$databaseFingerprint.registry_entry_count -ne 2 -or
        [int]$databaseFingerprint.turn_count -ne $expectedTurnCount -or
        [int]$databaseFingerprint.run_count -ne $expectedRunCount -or
        [int]$databaseFingerprint.run_provenance_count -ne $expectedRunCount -or
        [int]$databaseFingerprint.assisted_build_count -ne $expectedAssistedAuthorityCount -or
        [int]$databaseFingerprint.assisted_run_count -ne $expectedAssistedAuthorityCount -or
        [int]$databaseFingerprint.failure_run_count -ne $expectedFailureRunCount -or
        [int]$databaseFingerprint.successful_run_count -ne 1 -or
        [int]$databaseFingerprint.same_failure_key_count -ne $expectedFailureRunCount -or
        [int]$databaseFingerprint.distinct_failed_failure_keys -ne 1 -or
        [string]$databaseFingerprint.failure_count_sequence -ne $expectedFailureCountSequence -or
        [string]$databaseFingerprint.interaction_role_sequence -ne $expectedInteractionRoleSequence -or
        [int]$databaseFingerprint.world_snapshot_count -ne 1 -or
        [int]$databaseFingerprint.world_event_count -ne $expectedWorldCommitEventCount -or
        [int]$databaseFingerprint.evidence_count -ne $expectedEvidenceCount -or
        [int]$databaseFingerprint.interaction_count -ne $expectedTurnCount -or
        [int]$databaseFingerprint.command_count -ne $expectedCommandCount -or
        [int]$databaseFingerprint.terminal_command_count -ne $expectedCommandCount -or
        [int]$databaseFingerprint.applied_terminal_command_count -ne $expectedAppliedCommandCount -or
        [int]$databaseFingerprint.rejected_terminal_command_count -ne $expectedRejectedCommandCount -or
        [int]$databaseFingerprint.session_command_count -ne $expectedSessionCommandCount -or
        [int]$databaseFingerprint.build_command_count -ne $expectedBuildCommandCount -or
        [int]$databaseFingerprint.activation_command_count -ne $expectedActivationCommandCount -or
        [int]$databaseFingerprint.turn_command_count -ne $expectedTurnCount -or
        [int]$databaseFingerprint.command_receipt_count -ne $expectedCommandCount -or
        [int]$databaseFingerprint.product_receipt_count -ne $expectedProductReceiptCount -or
        [int]$databaseFingerprint.job_step_receipt_count -le 0 -or
        [int]$databaseFingerprint.learner_profile_count -ne 1 -or
        [int]$databaseFingerprint.learner_projection_count -ne $expectedLearnerCount -or
        [int]$databaseFingerprint.learner_projection_succeeded -ne $expectedLearnerCount -or
        [int]$databaseFingerprint.learner_projection_terminal_closed -ne $expectedLearnerCount -or
        [int]$databaseFingerprint.learner_revision -ne $expectedLearnerCount -or
        [string]$databaseFingerprint.learner_profile_sha256 -notmatch '^[a-f0-9]{64}$' -or
        [int]$databaseFingerprint.non_world_event_count -lt 2 -or
        [int]$databaseFingerprint.non_world_event_stream_count -lt 2 -or
        [int]$databaseFingerprint.presentation_stream_count -ne 1 -or
        [int]$databaseFingerprint.presentation_event_count -ne $expectedWorldPresentationEventCount -or
        [int]$databaseFingerprint.presentation_commit_count -ne 1 -or
        [int]$databaseFingerprint.presentation_last_sequence -ne $expectedWorldPresentationEventCount -or
        [int]$databaseFingerprint.presentation_gap_count -ne 0 -or
        $presentationDatabaseEventIds.Count -ne $expectedWorldPresentationEventCount -or
        (@($presentationDatabaseEventIds) -join ',') -cne (@($godotFingerprint.world_presentation.event_ids_started) -join ',') -or
        [int]$godotFingerprint.world_presentation.presentation_high_watermark -ne [int]$databaseFingerprint.presentation_last_sequence
    ) {
        throw 'Database receipts do not prove the exact selected M1/M2 A8 failure/fix chain and single logical side effects.'
    }
    if (
        [int]$relayStats.unique_dispatches -ne $providerDispatchCount -or
        [int]$relayStats.total_generations -ne [int]$relayStats.unique_dispatches
    ) {
        throw 'Relay generations and durable Provider dispatch/result receipts are not closed.'
    }
    if (
        ($RealProvider -and [int]$databaseFingerprint.relay_dispatch_count -ne [int]$relayStats.unique_dispatches) -or
        ((-not $RealProvider) -and [int]$databaseFingerprint.relay_dispatch_count -ne 0)
    ) {
        throw 'Recoverable relay table authority differs from the selected real/fixture Provider topology.'
    }
    $phase1DatabaseJson = ConvertTo-StableJson $databaseFingerprint
    $m2FullRowAuthoritySha256 = Get-Sha256 $phase1DatabaseJson

    $sandboxFingerprint = Get-DirectoryFingerprint (Join-Path $runtimeRoot 'sandbox-results') 'Sandbox result'
    $artifactFingerprint = Get-DirectoryFingerprint (Join-Path $runtimeRoot 'artifacts') 'Artifact'
    if ([int]$sandboxFingerprint.receipt_count -ne $expectedSandboxReceiptCount) {
        throw 'Persistent Sandbox result root does not contain the exact selected M1/M2 launch/result receipt pairs.'
    }
    if ([int]$artifactFingerprint.receipt_count -ne 2) {
        throw 'Persistent Artifact root does not contain exactly the failure and corrected artifacts.'
    }
    $sideEffectAuthority = [ordered]@{
        session_id = [string]$godotFingerprint.session_id
        activation_id = [string]$godotFingerprint.activation_id
        activation_ids = @($godotFingerprint.activation_ids)
        build_ids = @($godotFingerprint.build_ids)
        turn_ids = @($godotFingerprint.turn_ids)
        command_ids = @($godotFingerprint.command_ids)
        command_statuses = @($godotFingerprint.command_statuses)
        run_id = [string]$godotFingerprint.run_id
        run_ids = @($godotFingerprint.run_ids)
        run_statuses = @($godotFingerprint.run_statuses)
        interaction_id = [string]$godotFingerprint.interaction_id
        interaction_ids = @($godotFingerprint.interaction_ids)
        interaction_roles = @($godotFingerprint.interaction_roles)
        failure_reason = [string]$godotFingerprint.failure_reason
        world_revision = [int]$worldSnapshot.revision
        last_event_sequence = [int]$worldSnapshot.last_event_sequence
        world_state_hash = [string]$worldSnapshot.state_hash
        presentation_high_watermark = [int]$godotFingerprint.world_presentation.presentation_high_watermark
        presentation_event_ids = @($godotFingerprint.world_presentation.event_ids_started)
        presentation_playback_started = [int]$godotFingerprint.world_presentation.playback_started
        presentation_playback_finished = [int]$godotFingerprint.world_presentation.playback_finished
        presentation_playing_observed = [bool]$godotFingerprint.world_presentation.playing_observed
        presentation_stream_count = [int]$databaseFingerprint.presentation_stream_count
        presentation_event_count = [int]$databaseFingerprint.presentation_event_count
        draft_revision = [int]$godotFingerprint.saved_draft_revision
        draft_sha256 = [string]$godotFingerprint.draft_sha256
        workspace_revision = [int]$godotFingerprint.final_workspace_revision
        workspace_sha256 = [string]$godotFingerprint.final_workspace_sha256
        build_source_sha256 = [string]$godotFingerprint.build_source_sha256
        sandbox_receipt_count = [int]$sandboxFingerprint.receipt_count
        sandbox_receipt_set_sha256 = [string]$sandboxFingerprint.entries_sha256
        artifact_file_count = [int]$artifactFingerprint.receipt_count
        artifact_file_set_sha256 = [string]$artifactFingerprint.entries_sha256
        relay_unique_dispatches = [int]$relayStats.unique_dispatches
        relay_total_generations = [int]$relayStats.total_generations
        build_job_attempt = [int]$databaseFingerprint.build_job_attempt
        build_job_fencing_token = [int]$databaseFingerprint.build_job_fencing_token
        provider_dispatch_count = [int]$databaseFingerprint.provider_dispatch_receipts
        provider_result_count = [int]$databaseFingerprint.provider_result_receipts
        build_count = [int]$databaseFingerprint.build_count
        draft_revision_count = [int]$databaseFingerprint.draft_revision_count
        patch_request_count = [int]$databaseFingerprint.patch_request_count
        patch_proposal_count = [int]$databaseFingerprint.patch_proposal_count
        patch_evidence_count = [int]$databaseFingerprint.patch_evidence_count
        patch_decision_count = [int]$databaseFingerprint.patch_decision_count
        draft_assistance_count = [int]$databaseFingerprint.draft_assistance_count
        patch_decision_receipt_count = [int]$databaseFingerprint.patch_decision_receipt_count
        build_provenance_count = [int]$databaseFingerprint.build_provenance_count
        build_terminal_authority_count = [int]$databaseFingerprint.build_terminal_authority_count
        build_terminal_certified_count = [int]$databaseFingerprint.build_terminal_certified_count
        certification_count = [int]$databaseFingerprint.certification_count
        certification_provenance_count = [int]$databaseFingerprint.certification_provenance_count
        activation_count = [int]$databaseFingerprint.activation_count
        activation_provenance_count = [int]$databaseFingerprint.activation_provenance_count
        registry_revision = [int]$databaseFingerprint.registry_revision
        turn_count = [int]$databaseFingerprint.turn_count
        run_count = [int]$databaseFingerprint.run_count
        run_provenance_count = [int]$databaseFingerprint.run_provenance_count
        assisted_build_count = [int]$databaseFingerprint.assisted_build_count
        assisted_run_count = [int]$databaseFingerprint.assisted_run_count
        failure_run_count = [int]$databaseFingerprint.failure_run_count
        successful_run_count = [int]$databaseFingerprint.successful_run_count
        failure_count_sequence = [string]$databaseFingerprint.failure_count_sequence
        interaction_role_sequence = [string]$databaseFingerprint.interaction_role_sequence
        evidence_count = [int]$databaseFingerprint.evidence_count
        interaction_count = [int]$databaseFingerprint.interaction_count
        command_count = [int]$databaseFingerprint.command_count
        terminal_command_count = [int]$databaseFingerprint.terminal_command_count
        applied_terminal_command_count = [int]$databaseFingerprint.applied_terminal_command_count
        rejected_terminal_command_count = [int]$databaseFingerprint.rejected_terminal_command_count
        session_command_count = [int]$databaseFingerprint.session_command_count
        build_command_count = [int]$databaseFingerprint.build_command_count
        activation_command_count = [int]$databaseFingerprint.activation_command_count
        turn_command_count = [int]$databaseFingerprint.turn_command_count
        command_receipt_count = [int]$databaseFingerprint.command_receipt_count
        non_world_event_count = [int]$databaseFingerprint.non_world_event_count
        non_world_event_stream_count = [int]$databaseFingerprint.non_world_event_stream_count
        relay_dispatch_count = [int]$databaseFingerprint.relay_dispatch_count
        learner_profile_count = [int]$databaseFingerprint.learner_profile_count
        learner_projection_count = [int]$databaseFingerprint.learner_projection_count
        learner_revision = [int]$databaseFingerprint.learner_revision
        learner_profile_sha256 = [string]$databaseFingerprint.learner_profile_sha256
        job_step_receipt_count = [int]$databaseFingerprint.job_step_receipt_count
        product_receipt_count = [int]$databaseFingerprint.product_receipt_count
        registry_entry_count = [int]$databaseFingerprint.registry_entry_count
        world_snapshot_count = [int]$databaseFingerprint.world_snapshot_count
        m2_full_row_authority_sha256 = $m2FullRowAuthoritySha256
    }
    $sideEffectJson = $sideEffectAuthority | ConvertTo-Json -Compress -Depth 8

    # The first process group is now fully terminal.  Capture every durable
    # authority before crossing the explicit process boundary; the second
    # process group is allowed to read/reconcile only and must leave all of
    # these bytes and counts unchanged.
    $relaySideEffectFingerprint = Get-RelaySideEffectFingerprint $relayStats $RealProvider
    $phase1RelayCapabilityGets = if ($RealProvider) { $null } else { [int]$relayStats.capability_gets }
    $phase1RelayJson = ConvertTo-StableJson $relaySideEffectFingerprint
    $phase1FaultProxyJson = if ($RealProvider) { ConvertTo-StableJson $relayFaultStats } else { $null }
    $phase1SandboxJson = ConvertTo-StableJson $sandboxFingerprint
    $phase1ArtifactJson = ConvertTo-StableJson $artifactFingerprint

    # The database stop/start gate is deterministic-only.  It runs after all
    # business Jobs and learner projections are terminal, while the original
    # Gateway and both polling workers are still alive.  The unique named
    # volume (rather than tmpfs) is required so a real container stop/start
    # retains exactly the phase-1 PostgreSQL bytes.
    $databaseOutageRecovery = $null
    if (-not $RealProvider) {
        if ($gatewayProcess.HasExited -or $workerProcess.HasExited -or $learnerProcess.HasExited) {
            throw 'Gateway, workflow worker, or learner worker exited before the PostgreSQL outage gate.'
        }
        # Docker's stable CLI spelling is --timeout.  Avoid the historical
        # --time alias: current Docker releases reject it before naming the
        # stopped container, which would turn this recovery proof into a
        # harness-only failure.
        $postgresAuthorityBeforeStop = Get-DockerResourceAuthority 'container' $postgresId
        if ($postgresAuthorityBeforeStop -cne "$postgresId|$runId|$postgresResourceOwner") {
            throw 'Refusing to stop a PostgreSQL container without exact captured ownership.'
        }
        $stopPostgres = Invoke-DockerNativeCapture `
            'docker.exe' `
            @('stop', '--timeout', '5', $postgresId) `
            'stop-owned-postgres-for-outage'
        $stoppedPostgresId = ([string]$stopPostgres.stdout).Trim()
        if (
            [int]$stopPostgres.exit_code -ne 0 -or
            $stoppedPostgresId -cne $postgresId -or
            -not [string]::IsNullOrWhiteSpace([string]$stopPostgres.stderr)
        ) {
            throw 'Failed to stop the exact disposable PostgreSQL container.'
        }
        Wait-LocalPortClosed $postgresPort 15
        $stoppedPostgresState = (& docker inspect --format '{{.State.Status}}' $postgresId 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $stoppedPostgresState -cne 'exited') {
            throw 'Disposable PostgreSQL did not reach the exact exited state.'
        }
        $outageListener = Assert-SingleGatewayListener $gatewayPort $phase1GatewayPid 'postgres-outage'
        $outageStopwatch = [Diagnostics.Stopwatch]::StartNew()
        while ($outageStopwatch.ElapsedMilliseconds -lt 1000) {
            if ($gatewayProcess.HasExited -or $workerProcess.HasExited -or $learnerProcess.HasExited) {
                throw 'Gateway, workflow worker, or learner worker exited while PostgreSQL was unavailable.'
            }
            Start-Sleep -Milliseconds 100
        }

        $outageSnapshotHeaders = $snapshotHeaders.Clone()
        $outageSnapshotHeaders['X-Request-Id'] = "req_int1_db_outage_$($runId.Substring(0, 16))"
        $outageSnapshotHeaders['X-Trace-Id'] = "trace_int1_db_outage_$($runId.Substring(0, 16))"
        $outageSnapshotHeaders['X-Correlation-Id'] = "corr_int1_db_outage_$($runId.Substring(0, 16))"
        $outageReadUnexpectedlySucceeded = $false
        $outageReadFailureType = $null
        $outageReadHttpStatus = $null
        try {
            Invoke-RestMethod `
                -Uri "http://127.0.0.1:$gatewayPort/v1/worlds/$worldId/snapshot" `
                -Method Get `
                -Headers $outageSnapshotHeaders `
                -TimeoutSec 5 | Out-Null
            $outageReadUnexpectedlySucceeded = $true
        }
        catch {
            $outageReadFailureType = $_.Exception.GetType().FullName
            $responseProperty = $_.Exception.PSObject.Properties['Response']
            if ($null -ne $responseProperty -and $null -ne $responseProperty.Value) {
                $statusProperty = $responseProperty.Value.PSObject.Properties['StatusCode']
                if ($null -ne $statusProperty -and $null -ne $statusProperty.Value) {
                    $outageReadHttpStatus = [int]$statusProperty.Value
                }
            }
        }
        if ($outageReadUnexpectedlySucceeded) {
            throw 'A database-backed Gateway GET unexpectedly succeeded while PostgreSQL was stopped.'
        }
        if ($gatewayProcess.HasExited -or $workerProcess.HasExited -or $learnerProcess.HasExited) {
            throw 'Gateway, workflow worker, or learner worker exited after observing the PostgreSQL outage.'
        }
        $outageUnavailableMilliseconds = [long]$outageStopwatch.ElapsedMilliseconds
        $outageStopwatch.Stop()
        Write-Output 'INT1_LOCAL_DIAGNOSTIC_MILESTONE postgres-outage-observed'

        $postgresAuthorityBeforeStart = Get-DockerResourceAuthority 'container' $postgresId
        if ($postgresAuthorityBeforeStart -cne "$postgresId|$runId|$postgresResourceOwner") {
            throw 'Refusing to start a PostgreSQL container without exact captured ownership.'
        }
        $startPostgres = Invoke-DockerNativeCapture `
            'docker.exe' `
            @('start', $postgresId) `
            'start-owned-postgres-after-outage'
        $startedPostgresId = ([string]$startPostgres.stdout).Trim()
        if (
            [int]$startPostgres.exit_code -ne 0 -or
            $startedPostgresId -cne $postgresId -or
            -not [string]::IsNullOrWhiteSpace([string]$startPostgres.stderr)
        ) {
            throw 'Failed to restart the same disposable PostgreSQL container.'
        }
        Wait-PostgresHealthy $postgresId 45
        Wait-LocalPort $postgresPort 15
        $postgresIdAfterRestart = (& docker inspect --format '{{.Id}}' $postgresId 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $postgresIdAfterRestart -cne $postgresId) {
            throw 'PostgreSQL outage recovery did not restart the same container identity.'
        }

        $recoverySnapshotHeaders = $snapshotHeaders.Clone()
        $recoverySnapshotHeaders['X-Request-Id'] = "req_int1_db_recovery_$($runId.Substring(0, 16))"
        $recoverySnapshotHeaders['X-Trace-Id'] = "trace_int1_db_recovery_$($runId.Substring(0, 16))"
        $recoverySnapshotHeaders['X-Correlation-Id'] = "corr_int1_db_recovery_$($runId.Substring(0, 16))"
        $recoveredWorldSnapshot = $null
        $gatewayRecoveryDeadline = [DateTime]::UtcNow.AddSeconds(30)
        while ([DateTime]::UtcNow -lt $gatewayRecoveryDeadline) {
            if ($gatewayProcess.HasExited -or $workerProcess.HasExited -or $learnerProcess.HasExited) {
                throw 'Gateway, workflow worker, or learner worker exited during PostgreSQL recovery.'
            }
            try {
                $recoveredWorldSnapshot = Invoke-RestMethod `
                    -Uri "http://127.0.0.1:$gatewayPort/v1/worlds/$worldId/snapshot" `
                    -Method Get `
                    -Headers $recoverySnapshotHeaders `
                    -TimeoutSec 5
                break
            }
            catch {
                Start-Sleep -Milliseconds 250
            }
        }
        if ($null -eq $recoveredWorldSnapshot) {
            throw 'Gateway did not recover its database-backed GET after PostgreSQL restarted.'
        }
        $worldSnapshotJson = ConvertTo-StableJson $worldSnapshot
        $recoveredWorldSnapshotJson = ConvertTo-StableJson $recoveredWorldSnapshot
        if ($recoveredWorldSnapshotJson -cne $worldSnapshotJson) {
            throw 'Gateway GET after PostgreSQL recovery changed the terminal World snapshot.'
        }

        $serviceConnectionEvidence = Wait-ServicePostgresConnections `
            $postgresPort `
            ([ordered]@{
                gateway = $phase1GatewayPid
                workflow_worker = $phase1WorkerPid
                learner_worker = $phase1LearnerWorkerPid
            }) `
            30
        $relayStatsAfterDatabaseRecovery = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$relayPort/__int1_diagnostic__/stats" `
            -Method Get `
            -Headers $relayHeaders `
            -TimeoutSec 10
        $databaseFingerprintAfterOutage = Invoke-DatabaseFingerprint $postgresName $databaseSql
        $sandboxFingerprintAfterOutage = Get-DirectoryFingerprint (Join-Path $runtimeRoot 'sandbox-results') 'Sandbox result'
        $artifactFingerprintAfterOutage = Get-DirectoryFingerprint (Join-Path $runtimeRoot 'artifacts') 'Artifact'
        $relayFingerprintAfterOutage = Get-RelaySideEffectFingerprint $relayStatsAfterDatabaseRecovery $false
        $databaseOutageFingerprintComparison = [ordered]@{
            relay = New-RestartFingerprintComparison `
                $phase1RelayJson `
                (ConvertTo-StableJson $relayFingerprintAfterOutage)
            database = New-RestartFingerprintComparison `
                $phase1DatabaseJson `
                (ConvertTo-StableJson $databaseFingerprintAfterOutage)
            sandbox = New-RestartFingerprintComparison `
                $phase1SandboxJson `
                (ConvertTo-StableJson $sandboxFingerprintAfterOutage)
            artifact = New-RestartFingerprintComparison `
                $phase1ArtifactJson `
                (ConvertTo-StableJson $artifactFingerprintAfterOutage)
        }
        $databaseOutageSideEffectsUnchanged = (
            [int]$relayStatsAfterDatabaseRecovery.capability_gets -eq [int]$phase1RelayCapabilityGets -and
            $databaseOutageFingerprintComparison.relay.unchanged -eq $true -and
            $databaseOutageFingerprintComparison.database.unchanged -eq $true -and
            $databaseOutageFingerprintComparison.sandbox.unchanged -eq $true -and
            $databaseOutageFingerprintComparison.artifact.unchanged -eq $true
        )
        $databaseOutageRecovery = [ordered]@{
            classification = 'DETERMINISTIC_POSTGRES_STOP_START_RECOVERY'
            same_container_id = $postgresIdAfterRestart -ceq $postgresId
            published_port_closed_during_outage = $true
            unavailable_milliseconds = $outageUnavailableMilliseconds
            outage_read_failed = -not $outageReadUnexpectedlySucceeded
            outage_read_failure_type = $outageReadFailureType
            outage_read_http_status = $outageReadHttpStatus
            gateway_listener_during_outage = $outageListener
            service_database_connections_after_restart = $serviceConnectionEvidence
            recovered_world_snapshot_sha256 = Get-Sha256 $recoveredWorldSnapshotJson
            fingerprints = $databaseOutageFingerprintComparison
            side_effects_unchanged = $databaseOutageSideEffectsUnchanged
        }
        Write-Output (
            'INT1_LOCAL_DIAGNOSTIC_DATABASE_OUTAGE_FINGERPRINT ' +
            ($databaseOutageRecovery | ConvertTo-Json -Compress -Depth 8)
        )
        if (-not $databaseOutageSideEffectsUnchanged) {
            throw 'PostgreSQL stop/start recovery changed relay, database, Sandbox, or Artifact side effects.'
        }
        if ($gatewayProcess.HasExited -or $workerProcess.HasExited -or $learnerProcess.HasExited) {
            throw 'Gateway, workflow worker, or learner worker exited after PostgreSQL recovery verification.'
        }
        Assert-SingleGatewayListener $gatewayPort $phase1GatewayPid 'postgres-recovered' | Out-Null
        Write-Output 'INT1_LOCAL_DIAGNOSTIC_MILESTONE postgres-gateway-workflow-learner-recovered'
    }

    Stop-TestProcess $gatewayProcess 'phase-1 Gateway'
    Stop-TestProcess $workerProcess 'phase-1 workflow worker'
    Stop-TestProcess $learnerProcess 'phase-1 learner worker'
    Wait-LocalPortClosed $gatewayPort 15
    if (
        @(Get-ListeningProcessIds $gatewayPort).Count -ne 0 -or
        @(Get-BackendGatewayProcessIds).Count -ne 0
    ) {
        throw 'Gateway restart boundary retained a listener or Backend Gateway process.'
    }
    Write-Output 'INT1_LOCAL_DIAGNOSTIC_MILESTONE phase1-processes-stopped'

    $phase2WorkerId = "int1-local-phase2-$($runId.Substring(0, 12))"
    $phase2LearnerWorkerId = "int1-learner-phase2-$($runId.Substring(0, 12))"
    if ($phase2WorkerId -eq $phase1WorkerId -or $phase2LearnerWorkerId -eq $phase1LearnerWorkerId) {
        throw 'Restarted workers must use new durable worker identities.'
    }
    $env:WALNUT_WORKER_ID = $phase2WorkerId
    $env:WALNUT_LEARNER_WORKER_ID = $phase2LearnerWorkerId
    $phase2GatewayStdout = Join-Path $runRoot 'gateway-restarted.stdout.log'
    $gatewayRestarted = Start-Process -FilePath $backendPython -ArgumentList @('-m', 'uvicorn', 'walnut_backend.main:app', '--host', '127.0.0.1', '--port', [string]$gatewayPort) -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $phase2GatewayStdout -RedirectStandardError (Join-Path $runRoot 'gateway-restarted.stderr.log')
    $startedProcesses.Add($gatewayRestarted)
    $workerRestarted = Start-Process -FilePath $backendPython -ArgumentList @('-m', 'walnut_backend.worker_main') -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runRoot 'worker-restarted.stdout.log') -RedirectStandardError (Join-Path $runRoot 'worker-restarted.stderr.log')
    $startedProcesses.Add($workerRestarted)
    $learnerRestarted = Start-Process -FilePath $backendPython -ArgumentList @('-m', 'walnut_backend.learner_worker_main') -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runRoot 'learner-worker-restarted.stdout.log') -RedirectStandardError (Join-Path $runRoot 'learner-worker-restarted.stderr.log')
    $startedProcesses.Add($learnerRestarted)
    Wait-LocalPort $gatewayPort 45
    if ($gatewayRestarted.HasExited -or $workerRestarted.HasExited -or $learnerRestarted.HasExited) {
        throw 'Restarted Gateway, workflow worker, or learner worker exited during startup.'
    }
    if (
        [int]$gatewayRestarted.Id -eq $phase1GatewayPid -or
        [int]$workerRestarted.Id -eq $phase1WorkerPid -or
        [int]$learnerRestarted.Id -eq $phase1LearnerWorkerPid
    ) {
        throw 'The restart boundary did not produce three new OS process identities.'
    }
    $phase2Listener = Assert-SingleGatewayListener $gatewayPort ([int]$gatewayRestarted.Id) 'phase2'
    Write-Output 'INT1_LOCAL_DIAGNOSTIC_MILESTONE gateway-workflow-learner-restarted'

    $phase2GodotStdout = Join-Path $runRoot 'godot-phase2.stdout.log'
    $phase2GodotStderr = Join-Path $runRoot 'godot-phase2.stderr.log'
    $phase2FrontendArguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $frontendRunner,
        '-GodotExe', $GodotExe,
        '-TotalDeadlineSeconds', [string]$TotalDeadlineSeconds,
        '-ResourceDeadlineSeconds', '180',
        '-InteractionDeadlineSeconds', '90',
        '-Phase1FingerprintPath', $phase1GodotFingerprintPath,
        '-RecoveryOnly', '-CleanupPersistence'
    )
    if ($EnableWorldPresentation) {
        $phase2FrontendArguments += '-EnableWorldPresentation'
    }
    if ($EnableSkillPatch) {
        $phase2FrontendArguments += '-EnableSkillPatch'
    }
    $recoveryProcess = Start-Process -FilePath 'powershell.exe' -ArgumentList $phase2FrontendArguments `
        -WorkingDirectory $frontendRoot -WindowStyle Hidden -Wait -PassThru `
        -RedirectStandardOutput $phase2GodotStdout -RedirectStandardError $phase2GodotStderr
    $recoveryExitCode = $recoveryProcess.ExitCode
    $recoveryLines = @(
        @(Get-Content -LiteralPath $phase2GodotStdout -ErrorAction SilentlyContinue)
        @(Get-Content -LiteralPath $phase2GodotStderr -ErrorAction SilentlyContinue)
    ) | ForEach-Object { [string]$_ }
    $recoveryLines | ForEach-Object { Write-Output $_ }
    if ($recoveryExitCode -ne 0) {
        throw "Recovery-only Godot process failed with exit code $recoveryExitCode."
    }
    $recoveryPrefix = 'REAL_GATEWAY_CHAIN_RECOVERY_PASS '
    $recoveryPassLines = @($recoveryLines | Where-Object { $_.StartsWith($recoveryPrefix, [StringComparison]::Ordinal) })
    if ($recoveryPassLines.Count -ne 1) {
        throw 'Recovery-only Godot process emitted no unique structured PASS fingerprint.'
    }
    $recoveryFingerprint = $recoveryPassLines[0].Substring($recoveryPrefix.Length) | ConvertFrom-Json
    if (
        $recoveryFingerprint.recovery_only -ne $true -or
        $recoveryFingerprint.persisted_store_loaded -ne $true -or
        $recoveryFingerprint.persistence_cleanup_performed -ne $true -or
        [string]$recoveryFingerprint.persistence_identity -ne [string]$godotFingerprint.persistence_identity
    ) {
        throw 'Recovery-only Godot process did not prove its read-only persisted-state lifecycle.'
    }
    if ($EnableSkillPatch) {
        $phase1SkillPatchJson = ConvertTo-StableJson $godotFingerprint.skill_patch
        $phase2SkillPatchJson = ConvertTo-StableJson $recoveryFingerprint.skill_patch
        if (
            $recoveryFingerprint.phase1_skill_patch_exact_match -ne $true -or
            $recoveryFingerprint.skill_patch.enabled -ne $true -or
            [string]$recoveryFingerprint.skill_patch.status -ne 'PUBLIC_UI_CHAIN_CLOSED' -or
            $phase2SkillPatchJson -cne $phase1SkillPatchJson
        ) {
            throw 'Recovery-only Godot process changed the exact formal M2 Skill Patch public authority.'
        }
    }
    if (
        [string]$recoveryFingerprint.session_id -ne [string]$godotFingerprint.session_id -or
        [string]$recoveryFingerprint.workspace_id -ne [string]$godotFingerprint.workspace_id -or
        [int]$recoveryFingerprint.workspace_revision -ne [int]$godotFingerprint.final_workspace_revision -or
        [string]$recoveryFingerprint.workspace_sha256 -ne [string]$godotFingerprint.final_workspace_sha256 -or
        [string]$recoveryFingerprint.draft_id -ne [string]$godotFingerprint.draft_id -or
        [int]$recoveryFingerprint.draft_revision -ne [int]$godotFingerprint.saved_draft_revision -or
        [string]$recoveryFingerprint.draft_sha256 -ne [string]$godotFingerprint.draft_sha256 -or
        [string]$recoveryFingerprint.draft_source_sha256 -ne [string]$godotFingerprint.draft_source_sha256 -or
        [string]$recoveryFingerprint.activation_id -ne [string]$godotFingerprint.activation_id -or
        [string]$recoveryFingerprint.active_skill_tuple_sha256 -ne [string]$godotFingerprint.active_skill_tuple_sha256 -or
        [string]$recoveryFingerprint.turn_id -ne [string]$godotFingerprint.turn_id -or
        [string]$recoveryFingerprint.command_id -ne [string]$godotFingerprint.command_id -or
        [string]$recoveryFingerprint.run_id -ne [string]$godotFingerprint.run_id -or
        [string]$recoveryFingerprint.world_id -ne [string]$godotFingerprint.world_id -or
        [int]$recoveryFingerprint.world_revision -ne [int]$godotFingerprint.world_revision -or
        [int]$recoveryFingerprint.last_event_sequence -ne [int]$godotFingerprint.last_event_sequence -or
        [string]$recoveryFingerprint.world_state_hash -ne [string]$godotFingerprint.world_state_hash -or
        $recoveryFingerprint.world_presentation.enabled -ne $true -or
        $recoveryFingerprint.world_presentation.recovered_by_snapshot -ne $true -or
        [int]$recoveryFingerprint.world_presentation.presentation_high_watermark -ne [int]$godotFingerprint.world_presentation.presentation_high_watermark -or
        [string]$recoveryFingerprint.interaction_id -ne [string]$godotFingerprint.interaction_id -or
        [int]$recoveryFingerprint.interaction_sequence -ne [int]$godotFingerprint.interaction_sequence -or
        [int]$recoveryFingerprint.interaction_revision -ne [int]$godotFingerprint.interaction_revision -or
        [string]$recoveryFingerprint.interaction_role -ne [string]$godotFingerprint.interaction_role -or
        [string]$recoveryFingerprint.interaction_feedback_sha256 -ne [string]$godotFingerprint.interaction_feedback_sha256 -or
        $recoveryFingerprint.ui_display.task_workspace -ne $true -or
        $recoveryFingerprint.ui_display.dialogue_panel -ne $true -or
        $recoveryFingerprint.ui_display.world_viewport -ne $true
    ) {
        throw 'Recovery-only Godot authority differs from the exact phase-1 Session/Workspace/Draft/Activation/Run/World/Interaction fingerprint.'
    }

    $phase2HttpAudit = Get-GatewayRequestAudit $phase2GatewayStdout 5

    if ($RealProvider) {
        $relayStatsAfterRestart = Invoke-RestMethod -Uri "http://127.0.0.1:$privateRelayPort/__private__/llm-relay/statistics" -Method Get -Headers $relayHeaders -TimeoutSec 10
        $relayFaultStatsAfterRestart = Invoke-RestMethod -Uri "http://127.0.0.1:$relayPort/__int1_real_provider_fault_proxy__/statistics" -Method Get -Headers $relayHeaders -TimeoutSec 10
    }
    else {
        $relayStatsAfterRestart = Invoke-RestMethod -Uri "http://127.0.0.1:$relayPort/__int1_diagnostic__/stats" -Method Get -Headers $relayHeaders -TimeoutSec 10
    }
    $databaseFingerprintAfterRestart = Invoke-DatabaseFingerprint $postgresName $databaseSql
    $sandboxFingerprintAfterRestart = Get-DirectoryFingerprint (Join-Path $runtimeRoot 'sandbox-results') 'Sandbox result'
    $artifactFingerprintAfterRestart = Get-DirectoryFingerprint (Join-Path $runtimeRoot 'artifacts') 'Artifact'
    $relaySideEffectFingerprintAfterRestart = Get-RelaySideEffectFingerprint $relayStatsAfterRestart $RealProvider
    $phase2RelayJson = ConvertTo-StableJson $relaySideEffectFingerprintAfterRestart
    $phase2DatabaseJson = ConvertTo-StableJson $databaseFingerprintAfterRestart
    $phase2SandboxJson = ConvertTo-StableJson $sandboxFingerprintAfterRestart
    $phase2ArtifactJson = ConvertTo-StableJson $artifactFingerprintAfterRestart
    $restartFingerprintComparison = [ordered]@{
        relay = New-RestartFingerprintComparison $phase1RelayJson $phase2RelayJson
        database = New-RestartFingerprintComparison $phase1DatabaseJson $phase2DatabaseJson
        sandbox = New-RestartFingerprintComparison $phase1SandboxJson $phase2SandboxJson
        artifact = New-RestartFingerprintComparison $phase1ArtifactJson $phase2ArtifactJson
    }
    if ($RealProvider) {
        $phase2FaultProxyJson = ConvertTo-StableJson $relayFaultStatsAfterRestart
        $restartFingerprintComparison['response_loss_proxy'] = New-RestartFingerprintComparison $phase1FaultProxyJson $phase2FaultProxyJson
    }
    $relayCapabilityProbe = if ($RealProvider) { $null } else { [ordered]@{
        before = [int]$phase1RelayCapabilityGets
        after = [int]$relayStatsAfterRestart.capability_gets
        expected_delta = 1
        valid = [int]$relayStatsAfterRestart.capability_gets -eq ([int]$phase1RelayCapabilityGets + 1)
    } }
    $restartFingerprintDiagnostic = [ordered]@{
        fingerprints = $restartFingerprintComparison
        deterministic_relay_capability_probe = $relayCapabilityProbe
    }
    Write-Output ("INT1_LOCAL_DIAGNOSTIC_RESTART_FINGERPRINT " + ($restartFingerprintDiagnostic | ConvertTo-Json -Compress -Depth 6))
    if (-not $RealProvider -and $relayCapabilityProbe.valid -ne $true) {
        throw 'Restarted workflow worker did not perform exactly one additional read-only relay capability probe.'
    }
    if (
        $restartFingerprintComparison.relay.unchanged -ne $true -or
        $restartFingerprintComparison.database.unchanged -ne $true -or
        $restartFingerprintComparison.sandbox.unchanged -ne $true -or
        $restartFingerprintComparison.artifact.unchanged -ne $true -or
        ($RealProvider -and $restartFingerprintComparison.response_loss_proxy.unchanged -ne $true)
    ) {
        throw 'Gateway/worker/Godot restart changed Provider generations, database authority, Sandbox receipts, or Artifact bytes.'
    }
    if ($gatewayRestarted.HasExited -or $workerRestarted.HasExited -or $learnerRestarted.HasExited) {
        throw 'Restarted Gateway, workflow worker, or learner worker exited during recovery-only verification.'
    }
    $phase2ListenerAfterRecovery = Assert-SingleGatewayListener $gatewayPort ([int]$gatewayRestarted.Id) 'phase2-after-recovery'

    $restartAuthority = [ordered]@{
        phase1 = [ordered]@{
            gateway_pid = $phase1GatewayPid
            workflow_worker_pid = $phase1WorkerPid
            workflow_worker_id = $phase1WorkerId
            learner_worker_pid = $phase1LearnerWorkerPid
            learner_worker_id = $phase1LearnerWorkerId
            gateway_listener = $phase1Listener
        }
        no_gateway_listener_between_phases = $true
        phase2 = [ordered]@{
            gateway_pid = [int]$gatewayRestarted.Id
            workflow_worker_pid = [int]$workerRestarted.Id
            workflow_worker_id = $phase2WorkerId
            learner_worker_pid = [int]$learnerRestarted.Id
            learner_worker_id = $phase2LearnerWorkerId
            gateway_listener = $phase2Listener
            gateway_listener_after_recovery = $phase2ListenerAfterRecovery
            http_request_audit = $phase2HttpAudit
        }
        godot_processes = 2
        same_persistence_identity = [string]$recoveryFingerprint.persistence_identity
        persistence_reset_before_phase1 = $true
        persistence_cleanup_after_phase2 = $true
        database_authority_sha256 = Get-Sha256 $phase1DatabaseJson
        provider_authority_sha256 = Get-Sha256 $phase1RelayJson
        sandbox_authority_sha256 = Get-Sha256 $phase1SandboxJson
        artifact_authority_sha256 = Get-Sha256 $phase1ArtifactJson
        side_effects_unchanged = $true
    }
    if ($RealProvider) {
        $restartAuthority['response_loss_proxy_sha256'] = Get-Sha256 $phase1FaultProxyJson
    }
    $pendingPassResult = [ordered]@{
        classification = $classification
        status = 'PENDING_CLEANUP'
        feature_flags = [ordered]@{
            world_presentation = [bool]$EnableWorldPresentation
            skill_patch = [bool]$EnableSkillPatch
            gateway_skill_patch = [bool]$featureGates.gateway_skill_patch_enabled
            worker_skill_patch = [bool]$featureGates.worker_skill_patch_enabled
        }
        formal_orchestration = [ordered]@{
            provider_generation_minimum = $expectedRelayGenerationCount
            provider_generation_hard_limit = if ($RealProvider) { $expectedRealProviderGenerationLimit } else { $expectedRelayGenerationCount }
            turns = $expectedTurnCount
            runs = $expectedRunCount
            learner_jobs = $expectedLearnerCount
            session_commands = $expectedSessionCommandCount
            build_commands = $expectedBuildCommandCount
            activation_commands = $expectedActivationCommandCount
            turn_commands = $expectedTurnCount
            terminal_commands = $expectedCommandCount
            applied_terminal_commands = $expectedAppliedCommandCount
            rejected_terminal_commands = $expectedRejectedCommandCount
            command_receipts = $expectedCommandCount
            transport_post = $expectedFrontendPostCount
            transport_put = $expectedFrontendPutCount
            m2_full_row_authority_sha256 = $m2FullRowAuthoritySha256
        }
        provider_authority = if ($RealProvider) { [ordered]@{
            source = 'provider'; degraded = $false; relay_restart_get = $true;
            unique_dispatches = [int]$relayStats.unique_dispatches;
            total_generations = [int]$relayStats.total_generations;
            generation_count_max = [int]$relayStats.max_generation_count
        } } else { $null }
        response_loss_recovery = if ($RealProvider) { [ordered]@{
            put_ack_drops = [int]$relayFaultStats.acknowledgement_drops;
            forced_reconcile_unavailable_attempted = [int]$relayFaultStats.reconcile_unavailable_attempted;
            forced_reconcile_unavailable_delivered = [int]$relayFaultStats.reconcile_unavailable_delivered;
            terminal_before_drop = $relayFaultStats.terminal_before_drop;
            fault_dispatch_id = [string]$relayFaultStats.fault_dispatch_id;
            recovered_same_dispatch = $relayFaultStats.recovered_same_dispatch;
            recovered_generation_count = [int]$relayFaultStats.recovered_generation_count;
            turn_job_attempt = [int]$databaseFingerprint.turn_job_attempt;
            turn_job_fencing_token = [int]$databaseFingerprint.turn_job_fencing_token;
            worker_reconcile_receipts = [int]$databaseFingerprint.turn_worker_reconcile_receipts;
            worker_failure_receipts = [int]$databaseFingerprint.turn_worker_failure_receipts;
            generation_count_max = [int]$relayStats.max_generation_count
        } } else { [ordered]@{
            put_ack_drops = [int]$relayStats.acknowledgement_drops;
            forced_reconcile_unavailable = [int]$relayStats.reconcile_unavailable;
            reconcile_gets = [int]$relayStats.reconcile_gets;
            turn_job_attempt = [int]$databaseFingerprint.turn_job_attempt;
            turn_job_fencing_token = [int]$databaseFingerprint.turn_job_fencing_token;
            worker_reconcile_receipts = [int]$databaseFingerprint.turn_worker_reconcile_receipts;
            worker_failure_receipts = [int]$databaseFingerprint.turn_worker_failure_receipts;
            generation_count_max = [int]$relayStats.max_generation_count
        } }
        identity_revision_sequence_hash = $sideEffectAuthority
        side_effect_sha256 = Get-Sha256 $sideEffectJson
        ui_display = $godotFingerprint.ui_display
        skill_patch_public_authority = $godotFingerprint.skill_patch
        database_outage_recovery = $databaseOutageRecovery
        cross_process_restart = $restartAuthority
        run_root = $runRoot
        elapsed_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
    }
}
catch {
    $runFailure = $_
    $durableFailureState = $null
    if ($postgresStarted) {
        try {
            $durableFailureState = Invoke-DatabaseFingerprint $postgresName @'
SELECT json_build_object(
  'commands', COALESCE((SELECT json_agg(json_build_object(
    'command_id', command_id,
    'status', record_json->>'status',
    'stage', record_json->>'stage',
    'terminal', record_json->'terminal',
    'error', record_json->'error'
  ) ORDER BY command_id) FROM commands), '[]'::json),
  'jobs', COALESCE((SELECT json_agg(json_build_object(
    'job_id', job_id,
    'operation', operation,
    'status', status,
    'phase', phase,
    'attempt', attempt,
    'fencing_token', fencing_token,
    'last_error', last_error_json
  ) ORDER BY job_id) FROM workflow_jobs), '[]'::json),
  'step_names', COALESCE((SELECT json_agg(step_name ORDER BY job_id, step_name)
    FROM job_step_receipts), '[]'::json),
  'world_snapshot', COALESCE((SELECT json_build_object(
    'world_id', world_id,
    'revision', revision,
    'last_event_sequence', last_event_sequence,
    'state_hash', state_hash,
    'actor_id', actor_id,
    'content_hash', content_hash
  ) FROM world_snapshots WHERE tenant_id='tenant_yaya' AND world_id='world_watering_0001'), '{}'::json),
  'presentation_head', COALESCE((SELECT json_build_object(
    'stream_id', stream_id,
    'initial_world_revision', initial_world_revision,
    'initial_world_event_sequence', initial_world_event_sequence,
    'initial_snapshot_state_hash', initial_snapshot_state_hash,
    'last_sequence', last_sequence,
    'last_world_revision', last_world_revision,
    'last_world_event_sequence', last_world_event_sequence,
    'last_snapshot_state_hash', last_snapshot_state_hash,
    'gap_world_revision', gap_world_revision,
    'actor_id', actor_id,
    'content_hash', content_hash
  ) FROM world_presentation_streams WHERE tenant_id='tenant_yaya' AND world_id='world_watering_0001'), '{}'::json),
  'presentation_events', COALESCE((SELECT json_agg(json_build_object(
    'sequence', sequence,
    'event_id', event_id,
    'event_type', event_type,
    'world_revision', world_revision,
    'action_index', action_index,
    'action_count', action_count,
    'intent_id', intent_id,
    'state_hash_before', state_hash_before,
    'state_hash_after', state_hash_after,
    'final_world_event_sequence', final_world_event_sequence,
    'final_snapshot_state_hash', final_snapshot_state_hash,
    'session_id', session_id,
    'turn_id', turn_id,
    'command_id', command_id,
    'run_id', run_id,
    'commit_id', commit_id
  ) ORDER BY sequence) FROM world_presentation_events
    WHERE tenant_id='tenant_yaya' AND world_id='world_watering_0001'), '[]'::json),
  'world_commits', COALESCE((SELECT json_agg(json_build_object(
    'sequence', sequence,
    'event_type', event_json->>'event_type',
    'command_id', event_json->>'command_id',
    'payload', event_json->'payload'
  ) ORDER BY sequence) FROM domain_events
    WHERE tenant_id='tenant_yaya'
      AND event_json->>'event_type'='world.committed'
      AND event_json->'payload'->>'world_id'='world_watering_0001'), '[]'::json)
);
'@
        }
        catch {
            $durableFailureState = [ordered]@{ unavailable = $true }
        }
    }
}
finally {
    $realProviderUpstreamKey = $null
    $cleanupMessages = [Collections.Generic.List[string]]::new()
    foreach ($process in $startedProcesses) {
        try {
            if (-not $process.HasExited) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                if (-not $process.WaitForExit(5000)) {
                    throw "Child process $($process.Id) did not exit during cleanup."
                }
            }
        }
        catch {
            $cleanupMessages.Add($_.Exception.Message)
        }
    }
    try {
        Remove-OwnedPostgresContainer `
            $postgresCreated `
            $postgresName `
            $postgresId `
            $runId `
            $postgresResourceOwner
    }
    catch {
        $cleanupMessages.Add($_.Exception.Message)
    }
    try {
        Remove-OwnedPostgresVolume `
            $postgresVolumeCreated `
            $postgresVolumeAbsentBeforeCreate `
            $postgresVolumeName `
            $runId `
            $postgresResourceOwner
    }
    catch {
        $cleanupMessages.Add($_.Exception.Message)
    }
    try {
        $dockerBaselineAfterCleanup = Assert-DockerBaselineRestored $dockerBaseline
    }
    catch {
        $cleanupMessages.Add($_.Exception.Message)
    }
    if ($cleanupMessages.Count -ne 0) {
        $cleanupFailure = [InvalidOperationException]::new(
            "Diagnostic cleanup/postcondition failed: $($cleanupMessages -join ' | ')"
        )
    }
    try {
        foreach ($name in $environmentNames) {
            [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
        }
    }
    catch {
        if ($null -eq $cleanupFailure) {
            $cleanupFailure = $_.Exception
        }
    }
    $stopwatch.Stop()
}

$terminalFailure = if ($null -ne $cleanupFailure) {
    $cleanupFailure
}
elseif ($null -ne $runFailure) {
    $runFailure.Exception
}
else {
    $null
}
if ($null -ne $terminalFailure) {
    $failure = [ordered]@{
        classification = $classification
        status = 'FAIL'
        reason = $terminalFailure.Message
        durable_failure_state = $durableFailureState
        docker_baseline_sha256 = if ($null -eq $dockerBaseline) { $null } else { [string]$dockerBaseline.sha256 }
        run_root = $runRoot
        elapsed_seconds = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
    }
    Write-Output ("INT1_LOCAL_DIAGNOSTIC_FAIL " + ($failure | ConvertTo-Json -Compress -Depth 8))
    foreach ($name in @(
        'gateway.stderr.log',
        'worker.stderr.log',
        'learner-worker.stderr.log',
        'gateway-restarted.stderr.log',
        'worker-restarted.stderr.log',
        'learner-worker-restarted.stderr.log',
        'relay.stderr.log',
        'relay-restarted.stderr.log',
        'relay-fault-proxy.stderr.log'
    )) {
        $path = Join-Path $runRoot $name
        if (Test-Path -LiteralPath $path) {
            Write-Output "INT1_LOCAL_DIAGNOSTIC_LOG $name"
            Get-Content -LiteralPath $path -Tail 80
        }
    }
    throw $terminalFailure
}
if ($null -eq $pendingPassResult -or $null -eq $dockerBaselineAfterCleanup) {
    throw 'Diagnostic reached no terminal result after cleanup and baseline verification.'
}
$pendingPassResult['docker_baseline_authority'] = [ordered]@{
    count = [int]$dockerBaseline.count
    full_ids = @($dockerBaseline.ids)
    canonical_utf8_base64 = [string]$dockerBaseline.canonical_utf8_base64
    canonical_sha256 = [string]$dockerBaseline.sha256
    all_running_before = [bool]$dockerBaseline.all_running
    all_running_after = [bool]$dockerBaselineAfterCleanup.all_running
    exact_running_set_restored = $true
    canonical_bytes_restored = $true
}
$pendingPassResult['status'] = 'PASS'
$pendingPassResult['elapsed_seconds'] = [Math]::Round($stopwatch.Elapsed.TotalSeconds, 3)
Write-Output (
    "INT1_LOCAL_DIAGNOSTIC_PASS " +
    ($pendingPassResult | ConvertTo-Json -Compress -Depth 10)
)
