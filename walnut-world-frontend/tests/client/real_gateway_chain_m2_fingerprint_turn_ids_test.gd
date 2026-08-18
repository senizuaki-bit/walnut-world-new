extends SceneTree

const Phase1E2E := preload("res://tests/client/real_gateway_chain_e2e_test.gd")


func _initialize() -> void:
	# CommandResult is deliberately closed and has no top-level turn_id for the
	# fifth no-Run Patch proposal. Its canonical AgentInteraction owns that ID.
	var patch_chain := {
		"command": {
			"command_id": "cmd_patch_proposal_0001",
			"result": {"result_type": "NO_EFFECT", "reason_code": "SKILL_PATCH_PROPOSED"},
			"links": {"self": "/v1/commands/cmd_patch_proposal_0001"},
		},
		"decided_interaction": {"turn_id": "turn_patch_proposal_0005"},
	}
	var patch_turn: Dictionary = Phase1E2E._canonical_patch_turn_id(patch_chain)
	var turn_ids := [
		"turn_failure_0001",
		"turn_failure_0002",
		"turn_failure_0003",
		"turn_failure_0004",
		str(patch_turn.get("value", "")),
		"turn_success_0006",
	]
	var unique_turn_ids := {}
	for turn_id: String in turn_ids:
		if turn_id.is_empty() or unique_turn_ids.has(turn_id):
			return _fail("The formal M2 fingerprint contains an empty or duplicate Turn ID.")
		unique_turn_ids[turn_id] = true
	if (
		not patch_turn.get("ok", false)
		or turn_ids.size() != 6
		or unique_turn_ids.size() != 6
		or str(turn_ids[4]) != "turn_patch_proposal_0005"
	):
		return _fail("The formal M2 fingerprint did not close six Turn IDs through the decided Patch Interaction.")

	var malformed: Dictionary = Phase1E2E._canonical_patch_turn_id({
		"command": {"command_id": "cmd_patch_proposal_0001"},
		"decided_interaction": {},
	})
	if malformed.get("ok", true) or str(malformed.get("code", "")) != "M2_PATCH_TURN_ID_INVALID":
		return _fail("A missing decided-Interaction Turn ID must fail structurally without a runtime property error.")

	print("REAL_GATEWAY_CHAIN_M2_FINGERPRINT_TURN_IDS_TEST_PASS")
	quit(0)


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
