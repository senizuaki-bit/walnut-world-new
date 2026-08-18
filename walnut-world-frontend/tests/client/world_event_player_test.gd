extends SceneTree

const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const PlayerScript := preload("res://scripts/client/world_event_player.gd")
var _recovered_after := -1


class FakeRenderer:
	extends Node
	var begun: Array[String] = []
	var finished: Array[String] = []
	var speeds: Array[float] = []

	func begin_presentation_event(event: Dictionary, speed: float) -> Dictionary:
		begun.append(str(event.event_id))
		speeds.append(speed)
		return {"ok": true, "duration_seconds": 0.02}

	func finish_presentation_event(event: Dictionary, _skipped: bool) -> bool:
		finished.append(str(event.event_id))
		return true


func _initialize() -> void:
	var player := PlayerScript.new()
	var renderer := FakeRenderer.new()
	root.add_child(player)
	root.add_child(renderer)
	await process_frame
	player.playback_recovery_required.connect(_on_recovery_required)
	player.set_cursor(0)
	player.set_speed_multiplier(2.0)
	var event_1 := _event(1)
	var event_2 := _event(2)
	var result: Dictionary = await player.play([event_1, event_2], renderer)
	if not result.get("ok", false) or renderer.begun != [event_1.event_id, event_2.event_id] or renderer.speeds != [2.0, 2.0]:
		return _fail("Authoritative presentation events were not rendered in exact sequence at 2x speed.")
	var rendered_count := renderer.begun.size()
	result = await player.play([event_1, event_2], renderer)
	if not result.get("ok", false) or renderer.begun.size() != rendered_count:
		return _fail("Duplicate reads replayed an already consumed presentation event.")
	result = await player.play([event_2], renderer)
	if not result.get("ok", false) or renderer.begun.size() != rendered_count:
		return _fail("A partial duplicate read replayed an already consumed presentation event.")
	result = await player.play([event_2, event_1], renderer)
	if result.get("ok", false) or renderer.begun.size() != rendered_count:
		return _fail("An out-of-order duplicate read was silently accepted.")

	result = await player.play([_event(4), _event(3)], renderer)
	if result.get("ok", false) or _recovered_after != 2 or renderer.begun.size() != rendered_count:
		return _fail("Out-of-order input was sorted or partially rendered instead of failing closed.")

	var cross_commit_revision_jump := [_single_action_event(3, 3, 3), _single_action_event(4, 5, 4)]
	result = player.validate_batch(cross_commit_revision_jump, 2)
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_ACTION_CHAIN_MISMATCH":
		return _fail("Player accepted a self-consistently rehashed cross-commit World revision jump.")

	var cross_commit_final_sequence_reuse := [_single_action_event(3, 3, 3), _single_action_event(4, 4, 3)]
	result = player.validate_batch(cross_commit_final_sequence_reuse, 2)
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_ACTION_CHAIN_MISMATCH":
		return _fail("Player accepted a self-consistently rehashed cross-commit final World sequence reuse.")

	var cold_mid_commit := _event(1)
	cold_mid_commit.action_index = 1
	cold_mid_commit.state_hash_after = cold_mid_commit.final_snapshot_state_hash
	_reseal(cold_mid_commit)
	result = player.validate_batch([cold_mid_commit], 0)
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_ACTION_CHAIN_MISMATCH":
		return _fail("Player accepted cold presentation sequence 1 at a nonzero action_index.")

	result = await player.replay_current_result(renderer)
	if not result.get("ok", false) or renderer.begun.slice(rendered_count) != [event_1.event_id, event_2.event_id] or player.get_cursor() != 2:
		return _fail("Partial/invalid duplicate reads must not replace the complete current result used for replay.")

	var skip_player := PlayerScript.new()
	var skip_renderer := FakeRenderer.new()
	root.add_child(skip_player)
	root.add_child(skip_renderer)
	skip_player.set_cursor(0)
	skip_player.call_deferred("skip")
	result = await skip_player.play([event_1, event_2], skip_renderer)
	if not result.get("ok", false) or not bool(result.get("skipped", false)) or skip_player.get_cursor() != 2:
		return _fail("Skip did not close the verified event range at the same final cursor.")
	print("WORLD_EVENT_PLAYER_TEST_PASS")
	quit(0)


func _event(sequence: int) -> Dictionary:
	var event := {
		"event_id": "", "event_type": "world.action.harvested", "event_version": 1,
		"schema_version": "1.0.0", "stream_id": "world-presentation:world_demo",
		"sequence": sequence, "occurred_at": "2026-08-14T01:02:03Z",
		"producer": "walnut_world_engine", "tenant_id": "tenant_demo",
		"session_id": "session_demo", "turn_id": "turn_demo_00000001",
		"command_id": "cmd_demo_00000001", "run_id": "run_demo_00000001",
		"world_id": "world_demo", "commit_id": "commit_demo_00000001",
		"world_revision": 2, "action_index": sequence - 1, "action_count": 2,
		"intent_id": "intent_demo_%08d" % sequence,
		"state_hash_before": ("1" if sequence == 1 else "2").repeat(64),
		"state_hash_after": ("2" if sequence == 1 else "3").repeat(64),
		"final_snapshot_revision": 2, "final_world_event_sequence": 2,
		"final_snapshot_state_hash": "3".repeat(64),
		"payload": {
			"actor_entity_id": "student_avatar", "plot_id": "farm_plot_%04d" % sequence,
			"position": {"x": sequence, "y": 2}, "crop_type": "carrot",
			"growth_stage": 3, "ready_to_harvest": true,
		},
		"payload_sha256": "", "integrity_sha256": "",
	}
	_reseal(event)
	return event


func _single_action_event(sequence: int, world_revision: int, final_world_sequence: int) -> Dictionary:
	var event := _event(sequence)
	event.commit_id = "commit_demo_%08d" % sequence
	event.turn_id = "turn_demo_%08d" % sequence
	event.command_id = "cmd_demo_%08d" % sequence
	event.run_id = "run_demo_%08d" % sequence
	event.world_revision = world_revision
	event.action_index = 0
	event.action_count = 1
	event.final_snapshot_revision = world_revision
	event.final_world_event_sequence = final_world_sequence
	event.state_hash_before = "%x" % (sequence - 1)
	event.state_hash_before = event.state_hash_before.repeat(64)
	event.state_hash_after = "%x" % sequence
	event.state_hash_after = event.state_hash_after.repeat(64)
	event.final_snapshot_state_hash = event.state_hash_after
	_reseal(event)
	return event


func _reseal(event: Dictionary) -> void:
	event.payload_sha256 = ContractValidator.canonical_json_sha256_v1(event.payload)
	var p: Dictionary = event.payload
	var pos: Dictionary = p.position
	var projection := [
		event.event_type, event.event_version, event.schema_version, event.stream_id,
		event.sequence, event.occurred_at, event.producer, event.tenant_id,
		event.session_id, event.turn_id, event.command_id, event.run_id, event.world_id,
		event.commit_id, event.world_revision, event.action_index, event.action_count,
		event.intent_id, event.state_hash_before, event.state_hash_after,
		event.final_snapshot_revision, event.final_world_event_sequence,
		event.final_snapshot_state_hash, event.payload_sha256, p.actor_entity_id,
		p.plot_id, pos.x, pos.y, p.crop_type, p.growth_stage, p.ready_to_harvest,
	]
	event.integrity_sha256 = ContractValidator.canonical_json_sha256_v1(projection)
	event.event_id = "presentation_%s" % str(event.integrity_sha256).left(32)


func _on_recovery_required(sequence: int) -> void:
	_recovered_after = sequence


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
