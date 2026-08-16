from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
HARNESS = BACKEND_ROOT / "scripts" / "run-int1-local-diagnostic.ps1"
RELAY = BACKEND_ROOT / "scripts" / "int1_recoverable_relay.py"
REAL_PROVIDER_FAULT_PROXY = BACKEND_ROOT / "scripts" / "int1_real_provider_fault_proxy.py"
RUNBOOK = BACKEND_ROOT / "docs" / "operations" / "int1-local-diagnostic.md"
REAL_PROVIDER_WRAPPER = BACKEND_ROOT / "scripts" / "run-int1-real-provider-e2e.ps1"
COMPOSE = BACKEND_ROOT / "docker-compose.yml"


def test_formal_m2_seed_token_lifetime_covers_both_phases_and_transition_budget() -> None:
    harness = HARNESS.read_text(encoding="utf-8")
    wrapper = REAL_PROVIDER_WRAPPER.read_text(encoding="utf-8")
    token_match = re.search(r"\$int1E2eTokenLifetimeSeconds = (\d+)", harness)
    transition_match = re.search(r"\$int1E2eTransitionBudgetSeconds = (\d+)", harness)
    local_deadline_match = re.search(
        r"\[int\]\$TotalDeadlineSeconds = (\d+)", harness
    )
    live_deadline_match = re.search(
        r"\[int\]\$TotalDeadlineSeconds = (\d+)", wrapper
    )
    assert token_match is not None
    assert transition_match is not None
    assert local_deadline_match is not None
    assert live_deadline_match is not None
    token_lifetime_seconds = int(token_match.group(1))
    transition_budget_seconds = int(transition_match.group(1))
    local_deadline_seconds = int(local_deadline_match.group(1))
    live_deadline_seconds = int(live_deadline_match.group(1))

    assert "$int1E2eTokenLifetimeSeconds = 1800" in harness
    assert "$int1E2eTransitionBudgetSeconds = 300" in harness
    assert "([long]$TotalDeadlineSeconds * 2) + $int1E2eTransitionBudgetSeconds" in harness
    assert "$requiredInt1E2eTokenLifetimeSeconds -gt $int1E2eTokenLifetimeSeconds" in harness
    assert "formal two-phase token lifetime budget" in harness
    assert (
        "$env:WALNUT_AUTH_MAXIMUM_LIFETIME_SECONDS = "
        "[string]$int1E2eTokenLifetimeSeconds"
    ) in harness
    assert "[int]$TotalDeadlineSeconds = 720" in wrapper
    assert 2 * local_deadline_seconds + transition_budget_seconds <= token_lifetime_seconds
    assert 2 * live_deadline_seconds + transition_budget_seconds < token_lifetime_seconds
    assert token_lifetime_seconds - (
        2 * live_deadline_seconds + transition_budget_seconds
    ) == 60
    maximum_deadline_seconds = (
        token_lifetime_seconds - transition_budget_seconds
    ) // 2
    assert 2 * maximum_deadline_seconds + transition_budget_seconds == token_lifetime_seconds
    assert 2 * (maximum_deadline_seconds + 1) + transition_budget_seconds > token_lifetime_seconds


def test_harness_has_fresh_authority_recovery_and_official_godot_chain() -> None:
    script = HARNESS.read_text(encoding="utf-8")

    for required in (
        "$PSScriptRoot",
        "docker version",
        "docker image inspect",
        "function New-OwnedPostgresVolume",
        "type=volume,source=$postgresVolumeName,target=/var/lib/postgresql/data",
        "POSTGRES_DB=walnut_int1",
        "-m alembic upgrade head",
        "-m walnut_backend.int1_e2e_authority",
        "walnut_backend.main:app",
        "walnut_backend.worker_main",
        "run-real-gateway-e2e.ps1",
        "$gatewayPort = 8790",
        "Test-LocalTcpPortAvailable $gatewayPort",
        "WALNUT_RUNTIME_ROOT",
        "WALNUT_ENABLE_WORLD_PRESENTATION",
        "sandbox-results",
        "WALNUT_LLM_RELAY_ENDPOINT",
        "WALNUT_LLM_RELAY_API_KEY",
        "WALNUT_LLM_RELAY_ALLOW_INSECURE_LOCALHOST",
        "WALNUT_INT1_RELAY_DROP_FIRST_PUT_ACK",
        "WALNUT_INT1_RELAY_FAIL_FIRST_RECONCILE",
        "int1_real_provider_fault_proxy.py",
        "privateRelayPort",
        "REAL_PROVIDER_RESPONSE_LOSS_PROXY_TEST_ONLY",
        "terminal_before_drop",
        "recovered_same_dispatch",
        "response_loss_proxy",
        "WORKER_FAILURE_%",
        "WORKER_RECONCILE_%",
        "turn_job_attempt",
        "max_generation_count",
        "learner_worker_main",
        "WALNUT_LEARNER_WORKER_ID",
        "phase1-processes-stopped",
        "gateway-workflow-learner-restarted",
        "Assert-SingleGatewayListener",
        "Get-BackendGatewayProcessIds",
        "host_gateway_process_count",
        "Get-DockerRunningBaseline",
        "Assert-NoDiagnosticDockerConflict",
        "Assert-DockerBaselineRestored",
        "requires no preexisting Backend Gateway process",
        "Wait-LocalPortClosed",
        "phase1GatewayPid",
        "gatewayRestarted.Id",
        "$phase1GodotFingerprintPath = Join-Path $runRoot 'godot-phase1-authority-fingerprint.json'",
        "The run-scoped phase-1 Godot authority fingerprint is empty or unsafe.",
        "-ResetPersistence",
        "-RecoveryOnly",
        "-EnableWorldPresentation",
        "-CleanupPersistence",
        "REAL_GATEWAY_CHAIN_RECOVERY_PASS",
        "databaseFingerprintAfterRestart",
        "relayStatsAfterRestart",
        "sandboxFingerprintAfterRestart",
        "artifactFingerprintAfterRestart",
        "Get-GatewayRequestAudit",
        "mutating_method_count",
        "request_set_sha256",
        "command_count",
        "applied_terminal_command_count",
        "command_set_material",
        "learner_profile_count",
        "learner_profile_set_material",
        "learner_projection_succeeded",
        "learner_projection_terminal_closed",
        "learner_projection_set_material",
        "learner_profile_sha256",
        "Wait-LearnerProjectionClosure",
        "four-learner-projections-terminal",
        "failure_run_count",
        "successful_run_count",
        "same_failure_key_count",
        "distinct_failed_failure_keys",
        "failure_count_sequence",
        "interaction_role_sequence",
        "rejected_terminal_command_count",
        "registry_revision",
        "non_world_event_count",
        "domain_event_set_material",
        "non_world_event_stream_count",
        "event_stream_set_material",
        "relay_dispatch_count",
        "relay_dispatch_set_material",
        "phase2-after-recovery",
        "INT1_LOCAL_DIAGNOSTIC_RESTART_FINGERPRINT",
        "deterministic_relay_capability_probe",
        "side_effects_unchanged",
        "DETERMINISTIC_POSTGRES_STOP_START_RECOVERY",
        "postgres-gateway-workflow-learner-recovered",
        "database_outage_recovery",
        "no_gateway_listener_between_phases",
        "world_state_hash",
        "presentation_stream_count",
        "presentation_event_count",
        "presentation_event_set_material",
        "presentation_high_watermark",
        "event_ids_started",
        "playing_observed",
        "side_effect_sha256",
        "INT1_LOCAL_DIAGNOSTIC_NOT_LIVE",
        "INT1_LOCAL_DIAGNOSTIC_PASS",
        "DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER",
    ):
        assert required in script

    assert "AppData\\Local\\Temp\\walnut-int1-crossrepo" not in script
    assert re.search(r"WALNUT_LLM_ENDPOINT\b", script) is None
    assert re.search(r"WALNUT_LLM_API_KEY\b", script) is None
    assert "docker pull" not in script.lower()
    assert "--tmpfs" not in script
    assert script.count("'-Phase1FingerprintPath', $phase1GodotFingerprintPath") == 2
    assert script.count("'-EnableWorldPresentation'") == 2
    phase1_fingerprint_arg = script.index("'-Phase1FingerprintPath', $phase1GodotFingerprintPath")
    phase1_fingerprint_written = script.index(
        "Phase-1 Godot process did not persist its run-scoped authority fingerprint."
    )
    recovery_fingerprint_arg = script.rindex(
        "'-Phase1FingerprintPath', $phase1GodotFingerprintPath"
    )
    assert phase1_fingerprint_arg < phase1_fingerprint_written < recovery_fingerprint_arg
    assert "Stop-TestProcess $gatewayProcess" in script
    assert "Stop-TestProcess $workerProcess" in script
    assert "Stop-TestProcess $learnerProcess" in script
    assert script.index("Stop-TestProcess $gatewayProcess") < script.index(
        "$gatewayRestarted = Start-Process"
    )
    assert "@(Get-ListeningProcessIds $gatewayPort).Count -ne 0" in script
    assert "ConvertTo-StableJson $relaySideEffectFingerprintAfterRestart" in script
    assert "ConvertTo-StableJson $relayStatsAfterRestart" not in script
    assert "$projection.PSObject.Properties.Remove('capability_gets')" in script
    assert "expected_delta = 1" in script
    assert "$phase1RelayCapabilityGets + 1" in script
    assert "ConvertTo-StableJson $databaseFingerprintAfterRestart" in script
    assert "ConvertTo-StableJson $sandboxFingerprintAfterRestart" in script
    assert "ConvertTo-StableJson $artifactFingerprintAfterRestart" in script
    assert "recoveryFingerprint.no_mutating_flow_invoked" not in script
    assert "to_jsonb(command_row)" in script
    assert "to_jsonb(workflow_row)" in script
    assert "to_jsonb(receipt_row)" in script
    assert "to_jsonb(interaction_row)" in script
    assert "to_jsonb(learner_profile_row)" in script
    assert "to_jsonb(learner_job_row)" in script
    assert "to_jsonb(event_row)" in script
    assert "to_jsonb(stream_row)" in script
    assert "$expectedSessionCommandCount = 1" in script
    assert "$expectedBuildCommandCount = 2" in script
    assert "$expectedActivationCommandCount = 2" in script
    assert (
        "$expectedCommandCount = $expectedSessionCommandCount + "
        "$expectedBuildCommandCount + $expectedActivationCommandCount + "
        "$expectedTurnCount"
    ) in script
    assert "$expectedAppliedCommandCount = $expectedCommandCount - $expectedRejectedCommandCount" in script
    assert "$env:WALNUT_WORLD_SUCCESS_SCORE = '8'" in script
    assert "terminal_command_count -ne $expectedCommandCount" in script
    assert "applied_terminal_command_count -ne $expectedAppliedCommandCount" in script
    assert "rejected_terminal_command_count -ne $expectedRejectedCommandCount" in script
    assert "session_command_count -ne $expectedSessionCommandCount" in script
    assert "build_command_count -ne $expectedBuildCommandCount" in script
    assert "activation_command_count -ne $expectedActivationCommandCount" in script
    assert "$providerDispatchCount -ne $expectedRelayGenerationCount" in script
    assert "$providerDispatchCount -lt $expectedRelayGenerationCount" in script
    assert "$providerDispatchCount -gt $expectedRealProviderGenerationLimit" in script
    assert "$providerResultCount -lt $expectedProviderResultMinimum" in script
    assert "$providerResultCount -gt $providerDispatchCount" in script
    assert "relayStats.unique_dispatches -ne $providerDispatchCount" in script
    assert "relayStats.total_generations -ne [int]$relayStats.unique_dispatches" in script
    assert "sandbox_dispatch_receipts -ne $expectedRunCount" in script
    assert "build_certification_receipts -ne 2" in script
    assert "failure_run_count -ne $expectedFailureRunCount" in script
    assert "successful_run_count -ne 1" in script
    assert "same_failure_key_count -ne $expectedFailureRunCount" in script
    assert "distinct_failed_failure_keys -ne 1" in script
    assert "failure_count_sequence -ne $expectedFailureCountSequence" in script
    assert "interaction_role_sequence -ne $expectedInteractionRoleSequence" in script
    assert "learner_projection_terminal_closed -ne $expectedLearnerCount" in script
    assert "learner_revision -ne $expectedLearnerCount" in script
    assert "sandboxFingerprint.receipt_count -ne $expectedSandboxReceiptCount" in script
    assert "artifactFingerprint.receipt_count -ne 2" in script
    assert "world_id IS NULL" in script
    assert "-notin @('GET', 'HEAD', 'OPTIONS')" in script
    assert "$getCount -lt 8" in script
    assert "to_jsonb(relay_material)" in script
    assert "octet_length(request_body) AS request_body_length" in script
    assert "octet_length(response_body) AS response_body_length" in script
    assert "'last_error', last_error_json" in script
    assert script.index("REAL_GATEWAY_CHAIN_RECOVERY_PASS") < script.index(
        "$phase2HttpAudit = Get-GatewayRequestAudit"
    )
    assert script.index("$phase2HttpAudit = Get-GatewayRequestAudit") < script.index(
        "$databaseFingerprintAfterRestart = Invoke-DatabaseFingerprint"
    )
    assert script.index("Gateway/worker/Godot restart changed Provider generations") < script.index(
        "Restarted Gateway, workflow worker, or learner worker exited during recovery-only verification."
    )
    first_object, remaining_sql = script.split("SELECT (jsonb_build_object(", 1)[1].split(
        "\n) || jsonb_build_object(", 1
    )
    second_object, third_object = remaining_sql.split("\n) || jsonb_build_object(", 1)
    third_object = third_object.split("\n))::text;", 1)[0]
    field_groups = [
        re.findall(r"(?m)^  '([^']+)',", value)
        for value in (first_object, second_object, third_object)
    ]
    assert [len(group) for group in field_groups[:2]] == [50, 33]
    assert all(len(group) <= 50 for group in field_groups)
    fields = [field for group in field_groups for field in group]
    assert len(fields) >= 111
    assert len(set(fields)) == len(fields)
    fingerprint_function = script.split("function Invoke-DatabaseFingerprint", 1)[1].split(
        "function Wait-LearnerProjectionClosure", 1
    )[0]
    for required in (
        "convert_to((`n$statement`n)::text, 'UTF8')",
        "'base64'",
        "--tuples-only --no-align --no-psqlrc --quiet",
        "--set=ON_ERROR_STOP=1",
        "2> $stderrPath",
        "ConvertFrom-DatabaseFingerprintTransport $encodedText",
    ):
        assert required in fingerprint_function
    assert "2>&1" not in fingerprint_function
    assert "YAYA_AUTH_TOKEN = $rawAuthorization.Substring(7)" in script
    assert "[int]$relayFaultStats.reconcile_unavailable_attempted -ne 1" in script
    assert "[int]$relayFaultStats.reconcile_unavailable_delivered -ne 1" in script
    result_block = script.rsplit("$pendingPassResult = [ordered]@{", 1)[1].split("\n    }\n}", 1)[0]
    assert "authorization" not in result_block.lower()
    assert "forced_reconcile_unavailable_attempted" in result_block
    assert "forced_reconcile_unavailable_delivered" in result_block


def test_formal_command_accounting_closes_exact_public_operation_equation() -> None:
    script = HARNESS.read_text(encoding="utf-8")

    def constant(name: str) -> int:
        match = re.search(rf"\${name} = (\d+)", script)
        assert match is not None
        return int(match.group(1))

    def mode_counts(name: str) -> tuple[int, int]:
        match = re.search(
            rf"\${name} = if \(\$EnableSkillPatch\) \{{ (\d+) \}} else \{{ (\d+) \}}",
            script,
        )
        assert match is not None
        return int(match.group(1)), int(match.group(2))

    session_commands = constant("expectedSessionCommandCount")
    build_commands = constant("expectedBuildCommandCount")
    activation_commands = constant("expectedActivationCommandCount")
    m2_turn_commands, non_m2_turn_commands = mode_counts("expectedTurnCount")
    m2_rejected, non_m2_rejected = mode_counts("expectedRejectedCommandCount")
    m2_total = (
        session_commands + build_commands + activation_commands + m2_turn_commands
    )
    non_m2_total = (
        session_commands
        + build_commands
        + activation_commands
        + non_m2_turn_commands
    )

    assert (session_commands, build_commands, activation_commands) == (1, 2, 2)
    assert (m2_total, m2_total - m2_rejected, m2_rejected) == (11, 7, 4)
    assert (non_m2_total, non_m2_total - non_m2_rejected, non_m2_rejected) == (
        9,
        6,
        3,
    )
    assert "command_type='CREATE_AGENT_SESSION'" in script
    assert "command_type='CREATE_SKILL_BUILD'" in script
    assert "command_type='ACTIVATE_SKILL_VERSION'" in script
    assert "command_type='EXECUTE_AGENT_TURN'" in script
    assert "terminal_command_count -ne $expectedCommandCount" in script
    assert "applied_terminal_command_count -ne $expectedAppliedCommandCount" in script
    assert "rejected_terminal_command_count -ne $expectedRejectedCommandCount" in script
    assert "command_receipt_count -ne $expectedCommandCount" in script
    assert "applied_terminal_command_count -ne 6" not in script


def test_harness_keeps_world_commit_and_presentation_action_counts_distinct() -> None:
    script = HARNESS.read_text(encoding="utf-8")

    assert "$expectedWorldCommitEventCount = 1" in script
    assert "$expectedWorldPresentationEventCount = 8" in script
    assert "world_event_count -ne $expectedWorldCommitEventCount" in script
    assert (
        "presentation_event_count -ne $expectedWorldPresentationEventCount" in script
    )
    assert "world_event_count -ne 8" not in script


def test_harness_has_formal_m2_flags_counts_and_full_row_authority() -> None:
    script = HARNESS.read_text(encoding="utf-8")

    for required in (
        "[switch]$EnableSkillPatch",
        "$EnableSkillPatch -and -not $EnableWorldPresentation",
        "WALNUT_ENABLE_SKILL_PATCH",
        "$env:WALNUT_ENABLE_SKILL_PATCH = if ($EnableSkillPatch) { 'true' } else { 'false' }",
        "gateway_skill_patch_enabled",
        "worker_skill_patch_enabled",
        "$phase1FrontendArguments += '-EnableSkillPatch'",
        "$phase2FrontendArguments += '-EnableSkillPatch'",
        "$expectedRelayGenerationCount = if ($EnableSkillPatch) { 16 } else { 12 }",
        "$expectedTurnCount = if ($EnableSkillPatch) { 6 } else { 4 }",
        "$expectedRunCount = if ($EnableSkillPatch) { 5 } else { 4 }",
        "$expectedLearnerCount = if ($EnableSkillPatch) { 5 } else { 4 }",
        "$expectedFrontendPostCount = if ($EnableSkillPatch) { 12 } else { 9 }",
        "$expectedFrontendPutCount = if ($EnableSkillPatch) { 1 } else { 2 }",
        "$expectedSessionCommandCount = 1",
        "$expectedBuildCommandCount = 2",
        "$expectedActivationCommandCount = 2",
        "$expectedCommandCount = $expectedSessionCommandCount + $expectedBuildCommandCount + $expectedActivationCommandCount + $expectedTurnCount",
        "$expectedAppliedCommandCount = $expectedCommandCount - $expectedRejectedCommandCount",
        "$expectedFailureRunCount = if ($EnableSkillPatch) { 4 } else { 3 }",
        "$expectedSandboxReceiptCount = if ($EnableSkillPatch) { 10 } else { 8 }",
        "$expectedProviderResultMinimum = if ($EnableSkillPatch) { 11 } else { 8 }",
        "$expectedProductReceiptCount = if ($EnableSkillPatch) { 1 } else { 2 }",
        "$expectedEvidenceCount = if ($EnableSkillPatch) { 13 } else { 11 }",
        "$expectedPatchAuthorityCount = if ($EnableSkillPatch) { 1 } else { 0 }",
        "$expectedAssistedAuthorityCount = if ($EnableSkillPatch) { 1 } else { 0 }",
        "$expectedRealProviderGenerationLimit = if ($EnableSkillPatch) { 32 } else { 24 }",
        "patch_request_count -ne $expectedPatchAuthorityCount",
        "patch_proposal_count -ne $expectedPatchAuthorityCount",
        "patch_evidence_count -ne $expectedPatchAuthorityCount",
        "patch_decision_count -ne $expectedPatchAuthorityCount",
        "draft_revision_count -ne 3",
        "draft_assistance_count -ne $expectedPatchAuthorityCount",
        "patch_decision_receipt_count -ne $expectedPatchAuthorityCount",
        "build_provenance_count -ne 2",
        "build_terminal_authority_count -ne 2",
        "build_terminal_certified_count -ne 2",
        "certification_provenance_count -ne 2",
        "activation_provenance_count -ne 2",
        "run_provenance_count -ne $expectedRunCount",
        "assisted_build_count -ne $expectedAssistedAuthorityCount",
        "assisted_run_count -ne $expectedAssistedAuthorityCount",
        "relayStats.dispatch_puts -ne $expectedRelayGenerationCount",
        "relayStats.unique_dispatches -ne $expectedRelayGenerationCount",
        "$unexpectedGenerationRows.Count -ne 0",
        "phase1_skill_patch_exact_match",
        "PUBLIC_UI_CHAIN_CLOSED",
        "m2_full_row_authority_sha256",
        "command_receipt_count -ne $expectedCommandCount",
        "session_command_count -ne $expectedSessionCommandCount",
        "build_command_count -ne $expectedBuildCommandCount",
        "activation_command_count -ne $expectedActivationCommandCount",
        "applied_terminal_command_count -ne $expectedAppliedCommandCount",
        "session_commands = $expectedSessionCommandCount",
        "build_commands = $expectedBuildCommandCount",
        "activation_commands = $expectedActivationCommandCount",
        "turn_commands = $expectedTurnCount",
        "terminal_commands = $expectedCommandCount",
        "applied_terminal_commands = $expectedAppliedCommandCount",
        "rejected_terminal_commands = $expectedRejectedCommandCount",
        "command_receipts = $expectedCommandCount",
        "registry_entry_count -ne 2",
    ):
        assert required in script

    runbook = re.sub(r"\s+", " ", RUNBOOK.read_text(encoding="utf-8"))
    for documented_equation in (
        "one Session + two Builds + two Activations + six Turns = 11 terminal Commands",
        "seven Commands are `APPLIED`",
        "all 11 Commands have one durable command receipt each",
        "four = 9 terminal Commands",
        "six `APPLIED` and three `REJECTED`, with nine receipts",
    ):
        assert documented_equation in runbook

    full_row_aggregates = {
        "agent_sessions": ("session_row", "session_row.session_id"),
        "product_workspaces": ("workspace_row", "workspace_row.workspace_id"),
        "product_skill_drafts": ("draft_row", "draft_row.draft_row_id"),
        "product_skill_draft_revisions": (
            "draft_revision_row",
            "draft_revision_row.draft_revision_row_id",
        ),
        "product_skill_patch_requests": ("patch_request_row", "patch_request_row.request_id"),
        "product_skill_patch_proposals": (
            "patch_proposal_row",
            "patch_proposal_row.patch_id",
        ),
        "product_skill_patch_evidence": (
            "patch_evidence_row",
            "patch_evidence_row.patch_id, patch_evidence_row.evidence_id",
        ),
        "product_skill_patch_decisions": (
            "patch_decision_row",
            "patch_decision_row.decision_id",
        ),
        "product_draft_revision_assistance": (
            "draft_assistance_row",
            "draft_assistance_row.draft_revision_row_id",
        ),
        "skill_builds": ("build_row", "build_row.build_id"),
        "skill_build_provenance": (
            "build_provenance_row",
            "build_provenance_row.build_id",
        ),
        "skill_build_terminal_authority": (
            "build_terminal_authority_row",
            "build_terminal_authority_row.build_id",
        ),
        "skill_artifacts": (
            "artifact_row",
            "artifact_row.tenant_id, artifact_row.artifact_sha256",
        ),
        "skill_certifications": (
            "certification_row",
            "certification_row.certification_id",
        ),
        "skill_certification_provenance": (
            "certification_provenance_row",
            "certification_provenance_row.certification_id",
        ),
        "skill_activations": ("activation_row", "activation_row.activation_id"),
        "skill_activation_provenance": (
            "activation_provenance_row",
            "activation_provenance_row.activation_id",
        ),
        "game_runs": ("run_row", "run_row.run_id"),
        "game_evidence": ("evidence_row", "evidence_row.evidence_id"),
        "skill_run_provenance": ("run_provenance_row", "run_provenance_row.run_id"),
        "product_agent_interactions": (
            "interaction_row",
            "interaction_row.interaction_row_id",
        ),
        "learner_projection_jobs": ("learner_job_row", "learner_job_row.job_id"),
        "learner_profiles": (
            "learner_profile_row",
            "learner_profile_row.tenant_id, learner_profile_row.learner_id",
        ),
        "job_step_receipts": ("receipt_row", "receipt_row.receipt_id"),
        "idempotency_receipts": (
            "command_receipt_row",
            "command_receipt_row.receipt_id",
        ),
        "product_idempotency_receipts": (
            "product_receipt_row",
            "product_receipt_row.receipt_id",
        ),
        "product_patch_decision_receipts": (
            "patch_receipt_row",
            "patch_receipt_row.receipt_id",
        ),
        "world_snapshots": (
            "world_snapshot_row",
            "world_snapshot_row.tenant_id, world_snapshot_row.world_id",
        ),
        "world_streams": (
            "stream_row",
            "stream_row.tenant_id, stream_row.stream_id",
        ),
        "domain_events": (
            "event_row",
            "event_row.event_id",
        ),
        "registry_entries": (
            "registry_row",
            "registry_row.tenant_id, registry_row.actor_id, registry_row.content_hash, registry_row.world_id, registry_row.agent_profile_id, registry_row.revision",
        ),
        "world_presentation_streams": (
            "presentation_stream_row",
            "presentation_stream_row.tenant_id, presentation_stream_row.stream_id",
        ),
        "world_presentation_events": (
            "presentation_event_row",
            "presentation_event_row.event_id",
        ),
    }
    for table, (alias, order_by) in full_row_aggregates.items():
        assert f"jsonb_agg(to_jsonb({alias}) ORDER BY {order_by})::text" in script, table
    assert "jsonb_agg(to_jsonb(world_event_row) ORDER BY world_event_row.event_id)::text" in script


def test_harness_requires_and_records_exact_digest_pinned_images() -> None:
    script = HARNESS.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    postgres_image = (
        "postgres:16.9-alpine@sha256:"
        "7c688148e5e156d0e86df7ba8ae5a05a2386aaec1e2ad8e6d11bdf10504b1fb7"
    )
    sandbox_image = "gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c"

    assert f"[string]$PostgresImage = '{postgres_image}'" in script
    assert f"[string]$SandboxImage = '{sandbox_image}'" in script
    assert "function Test-DigestPinnedImage" in script
    assert "@sha256:[a-f0-9]{64}$" in script
    image_pattern_match = re.search(r"return \$Value -cmatch '([^']+)'", script)
    assert image_pattern_match is not None
    image_pattern = image_pattern_match.group(1)
    assert re.fullmatch(image_pattern, postgres_image) is not None
    assert re.fullmatch(image_pattern, sandbox_image) is not None
    for invalid in (
        "postgres:16.9-alpine",
        "gcc:latest",
        "gcc@sha256:not-a-digest",
        "GCC@sha256:" + "a" * 64,
    ):
        assert re.fullmatch(image_pattern, invalid) is None
    assert "$postgresImageDigestPinned = Test-DigestPinnedImage $PostgresImage" in script
    assert "$sandboxImageDigestPinned = Test-DigestPinnedImage $SandboxImage" in script
    for field, value in (
        ("postgres_image", "$PostgresImage"),
        ("postgres_image_digest_pinned", "$postgresImageDigestPinned"),
        ("sandbox_image", "$SandboxImage"),
        ("sandbox_image_digest_pinned", "$sandboxImageDigestPinned"),
    ):
        assert f"{field} = {value}" in script
    format_gate = "PostgreSQL and Sandbox images must use exact name@sha256:64hex identities."
    assert format_gate in script
    assert script.index(format_gate) < script.index("$postgresArguments = @(")
    assert script.index("$postgresImageDigestPinned = Test-DigestPinnedImage") < script.index(
        "docker image inspect $PostgresImage"
    )
    assert script.index("$sandboxImageDigestPinned = Test-DigestPinnedImage") < script.index(
        "docker image inspect $SandboxImage"
    )
    assert postgres_image in runbook
    assert sandbox_image in runbook


def test_running_docker_baseline_coexists_canonically_and_detects_conflict_or_drift(
    tmp_path: Path,
) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    baseline_functions = (
        "function Get-DockerProjectionProperty"
        + script.split("function Get-DockerProjectionProperty", 1)[1].split(
            "function Get-DirectoryFingerprint", 1
        )[0]
    )
    for required in (
        "@('ps', '--quiet', '--no-trunc')",
        "$inspectArguments = @('container', 'inspect') + @($firstIds)",
        "name = [string](Get-DockerProjectionProperty $resource 'Name' '')",
        "image_id = [string](Get-DockerProjectionProperty $resource 'Image' '')",
        "config_image = [string](Get-DockerProjectionProperty $config 'Image' '')",
        "labels = ConvertTo-DockerCanonicalStringMap",
        "path = [string](Get-DockerProjectionProperty $resource 'Path' '')",
        "args = @(",
        "mounts = $mountProjection",
        "published_ports = $publishedPortProjection",
        "network_endpoints = @($networkProjection)",
        "state = [ordered]@{",
        "restart_count = [int](Get-DockerProjectionProperty $resource 'RestartCount' 0)",
        "started_at = $startedAt",
        "canonical_utf8_base64",
        "sha256 = $sha256",
        "$secondIds = @(Get-DockerRunningIds $DockerExecutable)",
        "Running Docker container identities changed while the baseline was captured.",
    ):
        assert required in baseline_functions
    assert "Get-DockerProjectionProperty $health 'Log'" not in baseline_functions

    container_id = "a" * 64
    fixture = {
        "Id": container_id,
        "Name": "/unrelated-baseline",
        "Image": "b" * 64,
        "Config": {
            "Image": "example/image@sha256:" + "c" * 64,
            "Labels": {
                "walnut.int1.owner": "foreign-owner",
                "com.example.role": "unrelated",
            },
        },
        "Path": "/entrypoint",
        "Args": ["--serve", "value with spaces"],
        "Mounts": [
            {
                "Type": "volume",
                "Name": "foreign-data",
                "Source": "/var/lib/docker/volumes/foreign-data/_data",
                "Destination": "/data",
                "Driver": "local",
                "Mode": "z",
                "RW": True,
                "Propagation": "rprivate",
                "Consistency": "delegated",
            }
        ],
        "NetworkSettings": {
            "Ports": {
                "8080/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": "55432"},
                    {"HostIp": "0.0.0.0", "HostPort": "15432"},
                ],
                "9090/tcp": None,
            },
            "Networks": {
                "foreign-net": {
                    "IPAMConfig": {
                        "IPv4Address": "172.30.0.2",
                        "IPv6Address": "",
                        "LinkLocalIPs": ["169.254.2.2", "169.254.1.1"],
                    },
                    "NetworkID": "d" * 64,
                    "EndpointID": "e" * 64,
                    "Gateway": "172.30.0.1",
                    "IPAddress": "172.30.0.2",
                    "IPPrefixLen": 16,
                    "IPv6Gateway": "",
                    "GlobalIPv6Address": "",
                    "GlobalIPv6PrefixLen": 0,
                    "MacAddress": "02:42:ac:1e:00:02",
                    "GwPriority": 7,
                    "Aliases": ["z-alias", "a-alias"],
                    "DNSNames": ["z-name", "a-name"],
                    "DriverOpts": {"z": "2", "a": "1"},
                    "Links": ["z:link", "a:link"],
                }
            },
        },
        "State": {
            "Status": "running",
            "Running": True,
            "Paused": False,
            "Restarting": False,
            "OOMKilled": False,
            "Dead": False,
            "Pid": 4321,
            "ExitCode": 0,
            "Error": "",
            "StartedAt": "2026-08-15T00:00:00.000000000Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
            "Health": {
                "Status": "healthy",
                "FailingStreak": 0,
                "Log": [{"Output": "volatile-health-log-a"}],
            },
        },
        "RestartCount": 3,
    }
    second_fixture = json.loads(json.dumps(fixture))
    second_fixture["Id"] = "f" * 64
    second_fixture["Name"] = "/second-unrelated-baseline"
    second_fixture["Config"]["Labels"]["walnut.int1.owner"] = "second-foreign-owner"
    second_fixture["NetworkSettings"]["Ports"] = {}
    second_fixture["RestartCount"] = 0
    fixtures = [second_fixture, fixture]
    health_only_drift = json.loads(json.dumps(fixtures))
    health_only_drift[1]["State"]["Health"]["Log"][0]["Output"] = "volatile-health-log-b"

    probe = tmp_path / "docker-baseline-probe.ps1"
    probe.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        + baseline_functions
        + r"""
$script:containerIds = @($env:WALNUT_TEST_CONTAINER_IDS -split ',')
$script:inspectJson = [Text.UTF8Encoding]::new($false, $true).GetString(
    [Convert]::FromBase64String($env:WALNUT_TEST_BASELINE_JSON)
)
$script:baselineInspectJson = $script:inspectJson
$script:dockerCalls = [Collections.Generic.List[string]]::new()
function docker {
    $script:dockerCalls.Add(($args -join ' '))
    if ($args[0] -eq 'ps') {
        $global:LASTEXITCODE = 0
        Write-Output $script:containerIds
        return
    }
    if ($args[0] -eq 'container' -and $args[1] -eq 'inspect') {
        $requestedIds = @($args | Select-Object -Skip 2)
        if (($requestedIds -join "`n") -cne (($script:containerIds | Sort-Object) -join "`n")) {
            throw 'inspect did not receive every full sorted running identity'
        }
        $global:LASTEXITCODE = 0
        Write-Output $script:inspectJson
        return
    }
    throw "unexpected Docker call: $args"
}
function Invoke-DockerNativeCapture {
    param([string]$DockerExecutable, [string[]]$Arguments, [string]$Operation)
    $lines = @(& $DockerExecutable @Arguments)
    return [PSCustomObject]@{
        operation=$Operation
        exit_code=$global:LASTEXITCODE
        stdout=($lines -join "`n")
        stderr=''
    }
}

$baseline = Get-DockerRunningBaseline 'docker'
if (
    $baseline.count -ne 2 -or
    $baseline.ids[0] -cne ([string]::new('a', 64)) -or
    $baseline.ids[1] -cne ([string]::new('f', 64))
) {
    throw 'running baseline identity was not captured exactly'
}
$projectionJson = ConvertTo-Json -InputObject @($baseline.projection) -Compress -Depth 30
$canonicalBytes = [Text.UTF8Encoding]::new($false, $true).GetBytes($baseline.canonical_json)
$canonicalHasher = [Security.Cryptography.SHA256]::Create()
try {
    $canonicalHash = ([BitConverter]::ToString(
        $canonicalHasher.ComputeHash($canonicalBytes)
    )).Replace('-', '').ToLowerInvariant()
}
finally {
    $canonicalHasher.Dispose()
}
if (
    [string]$baseline.canonical_json -cne $projectionJson -or
    [string]$baseline.canonical_utf8_base64 -cne [Convert]::ToBase64String($canonicalBytes) -or
    [string]$baseline.sha256 -cne $canonicalHash
) { throw 'canonical JSON, strict UTF-8 bytes, base64, and SHA-256 were not closed exactly' }

$projection = $baseline.projection[0]
$mount = $projection.mounts[0]
$firstPort = $projection.published_ports[0]
$secondPort = $projection.published_ports[1]
$endpoint = $projection.network_endpoints[0]
if (
    [string]$projection.name -cne '/unrelated-baseline' -or
    [string]$projection.normalized_name -cne 'unrelated-baseline' -or
    [string]$projection.image_id -cne ([string]::new('b', 64)) -or
    [string]$projection.config_image -notlike 'example/image@sha256:*' -or
    (@($projection.labels.Keys) -join ',') -cne 'com.example.role,walnut.int1.owner' -or
    [string]$projection.labels['com.example.role'] -cne 'unrelated' -or
    [string]$projection.labels['walnut.int1.owner'] -cne 'foreign-owner' -or
    [string]$projection.path -cne '/entrypoint' -or
    (@($projection.args) -join '|') -cne '--serve|value with spaces' -or
    @($projection.mounts).Count -ne 1 -or
    [string]$mount.type -cne 'volume' -or
    [string]$mount.name -cne 'foreign-data' -or
    [string]$mount.source -cne '/var/lib/docker/volumes/foreign-data/_data' -or
    [string]$mount.destination -cne '/data' -or
    [string]$mount.driver -cne 'local' -or
    [string]$mount.mode -cne 'z' -or
    [bool]$mount.read_write -ne $true -or
    [string]$mount.propagation -cne 'rprivate' -or
    [string]$mount.consistency -cne 'delegated' -or
    @($projection.published_ports).Count -ne 2 -or
    [string]$firstPort.container_port -cne '8080/tcp' -or
    [string]$firstPort.host_ip -cne '0.0.0.0' -or
    [int]$firstPort.host_port -ne 15432 -or
    [string]$secondPort.container_port -cne '8080/tcp' -or
    [string]$secondPort.host_ip -cne '127.0.0.1' -or
    [int]$secondPort.host_port -ne 55432 -or
    @($projection.network_endpoints).Count -ne 1 -or
    [string]$endpoint.network_name -cne 'foreign-net' -or
    [string]$endpoint.ipam_config.ipv4_address -cne '172.30.0.2' -or
    (@($endpoint.ipam_config.link_local_ips) -join ',') -cne '169.254.1.1,169.254.2.2' -or
    [string]$endpoint.network_id -cne ([string]::new('d', 64)) -or
    [string]$endpoint.endpoint_id -cne ([string]::new('e', 64)) -or
    [string]$endpoint.gateway -cne '172.30.0.1' -or
    [string]$endpoint.ip_address -cne '172.30.0.2' -or
    [int]$endpoint.ip_prefix_length -ne 16 -or
    [string]$endpoint.mac_address -cne '02:42:ac:1e:00:02' -or
    [int]$endpoint.gateway_priority -ne 7 -or
    (@($endpoint.aliases) -join ',') -cne 'a-alias,z-alias' -or
    (@($endpoint.dns_names) -join ',') -cne 'a-name,z-name' -or
    (@($endpoint.driver_options.Keys) -join ',') -cne 'a,z' -or
    [string]$endpoint.driver_options['a'] -cne '1' -or
    [string]$endpoint.driver_options['z'] -cne '2' -or
    (@($endpoint.links) -join ',') -cne 'a:link,z:link' -or
    [string]$projection.state.status -cne 'running' -or
    [bool]$projection.state.running -ne $true -or
    [bool]$projection.state.paused -ne $false -or
    [bool]$projection.state.restarting -ne $false -or
    [bool]$projection.state.oom_killed -ne $false -or
    [bool]$projection.state.dead -ne $false -or
    [int64]$projection.state.pid -ne 4321 -or
    [int]$projection.state.exit_code -ne 0 -or
    [string]$projection.state.error -cne '' -or
    [string]$projection.state.started_at -cne '2026-08-15T00:00:00.000000000Z' -or
    [string]$projection.state.finished_at -cne '0001-01-01T00:00:00Z' -or
    [string]$projection.state.health.status -cne 'healthy' -or
    [int]$projection.state.health.failing_streak -ne 0 -or
    [int]$projection.restart_count -ne 3 -or
    [string]$projection.started_at -cne '2026-08-15T00:00:00.000000000Z'
) { throw 'canonical running baseline projection is incomplete or unsorted' }
if ((ConvertTo-Json $baseline.projection -Compress -Depth 30).Contains('volatile-health-log')) {
    throw 'health log entered the canonical baseline'
}

Assert-NoDiagnosticDockerConflict `
    $baseline `
    'walnut-int1-pg-fresh' `
    'run-int1-local-diagnostic' `
    @(8790, 20001, 20002, 20003)

$script:inspectJson = [Text.UTF8Encoding]::new($false, $true).GetString(
    [Convert]::FromBase64String($env:WALNUT_TEST_HEALTH_DRIFT_JSON)
)
$healthDrift = Assert-DockerBaselineRestored $baseline 'docker'
if ($healthDrift.sha256 -cne $baseline.sha256) {
    throw 'health-only drift changed the canonical baseline'
}

foreach ($case in @(
    [PSCustomObject]@{name='unrelated-baseline';owner='run-int1-local-diagnostic';ports=@(8790)},
    [PSCustomObject]@{name='walnut-int1-pg-fresh';owner='foreign-owner';ports=@(8790)},
    [PSCustomObject]@{name='walnut-int1-pg-fresh';owner='run-int1-local-diagnostic';ports=@(55432)}
)) {
        try {
            Assert-NoDiagnosticDockerConflict $baseline $case.name $case.owner $case.ports
            throw "Docker baseline conflict was accepted: $($case.name)|$($case.owner)|$($case.ports -join ',')"
    }
    catch {
        if ($_.Exception.Message -notlike 'Running Docker baseline conflicts*') { throw }
    }
}

$driftCases = @(
    [PSCustomObject]@{name='name'; mutate={param($value) $value[1].Name = '/drifted-name'}},
    [PSCustomObject]@{name='image'; mutate={param($value) $value[1].Config.Image = 'drift/image:latest'}},
    [PSCustomObject]@{name='labels'; mutate={param($value) $value[1].Config.Labels.'com.example.role' = 'drifted'}},
    [PSCustomObject]@{name='path'; mutate={param($value) $value[1].Path = '/drifted-entrypoint'}},
    [PSCustomObject]@{name='args'; mutate={param($value) $value[1].Args[1] = 'drifted-argument'}},
    [PSCustomObject]@{name='mounts'; mutate={param($value) $value[1].Mounts[0].Source = '/drifted-mount'}},
    [PSCustomObject]@{name='published-ports'; mutate={param($value) $value[1].NetworkSettings.Ports.'8080/tcp'[0].HostPort = '55433'}},
    [PSCustomObject]@{name='network-endpoints'; mutate={param($value) $value[1].NetworkSettings.Networks.'foreign-net'.IPAddress = '172.30.0.99'}},
    [PSCustomObject]@{name='state-health'; mutate={param($value) $value[1].State.Health.Status = 'unhealthy'}},
    [PSCustomObject]@{name='restart-count'; mutate={param($value) $value[1].RestartCount = 4}},
    [PSCustomObject]@{name='started-at'; mutate={param($value) $value[1].State.StartedAt = '2026-08-15T00:00:01.000000000Z'}}
)
foreach ($case in $driftCases) {
    $decodedCandidate = $script:baselineInspectJson | ConvertFrom-Json -ErrorAction Stop
    $candidate = @($decodedCandidate.GetEnumerator())
    & $case.mutate $candidate
    $script:inspectJson = ConvertTo-Json -InputObject @($candidate) -Compress -Depth 30
    try {
        Assert-DockerBaselineRestored $baseline 'docker' | Out-Null
        throw "Docker baseline $($case.name) drift was accepted"
    }
    catch {
        if ($_.Exception.Message -notlike 'Running Docker baseline changed*') { throw }
    }
}

$mutations = @(
    $script:dockerCalls | Where-Object {
        $_ -match '(^| )(stop|start|rm)( |$)' -or $_ -match '^volume rm( |$)'
    }
)
if ($mutations.Count -ne 0) {
    throw "unknown baseline entered a Docker mutation: $($mutations -join ',')"
}
$script:containerIds = @()
$emptyBaseline = Get-DockerRunningBaseline 'docker'
if (
    $emptyBaseline.count -ne 0 -or
    @($emptyBaseline.ids).Count -ne 0 -or
    [string]$emptyBaseline.canonical_json -cne '[]' -or
    $emptyBaseline.all_running -ne $true
) { throw 'empty running baseline was not closed canonically' }
Assert-DockerBaselineRestored $emptyBaseline 'docker' | Out-Null
Write-Output 'DOCKER_BASELINE_AUTHORITY_PASS'
""",
        encoding="utf-8-sig",
    )
    environment = os.environ.copy()
    environment["WALNUT_TEST_CONTAINER_IDS"] = f"{container_id},{'f' * 64}"
    environment["WALNUT_TEST_BASELINE_JSON"] = base64.b64encode(
        json.dumps(fixtures, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    environment["WALNUT_TEST_HEALTH_DRIFT_JSON"] = base64.b64encode(
        json.dumps(health_only_drift, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "DOCKER_BASELINE_AUTHORITY_PASS" in completed.stdout


def test_running_docker_baseline_uses_strict_native_utf8_and_closed_identity_set(
    tmp_path: Path,
) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    native_capture = (
        "function Invoke-DockerNativeCapture"
        + script.split("function Invoke-DockerNativeCapture", 1)[1].split(
            "function Remove-OwnedPostgresContainer", 1
        )[0]
    )
    baseline_functions = (
        "function Get-DockerProjectionProperty"
        + script.split("function Get-DockerProjectionProperty", 1)[1].split(
            "function Get-DirectoryFingerprint", 1
        )[0]
    )

    first_id = "a" * 64
    second_id = "f" * 64

    def fixture(container_id: str, name: str, *, cjk: bool) -> dict[str, object]:
        return {
            "Id": container_id,
            "Name": name,
            "Image": "b" * 64,
            "Config": {
                "Image": "example/native@sha256:" + "c" * 64,
                "Labels": {
                    "z.example": "末尾" if cjk else "tail",
                    "a.example": "中文标签" if cjk else "head",
                },
            },
            "Path": "/native-entrypoint",
            "Args": ["--student", "学生" if cjk else "learner"],
            "Mounts": [
                {
                    "Type": "bind",
                    "Name": "",
                    "Source": "C:/临时/数据" if cjk else "C:/native/data",
                    "Destination": "/data",
                    "Driver": "",
                    "Mode": "ro",
                    "RW": False,
                    "Propagation": "rprivate",
                    "Consistency": "consistent",
                }
            ],
            "NetworkSettings": {"Ports": {}, "Networks": {}},
            "State": {
                "Status": "running",
                "Running": True,
                "Paused": False,
                "Restarting": False,
                "OOMKilled": False,
                "Dead": False,
                "Pid": 1234,
                "ExitCode": 0,
                "Error": "",
                "StartedAt": "2026-08-15T01:00:00.000000000Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
                "Health": {
                    "Status": "healthy",
                    "FailingStreak": 0,
                    "Log": [{"Output": "不进入 authority"}],
                },
            },
            "RestartCount": 0,
        }

    fixtures = [
        fixture(second_id, "/native-second", cjk=False),
        fixture(first_id, "/native-first", cjk=True),
    ]
    missing = fixtures[:-1]
    extra = fixtures + [fixture("e" * 64, "/native-extra", cjk=False)]

    fake_script = tmp_path / "fake-docker.ps1"
    fake_script.write_text(
        r"""param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DockerArguments
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false, $true)
function Write-RawUtf8([string]$Text, [bool]$ToError = $false) {
    $bytes = $utf8.GetBytes($Text)
    $stream = if ($ToError) {
        [Console]::OpenStandardError()
    }
    else {
        [Console]::OpenStandardOutput()
    }
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush()
}
function Write-Base64Payload([string]$Encoded) {
    $bytes = [Convert]::FromBase64String($Encoded)
    $stream = [Console]::OpenStandardOutput()
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush()
}

$mode = $env:WALNUT_TEST_DOCKER_MODE
$ids = @($env:WALNUT_TEST_CONTAINER_IDS -split ',' | Sort-Object)
if ($mode -ceq 'invalid-utf8') {
    $stream = [Console]::OpenStandardOutput()
    $invalid = [byte[]]@(0xff)
    $stream.Write($invalid, 0, $invalid.Length)
    $stream.Flush()
    exit 0
}
if (
    $DockerArguments.Count -eq 3 -and
    $DockerArguments[0] -ceq 'ps' -and
    $DockerArguments[1] -ceq '--quiet' -and
    $DockerArguments[2] -ceq '--no-trunc'
) {
    if ($mode -ceq 'ps-exit') {
        Write-RawUtf8 'daemon unavailable' $true
        exit 9
    }
    $reportedIds = if ($mode -ceq 'ps-short') {
        @($ids[0].Substring(0, 12))
    }
    elseif ($mode -ceq 'ps-duplicate') {
        @($ids + $ids[0])
    }
    elseif ($mode -ceq 'second-ps-drift') {
        $counterPath = $env:WALNUT_TEST_PS_COUNTER_PATH
        $callCount = [int]([IO.File]::ReadAllText($counterPath))
        [IO.File]::WriteAllText(
            $counterPath,
            [string]($callCount + 1),
            [Text.Encoding]::ASCII
        )
        if ($callCount -eq 0) { @($ids) } else { @($ids[0]) }
    }
    else {
        @($ids)
    }
    Write-RawUtf8 (($reportedIds -join "`n") + "`n")
    if ($mode -ceq 'ps-stderr') {
        Write-RawUtf8 'unexpected warning' $true
    }
    exit 0
}
if (
    $DockerArguments.Count -ge 2 -and
    $DockerArguments[0] -ceq 'container' -and
    $DockerArguments[1] -ceq 'inspect'
) {
    $requested = @($DockerArguments | Select-Object -Skip 2)
    if (($requested -join "`n") -cne ($ids -join "`n")) {
        Write-RawUtf8 'inspect IDs were not the exact sorted full set' $true
        exit 65
    }
    if ($mode -ceq 'inspect-exit') {
        Write-RawUtf8 'inspect unavailable' $true
        exit 8
    }
    $encoded = if ($mode -ceq 'missing') {
        $env:WALNUT_TEST_MISSING_JSON
    }
    elseif ($mode -ceq 'extra') {
        $env:WALNUT_TEST_EXTRA_JSON
    }
    else {
        $env:WALNUT_TEST_BASELINE_JSON
    }
    Write-Base64Payload $encoded
    if ($mode -ceq 'inspect-stderr') {
        Write-RawUtf8 'unexpected inspect warning' $true
    }
    exit 0
}
Write-RawUtf8 "unexpected fake Docker invocation: $DockerArguments" $true
exit 64
""",
        encoding="utf-8-sig",
    )
    fake_docker = tmp_path / "fake-docker.cmd"
    fake_docker.write_text(
        r"""@echo off
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0fake-docker.ps1" %*
exit /b %ERRORLEVEL%
""",
        encoding="ascii",
    )

    probe = tmp_path / "docker-native-baseline-probe.ps1"
    probe.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        + native_capture
        + baseline_functions
        + r"""
$docker = $env:WALNUT_TEST_FAKE_DOCKER
$env:WALNUT_TEST_DOCKER_MODE = 'normal'
$baseline = Get-DockerRunningBaseline $docker
if (
    $baseline.count -ne 2 -or
    $baseline.ids[0] -cne ([string]::new('a', 64)) -or
    $baseline.ids[1] -cne ([string]::new('f', 64)) -or
    [string]$baseline.projection[0].labels['a.example'] -cne '中文标签' -or
    (@($baseline.projection[0].labels.Keys) -join ',') -cne 'a.example,z.example' -or
    (@($baseline.projection[0].args) -join '|') -cne '--student|学生' -or
    [string]$baseline.projection[0].mounts[0].source -cne 'C:/临时/数据' -or
    -not ([string]$baseline.canonical_json).Contains('中文标签') -or
    ([string]$baseline.canonical_json).Contains('不进入 authority')
) { throw 'strict native UTF-8 baseline projection was corrupted or incomplete' }
$canonicalBytes = [Text.UTF8Encoding]::new($false, $true).GetBytes($baseline.canonical_json)
$hasher = [Security.Cryptography.SHA256]::Create()
try {
    $canonicalHash = ([BitConverter]::ToString(
        $hasher.ComputeHash($canonicalBytes)
    )).Replace('-', '').ToLowerInvariant()
}
finally {
    $hasher.Dispose()
}
if (
    [Convert]::ToBase64String($canonicalBytes) -cne $baseline.canonical_utf8_base64 -or
    $canonicalHash -cne $baseline.sha256
) { throw 'native CJK canonical bytes/base64/hash did not close exactly' }
Assert-DockerBaselineRestored $baseline $docker | Out-Null

$failureCases = @(
    [PSCustomObject]@{mode='ps-stderr'; expected='Could not enumerate the complete running Docker container baseline*'},
    [PSCustomObject]@{mode='ps-exit'; expected='Could not enumerate the complete running Docker container baseline*'},
    [PSCustomObject]@{mode='ps-short'; expected='Docker returned a malformed, truncated, or duplicate running container identity.'},
    [PSCustomObject]@{mode='ps-duplicate'; expected='Docker returned a malformed, truncated, or duplicate running container identity.'},
    [PSCustomObject]@{mode='inspect-stderr'; expected='Could not inspect the complete running Docker container baseline*'},
    [PSCustomObject]@{mode='inspect-exit'; expected='Could not inspect the complete running Docker container baseline*'},
    [PSCustomObject]@{mode='missing'; expected='Docker inspection did not close the exact full running container identity set*'},
    [PSCustomObject]@{mode='extra'; expected='Docker inspection did not close the exact full running container identity set*'},
    [PSCustomObject]@{mode='second-ps-drift'; expected='Running Docker container identities changed while the baseline was captured.'},
    [PSCustomObject]@{mode='invalid-utf8'; expected='Docker native capture-running-container-ids output is not strict UTF-8.'}
)
foreach ($case in $failureCases) {
    $env:WALNUT_TEST_DOCKER_MODE = $case.mode
    [IO.File]::WriteAllText(
        $env:WALNUT_TEST_PS_COUNTER_PATH,
        '0',
        [Text.Encoding]::ASCII
    )
    try {
        Get-DockerRunningBaseline $docker | Out-Null
        throw "native Docker baseline $($case.mode) outcome was accepted"
    }
    catch {
        if ($_.Exception.Message -notlike $case.expected) { throw }
    }
}
Write-Output 'DOCKER_NATIVE_BASELINE_UTF8_PASS'
""",
        encoding="utf-8-sig",
    )
    environment = os.environ.copy()
    environment["WALNUT_TEST_FAKE_DOCKER"] = str(fake_docker)
    environment["WALNUT_TEST_CONTAINER_IDS"] = f"{second_id},{first_id}"
    environment["WALNUT_TEST_PS_COUNTER_PATH"] = str(tmp_path / "ps-counter.txt")
    for name, value in (
        ("BASELINE", fixtures),
        ("MISSING", missing),
        ("EXTRA", extra),
    ):
        environment[f"WALNUT_TEST_{name}_JSON"] = base64.b64encode(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "DOCKER_NATIVE_BASELINE_UTF8_PASS" in completed.stdout


def test_structured_pass_is_emitted_only_after_owned_cleanup_and_baseline_postcondition() -> None:
    script = HARNESS.read_text(encoding="utf-8")

    assert script.count('"INT1_LOCAL_DIAGNOSTIC_PASS " +') == 1
    pass_position = script.index('"INT1_LOCAL_DIAGNOSTIC_PASS " +')
    cleanup_position = script.rindex("Remove-OwnedPostgresVolume")
    postcondition_position = script.rindex("Assert-DockerBaselineRestored")
    pass_status_position = script.rindex("$pendingPassResult['status'] = 'PASS'")
    assert cleanup_position < postcondition_position < pass_position
    assert postcondition_position < pass_status_position < pass_position
    assert "status = 'PENDING_CLEANUP'" in script
    assert "status = 'PASS'" not in script[:postcondition_position]
    assert "$pendingPassResult" in script
    assert "$runFailure" in script
    assert "$cleanupFailure" in script

    final_cleanup_position = script.rindex("finally {")
    terminal_failure_position = script.index(
        "$terminalFailure = if ($null -ne $cleanupFailure)", final_cleanup_position
    )
    run_failure_fallback_position = script.index(
        "elseif ($null -ne $runFailure)", terminal_failure_position
    )
    failure_branch_position = script.index(
        "if ($null -ne $terminalFailure)", run_failure_fallback_position
    )
    fail_position = script.index('"INT1_LOCAL_DIAGNOSTIC_FAIL " +', failure_branch_position)
    failure_throw_position = script.index("throw $terminalFailure", fail_position)
    pending_pass_guard_position = script.index(
        "if ($null -eq $pendingPassResult", failure_throw_position
    )
    assert final_cleanup_position < cleanup_position < postcondition_position
    assert postcondition_position < terminal_failure_position
    assert terminal_failure_position < run_failure_fallback_position < failure_branch_position
    assert failure_branch_position < fail_position < failure_throw_position
    assert failure_throw_position < pending_pass_guard_position < pass_status_position
    failure_branch = script[failure_branch_position:failure_throw_position]
    assert "INT1_LOCAL_DIAGNOSTIC_PASS" not in failure_branch
    assert "$pendingPassResult['status'] = 'PASS'" not in failure_branch


def test_deterministic_postgres_outage_gate_is_real_read_only_and_disposable() -> None:
    script = HARNESS.read_text(encoding="utf-8")

    for required in (
        '$postgresVolumeName = "walnut-int1-pgdata-',
        "function New-OwnedPostgresVolume",
        '"walnut.int1.run_id=$runId"',
        '"walnut.int1.owner=$postgresResourceOwner"',
        "type=volume,source=$postgresVolumeName,target=/var/lib/postgresql/data",
        "Wait-ServicePostgresConnections",
        "@('stop', '--timeout', '5', $postgresId)",
        "Wait-LocalPortClosed $postgresPort 15",
        "'{{.State.Status}}' $postgresId",
        "@('start', $postgresId)",
        "Wait-PostgresHealthy $postgresId 45",
        "'{{.Id}}' $postgresId",
        "$postgresIdAfterRestart -cne $postgresId",
        "service_database_connections_after_restart",
        "INT1_LOCAL_DIAGNOSTIC_DATABASE_OUTAGE_FINGERPRINT",
        "PostgreSQL stop/start recovery changed relay, database, Sandbox, or Artifact side effects.",
        "database_outage_recovery = $databaseOutageRecovery",
        "Invoke-DockerNativeCapture",
        "@('rm', '--force', $ownedId)",
        "@('volume', 'rm', '--force', $Name)",
    ):
        assert required in script

    assert "--tmpfs" not in script
    postgres_arguments = script.split("$postgresArguments = @(", 1)[1].split(")", 1)[0]
    assert "'--rm'" not in postgres_arguments

    outage_gate = script.split("$databaseOutageRecovery = $null", 1)[1].split(
        "Stop-TestProcess $gatewayProcess", 1
    )[0]
    assert "if (-not $RealProvider)" in outage_gate
    assert "if ($RealProvider)" not in outage_gate
    assert "-Method Get" in outage_gate
    for mutating_method in ("Post", "Put", "Patch", "Delete"):
        assert f"-Method {mutating_method}" not in outage_gate
    for process in ("$gatewayProcess", "$workerProcess", "$learnerProcess"):
        assert f"{process}.HasExited" in outage_gate
    for service in ("gateway", "workflow_worker", "learner_worker"):
        assert f"{service} = $phase1" in outage_gate

    ordered_evidence = (
        "$postgresAuthorityBeforeStop = Get-DockerResourceAuthority",
        "@('stop', '--timeout', '5', $postgresId)",
        "Wait-LocalPortClosed $postgresPort 15",
        "postgres-outage-observed",
        "$postgresAuthorityBeforeStart = Get-DockerResourceAuthority",
        "@('start', $postgresId)",
        "Wait-PostgresHealthy $postgresId 45",
        "Wait-ServicePostgresConnections",
        "$databaseFingerprintAfterOutage = Invoke-DatabaseFingerprint",
        "$databaseOutageFingerprintComparison = [ordered]@{",
        "postgres-gateway-workflow-learner-recovered",
    )
    positions = [outage_gate.index(value) for value in ordered_evidence]
    assert positions == sorted(positions)
    assert (
        '$postgresAuthorityBeforeStop -cne "$postgresId|$runId|$postgresResourceOwner"'
        in outage_gate
    )
    assert (
        '$postgresAuthorityBeforeStart -cne "$postgresId|$runId|$postgresResourceOwner"'
        in outage_gate
    )
    assert "Get-DockerResourceAuthority 'container' $postgresId" in outage_gate
    assert "$stoppedPostgresId -cne $postgresId" in outage_gate
    assert "$startedPostgresId -cne $postgresId" in outage_gate
    assert "docker stop" not in outage_gate
    assert "docker start" not in outage_gate
    assert "$dockerBaseline" not in outage_gate

    for authority in ("relay", "database", "sandbox", "artifact"):
        assert (
            f"$databaseOutageFingerprintComparison.{authority}.unchanged -eq $true" in outage_gate
        )

    container_cleanup = script.split("function Remove-OwnedPostgresContainer", 1)[1].split(
        "function Remove-OwnedPostgresVolume", 1
    )[0]
    volume_cleanup = script.split("function Remove-OwnedPostgresVolume", 1)[1].split(
        "function Assert-ProviderSecretText", 1
    )[0]
    final_cleanup = script.rsplit("finally {", 1)[1]
    for required in (
        "-not $Created",
        "$ContainerId -cnotmatch '^[a-f0-9]{64}$'",
        "Get-ExactOwnedPostgresContainerId $Name $RunId $Owner",
        "$ContainerId -cne $ownedId",
        "Invoke-DockerNativeCapture",
        "@('rm', '--force', $ownedId)",
        "$removeExitCode = [int]$removal.exit_code",
        "Test-DockerContainerAbsent $ownedId $DockerExecutable",
    ):
        assert required in container_cleanup
    for required in (
        "-not $Created",
        "-not $AbsentBeforeCreate",
        "Get-DockerResourceAuthority 'volume' $Name",
        '$authority -cne "$Name|$RunId|$Owner"',
        "Invoke-DockerNativeCapture",
        "@('volume', 'rm', '--force', $Name)",
        "$removeExitCode = [int]$removal.exit_code",
        "Test-DockerVolumeAbsent $Name $DockerExecutable",
    ):
        assert required in volume_cleanup
    assert "rm --force $Name" not in container_cleanup
    assert "volume rm --force $ContainerId" not in volume_cleanup
    assert final_cleanup.index("Remove-OwnedPostgresContainer") < final_cleanup.index(
        "Remove-OwnedPostgresVolume"
    )
    assert "$postgresCreated" in final_cleanup
    assert "$postgresId" in final_cleanup
    assert "$postgresVolumeCreated" in final_cleanup
    assert "$postgresVolumeAbsentBeforeCreate" in final_cleanup
    assert "Refusing to reuse a preexisting PostgreSQL data volume." in script
    assert "Test-DockerVolumeAbsent $postgresVolumeName" in script
    assert "function Get-DockerResourceAuthority" in script
    assert '$startInfo.Arguments = "$Kind inspect $Name"' in script
    assert "$labels.PSObject.Properties['walnut.int1.run_id']" in script
    assert "$labels.PSObject.Properties['walnut.int1.owner']" in script
    assert "{{index .Labels" not in script
    assert "{{index .Config.Labels" not in script
    assert "Start-Process -FilePath $DockerExecutable" in script
    assert "-RedirectStandardOutput $stdoutPath" in script
    assert "-RedirectStandardError $stderrPath" in script
    assert "Error response from daemon: get ${Name}: no such volume" in script
    assert "$postgresVolumeAbsentBeforeCreate = $true" in script
    assert script.index("Test-DockerVolumeAbsent $postgresVolumeName") < script.index(
        "$postgresVolumeAbsentBeforeCreate = $true", script.index("try {")
    )


def test_postgres_volume_absence_probe_distinguishes_winps_native_outcomes(
    tmp_path: Path,
) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    probe_function = (
        "function Test-DockerVolumeAbsent"
        + script.split("function Test-DockerVolumeAbsent", 1)[1].split(
            "function Assert-ProviderSecretText", 1
        )[0]
    )
    fake_docker = tmp_path / "fake-docker.cmd"
    fake_docker.write_text(
        r"""@echo off
if not "%1"=="volume" exit /b 64
if not "%2"=="inspect" exit /b 65
if "%WALNUT_TEST_DOCKER_MODE%"=="absent" (
  >&2 echo Error response from daemon: get %5: no such volume
  exit /b 1
)
if "%WALNUT_TEST_DOCKER_MODE%"=="exists" (
  echo %5
  exit /b 0
)
>&2 echo Error response from daemon: Docker Desktop is unavailable
exit /b 1
""",
        encoding="ascii",
    )
    winps_probe = tmp_path / "postgres-volume-absence-probe.ps1"
    winps_probe.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        + probe_function
        + r"""
$name = 'walnut-int1-pgdata-probe'
$docker = $env:WALNUT_TEST_FAKE_DOCKER
$env:WALNUT_TEST_DOCKER_MODE = 'absent'
if ((Test-DockerVolumeAbsent $name $docker) -ne $true) {
    throw 'expected absent volume was not classified as absent'
}
$env:WALNUT_TEST_DOCKER_MODE = 'exists'
if ((Test-DockerVolumeAbsent $name $docker) -ne $false) {
    throw 'existing volume was not classified as existing'
}
$env:WALNUT_TEST_DOCKER_MODE = 'inspect-error'
try {
    Test-DockerVolumeAbsent $name $docker | Out-Null
    throw 'unexpected inspect failure was accepted as absence'
}
catch {
    if ($_.Exception.Message -notlike 'Could not prove the PostgreSQL data volume was absent*') {
        throw
    }
}
Write-Output 'POSTGRES_VOLUME_ABSENCE_PROBE_PASS'
""",
        encoding="utf-8-sig",
    )
    environment = os.environ.copy()
    environment["WALNUT_TEST_FAKE_DOCKER"] = str(fake_docker)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(winps_probe),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "POSTGRES_VOLUME_ABSENCE_PROBE_PASS" in completed.stdout


def test_docker_resource_authority_reads_exact_native_container_and_volume_json(
    tmp_path: Path,
) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    authority_function = (
        "function Get-DockerResourceAuthority"
        + script.split("function Get-DockerResourceAuthority", 1)[1].split(
            "function Test-DockerContainerAbsent", 1
        )[0]
    )
    fake_docker = tmp_path / "fake-docker.cmd"
    fake_docker.write_text(
        r"""@echo off
if "%WALNUT_TEST_DOCKER_MODE%"=="container" (
  echo [{"Id":"%WALNUT_TEST_CONTAINER_ID%","Name":"/walnut-int1-pg-probe","Config":{"Labels":{"walnut.int1.run_id":"%WALNUT_TEST_RUN_ID%","walnut.int1.owner":"run-int1-local-diagnostic"}}}]
  exit /b 0
)
if "%WALNUT_TEST_DOCKER_MODE%"=="volume" (
  echo [{"Name":"%3","Labels":{"walnut.int1.run_id":"%WALNUT_TEST_RUN_ID%","walnut.int1.owner":"run-int1-local-diagnostic"}}]
  exit /b 0
)
if "%WALNUT_TEST_DOCKER_MODE%"=="wrong-owner" (
  echo [{"Id":"%WALNUT_TEST_CONTAINER_ID%","Name":"/walnut-int1-pg-probe","Config":{"Labels":{"walnut.int1.run_id":"%WALNUT_TEST_RUN_ID%","walnut.int1.owner":"unknown-owner"}}}]
  exit /b 0
)
>&2 echo unexpected fake Docker mode
exit /b 9
""",
        encoding="ascii",
    )
    probe = tmp_path / "docker-resource-authority-probe.ps1"
    probe.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        + authority_function
        + r"""
$docker = $env:WALNUT_TEST_FAKE_DOCKER
$containerId = $env:WALNUT_TEST_CONTAINER_ID
$runId = $env:WALNUT_TEST_RUN_ID
$owner = 'run-int1-local-diagnostic'
$env:WALNUT_TEST_DOCKER_MODE = 'container'
$containerAuthority = Get-DockerResourceAuthority 'container' 'walnut-int1-pg-probe' $docker
if ($containerAuthority -cne "$containerId|$runId|$owner") {
    throw 'container authority did not preserve exact native inspect identity and labels'
}
$containerIdAuthority = Get-DockerResourceAuthority 'container' $containerId $docker
if ($containerIdAuthority -cne "$containerId|$runId|$owner") {
    throw 'container authority did not preserve an exact full-ID request'
}
$env:WALNUT_TEST_DOCKER_MODE = 'volume'
$volumeAuthority = Get-DockerResourceAuthority 'volume' 'walnut-int1-pgdata-probe' $docker
if ($volumeAuthority -cne "walnut-int1-pgdata-probe|$runId|$owner") {
    throw 'volume authority did not preserve exact native inspect identity and labels'
}
$env:WALNUT_TEST_DOCKER_MODE = 'wrong-owner'
$wrongOwner = Get-DockerResourceAuthority 'container' 'walnut-int1-pg-probe' $docker
if ($wrongOwner -cne "$containerId|$runId|unknown-owner") {
    throw 'authority reader did not expose the exact mismatching owner for fail-closed comparison'
}
Write-Output 'DOCKER_RESOURCE_AUTHORITY_PASS'
""",
        encoding="utf-8-sig",
    )
    environment = os.environ.copy()
    environment["WALNUT_TEST_FAKE_DOCKER"] = str(fake_docker)
    environment["WALNUT_TEST_CONTAINER_ID"] = "a" * 64
    environment["WALNUT_TEST_RUN_ID"] = "b" * 32
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "DOCKER_RESOURCE_AUTHORITY_PASS" in completed.stdout


def test_owned_cleanup_honors_real_native_remove_exit_codes_without_docker(
    tmp_path: Path,
) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    cleanup_functions = (
        "function Invoke-DockerNativeCapture"
        + script.split("function Invoke-DockerNativeCapture", 1)[1].split(
            "function Assert-ProviderSecretText", 1
        )[0]
    )
    fake_docker = tmp_path / "fake-docker.cmd"
    fake_docker.write_text(
        r"""@echo off
if "%WALNUT_TEST_DOCKER_MODE%"=="fail" (
  >&2 echo native remove failed
  exit /b 9
)
if "%WALNUT_TEST_DOCKER_MODE%"=="malformed" (
  echo wrong-resource
  exit /b 0
)
if "%1"=="rm" (
  echo %3
  exit /b 0
)
if "%1"=="volume" if "%2"=="rm" (
  echo %4
  exit /b 0
)
>&2 echo unexpected fake Docker invocation
exit /b 64
""",
        encoding="ascii",
    )
    probe = tmp_path / "owned-cleanup-native-exit-probe.ps1"
    probe.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        + cleanup_functions
        + r"""
function Get-DockerResourceAuthority {
    param([string]$Kind, [string]$Name, [string]$DockerExecutable = 'docker.exe')
    if ($Kind -eq 'container') { return "$script:containerId|$script:runId|$script:owner" }
    if ($Kind -eq 'volume') { return "$Name|$script:runId|$script:owner" }
    throw 'unexpected resource kind'
}
function Test-DockerContainerAbsent {
    param([string]$ContainerId, [string]$DockerExecutable = 'docker.exe')
    return $true
}
function Test-DockerVolumeAbsent {
    param([string]$Name, [string]$DockerExecutable = 'docker.exe')
    return $true
}
$script:containerId = [string]::new('a', 64)
$script:runId = [string]::new('b', 32)
$script:owner = 'run-int1-local-diagnostic'
$containerName = 'walnut-int1-pg-bbbbbbbbbbbb'
$volumeName = 'walnut-int1-pgdata-bbbbbbbbbbbb'
$docker = $env:WALNUT_TEST_FAKE_DOCKER
$env:WALNUT_TEST_DOCKER_MODE = 'success'
Remove-OwnedPostgresContainer $true $containerName $script:containerId $script:runId $script:owner $docker
Remove-OwnedPostgresVolume $true $true $volumeName $script:runId $script:owner $docker
foreach ($mode in @('fail', 'malformed')) {
    $env:WALNUT_TEST_DOCKER_MODE = $mode
    try {
        Remove-OwnedPostgresContainer $true $containerName $script:containerId $script:runId $script:owner $docker
        throw "native $mode container removal was accepted"
    }
    catch {
        if ($_.Exception.Message -notlike 'Failed to remove the exact owned PostgreSQL container*') {
            throw
        }
    }
}
Write-Output 'OWNED_CLEANUP_NATIVE_EXIT_PASS'
""",
        encoding="utf-8-sig",
    )
    environment = os.environ.copy()
    environment["WALNUT_TEST_FAKE_DOCKER"] = str(fake_docker)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "OWNED_CLEANUP_NATIVE_EXIT_PASS" in completed.stdout


def test_postgres_partial_create_failures_retry_exact_owned_cleanup_without_docker(
    tmp_path: Path,
) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    for required in (
        "function New-OwnedPostgresVolume",
        "function Start-OwnedPostgresContainer",
        "$postgresCreated = $false",
        "Remove-OwnedPostgresContainer `\n            $postgresCreated",
    ):
        assert required in script

    docker_functions = (
        "function Get-DockerResourceAuthority"
        + script.split("function Get-DockerResourceAuthority", 1)[1].split(
            "function Assert-ProviderSecretText", 1
        )[0]
    )
    fake_script = tmp_path / "fake-docker-partial-create.ps1"
    fake_script.write_text(
        r"""param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DockerArguments
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false, $true)
$stateRoot = $env:WALNUT_TEST_FAKE_STATE_ROOT
function Write-RawUtf8([string]$Text, [bool]$ToError = $false) {
    $bytes = $utf8.GetBytes($Text)
    $stream = if ($ToError) {
        [Console]::OpenStandardError()
    }
    else {
        [Console]::OpenStandardOutput()
    }
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush()
}
function Get-State([string]$Name, [string]$DefaultValue) {
    $path = Join-Path $stateRoot $Name
    if (-not [IO.File]::Exists($path)) { return $DefaultValue }
    return [IO.File]::ReadAllText($path)
}
function Set-State([string]$Name, [string]$Value) {
    [IO.File]::WriteAllText((Join-Path $stateRoot $Name), $Value, [Text.Encoding]::ASCII)
}
function Add-Mutation([string]$Value) {
    [IO.File]::AppendAllText(
        (Join-Path $stateRoot 'mutations.log'),
        "$Value`n",
        [Text.Encoding]::ASCII
    )
}

$mode = $env:WALNUT_TEST_DOCKER_MODE
$runId = $env:WALNUT_TEST_RUN_ID
$owner = if ($mode -ceq 'mismatch-labels') {
    'unknown-owner'
}
else {
    'run-int1-local-diagnostic'
}
$containerId = $env:WALNUT_TEST_CONTAINER_ID
$replacementContainerId = $env:WALNUT_TEST_REPLACEMENT_CONTAINER_ID
$containerName = $env:WALNUT_TEST_CONTAINER_NAME
$volumeName = $env:WALNUT_TEST_VOLUME_NAME

if ($DockerArguments[0] -ceq 'volume' -and $DockerArguments[1] -ceq 'create') {
    Set-State 'volume-exists' '1'
    Set-State 'volume-inspects' '0'
    if ($mode -ceq 'volume-malformed-stdout') {
        Write-RawUtf8 "wrong-volume`n"
    }
    else {
        Write-RawUtf8 "$volumeName`n"
    }
    if ($mode -ceq 'volume-stderr') {
        Write-RawUtf8 'unexpected create warning' $true
    }
    exit 0
}
if ($DockerArguments[0] -ceq 'volume' -and $DockerArguments[1] -ceq 'inspect') {
    if ($DockerArguments.Count -ge 3 -and $DockerArguments[2] -ceq '--format') {
        if ((Get-State 'volume-exists' '0') -ceq '0') {
            Write-RawUtf8 "Error response from daemon: get ${volumeName}: no such volume" $true
            exit 1
        }
        Write-RawUtf8 "$volumeName`n"
        exit 0
    }
    $inspectCount = [int](Get-State 'volume-inspects' '0')
    Set-State 'volume-inspects' ([string]($inspectCount + 1))
    if ($mode -ceq 'volume-probe-retry' -and $inspectCount -eq 0) {
        Write-RawUtf8 'transient volume inspect failure' $true
        exit 9
    }
    $resource = @([ordered]@{
        Name = $volumeName
        Labels = [ordered]@{
            'walnut.int1.run_id' = $runId
            'walnut.int1.owner' = $owner
        }
    })
    Write-RawUtf8 (ConvertTo-Json -InputObject $resource -Compress -Depth 5)
    exit 0
}
if (
    $DockerArguments[0] -ceq 'volume' -and
    $DockerArguments[1] -ceq 'rm' -and
    $DockerArguments[2] -ceq '--force'
) {
    Add-Mutation "volume-rm:$($DockerArguments[3])"
    Set-State 'volume-exists' '0'
    Write-RawUtf8 "$($DockerArguments[3])`n"
    exit 0
}
if ($DockerArguments[0] -ceq 'run') {
    Set-State 'container-exists' '1'
    Set-State 'container-inspects' '0'
    Write-RawUtf8 'container was created but failed to start' $true
    exit 7
}
if ($DockerArguments[0] -ceq 'container' -and $DockerArguments[1] -ceq 'inspect') {
    if ($DockerArguments.Count -ge 3 -and $DockerArguments[2] -ceq '--format') {
        if ((Get-State 'container-exists' '0') -ceq '0') {
            Write-RawUtf8 "Error: No such object: $containerId" $true
            exit 1
        }
        Write-RawUtf8 "$containerId`n"
        exit 0
    }
    if ((Get-State 'container-exists' '0') -ceq '0') {
        Write-RawUtf8 "Error: No such object: $containerName" $true
        exit 1
    }
    $containerInspectCount = [int](Get-State 'container-inspects' '0')
    Set-State 'container-inspects' ([string]($containerInspectCount + 1))
    if ($mode -ceq 'run-probe-retry' -and $containerInspectCount -eq 0) {
        Write-RawUtf8 'transient container inspect failure' $true
        exit 9
    }
    $inspectedContainerId = if (
        $mode -ceq 'identity-swap' -and
        $containerInspectCount -gt 0
    ) {
        $replacementContainerId
    }
    else {
        $containerId
    }
    $resource = @([ordered]@{
        Id = $inspectedContainerId
        Name = "/$containerName"
        Config = [ordered]@{
            Labels = [ordered]@{
                'walnut.int1.run_id' = $runId
                'walnut.int1.owner' = $owner
            }
        }
    })
    Write-RawUtf8 (ConvertTo-Json -InputObject $resource -Compress -Depth 6)
    exit 0
}
if (
    $DockerArguments[0] -ceq 'rm' -and
    $DockerArguments[1] -ceq '--force'
) {
    Add-Mutation "container-rm:$($DockerArguments[2])"
    Set-State 'container-exists' '0'
    Write-RawUtf8 "$($DockerArguments[2])`n"
    exit 0
}
Write-RawUtf8 "unexpected fake Docker invocation: $DockerArguments" $true
exit 64
""",
        encoding="utf-8-sig",
    )
    fake_docker = tmp_path / "fake-docker-partial-create.cmd"
    fake_docker.write_text(
        r"""@echo off
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0fake-docker-partial-create.ps1" %*
exit /b %ERRORLEVEL%
""",
        encoding="ascii",
    )

    probe = tmp_path / "postgres-partial-create-cleanup-probe.ps1"
    probe.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        + docker_functions
        + r"""
$docker = $env:WALNUT_TEST_FAKE_DOCKER
$runId = $env:WALNUT_TEST_RUN_ID
$owner = 'run-int1-local-diagnostic'
$containerId = $env:WALNUT_TEST_CONTAINER_ID
$containerName = $env:WALNUT_TEST_CONTAINER_NAME
$volumeName = $env:WALNUT_TEST_VOLUME_NAME
$mutationsPath = Join-Path $env:WALNUT_TEST_FAKE_STATE_ROOT 'mutations.log'

$env:WALNUT_TEST_DOCKER_MODE = 'volume-probe-retry'
$volumeCreated = $false
try {
    New-OwnedPostgresVolume `
        $volumeName $runId $owner ([ref]$volumeCreated) $docker | Out-Null
    throw 'first volume authority failure was accepted'
}
catch {
    if ($_.Exception.Message -notlike 'Created PostgreSQL data volume does not have*') { throw }
}
if (-not $volumeCreated) {
    throw 'native volume creation success was not captured before the first authority probe'
}
Remove-OwnedPostgresVolume $volumeCreated $true $volumeName $runId $owner $docker
$volumeMutations = @([IO.File]::ReadAllLines($mutationsPath) | Where-Object { $_ })
if (
    $volumeMutations.Count -ne 1 -or
    $volumeMutations[0] -cne "volume-rm:$volumeName"
) { throw 'retry cleanup did not remove only the exact owned volume' }

foreach ($volumeMode in @('volume-malformed-stdout', 'volume-stderr')) {
    [IO.File]::WriteAllText($mutationsPath, '', [Text.Encoding]::ASCII)
    $env:WALNUT_TEST_DOCKER_MODE = $volumeMode
    $presentationFailureCreated = $false
    try {
        New-OwnedPostgresVolume `
            $volumeName $runId $owner ([ref]$presentationFailureCreated) $docker | Out-Null
        throw "exit-zero $volumeMode was accepted"
    }
    catch {
        if ($_.Exception.Message -notlike 'Failed to create the exact disposable*') { throw }
    }
    if (-not $presentationFailureCreated) {
        throw "exit-zero $volumeMode did not capture cleanup authority"
    }
    Remove-OwnedPostgresVolume `
        $presentationFailureCreated $true $volumeName $runId $owner $docker
    $presentationMutations = @(
        [IO.File]::ReadAllLines($mutationsPath) | Where-Object { $_ }
    )
    if (
        $presentationMutations.Count -ne 1 -or
        $presentationMutations[0] -cne "volume-rm:$volumeName"
    ) { throw "exit-zero $volumeMode was not cleaned by exact ownership" }
}

[IO.File]::WriteAllText($mutationsPath, '', [Text.Encoding]::ASCII)
$env:WALNUT_TEST_DOCKER_MODE = 'run-start-fail'
$containerCreated = $false
$containerStarted = $false
$capturedContainerId = $null
$arguments = @(
    'run', '--detach', '--name', $containerName,
    '--label', "walnut.int1.run_id=$runId",
    '--label', "walnut.int1.owner=$owner",
    'example/postgres@sha256:' + ([string]::new('c', 64))
)
try {
    Start-OwnedPostgresContainer `
        $containerName `
        $runId `
        $owner `
        $arguments `
        ([ref]$containerCreated) `
        ([ref]$containerStarted) `
        ([ref]$capturedContainerId) `
        $docker | Out-Null
    throw 'created-but-not-started container was accepted'
}
catch {
    if ($_.Exception.Message -notlike 'Failed to start fresh disposable PostgreSQL*') { throw }
}
if (
    -not $containerCreated -or
    $containerStarted -or
    [string]$capturedContainerId -cne $containerId
) { throw 'container created/started/identity authority was not distinguished' }
Remove-OwnedPostgresContainer `
    $containerCreated $containerName $capturedContainerId $runId $owner $docker
$containerMutations = @([IO.File]::ReadAllLines($mutationsPath) | Where-Object { $_ })
if (
    $containerMutations.Count -ne 1 -or
    $containerMutations[0] -cne "container-rm:$containerId"
) { throw 'created-but-not-started cleanup did not target only the captured full identity' }

[IO.File]::WriteAllText($mutationsPath, '', [Text.Encoding]::ASCII)
$env:WALNUT_TEST_DOCKER_MODE = 'run-probe-retry'
$retryCreated = $false
$retryStarted = $false
$retryId = $null
try {
    Start-OwnedPostgresContainer `
        $containerName `
        $runId `
        $owner `
        $arguments `
        ([ref]$retryCreated) `
        ([ref]$retryStarted) `
        ([ref]$retryId) `
        $docker | Out-Null
    throw 'transient first container authority failure was accepted'
}
catch {
    if ($_.Exception.Message -notlike 'Created PostgreSQL container does not have*') { throw }
}
if (-not $retryCreated -or $retryStarted -or $null -ne $retryId) {
    throw 'transient first container authority failure lost created/started authority'
}
Remove-OwnedPostgresContainer `
    $retryCreated $containerName $retryId $runId $owner $docker
$retryMutations = @([IO.File]::ReadAllLines($mutationsPath) | Where-Object { $_ })
if (
    $retryMutations.Count -ne 1 -or
    $retryMutations[0] -cne "container-rm:$containerId"
) { throw 'finally-style name retry did not remove the exact owned full identity' }

[IO.File]::WriteAllText($mutationsPath, '', [Text.Encoding]::ASCII)
$env:WALNUT_TEST_DOCKER_MODE = 'identity-swap'
$swapCreated = $false
$swapStarted = $false
$swapId = $null
try {
    Start-OwnedPostgresContainer `
        $containerName `
        $runId `
        $owner `
        $arguments `
        ([ref]$swapCreated) `
        ([ref]$swapStarted) `
        ([ref]$swapId) `
        $docker | Out-Null
    throw 'identity-swap setup unexpectedly started'
}
catch {
    if ($_.Exception.Message -notlike 'Failed to start fresh disposable PostgreSQL*') { throw }
}
if (-not $swapCreated -or $swapStarted -or [string]$swapId -cne $containerId) {
    throw 'identity-swap setup did not capture the original full identity'
}
try {
    Remove-OwnedPostgresContainer `
        $swapCreated $containerName $swapId $runId $owner $docker
    throw 'same-name replacement identity was accepted during cleanup'
}
catch {
    if ($_.Exception.Message -notlike 'Refusing to remove a PostgreSQL container*') { throw }
}
$swapMutations = @([IO.File]::ReadAllLines($mutationsPath) | Where-Object { $_ })
if ($swapMutations.Count -ne 0) {
    throw "same-name replacement identity entered a Docker mutation: $swapMutations"
}

[IO.File]::WriteAllText($mutationsPath, '', [Text.Encoding]::ASCII)
$env:WALNUT_TEST_DOCKER_MODE = 'mismatch-labels'
$mismatchCreated = $false
$mismatchStarted = $false
$mismatchId = $null
try {
    Start-OwnedPostgresContainer `
        $containerName `
        $runId `
        $owner `
        $arguments `
        ([ref]$mismatchCreated) `
        ([ref]$mismatchStarted) `
        ([ref]$mismatchId) `
        $docker | Out-Null
    throw 'mismatching container labels were accepted at creation'
}
catch {
    if ($_.Exception.Message -notlike 'Created PostgreSQL container does not have*') { throw }
}
try {
    Remove-OwnedPostgresContainer `
        $mismatchCreated $containerName $mismatchId $runId $owner $docker
    throw 'mismatching container labels were accepted during cleanup'
}
catch {
    if ($_.Exception.Message -notlike 'Refusing to remove a PostgreSQL container*') { throw }
}
$mismatchMutations = @(
    if ([IO.File]::Exists($mutationsPath)) {
        [IO.File]::ReadAllLines($mutationsPath) | Where-Object { $_ }
    }
)
if ($mismatchMutations.Count -ne 0) {
    throw "mismatching labels entered a Docker mutation: $mismatchMutations"
}
Write-Output 'POSTGRES_PARTIAL_CREATE_CLEANUP_PASS'
""",
        encoding="utf-8-sig",
    )
    state_root = tmp_path / "fake-state"
    state_root.mkdir()
    environment = os.environ.copy()
    environment["WALNUT_TEST_FAKE_DOCKER"] = str(fake_docker)
    environment["WALNUT_TEST_FAKE_STATE_ROOT"] = str(state_root)
    environment["WALNUT_TEST_RUN_ID"] = "b" * 32
    environment["WALNUT_TEST_CONTAINER_ID"] = "a" * 64
    environment["WALNUT_TEST_REPLACEMENT_CONTAINER_ID"] = "d" * 64
    environment["WALNUT_TEST_CONTAINER_NAME"] = "walnut-int1-pg-" + "b" * 12
    environment["WALNUT_TEST_VOLUME_NAME"] = "walnut-int1-pgdata-" + "b" * 12
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "POSTGRES_PARTIAL_CREATE_CLEANUP_PASS" in completed.stdout


def test_postgres_container_absence_probe_distinguishes_winps_native_outcomes(
    tmp_path: Path,
) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    probe_function = (
        "function Test-DockerContainerAbsent"
        + script.split("function Test-DockerContainerAbsent", 1)[1].split(
            "function Remove-OwnedPostgresContainer", 1
        )[0]
    )
    fake_docker = tmp_path / "fake-docker.cmd"
    fake_docker.write_text(
        r"""@echo off
if not "%1"=="container" exit /b 64
if not "%2"=="inspect" exit /b 65
if "%WALNUT_TEST_DOCKER_MODE%"=="absent" (
  >&2 echo Error: No such object: %5
  exit /b 1
)
if "%WALNUT_TEST_DOCKER_MODE%"=="exists" (
  echo %5
  exit /b 0
)
>&2 echo Error response from daemon: Docker Desktop is unavailable
exit /b 1
""",
        encoding="ascii",
    )
    winps_probe = tmp_path / "postgres-container-absence-probe.ps1"
    winps_probe.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        + probe_function
        + r"""
$containerId = [string]::new('a', 64)
$docker = $env:WALNUT_TEST_FAKE_DOCKER
$env:WALNUT_TEST_DOCKER_MODE = 'absent'
if ((Test-DockerContainerAbsent $containerId $docker) -ne $true) {
    throw 'expected absent container was not classified as absent'
}
$env:WALNUT_TEST_DOCKER_MODE = 'exists'
if ((Test-DockerContainerAbsent $containerId $docker) -ne $false) {
    throw 'existing container was not classified as existing'
}
$env:WALNUT_TEST_DOCKER_MODE = 'inspect-error'
try {
    Test-DockerContainerAbsent $containerId $docker | Out-Null
    throw 'unexpected inspect failure was accepted as absence'
}
catch {
    if ($_.Exception.Message -notlike 'Could not prove the PostgreSQL container was absent*') {
        throw
    }
}
Write-Output 'POSTGRES_CONTAINER_ABSENCE_PROBE_PASS'
""",
        encoding="utf-8-sig",
    )
    environment = os.environ.copy()
    environment["WALNUT_TEST_FAKE_DOCKER"] = str(fake_docker)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(winps_probe),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "POSTGRES_CONTAINER_ABSENCE_PROBE_PASS" in completed.stdout


def test_postgres_cleanup_requires_exact_runtime_ownership_without_docker(tmp_path: Path) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    cleanup_functions = (
        "function Invoke-DockerNativeCapture"
        + script.split("function Invoke-DockerNativeCapture", 1)[1].split(
            "function Assert-ProviderSecretText", 1
        )[0]
    )
    probe = tmp_path / "postgres-cleanup-ownership-probe.ps1"
    probe.write_text(
        r"""
Set-StrictMode -Version Latest
$script:containerAuthority = ''
$script:volumeAuthority = ''
$script:containerRemovals = 0
$script:volumeRemovals = 0
$script:dockerCalls = 0
$script:removeExitCode = 0
$script:containerAbsent = $true
$script:volumeAbsent = $true
function docker {
    $script:dockerCalls += 1
    if ($args[0] -eq 'container' -and $args[1] -eq 'inspect') {
        $global:LASTEXITCODE = 0
        Write-Output $script:containerAuthority
        return
    }
    if ($args[0] -eq 'volume' -and $args[1] -eq 'inspect') {
        $global:LASTEXITCODE = 0
        Write-Output $script:volumeAuthority
        return
    }
    if ($args[0] -eq 'rm') {
        $script:containerRemovals += 1
        $global:LASTEXITCODE = $script:removeExitCode
        if ($script:removeExitCode -eq 0) { Write-Output $args[2] }
        return
    }
    if ($args[0] -eq 'volume' -and $args[1] -eq 'rm') {
        $script:volumeRemovals += 1
        $global:LASTEXITCODE = $script:removeExitCode
        if ($script:removeExitCode -eq 0) { Write-Output $args[3] }
        return
    }
    throw "unexpected docker invocation: $args"
}
"""
        + cleanup_functions
        + r"""
function Get-DockerResourceAuthority {
    param([string]$Kind, [string]$Name, [string]$DockerExecutable = 'docker.exe')
    $script:dockerCalls += 1
    if ($Kind -eq 'container') { return $script:containerAuthority }
    if ($Kind -eq 'volume') { return $script:volumeAuthority }
    throw "unexpected resource kind: $Kind"
}
function Test-DockerContainerAbsent {
    param([string]$ContainerId, [string]$DockerExecutable = 'docker.exe')
    return $script:containerAbsent
}
function Test-DockerVolumeAbsent {
    param([string]$Name, [string]$DockerExecutable = 'docker.exe')
    return $script:volumeAbsent
}
function Invoke-DockerNativeCapture {
    param([string]$DockerExecutable, [string[]]$Arguments, [string]$Operation)
    $script:dockerCalls += 1
    if ($Arguments[0] -eq 'rm') {
        $script:containerRemovals += 1
        $expectedOutput = $Arguments[2]
    }
    elseif ($Arguments[0] -eq 'volume' -and $Arguments[1] -eq 'rm') {
        $script:volumeRemovals += 1
        $expectedOutput = $Arguments[3]
    }
    else {
        throw "unexpected native Docker capture: $Arguments"
    }
    return [PSCustomObject]@{
        operation=$Operation
        exit_code=$script:removeExitCode
        stdout=if ($script:removeExitCode -eq 0) { $expectedOutput } else { '' }
        stderr=if ($script:removeExitCode -eq 0) { '' } else { 'native failure' }
    }
}
$containerId = [string]::new('a', 64)
$runId = [string]::new('b', 32)
$owner = 'run-int1-local-diagnostic'
$containerName = 'walnut-int1-pg-bbbbbbbbbbbb'
$volumeName = 'walnut-int1-pgdata-bbbbbbbbbbbb'

$script:containerAuthority = "$containerId|$runId|$owner"
$script:volumeAuthority = "$volumeName|$runId|$owner"
Remove-OwnedPostgresContainer $true $containerName $containerId $runId $owner 'docker'
Remove-OwnedPostgresVolume $true $true $volumeName $runId $owner 'docker'
if ($script:containerRemovals -ne 1 -or $script:volumeRemovals -ne 1) {
    throw 'exact owned resources were not removed'
}

$callsAfterOwnedCleanup = $script:dockerCalls
Remove-OwnedPostgresContainer $false $containerName $containerId $runId $owner 'docker'
try {
    Remove-OwnedPostgresVolume $true $false $volumeName $runId $owner 'docker'
    throw 'preexisting-volume cleanup authority was accepted'
}
catch {
    if ($_.Exception.Message -notlike 'Captured PostgreSQL volume cleanup authority*') { throw }
}
if ($script:dockerCalls -ne $callsAfterOwnedCleanup) {
    throw 'unstarted or preexisting resources were inspected for deletion'
}

$script:containerAuthority = "$([string]::new('c', 64))|$runId|$owner"
$script:volumeAuthority = "$volumeName|wrong-run|$owner"
try {
    Remove-OwnedPostgresContainer $true $containerName $containerId $runId $owner 'docker'
    throw 'container ownership mismatch was accepted'
}
catch {
    if ($_.Exception.Message -notlike 'Refusing to remove a PostgreSQL container*') { throw }
}
try {
    Remove-OwnedPostgresVolume $true $true $volumeName $runId $owner 'docker'
    throw 'volume ownership mismatch was accepted'
}
catch {
    if ($_.Exception.Message -notlike 'Refusing to remove a PostgreSQL volume*') { throw }
}
if ($script:containerRemovals -ne 1 -or $script:volumeRemovals -ne 1) {
    throw 'identity or label mismatch permitted deletion'
}

$script:containerAuthority = "$containerId|$runId|$owner"
$script:removeExitCode = 9
try {
    Remove-OwnedPostgresContainer $true $containerName $containerId $runId $owner 'docker'
    throw 'native removal failure was accepted'
}
catch {
    if ($_.Exception.Message -notlike 'Failed to remove the exact owned PostgreSQL container*') { throw }
}
$script:removeExitCode = 0
$script:containerAbsent = $false
try {
    Remove-OwnedPostgresContainer $true $containerName $containerId $runId $owner 'docker'
    throw 'existing container after removal was accepted'
}
catch {
    if ($_.Exception.Message -notlike 'Owned PostgreSQL container still exists*') { throw }
}
$script:volumeAuthority = "$volumeName|$runId|$owner"
$script:removeExitCode = 9
try {
    Remove-OwnedPostgresVolume $true $true $volumeName $runId $owner 'docker'
    throw 'native volume removal failure was accepted'
}
catch {
    if ($_.Exception.Message -notlike 'Failed to remove the exact owned PostgreSQL volume*') { throw }
}
$script:removeExitCode = 0
$script:volumeAbsent = $false
try {
    Remove-OwnedPostgresVolume $true $true $volumeName $runId $owner 'docker'
    throw 'existing volume after removal was accepted'
}
catch {
    if ($_.Exception.Message -notlike 'Owned PostgreSQL volume still exists*') { throw }
}
Write-Output 'POSTGRES_CLEANUP_OWNERSHIP_PASS'
""",
        encoding="utf-8-sig",
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "POSTGRES_CLEANUP_OWNERSHIP_PASS" in completed.stdout


def test_service_postgres_reconnection_evidence_executes_without_docker(tmp_path: Path) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    wait_function = (
        "function Wait-ServicePostgresConnections"
        + script.split("function Wait-ServicePostgresConnections", 1)[1].split(
            "function New-RandomHex", 1
        )[0]
    )
    probe = tmp_path / "postgres-reconnection-evidence-probe.ps1"
    probe.write_text(
        r"""
Set-StrictMode -Version Latest
function Test-ProcessDescendsFrom([int]$ProcessId, [int]$ExpectedAncestorId) {
    return $ProcessId -eq $ExpectedAncestorId
}
function Get-PostgresClientProcessIds([int]$Port) {
    if ($Port -ne 55432) { throw 'unexpected test port' }
    return @(101, 202, 303)
}
"""
        + wait_function
        + r"""
$evidence = Wait-ServicePostgresConnections 55432 ([ordered]@{
    gateway=101; workflow_worker=202; learner_worker=303
}) 1
if ($evidence.postgres_port -ne 55432) { throw 'port evidence drifted' }
if ($evidence.service_client_process_ids.gateway -ne 101) { throw 'Gateway was not observed' }
if ($evidence.service_client_process_ids.workflow_worker -ne 202) { throw 'workflow worker was not observed' }
if ($evidence.service_client_process_ids.learner_worker -ne 303) { throw 'learner worker was not observed' }
if (@($evidence.final_established_client_process_ids).Count -ne 3) { throw 'client set drifted' }
Write-Output 'POSTGRES_RECONNECTION_EVIDENCE_PASS'
""",
        encoding="utf-8-sig",
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "POSTGRES_RECONNECTION_EVIDENCE_PASS" in completed.stdout


def test_local_port_allocator_uses_distinct_non_dynamic_ports_immediately_bindable(
    tmp_path: Path,
) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    allocator = (
        "function Get-FreeTcpPort"
        + script.split("function Get-FreeTcpPort", 1)[1].split(
            "function Test-LocalTcpPortAvailable", 1
        )[0]
    )

    assert "$minimumPort = 20000" in allocator
    assert "$maximumPort = 45000" in allocator
    assert "$maximumCandidateCount" in allocator
    assert "RandomNumberGenerator]::Create()" in allocator
    assert re.search(r"TcpListener\]::new\([^\r\n]+,[ \t]*0\)", allocator) is None
    assert "$privateRelayPort = Get-FreeTcpPort -ExcludedPorts @($relayPort)" in script
    assert (
        "$postgresPort = Get-FreeTcpPort -ExcludedPorts @($relayPort, $privateRelayPort)" in script
    )

    probe = tmp_path / "local-port-allocator-probe.ps1"
    probe.write_text(
        "Set-StrictMode -Version Latest\n"
        + allocator
        + r"""
for ($iteration = 0; $iteration -lt 40; $iteration++) {
    $first = Get-FreeTcpPort
    $second = Get-FreeTcpPort -ExcludedPorts @($first)
    $third = Get-FreeTcpPort -ExcludedPorts @($first, $second)
    $ports = @($first, $second, $third)
    if (@($ports | Sort-Object -Unique).Count -ne 3) {
        throw 'allocator returned duplicate ports'
    }
    if (@($ports | Where-Object { $_ -lt 20000 -or $_ -gt 45000 }).Count -ne 0) {
        throw 'allocator returned a dynamic-range candidate'
    }
    $listeners = [Collections.Generic.List[Net.Sockets.TcpListener]]::new()
    try {
        foreach ($port in $ports) {
            $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $port)
            $listener.ExclusiveAddressUse = $true
            $listener.Start()
            $listeners.Add($listener)
        }
    }
    finally {
        foreach ($listener in $listeners) {
            $listener.Stop()
        }
    }
}
Write-Output 'LOCAL_PORT_ALLOCATOR_PASS'
""",
        encoding="utf-8-sig",
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "LOCAL_PORT_ALLOCATOR_PASS" in completed.stdout


def test_harness_powershell_syntax_is_valid_without_execution() -> None:
    for script_path in (HARNESS, REAL_PROVIDER_WRAPPER):
        environment = os.environ.copy()
        environment["INT1_DIAGNOSTIC_SCRIPT"] = str(script_path)
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$errors=$null; "
                    "[Management.Automation.Language.Parser]::ParseFile("
                    "$env:INT1_DIAGNOSTIC_SCRIPT,[ref]$null,[ref]$errors)|Out-Null; "
                    "if($errors.Count){$errors|ForEach-Object{$_.ToString()};exit 1}"
                ),
            ],
            cwd=BACKEND_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr


def test_turn_reconcile_threshold_executes_on_windows_powershell_51() -> None:
    script = HARNESS.read_text(encoding="utf-8")
    assert re.search(r"\([ \t]*if[ \t]*\(", script) is None
    assignment_match = re.search(
        r"(?m)^    (\$expectedTurnJobAttempt = 2)$",
        script,
    )
    assert assignment_match is not None
    assignment = assignment_match.group(1)
    conditions = (
        "[int]$databaseFingerprint.turn_job_attempt -lt $expectedTurnJobAttempt",
        "[int]$databaseFingerprint.turn_worker_reconcile_receipts -lt 1",
        "[int]$databaseFingerprint.turn_worker_failure_receipts -ne 0",
    )
    for condition in conditions:
        assert f"{condition} -or" in script
    assert "turn_worker_failure_receipts -lt 1" not in script
    environment = os.environ.copy()
    environment["WALNUT_TEST_TURN_ATTEMPT_ASSIGNMENT"] = assignment
    environment["WALNUT_TEST_TURN_RECOVERY_EXPRESSION"] = " -or ".join(conditions)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$assignment=$env:WALNUT_TEST_TURN_ATTEMPT_ASSIGNMENT; "
                "$expression=$env:WALNUT_TEST_TURN_RECOVERY_EXPRESSION; "
                "$RealProvider=$false; Invoke-Expression $assignment; "
                "$databaseFingerprint=[PSCustomObject]@{turn_job_attempt=2;"
                "turn_worker_reconcile_receipts=1;turn_worker_failure_receipts=0}; "
                "if(Invoke-Expression $expression){throw 'valid reconcile closure rejected'}; "
                "$databaseFingerprint.turn_worker_reconcile_receipts=0; "
                "if(-not(Invoke-Expression $expression)){throw 'missing reconcile accepted'}; "
                "$databaseFingerprint.turn_worker_reconcile_receipts=1; "
                "$databaseFingerprint.turn_worker_failure_receipts=1; "
                "if(-not(Invoke-Expression $expression)){throw 'worker failure accepted'}; "
                "$RealProvider=$true; Invoke-Expression $assignment; "
                "$databaseFingerprint=[PSCustomObject]@{turn_job_attempt=2;"
                "turn_worker_reconcile_receipts=1;turn_worker_failure_receipts=0}; "
                "if(Invoke-Expression $expression){throw 'provider threshold rejected'}; "
                "Write-Output 'TURN_RECONCILE_THRESHOLD_PASS'"
            ),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "TURN_RECONCILE_THRESHOLD_PASS" in completed.stdout


def test_database_fingerprint_transport_preserves_utf8_nested_json_without_docker(
    tmp_path: Path,
) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    transport_function = (
        "function ConvertFrom-DatabaseFingerprintTransport"
        + script.split("function ConvertFrom-DatabaseFingerprintTransport", 1)[1].split(
            "function Invoke-DatabaseFingerprint", 1
        )[0]
    )
    nested = '{"message":"\u3002","next":"ok"}'
    payload = {f"field_{index:02d}": index for index in range(81)}
    payload["run_set_material"] = nested
    wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # This is the exact Windows PowerShell 5.1 failure mode from the live
    # fingerprint: CP936 consumes the escape backslash after the UTF-8 bytes
    # for U+3002, even though PostgreSQL emitted valid JSON.
    corrupted = wire.encode("utf-8").decode("cp936")
    assert '\u9286\u4fd3"' in corrupted
    with pytest.raises(json.JSONDecodeError):
        json.loads(corrupted)

    probe = tmp_path / "fingerprint-transport-probe.ps1"
    probe.write_text(
        transport_function
        + r"""
[Console]::OutputEncoding = [Text.Encoding]::GetEncoding(936)
$value = ConvertFrom-DatabaseFingerprintTransport $env:WALNUT_TEST_FINGERPRINT_BASE64
$expectedBytes = [Convert]::FromBase64String($env:WALNUT_TEST_NESTED_BASE64)
$expected = [Text.UTF8Encoding]::new($false, $true).GetString($expectedBytes)
if (@($value.PSObject.Properties).Count -ne 82) { throw 'fingerprint field count drifted' }
if ([string]$value.run_set_material -cne $expected) { throw 'UTF-8 material drifted' }
try {
    ConvertFrom-DatabaseFingerprintTransport ($env:WALNUT_TEST_FINGERPRINT_BASE64 + "`nNOTICE") | Out-Null
    throw 'polluted transport was accepted'
}
catch {
    if ($_.Exception.Message -notlike '*canonical base64*') { throw }
}
try {
    ConvertFrom-DatabaseFingerprintTransport '/w==' | Out-Null
    throw 'non-UTF-8 transport was accepted'
}
catch {
    if ($_.Exception.Message -notlike '*strict UTF-8*') { throw }
}
try {
    ConvertFrom-DatabaseFingerprintTransport 'W10=' | Out-Null
    throw 'non-object JSON transport was accepted'
}
catch {
    if ($_.Exception.Message -notlike '*one JSON object*') { throw }
}
Write-Output 'FINGERPRINT_TRANSPORT_PASS'
""",
        encoding="utf-8-sig",
    )
    environment = os.environ.copy()
    environment["WALNUT_TEST_FINGERPRINT_BASE64"] = base64.b64encode(wire.encode("utf-8")).decode(
        "ascii"
    )
    environment["WALNUT_TEST_NESTED_BASE64"] = base64.b64encode(nested.encode("utf-8")).decode(
        "ascii"
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "FINGERPRINT_TRANSPORT_PASS" in completed.stdout


def test_restart_fingerprint_excludes_only_read_only_relay_capability_probe(
    tmp_path: Path,
) -> None:
    script = HARNESS.read_text(encoding="utf-8")
    sha_function = (
        "function Get-Sha256"
        + script.split("function Get-Sha256", 1)[1].split("function Get-DirectoryFingerprint", 1)[0]
    )
    comparison_functions = (
        "function ConvertTo-StableJson"
        + script.split("function ConvertTo-StableJson", 1)[1].split(
            "function Get-GatewayRequestAudit", 1
        )[0]
    )
    probe = tmp_path / "restart-fingerprint-probe.ps1"
    probe.write_text(
        sha_function
        + comparison_functions
        + r"""
Set-StrictMode -Version Latest
$before = [PSCustomObject]@{
    schema_version='1.0.0'; classification='DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER';
    protocol='YAYA_RECOVERABLE_LLM_V1'; capability_gets=13; dispatch_puts=12;
    reconcile_gets=2; acknowledgement_drops=1; reconcile_unavailable=1;
    unique_dispatches=12; total_generations=12; max_generation_count=1;
    dispatches=@([PSCustomObject]@{dispatch_id='llmdsp_example'; generation_count=1})
}
$after = $before | ConvertTo-Json -Compress -Depth 8 | ConvertFrom-Json
$after.capability_gets = 14
$beforeJson = ConvertTo-StableJson (Get-RelaySideEffectFingerprint $before $false)
$afterJson = ConvertTo-StableJson (Get-RelaySideEffectFingerprint $after $false)
if ($beforeJson -cne $afterJson) { throw 'read-only capability probe changed side-effect authority' }
$after.dispatch_puts = 13
$changedJson = ConvertTo-StableJson (Get-RelaySideEffectFingerprint $after $false)
$comparison = New-RestartFingerprintComparison $beforeJson $changedJson
if (
    $comparison.unchanged -ne $false -or
    $comparison.first_different_property -cne 'dispatch_puts' -or
    $comparison.before_sha256 -notmatch '^[a-f0-9]{64}$' -or
    $comparison.after_sha256 -notmatch '^[a-f0-9]{64}$'
) { throw 'side-effect drift was not safely classified' }
$real = [PSCustomObject]@{
    schema_version='1.0.0'; protocol='YAYA_RECOVERABLE_LLM_V1'; unique_dispatches=1;
    total_generations=1; max_generation_count=1; states=[PSCustomObject]@{SUCCEEDED=1};
    dispatches=@([PSCustomObject]@{dispatch_id='llmdsp_real'; generation_count=1})
}
$realProjectionJson = ConvertTo-StableJson (Get-RelaySideEffectFingerprint $real $true)
$realJson = ConvertTo-StableJson $real
if ($realProjectionJson -cne $realJson) { throw 'real Provider generation authority was projected incompletely' }
Write-Output 'RESTART_FINGERPRINT_PROJECTION_PASS'
""",
        encoding="utf-8-sig",
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "RESTART_FINGERPRINT_PROJECTION_PASS" in completed.stdout


def test_real_provider_secret_reaches_only_relay_child_environment() -> None:
    script = HARNESS.read_text(encoding="utf-8")
    fault_proxy = REAL_PROVIDER_FAULT_PROXY.read_text(encoding="utf-8")
    wrapper = REAL_PROVIDER_WRAPPER.read_text(encoding="utf-8")

    for required in (
        "Read-WindowsAclControlledProviderKey",
        "System.IO.FileAttributes]::ReparsePoint",
        "GetAccessRules(",
        "RawSecurityDescriptor",
        "DiscretionaryAclPresent",
        "$null -eq $raw.DiscretionaryAcl",
        "S-1-1-0",
        "S-1-5-11",
        "S-1-5-32-545",
        "FileSystemRights]::ReadData",
        "Start-PrivateRealProviderRelay",
        "Clear-UpstreamKeyEnvironment",
        "finally",
    ):
        assert required in script

    real_start = script.split("function Start-PrivateRealProviderRelay", 1)[1].split(
        "function Get-FreeTcpPort", 1
    )[0]
    assert "WALNUT_LLM_UPSTREAM_API_KEY', $realProviderUpstreamKey" in real_start
    assert "-ArgumentList @('-m', 'walnut_backend.llm_relay.main')" in real_start
    assert "Clear-UpstreamKeyEnvironment" in real_start
    assert real_start.index("Start-Process") < real_start.index("Clear-UpstreamKeyEnvironment")
    assert script.count("Start-PrivateRealProviderRelay `") == 2
    result_block = script.rsplit("$pendingPassResult = [ordered]@{", 1)[1].split("\n    }\n}", 1)[0]
    assert "WALNUT_LLM_UPSTREAM_API_KEY" not in result_block
    assert "FORBIDDEN_PROVIDER_KEY_ENVS" in fault_proxy
    assert "fault proxy must not inherit an upstream Provider credential" in fault_proxy
    assert 'parser.add_argument("--upstream-port"' in fault_proxy
    assert 'parser.add_argument("--upstream-endpoint"' not in fault_proxy
    assert script.index("$faultProxyProcess = Start-Process") > script.index(
        "$relayProcess = Start-PrivateRealProviderRelay `"
    )
    assert "use process-environment key injection on Windows" not in wrapper


def test_real_provider_wrapper_scopes_the_runtime_theoretical_generation_limit() -> None:
    harness = HARNESS.read_text(encoding="utf-8")
    wrapper = REAL_PROVIDER_WRAPPER.read_text(encoding="utf-8")
    compose = COMPOSE.read_text(encoding="utf-8")
    limit_name = "WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS"

    assert "$generationLimitName = 'WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS'" in wrapper
    assert "[switch]$EnableSkillPatch" in wrapper
    assert "$EnableSkillPatch -and -not $EnableWorldPresentation" in wrapper
    assert "$generationLimit = if ($EnableSkillPatch) { 32 } else { 24 }" in wrapper
    assert (
        "SetEnvironmentVariable($generationLimitName, [string]$generationLimit, 'Process')"
        in wrapper
    )
    assert "$harnessArguments += '-EnableSkillPatch'" in wrapper
    assert "$harnessArguments += '-EnableWorldPresentation'" in wrapper
    assert "$originalGenerationLimit" in wrapper
    assert "$harnessExitCode = $LASTEXITCODE" in wrapper
    assert wrapper.index(
        "SetEnvironmentVariable($generationLimitName, [string]$generationLimit"
    ) < wrapper.index("& powershell.exe")
    assert wrapper.index("$harnessExitCode = $LASTEXITCODE") < wrapper.index(
        "$originalGenerationLimit,"
    )

    real_preflight = harness.split("if ($RealProvider) {", 1)[1].split(
        "$preflight = [ordered]@{", 1
    )[0]
    assert f"$env:{limit_name} -cne" in real_preflight
    assert "[string]$expectedRealProviderGenerationLimit" in real_preflight
    assert (
        "bounded $expectedRealProviderGenerationLimit-generation pre-billing limit"
        in real_preflight
    )
    assert (
        "$expectedRealProviderGenerationLimit = if ($EnableSkillPatch) { 32 } else { 24 }"
        in harness
    )
    assert f"'{limit_name}'," in harness
    assert limit_name not in compose
    verifier = (BACKEND_ROOT / "scripts" / "verify_real_provider_relay.py").read_text(
        encoding="utf-8"
    )
    assert "r.step_name LIKE '%PROVIDER_RESULT_%'" in verifier
    assert "r.step_name LIKE 'PROVIDER_RESULT_%'" not in verifier


def test_relay_and_runbook_are_closed_local_test_infrastructure() -> None:
    relay_source = RELAY.read_text(encoding="utf-8")
    fault_proxy_source = REAL_PROVIDER_FAULT_PROXY.read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")

    compile(relay_source, str(RELAY), "exec")
    compile(fault_proxy_source, str(REAL_PROVIDER_FAULT_PROXY), "exec")
    assert 'HOST = "127.0.0.1"' in relay_source
    assert 'PROTOCOL = "YAYA_RECOVERABLE_LLM_V1"' in relay_source
    assert "field(repr=False)" in relay_source
    assert 'parser.add_argument("--port"' in relay_source
    assert 'parser.add_argument("--host"' not in relay_source
    assert '"atomic_put_by_dispatch_id": True' in relay_source
    assert '"linearizable_get": True' in relay_source
    assert '"immutable_request_hash": True' in relay_source
    assert '"max_generation_count": 1' in relay_source
    assert '"generation_count": 1' in relay_source
    assert "self.connection.shutdown(socket.SHUT_RDWR)" in relay_source
    assert "api_key" not in relay_source.split("def statistics", 1)[1].split("class ", 1)[0]

    assert 'HOST: Final = "127.0.0.1"' in fault_proxy_source
    assert 'PROTOCOL: Final = "YAYA_RECOVERABLE_LLM_V1"' in fault_proxy_source
    assert "field(repr=False)" in fault_proxy_source
    assert 'parser.add_argument("--port"' in fault_proxy_source
    assert 'parser.add_argument("--host"' not in fault_proxy_source
    assert "self.connection.shutdown(socket.SHUT_RDWR)" in fault_proxy_source
    assert "terminal_before_drop" in fault_proxy_source
    assert "recovered_same_dispatch" in fault_proxy_source
    proxy_statistics = fault_proxy_source.split("def statistics", 1)[1].split(
        "    def _request", 1
    )[0]
    assert "api_key" not in proxy_statistics
    assert "provider_response" not in proxy_statistics
    assert "completion" not in proxy_statistics

    assert "never evidence" in runbook
    assert "never downloads" in runbook
    assert "NOT_LIVE" in runbook
    assert "generation_count=1" in runbook
    assert "two-process recovery acceptance" in runbook
    assert "dedicated learner worker" in runbook
    assert "access log" in runbook
    assert "non-World event" in runbook
    assert "full-row" in runbook
    assert "not evidence\nthat the new two-process restart gate has executed" in runbook
