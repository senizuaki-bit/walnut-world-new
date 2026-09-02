extends SceneTree

const AgentApiTransport = preload("res://addons/yaya_contract_client/agent_api_transport.gd")
const ContractValidator = preload("res://addons/yaya_contract_client/contract_validator.gd")
const Gateway = preload("res://scripts/client/extended_agent_api_gateway.gd")


class FakeTransport:
	extends AgentApiTransport
	var response: Dictionary
	var operation := ""
	var arguments: Dictionary = {}

	func execute(next_operation: String, next_arguments: Dictionary) -> Dictionary:
		operation = next_operation
		arguments = next_arguments.duplicate(true)
		return response.duplicate(true)


func _initialize() -> void:
	var transport := FakeTransport.new()
	var gateway := Gateway.new(transport)
	var evidence := _evidence()
	transport.response = _success(evidence)
	var result: Dictionary = await gateway.get_build_rejection_evidence(
		_context(), str(evidence.evidence_ref.evidence_id),
	)
	if not result.get("ok", false) or result.value != evidence:
		return _fail("Additive BUILD_REJECTION Evidence did not pass the extended Gateway: %s" % result)
	if transport.operation != "get_evidence" or transport.arguments != {
		"request_context": _context(),
		"evidence_id": evidence.evidence_ref.evidence_id,
	}:
		return _fail("Extended Gateway changed the public get_evidence operation identity.")

	transport.response = _success(evidence)
	result = await gateway.get_evidence(_context(), str(evidence.evidence_ref.evidence_id))
	if result.get("ok", false):
		return _fail("Frozen canonical Evidence path silently accepted the additive payload.")

	var source_drift := evidence.duplicate(true)
	source_drift.source.source_id = "build_other_00000001"
	transport.response = _success(source_drift)
	result = await gateway.get_build_rejection_evidence(
		_context(), str(evidence.evidence_ref.evidence_id),
	)
	if result.get("ok", false):
		return _fail("BUILD_REJECTION Evidence accepted a mismatched Build source.")

	var hash_drift := evidence.duplicate(true)
	hash_drift.payload.failure_code = "DIFFERENT_FAILURE"
	transport.response = _success(hash_drift)
	result = await gateway.get_build_rejection_evidence(
		_context(), str(evidence.evidence_ref.evidence_id),
	)
	if result.get("ok", false):
		return _fail("BUILD_REJECTION Evidence accepted payload hash drift.")

	print("BUILD_REJECTION_EVIDENCE_GATEWAY_TEST_PASS")
	quit(0)


func _evidence() -> Dictionary:
	var payload := {
		"evidence_kind": "BUILD_REJECTION",
		"build_id": "build_rejected_00000001",
		"skill_id": "skill_watering_00000001",
		"test_suite_version": "watering-suite-v1",
		"outcome": "REJECTED",
		"failure_stage": "VALIDATE",
		"failure_code": "CPP_COMPILE_FAILED",
		"diagnostic_codes": ["CPP_PARSE_ERROR"],
	}
	var digest := ContractValidator.canonical_json_sha256_v1(payload)
	return {
		"request_context": _context(),
		"evidence_ref": {
			"evidence_id": "evidence_build_rejection_00000001",
			"evidence_type": "TEST_REPORT",
			"created_at": "2026-09-02T01:02:03Z",
			"sha256": digest,
		},
		"subject": {"learner_id": "student_demo_00000001"},
		"source": {
			"source_type": "SKILL_BUILD",
			"source_id": payload.build_id,
			"command_id": "cmd_build_rejection_00000001",
			"world_id": "world_watering_00000001",
		},
		"occurred_at": "2026-09-02T01:02:03Z",
		"recorded_at": "2026-09-02T01:02:03Z",
		"integrity": {"payload_sha256": digest, "previous_evidence_sha256": null},
		"payload": payload,
		"related_evidence": [],
		"versions": {
			"api_version": "1.0.0",
			"event_version": "1.0.0",
			"policy_version": "1.0.0",
			"world_rules_version": "1.0.0",
			"teaching_spec_version": "1.0.0",
		},
	}


func _context() -> Dictionary:
	return {
		"schema_version": "1.0.0",
		"request_id": "req_build_rejection_00000001",
		"trace_id": "trace_build_rejection_00000001",
		"correlation_id": "corr_build_rejection_00000001",
		"requested_at": "2026-09-02T01:02:03Z",
		"actor": {
			"tenant_id": "tenant_demo",
			"actor_id": "student_demo_00000001",
			"actor_type": "student",
			"roles": ["game:player"],
		},
		"content_ref": {
			"unit_id": "YAYA_FARM_001",
			"version": "1.0.0",
			"content_hash": "a".repeat(64),
		},
	}


func _success(value: Dictionary) -> Dictionary:
	return {
		"ok": true,
		"status": 200,
		"headers": {
			"x-request-id": "req_build_rejection_00000001",
			"x-trace-id": "trace_build_rejection_00000001",
			"x-correlation-id": "corr_build_rejection_00000001",
			"etag": "\"%s\"" % value.integrity.payload_sha256,
		},
		"value": value.duplicate(true),
	}


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
