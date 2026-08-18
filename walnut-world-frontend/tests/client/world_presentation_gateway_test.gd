extends SceneTree

const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const PresentationGateway := preload("res://scripts/client/world_presentation_gateway.gd")


class FakeTransport:
	extends RefCounted
	var response: Dictionary = {}
	var operation := ""
	var args: Dictionary = {}

	func execute(next_operation: String, next_args: Dictionary) -> Dictionary:
		operation = next_operation
		args = next_args.duplicate(true)
		return response.duplicate(true)


func _initialize() -> void:
	var transport := FakeTransport.new()
	var gateway := PresentationGateway.new(transport)
	var context := _attempt_context()
	var event := _event(1, "world.action.harvested")
	transport.response = _success(_page([event]))
	var result: Dictionary = await gateway.get_world_presentation_events(context, "world_demo", 0, 100)
	if not result.get("ok", false):
		return _fail("A byte-closed authoritative presentation page must validate: %s" % result)
	if transport.operation != "get_world_presentation_events":
		return _fail("The additive gateway used the wrong transport operation.")
	if transport.args.get("world_id") != "world_demo" or int(transport.args.get("after_sequence", -1)) != 0:
		return _fail("The additive gateway changed the requested authority cursor.")

	var unknown := event.duplicate(true)
	unknown.event_type = "world.action.planted"
	_reseal(unknown)
	transport.response = _success(_page([unknown]))
	result = await gateway.get_world_presentation_events(context, "world_demo", 0, 100)
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_EVENT_UNSUPPORTED":
		return _fail("An unknown but self-consistently hashed event type must fail closed.")

	var tampered := event.duplicate(true)
	tampered.payload.crop_type = "pumpkin"
	transport.response = _success(_page([tampered]))
	result = await gateway.get_world_presentation_events(context, "world_demo", 0, 100)
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_PAYLOAD_HASH_MISMATCH":
		return _fail("Payload tampering must fail before anything can be projected.")

	var gap := _event(2, "world.action.harvested")
	transport.response = _success(_page([gap], 2))
	result = await gateway.get_world_presentation_events(context, "world_demo", 0, 100)
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_SEQUENCE_GAP":
		return _fail("A presentation sequence gap must not be sorted or skipped.")

	var final_mismatch := _page([event])
	final_mismatch.snapshot_state_hash = "f".repeat(64)
	transport.response = _success(final_mismatch)
	result = await gateway.get_world_presentation_events(context, "world_demo", 0, 100)
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_FINAL_SNAPSHOT_MISMATCH":
		return _fail("The event stream must close to the page's authoritative Snapshot fingerprint.")

	var revision_jump := _cross_commit_page()
	revision_jump.events[1].world_revision = 4
	revision_jump.events[1].final_snapshot_revision = 4
	revision_jump.snapshot_revision = 4
	_reseal(revision_jump.events[1])
	transport.response = _success(revision_jump)
	result = await gateway.get_world_presentation_events(context, "world_demo", 0, 100)
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_ACTION_CHAIN_MISMATCH":
		return _fail("A self-consistently rehashed cross-commit World revision jump must fail closed.")

	var final_sequence_reuse := _cross_commit_page()
	final_sequence_reuse.events[1].final_world_event_sequence = 1
	final_sequence_reuse.snapshot_last_event_sequence = 1
	_reseal(final_sequence_reuse.events[1])
	transport.response = _success(final_sequence_reuse)
	result = await gateway.get_world_presentation_events(context, "world_demo", 0, 100)
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_ACTION_CHAIN_MISMATCH":
		return _fail("A self-consistently rehashed cross-commit final World event sequence reuse must fail closed.")

	var cold_mid_commit := _event(1, "world.action.harvested")
	cold_mid_commit.action_index = 1
	cold_mid_commit.action_count = 2
	_reseal(cold_mid_commit)
	transport.response = _success(_page([cold_mid_commit]))
	result = await gateway.get_world_presentation_events(context, "world_demo", 0, 100)
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_ACTION_CHAIN_MISMATCH":
		return _fail("Cold stream sequence 1 must begin at action_index 0.")

	print("WORLD_PRESENTATION_GATEWAY_TEST_PASS")
	quit(0)


func _attempt_context() -> Dictionary:
	return {
		"schema_version": "1.0.0",
		"request_id": "req_presentation_gateway_0001",
		"trace_id": "trace_presentation_gateway_0001",
		"correlation_id": "corr_presentation_gateway_0001",
	}


func _origin_context() -> Dictionary:
	return {
		"schema_version": "1.0.0",
		"request_id": "req_presentation_gateway_0001",
		"trace_id": "trace_presentation_gateway_0001",
		"correlation_id": "corr_presentation_gateway_0001",
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


func _event(sequence: int, event_type: String) -> Dictionary:
	var event := {
		"event_id": "", "event_type": event_type, "event_version": 1,
		"schema_version": "1.0.0", "stream_id": "world-presentation:world_demo",
		"sequence": sequence, "occurred_at": "2026-08-14T01:02:03Z",
		"producer": "walnut_world_engine", "tenant_id": "tenant_demo",
		"session_id": "session_demo", "turn_id": "turn_demo_00000001",
		"command_id": "cmd_demo_00000001", "run_id": "run_demo_00000001",
		"world_id": "world_demo", "commit_id": "commit_demo_00000001",
		"world_revision": 2, "action_index": 0, "action_count": 1,
		"intent_id": "intent_demo_00000001", "state_hash_before": "1".repeat(64),
		"state_hash_after": "2".repeat(64), "final_snapshot_revision": 2,
		"final_world_event_sequence": 1, "final_snapshot_state_hash": "2".repeat(64),
		"payload": {
			"actor_entity_id": "student_avatar", "plot_id": "farm_plot_0001",
			"position": {"x": 1, "y": 2}, "crop_type": "carrot",
			"growth_stage": 3, "ready_to_harvest": true,
		},
		"payload_sha256": "", "integrity_sha256": "",
	}
	_reseal(event)
	return event


func _reseal(event: Dictionary) -> void:
	event.payload_sha256 = ContractValidator.canonical_json_sha256_v1(event.payload)
	var payload: Dictionary = event.payload
	var position: Dictionary = payload.position
	var projection := [
		event.event_type, event.event_version, event.schema_version, event.stream_id,
		event.sequence, event.occurred_at, event.producer, event.tenant_id,
		event.session_id, event.turn_id, event.command_id, event.run_id, event.world_id,
		event.commit_id, event.world_revision, event.action_index, event.action_count,
		event.intent_id, event.state_hash_before, event.state_hash_after,
		event.final_snapshot_revision, event.final_world_event_sequence,
		event.final_snapshot_state_hash, event.payload_sha256, payload.actor_entity_id,
		payload.plot_id, position.x, position.y, payload.crop_type, payload.growth_stage,
		payload.ready_to_harvest,
	]
	event.integrity_sha256 = ContractValidator.canonical_json_sha256_v1(projection)
	event.event_id = "presentation_%s" % str(event.integrity_sha256).left(32)


func _page(events: Array, high_watermark: int = 1) -> Dictionary:
	return {
		"request_context": _origin_context(), "world_id": "world_demo",
		"snapshot_revision": 2, "snapshot_last_event_sequence": 1,
		"snapshot_state_hash": "2".repeat(64),
		"presentation_high_watermark": high_watermark,
		"from_sequence": 0 if events.is_empty() else int(events[0].sequence),
		"to_sequence": 0 if events.is_empty() else int(events[-1].sequence),
		"has_more": false,
		"next_after_sequence": 0 if events.is_empty() else int(events[-1].sequence),
		"events": events.duplicate(true),
	}


func _cross_commit_page() -> Dictionary:
	var first := _event(1, "world.action.harvested")
	var second := _event(2, "world.action.harvested")
	second.commit_id = "commit_demo_00000002"
	second.turn_id = "turn_demo_00000002"
	second.command_id = "cmd_demo_00000002"
	second.run_id = "run_demo_00000002"
	second.world_revision = 3
	second.final_snapshot_revision = 3
	second.final_world_event_sequence = 2
	second.state_hash_before = "2".repeat(64)
	second.state_hash_after = "3".repeat(64)
	second.final_snapshot_state_hash = "3".repeat(64)
	second.payload.plot_id = "farm_plot_0002"
	second.payload.position.x = 2
	_reseal(second)
	var page := _page([first, second], 2)
	page.snapshot_revision = 3
	page.snapshot_last_event_sequence = 2
	page.snapshot_state_hash = "3".repeat(64)
	page.to_sequence = 2
	page.next_after_sequence = 2
	return page


func _success(value: Dictionary) -> Dictionary:
	return {
		"ok": true,
		"status": 200,
		"headers": {
			"x-request-id": "req_presentation_gateway_0001",
			"x-trace-id": "trace_presentation_gateway_0001",
			"x-correlation-id": "corr_presentation_gateway_0001",
		},
		"value": value.duplicate(true),
	}


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
