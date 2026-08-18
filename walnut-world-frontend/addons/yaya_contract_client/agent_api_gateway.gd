class_name YayaAgentApiGateway
extends RefCounted

## Single validated entry point used by scenes and UI controllers.
##
## Adapters are injected through composition and expose only execute(). HTTP,
## fixture, replay, and future Feishu adapters therefore cannot leak transport
## details into gameplay code. Every successful response is validated before it
## is returned as {"ok": true, "status": ..., "headers": ..., "value": ...}.
## WireAttemptContext identifies one HTTP attempt. Resource request_context is
## immutable origin/domain context; polling must never overwrite or equate them.

const ContractValidator = preload("res://addons/yaya_contract_client/contract_validator.gd")
const AgentApiTransport = preload("res://addons/yaya_contract_client/agent_api_transport.gd")
const LOCAL_ERROR_CATEGORIES := [
	"VALIDATION", "AUTHENTICATION", "AUTHORIZATION", "POLICY", "CONCURRENCY",
	"SKILL", "SANDBOX", "WORLD_RULE", "DEPENDENCY", "INVARIANT", "RATE_LIMIT",
	"INTERNAL",
]

signal command_event_received(event: Dictionary)
signal contract_violation(error: Dictionary)
signal connection_state_changed(state: String)

var _transport: AgentApiTransport


func _init(transport: AgentApiTransport = null) -> void:
	_transport = transport


func set_transport(transport: AgentApiTransport) -> void:
	_transport = transport


func get_bootstrap(attempt_context: Dictionary) -> Dictionary:
	var guard := _validate_wire_attempt_context(attempt_context)
	if not guard.ok:
		return _reject_request("get_bootstrap", guard.error)
	return await _dispatch("get_bootstrap", {"attempt_context": attempt_context}, "bootstrap")


func get_student_bootstrap(attempt_context: Dictionary) -> Dictionary:
	var guard := _validate_wire_attempt_context(attempt_context)
	if not guard.ok:
		return _reject_request("get_student_bootstrap", guard.error)
	return await _dispatch(
		"get_student_bootstrap",
		{"attempt_context": attempt_context},
		"student_bootstrap_v2",
	)


func submit_skill_build(request_context: Dictionary, idempotency_key: String, request: Dictionary) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("submit_skill_build", context_guard.error)
	var idempotency_guard := _validate_idempotency_key(idempotency_key)
	if not idempotency_guard.ok:
		return _reject_request("submit_skill_build", idempotency_guard.error)
	var request_guard := ContractValidator.validate_skill_build_create_request(request)
	if not request_guard.ok:
		return _reject_request("submit_skill_build", request_guard.error)
	return await _dispatch("submit_skill_build", {"request_context": request_context, "idempotency_key": idempotency_key, "request": request}, "operation_accepted", -1, {"trace_id": request_context.trace_id})


func get_skill_build(request_context: Dictionary, build_id: String) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("get_skill_build", context_guard.error)
	var guard := ContractValidator.validate_identifier(build_id, "build_id")
	if not guard.ok:
		return _reject_request("get_skill_build", guard.error)
	return await _dispatch("get_skill_build", {"request_context": request_context, "build_id": build_id}, "skill_build", -1, {"origin_actor": request_context.actor, "build_id": build_id})


func activate_skill_version(
	request_context: Dictionary,
	skill_version_id: String,
	idempotency_key: String,
	request: Dictionary
) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("activate_skill_version", context_guard.error)
	var id_guard := ContractValidator.validate_identifier(skill_version_id, "skill_version_id")
	if not id_guard.ok:
		return _reject_request("activate_skill_version", id_guard.error)
	var idempotency_guard := _validate_idempotency_key(idempotency_key)
	if not idempotency_guard.ok:
		return _reject_request("activate_skill_version", idempotency_guard.error)
	var request_guard := ContractValidator.validate_skill_activation_request(request)
	if not request_guard.ok:
		return _reject_request("activate_skill_version", request_guard.error)
	return await _dispatch("activate_skill_version", {
		"request_context": request_context,
		"skill_version_id": skill_version_id,
		"idempotency_key": idempotency_key,
		"request": request,
	}, "operation_accepted", -1, {"trace_id": request_context.trace_id})


func get_skill_activation(request_context: Dictionary, activation_id: String) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("get_skill_activation", context_guard.error)
	var id_guard := _validate_prefixed_id(
		activation_id,
		"^activation_[A-Za-z0-9_-]{8,118}$",
		"activation_id"
	)
	if not id_guard.ok:
		return _reject_request("get_skill_activation", id_guard.error)
	return await _dispatch(
		"get_skill_activation",
		{"request_context": request_context, "activation_id": activation_id},
		"skill_activation",
		-1,
		{"origin_actor": request_context.actor, "activation_id": activation_id}
	)


func create_agent_session(request_context: Dictionary, idempotency_key: String, request: Dictionary) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("create_agent_session", context_guard.error)
	var idempotency_guard := _validate_idempotency_key(idempotency_key)
	if not idempotency_guard.ok:
		return _reject_request("create_agent_session", idempotency_guard.error)
	var request_guard := ContractValidator.validate_agent_session_create_request(request)
	if not request_guard.ok:
		return _reject_request("create_agent_session", request_guard.error)
	return await _dispatch("create_agent_session", {"request_context": request_context, "idempotency_key": idempotency_key, "request": request}, "operation_accepted", -1, {"trace_id": request_context.trace_id})


func get_agent_session(request_context: Dictionary, session_id: String) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("get_agent_session", context_guard.error)
	var guard := ContractValidator.validate_identifier(session_id, "session_id")
	if not guard.ok:
		return _reject_request("get_agent_session", guard.error)
	return await _dispatch("get_agent_session", {"request_context": request_context, "session_id": session_id}, "agent_session", -1, {"origin_actor": request_context.actor, "session_id": session_id})


func submit_agent_turn(
	request_context: Dictionary,
	session_id: String,
	idempotency_key: String,
	request: Dictionary
) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("submit_agent_turn", context_guard.error)
	var id_guard := ContractValidator.validate_identifier(session_id, "session_id")
	if not id_guard.ok:
		return _reject_request("submit_agent_turn", id_guard.error)
	var idempotency_guard := _validate_idempotency_key(idempotency_key)
	if not idempotency_guard.ok:
		return _reject_request("submit_agent_turn", idempotency_guard.error)
	var request_guard := ContractValidator.validate_agent_turn_create_request(request)
	if not request_guard.ok:
		return _reject_request("submit_agent_turn", request_guard.error)
	return await _dispatch("submit_agent_turn", {
		"request_context": request_context, "session_id": session_id,
		"idempotency_key": idempotency_key, "request": request,
	}, "operation_accepted", -1, {"trace_id": request_context.trace_id})


func get_command(request_context: Dictionary, command_id: String) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("get_command", context_guard.error)
	var guard := _validate_prefixed_id(command_id, "^cmd_[A-Za-z0-9_-]{8,96}$", "command_id")
	if not guard.ok:
		return _reject_request("get_command", guard.error)
	return await _dispatch("get_command", {"request_context": request_context, "command_id": command_id}, "command", -1, {"origin_actor": request_context.actor, "command_id": command_id})


func get_run(request_context: Dictionary, run_id: String) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("get_run", context_guard.error)
	var guard := ContractValidator.validate_identifier(run_id, "run_id")
	if not guard.ok:
		return _reject_request("get_run", guard.error)
	return await _dispatch("get_run", {"request_context": request_context, "run_id": run_id}, "run", -1, {"origin_actor": request_context.actor, "run_id": run_id})


func get_world_snapshot(request_context: Dictionary, world_id: String) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("get_world_snapshot", context_guard.error)
	var guard := ContractValidator.validate_identifier(world_id, "world_id")
	if not guard.ok:
		return _reject_request("get_world_snapshot", guard.error)
	return await _dispatch("get_world_snapshot", {"request_context": request_context, "world_id": world_id}, "world_snapshot", -1, {"origin_actor": request_context.actor, "world_id": world_id})


func get_world_events(request_context: Dictionary, world_id: String, after_sequence: int, limit: int = 100) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("get_world_events", context_guard.error)
	var guard := ContractValidator.validate_identifier(world_id, "world_id")
	if not guard.ok:
		return _reject_request("get_world_events", guard.error)
	if after_sequence < 0:
		return _reject_request("get_world_events", _local_error(
			"LOCAL_CONTRACT_REQUEST_INVALID", "after_sequence must be non-negative."
		))
	if limit < 1 or limit > 500:
		return _reject_request("get_world_events", _local_error(
			"LOCAL_CONTRACT_REQUEST_INVALID", "limit must be between 1 and 500."
		))
	return await _dispatch("get_world_events", {
		"request_context": request_context, "world_id": world_id,
		"after_sequence": after_sequence, "limit": limit,
	}, "world_event_page", after_sequence, {"origin_actor": request_context.actor, "world_id": world_id})


func upload_client_events(request_context: Dictionary, idempotency_key: String, batch: Dictionary) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("upload_client_events", context_guard.error)
	var idempotency_guard := _validate_idempotency_key(idempotency_key)
	if not idempotency_guard.ok:
		return _reject_request("upload_client_events", idempotency_guard.error)
	var batch_guard := ContractValidator.validate_client_event_batch_request(batch)
	if not batch_guard.ok:
		return _reject_request("upload_client_events", batch_guard.error)
	return await _dispatch("upload_client_events", {
		"request_context": request_context, "idempotency_key": idempotency_key, "batch": batch,
	}, "operation_accepted", -1, {"trace_id": request_context.trace_id})


func get_evidence(request_context: Dictionary, evidence_id: String) -> Dictionary:
	var context_guard := ContractValidator.validate_request_context(request_context)
	if not context_guard.ok:
		return _reject_request("get_evidence", context_guard.error)
	var guard := _validate_prefixed_id(evidence_id, "^evidence_[A-Za-z0-9_-]{8,128}$", "evidence_id")
	if not guard.ok:
		return _reject_request("get_evidence", guard.error)
	return await _dispatch("get_evidence", {"request_context": request_context, "evidence_id": evidence_id}, "evidence", -1, {"origin_actor": request_context.actor, "evidence_id": evidence_id})


func cancel_attempt(request_id: String) -> bool:
	if not _header_matches(request_id, "^req_[A-Za-z0-9_-]{8,96}$"):
		return false
	if _transport == null:
		return false
	var cancelled: Variant = _transport.cancel(request_id)
	return typeof(cancelled) == TYPE_BOOL and cancelled


func shutdown_transport() -> void:
	if _transport != null:
		_transport.shutdown()


func ingest_command_event(event: Dictionary, expected_after_sequence: int = -1) -> Dictionary:
	var validation := ContractValidator.validate_runtime_event(event, expected_after_sequence)
	if not validation.ok:
		contract_violation.emit(validation.error)
		return _failure_result(0, {}, validation.error)
	command_event_received.emit(event)
	return _success_result(0, {}, event)


func _dispatch(
	operation: String,
	arguments: Dictionary,
	response_contract: String,
	expected_after_sequence: int = -1,
	expected_identity: Dictionary = {},
) -> Dictionary:
	var reconciliation_context := _trusted_reconciliation_context(expected_identity)
	if _transport == null:
		return _fail_local(
			operation,
			"LOCAL_GATEWAY_NOT_CONFIGURED",
			"No Agent API transport is configured.",
			reconciliation_context,
		)
	var attempt_context: Variant = arguments.get("attempt_context", arguments.get("request_context"))
	var result: Variant = await _transport.execute(operation, arguments)
	if not result is Dictionary or not result.has("ok") or typeof(result.ok) != TYPE_BOOL:
		return _fail_local(
			operation,
			"LOCAL_TRANSPORT_RESULT_INVALID",
			"Transport result must contain a boolean ok field.",
			reconciliation_context,
		)
	if result.ok:
		if result.size() != 4 or not result.has("status") or not result.has("headers") or not result.has("value"):
			return _fail_local(
				operation,
				"LOCAL_TRANSPORT_RESULT_INVALID",
				"Successful transport result must contain only ok, status, headers and value.",
				reconciliation_context,
			)
		var metadata_validation := _validate_success_metadata(
			response_contract, result.status, result.headers, result.value, attempt_context,
		)
		if response_contract == "operation_accepted":
			var accepted_context := _trusted_accepted_reconciliation_context(
				result.value,
				metadata_validation.get("headers", {}),
			)
			if accepted_context.has("command_id"):
				reconciliation_context["command_id"] = accepted_context.command_id
		if not metadata_validation.ok:
			return _fail_local(
				operation,
				"LOCAL_CONTRACT_RESPONSE_INVALID",
				metadata_validation.error.message,
				reconciliation_context,
			)
		var response_identity := expected_identity
		if (
			response_contract == "operation_accepted"
			and metadata_validation.headers.get("idempotency-replayed") == "true"
		):
			response_identity = expected_identity.duplicate(true)
			response_identity.erase("trace_id")
		var validation := _validate_response(
			response_contract, result.value, expected_after_sequence, response_identity,
		)
		if not validation.ok:
			return _fail_local(
				operation,
				"LOCAL_CONTRACT_RESPONSE_INVALID",
				validation.error.get("message", "Transport returned an invalid response."),
				reconciliation_context,
			)
		return _success_result(result.status, metadata_validation.headers, result.value)
	if result.size() != 4 or not result.has("status") or not result.has("headers") or not result.has("error") or not result.error is Dictionary:
		return _fail_local(
			operation,
			"LOCAL_TRANSPORT_RESULT_INVALID",
			"Failed transport result must contain only ok, status, headers and a structured error.",
			reconciliation_context,
		)
	if result.error.get("scope") == "CLIENT_LOCAL":
		if result.status != 0 or not result.headers is Dictionary or not result.headers.is_empty():
			return _fail_local(
				operation,
				"LOCAL_TRANSPORT_RESULT_INVALID",
				"Local transport failures must use status 0 and empty headers.",
				reconciliation_context,
			)
		var local_error_validation := _validate_local_transport_error(result.error, operation)
		if not local_error_validation.ok:
			return _fail_local(
				operation,
				"LOCAL_TRANSPORT_RESULT_INVALID",
				local_error_validation.error.message,
				reconciliation_context,
			)
		return _failure_result(0, {}, result.error.duplicate(true))
	if typeof(result.status) != TYPE_INT or result.status < 400 or result.status > 599:
		return _fail_local(
			operation,
			"LOCAL_TRANSPORT_RESULT_INVALID",
			"Server transport failures must carry an HTTP error status.",
			reconciliation_context,
		)
	var error_headers_check := _validate_identity_headers(result.headers, attempt_context)
	if not error_headers_check.ok:
		return _fail_local(
			operation,
			"LOCAL_CONTRACT_RESPONSE_INVALID",
			error_headers_check.error.message,
			reconciliation_context,
		)
	var error_validation := ContractValidator.validate_error_response(result.error)
	if not error_validation.ok:
		return _fail_local(
			operation,
			"LOCAL_CONTRACT_RESPONSE_INVALID",
			"Transport returned an invalid server error response.",
			reconciliation_context,
		)
	if result.status != ContractValidator.http_status_for_error_code(result.error.error.code):
		return _fail_local(
			operation,
			"LOCAL_CONTRACT_RESPONSE_INVALID",
			"HTTP status disagrees with the error catalog.",
			reconciliation_context,
		)
	if result.error.error.code == "UNKNOWN_COMMIT_STATE":
		var expected_location := "/v1/commands/%s" % result.error.command_id
		if error_headers_check.headers.get("location") != expected_location:
			return _fail_local(
				operation,
				"LOCAL_CONTRACT_RESPONSE_INVALID",
				"UNKNOWN_COMMIT_STATE Location does not match command_id.",
				reconciliation_context,
			)
		if not reconciliation_context.has("command_id"):
			reconciliation_context["command_id"] = result.error.command_id
		if (
			expected_identity.has("command_id")
			and result.error.command_id != expected_identity.command_id
		):
			return _fail_local(
				operation,
				"LOCAL_CONTRACT_RESPONSE_INVALID",
				"UNKNOWN_COMMIT_STATE command_id does not match the requested command.",
				reconciliation_context,
			)
	var retry_after_required: bool = result.status == 429 or (
		result.status == 503 and result.error.error.retryable == true
	)
	if retry_after_required and not _header_matches(
		error_headers_check.headers.get("retry-after"), "^[1-9][0-9]*$"
	):
		return _fail_local(
			operation,
			"LOCAL_CONTRACT_RESPONSE_INVALID",
			"Retryable server error is missing a valid Retry-After delay.",
			reconciliation_context,
		)
	if attempt_context is Dictionary and (
		result.error.request_id != attempt_context.request_id
		or result.error.trace_id != attempt_context.trace_id
	):
		return _fail_local(
			operation,
			"LOCAL_CONTRACT_RESPONSE_INVALID",
			"Server error identity does not match request context.",
			reconciliation_context,
		)
	return _failure_result(result.status, error_headers_check.headers, result.error)


func _validate_success_metadata(
	response_contract: String,
	status: Variant,
	headers: Variant,
	value: Variant,
	attempt_context: Variant,
) -> Dictionary:
	var identity_check := _validate_identity_headers(headers, attempt_context)
	if not identity_check.ok:
		return identity_check
	var normalized: Dictionary = identity_check.headers
	var expected_status := 202 if response_contract == "operation_accepted" else 200
	if typeof(status) != TYPE_INT or status != expected_status:
		return {
			"ok": false,
			"error": _local_error("LOCAL_CONTRACT_RESPONSE_INVALID", "HTTP status does not match the response contract."),
			"headers": normalized,
		}
	match response_contract:
		"operation_accepted":
			if not value is Dictionary or not value.has("command_id"):
				return _metadata_failure("Accepted response has no command identity.", normalized)
			if normalized.get("location") != "/v1/commands/%s" % value.command_id:
				return _metadata_failure("Accepted response Location does not match command_id.", normalized)
			if not _header_matches(normalized.get("retry-after"), "^[1-9][0-9]*$"):
				return _metadata_failure("Accepted response Retry-After is invalid.", normalized)
			var replayed: Variant = normalized.get("idempotency-replayed")
			if replayed not in ["false", "true"]:
				return _metadata_failure("Accepted response Idempotency-Replayed is invalid.", normalized)
			if replayed == "false" and (
				not attempt_context is Dictionary
				or not value.has("trace_id")
				or typeof(value.trace_id) != TYPE_STRING
				or value.trace_id != attempt_context.trace_id
			):
				return _metadata_failure("First accepted response trace_id does not match this attempt.", normalized)
		"world_snapshot":
			if not _header_matches(normalized.get("etag"), "^\"[A-Za-z0-9:_-]{8,200}\"$"):
				return _metadata_failure("World snapshot ETag is invalid.", normalized)
			if (
				not value is Dictionary
				or not value.has("revision")
				or typeof(value.revision) != TYPE_INT
			):
				return _metadata_failure("World snapshot revision is missing or invalid.", normalized)
			if normalized.get("x-world-revision") != String.num_int64(value.revision):
				return _metadata_failure("World revision header disagrees with snapshot.", normalized)
		"world_event_page":
			if (
				not value is Dictionary
				or not value.has("snapshot_revision")
				or typeof(value.snapshot_revision) != TYPE_INT
			):
				return _metadata_failure("World event snapshot revision is missing or invalid.", normalized)
			if normalized.get("x-world-revision") != String.num_int64(value.snapshot_revision):
				return _metadata_failure("World revision header disagrees with event page.", normalized)
		"evidence":
			if not _header_matches(normalized.get("etag"), "^\"[a-f0-9]{64}\"$"):
				return _metadata_failure("Evidence ETag is invalid.", normalized)
			if value is Dictionary:
				var evidence_ref: Variant = value.get("evidence_ref")
				if (
					evidence_ref is Dictionary
					and evidence_ref.has("sha256")
					and typeof(evidence_ref.sha256) == TYPE_STRING
					and normalized.etag != "\"%s\"" % evidence_ref.sha256
				):
					return _metadata_failure("Evidence ETag disagrees with evidence hash.", normalized)
	return {"ok": true, "error": null, "headers": normalized}


func _metadata_failure(message: String, headers: Dictionary) -> Dictionary:
	return {
		"ok": false,
		"error": _local_error("LOCAL_CONTRACT_RESPONSE_INVALID", message),
		"headers": headers,
	}


func _validate_identity_headers(headers: Variant, attempt_context: Variant) -> Dictionary:
	if not headers is Dictionary:
		return {"ok": false, "error": _local_error("LOCAL_CONTRACT_RESPONSE_INVALID", "Transport headers must be a Dictionary.")}
	var normalized := {}
	for name in headers:
		if typeof(name) != TYPE_STRING or typeof(headers[name]) != TYPE_STRING:
			return {"ok": false, "error": _local_error("LOCAL_CONTRACT_RESPONSE_INVALID", "Transport headers must contain string names and values.")}
		normalized[name.to_lower()] = headers[name]
	if attempt_context is Dictionary:
		if (
			normalized.get("x-request-id") != attempt_context.request_id
			or normalized.get("x-trace-id") != attempt_context.trace_id
			or normalized.get("x-correlation-id") != attempt_context.correlation_id
		):
			return {
				"ok": false,
				"error": _local_error("LOCAL_CONTRACT_RESPONSE_INVALID", "Response headers do not match the current HTTP attempt."),
				"headers": normalized,
			}
	if normalized.has("x-schema-version") and normalized["x-schema-version"] != "1.0.0":
		return {
			"ok": false,
			"error": _local_error("LOCAL_CONTRACT_RESPONSE_INVALID", "Response schema version is unsupported."),
			"headers": normalized,
		}
	return {"ok": true, "error": null, "headers": normalized}


func _header_matches(value: Variant, pattern: String) -> bool:
	if typeof(value) != TYPE_STRING:
		return false
	var regex := RegEx.new()
	return regex.compile(pattern) == OK and regex.search(value) != null


func _validate_response(contract_name: String, value: Variant, expected_after_sequence: int, expected_identity: Dictionary) -> Dictionary:
	match contract_name:
		"operation_accepted":
			var validation := ContractValidator.validate_operation_accepted(value)
			return _bind_response_identity(validation, value, expected_identity)
		"bootstrap":
			var validation := ContractValidator.validate_bootstrap_response(value)
			return _bind_response_identity(validation, value, expected_identity)
		"student_bootstrap_v2":
			var validation := ContractValidator.validate_student_bootstrap_v2(value)
			return _bind_response_identity(validation, value, expected_identity)
		"skill_build":
			var validation := ContractValidator.validate_skill_build(value)
			return _bind_response_identity(validation, value, expected_identity)
		"skill_activation":
			var validation := ContractValidator.validate_skill_activation(value)
			return _bind_response_identity(validation, value, expected_identity)
		"command":
			var validation := ContractValidator.validate_command_result(value)
			return _bind_response_identity(validation, value, expected_identity)
		"agent_session":
			var validation := ContractValidator.validate_agent_session(value)
			return _bind_response_identity(validation, value, expected_identity)
		"run":
			var validation := ContractValidator.validate_run(value)
			return _bind_response_identity(validation, value, expected_identity)
		"world_snapshot":
			var validation := ContractValidator.validate_world_snapshot(value)
			return _bind_response_identity(validation, value, expected_identity)
		"world_event_page":
			var validation := ContractValidator.validate_world_event_page(value, expected_after_sequence)
			return _bind_response_identity(validation, value, expected_identity)
		"evidence":
			var validation := ContractValidator.validate_evidence(value)
			return _bind_response_identity(validation, value, expected_identity)
		_:
			return {"ok": false, "error": _local_error("LOCAL_CONTRACT_RESPONSE_INVALID", "Unknown response contract.")}


func _bind_response_identity(validation: Dictionary, value: Variant, expected_identity: Dictionary) -> Dictionary:
	if not validation.ok:
		return validation
	if not value is Dictionary:
		return {"ok": false, "error": _local_error("LOCAL_CONTRACT_RESPONSE_INVALID", "Response identity cannot be inspected.")}
	for field in expected_identity:
		var actual: Variant
		if field == "evidence_id":
			actual = value.get("evidence_ref", {}).get("evidence_id")
		elif field == "origin_actor":
			if not _origin_actor_matches(value.get("request_context"), expected_identity[field]):
				return {"ok": false, "error": _local_error("LOCAL_CONTRACT_RESPONSE_INVALID", "Response origin actor does not match the authenticated actor.")}
			continue
		else:
			actual = value.get(field)
		if actual != expected_identity[field]:
			return {"ok": false, "error": _local_error("LOCAL_CONTRACT_RESPONSE_INVALID", "Response %s does not match request." % field)}
	return validation


func _origin_actor_matches(actual_context: Variant, expected_actor: Variant) -> bool:
	if not actual_context is Dictionary or not expected_actor is Dictionary:
		return false
	return actual_context.get("actor") == expected_actor


func _validate_wire_attempt_context(attempt_context: Dictionary) -> Dictionary:
	var expected_fields := ["schema_version", "request_id", "trace_id", "correlation_id"]
	if attempt_context.size() != expected_fields.size():
		return {"ok": false, "error": _local_error(
			"LOCAL_CONTRACT_REQUEST_INVALID",
			"Wire attempt context must contain only schema_version, request_id, trace_id and correlation_id."
		)}
	for field in expected_fields:
		if not attempt_context.has(field) or typeof(attempt_context[field]) != TYPE_STRING:
			return {"ok": false, "error": _local_error(
				"LOCAL_CONTRACT_REQUEST_INVALID", "Wire attempt context %s is missing or invalid." % field
			)}
	if attempt_context.schema_version != "1.0.0":
		return {"ok": false, "error": _local_error(
			"LOCAL_CONTRACT_REQUEST_INVALID", "Wire attempt schema_version is unsupported."
		)}
	if not _header_matches(attempt_context.request_id, "^req_[A-Za-z0-9_-]{8,96}$"):
		return {"ok": false, "error": _local_error(
			"LOCAL_CONTRACT_REQUEST_INVALID", "Wire attempt request_id has an invalid format."
		)}
	if not _header_matches(attempt_context.trace_id, "^trace_[A-Za-z0-9_-]{8,96}$"):
		return {"ok": false, "error": _local_error(
			"LOCAL_CONTRACT_REQUEST_INVALID", "Wire attempt trace_id has an invalid format."
		)}
	if not _header_matches(attempt_context.correlation_id, "^corr_[A-Za-z0-9_-]{8,96}$"):
		return {"ok": false, "error": _local_error(
			"LOCAL_CONTRACT_REQUEST_INVALID", "Wire attempt correlation_id has an invalid format."
		)}
	return {"ok": true, "error": null}


func _validate_prefixed_id(value: String, pattern: String, label: String) -> Dictionary:
	var regex := RegEx.new()
	if regex.compile(pattern) != OK or regex.search(value) == null:
		return {"ok": false, "error": _local_error("LOCAL_CONTRACT_REQUEST_INVALID", "%s has an invalid format." % label)}
	return {"ok": true, "error": null}


func _trusted_reconciliation_context(expected_identity: Dictionary) -> Dictionary:
	var context := {}
	var command_id: Variant = expected_identity.get("command_id")
	if _header_matches(command_id, "^cmd_[A-Za-z0-9_-]{8,96}$"):
		context["command_id"] = command_id
	return context


func _trusted_accepted_reconciliation_context(value: Variant, headers: Dictionary) -> Dictionary:
	if not value is Dictionary:
		return {}
	var command_id: Variant = value.get("command_id")
	if not _header_matches(command_id, "^cmd_[A-Za-z0-9_-]{8,96}$"):
		return {}
	if headers.get("location") != "/v1/commands/%s" % command_id:
		return {}
	return {"command_id": command_id}


func _validate_local_transport_error(value: Dictionary, expected_operation: String) -> Dictionary:
	var expected_fields := ["scope", "code", "category", "retryable", "operation", "message"]
	if value.size() != expected_fields.size():
		return {"ok": false, "error": _local_error(
			"LOCAL_TRANSPORT_RESULT_INVALID",
			"Local transport error must contain only the public error fields.",
		)}
	for field in expected_fields:
		if not value.has(field):
			return {"ok": false, "error": _local_error(
				"LOCAL_TRANSPORT_RESULT_INVALID",
				"Local transport error is missing %s." % field,
			)}
	if value.scope != "CLIENT_LOCAL":
		return {"ok": false, "error": _local_error(
			"LOCAL_TRANSPORT_RESULT_INVALID", "Local transport error scope is invalid.",
		)}
	if not _header_matches(value.code, "^LOCAL_[A-Z0-9_]{3,121}$"):
		return {"ok": false, "error": _local_error(
			"LOCAL_TRANSPORT_RESULT_INVALID", "Local transport error code is invalid.",
		)}
	if typeof(value.category) != TYPE_STRING or value.category not in LOCAL_ERROR_CATEGORIES:
		return {"ok": false, "error": _local_error(
			"LOCAL_TRANSPORT_RESULT_INVALID", "Local transport error category is invalid.",
		)}
	if typeof(value.retryable) != TYPE_BOOL:
		return {"ok": false, "error": _local_error(
			"LOCAL_TRANSPORT_RESULT_INVALID", "Local transport retryable must be a boolean.",
		)}
	if (
		typeof(value.operation) != TYPE_STRING
		or value.operation != expected_operation
		or not _header_matches(value.operation, "^[a-z][a-z0-9_]{2,63}$")
	):
		return {"ok": false, "error": _local_error(
			"LOCAL_TRANSPORT_RESULT_INVALID", "Local transport operation does not match the request.",
		)}
	if typeof(value.message) != TYPE_STRING or value.message.is_empty() or value.message.length() > 512:
		return {"ok": false, "error": _local_error(
			"LOCAL_TRANSPORT_RESULT_INVALID", "Local transport error message is invalid.",
		)}
	return {"ok": true, "error": null}


func _validate_idempotency_key(value: String) -> Dictionary:
	return _validate_prefixed_id(value, "^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$", "idempotency_key")


func _reject_request(operation: String, source_error: Dictionary) -> Dictionary:
	var error := _local_error("LOCAL_CONTRACT_REQUEST_INVALID", source_error.get("message", "Invalid request."))
	error["operation"] = operation
	contract_violation.emit(error)
	return _failure_result(0, {}, error)


func _fail_local(
	operation: String,
	code: String,
	message: String,
	context: Dictionary = {},
) -> Dictionary:
	var error := _local_error(code, message)
	error["operation"] = operation
	if context.has("command_id"):
		error["command_id"] = context.command_id
	contract_violation.emit(error)
	return _failure_result(0, {}, error)


func _success_result(status: int, headers: Dictionary, value: Variant) -> Dictionary:
	return {"ok": true, "status": status, "headers": headers, "value": value}


func _failure_result(status: int, headers: Dictionary, error: Dictionary) -> Dictionary:
	return {"ok": false, "status": status, "headers": headers, "error": error}


func _local_error(code: String, message: String) -> Dictionary:
	return {
		"scope": "CLIENT_LOCAL",
		"code": code,
		"category": "VALIDATION",
		"retryable": false,
		"message": message,
	}
