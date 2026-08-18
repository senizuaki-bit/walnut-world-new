extends SceneTree

const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const PlayerScript := preload("res://scripts/client/world_event_player.gd")


class FakePresentationGateway:
	extends RefCounted
	var pages: Dictionary = {}
	var calls: Array[int] = []

	func get_world_presentation_events(
		_attempt: Dictionary,
		_world_id: String,
		after_sequence: int,
		_limit: int,
	) -> Dictionary:
		calls.append(after_sequence)
		return pages.get(after_sequence, {
			"ok": false,
			"status": 0,
			"headers": {},
			"error": {"code": "MISSING_PAGE", "message": "missing", "retryable": false},
		}).duplicate(true)


func _initialize() -> void:
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	var controller := root.get_node_or_null("SessionController")
	if store == null or controller == null:
		return _fail("Required production autoloads are unavailable.")
	store.persistence_enabled = false
	var snapshot := _snapshot()
	if not store.replace_world(snapshot):
		return _fail("Could not establish cold synchronization Snapshot authority.")
	var player := PlayerScript.new()
	root.add_child(player)
	await process_frame
	var gateway := FakePresentationGateway.new()
	var event_1 := _event(1)
	var event_2 := _event(2)
	controller.configure_authority(_bootstrap(), _session())
	gateway.pages = {
		0: _success(_page([event_1], true, 1)),
		1: _success(_page([event_2], false, 2)),
	}
	controller.configure_world_presentation(gateway, player, null, true)
	var result: Dictionary = await controller.synchronize_world_presentation_cursor()
	if not result.get("ok", false) or gateway.calls != [0, 1] or player.get_cursor() != 2:
		return _fail("Cold synchronization did not validate every page before advancing to the high watermark: %s" % result)
	result = await player.replay_current_result()
	if result.get("ok", false):
		return _fail("Cold synchronization silently made historical actions available for replay.")

	var corrupt_player := PlayerScript.new()
	root.add_child(corrupt_player)
	var corrupt_gateway := FakePresentationGateway.new()
	var corrupt_event := _event(2)
	corrupt_event.state_hash_before = "f".repeat(64)
	_reseal(corrupt_event)
	corrupt_gateway.pages = {
		0: _success(_page([event_1], true, 1)),
		1: _success(_page([corrupt_event], false, 2)),
	}
	controller.configure_world_presentation(corrupt_gateway, corrupt_player, null, true)
	result = await controller.synchronize_world_presentation_cursor()
	if (
		result.get("ok", false)
		or str(result.get("error", {}).get("code", "")) != "PRESENTATION_STATE_CHAIN_MISMATCH"
		or corrupt_player.get_cursor() != 0
		or corrupt_gateway.calls != [0, 1]
	):
		return _fail("Corruption after the first cold page was skipped or advanced the cursor: %s" % result)

	var wrong_authority_player := PlayerScript.new()
	root.add_child(wrong_authority_player)
	var wrong_authority_gateway := FakePresentationGateway.new()
	var wrong_authority_page := _page([event_1, event_2], false, 2)
	wrong_authority_page.request_context.actor.actor_id = "student_other"
	wrong_authority_gateway.pages = {0: _success(wrong_authority_page)}
	controller.configure_world_presentation(wrong_authority_gateway, wrong_authority_player, null, true)
	result = await controller.synchronize_world_presentation_cursor()
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_STARTUP_AUTHORITY_MISMATCH":
		return _fail("Cold startup accepted a page actor/content authority that disagrees with Bootstrap/Session: %s" % result)
	print("WORLD_PRESENTATION_CURSOR_SYNC_TEST_PASS")
	quit(0)


func _snapshot() -> Dictionary:
	return {
		"world_id": "world_demo", "revision": 2, "last_event_sequence": 2,
		"state_schema_version": "1.0.0", "state_hash": "3".repeat(64),
		"world_rules_version": "rules_demo", "state": {},
	}


func _page(events: Array, has_more: bool, next_sequence: int) -> Dictionary:
	return {
		"request_context": _origin_context(),
		"world_id": "world_demo", "snapshot_revision": 2,
		"snapshot_last_event_sequence": 2, "snapshot_state_hash": "3".repeat(64),
		"presentation_high_watermark": 2,
		"from_sequence": int(events[0].sequence), "to_sequence": int(events[-1].sequence),
		"has_more": has_more, "next_after_sequence": next_sequence,
		"events": events.duplicate(true),
	}


func _origin_context() -> Dictionary:
	return {
		"schema_version": "1.0.0",
		"request_id": "req_cursor_sync_00000001",
		"trace_id": "trace_cursor_sync_00000001",
		"correlation_id": "corr_cursor_sync_00000001",
		"requested_at": "2026-08-14T01:02:03Z",
		"actor": {
			"tenant_id": "tenant_demo", "actor_id": "student_demo",
			"actor_type": "student", "roles": ["game:player"],
		},
		"content_ref": {
			"unit_id": "YAYA_FARM_001", "version": "1.0.0",
			"content_hash": "a".repeat(64),
		},
	}


func _bootstrap() -> Dictionary:
	return {"actor": _origin_context().actor, "content": _origin_context().content_ref}


func _session() -> Dictionary:
	return {
		"session_id": "session_demo",
		"request_context": _origin_context(),
	}


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


func _success(value: Dictionary) -> Dictionary:
	return {"ok": true, "status": 200, "headers": {}, "value": value.duplicate(true)}


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
