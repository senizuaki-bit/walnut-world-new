extends SceneTree


func _initialize() -> void:
	var workspace := FileAccess.get_file_as_string("res://scenes/task/task_workspace.gd")
	var phase1 := FileAccess.get_file_as_string("res://tests/client/real_gateway_chain_e2e_test.gd")
	var recovery := FileAccess.get_file_as_string("res://tests/client/real_gateway_chain_recovery_e2e_test.gd")
	var runner := FileAccess.get_file_as_string("res://scripts/run-real-gateway-e2e.ps1")
	var timeout_test := FileAccess.get_file_as_string("res://tests/client/run-real-gateway-e2e-timeout-test.ps1")
	if workspace.is_empty() or phase1.is_empty() or recovery.is_empty() or runner.is_empty() or timeout_test.is_empty():
		return _fail("Formal M2 harness source is missing.")

	for required in [
		"signal patch_request_action_finished(result: Dictionary)",
		"signal patch_decision_action_finished(decision: String, result: Dictionary)",
		"patch_request_action_finished.emit",
		"patch_decision_action_finished.emit",
	]:
		if not workspace.contains(required):
			return _fail("TaskWorkspace lacks an observable formal Patch action seam: %s" % required)

	for required in [
		"YAYA_REAL_GATEWAY_E2E_ENABLE_SKILL_PATCH",
		"app.skill_patch_enabled = skill_patch_enabled",
		"EXPECTED_M2_POST_COUNT := 12",
		"EXPECTED_M2_PUT_COUNT := 1",
		"EXPECTED_M2_TURN_COUNT := 6",
		"EXPECTED_M2_RUN_COUNT := 5",
		"EXPECTED_M2_LEARNER_COUNT := 5",
		'"REQUEST_PATCH"',
		'"ACCEPT_PATCH"',
		'"record_product_patch_decision": 1',
		'"submit_agent_turn": 6',
		'"RequestAiPatchButton"',
		'"CodePatchDialog"',
		'"skill_patch"',
		'"PUBLIC_UI_CHAIN_CLOSED"',
		"_verify_patch_proposal",
		"_verify_accepted_patch_decision",
		"_verify_m2_public_read_closure",
		"recover_patch_failure_authority",
		"patch_decision_action_finished",
		"func(_decision: String, result: Dictionary)",
		'"backend_authority_fingerprint_required": true',
	]:
		if not phase1.contains(required):
			return _fail("Phase-1 formal M2 harness lacks its real UI/public-authority seam: %s" % required)
	for forbidden in [
		"BACKEND_PUBLIC_M2_FIELDS_NOT_READY",
		"_exercise_formal_patch_not_ready_seam",
		"_exercise_formal_patch_candidate_seam",
		"patch_chain.command.turn_id",
	]:
		if phase1.contains(forbidden):
			return _fail("Phase-1 formal M2 harness still contains its retired NOT_READY skeleton: %s" % forbidden)
	for required in [
		"_canonical_patch_turn_id(patch_chain)",
		'var decided_interaction: Variant = patch_chain.get("decided_interaction")',
		'not command is Dictionary',
		'command.has("turn_id")',
		'str(decided_interaction.get("turn_id", ""))',
	]:
		if not phase1.contains(required):
			return _fail("Phase-1 formal M2 fingerprint must source the fifth no-Run Turn ID from its canonical decided Interaction: %s" % required)
	for forbidden in ["controller.request_ai_patch", "controller.decide_patch"]:
		if phase1.contains(forbidden):
			return _fail("Formal M2 harness bypasses TaskWorkspace UI: %s" % forbidden)

	for required in [
		"YAYA_REAL_GATEWAY_E2E_ENABLE_SKILL_PATCH",
		"app.skill_patch_enabled = skill_patch_enabled",
		'"skill_patch"',
		'"phase1_skill_patch_exact_match"',
		'"PUBLIC_UI_CHAIN_CLOSED"',
		"_verify_recovered_m2_public_read_closure",
		'"backend_authority_fingerprint_required": true',
	]:
		if not recovery.contains(required):
			return _fail("Recovery-only M2 harness lacks its exact Patch fingerprint seam: %s" % required)
	if recovery.contains("BACKEND_PUBLIC_M2_FIELDS_NOT_READY"):
		return _fail("Recovery-only M2 harness still accepts the retired generic NOT_READY fingerprint.")

	for required in [
		"[switch]$EnableSkillPatch",
		"YAYA_REAL_GATEWAY_E2E_ENABLE_SKILL_PATCH",
		"Skill Patch requires -EnableWorldPresentation",
		"phase1_skill_patch_exact_match",
		"$EnableSkillPatch -or",
		"WaitForExit",
		"Stop-VerifiedSpawnedProcessTree",
		'$processDeadlineMilliseconds = ([long]$TotalDeadlineSeconds + $processDeadlineGraceSeconds) * 1000',
		'if (-not $godotProcess.WaitForExit([int]$processDeadlineMilliseconds))',
		'[void]$godotProcess.Handle',
		'$godotProcess.WaitForExit()',
		'$godotProcess.Refresh()',
		'if (-not $godotProcess.HasExited)',
		'$observedExitCode = $godotProcess.ExitCode',
		'$observedExitCode -isnot [int]',
		'/PID ([string]$Process.Id) /T /F',
		"exceeded external process deadline",
	]:
		if not runner.contains(required):
			return _fail("PowerShell formal runner lacks a double-flagged M2 assertion: %s" % required)
	if runner.contains("-Wait `"):
		return _fail("PowerShell formal runner still contains an unbounded Start-Process -Wait.")
	if runner.contains('$taskKillExitCode -ne 0 -and -not $Process.HasExited'):
		return _fail("PowerShell formal runner masks a non-zero process-tree termination result after its root exits.")
	for required in [
		"FAKE_GODOT_TIMEOUT_DIAGNOSTIC",
		"FAKE_GODOT_EXIT_0_DIAGNOSTIC",
		"FAKE_GODOT_EXIT_23_DIAGNOSTIC",
		"failed with exit code 23",
		"RUN_REAL_GATEWAY_E2E_TIMEOUT_TEST_PASS",
		"exact spawned Godot descendant",
	]:
		if not timeout_test.contains(required):
			return _fail("PowerShell formal runner lacks focused fake-process timeout coverage: %s" % required)

	print("REAL_GATEWAY_CHAIN_M2_HARNESS_SEAM_TEST_PASS")
	quit(0)


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
