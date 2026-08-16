extends SceneTree

const GatewayScript := preload("res://scripts/client/product_capability_gateway.gd")


class FakeTransport:
	extends RefCounted
	var response: Dictionary
	var operation := ""
	var arguments: Dictionary = {}

	func execute(next_operation: String, next_arguments: Dictionary) -> Dictionary:
		operation = next_operation
		arguments = next_arguments.duplicate(true)
		return response.duplicate(true)


func _initialize() -> void:
	var transport := FakeTransport.new()
	var gateway := GatewayScript.new(transport)
	transport.response = _success(_capability(true))
	var result: Dictionary = await gateway.get_product_capabilities(
		_attempt(), _origin().actor, _origin().content_ref,
	)
	if not result.get("ok", false):
		return _fail("Closed v0.6 ProductCapability should validate: %s" % result)
	if transport.operation != "get_product_capabilities" or transport.arguments != {"attempt_context": _attempt()}:
		return _fail("Product capability GET changed its exact read-only transport identity.")

	var auto_build := _capability(true)
	auto_build.skill_patch_constraints.auto_build = true
	transport.response = _success(auto_build)
	result = await gateway.get_product_capabilities(_attempt(), _origin().actor, _origin().content_ref)
	if result.get("ok", false):
		return _fail("A capability that permits automatic Build must fail closed.")

	var actor_drift := _capability(true)
	actor_drift.request_context.actor.actor_id = "student_other"
	transport.response = _success(actor_drift)
	result = await gateway.get_product_capabilities(_attempt(), _origin().actor, _origin().content_ref)
	if result.get("ok", false):
		return _fail("Capability actor drift was accepted as student authority.")

	var extra_field := _capability(false)
	extra_field["silent_rollout"] = true
	transport.response = _success(extra_field)
	result = await gateway.get_product_capabilities(_attempt(), _origin().actor, _origin().content_ref)
	if result.get("ok", false):
		return _fail("Capability additionalProperties drift was silently ignored.")
	print("PRODUCT_CAPABILITY_GATEWAY_TEST_PASS")
	quit(0)


func _capability(skill_patch_enabled: bool) -> Dictionary:
	return {
		"request_context": _origin(),
		"api_version": "1.2.0",
		"contract_version": "0.6.0",
		"world_presentation_enabled": true,
		"skill_patch_enabled": skill_patch_enabled,
		"skill_patch_constraints": {
			"request_mode": "EXPLICIT_UI_ACTION",
			"selection_target": "FAILED_INTERACTION",
			"agent_role": "teaching_agent",
			"scenario": "RECTIFICATION",
			"required_hint_level": 4,
			"operation": "UPSERT_FILE",
			"target": "CURRENT_ENTRYPOINT",
			"max_files": 1,
			"max_operations": 1,
			"requires_failed_evidence": true,
			"cas_required": true,
			"requires_student_confirmation": true,
			"auto_build": false,
			"auto_activate": false,
			"auto_run": false,
		},
	}


func _attempt() -> Dictionary:
	return {
		"schema_version": "1.0.0", "request_id": "req_capability_00000001",
		"trace_id": "trace_capability_00000001",
		"correlation_id": "corr_capability_00000001",
	}


func _origin() -> Dictionary:
	return {
		"schema_version": "1.0.0", "request_id": "req_capability_00000001",
		"trace_id": "trace_capability_00000001",
		"correlation_id": "corr_capability_00000001",
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


func _success(value: Dictionary) -> Dictionary:
	return {
		"ok": true, "status": 200,
		"headers": {
			"x-request-id": "req_capability_00000001",
			"x-trace-id": "trace_capability_00000001",
			"x-correlation-id": "corr_capability_00000001",
		},
		"value": value.duplicate(true),
	}


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
