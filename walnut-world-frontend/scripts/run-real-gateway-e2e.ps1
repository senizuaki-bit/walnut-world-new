[CmdletBinding()]
param(
    [string]$GodotExe = $env:GODOT_EXE,
    [int]$TotalDeadlineSeconds = 600,
    [int]$ResourceDeadlineSeconds = 180,
    [int]$InteractionDeadlineSeconds = 90,
    [string]$Phase1FingerprintPath,
    [switch]$EnableWorldPresentation,
    [switch]$EnableSkillPatch,
    [switch]$RecoveryOnly,
    [switch]$ResetPersistence,
    [switch]$CleanupPersistence
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-JsonIntProperty {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return 0
    }
    return [int]$property.Value
}

function Stop-VerifiedSpawnedProcessTree {
    param(
        [Parameter(Mandatory = $true)][Diagnostics.Process]$Process,
        [string]$TaskKillPath = (Join-Path $env:SystemRoot 'System32\taskkill.exe')
    )

    if ($Process.HasExited) {
        return
    }
    $spawnedStartTime = $Process.StartTime.ToUniversalTime()
    $liveProcess = Get-Process -Id $Process.Id -ErrorAction Stop
    if ($liveProcess.StartTime.ToUniversalTime() -ne $spawnedStartTime) {
        throw 'Refusing to terminate a process whose PID no longer identifies the exact spawned Godot process.'
    }
    if (-not (Test-Path -LiteralPath $TaskKillPath -PathType Leaf)) {
        throw 'Cannot terminate the exact spawned Godot process tree because taskkill.exe is unavailable.'
    }
    & $TaskKillPath /PID ([string]$Process.Id) /T /F 2>$null | Out-Null
    $taskKillExitCode = $LASTEXITCODE
    if (-not $Process.WaitForExit(5000)) {
        throw 'The exact spawned Godot process tree did not exit after forced termination.'
    }
    if ($taskKillExitCode -ne 0) {
        throw "taskkill.exe failed to terminate the exact spawned Godot process tree (exit $taskKillExitCode)."
    }
}

$projectPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($GodotExe)) {
    $bundledCandidates = @(
        (Join-Path (Split-Path -Parent $projectPath) 'tools\godot-4.5.2\Godot_v4.5.2-stable_win64_console.exe'),
        (Join-Path $projectPath '..\..\tools\godot-4.5.2\Godot_v4.5.2-stable_win64_console.exe')
    )
    foreach ($bundledCandidate in $bundledCandidates) {
        if (Test-Path -LiteralPath $bundledCandidate -PathType Leaf) {
            $GodotExe = (Resolve-Path -LiteralPath $bundledCandidate).Path
            break
        }
    }
}
if ([string]::IsNullOrWhiteSpace($GodotExe) -or -not (Test-Path -LiteralPath $GodotExe)) {
    throw 'Set GODOT_EXE or pass -GodotExe with the Godot 4.5.2 console executable.'
}
if ([string]::IsNullOrWhiteSpace($env:YAYA_API_BASE_URL)) {
    throw 'YAYA_API_BASE_URL must identify the independently running real Gateway.'
}
if ([string]::IsNullOrWhiteSpace($env:YAYA_AUTH_TOKEN)) {
    throw 'YAYA_AUTH_TOKEN must contain a valid student Bearer JWT. The runner never prints it.'
}
if ($TotalDeadlineSeconds -le 0 -or $ResourceDeadlineSeconds -le 0 -or $InteractionDeadlineSeconds -le 0) {
    throw 'All E2E deadline values must be positive.'
}
if ($ResourceDeadlineSeconds -ge $TotalDeadlineSeconds -or $InteractionDeadlineSeconds -ge $TotalDeadlineSeconds) {
    throw 'Resource and Interaction deadlines must be smaller than the total deadline.'
}
if ($RecoveryOnly -and $ResetPersistence) {
    throw 'Recovery-only mode must retain the exact phase-1 persistence file.'
}
if (-not $RecoveryOnly -and -not $ResetPersistence) {
    throw 'Phase 1 must use -ResetPersistence so its authority fingerprint starts from the exact empty persistence family.'
}
if ($RecoveryOnly -and -not $CleanupPersistence) {
    throw 'The final recovery-only phase must use -CleanupPersistence and verify no target, backup, or temporary file remains.'
}
if ([string]::IsNullOrWhiteSpace($Phase1FingerprintPath)) {
    throw 'Pass -Phase1FingerprintPath to persist phase 1 and bind recovery-only to that exact authority fingerprint.'
}
$phase1FingerprintFullPath = [IO.Path]::GetFullPath($Phase1FingerprintPath)
$phase1FingerprintParent = Split-Path -Parent $phase1FingerprintFullPath
if ([string]::IsNullOrWhiteSpace($phase1FingerprintParent) -or -not (Test-Path -LiteralPath $phase1FingerprintParent -PathType Container)) {
    throw 'The parent directory for -Phase1FingerprintPath must already exist.'
}
if ($RecoveryOnly -and -not (Test-Path -LiteralPath $phase1FingerprintFullPath -PathType Leaf)) {
    throw 'Recovery-only mode requires the exact phase-1 fingerprint file produced before the external service restart.'
}
if (-not $RecoveryOnly -and $CleanupPersistence) {
    throw 'Persistence cleanup belongs only to the final recovery-only process.'
}
if ($EnableSkillPatch -and -not $EnableWorldPresentation) {
    throw 'Skill Patch requires -EnableWorldPresentation so M2 cannot bypass the completed M1 rollout gate.'
}

$environmentNames = @(
    'YAYA_REAL_GATEWAY_E2E',
    'YAYA_REAL_GATEWAY_E2E_RECOVERY_ONLY',
    'YAYA_REAL_GATEWAY_E2E_RESET_PERSISTENCE',
    'YAYA_REAL_GATEWAY_E2E_CLEANUP_PERSISTENCE',
    'YAYA_REAL_GATEWAY_E2E_PHASE1_FINGERPRINT_PATH',
    'YAYA_REAL_GATEWAY_E2E_ENABLE_WORLD_PRESENTATION',
    'YAYA_REAL_GATEWAY_E2E_ENABLE_SKILL_PATCH',
    'YAYA_REAL_GATEWAY_E2E_TOTAL_DEADLINE_SECONDS',
    'YAYA_REAL_GATEWAY_E2E_RESOURCE_DEADLINE_SECONDS',
    'YAYA_REAL_GATEWAY_E2E_INTERACTION_DEADLINE_SECONDS'
)
$previousValues = @{}
foreach ($name in $environmentNames) {
    $previousValues[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

try {
    [Environment]::SetEnvironmentVariable('YAYA_REAL_GATEWAY_E2E', '1', 'Process')
    [Environment]::SetEnvironmentVariable(
        'YAYA_REAL_GATEWAY_E2E_RECOVERY_ONLY',
        $(if ($RecoveryOnly) { '1' } else { '0' }),
        'Process'
    )
    [Environment]::SetEnvironmentVariable('YAYA_REAL_GATEWAY_E2E_PHASE1_FINGERPRINT_PATH', $phase1FingerprintFullPath, 'Process')
    [Environment]::SetEnvironmentVariable(
        'YAYA_REAL_GATEWAY_E2E_ENABLE_WORLD_PRESENTATION',
        $(if ($EnableWorldPresentation) { '1' } else { '0' }),
        'Process'
    )
    [Environment]::SetEnvironmentVariable(
        'YAYA_REAL_GATEWAY_E2E_ENABLE_SKILL_PATCH',
        $(if ($EnableSkillPatch) { '1' } else { '0' }),
        'Process'
    )
    [Environment]::SetEnvironmentVariable(
        'YAYA_REAL_GATEWAY_E2E_RESET_PERSISTENCE',
        $(if ($ResetPersistence) { '1' } else { '0' }),
        'Process'
    )
    [Environment]::SetEnvironmentVariable(
        'YAYA_REAL_GATEWAY_E2E_CLEANUP_PERSISTENCE',
        $(if ($CleanupPersistence) { '1' } else { '0' }),
        'Process'
    )
    [Environment]::SetEnvironmentVariable('YAYA_REAL_GATEWAY_E2E_TOTAL_DEADLINE_SECONDS', [string]$TotalDeadlineSeconds, 'Process')
    [Environment]::SetEnvironmentVariable('YAYA_REAL_GATEWAY_E2E_RESOURCE_DEADLINE_SECONDS', [string]$ResourceDeadlineSeconds, 'Process')
    [Environment]::SetEnvironmentVariable('YAYA_REAL_GATEWAY_E2E_INTERACTION_DEADLINE_SECONDS', [string]$InteractionDeadlineSeconds, 'Process')

    $testScript = if ($RecoveryOnly) {
        'res://tests/client/real_gateway_chain_recovery_e2e_test.gd'
    }
    else {
        'res://tests/client/real_gateway_chain_e2e_test.gd'
    }
    # PowerShell 5.1 promotes native stderr records to terminating errors when
    # ErrorActionPreference is Stop, truncating the actual Godot failure.  Use
    # explicit files so the complete sanitized diagnostic remains observable.
    $stdoutPath = [IO.Path]::GetTempFileName()
    $stderrPath = [IO.Path]::GetTempFileName()
    $processDeadlineGraceSeconds = 5
    $processDeadlineMilliseconds = ([long]$TotalDeadlineSeconds + $processDeadlineGraceSeconds) * 1000
    if ($processDeadlineMilliseconds -gt [int]::MaxValue) {
        throw 'TotalDeadlineSeconds is too large for the external Godot process deadline.'
    }
    $processTimedOut = $false
    $processTerminationError = $null
    $testExitCode = -1
    $outputLines = @()
    try {
        $godotProcess = Start-Process -FilePath $GodotExe `
            -ArgumentList @('--headless', '--path', $projectPath, '--script', $testScript) `
            -WorkingDirectory $projectPath `
            -WindowStyle Hidden `
            -PassThru `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        # Materialize and retain the native process handle before a short-lived
        # child can exit, so PowerShell 5.1 can later expose ExitCode reliably.
        [void]$godotProcess.Handle
        if (-not $godotProcess.WaitForExit([int]$processDeadlineMilliseconds)) {
            $processTimedOut = $true
            try {
                Stop-VerifiedSpawnedProcessTree -Process $godotProcess
            }
            catch {
                $processTerminationError = $_.Exception.Message
            }
        }
        if (-not $processTimedOut) {
            # The timed overload can report completion before the redirected
            # process object has populated ExitCode.  The no-argument wait is
            # now non-blocking and completes redirected-stream bookkeeping.
            $godotProcess.WaitForExit()
            $godotProcess.Refresh()
            if (-not $godotProcess.HasExited) {
                throw 'The spawned Godot process reported completion but is not exited.'
            }
            $observedExitCode = $godotProcess.ExitCode
            if ($null -eq $observedExitCode -or $observedExitCode -isnot [int]) {
                $observedExitCodeType = if ($null -eq $observedExitCode) {
                    '<null>'
                }
                else {
                    $observedExitCode.GetType().FullName
                }
                throw "The exited Godot process did not expose a strict integer exit code (type $observedExitCodeType)."
            }
            $testExitCode = [int]$observedExitCode
        }
        $outputLines = @(
            @(Get-Content -LiteralPath $stdoutPath -Encoding UTF8 -ErrorAction SilentlyContinue)
            @(Get-Content -LiteralPath $stderrPath -Encoding UTF8 -ErrorAction SilentlyContinue)
        ) | ForEach-Object { [string]$_ }
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
    $outputLines | ForEach-Object { Write-Host $_ }
    if ($processTimedOut) {
        $timeoutSeconds = $TotalDeadlineSeconds + $processDeadlineGraceSeconds
        if ($null -ne $processTerminationError) {
            throw "Real Gateway Godot E2E exceeded external process deadline of $timeoutSeconds seconds, and exact process-tree termination failed: $processTerminationError"
        }
        throw "Real Gateway Godot E2E exceeded external process deadline of $timeoutSeconds seconds; the exact spawned process tree was terminated."
    }
    if ($testExitCode -ne 0) {
        throw "Real Gateway Godot E2E failed with exit code $testExitCode."
    }

    $passPrefix = if ($RecoveryOnly) {
        'REAL_GATEWAY_CHAIN_RECOVERY_PASS '
    }
    else {
        'REAL_GATEWAY_CHAIN_E2E_PASS '
    }
    $passLines = @($outputLines | Where-Object { $_.StartsWith($passPrefix, [StringComparison]::Ordinal) })
    if ($passLines.Count -ne 1) {
        throw 'Real Gateway Godot E2E did not emit exactly one structured PASS fingerprint.'
    }
    $fingerprintJson = $passLines[0].Substring($passPrefix.Length)
    $fingerprint = $fingerprintJson | ConvertFrom-Json
    if ($RecoveryOnly) {
        $expectedPhase1 = Get-Content -LiteralPath $phase1FingerprintFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $actualAuthorityJson = $fingerprint.authority_fingerprint | ConvertTo-Json -Depth 100 -Compress
        $expectedAuthorityJson = $expectedPhase1.authority_fingerprint | ConvertTo-Json -Depth 100 -Compress
        $actualSkillPatchJson = $fingerprint.skill_patch | ConvertTo-Json -Depth 100 -Compress
        $expectedSkillPatchJson = $expectedPhase1.skill_patch | ConvertTo-Json -Depth 100 -Compress
        $recoveryAudit = $fingerprint.transport_attempt_audit
        if (
            $fingerprint.recovery_only -ne $true -or
            $fingerprint.persisted_store_loaded -ne $true -or
            $fingerprint.phase1_authority_exact_match -ne $true -or
            [string]$fingerprint.phase1_fingerprint_schema -ne '1.0.0' -or
            $actualAuthorityJson -cne $expectedAuthorityJson -or
            [int]$recoveryAudit.total_started -le 0 -or
            [int]$recoveryAudit.total_started -ne [int]$recoveryAudit.total_completed -or
            (Get-JsonIntProperty $recoveryAudit.method_counts 'GET') -ne [int]$recoveryAudit.total_started -or
            (Get-JsonIntProperty $recoveryAudit.method_counts 'POST') -ne 0 -or
            (Get-JsonIntProperty $recoveryAudit.method_counts 'PUT') -ne 0 -or
            (Get-JsonIntProperty $recoveryAudit.method_counts 'PATCH') -ne 0 -or
            (Get-JsonIntProperty $recoveryAudit.method_counts 'DELETE') -ne 0 -or
            [string]$fingerprint.persistence_identity -notmatch '^[0-9a-f]{16}$' -or
            [string]$fingerprint.session_id -notmatch '^session_' -or
            [int]$fingerprint.draft_revision -lt 1 -or
            [string]$fingerprint.workspace_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$fingerprint.draft_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$fingerprint.world_state_hash -notmatch '^[0-9a-f]{64}$' -or
            [string]$fingerprint.active_skill_tuple_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$fingerprint.interaction_feedback_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$fingerprint.live_pending_response_loss.status -ne 'NOT_PROVEN' -or
            $fingerprint.ui_display.crop_adaptive_watering_demo -ne $true -or
            $fingerprint.ui_display.crop_agent_bridge -ne $true -or
            $fingerprint.ui_display.run_button -ne $true -or
            $fingerprint.ui_display.content_draft_interaction_snapshot -ne $true
        ) {
            throw 'Recovery-only Godot PASS fingerprint does not prove persisted authority and formal UI recovery.'
        }
        if ($EnableWorldPresentation -and (
            $fingerprint.world_presentation.enabled -ne $true -or
            $fingerprint.world_presentation.recovered_by_snapshot -ne $true -or
            [int]$fingerprint.world_presentation.presentation_high_watermark -ne [int]$expectedPhase1.world_presentation.presentation_high_watermark
        )) {
            throw 'Recovery-only Godot PASS fingerprint does not prove GET-only authoritative presentation resynchronization.'
        }
        if ($EnableSkillPatch -and (
            $fingerprint.phase1_skill_patch_exact_match -ne $true -or
            $fingerprint.skill_patch.enabled -ne $true -or
            [string]$fingerprint.skill_patch.status -ne 'PUBLIC_UI_CHAIN_CLOSED' -or
            $fingerprint.skill_patch.backend_authority_fingerprint_required -ne $true -or
            $actualSkillPatchJson -cne $expectedSkillPatchJson -or
            [int]$fingerprint.skill_patch.expected_transport_counts.POST -ne 12 -or
            [int]$fingerprint.skill_patch.expected_transport_counts.PUT -ne 1 -or
            [int]$fingerprint.skill_patch.expected_backend_counts.turns -ne 6 -or
            [int]$fingerprint.skill_patch.expected_backend_counts.runs -ne 5 -or
            [int]$fingerprint.skill_patch.expected_backend_counts.learner_jobs -ne 5 -or
            $fingerprint.skill_patch.public_terminal_run_get_validated_learner_projection -ne $true -or
            [string]$fingerprint.skill_patch.public_chain_sha256 -notmatch '^[0-9a-f]{64}$'
        )) {
            throw 'Recovery-only Godot fingerprint does not reconstruct the exact public M2 Patch authority through GET-only reads.'
        }
        if (
            $fingerprint.persistence_cleanup_performed -ne $true -or
            [int]$fingerprint.persistence_cleanup_residual_count -ne 0
        ) {
            throw 'Recovery-only Godot process did not remove and verify the exact persistence target, backup, and temporary files.'
        }
    }
    else {
        $phase1Audit = $fingerprint.transport_attempt_audit
        $expectedTurnCount = if ($EnableSkillPatch) { 6 } else { 4 }
        $expectedRunCount = if ($EnableSkillPatch) { 5 } else { 4 }
        $expectedPostCount = if ($EnableSkillPatch) { 12 } else { 9 }
        $expectedPutCount = if ($EnableSkillPatch) { 1 } else { 2 }
        $expectedDraftUpsertCount = if ($EnableSkillPatch) { 1 } else { 2 }
        $expectedPatchDecisionCount = if ($EnableSkillPatch) { 1 } else { 0 }
        $expectedInteractionRoles = if ($EnableSkillPatch) {
            'teaching_agent,teaching_agent,bug_agent,bug_agent,teaching_agent,book_agent'
        }
        else {
            'teaching_agent,teaching_agent,bug_agent,book_agent'
        }
        $expectedCommandStatuses = if ($EnableSkillPatch) {
            'REJECTED,REJECTED,REJECTED,REJECTED,APPLIED,APPLIED'
        }
        else {
            'REJECTED,REJECTED,REJECTED,APPLIED'
        }
        $expectedRunStatuses = if ($EnableSkillPatch) {
            'REJECTED,REJECTED,REJECTED,REJECTED,SUCCEEDED'
        }
        else {
            'REJECTED,REJECTED,REJECTED,SUCCEEDED'
        }
        if (
        [string]$fingerprint.phase1_fingerprint_schema -ne '1.0.0' -or
        $fingerprint.api_store_closure.draft_cas_performed -ne $true -or
        $fingerprint.api_store_closure.failure_chain_closed -ne $true -or
        ($EnableSkillPatch -or $fingerprint.api_store_closure.correction_draft_cas_performed -eq $true) -ne $true -or
        ($EnableSkillPatch -and $fingerprint.api_store_closure.correction_draft_cas_performed -ne $false) -or
        ($fingerprint.api_store_closure.patch_decision_performed -ne [bool]$EnableSkillPatch) -or
        $fingerprint.api_store_closure.second_build_performed -ne $true -or
        $fingerprint.api_store_closure.build_performed -ne $true -or
        $fingerprint.api_store_closure.run_closed -ne $true -or
        [string]$fingerprint.persistence_identity -notmatch '^[0-9a-f]{16}$' -or
        [int]$fingerprint.starter_draft_revision -ne 1 -or
        [int]$fingerprint.failure_draft_revision -ne 2 -or
        [int]$fingerprint.saved_draft_revision -ne 3 -or
        [int]$fingerprint.starter_workspace_revision -ne 1 -or
        [int]$fingerprint.failure_workspace_revision -ne 2 -or
        [int]$fingerprint.saved_workspace_revision -le [int]$fingerprint.failure_workspace_revision -or
        [int]$fingerprint.final_workspace_revision -lt [int]$fingerprint.saved_workspace_revision -or
        [string]$fingerprint.final_workspace_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$fingerprint.failure_draft_source_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$fingerprint.failure_draft_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$fingerprint.draft_source_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$fingerprint.draft_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$fingerprint.failure_build_source_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$fingerprint.build_source_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$fingerprint.failure_draft_source_sha256 -eq [string]$fingerprint.draft_source_sha256 -or
        [string]$fingerprint.failure_draft_sha256 -eq [string]$fingerprint.draft_sha256 -or
        [string]$fingerprint.failure_build_source_sha256 -eq [string]$fingerprint.build_source_sha256 -or
        [string]$fingerprint.active_skill_tuple_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [int]$fingerprint.active_skill_tuple.registry_revision -ne 2 -or
        [string]$fingerprint.failure_reason -ne 'TASK_INCOMPLETE' -or
        @($fingerprint.build_ids).Count -ne 2 -or
        @($fingerprint.activation_ids).Count -ne 2 -or
        @($fingerprint.turn_ids).Count -ne $expectedTurnCount -or
        @($fingerprint.command_ids).Count -ne $expectedTurnCount -or
        @($fingerprint.run_ids).Count -ne $expectedRunCount -or
        @($fingerprint.interaction_ids).Count -ne $expectedTurnCount -or
        @($fingerprint.evidence_ids).Count -ne [int]$fingerprint.evidence_count -or
        [int]$fingerprint.evidence_count -lt 5 -or
        @($fingerprint.build_ids | Sort-Object -Unique).Count -ne 2 -or
        @($fingerprint.activation_ids | Sort-Object -Unique).Count -ne 2 -or
        @($fingerprint.turn_ids | Sort-Object -Unique).Count -ne $expectedTurnCount -or
        @($fingerprint.command_ids | Sort-Object -Unique).Count -ne $expectedTurnCount -or
        @($fingerprint.run_ids | Sort-Object -Unique).Count -ne $expectedRunCount -or
        @($fingerprint.interaction_ids | Sort-Object -Unique).Count -ne $expectedTurnCount -or
        (@($fingerprint.interaction_roles) -join ',') -ne $expectedInteractionRoles -or
        (@($fingerprint.command_statuses) -join ',') -ne $expectedCommandStatuses -or
        (@($fingerprint.run_statuses) -join ',') -ne $expectedRunStatuses -or
        [string]$fingerprint.interaction_role -ne 'book_agent' -or
        [int]$fingerprint.interaction_sequence -ne $expectedTurnCount -or
        [int]$phase1Audit.total_started -le 0 -or
        [int]$phase1Audit.total_started -ne [int]$phase1Audit.total_completed -or
        (Get-JsonIntProperty $phase1Audit.method_counts 'POST') -ne $expectedPostCount -or
        (Get-JsonIntProperty $phase1Audit.method_counts 'PUT') -ne $expectedPutCount -or
        (Get-JsonIntProperty $phase1Audit.method_counts 'PATCH') -ne 0 -or
        (Get-JsonIntProperty $phase1Audit.method_counts 'DELETE') -ne 0 -or
        (Get-JsonIntProperty $phase1Audit.operation_counts 'create_agent_session') -ne 1 -or
        (Get-JsonIntProperty $phase1Audit.operation_counts 'upsert_product_skill_draft') -ne $expectedDraftUpsertCount -or
        (Get-JsonIntProperty $phase1Audit.operation_counts 'submit_skill_build') -ne 2 -or
        (Get-JsonIntProperty $phase1Audit.operation_counts 'activate_skill_version') -ne 2 -or
        (Get-JsonIntProperty $phase1Audit.operation_counts 'submit_agent_turn') -ne $expectedTurnCount -or
        (Get-JsonIntProperty $phase1Audit.operation_counts 'record_product_patch_decision') -ne $expectedPatchDecisionCount -or
        [string]$fingerprint.persistence_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$fingerprint.live_pending_response_loss.status -ne 'NOT_PROVEN' -or
        $fingerprint.ui_display.crop_adaptive_watering_demo -ne $true -or
        $fingerprint.ui_display.crop_agent_bridge -ne $true -or
        $fingerprint.ui_display.run_button -ne $true -or
        $fingerprint.ui_display.content_draft_interaction_snapshot -ne $true
        ) {
            throw 'Real Gateway Godot E2E PASS fingerprint does not prove the selected audited failure/correction chain and formal UI display.'
        }
		if ($EnableSkillPatch -and (
			$fingerprint.skill_patch.enabled -ne $true -or
			[string]$fingerprint.skill_patch.status -ne 'PUBLIC_UI_CHAIN_CLOSED' -or
			$fingerprint.skill_patch.backend_authority_fingerprint_required -ne $true -or
			[int]$fingerprint.skill_patch.expected_transport_counts.POST -ne 12 -or
			[int]$fingerprint.skill_patch.expected_transport_counts.PUT -ne 1 -or
			[int]$fingerprint.skill_patch.expected_backend_counts.turns -ne 6 -or
			[int]$fingerprint.skill_patch.expected_backend_counts.runs -ne 5 -or
			[int]$fingerprint.skill_patch.expected_backend_counts.learner_jobs -ne 5 -or
			$fingerprint.skill_patch.public_terminal_run_get_validated_learner_projection -ne $true -or
			[string]$fingerprint.skill_patch.public_chain_sha256 -notmatch '^[0-9a-f]{64}$' -or
			[string]$fingerprint.skill_patch.public_hashes.proposal_command_sha256 -notmatch '^[0-9a-f]{64}$' -or
			[string]$fingerprint.skill_patch.public_hashes.proposal_interaction_sha256 -notmatch '^[0-9a-f]{64}$' -or
			[string]$fingerprint.skill_patch.public_hashes.patch_sha256 -notmatch '^[0-9a-f]{64}$' -or
			[string]$fingerprint.skill_patch.public_hashes.decision_sha256 -notmatch '^[0-9a-f]{64}$' -or
			[string]$fingerprint.skill_patch.public_hashes.accepted_draft_resource_sha256 -notmatch '^[0-9a-f]{64}$' -or
			[string]$fingerprint.skill_patch.public_hashes.build_resource_sha256 -notmatch '^[0-9a-f]{64}$' -or
			[string]$fingerprint.skill_patch.public_hashes.activation_resource_sha256 -notmatch '^[0-9a-f]{64}$' -or
			[string]$fingerprint.skill_patch.public_hashes.run_resource_sha256 -notmatch '^[0-9a-f]{64}$' -or
			[string]$fingerprint.skill_patch.public_hashes.final_interaction_sha256 -notmatch '^[0-9a-f]{64}$'
		)) {
			throw 'Formal M2 public UI/read fingerprint is incomplete; independent Backend PostgreSQL authority fingerprint remains required.'
		}
        if ($EnableWorldPresentation -and (
            $fingerprint.world_presentation.enabled -ne $true -or
            [int]$fingerprint.world_presentation.playback_started -ne 1 -or
            [int]$fingerprint.world_presentation.playback_finished -ne 1 -or
            $fingerprint.world_presentation.playing_observed -ne $true -or
            @($fingerprint.world_presentation.event_ids_started).Count -ne 8 -or
            (@($fingerprint.world_presentation.event_ids_started) -join ',') -cne (@($fingerprint.world_presentation.event_ids_finished) -join ',') -or
            [int]$fingerprint.world_presentation.presentation_high_watermark -lt 8
        )) {
            throw 'Real Gateway Godot E2E PASS fingerprint does not prove eight ordered formal HARVEST presentations through PLAYING.'
        }
        if (
            $fingerprint.persistence_reset_performed -ne $true -or
            [int]$fingerprint.persistence_reset_residual_count -ne 0
        ) {
            throw 'Phase-1 Godot process did not reset and verify its exact persistence target, backup, and temporary files.'
        }
        $fingerprintTemporaryPath = "$phase1FingerprintFullPath.tmp"
        try {
            if (Test-Path -LiteralPath $fingerprintTemporaryPath) {
                Remove-Item -LiteralPath $fingerprintTemporaryPath -Force
            }
            [IO.File]::WriteAllText($fingerprintTemporaryPath, $fingerprintJson, [Text.UTF8Encoding]::new($false))
            Move-Item -LiteralPath $fingerprintTemporaryPath -Destination $phase1FingerprintFullPath -Force
        }
        finally {
            if (Test-Path -LiteralPath $fingerprintTemporaryPath) {
                Remove-Item -LiteralPath $fingerprintTemporaryPath -Force
            }
        }
        if (-not (Test-Path -LiteralPath $phase1FingerprintFullPath -PathType Leaf)) {
            throw 'The validated phase-1 authority fingerprint could not be persisted for recovery-only.'
        }
    }
}
finally {
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $previousValues[$name], 'Process')
    }
}

if ($RecoveryOnly) {
    Write-Host 'Real Gateway Godot recovery-only E2E passed with exact phase-1 authority matching, audited GET-only recovery, cleanup, and formal UI reconstruction. For M2, the independent Backend PostgreSQL authority fingerprint is still required. Live response-loss replay remains NOT_PROVEN.'
}
else {
    Write-Host "Real Gateway Godot phase 1 passed and persisted its authority fingerprint at $phase1FingerprintFullPath. Restart external services before RecoveryOnly. For M2, pair this public UI/read result with the independent Backend PostgreSQL authority fingerprint. Live response-loss replay remains NOT_PROVEN."
}
