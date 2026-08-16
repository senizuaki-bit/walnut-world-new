extends SceneTree

const ContractValidator = preload("res://contract_validator.gd")
const AgentApiGateway = preload("res://agent_api_gateway.gd")
const HttpAgentApiTransport = preload("res://http_agent_api_transport.gd")


class FixtureTransport:
	extends "res://agent_api_transport.gd"
	var result: Dictionary
	var last_operation: String = ""
	var last_arguments: Dictionary = {}

	func _init(next_result: Dictionary) -> void:
		result = next_result

	func execute(operation: String, arguments: Dictionary) -> Dictionary:
		last_operation = operation
		last_arguments = arguments
		await Engine.get_main_loop().process_frame
		return result


func _initialize() -> void:
	call_deferred("_run_tests")


func _run_tests() -> void:
	var bootstrap: Dictionary = _example("game-bootstrap-response.json")
	var student_bootstrap: Dictionary = _example("game-student-bootstrap-v2.json")
	var skill_build: Dictionary = _example("game-skill-build.json")
	var skill_activation: Dictionary = _example("game-skill-activation.json")
	var agent_session: Dictionary = _example("game-agent-session.json")
	var run: Dictionary = _example("game-run.json")
	var evidence: Dictionary = _example("game-evidence.json")
	var snapshot: Dictionary = _example("game-world-snapshot.json")
	var bootstrap_check := ContractValidator.validate_bootstrap_response(bootstrap)
	if not bootstrap_check.ok:
		print("BOOTSTRAP_CHECK=", bootstrap_check)
	assert(bootstrap_check.ok)
	assert(ContractValidator.validate_student_bootstrap_v2(student_bootstrap).ok)
	assert(ContractValidator.validate_agent_session_create_request(
		student_bootstrap.session.create_request
	).ok)
	var stale_student_session_request := student_bootstrap.duplicate(true)
	stale_student_session_request.session.create_request.expected_world_revision += 1
	assert(not ContractValidator.validate_student_bootstrap_v2(stale_student_session_request).ok)
	var incomplete_student_session_request := student_bootstrap.duplicate(true)
	incomplete_student_session_request.session.create_request.erase("locale")
	assert(not ContractValidator.validate_student_bootstrap_v2(incomplete_student_session_request).ok)
	var leaked_student_teaching_authority := student_bootstrap.duplicate(true)
	leaked_student_teaching_authority.session.create_request["teaching_spec_version"] = "agent-teaching-v1"
	assert(not ContractValidator.validate_student_bootstrap_v2(leaked_student_teaching_authority).ok)
	var wrong_student_contract_version := student_bootstrap.duplicate(true)
	wrong_student_contract_version.contract_version = "0.4.1"
	assert(not ContractValidator.validate_student_bootstrap_v2(wrong_student_contract_version).ok)
	var wrong_student_world := student_bootstrap.duplicate(true)
	wrong_student_world.activation.scope.world_id = "world_other_001"
	assert(not ContractValidator.validate_student_bootstrap_v2(wrong_student_world).ok)
	var wrong_student_registry := student_bootstrap.duplicate(true)
	wrong_student_registry.activation.active.registry_revision += 1
	assert(not ContractValidator.validate_student_bootstrap_v2(wrong_student_registry).ok)
	var open_student_shape := student_bootstrap.duplicate(true)
	open_student_shape["unexpected"] = true
	assert(not ContractValidator.validate_student_bootstrap_v2(open_student_shape).ok)
	var drifted_source_file_limit := bootstrap.duplicate(true)
	drifted_source_file_limit.limits.max_source_files = 64
	assert(not ContractValidator.validate_bootstrap_response(drifted_source_file_limit).ok)
	var drifted_source_byte_limit := bootstrap.duplicate(true)
	drifted_source_byte_limit.limits.max_source_bytes = 1048575
	assert(not ContractValidator.validate_bootstrap_response(drifted_source_byte_limit).ok)
	var missing_stream_id := bootstrap.duplicate(true)
	missing_stream_id.world.erase("stream_id")
	assert(not ContractValidator.validate_bootstrap_response(missing_stream_id).ok)
	var wrong_stream_id := bootstrap.duplicate(true)
	wrong_stream_id.world.stream_id = "world:world_other_001"
	assert(not ContractValidator.validate_bootstrap_response(wrong_stream_id).ok)
	var wrong_stream_protocol := bootstrap.duplicate(true)
	wrong_stream_protocol.world.stream_protocol_version = "2.0.0"
	assert(not ContractValidator.validate_bootstrap_response(wrong_stream_protocol).ok)
	for unsafe_stream_url in [
		"ws://api.yaya.example/v1/realtime",
		"wss://api.yaya.example/v1/realtime?token=secret",
		"wss://user:secret@api.yaya.example/v1/realtime",
	]:
		var unsafe_stream_bootstrap := bootstrap.duplicate(true)
		unsafe_stream_bootstrap.world.stream_url = unsafe_stream_url
		assert(not ContractValidator.validate_bootstrap_response(unsafe_stream_bootstrap).ok)
	assert(ContractValidator.validate_skill_build(skill_build).ok)
	var duplicate_phase_diagnostic := skill_build.duplicate(true)
	duplicate_phase_diagnostic.phases[0].diagnostic_codes = ["DUPLICATE_CODE", "DUPLICATE_CODE"]
	assert(not ContractValidator.validate_skill_build(duplicate_phase_diagnostic).ok)
	assert(ContractValidator.validate_skill_activation(skill_activation).ok)
	var skipped_registry_revision := skill_activation.duplicate(true)
	skipped_registry_revision.registry_revision = skipped_registry_revision.previous_registry_revision + 2
	assert(not ContractValidator.validate_skill_activation(skipped_registry_revision).ok)
	assert(ContractValidator.validate_agent_session(agent_session).ok)
	for valid_uri_reference in [
		"../worlds/1", "//example.com/path", "resource%20name", "?query=value", "#fragment",
	]:
		var valid_link_session := agent_session.duplicate(true)
		valid_link_session.links.self = valid_uri_reference
		assert(ContractValidator.validate_agent_session(valid_link_session).ok)
	for invalid_uri_reference in ["", "%zz", "[", "a\\b", "a|b", "a{b}", "a^b", "://bad", "1http://x", "x".repeat(2049)]:
		var invalid_link_session := agent_session.duplicate(true)
		invalid_link_session.links.self = invalid_uri_reference
		assert(not ContractValidator.validate_agent_session(invalid_link_session).ok)
		var invalid_bootstrap_link := bootstrap.duplicate(true)
		invalid_bootstrap_link.world.snapshot_url = invalid_uri_reference
		assert(not ContractValidator.validate_bootstrap_response(invalid_bootstrap_link).ok)
	assert(ContractValidator.validate_run(run).ok)
	var missing_run_owner := run.duplicate(true)
	missing_run_owner.erase("session_id")
	assert(not ContractValidator.validate_run(missing_run_owner).ok)
	var half_null_run_owner := run.duplicate(true)
	half_null_run_owner.session_id = null
	assert(not ContractValidator.validate_run(half_null_run_owner).ok)
	var non_agent_run := run.duplicate(true)
	non_agent_run.session_id = null
	non_agent_run.turn_id = null
	non_agent_run.agent_feedback = null
	assert(ContractValidator.validate_run(non_agent_run).ok)
	for owner_field in ["session_id", "turn_id"]:
		var wrong_feedback_owner := run.duplicate(true)
		wrong_feedback_owner.agent_feedback[owner_field] = "%s_wrong_0001" % owner_field.trim_suffix("_id")
		assert(not ContractValidator.validate_run(wrong_feedback_owner).ok)
	var conflicting_evidence_id := run.duplicate(true)
	var conflicting_ref: Dictionary = conflicting_evidence_id.evidence_refs[0].duplicate(true)
	conflicting_ref.sha256 = "f".repeat(64)
	conflicting_ref.created_at = "2026-08-07T14:00:01Z"
	conflicting_evidence_id.evidence_refs.append(conflicting_ref)
	conflicting_evidence_id.agent_feedback.evidence_refs.append(conflicting_ref.duplicate(true))
	assert(not ContractValidator.validate_run(conflicting_evidence_id).ok)
	var maximum_water := run.duplicate(true)
	maximum_water.sandbox.action_intents[0].amount_ml = 10000
	assert(ContractValidator.validate_run(maximum_water).ok)
	var excessive_water := maximum_water.duplicate(true)
	excessive_water.sandbox.action_intents[0].amount_ml = 10001
	assert(not ContractValidator.validate_run(excessive_water).ok)
	var invalid_interaction := run.duplicate(true)
	invalid_interaction.sandbox.action_intents[0] = {
		"intent_id": "intent_interact_0001",
		"action_type": "INTERACT",
		"actor_entity_id": "agent_entity_001",
		"expected_world_revision": 184,
		"target_entity_id": "target_entity_001",
		"interaction": "INVALID UPPERCASE",
	}
	assert(not ContractValidator.validate_run(invalid_interaction).ok)
	var empty_evidence_uri := run.duplicate(true)
	empty_evidence_uri.evidence_refs[0].uri = ""
	empty_evidence_uri.agent_feedback.evidence_refs[0].uri = ""
	assert(not ContractValidator.validate_run(empty_evidence_uri).ok)
	var excessive_model_version := run.duplicate(true)
	excessive_model_version.versions.model_version = "v".repeat(129)
	assert(not ContractValidator.validate_run(excessive_model_version).ok)
	var excessive_world_rules_version := snapshot.duplicate(true)
	excessive_world_rules_version.world_rules_version = "v".repeat(97)
	assert(not ContractValidator.validate_world_snapshot(excessive_world_rules_version).ok)
	assert(ContractValidator.validate_evidence(evidence).ok)
	var canonical_json_vector := {
		"z": [3, {"β": "芽芽", "a": true}],
		"a": {"汉字": "浇水", "n": 0},
		"m": null,
	}
	assert(
		ContractValidator.canonical_json_sha256_v1(canonical_json_vector)
		== "cf981d59e76cab7b309d61a629c6f6c5dd4e3e324ec797d84cd852f719bc625d"
	)
	assert(not ContractValidator.canonical_json_sha256_v1({"emoji": String.chr(0x1F331)}).is_empty())
	var tampered_evidence_payload := evidence.duplicate(true)
	tampered_evidence_payload.payload.state_hash = "0".repeat(64)
	assert(not ContractValidator.validate_evidence(tampered_evidence_payload).ok)
	var leap_day_bootstrap := bootstrap.duplicate(true)
	leap_day_bootstrap.server_time = "2024-02-29T23:59:59.123+23:59"
	assert(ContractValidator.validate_bootstrap_response(leap_day_bootstrap).ok)
	var lowercase_rfc3339_bootstrap := bootstrap.duplicate(true)
	lowercase_rfc3339_bootstrap.server_time = "2026-08-07t10:00:00z"
	assert(ContractValidator.validate_bootstrap_response(lowercase_rfc3339_bootstrap).ok)
	for invalid_date_time in [
		"2026-99-99T99:99:99Z",
		"2026-02-29T12:00:00Z",
		"2026-04-31T12:00:00Z",
		"2026-12-31T24:00:00Z",
		"2026-12-31T23:59:60Z",
		"2026-12-31T23:59:59+24:00",
		"2026-12-31T23:59:59+00:60",
	]:
		var invalid_time_bootstrap := bootstrap.duplicate(true)
		invalid_time_bootstrap.server_time = invalid_date_time
		assert(not ContractValidator.validate_bootstrap_response(invalid_time_bootstrap).ok)
	var skipped_evidence_revision := evidence.duplicate(true)
	skipped_evidence_revision.payload.world_revision = skipped_evidence_revision.payload.previous_revision + 2
	assert(not ContractValidator.validate_evidence(skipped_evidence_revision).ok)

	var skill_build_request: Dictionary = _example("game-skill-build-create-request.json")
	var agent_session_request: Dictionary = _example("game-agent-session-create-request.json")
	var agent_turn_request: Dictionary = _example("game-agent-turn-create-request.json")
	var activation_request: Dictionary = _example("game-skill-activation-request.json")
	var client_event_batch: Dictionary = _example("game-client-event-batch-request.json")
	assert(ContractValidator.validate_skill_build_create_request(skill_build_request).ok)
	assert(ContractValidator.validate_agent_session_create_request(agent_session_request).ok)
	assert(ContractValidator.validate_agent_turn_create_request(agent_turn_request).ok)
	assert(ContractValidator.validate_skill_activation_request(activation_request).ok)
	assert(ContractValidator.validate_client_event_batch_request(client_event_batch).ok)
	var unsafe_source := skill_build_request.duplicate(true)
	unsafe_source.source_bundle.files[0].path = "../secret.cpp"
	assert(not ContractValidator.validate_skill_build_create_request(unsafe_source).ok)
	var forged_source_hash := skill_build_request.duplicate(true)
	forged_source_hash.source_bundle.files[0].content_sha256 = "0".repeat(64)
	assert(not ContractValidator.validate_skill_build_create_request(forged_source_hash).ok)
	var duplicate_source_path := skill_build_request.duplicate(true)
	var duplicate_file: Dictionary = duplicate_source_path.source_bundle.files[0].duplicate(true)
	duplicate_file.content += "// duplicate path\n"
	duplicate_file.content_sha256 = duplicate_file.content.sha256_text()
	duplicate_source_path.source_bundle.files.push_back(duplicate_file)
	assert(not ContractValidator.validate_skill_build_create_request(duplicate_source_path).ok)
	var missing_entrypoint := skill_build_request.duplicate(true)
	missing_entrypoint.source_bundle.entrypoint = "src/missing.cpp"
	assert(not ContractValidator.validate_skill_build_create_request(missing_entrypoint).ok)
	var too_many_source_files := skill_build_request.duplicate(true)
	for index in range(1, 33):
		var extra_content := "int source_file_%d = %d;\n" % [index, index]
		too_many_source_files.source_bundle.files.push_back({
			"path": "src/file_%d.cpp" % index,
			"content": extra_content,
			"content_sha256": extra_content.sha256_text(),
		})
	assert(not ContractValidator.validate_skill_build_create_request(too_many_source_files).ok)
	var oversized_multibyte_bundle := skill_build_request.duplicate(true)
	var multibyte_content := "芽".repeat(174763)
	oversized_multibyte_bundle.source_bundle.entrypoint = "src/multibyte_0.cpp"
	oversized_multibyte_bundle.source_bundle.files = []
	for index in range(2):
		oversized_multibyte_bundle.source_bundle.files.push_back({
			"path": "src/multibyte_%d.cpp" % index,
			"content": multibyte_content,
			"content_sha256": multibyte_content.sha256_text(),
		})
	assert(not ContractValidator.validate_skill_build_create_request(oversized_multibyte_bundle).ok)
	var wrong_turn_input := agent_turn_request.duplicate(true)
	wrong_turn_input.input["unexpected"] = true
	assert(not ContractValidator.validate_agent_turn_create_request(wrong_turn_input).ok)
	var wrong_client_payload := client_event_batch.duplicate(true)
	wrong_client_payload.events[0].payload.changed_file_count = 0
	assert(not ContractValidator.validate_client_event_batch_request(wrong_client_payload).ok)
	var corrupt_build := skill_build.duplicate(true)
	corrupt_build.artifact.artifact_sha256 = "not-a-hash"
	assert(not ContractValidator.validate_skill_build(corrupt_build).ok)
	var corrupt_run := run.duplicate(true)
	corrupt_run.sandbox.status = "RUNNING"
	assert(not ContractValidator.validate_run(corrupt_run).ok)
	var corrupt_evidence := evidence.duplicate(true)
	corrupt_evidence.payload["unexpected"] = true
	assert(not ContractValidator.validate_evidence(corrupt_evidence).ok)

	var command := _valid_command()
	assert(ContractValidator.validate_command_result(command).ok)
	var resource_command := command.duplicate(true)
	resource_command.command_type = "CREATE_AGENT_SESSION"
	resource_command.result = {
		"result_type": "RESOURCE_CREATED",
		"resource_type": "AGENT_SESSION",
		"resource_id": "session_contract_0001",
		"resource_url": "/v1/agent-sessions/session_contract_0001",
	}
	assert(ContractValidator.validate_command_result(resource_command).ok)
	for invalid_resource_url in ["", "%zz", "[", "a\\b", "a|b", "a{b}", "a^b", "://bad", "1http://x", "x".repeat(2049)]:
		var invalid_resource_command := resource_command.duplicate(true)
		invalid_resource_command.result.resource_url = invalid_resource_url
		assert(not ContractValidator.validate_command_result(invalid_resource_command).ok)
		var invalid_command_link := command.duplicate(true)
		invalid_command_link.links.self = invalid_resource_url
		assert(not ContractValidator.validate_command_result(invalid_command_link).ok)
	var missing_command_revision := command.duplicate(true)
	missing_command_revision.erase("revision")
	assert(not ContractValidator.validate_command_result(missing_command_revision).ok)
	var zero_command_revision := command.duplicate(true)
	zero_command_revision.revision = 0
	assert(not ContractValidator.validate_command_result(zero_command_revision).ok)
	var string_command_revision := command.duplicate(true)
	string_command_revision.revision = "5"
	assert(not ContractValidator.validate_command_result(string_command_revision).ok)

	var false_success := command.duplicate(true)
	false_success.result = null
	assert(not ContractValidator.validate_command_result(false_success).ok)
	var skipped_revision := command.duplicate(true)
	skipped_revision.result.world_revision = skipped_revision.result.previous_revision + 2
	assert(not ContractValidator.validate_command_result(skipped_revision).ok)

	var coercion_attempt := command.duplicate(true)
	coercion_attempt.request_context.actor["unexpected"] = true
	assert(not ContractValidator.validate_command_result(coercion_attempt).ok)

	var unknown := command.duplicate(true)
	unknown.status = "UNKNOWN"
	unknown.stage = "WORLD_COMMIT"
	unknown.result = null
	unknown.error = {
		"code": "UNKNOWN_COMMIT_STATE",
		"category": "DEPENDENCY",
		"retryable": false,
		"user_message_key": "command.reconciling",
		"stage": "WORLD_COMMIT",
	}
	assert(ContractValidator.validate_command_result(unknown).ok)
	var empty_error_message := unknown.duplicate(true)
	empty_error_message.error.message = ""
	assert(not ContractValidator.validate_command_result(empty_error_message).ok)
	unknown.terminal = false
	assert(not ContractValidator.validate_command_result(unknown).ok)
	var unknown_error_response: Dictionary = _unknown_commit_failure("cmd_contract_0001").error
	assert(ContractValidator.validate_error_response(unknown_error_response).ok)
	var unknown_error_without_command := unknown_error_response.duplicate(true)
	unknown_error_without_command.erase("command_id")
	assert(not ContractValidator.validate_error_response(unknown_error_without_command).ok)
	var unknown_code_with_failed_status := unknown_error_response.duplicate(true)
	unknown_code_with_failed_status.status = "FAILED"
	assert(not ContractValidator.validate_error_response(unknown_code_with_failed_status).ok)
	var ordinary_code_with_unknown_status := unknown_error_response.duplicate(true)
	ordinary_code_with_unknown_status.error = _server_failure().error.error
	assert(not ContractValidator.validate_error_response(ordinary_code_with_unknown_status).ok)

	var first_event := _valid_event(732, "evt_world_00000001")
	var second_event := _valid_event(733, "evt_world_00000002")
	second_event.causation_id = first_event.event_id
	assert(ContractValidator.validate_event(first_event, 731).ok)
	assert(ContractValidator.validate_event(first_event, 730).error.code == "LOCAL_EVENT_SEQUENCE_GAP")
	var runtime_event := _valid_runtime_event()
	assert(ContractValidator.validate_runtime_event(runtime_event, 0).ok)
	_assert_command_status_graph()
	var feedback_ready: Dictionary = _example("runtime-agent-turn-feedback-ready.json")
	assert(ContractValidator.validate_runtime_event(feedback_ready, 17).ok)
	var fallback_feedback := feedback_ready.duplicate(true)
	fallback_feedback.payload.source = "provider_fallback"
	fallback_feedback.payload.degraded = true
	fallback_feedback.payload.fallback_reason = "PROVIDER_TIMEOUT"
	assert(ContractValidator.validate_runtime_event(fallback_feedback, 17).ok)
	var feedback_without_run := feedback_ready.duplicate(true)
	feedback_without_run.payload.run_id = null
	assert(ContractValidator.validate_runtime_event(feedback_without_run, 17).ok)
	var wrong_feedback_command := feedback_ready.duplicate(true)
	wrong_feedback_command.payload.command_id = "cmd_feedback_other_0001"
	assert(not ContractValidator.validate_runtime_event(wrong_feedback_command, 17).ok)
	var contradictory_provider_feedback := feedback_ready.duplicate(true)
	contradictory_provider_feedback.payload.source = "provider_fallback"
	assert(not ContractValidator.validate_runtime_event(contradictory_provider_feedback, 17).ok)
	var contradictory_fallback_feedback := feedback_ready.duplicate(true)
	contradictory_fallback_feedback.payload.degraded = true
	contradictory_fallback_feedback.payload.fallback_reason = "PROVIDER_TIMEOUT"
	assert(not ContractValidator.validate_runtime_event(contradictory_fallback_feedback, 17).ok)
	var missing_fallback_reason := fallback_feedback.duplicate(true)
	missing_fallback_reason.payload.fallback_reason = null
	assert(not ContractValidator.validate_runtime_event(missing_fallback_reason, 17).ok)
	var feedback_with_unknown_field := feedback_ready.duplicate(true)
	feedback_with_unknown_field.payload["unexpected"] = true
	assert(not ContractValidator.validate_runtime_event(feedback_with_unknown_field, 17).ok)
	var activation_applied := _runtime_event_with_payload("skill.activation.applied", {
		"skill_id": "skill_water_001",
		"skill_version_id": "skillver_water_001",
		"certification_id": "cert_water_001",
		"artifact_sha256": "c".repeat(64),
		"activation_scope": {
			"world_id": "world_demo_001",
			"agent_profile_id": "agent_farmer_001",
		},
		"previous_registry_revision": 17,
		"registry_revision": 18,
		"activated_at": "2026-08-06T10:00:01Z",
	})
	assert(ContractValidator.validate_runtime_event(activation_applied, 0).ok)
	var skipped_activation_revision := activation_applied.duplicate(true)
	skipped_activation_revision.payload.registry_revision = 19
	assert(not ContractValidator.validate_runtime_event(skipped_activation_revision, 0).ok)
	var repeated_activation_revision := activation_applied.duplicate(true)
	repeated_activation_revision.payload.registry_revision = 17
	assert(not ContractValidator.validate_runtime_event(repeated_activation_revision, 0).ok)
	var learner_model_updated := _runtime_event_with_payload("learner.model.updated", {
		"learner_id": "learner_0001",
		"previous_revision": 8,
		"learner_revision": 9,
		"projected_through_sequence": 42,
		"changed_competency_ids": ["watering"],
		"updated_at": "2026-08-06T10:00:01Z",
		"evidence_refs": [],
	})
	assert(ContractValidator.validate_runtime_event(learner_model_updated, 0).ok)
	var skipped_learner_revision := learner_model_updated.duplicate(true)
	skipped_learner_revision.payload.learner_revision = 10
	assert(not ContractValidator.validate_runtime_event(skipped_learner_revision, 0).ok)
	var repeated_learner_revision := learner_model_updated.duplicate(true)
	repeated_learner_revision.payload.learner_revision = 8
	assert(not ContractValidator.validate_runtime_event(repeated_learner_revision, 0).ok)
	var unknown_runtime_event := runtime_event.duplicate(true)
	unknown_runtime_event.event_type = "world.plot_watered"
	assert(not ContractValidator.validate_runtime_event(unknown_runtime_event, 0).ok)
	var corrupt_runtime_payload := runtime_event.duplicate(true)
	corrupt_runtime_payload.payload.status = "RUNNING"
	assert(not ContractValidator.validate_runtime_event(corrupt_runtime_payload, 0).ok)

	var page := {
		"request_context": _request_context(),
		"world_id": "world_demo_001",
		"snapshot_revision": 185,
		"from_sequence": 732,
		"to_sequence": 733,
		"has_more": false,
		"next_after_sequence": 733,
		"events": [first_event, second_event],
	}
	assert(ContractValidator.validate_world_event_page(page, 731).ok)
	var gap_page := page.duplicate(true)
	gap_page.events[1].sequence = 734
	gap_page.to_sequence = 734
	gap_page.next_after_sequence = 734
	assert(not ContractValidator.validate_world_event_page(gap_page, 731).ok)
	var duplicate_page := page.duplicate(true)
	duplicate_page.events[1].event_id = duplicate_page.events[0].event_id
	assert(not ContractValidator.validate_world_event_page(duplicate_page, 731).ok)

	var snapshot_for_gateway := snapshot.duplicate(true)
	snapshot_for_gateway.request_context = _request_context()
	var snapshot_headers := {
		"ETag": "\"snapshot_contract_0001\"",
		"X-World-Revision": String.num_int64(snapshot_for_gateway.revision),
	}
	var missing_snapshot_revision := snapshot_for_gateway.duplicate(true)
	missing_snapshot_revision.erase("revision")
	var malformed_snapshot_gateway := AgentApiGateway.new(
		FixtureTransport.new(_success(missing_snapshot_revision, 200, snapshot_headers))
	)
	var missing_snapshot_result: Dictionary = await malformed_snapshot_gateway.get_world_snapshot(
		_request_context(), snapshot_for_gateway.world_id,
	)
	assert(not missing_snapshot_result.ok)
	assert(missing_snapshot_result.size() == 4)
	assert(missing_snapshot_result.status == 0)
	assert(missing_snapshot_result.headers.is_empty())
	assert(missing_snapshot_result.error.code == "LOCAL_CONTRACT_RESPONSE_INVALID")
	var string_snapshot_revision := snapshot_for_gateway.duplicate(true)
	string_snapshot_revision.revision = String.num_int64(snapshot_for_gateway.revision)
	malformed_snapshot_gateway.set_transport(
		FixtureTransport.new(_success(string_snapshot_revision, 200, snapshot_headers))
	)
	assert(
		(await malformed_snapshot_gateway.get_world_snapshot(
			_request_context(), snapshot_for_gateway.world_id,
		)).error.code == "LOCAL_CONTRACT_RESPONSE_INVALID"
	)
	var missing_page_revision := page.duplicate(true)
	missing_page_revision.erase("snapshot_revision")
	var malformed_page_gateway := AgentApiGateway.new(FixtureTransport.new(_success(
		missing_page_revision,
		200,
		{"X-World-Revision": String.num_int64(page.snapshot_revision)},
	)))
	var missing_page_result: Dictionary = await malformed_page_gateway.get_world_events(
		_request_context(), page.world_id, 731,
	)
	assert(not missing_page_result.ok)
	assert(missing_page_result.size() == 4)
	assert(missing_page_result.status == 0)
	assert(missing_page_result.headers.is_empty())
	assert(missing_page_result.error.code == "LOCAL_CONTRACT_RESPONSE_INVALID")

	var gateway := AgentApiGateway.new(FixtureTransport.new(_success(command)))
	var gateway_result: Dictionary = await gateway.get_command(_request_context(), command.command_id)
	assert(gateway_result.ok)
	assert(gateway_result.size() == 4)
	assert(gateway_result.status == 200)
	assert(gateway_result.headers["x-request-id"] == _request_context().request_id)
	assert(gateway_result.headers["x-trace-id"] == _request_context().trace_id)
	assert(gateway_result.headers["x-correlation-id"] == _request_context().correlation_id)
	assert(gateway_result.value.command_id == command.command_id)
	var activation_for_gateway := skill_activation.duplicate(true)
	activation_for_gateway.request_context = _request_context()
	var activation_gateway := AgentApiGateway.new(FixtureTransport.new(_success(activation_for_gateway)))
	var activation_result: Dictionary = await activation_gateway.get_skill_activation(
		_request_context(), activation_for_gateway.activation_id
	)
	assert(activation_result.ok)
	assert(activation_result.value.activation_id == activation_for_gateway.activation_id)
	var student_transport := FixtureTransport.new(_success(student_bootstrap))
	var student_gateway := AgentApiGateway.new(student_transport)
	var student_result: Dictionary = await student_gateway.get_student_bootstrap({
		"schema_version": "1.0.0",
		"request_id": "req_contract_0001",
		"trace_id": "trace_contract_0001",
		"correlation_id": "corr_contract_0001",
	})
	assert(student_result.ok)
	assert(student_result.value.contract_version == "0.4.0")
	assert(student_transport.last_operation == "get_student_bootstrap")
	var wrong_headers := _success(command)
	wrong_headers.headers["X-Request-Id"] = "req_other_00000001"
	gateway.set_transport(FixtureTransport.new(wrong_headers))
	assert(not (await gateway.get_command(_request_context(), command.command_id)).ok)
	var wrong_status := _success(command, 202)
	gateway.set_transport(FixtureTransport.new(wrong_status))
	var wrong_status_result: Dictionary = await gateway.get_command(_request_context(), command.command_id)
	assert(not wrong_status_result.ok)
	assert(wrong_status_result.size() == 4)
	assert(wrong_status_result.status == 0)
	assert(wrong_status_result.headers.is_empty())
	assert(wrong_status_result.error.command_id == command.command_id)
	var server_failure := _server_failure()
	gateway.set_transport(FixtureTransport.new(server_failure))
	var server_failure_result: Dictionary = await gateway.get_command(_request_context(), command.command_id)
	assert(not server_failure_result.ok)
	assert(server_failure_result.size() == 4)
	assert(server_failure_result.status == 500)
	assert(server_failure_result.headers["retry-after"] == "9")
	assert(server_failure_result.headers["x-request-id"] == _request_context().request_id)
	assert(server_failure_result.headers["x-trace-id"] == _request_context().trace_id)
	assert(server_failure_result.error.error.code == "INTERNAL_ERROR")
	server_failure.status = 400
	gateway.set_transport(FixtureTransport.new(server_failure))
	assert(not (await gateway.get_command(_request_context(), command.command_id)).ok)
	var missing_rate_limit_delay := _catalog_failure(
		"RATE_LIMITED", "RATE_LIMIT", true, "request.rate_limited", 429,
	)
	gateway.set_transport(FixtureTransport.new(missing_rate_limit_delay))
	var missing_rate_limit_result: Dictionary = await gateway.get_command(_request_context(), command.command_id)
	assert(not missing_rate_limit_result.ok)
	assert(missing_rate_limit_result.size() == 4)
	assert(missing_rate_limit_result.status == 0)
	assert(missing_rate_limit_result.headers.is_empty())
	assert(missing_rate_limit_result.error.command_id == command.command_id)
	assert(missing_rate_limit_result.error.code == "LOCAL_CONTRACT_RESPONSE_INVALID")
	var valid_rate_limit := _catalog_failure(
		"RATE_LIMITED", "RATE_LIMIT", true, "request.rate_limited", 429, "3",
	)
	gateway.set_transport(FixtureTransport.new(valid_rate_limit))
	var valid_rate_limit_result: Dictionary = await gateway.get_command(_request_context(), command.command_id)
	assert(not valid_rate_limit_result.ok)
	assert(valid_rate_limit_result.error.error.code == "RATE_LIMITED")
	assert(valid_rate_limit_result.headers["retry-after"] == "3")
	var missing_dependency_delay := _catalog_failure(
		"DEPENDENCY_UNAVAILABLE", "DEPENDENCY", true,
		"dependency.temporarily_unavailable", 503,
	)
	gateway.set_transport(FixtureTransport.new(missing_dependency_delay))
	assert(
		(await gateway.get_command(_request_context(), command.command_id)).error.code
		== "LOCAL_CONTRACT_RESPONSE_INVALID"
	)
	var valid_dependency_failure := _catalog_failure(
		"DEPENDENCY_UNAVAILABLE", "DEPENDENCY", true,
		"dependency.temporarily_unavailable", 503, "7",
	)
	gateway.set_transport(FixtureTransport.new(valid_dependency_failure))
	var valid_dependency_result: Dictionary = await gateway.get_command(_request_context(), command.command_id)
	assert(valid_dependency_result.error.error.code == "DEPENDENCY_UNAVAILABLE")
	assert(valid_dependency_result.headers["retry-after"] == "7")
	var non_retryable_unknown := _unknown_commit_failure(command.command_id)
	gateway.set_transport(FixtureTransport.new(non_retryable_unknown))
	assert(
		(await gateway.get_command(_request_context(), command.command_id)).error.error.code
		== "UNKNOWN_COMMIT_STATE"
	)
	var missing_unknown_location := non_retryable_unknown.duplicate(true)
	missing_unknown_location.headers.erase("Location")
	gateway.set_transport(FixtureTransport.new(missing_unknown_location))
	assert(
		(await gateway.get_command(_request_context(), command.command_id)).error.code
		== "LOCAL_CONTRACT_RESPONSE_INVALID"
	)
	var wrong_unknown_location := non_retryable_unknown.duplicate(true)
	wrong_unknown_location.headers.Location = "/v1/commands/cmd_contract_other_0001"
	gateway.set_transport(FixtureTransport.new(wrong_unknown_location))
	assert(
		(await gateway.get_command(_request_context(), command.command_id)).error.code
		== "LOCAL_CONTRACT_RESPONSE_INVALID"
	)
	var wrong_unknown_command := _unknown_commit_failure("cmd_contract_other_0001")
	gateway.set_transport(FixtureTransport.new(wrong_unknown_command))
	assert(
		(await gateway.get_command(_request_context(), command.command_id)).error.code
		== "LOCAL_CONTRACT_RESPONSE_INVALID"
	)
	var polling_context := _request_context()
	polling_context.request_id = "req_polling_00000001"
	polling_context.trace_id = "trace_polling_00000001"
	polling_context.correlation_id = "corr_polling_00000001"
	polling_context.requested_at = "2026-08-06T10:00:02Z"
	gateway.set_transport(FixtureTransport.new(_success(command, 200, {
		"X-Request-Id": polling_context.request_id,
		"X-Trace-Id": polling_context.trace_id,
		"X-Correlation-Id": polling_context.correlation_id,
	})))
	var polling_result: Dictionary = await gateway.get_command(polling_context, command.command_id)
	assert(polling_result.ok)
	assert(polling_result.headers["x-request-id"] == polling_context.request_id)
	assert(polling_result.headers["x-trace-id"] == polling_context.trace_id)
	assert(polling_result.headers["x-correlation-id"] == polling_context.correlation_id)
	assert(polling_result.value.request_context.request_id == command.request_context.request_id)
	assert(polling_result.value.request_context.trace_id == command.request_context.trace_id)
	assert(polling_result.value.request_context.correlation_id == command.request_context.correlation_id)
	var wrong_origin_actor := command.duplicate(true)
	wrong_origin_actor.request_context.actor.actor_id = "student_other_0001"
	gateway.set_transport(FixtureTransport.new(_success(wrong_origin_actor)))
	assert(not (await gateway.get_command(_request_context(), command.command_id)).ok)
	var wrong_identity := command.duplicate(true)
	wrong_identity.command_id = "cmd_contract_9999"
	wrong_identity.links.self = "/v1/commands/cmd_contract_9999"
	gateway.set_transport(FixtureTransport.new(_success(wrong_identity)))
	assert(not (await gateway.get_command(_request_context(), command.command_id)).ok)

	var invalid_response := command.duplicate(true)
	invalid_response.result.first_event_sequence = "732"
	gateway.set_transport(FixtureTransport.new(_success(invalid_response)))
	var invalid_command_result: Dictionary = await gateway.get_command(_request_context(), command.command_id)
	assert(not invalid_command_result.ok)
	assert(invalid_command_result.status == 0)
	assert(invalid_command_result.headers.is_empty())
	assert(invalid_command_result.error.command_id == command.command_id)

	var ignored_transport_result := _success(command)
	ignored_transport_result["ignored"] = true
	gateway.set_transport(FixtureTransport.new(ignored_transport_result))
	var ignored_result: Dictionary = await gateway.get_command(_request_context(), command.command_id)
	assert(not ignored_result.ok)
	assert(ignored_result.status == 0)
	assert(ignored_result.headers.is_empty())
	assert(ignored_result.error.command_id == command.command_id)

	var valid_local_transport_failure := _local_transport_failure("get_command")
	gateway.set_transport(FixtureTransport.new(valid_local_transport_failure))
	var local_transport_result: Dictionary = await gateway.get_command(
		_request_context(), command.command_id,
	)
	assert(not local_transport_result.ok)
	assert(local_transport_result.status == 0)
	assert(local_transport_result.headers.is_empty())
	assert(local_transport_result.error.size() == 6)
	assert(local_transport_result.error.operation == "get_command")
	assert(local_transport_result.error.code == "LOCAL_TRANSPORT_TIMEOUT")
	var private_local_transport_failure := _local_transport_failure("get_command")
	private_local_transport_failure.error["debug_secret"] = "must-not-cross-gateway"
	gateway.set_transport(FixtureTransport.new(private_local_transport_failure))
	var private_local_result: Dictionary = await gateway.get_command(
		_request_context(), command.command_id,
	)
	assert(not private_local_result.ok)
	assert(private_local_result.status == 0)
	assert(private_local_result.headers.is_empty())
	assert(private_local_result.error.code == "LOCAL_TRANSPORT_RESULT_INVALID")
	assert(private_local_result.error.command_id == command.command_id)
	assert(not private_local_result.error.has("debug_secret"))
	var missing_local_code := _local_transport_failure("get_command")
	missing_local_code.error.erase("code")
	gateway.set_transport(FixtureTransport.new(missing_local_code))
	assert(
		(await gateway.get_command(_request_context(), command.command_id)).error.code
		== "LOCAL_TRANSPORT_RESULT_INVALID"
	)
	var wrong_local_operation := _local_transport_failure("submit_agent_turn")
	gateway.set_transport(FixtureTransport.new(wrong_local_operation))
	assert(
		(await gateway.get_command(_request_context(), command.command_id)).error.code
		== "LOCAL_TRANSPORT_RESULT_INVALID"
	)

	var unconfigured := AgentApiGateway.new()
	var missing_transport: Dictionary = await unconfigured.get_command(_request_context(), command.command_id)
	assert(not missing_transport.ok)
	assert(missing_transport.size() == 4)
	assert(missing_transport.status == 0)
	assert(missing_transport.headers.is_empty())
	assert(missing_transport.error.code == "LOCAL_GATEWAY_NOT_CONFIGURED")

	var write_transport := FixtureTransport.new(_accepted_success(_accepted_job()))
	var write_gateway := AgentApiGateway.new(write_transport)
	var forged_write_transport := FixtureTransport.new(_accepted_success(_accepted_job()))
	var forged_write_gateway := AgentApiGateway.new(forged_write_transport)
	var forged_write_request := skill_build_request.duplicate(true)
	forged_write_request.source_bundle.files[0].content_sha256 = "f".repeat(64)
	var forged_write_result: Dictionary = await forged_write_gateway.submit_skill_build(
		_request_context(), "idem_contract_forged_001", forged_write_request,
	)
	assert(not forged_write_result.ok)
	assert(forged_write_result.status == 0)
	assert(forged_write_result.headers.is_empty())
	assert(forged_write_transport.last_operation.is_empty())
	var write_result: Dictionary = await write_gateway.submit_skill_build(
		_request_context(), "idem_contract_00000001", skill_build_request,
	)
	assert(write_result.ok)
	assert(write_result.status == 202)
	assert(write_result.headers["retry-after"] == "1")
	assert(write_result.headers["idempotency-replayed"] == "false")
	assert(write_transport.last_operation == "submit_skill_build")
	assert(write_transport.last_arguments.idempotency_key == "idem_contract_00000001")
	var wrong_trace_job := _accepted_job()
	wrong_trace_job.trace_id = "trace_other_00000001"
	write_gateway.set_transport(FixtureTransport.new(_accepted_success(wrong_trace_job)))
	var wrong_trace_result: Dictionary = await write_gateway.submit_skill_build(
		_request_context(), "idem_contract_00000002", skill_build_request,
	)
	assert(not wrong_trace_result.ok)
	assert(wrong_trace_result.status == 0)
	assert(wrong_trace_result.headers.is_empty())
	assert(wrong_trace_result.error.command_id == wrong_trace_job.command_id)
	var retry_context := _request_context()
	retry_context.request_id = "req_contract_retry_0001"
	retry_context.trace_id = "trace_contract_retry_0001"
	var original_receipt := _accepted_job()
	write_gateway.set_transport(FixtureTransport.new(_accepted_success_for_attempt(
		original_receipt, retry_context, "true",
	)))
	var replayed_write_result: Dictionary = await write_gateway.submit_skill_build(
		retry_context, "idem_contract_00000001", skill_build_request,
	)
	assert(replayed_write_result.ok)
	assert(replayed_write_result.headers["x-request-id"] == retry_context.request_id)
	assert(replayed_write_result.headers["x-trace-id"] == retry_context.trace_id)
	assert(replayed_write_result.headers["x-correlation-id"] == retry_context.correlation_id)
	assert(replayed_write_result.headers["idempotency-replayed"] == "true")
	assert(replayed_write_result.value.trace_id == _request_context().trace_id)
	write_gateway.set_transport(FixtureTransport.new(_accepted_success_for_attempt(
		original_receipt, retry_context, "false",
	)))
	assert(not (await write_gateway.submit_skill_build(
		retry_context, "idem_contract_false_claim_01", skill_build_request,
	)).ok)
	var missing_replay_header := _accepted_success(original_receipt)
	missing_replay_header.headers.erase("Idempotency-Replayed")
	write_gateway.set_transport(FixtureTransport.new(missing_replay_header))
	assert(not (await write_gateway.submit_skill_build(
		_request_context(), "idem_contract_missing_replay", skill_build_request,
	)).ok)
	assert(not (await write_gateway.submit_skill_build(_request_context(), "short", skill_build_request)).ok)
	assert(not (await write_gateway.get_world_events(_request_context(), "world_demo_001", 731, 501)).ok)

	var evidence_headers := {
		"ETag": "\"%s\"" % evidence.evidence_ref.sha256,
	}
	var evidence_for_gateway := evidence.duplicate(true)
	evidence_for_gateway.request_context = _request_context()
	var evidence_gateway := AgentApiGateway.new(FixtureTransport.new(_success(evidence_for_gateway, 200, evidence_headers)))
	var evidence_result: Dictionary = await evidence_gateway.get_evidence(
		_request_context(), evidence_for_gateway.evidence_ref.evidence_id,
	)
	assert(evidence_result.ok)
	assert(evidence_result.status == 200)
	assert(evidence_result.headers.etag == evidence_headers.ETag)
	var malformed_evidence := evidence_for_gateway.duplicate(true)
	malformed_evidence.evidence_ref = 1
	evidence_gateway.set_transport(FixtureTransport.new(_success(malformed_evidence, 200, evidence_headers)))
	var malformed_evidence_result: Dictionary = await evidence_gateway.get_evidence(
		_request_context(), evidence_for_gateway.evidence_ref.evidence_id,
	)
	assert(not malformed_evidence_result.ok)
	assert(malformed_evidence_result.size() == 4)
	assert(malformed_evidence_result.status == 0)
	assert(malformed_evidence_result.headers.is_empty())

	print("AGENT_GODOT_CONTRACT_TEST_OK")
	quit(0)


func _example(name: String) -> Dictionary:
	var path := ProjectSettings.globalize_path("res://../../contracts/examples/%s" % name)
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	assert(parsed is Dictionary and parsed.has("value"))
	return parsed.value


func _request_context() -> Dictionary:
	return {
		"schema_version": "1.0.0",
		"request_id": "req_contract_0001",
		"correlation_id": "corr_contract_0001",
		"trace_id": "trace_contract_0001",
		"requested_at": "2026-08-06T10:00:00Z",
		"actor": {
			"tenant_id": "tenant_yaya",
			"actor_id": "student_0001",
			"actor_type": "student",
			"roles": ["game:player"],
		},
		"content_ref": _content_ref(),
	}


func _content_ref() -> Dictionary:
	return {
		"unit_id": "YAYA_FARM_001",
		"version": "1.4.0",
		"content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
	}


func _versions() -> Dictionary:
	return {
		"api_version": "1.0.0",
		"event_version": "1",
		"policy_version": "policy-38",
		"world_rules_version": "farm-rules-12",
		"teaching_spec_version": "teaching-7",
	}


func _valid_command() -> Dictionary:
	return {
		"request_context": _request_context(),
		"command_id": "cmd_contract_0001",
		"revision": 5,
		"command_type": "EXECUTE_AGENT_TURN",
		"status": "APPLIED",
		"stage": "COMPLETE",
		"terminal": true,
		"accepted_at": "2026-08-06T10:00:00Z",
		"updated_at": "2026-08-06T10:00:01Z",
		"result": {
			"result_type": "WORLD_COMMIT",
			"world_id": "world_demo_001",
			"previous_revision": 184,
			"world_revision": 185,
			"first_event_sequence": 732,
			"last_event_sequence": 733,
		},
		"error": null,
		"evidence_refs": [],
		"versions": _versions(),
		"links": {"self": "/v1/commands/cmd_contract_0001"},
	}


func _valid_event(sequence: int, event_id: String) -> Dictionary:
	return {
		"event_id": event_id,
		"event_type": "world.plot_watered",
		"event_version": 1,
		"schema_version": "1.0.0",
		"stream_id": "world:world_demo_001",
		"sequence": sequence,
		"occurred_at": "2026-08-06T10:00:01Z",
		"producer": "world_engine",
		"trace_id": "trace_contract_0001",
		"command_id": "cmd_contract_0001",
		"correlation_id": "corr_contract_0001",
		"causation_id": "cmd_contract_0001",
		"content_ref": _content_ref(),
		"payload": {"world_revision": 185},
	}


func _valid_runtime_event() -> Dictionary:
	var event := _valid_event(1, "evt_runtime_00000001")
	event.event_type = "command.accepted"
	event.stream_id = "command:cmd_contract_0001"
	event.payload = {
		"command_type": "EXECUTE_AGENT_TURN",
		"status": "ACCEPTED",
		"accepted_at": "2026-08-06T10:00:01Z",
	}
	return event


func _runtime_event_with_payload(event_type: String, payload: Dictionary) -> Dictionary:
	var event := _valid_event(1, "evt_runtime_00000001")
	event.event_type = event_type
	event.stream_id = "runtime:contract_0001"
	event.payload = payload
	return event


func _assert_command_status_graph() -> void:
	var statuses := [
		"ACCEPTED", "VALIDATING", "RUNNING_SANDBOX", "APPLYING_WORLD",
		"APPLIED", "REJECTED", "FAILED", "UNKNOWN", "CANCELLED",
	]
	var legal_successors := {
		"ACCEPTED": ["VALIDATING", "REJECTED", "FAILED", "CANCELLED"],
		"VALIDATING": ["RUNNING_SANDBOX", "APPLYING_WORLD", "APPLIED", "REJECTED", "FAILED", "CANCELLED"],
		"RUNNING_SANDBOX": ["APPLYING_WORLD", "APPLIED", "REJECTED", "FAILED", "CANCELLED"],
		"APPLYING_WORLD": ["APPLIED", "REJECTED", "FAILED", "UNKNOWN", "CANCELLED"],
	}
	for from_status in statuses:
		for to_status in statuses:
			var event := _runtime_event_with_payload("command.stage_changed", {
				"from_status": from_status,
				"to_status": to_status,
				"command_revision": 2,
				"attempt": 1,
			})
			var expected_ok: bool = legal_successors.has(from_status) and to_status in legal_successors[from_status]
			var actual: Dictionary = ContractValidator.validate_runtime_event(event, 0)
			assert(actual.ok == expected_ok, "unexpected command transition validation: %s -> %s" % [from_status, to_status])


func _accepted_job() -> Dictionary:
	return {
		"job_id": "job_contract_0001",
		"job_type": "CREATE_SKILL_BUILD",
		"status": "ACCEPTED",
		"created_at": "2026-08-06T10:00:00Z",
		"updated_at": "2026-08-06T10:00:00Z",
		"command_id": "cmd_contract_0001",
		"trace_id": "trace_contract_0001",
		"error": null,
	}


func _success(value: Dictionary, status: int = 200, extra_headers: Dictionary = {}) -> Dictionary:
	var headers := {
		"X-Request-Id": "req_contract_0001",
		"X-Trace-Id": "trace_contract_0001",
		"X-Correlation-Id": "corr_contract_0001",
		"X-Schema-Version": "1.0.0",
	}
	headers.merge(extra_headers, true)
	return {"ok": true, "status": status, "headers": headers, "value": value}


func _accepted_success(value: Dictionary) -> Dictionary:
	return _success(value, 202, {
		"Location": "/v1/commands/%s" % value.command_id,
		"Retry-After": "1",
		"Idempotency-Replayed": "false",
	})


func _accepted_success_for_attempt(
	value: Dictionary,
	request_context: Dictionary,
	replayed: String,
) -> Dictionary:
	return _success(value, 202, {
		"X-Request-Id": request_context.request_id,
		"X-Trace-Id": request_context.trace_id,
		"X-Correlation-Id": request_context.correlation_id,
		"Location": "/v1/commands/%s" % value.command_id,
		"Retry-After": "1",
		"Idempotency-Replayed": replayed,
	})


func _server_failure() -> Dictionary:
	return {
		"ok": false,
		"status": 500,
		"headers": {
			"X-Request-Id": "req_contract_0001",
			"X-Trace-Id": "trace_contract_0001",
			"X-Correlation-Id": "corr_contract_0001",
			"X-Schema-Version": "1.0.0",
			"Retry-After": "9",
		},
		"error": {
			"request_id": "req_contract_0001",
			"trace_id": "trace_contract_0001",
			"status": "FAILED",
			"data": null,
			"error": {
				"code": "INTERNAL_ERROR",
				"category": "INTERNAL",
				"retryable": false,
				"user_message_key": "system.internal_error",
				"stage": "HTTP_ADAPTER",
			},
		},
	}


func _local_transport_failure(operation: String) -> Dictionary:
	return {
		"ok": false,
		"status": 0,
		"headers": {},
		"error": {
			"scope": "CLIENT_LOCAL",
			"code": "LOCAL_TRANSPORT_TIMEOUT",
			"category": "DEPENDENCY",
			"retryable": true,
			"operation": operation,
			"message": "The request timed out.",
		},
	}


func _catalog_failure(
	code: String,
	category: String,
	retryable: bool,
	user_message_key: String,
	status: int,
	retry_after: String = "",
) -> Dictionary:
	var headers := {
		"X-Request-Id": "req_contract_0001",
		"X-Trace-Id": "trace_contract_0001",
		"X-Correlation-Id": "corr_contract_0001",
		"X-Schema-Version": "1.0.0",
	}
	if not retry_after.is_empty():
		headers["Retry-After"] = retry_after
	return {
		"ok": false,
		"status": status,
		"headers": headers,
		"error": {
			"request_id": "req_contract_0001",
			"trace_id": "trace_contract_0001",
			"status": "FAILED",
			"data": null,
			"error": {
				"code": code,
				"category": category,
				"retryable": retryable,
				"user_message_key": user_message_key,
				"stage": "HTTP_ADAPTER",
			},
		},
	}


func _unknown_commit_failure(command_id: String) -> Dictionary:
	return {
		"ok": false,
		"status": 503,
		"headers": {
			"X-Request-Id": "req_contract_0001",
			"X-Trace-Id": "trace_contract_0001",
			"X-Correlation-Id": "corr_contract_0001",
			"X-Schema-Version": "1.0.0",
			"Location": "/v1/commands/%s" % command_id,
		},
		"error": {
			"request_id": "req_contract_0001",
			"command_id": command_id,
			"trace_id": "trace_contract_0001",
			"status": "UNKNOWN",
			"data": null,
			"error": {
				"code": "UNKNOWN_COMMIT_STATE",
				"category": "DEPENDENCY",
				"retryable": false,
				"user_message_key": "command.reconciling",
				"stage": "WORLD_COMMIT",
			},
		},
	}
