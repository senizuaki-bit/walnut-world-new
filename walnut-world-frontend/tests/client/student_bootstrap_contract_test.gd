extends SceneTree

const Validator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const Gateway := preload("res://addons/yaya_contract_client/agent_api_gateway.gd")
const Transport := preload("res://addons/yaya_contract_client/agent_api_transport.gd")
const HttpTransport := preload("res://addons/yaya_contract_client/http_agent_api_transport.gd")


class FixtureTransport:
	extends Transport
	var value: Dictionary

	func _init(fixture: Dictionary) -> void:
		value = fixture

	func execute(operation: String, arguments: Dictionary) -> Dictionary:
		var attempt: Dictionary = arguments.attempt_context
		return {
			"ok": true,
			"status": 200,
			"headers": {
				"x-request-id": attempt.request_id,
				"x-trace-id": attempt.trace_id,
				"x-correlation-id": attempt.correlation_id,
				"x-schema-version": attempt.schema_version,
			},
			"value": value.duplicate(true),
		}


func _initialize() -> void:
	var fixture := _bootstrap()
	var fixture_guard := Validator.validate_student_bootstrap_v2(fixture)
	if not fixture_guard.ok:
		push_error("Valid StudentBootstrap fixture was rejected: %s" % fixture_guard.error)
		quit(1)
		return
	var extra := fixture.duplicate(true)
	extra["unexpected"] = true
	var stale := fixture.duplicate(true)
	stale.activation.active.registry_revision = 6
	if Validator.validate_student_bootstrap_v2(extra).ok or Validator.validate_student_bootstrap_v2(stale).ok:
		push_error("StudentBootstrap validator must reject unknown fields and stale active tuple revisions.")
		quit(1)
		return
	var attempt := {
		"schema_version": "1.0.0",
		"request_id": "req_student_bootstrap_0001",
		"trace_id": "trace_student_bootstrap_0001",
		"correlation_id": "corr_student_bootstrap_0001",
	}
	var gateway := Gateway.new(FixtureTransport.new(fixture))
	var result: Dictionary = await gateway.get_student_bootstrap(attempt)
	if not result.get("ok", false):
		push_error("Gateway must dispatch get_student_bootstrap through strict validation: %s" % str(result))
		quit(1)
		return
	var http := HttpTransport.new(root, "http://127.0.0.1:8790", "token")
	var path_spec: Dictionary = http._build_request_spec("get_student_bootstrap", {})
	var content_spec: Dictionary = http._build_request_spec("get_product_content_unit", {
		"content_ref": fixture.content,
	})
	if (
		not path_spec.get("ok", false)
		or str(path_spec.spec.path) != "/v1/student-bootstrap"
		or not content_spec.get("ok", false)
		or not str(content_spec.spec.path).ends_with("content_hash=%s" % fixture.content.content_hash)
	):
		push_error("HTTP transport must map StudentBootstrap and URI-encode string content_hash query values.")
		quit(1)
		return
	http.shutdown()
	print("STUDENT_BOOTSTRAP_CONTRACT_TEST_PASS")
	quit(0)


func _bootstrap() -> Dictionary:
	var actor := {
		"tenant_id": "tenant_demo",
		"actor_id": "learner_demo_0001",
		"actor_type": "student",
		"roles": ["student"],
	}
	var content := {
		"unit_id": "TASK_DEMO_001",
		"version": "1.0.0",
		"content_hash": "a".repeat(64),
	}
	return {
		"request_context": {
			"schema_version": "1.0.0",
			"request_id": "req_student_context_0001",
			"correlation_id": "corr_student_context_0001",
			"trace_id": "trace_student_context_0001",
			"requested_at": "2026-08-12T00:00:00Z",
			"actor": actor,
			"content_ref": content,
		},
		"api_version": "1.1.0",
		"contract_version": "0.4.0",
		"server_time": "2026-08-12T00:00:00Z",
		"actor": actor,
		"content": content,
		"capabilities": {
			"skill_builds": true,
			"skill_activations": true,
			"agent_sessions": true,
			"http_world_recovery": true,
			"evidence_query": true,
		},
		"session": {
			"current_session_id": null,
			"teaching_spec_version": "agent-teaching-v1",
			"create_request": {
				"world_id": "world_demo_0001",
				"learner_id": "learner_demo_0001",
				"agent_profile_id": "profile_demo_0001",
				"channel": "GAME",
				"locale": "zh-CN",
				"content": content,
				"expected_world_revision": 5,
			},
		},
		"build": {
			"build_policy_id": "policy_demo_0001",
			"compiler_profile": "YAYA_CPP20_SAFE_V1",
			"compiler_version": "clang-20.1.0",
			"sandbox_image_digest": "sha256:" + "b".repeat(64),
			"test_suite_version": "farm-water-v3",
			"allowed_capabilities": ["WORLD_READ", "WATER"],
			"max_source_files": 32,
			"max_source_bytes": 1048576,
		},
		"activation": {
			"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"},
			"registry_revision": 7,
			"active": {
				"activation_id": "activation_demo_0001",
				"skill_id": "skill_demo_0001",
				"skill_version_id": "skillver_demo_0001",
				"artifact_sha256": "c".repeat(64),
				"certification_id": "cert_demo_0001",
				"registry_revision": 7,
				"activated_at": "2026-08-12T00:00:00Z",
			},
		},
		"world": {
			"world_id": "world_demo_0001",
			"revision": 5,
			"last_event_sequence": 12,
			"state_hash": "d".repeat(64),
			"snapshot_url": "/v1/worlds/world_demo_0001/snapshot",
			"events_url": "/v1/worlds/world_demo_0001/events",
		},
	}
