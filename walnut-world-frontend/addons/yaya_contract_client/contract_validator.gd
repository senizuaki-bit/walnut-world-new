class_name YayaAgentContractValidator
extends RefCounted

## Strict runtime guard for every value crossing the Godot/backend boundary.
##
## This file deliberately does not coerce values. A JSON string such as "12"
## is not an integer, unknown fields are rejected, and an invalid response must
## never be rendered as a successful game action.

const TERMINAL_COMMAND_STATUSES := [
	"APPLIED", "REJECTED", "FAILED", "UNKNOWN", "CANCELLED",
]
const ALL_COMMAND_STATUSES := [
	"ACCEPTED", "VALIDATING", "RUNNING_SANDBOX", "APPLYING_WORLD",
	"APPLIED", "REJECTED", "FAILED", "UNKNOWN", "CANCELLED",
]
const COMMAND_STATUS_SUCCESSORS := {
	"ACCEPTED": ["VALIDATING", "REJECTED", "FAILED", "CANCELLED"],
	"VALIDATING": ["RUNNING_SANDBOX", "APPLYING_WORLD", "APPLIED", "REJECTED", "FAILED", "CANCELLED"],
	"RUNNING_SANDBOX": ["APPLYING_WORLD", "APPLIED", "REJECTED", "FAILED", "CANCELLED"],
	"APPLYING_WORLD": ["APPLIED", "REJECTED", "FAILED", "UNKNOWN", "CANCELLED"],
}
const COMMAND_TYPES := [
	"CREATE_SKILL_BUILD", "ACTIVATE_SKILL_VERSION", "CREATE_AGENT_SESSION",
	"EXECUTE_AGENT_TURN", "INGEST_CLIENT_EVENTS",
]
const COMMAND_STAGES := [
	"ACCEPT", "VALIDATE", "POLICY", "REGISTRY", "SANDBOX",
	"WORLD_VALIDATE", "WORLD_COMMIT", "EVIDENCE", "COMPLETE",
]
const ACTOR_TYPES := ["student", "agent", "teacher", "researcher", "operator", "service"]
const EVIDENCE_TYPES := [
	"DOMAIN_EVENT", "ACTION_LOG", "SANDBOX_LOG", "TEST_REPORT",
	"POLICY_DECISION", "WORLD_COMMIT", "LEARNER_UPDATE", "AUDIT_LOG",
]
const CLIENT_EVENT_TYPES := [
	"UI_ACTION", "CODE_EDITED", "BUILD_REQUESTED", "HINT_VIEWED",
	"FEEDBACK_SHOWN", "ANIMATION_COMPLETED", "CLIENT_ERROR", "SESSION_HEARTBEAT",
]
const SKILL_CAPABILITIES := [
	"WORLD_READ", "MOVE", "PLANT", "WATER", "HARVEST", "INTERACT", "SPEAK",
]
const MAX_SOURCE_FILES := 32
const MAX_SOURCE_BYTES := 1048576
const REALTIME_PROTOCOL_VERSION := "1.0.0"
const URI_UNRESERVED := "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-"
const URI_SUB_DELIMITERS := "!$&'()*+,;="
const RUNTIME_EVENT_PAYLOAD_FIELDS := {
	"command.accepted": ["command_type", "status", "accepted_at"],
	"command.stage_changed": ["from_status", "to_status", "command_revision", "attempt"],
	"command.terminal": ["status", "terminal_at", "result_ref", "error"],
	"agent.turn.feedback_ready": ["session_id", "turn_id", "command_id", "run_id", "message_key", "message", "source", "degraded", "fallback_reason", "evidence_refs", "completed_at"],
	"skill.build.requested": ["build_id", "skill_id", "source_sha256", "compiler_profile", "test_suite_version"],
	"skill.build.started": ["build_id", "worker_id", "attempt", "started_at"],
	"skill.build.completed": ["build_id", "artifact", "tests", "completed_at"],
	"skill.build.failed": ["build_id", "failed_at", "error"],
	"skill.certification.granted": ["build_id", "certification_id", "skill_id", "skill_version_id", "artifact_sha256", "capabilities", "certified_at"],
	"skill.certification.rejected": ["build_id", "skill_id", "rejected_at", "error", "evidence_refs"],
	"skill.activation.applied": ["skill_id", "skill_version_id", "certification_id", "artifact_sha256", "activation_scope", "previous_registry_revision", "registry_revision", "activated_at"],
	"skill.activation.rejected": ["skill_version_id", "activation_scope", "expected_registry_revision", "current_registry_revision", "rejected_at", "error"],
	"sandbox.run.started": ["run_id", "skill_version_id", "world_id", "expected_world_revision", "worker_id", "started_at"],
	"sandbox.run.completed": ["run_id", "exit_code", "action_intents", "finished_at", "evidence_refs"],
	"sandbox.run.failed": ["run_id", "failed_at", "error", "evidence_refs"],
	"world.committed": ["commit_id", "run_id", "world_id", "previous_world_revision", "world_revision", "state_hash", "applied_intent_ids", "committed_at", "evidence_refs"],
	"world.rejected": ["run_id", "world_id", "expected_world_revision", "current_world_revision", "rejected_intent_ids", "rejected_at", "error"],
	"learner.evidence.recorded": ["learner_id", "evidence_refs", "competency_ids", "recorded_at"],
	"learner.model.updated": ["learner_id", "previous_revision", "learner_revision", "projected_through_sequence", "changed_competency_ids", "updated_at", "evidence_refs"],
	"learner.projection.failed": ["learner_id", "source_event_id", "failed_at", "error"],
	"feishu.sync.requested": ["sync_id", "sync_kind", "target_ref", "attempt", "requested_at"],
	"feishu.sync.succeeded": ["sync_id", "remote_object_id", "attempt", "succeeded_at"],
	"feishu.sync.failed": ["sync_id", "attempt", "next_attempt_at", "failed_at", "error"],
	"feishu.sync.dead_lettered": ["sync_id", "attempts", "dead_lettered_at", "error"],
}
const VERSION_REQUIRED := [
	"api_version", "event_version", "policy_version", "world_rules_version",
	"teaching_spec_version",
]
const VERSION_OPTIONAL := [
	"skill_version", "artifact_sha256", "compiler_version",
	"sandbox_image_digest", "test_suite_version", "prompt_version", "model_version",
]
const VERSION_MAX_LENGTHS := {
	"api_version": 64,
	"event_version": 64,
	"policy_version": 96,
	"world_rules_version": 96,
	"teaching_spec_version": 96,
	"skill_version": 96,
	"compiler_version": 96,
	"sandbox_image_digest": 256,
	"test_suite_version": 96,
	"prompt_version": 96,
	"model_version": 128,
}

# Generated from contracts/error-catalog.json. Contract tests compare the exact
# set so a backend error cannot silently become an unhandled client state.
const ERROR_DEFINITIONS := {
	"INVALID_REQUEST": ["VALIDATION", false, "request.invalid"],
	"SCHEMA_VERSION_UNSUPPORTED": ["VALIDATION", false, "schema.version_unsupported"],
	"CONTENT_VERSION_MISMATCH": ["VALIDATION", false, "content.version_mismatch"],
	"AUTHENTICATION_REQUIRED": ["AUTHENTICATION", false, "auth.login_required"],
	"AUTHORIZATION_DENIED": ["AUTHORIZATION", false, "auth.permission_denied"],
	"POLICY_DENIED": ["POLICY", false, "policy.action_denied"],
	"NOT_FOUND": ["VALIDATION", false, "resource.not_found"],
	"PAYLOAD_TOO_LARGE": ["VALIDATION", false, "request.payload_too_large"],
	"IDEMPOTENCY_KEY_REUSED": ["CONCURRENCY", false, "request.idempotency_conflict"],
	"WORLD_REVISION_CONFLICT": ["CONCURRENCY", true, "world.changed_retry"],
	"EVENT_SEQUENCE_GAP": ["CONCURRENCY", true, "event.resync_required"],
	"SKILL_NOT_CERTIFIED": ["SKILL", false, "skill.not_certified"],
	"SKILL_VERSION_MISMATCH": ["SKILL", false, "skill.version_mismatch"],
	"ACTIVE_SKILL_ARTIFACT_MISMATCH": ["INVARIANT", false, "skill.artifact_mismatch"],
	"SANDBOX_COMPILE_ERROR": ["SANDBOX", false, "sandbox.compile_error"],
	"SANDBOX_RUNTIME_ERROR": ["SANDBOX", false, "sandbox.runtime_error"],
	"SANDBOX_RESOURCE_LIMIT": ["SANDBOX", false, "sandbox.resource_limit"],
	"WORLD_RULE_REJECTED": ["WORLD_RULE", false, "world.rule_rejected"],
	"DEPENDENCY_UNAVAILABLE": ["DEPENDENCY", true, "dependency.temporarily_unavailable"],
	"FEISHU_SIGNATURE_INVALID": ["AUTHENTICATION", false, "feishu.signature_invalid"],
	"FEISHU_REPLAY_DETECTED": ["AUTHENTICATION", false, "feishu.replay_detected"],
	"FEISHU_SYNC_FAILED": ["DEPENDENCY", true, "feishu.sync_delayed"],
	"RATE_LIMITED": ["RATE_LIMIT", true, "request.rate_limited"],
	"UNKNOWN_COMMIT_STATE": ["DEPENDENCY", false, "command.reconciling"],
	"INVARIANT_VIOLATION": ["INVARIANT", false, "system.invariant_violation"],
	"INTERNAL_ERROR": ["INTERNAL", false, "system.internal_error"],
}
const ERROR_HTTP_STATUSES := {
	"INVALID_REQUEST": 400,
	"SCHEMA_VERSION_UNSUPPORTED": 409,
	"CONTENT_VERSION_MISMATCH": 409,
	"AUTHENTICATION_REQUIRED": 401,
	"AUTHORIZATION_DENIED": 403,
	"POLICY_DENIED": 403,
	"NOT_FOUND": 404,
	"PAYLOAD_TOO_LARGE": 413,
	"IDEMPOTENCY_KEY_REUSED": 409,
	"WORLD_REVISION_CONFLICT": 409,
	"EVENT_SEQUENCE_GAP": 409,
	"SKILL_NOT_CERTIFIED": 422,
	"SKILL_VERSION_MISMATCH": 409,
	"ACTIVE_SKILL_ARTIFACT_MISMATCH": 500,
	"SANDBOX_COMPILE_ERROR": 422,
	"SANDBOX_RUNTIME_ERROR": 422,
	"SANDBOX_RESOURCE_LIMIT": 422,
	"WORLD_RULE_REJECTED": 422,
	"DEPENDENCY_UNAVAILABLE": 503,
	"FEISHU_SIGNATURE_INVALID": 401,
	"FEISHU_REPLAY_DETECTED": 409,
	"FEISHU_SYNC_FAILED": 503,
	"RATE_LIMITED": 429,
	"UNKNOWN_COMMIT_STATE": 503,
	"INVARIANT_VIOLATION": 500,
	"INTERNAL_ERROR": 500,
}


static func validate_operation_accepted(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"job_id", "job_type", "status", "created_at", "updated_at",
		"command_id", "trace_id", "error",
	], [], "OperationAccepted")
	if not shape.ok:
		return shape
	for check in [
		_validate_pattern(value.job_id, "^job_[A-Za-z0-9_-]{8,96}$", "OperationAccepted.job_id"),
		_validate_pattern(value.job_type, "^[A-Z][A-Z0-9_]{2,63}$", "OperationAccepted.job_type"),
		_validate_pattern(value.command_id, "^cmd_[A-Za-z0-9_-]{8,96}$", "OperationAccepted.command_id"),
		_validate_pattern(value.trace_id, "^trace_[A-Za-z0-9_-]{8,96}$", "OperationAccepted.trace_id"),
		_validate_date_time(value.created_at, "OperationAccepted.created_at"),
		_validate_date_time(value.updated_at, "OperationAccepted.updated_at"),
	]:
		if not check.ok:
			return check
	if typeof(value.status) != TYPE_STRING or value.status not in ["ACCEPTED", "QUEUED"]:
		return _failure("OperationAccepted.status must be ACCEPTED or QUEUED")
	if value.error != null:
		return _failure("An accepted operation cannot contain an error")
	return _success()


static func validate_bootstrap_response(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"request_context", "api_version", "server_time", "actor", "content",
		"capabilities", "limits", "world",
	], [], "BootstrapResponse")
	if not shape.ok:
		return shape
	for check in [
		validate_request_context(value.request_context),
		_validate_pattern(value.api_version, "^[0-9]+\\.[0-9]+\\.[0-9]+$", "BootstrapResponse.api_version"),
		_validate_date_time(value.server_time, "BootstrapResponse.server_time"),
		_validate_actor(value.actor),
		_validate_content_ref(value.content),
	]:
		if not check.ok:
			return check
	if value.actor != value.request_context.actor or value.content != value.request_context.content_ref:
		return _failure("BootstrapResponse duplicates must agree with request_context")
	var capabilities_shape := _require_shape(value.capabilities, [
		"skill_builds", "agent_sessions", "world_event_stream", "client_event_batch", "evidence_query",
	], [], "BootstrapResponse.capabilities")
	if not capabilities_shape.ok:
		return capabilities_shape
	for field in value.capabilities:
		if typeof(value.capabilities[field]) != TYPE_BOOL:
			return _failure("BootstrapResponse.capabilities.%s must be boolean" % field)
	var limits_shape := _require_shape(value.limits, [
		"max_source_files", "max_source_bytes", "max_client_events_per_batch", "max_agent_turn_chars",
	], [], "BootstrapResponse.limits")
	if not limits_shape.ok:
		return limits_shape
	for field in value.limits:
		if not _is_integer_in_range(value.limits[field], 1):
			return _failure("BootstrapResponse.limits.%s must be a positive integer" % field)
	if value.limits.max_source_files != MAX_SOURCE_FILES:
		return _failure("BootstrapResponse.limits.max_source_files disagrees with the request boundary")
	if value.limits.max_source_bytes != MAX_SOURCE_BYTES:
		return _failure("BootstrapResponse.limits.max_source_bytes disagrees with the request boundary")
	var world_shape := _require_shape(value.world, [
		"world_id", "revision", "stream_id", "last_event_sequence",
		"stream_protocol_version", "snapshot_url", "events_url", "stream_url",
	], [], "BootstrapResponse.world")
	if not world_shape.ok:
		return world_shape
	var world_id_check := validate_identifier(value.world.world_id, "BootstrapResponse.world.world_id")
	if not world_id_check.ok:
		return world_id_check
	if not _is_integer_in_range(value.world.revision, 0) or not _is_integer_in_range(value.world.last_event_sequence, 0):
		return _failure("BootstrapResponse.world revision fields must be non-negative integers")
	var stream_id_check := _validate_pattern(value.world.stream_id, "^[A-Za-z][A-Za-z0-9:_-]{2,159}$", "BootstrapResponse.world.stream_id")
	if not stream_id_check.ok:
		return stream_id_check
	if value.world.stream_id != "world:%s" % value.world.world_id:
		return _failure("BootstrapResponse.world.stream_id must equal world: plus world_id")
	if value.world.stream_protocol_version != REALTIME_PROTOCOL_VERSION:
		return _failure("BootstrapResponse.world.stream_protocol_version is unsupported")
	for field in ["snapshot_url", "events_url"]:
		if (
			not _string_with_length(value.world[field], 1, 2048)
			or not _is_rfc3986_reference(value.world[field], false)
		):
			return _failure("BootstrapResponse.world.%s must be a valid URI reference" % field)
	var stream_url_check := _validate_pattern(value.world.stream_url, "^wss://[^@/?#]+(/[^?#]*)?$", "BootstrapResponse.world.stream_url")
	if (
		not stream_url_check.ok
		or value.world.stream_url.length() > 2048
		or not _is_rfc3986_reference(value.world.stream_url, true)
	):
		return _failure("BootstrapResponse.world.stream_url must be a credential-free wss URL without query or fragment")
	return _success()


static func validate_student_bootstrap_v2(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"request_context", "api_version", "contract_version", "server_time", "actor",
		"content", "capabilities", "session", "build", "activation", "world",
	], [], "StudentBootstrapV2")
	if not shape.ok:
		return shape
	for check in [
		validate_request_context(value.request_context),
		_validate_date_time(value.server_time, "StudentBootstrapV2.server_time"),
		_validate_actor(value.actor),
		_validate_content_ref(value.content),
	]:
		if not check.ok:
			return check
	if value.api_version != "1.1.0" or value.contract_version != "0.4.0":
		return _failure("StudentBootstrapV2 version authority drifted")
	if value.actor != value.request_context.actor or value.content != value.request_context.content_ref:
		return _failure("StudentBootstrapV2 actor/content must equal request_context authority")
	if value.actor.actor_type != "student":
		return _failure("StudentBootstrapV2 actor must be a student")

	var capabilities_shape := _require_shape(value.capabilities, [
		"skill_builds", "skill_activations", "agent_sessions", "http_world_recovery",
		"evidence_query",
	], [], "StudentBootstrapV2.capabilities")
	if not capabilities_shape.ok:
		return capabilities_shape
	for field in value.capabilities:
		if typeof(value.capabilities[field]) != TYPE_BOOL:
			return _failure("StudentBootstrapV2.capabilities.%s must be boolean" % field)

	var session_shape := _require_shape(
		value.session, ["current_session_id", "teaching_spec_version", "create_request"], [], "StudentBootstrapV2.session"
	)
	if not session_shape.ok:
		return session_shape
	if value.session.current_session_id != null:
		var current_session_check := validate_identifier(
			value.session.current_session_id, "StudentBootstrapV2.session.current_session_id"
		)
		if not current_session_check.ok:
			return current_session_check
	var teaching_check := _validate_pattern(
		value.session.teaching_spec_version,
		"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
		"StudentBootstrapV2.session.teaching_spec_version",
	)
	if not teaching_check.ok:
		return teaching_check
	var create_shape := _require_shape(value.session.create_request, [
		"world_id", "learner_id", "agent_profile_id", "channel", "locale", "content",
		"expected_world_revision",
	], [], "StudentBootstrapV2.session.create_request")
	if not create_shape.ok:
		return create_shape
	var create_request_check := validate_agent_session_create_request(value.session.create_request)
	if not create_request_check.ok:
		return create_request_check
	if value.session.create_request.channel != "GAME":
		return _failure("StudentBootstrapV2 session channel must be GAME")
	if value.session.create_request.learner_id != value.actor.actor_id:
		return _failure("StudentBootstrapV2 learner_id must equal actor_id")
	if value.session.create_request.content != value.content:
		return _failure("StudentBootstrapV2 create_request content must equal content authority")

	var build_shape := _require_shape(value.build, [
		"build_policy_id", "compiler_profile", "compiler_version", "sandbox_image_digest",
		"test_suite_version", "allowed_capabilities", "max_source_files", "max_source_bytes",
	], [], "StudentBootstrapV2.build")
	if not build_shape.ok:
		return build_shape
	for field in ["build_policy_id", "compiler_profile", "compiler_version", "test_suite_version"]:
		var version_check := _validate_pattern(
			value.build[field],
			"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
			"StudentBootstrapV2.build.%s" % field,
		)
		if not version_check.ok:
			return version_check
	var image_check := _validate_pattern(
		value.build.sandbox_image_digest,
		"^sha256:[a-f0-9]{64}$",
		"StudentBootstrapV2.build.sandbox_image_digest",
	)
	if not image_check.ok:
		return image_check
	var allowed_check := _validate_unique_string_array(
		value.build.allowed_capabilities, 7, 10, "StudentBootstrapV2.build.allowed_capabilities"
	)
	if not allowed_check.ok:
		return allowed_check
	for capability in value.build.allowed_capabilities:
		if capability not in SKILL_CAPABILITIES:
			return _failure("StudentBootstrapV2 contains an unsupported build capability")
	if value.build.max_source_files != MAX_SOURCE_FILES or value.build.max_source_bytes != MAX_SOURCE_BYTES:
		return _failure("StudentBootstrapV2 build source limits drifted")

	var activation_shape := _require_shape(
		value.activation, ["scope", "registry_revision", "active"], [], "StudentBootstrapV2.activation"
	)
	if not activation_shape.ok:
		return activation_shape
	var scope_shape := _require_shape(
		value.activation.scope, ["world_id", "agent_profile_id"], [], "StudentBootstrapV2.activation.scope"
	)
	if not scope_shape.ok:
		return scope_shape
	for field in ["world_id", "agent_profile_id"]:
		var scope_id_check := validate_identifier(
			value.activation.scope[field], "StudentBootstrapV2.activation.scope.%s" % field
		)
		if not scope_id_check.ok:
			return scope_id_check
	if not _is_integer_in_range(value.activation.registry_revision, 0):
		return _failure("StudentBootstrapV2 activation registry_revision must be non-negative")
	if value.activation.active != null:
		var active_shape := _require_shape(value.activation.active, [
			"activation_id", "skill_id", "skill_version_id", "artifact_sha256",
			"certification_id", "registry_revision", "activated_at",
		], [], "StudentBootstrapV2.activation.active")
		if not active_shape.ok:
			return active_shape
		for check in [
			_validate_pattern(value.activation.active.activation_id, "^activation_[A-Za-z0-9_-]{8,118}$", "StudentBootstrapV2.activation.active.activation_id"),
			validate_identifier(value.activation.active.skill_id, "StudentBootstrapV2.activation.active.skill_id"),
			_validate_pattern(value.activation.active.skill_version_id, "^skillver_[A-Za-z0-9_-]{8,118}$", "StudentBootstrapV2.activation.active.skill_version_id"),
			_validate_pattern(value.activation.active.artifact_sha256, "^[a-f0-9]{64}$", "StudentBootstrapV2.activation.active.artifact_sha256"),
			_validate_pattern(value.activation.active.certification_id, "^cert_[A-Za-z0-9_-]{8,122}$", "StudentBootstrapV2.activation.active.certification_id"),
			_validate_date_time(value.activation.active.activated_at, "StudentBootstrapV2.activation.active.activated_at"),
		]:
			if not check.ok:
				return check
		if not _is_integer_in_range(value.activation.active.registry_revision, 1):
			return _failure("StudentBootstrapV2 active registry_revision must be positive")
		if value.activation.active.registry_revision != value.activation.registry_revision:
			return _failure("StudentBootstrapV2 active registry_revision disagrees with activation")

	var world_shape := _require_shape(value.world, [
		"world_id", "revision", "last_event_sequence", "state_hash", "snapshot_url", "events_url",
	], [], "StudentBootstrapV2.world")
	if not world_shape.ok:
		return world_shape
	for check in [
		validate_identifier(value.world.world_id, "StudentBootstrapV2.world.world_id"),
		_validate_pattern(value.world.state_hash, "^[a-f0-9]{64}$", "StudentBootstrapV2.world.state_hash"),
	]:
		if not check.ok:
			return check
	if not _is_integer_in_range(value.world.revision, 0) or not _is_integer_in_range(value.world.last_event_sequence, 0):
		return _failure("StudentBootstrapV2 world revisions must be non-negative integers")
	for field in ["snapshot_url", "events_url"]:
		if not _string_with_length(value.world[field], 1, 2048) or not _is_rfc3986_reference(value.world[field], false):
			return _failure("StudentBootstrapV2.world.%s must be a URI reference" % field)
	if value.session.create_request.world_id != value.world.world_id or value.activation.scope.world_id != value.world.world_id:
		return _failure("StudentBootstrapV2 session and activation must target the bootstrap world")
	if value.activation.scope.agent_profile_id != value.session.create_request.agent_profile_id:
		return _failure("StudentBootstrapV2 session and activation agent_profile_id must agree")
	if value.session.create_request.expected_world_revision != value.world.revision:
		return _failure("StudentBootstrapV2 expected_world_revision must equal world revision")
	if value.world.snapshot_url != "/v1/worlds/%s/snapshot" % value.world.world_id:
		return _failure("StudentBootstrapV2 snapshot_url must identify the bootstrap world")
	if value.world.events_url != "/v1/worlds/%s/events" % value.world.world_id:
		return _failure("StudentBootstrapV2 events_url must identify the bootstrap world")
	return _success()


static func validate_skill_build(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"request_context", "build_id", "skill_id", "skill_version_id", "status", "terminal",
		"created_at", "updated_at", "artifact", "certification", "phases", "failure",
		"evidence_refs", "versions",
	], [], "SkillBuild")
	if not shape.ok:
		return shape
	for check in [
		validate_request_context(value.request_context),
		validate_identifier(value.build_id, "SkillBuild.build_id"),
		validate_identifier(value.skill_id, "SkillBuild.skill_id"),
		_validate_date_time(value.created_at, "SkillBuild.created_at"),
		_validate_date_time(value.updated_at, "SkillBuild.updated_at"),
	]:
		if not check.ok:
			return check
	if value.skill_version_id != null:
		var version_id_check := validate_identifier(value.skill_version_id, "SkillBuild.skill_version_id")
		if not version_id_check.ok:
			return version_id_check
	if typeof(value.status) != TYPE_STRING or value.status not in [
		"ACCEPTED", "QUEUED", "COMPILING", "TESTING", "CERTIFYING", "CERTIFIED", "REJECTED", "FAILED",
	]:
		return _failure("SkillBuild.status is invalid")
	if typeof(value.terminal) != TYPE_BOOL:
		return _failure("SkillBuild.terminal must be boolean")
	var terminal_expected: bool = value.status in ["CERTIFIED", "REJECTED", "FAILED"]
	if value.terminal != terminal_expected:
		return _failure("SkillBuild.terminal disagrees with status")
	if value.status == "CERTIFIED" and (value.skill_version_id == null or not value.artifact is Dictionary or not value.certification is Dictionary or value.failure != null):
		return _failure("CERTIFIED SkillBuild requires version, artifact and certification without failure")
	if value.status in ["REJECTED", "FAILED"] and not value.failure is Dictionary:
		return _failure("Rejected or failed SkillBuild requires a structured failure")
	if not terminal_expected and value.failure != null:
		return _failure("Non-terminal SkillBuild cannot contain failure")
	if value.artifact != null:
		var artifact_shape := _require_shape(value.artifact, [
			"artifact_sha256", "source_sha256", "compiler_profile", "compiler_version", "test_suite_version",
		], [], "SkillBuild.artifact")
		if not artifact_shape.ok:
			return artifact_shape
		for field in ["artifact_sha256", "source_sha256"]:
			var hash_check := _validate_pattern(value.artifact[field], "^[a-f0-9]{64}$", "SkillBuild.artifact.%s" % field)
			if not hash_check.ok:
				return hash_check
		for field in ["compiler_profile", "compiler_version", "test_suite_version"]:
			if not _is_non_empty_string(value.artifact[field]) or value.artifact[field].length() > 64:
				return _failure("SkillBuild.artifact.%s must contain 1 to 64 characters" % field)
	if value.certification != null:
		var certification_shape := _require_shape(value.certification, ["certification_id", "issued_at", "capabilities"], [], "SkillBuild.certification")
		if not certification_shape.ok:
			return certification_shape
		for check in [
			validate_identifier(value.certification.certification_id, "SkillBuild.certification.certification_id"),
			_validate_date_time(value.certification.issued_at, "SkillBuild.certification.issued_at"),
		]:
			if not check.ok:
				return check
		var capabilities_check := _validate_unique_string_array(value.certification.capabilities, 1000, 64, "SkillBuild.certification.capabilities")
		if not capabilities_check.ok:
			return capabilities_check
	if not value.phases is Array or value.phases.is_empty():
		return _failure("SkillBuild.phases must be a non-empty Array")
	for phase in value.phases:
		var phase_shape := _require_shape(phase, ["name", "status"], ["started_at", "finished_at", "diagnostic_codes"], "SkillBuild.phase")
		if not phase_shape.ok:
			return phase_shape
		if typeof(phase.name) != TYPE_STRING or phase.name not in ["VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST", "CERTIFY"]:
			return _failure("SkillBuild.phase.name is invalid")
		if typeof(phase.status) != TYPE_STRING or phase.status not in ["PENDING", "RUNNING", "PASSED", "FAILED", "SKIPPED"]:
			return _failure("SkillBuild.phase.status is invalid")
		for field in ["started_at", "finished_at"]:
			if phase.has(field) and phase[field] != null:
				var date_check := _validate_date_time(phase[field], "SkillBuild.phase.%s" % field)
				if not date_check.ok:
					return date_check
		if phase.has("diagnostic_codes"):
			var diagnostic_check := _validate_unique_string_array(phase.diagnostic_codes, 100, 96, "SkillBuild.phase.diagnostic_codes")
			if not diagnostic_check.ok:
				return diagnostic_check
	if value.failure != null:
		var failure_check := validate_contract_error(value.failure)
		if not failure_check.ok:
			return failure_check
		if value.failure.stage not in ["VALIDATE_SOURCE", "COMPILE", "PUBLIC_TEST", "HIDDEN_TEST", "CERTIFY"]:
			return _failure("SkillBuild.failure.stage is invalid")
	if not value.evidence_refs is Array:
		return _failure("SkillBuild.evidence_refs must be an Array")
	for evidence in value.evidence_refs:
		var evidence_check := _validate_evidence_ref(evidence)
		if not evidence_check.ok:
			return evidence_check
	return _validate_version_set(value.versions)


static func validate_skill_activation(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"request_context", "activation_id", "skill_id", "skill_version_id",
		"certification_id", "artifact_sha256", "activation_scope",
		"previous_registry_revision", "registry_revision", "activated_at",
	], [], "SkillActivation")
	if not shape.ok:
		return shape
	for check in [
		validate_request_context(value.request_context),
		_validate_pattern(value.activation_id, "^activation_[A-Za-z0-9_-]{8,118}$", "SkillActivation.activation_id"),
		validate_identifier(value.skill_id, "SkillActivation.skill_id"),
		_validate_pattern(value.skill_version_id, "^skillver_[A-Za-z0-9_-]{8,118}$", "SkillActivation.skill_version_id"),
		_validate_pattern(value.certification_id, "^cert_[A-Za-z0-9_-]{8,122}$", "SkillActivation.certification_id"),
		_validate_pattern(value.artifact_sha256, "^[a-f0-9]{64}$", "SkillActivation.artifact_sha256"),
		_validate_date_time(value.activated_at, "SkillActivation.activated_at"),
	]:
		if not check.ok:
			return check
	var scope_shape := _require_shape(value.activation_scope, ["world_id", "agent_profile_id"], [], "SkillActivation.activation_scope")
	if not scope_shape.ok:
		return scope_shape
	for field in ["world_id", "agent_profile_id"]:
		var id_check := validate_identifier(value.activation_scope[field], "SkillActivation.activation_scope.%s" % field)
		if not id_check.ok:
			return id_check
	if not _is_integer_in_range(value.previous_registry_revision, 0):
		return _failure("SkillActivation.previous_registry_revision must be a non-negative integer")
	if not _is_integer_in_range(value.registry_revision, 1):
		return _failure("SkillActivation.registry_revision must be a positive integer")
	if value.registry_revision != value.previous_registry_revision + 1:
		return _failure("SkillActivation must advance exactly one registry revision")
	return _success()


static func validate_command_result(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"request_context", "command_id", "revision", "command_type", "status", "stage",
		"terminal", "accepted_at", "updated_at", "result", "error",
		"evidence_refs", "versions", "links",
	], [], "CommandResult")
	if not shape.ok:
		return shape
	for check in [
		validate_request_context(value.request_context),
		_validate_pattern(value.command_id, "^cmd_[A-Za-z0-9_-]{8,96}$", "CommandResult.command_id"),
		_validate_date_time(value.accepted_at, "CommandResult.accepted_at"),
		_validate_date_time(value.updated_at, "CommandResult.updated_at"),
	]:
		if not check.ok:
			return check
	if typeof(value.command_type) != TYPE_STRING or value.command_type not in COMMAND_TYPES:
		return _failure("CommandResult.command_type is unknown")
	if not _is_integer_in_range(value.revision, 1):
		return _failure("CommandResult.revision must be a positive integer")
	if typeof(value.status) != TYPE_STRING or value.status not in ALL_COMMAND_STATUSES:
		return _failure("CommandResult.status is unknown")
	if typeof(value.stage) != TYPE_STRING or value.stage not in COMMAND_STAGES:
		return _failure("CommandResult.stage is unknown")
	if typeof(value.terminal) != TYPE_BOOL:
		return _failure("CommandResult.terminal must be a boolean")
	if value.terminal != is_terminal_command_status(value.status):
		return _failure("CommandResult.terminal disagrees with status %s" % value.status)

	if value.status in ["ACCEPTED", "VALIDATING", "RUNNING_SANDBOX", "APPLYING_WORLD"]:
		if value.result != null or value.error != null:
			return _failure("A non-terminal command cannot contain result or error")
	elif value.status == "APPLIED":
		if value.stage != "COMPLETE" or value.result == null or value.error != null:
			return _failure("APPLIED requires COMPLETE, a result, and no error")
	elif value.status in ["REJECTED", "FAILED", "UNKNOWN"]:
		if value.result != null or not value.error is Dictionary:
			return _failure("%s requires a structured error and no result" % value.status)
		if value.status == "UNKNOWN" and (value.stage != "WORLD_COMMIT" or value.error.get("code") != "UNKNOWN_COMMIT_STATE" or value.error.get("stage") != "WORLD_COMMIT"):
			return _failure("UNKNOWN must carry UNKNOWN_COMMIT_STATE at WORLD_COMMIT")
	elif value.status == "CANCELLED" and value.result != null:
		return _failure("CANCELLED cannot contain a result")

	if value.result != null:
		var result_check := _validate_command_result_payload(value.result, value.command_type)
		if not result_check.ok:
			return result_check
	if value.error != null:
		var error_check := validate_contract_error(value.error)
		if not error_check.ok:
			return error_check
	if not value.evidence_refs is Array:
		return _failure("CommandResult.evidence_refs must be an Array")
	for evidence in value.evidence_refs:
		var evidence_check := _validate_evidence_ref(evidence)
		if not evidence_check.ok:
			return evidence_check
	var versions_check := _validate_version_set(value.versions)
	if not versions_check.ok:
		return versions_check
	return _validate_links(value.links, ["self"], ["run", "world_snapshot"], "CommandResult.links")


static func validate_agent_session(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"request_context", "session_id", "world_id", "learner_id", "agent_profile_id",
		"channel", "status", "created_at", "updated_at", "last_turn_sequence",
		"content", "versions", "links",
	], [], "AgentSession")
	if not shape.ok:
		return shape
	var context_check := validate_request_context(value.request_context)
	if not context_check.ok:
		return context_check
	for field in ["session_id", "world_id", "learner_id", "agent_profile_id"]:
		var id_check := validate_identifier(value[field], "AgentSession.%s" % field)
		if not id_check.ok:
			return id_check
	if typeof(value.channel) != TYPE_STRING or value.channel not in ["GAME", "TEACHER_PREVIEW"]:
		return _failure("AgentSession.channel is invalid")
	if typeof(value.status) != TYPE_STRING or value.status not in ["ACTIVE", "CLOSING", "CLOSED", "FAILED"]:
		return _failure("AgentSession.status is invalid")
	for field in ["created_at", "updated_at"]:
		var date_check := _validate_date_time(value[field], "AgentSession.%s" % field)
		if not date_check.ok:
			return date_check
	if not _is_integer_in_range(value.last_turn_sequence, 0):
		return _failure("AgentSession.last_turn_sequence must be a non-negative integer")
	for check in [
		_validate_content_ref(value.content),
		_validate_version_set(value.versions),
		_validate_links(value.links, ["self", "turns", "world_snapshot"], [], "AgentSession.links"),
	]:
		if not check.ok:
			return check
	return _success()


static func validate_evidence(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"request_context", "evidence_ref", "subject", "source", "occurred_at", "recorded_at",
		"integrity", "payload", "related_evidence", "versions",
	], [], "Evidence")
	if not shape.ok:
		return shape
	for check in [
		validate_request_context(value.request_context),
		_validate_evidence_ref(value.evidence_ref),
		_validate_date_time(value.occurred_at, "Evidence.occurred_at"),
		_validate_date_time(value.recorded_at, "Evidence.recorded_at"),
	]:
		if not check.ok:
			return check
	var subject_shape := _require_shape(value.subject, ["learner_id"], [], "Evidence.subject")
	if not subject_shape.ok:
		return subject_shape
	var learner_check := validate_identifier(value.subject.learner_id, "Evidence.subject.learner_id")
	if not learner_check.ok:
		return learner_check
	var source_shape := _require_shape(value.source, ["source_type", "source_id", "command_id", "world_id"], [], "Evidence.source")
	if not source_shape.ok:
		return source_shape
	if typeof(value.source.source_type) != TYPE_STRING or value.source.source_type not in [
		"SKILL_BUILD", "SKILL_RUN", "WORLD", "CLIENT_EVENT", "POLICY", "LEARNER_PROJECTOR",
	]:
		return _failure("Evidence.source.source_type is invalid")
	var source_id_check := validate_identifier(value.source.source_id, "Evidence.source.source_id")
	if not source_id_check.ok:
		return source_id_check
	if value.source.command_id != null:
		var command_check := _validate_pattern(value.source.command_id, "^cmd_[A-Za-z0-9_-]{8,96}$", "Evidence.source.command_id")
		if not command_check.ok:
			return command_check
	if value.source.world_id != null:
		var world_check := validate_identifier(value.source.world_id, "Evidence.source.world_id")
		if not world_check.ok:
			return world_check
	var integrity_shape := _require_shape(value.integrity, ["payload_sha256", "previous_evidence_sha256"], [], "Evidence.integrity")
	if not integrity_shape.ok:
		return integrity_shape
	var payload_hash_check := _validate_pattern(value.integrity.payload_sha256, "^[a-f0-9]{64}$", "Evidence.integrity.payload_sha256")
	if not payload_hash_check.ok:
		return payload_hash_check
	if value.integrity.previous_evidence_sha256 != null:
		var previous_hash_check := _validate_pattern(value.integrity.previous_evidence_sha256, "^[a-f0-9]{64}$", "Evidence.integrity.previous_evidence_sha256")
		if not previous_hash_check.ok:
			return previous_hash_check
	var payload_check := _validate_evidence_payload(value.payload)
	if not payload_check.ok:
		return payload_check
	var calculated_payload_hash := canonical_json_sha256_v1(value.payload)
	if calculated_payload_hash.is_empty() or calculated_payload_hash != value.integrity.payload_sha256:
		return _failure("Evidence.payload does not match its YAYA_CANONICAL_JSON_V1 hash")
	if value.evidence_ref.has("sha256") and value.evidence_ref.sha256 != calculated_payload_hash:
		return _failure("Evidence.evidence_ref.sha256 does not match its payload")
	if value.payload.evidence_kind == "WORLD_COMMIT" and (
		value.source.source_type != "WORLD"
		or value.source.source_id != value.payload.world_id
		or value.source.world_id != value.payload.world_id
	):
		return _failure("WORLD_COMMIT Evidence source and payload world identities must match")
	if not value.related_evidence is Array or value.related_evidence.size() > 64:
		return _failure("Evidence.related_evidence must contain at most 64 items")
	for evidence in value.related_evidence:
		var related_check := _validate_evidence_ref(evidence)
		if not related_check.ok:
			return related_check
	return _validate_version_set(value.versions)


static func validate_run(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"request_context", "run_id", "session_id", "turn_id", "command_id", "status", "terminal", "skill", "sandbox",
		"world_application", "agent_feedback", "created_at", "updated_at", "evidence_refs", "versions",
	], [], "Run")
	if not shape.ok:
		return shape
	for check in [
		validate_request_context(value.request_context),
		validate_identifier(value.run_id, "Run.run_id"),
		_validate_pattern(value.command_id, "^cmd_[A-Za-z0-9_-]{8,96}$", "Run.command_id"),
		_validate_date_time(value.created_at, "Run.created_at"),
		_validate_date_time(value.updated_at, "Run.updated_at"),
	]:
		if not check.ok:
			return check
	if (value.session_id == null) != (value.turn_id == null):
		return _failure("Run.session_id and Run.turn_id must both be null or both be identifiers")
	if value.session_id != null:
		for field in ["session_id", "turn_id"]:
			var owner_check := validate_identifier(value[field], "Run.%s" % field)
			if not owner_check.ok:
				return owner_check
	if typeof(value.status) != TYPE_STRING or value.status not in ["QUEUED", "RUNNING_SANDBOX", "APPLYING_WORLD", "SUCCEEDED", "REJECTED", "FAILED", "UNKNOWN"]:
		return _failure("Run.status is invalid")
	if typeof(value.terminal) != TYPE_BOOL or value.terminal != (value.status in ["SUCCEEDED", "REJECTED", "FAILED", "UNKNOWN"]):
		return _failure("Run.terminal disagrees with status")
	var skill_shape := _require_shape(value.skill, ["skill_id", "skill_version_id", "artifact_sha256", "certification_id"], [], "Run.skill")
	if not skill_shape.ok:
		return skill_shape
	for field in ["skill_id", "skill_version_id", "certification_id"]:
		var id_check := validate_identifier(value.skill[field], "Run.skill.%s" % field)
		if not id_check.ok:
			return id_check
	var artifact_check := _validate_pattern(value.skill.artifact_sha256, "^[a-f0-9]{64}$", "Run.skill.artifact_sha256")
	if not artifact_check.ok:
		return artifact_check

	var sandbox_check := _validate_run_sandbox(value.sandbox)
	if not sandbox_check.ok:
		return sandbox_check
	var world_check := _validate_run_world_application(value.world_application)
	if not world_check.ok:
		return world_check
	var pair: Array = [value.sandbox.status, value.world_application.status]
	var valid_pair := false
	match value.status:
		"QUEUED":
			valid_pair = pair == ["QUEUED", "NOT_ATTEMPTED"]
		"RUNNING_SANDBOX":
			valid_pair = pair == ["RUNNING", "NOT_ATTEMPTED"]
		"APPLYING_WORLD":
			valid_pair = pair == ["SUCCEEDED", "VALIDATING"]
		"SUCCEEDED":
			valid_pair = pair == ["SUCCEEDED", "COMMITTED"]
		"REJECTED":
			valid_pair = pair in [["REJECTED", "NOT_ATTEMPTED"], ["SUCCEEDED", "REJECTED"]]
		"FAILED":
			valid_pair = pair in [["TIMED_OUT", "NOT_ATTEMPTED"], ["FAILED", "NOT_ATTEMPTED"], ["SUCCEEDED", "FAILED"]]
		"UNKNOWN":
			valid_pair = pair == ["SUCCEEDED", "UNKNOWN"]
	if not valid_pair:
		return _failure("Run status disagrees with sandbox/world_application phases")
	if value.status == "UNKNOWN" and (value.world_application.failure.code != "UNKNOWN_COMMIT_STATE" or value.world_application.failure.stage != "WORLD_COMMIT"):
		return _failure("UNKNOWN Run must carry UNKNOWN_COMMIT_STATE at WORLD_COMMIT")
	if value.status in ["QUEUED", "RUNNING_SANDBOX", "APPLYING_WORLD"] and value.agent_feedback != null:
		return _failure("Non-terminal Run cannot expose Agent feedback")
	if value.agent_feedback != null:
		if value.session_id == null:
			return _failure("Run with Agent feedback must identify its session and turn")
		var feedback_check := validate_agent_turn_feedback(
			value.agent_feedback,
			value.command_id,
			value.run_id,
			value.session_id,
			value.turn_id,
		)
		if not feedback_check.ok:
			return feedback_check
	var run_evidence_check := _validate_evidence_refs(value.evidence_refs, "Run.evidence_refs")
	if not run_evidence_check.ok:
		return run_evidence_check
	if value.agent_feedback != null and _evidence_refs_signature(value.agent_feedback.evidence_refs) != _evidence_refs_signature(value.evidence_refs):
		return _failure("Run and AgentTurnFeedback evidence_refs must match")
	return _validate_version_set(value.versions)


static func validate_agent_turn_feedback(
	value: Variant,
	expected_command_id: String = "",
	expected_run_id: Variant = null,
	expected_session_id: Variant = null,
	expected_turn_id: Variant = null,
) -> Dictionary:
	var shape := _require_shape(value, [
		"session_id", "turn_id", "command_id", "run_id", "message_key", "message",
		"source", "degraded", "fallback_reason", "evidence_refs", "completed_at",
	], [], "AgentTurnFeedback")
	if not shape.ok:
		return shape
	for field in ["session_id", "turn_id"]:
		var identity_check := validate_identifier(value[field], "AgentTurnFeedback.%s" % field)
		if not identity_check.ok:
			return identity_check
	if typeof(expected_session_id) == TYPE_STRING and value.session_id != expected_session_id:
		return _failure("AgentTurnFeedback.session_id does not match its Run")
	if typeof(expected_turn_id) == TYPE_STRING and value.turn_id != expected_turn_id:
		return _failure("AgentTurnFeedback.turn_id does not match its Run")
	var command_check := _validate_pattern(value.command_id, "^cmd_[A-Za-z0-9_-]{8,96}$", "AgentTurnFeedback.command_id")
	if not command_check.ok:
		return command_check
	if not expected_command_id.is_empty() and value.command_id != expected_command_id:
		return _failure("AgentTurnFeedback.command_id does not match its owner")
	if value.run_id != null:
		var run_check := validate_identifier(value.run_id, "AgentTurnFeedback.run_id")
		if not run_check.ok:
			return run_check
	if typeof(expected_run_id) == TYPE_STRING and value.run_id != expected_run_id:
		return _failure("AgentTurnFeedback.run_id does not match its Run")
	var message_key_check := _validate_pattern(value.message_key, "^[a-z][a-z0-9_.-]{2,127}$", "AgentTurnFeedback.message_key")
	if not message_key_check.ok:
		return message_key_check
	if not _string_with_length(value.message, 1, 4000):
		return _failure("AgentTurnFeedback.message must contain 1 to 4000 characters")
	if typeof(value.degraded) != TYPE_BOOL:
		return _failure("AgentTurnFeedback.degraded must be a boolean")
	if value.degraded:
		if value.source != "provider_fallback":
			return _failure("Degraded AgentTurnFeedback must use provider_fallback")
		var fallback_check := _validate_pattern(value.fallback_reason, "^[A-Z][A-Z0-9_]{2,95}$", "AgentTurnFeedback.fallback_reason")
		if not fallback_check.ok:
			return fallback_check
	elif value.source != "provider" or value.fallback_reason != null:
		return _failure("Non-degraded AgentTurnFeedback must use provider with no fallback_reason")
	var evidence_check := _validate_evidence_refs(value.evidence_refs, "AgentTurnFeedback.evidence_refs")
	if not evidence_check.ok:
		return evidence_check
	return _validate_date_time(value.completed_at, "AgentTurnFeedback.completed_at")


static func validate_world_snapshot(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"request_context", "world_id", "revision", "last_event_sequence",
		"state_schema_version", "state_hash", "generated_at", "world_rules_version", "state",
	], [], "WorldSnapshot")
	if not shape.ok:
		return shape
	for check in [
		validate_request_context(value.request_context),
		validate_identifier(value.world_id, "WorldSnapshot.world_id"),
		_validate_pattern(value.state_hash, "^[a-f0-9]{64}$", "WorldSnapshot.state_hash"),
		_validate_date_time(value.generated_at, "WorldSnapshot.generated_at"),
	]:
		if not check.ok:
			return check
	if not _is_integer_in_range(value.revision, 0) or not _is_integer_in_range(value.last_event_sequence, 0):
		return _failure("WorldSnapshot revision and sequence must be non-negative integers")
	if value.state_schema_version != "1.0.0":
		return _failure("WorldSnapshot.state_schema_version must be 1.0.0")
	if not _string_with_length(value.world_rules_version, 1, 96):
		return _failure("WorldSnapshot.world_rules_version must contain 1 to 96 characters")
	return _validate_world_state(value.state)


static func validate_event(value: Variant, expected_after_sequence: int = -1) -> Dictionary:
	var shape := _require_shape(value, [
		"event_id", "event_type", "event_version", "schema_version", "stream_id",
		"sequence", "occurred_at", "producer", "trace_id", "command_id",
		"correlation_id", "causation_id", "content_ref", "payload",
	], [], "EventEnvelope")
	if not shape.ok:
		return shape
	for check in [
		_validate_pattern(value.event_id, "^evt_[A-Za-z0-9_-]{8,128}$", "EventEnvelope.event_id"),
		_validate_pattern(value.event_type, "^[a-z][a-z0-9_.-]{2,127}$", "EventEnvelope.event_type"),
		_validate_pattern(value.stream_id, "^[A-Za-z][A-Za-z0-9:_-]{2,159}$", "EventEnvelope.stream_id"),
		_validate_date_time(value.occurred_at, "EventEnvelope.occurred_at"),
		_validate_pattern(value.producer, "^[a-z][a-z0-9_-]{2,63}$", "EventEnvelope.producer"),
		_validate_pattern(value.trace_id, "^trace_[A-Za-z0-9_-]{8,96}$", "EventEnvelope.trace_id"),
		_validate_pattern(value.command_id, "^cmd_[A-Za-z0-9_-]{8,96}$", "EventEnvelope.command_id"),
		_validate_pattern(value.correlation_id, "^corr_[A-Za-z0-9_-]{8,96}$", "EventEnvelope.correlation_id"),
		_validate_content_ref(value.content_ref),
	]:
		if not check.ok:
			return check
	if not _is_integer_in_range(value.event_version, 1):
		return _failure("EventEnvelope.event_version must be a positive integer")
	if value.schema_version != "1.0.0":
		return _failure("EventEnvelope.schema_version must be 1.0.0")
	if not _is_integer_in_range(value.sequence, 1):
		return _failure("EventEnvelope.sequence must be a positive integer")
	if value.causation_id != null:
		var causation_check := _validate_pattern(value.causation_id, "^(evt|cmd)_[A-Za-z0-9_-]{8,128}$", "EventEnvelope.causation_id")
		if not causation_check.ok:
			return causation_check
	if not value.payload is Dictionary:
		return _failure("EventEnvelope.payload must be a Dictionary")
	if expected_after_sequence >= 0 and value.sequence != expected_after_sequence + 1:
		return _sequence_gap(expected_after_sequence + 1, value.sequence)
	return _success()


static func validate_runtime_event(value: Variant, expected_after_sequence: int = -1) -> Dictionary:
	var envelope_check := validate_event(value, expected_after_sequence)
	if not envelope_check.ok:
		return envelope_check
	if value.event_version != 1:
		return _failure("RuntimeEvent.event_version must be exactly 1")
	if not RUNTIME_EVENT_PAYLOAD_FIELDS.has(value.event_type):
		return _failure("RuntimeEvent.event_type is not declared by AsyncAPI")
	var payload_shape := _require_shape(
		value.payload,
		RUNTIME_EVENT_PAYLOAD_FIELDS[value.event_type],
		[],
		"RuntimeEvent.%s.payload" % value.event_type,
	)
	if not payload_shape.ok:
		return payload_shape
	return _validate_runtime_event_payload(value.event_type, value.payload, value)


static func validate_world_event_page(value: Variant, expected_after_sequence: int = -1) -> Dictionary:
	var shape := _require_shape(value, [
		"request_context", "world_id", "snapshot_revision", "from_sequence",
		"to_sequence", "has_more", "next_after_sequence", "events",
	], [], "WorldEventPage")
	if not shape.ok:
		return shape
	for check in [
		validate_request_context(value.request_context),
		validate_identifier(value.world_id, "WorldEventPage.world_id"),
	]:
		if not check.ok:
			return check
	for field in ["snapshot_revision", "from_sequence", "to_sequence", "next_after_sequence"]:
		if not _is_integer_in_range(value[field], 0):
			return _failure("WorldEventPage.%s must be a non-negative integer" % field)
	if typeof(value.has_more) != TYPE_BOOL:
		return _failure("WorldEventPage.has_more must be a boolean")
	if not value.events is Array or value.events.size() > 500:
		return _failure("WorldEventPage.events must be an Array with at most 500 items")
	if value.events.is_empty():
		var expected_cursor: int = expected_after_sequence if expected_after_sequence >= 0 else floori(value.from_sequence)
		if value.from_sequence != expected_cursor or value.to_sequence != expected_cursor or value.next_after_sequence != expected_cursor:
			return _sequence_gap(expected_cursor, value.next_after_sequence)
		return _success()

	var seen_event_ids := {}
	var previous_sequence: int = expected_after_sequence if expected_after_sequence >= 0 else floori(value.events[0].sequence) - 1
	for event in value.events:
		var event_check := validate_event(event, previous_sequence)
		if not event_check.ok:
			return event_check
		if event.event_id in seen_event_ids:
			return _failure("WorldEventPage contains duplicate event_id %s" % event.event_id)
		seen_event_ids[event.event_id] = true
		if event.stream_id != "world:%s" % value.world_id:
			return _failure("WorldEventPage event stream_id does not match world_id")
		previous_sequence = event.sequence
	if value.from_sequence != value.events[0].sequence:
		return _failure("WorldEventPage.from_sequence disagrees with first event")
	if value.to_sequence != previous_sequence or value.next_after_sequence != previous_sequence:
		return _failure("WorldEventPage terminal cursors disagree with last event")
	return _success()


static func validate_request_context(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"schema_version", "request_id", "correlation_id", "trace_id",
		"requested_at", "actor", "content_ref",
	], [], "RequestContext")
	if not shape.ok:
		return shape
	if value.schema_version != "1.0.0":
		return _failure("RequestContext.schema_version must be 1.0.0")
	for check in [
		_validate_pattern(value.request_id, "^req_[A-Za-z0-9_-]{8,96}$", "RequestContext.request_id"),
		_validate_pattern(value.correlation_id, "^corr_[A-Za-z0-9_-]{8,96}$", "RequestContext.correlation_id"),
		_validate_pattern(value.trace_id, "^trace_[A-Za-z0-9_-]{8,96}$", "RequestContext.trace_id"),
		_validate_date_time(value.requested_at, "RequestContext.requested_at"),
		_validate_actor(value.actor),
		_validate_content_ref(value.content_ref),
	]:
		if not check.ok:
			return check
	return _success()


static func validate_skill_activation_request(value: Variant) -> Dictionary:
	var shape := _require_shape(value, ["expected_registry_revision", "activation_scope"], ["reason"], "SkillActivationRequest")
	if not shape.ok:
		return shape
	if not _is_integer_in_range(value.expected_registry_revision, 0):
		return _failure("SkillActivationRequest.expected_registry_revision must be a non-negative integer")
	var scope_shape := _require_shape(value.activation_scope, ["world_id", "agent_profile_id"], [], "SkillActivationRequest.activation_scope")
	if not scope_shape.ok:
		return scope_shape
	for field in ["world_id", "agent_profile_id"]:
		var id_check := validate_identifier(value.activation_scope[field], "SkillActivationRequest.activation_scope.%s" % field)
		if not id_check.ok:
			return id_check
	if value.has("reason") and (typeof(value.reason) != TYPE_STRING or value.reason.is_empty() or value.reason.length() > 500):
		return _failure("SkillActivationRequest.reason must contain 1 to 500 characters")
	return _success()


static func validate_skill_build_create_request(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"skill_id", "display_name", "client_draft_revision", "source_bundle",
		"compiler_profile", "test_suite_version",
	], ["requested_capabilities"], "SkillBuildCreateRequest")
	if not shape.ok:
		return shape
	var skill_check := validate_identifier(value.skill_id, "SkillBuildCreateRequest.skill_id")
	if not skill_check.ok:
		return skill_check
	if typeof(value.display_name) != TYPE_STRING or value.display_name.is_empty() or value.display_name.length() > 80:
		return _failure("SkillBuildCreateRequest.display_name must contain 1 to 80 characters")
	if not _is_integer_in_range(value.client_draft_revision, 0):
		return _failure("SkillBuildCreateRequest.client_draft_revision must be a non-negative integer")
	var bundle_shape := _require_shape(value.source_bundle, ["language", "entrypoint", "files"], [], "SkillBuildCreateRequest.source_bundle")
	if not bundle_shape.ok:
		return bundle_shape
	if value.source_bundle.language != "CPP20":
		return _failure("SkillBuildCreateRequest.source_bundle.language must be CPP20")
	var entrypoint_check := _validate_pattern(value.source_bundle.entrypoint, "^[A-Za-z0-9_.\\/-]{1,240}$", "SkillBuildCreateRequest.source_bundle.entrypoint")
	if not entrypoint_check.ok:
		return entrypoint_check
	if not value.source_bundle.files is Array or value.source_bundle.files.is_empty() or value.source_bundle.files.size() > MAX_SOURCE_FILES:
		return _failure("SkillBuildCreateRequest.source_bundle.files must contain 1 to %d files" % MAX_SOURCE_FILES)
	var seen_paths := {}
	var entrypoint_matches := 0
	var total_source_bytes := 0
	for file in value.source_bundle.files:
		var file_shape := _require_shape(file, ["path", "content", "content_sha256"], [], "SkillBuildCreateRequest.source_bundle.file")
		if not file_shape.ok:
			return file_shape
		if typeof(file.content) != TYPE_STRING or file.content.length() > 1048576:
			return _failure("SkillBuildCreateRequest.source_bundle.file.content must be a string up to 1048576 characters")
		total_source_bytes += file.content.to_utf8_buffer().size()
		if total_source_bytes > MAX_SOURCE_BYTES:
			return _failure("SkillBuildCreateRequest.source_bundle content must total at most %d UTF-8 bytes" % MAX_SOURCE_BYTES)
		var path_check := _validate_pattern(file.path, "^(?!/)(?!.*(?:^|/)\\.\\.(?:/|$))[A-Za-z0-9_.\\/-]{1,240}$", "SkillBuildCreateRequest.source_bundle.file.path")
		if not path_check.ok:
			return path_check
		if seen_paths.has(file.path):
			return _failure("SkillBuildCreateRequest.source_bundle.file.path values must be unique")
		seen_paths[file.path] = true
		if file.path == value.source_bundle.entrypoint:
			entrypoint_matches += 1
		var hash_check := _validate_pattern(file.content_sha256, "^[a-f0-9]{64}$", "SkillBuildCreateRequest.source_bundle.file.content_sha256")
		if not hash_check.ok:
			return hash_check
		if file.content.sha256_text() != file.content_sha256:
			return _failure("SkillBuildCreateRequest.source_bundle.file.content_sha256 must match the UTF-8 content SHA-256")
	if entrypoint_matches != 1:
		return _failure("SkillBuildCreateRequest.source_bundle.entrypoint must match exactly one source file path")
	if value.compiler_profile != "YAYA_CPP20_SAFE_V1":
		return _failure("SkillBuildCreateRequest.compiler_profile is unsupported")
	var suite_check := _validate_pattern(value.test_suite_version, "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", "SkillBuildCreateRequest.test_suite_version")
	if not suite_check.ok:
		return suite_check
	if value.has("requested_capabilities"):
		if not value.requested_capabilities is Array or value.requested_capabilities.size() > 16:
			return _failure("SkillBuildCreateRequest.requested_capabilities must contain at most 16 values")
		var seen_capabilities := {}
		for capability in value.requested_capabilities:
			if typeof(capability) != TYPE_STRING or capability not in SKILL_CAPABILITIES or capability in seen_capabilities:
				return _failure("SkillBuildCreateRequest.requested_capabilities contains an invalid or duplicate value")
			seen_capabilities[capability] = true
	return _success()


static func validate_agent_session_create_request(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"world_id", "learner_id", "agent_profile_id", "channel", "locale", "content",
	], ["expected_world_revision"], "AgentSessionCreateRequest")
	if not shape.ok:
		return shape
	for field in ["world_id", "learner_id", "agent_profile_id"]:
		var id_check := validate_identifier(value[field], "AgentSessionCreateRequest.%s" % field)
		if not id_check.ok:
			return id_check
	if typeof(value.channel) != TYPE_STRING or value.channel not in ["GAME", "TEACHER_PREVIEW"]:
		return _failure("AgentSessionCreateRequest.channel is invalid")
	var locale_check := _validate_pattern(value.locale, "^[a-z]{2,3}(?:-[A-Z]{2})?$", "AgentSessionCreateRequest.locale")
	if not locale_check.ok:
		return locale_check
	var content_check := _validate_content_ref(value.content)
	if not content_check.ok:
		return content_check
	if value.has("expected_world_revision") and not _is_integer_in_range(value.expected_world_revision, 0):
		return _failure("AgentSessionCreateRequest.expected_world_revision must be a non-negative integer")
	return _success()


static func validate_agent_turn_create_request(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"turn_id", "expected_world_revision", "input", "skill_bindings", "client_state",
	], [], "AgentTurnCreateRequest")
	if not shape.ok:
		return shape
	var turn_check := validate_identifier(value.turn_id, "AgentTurnCreateRequest.turn_id")
	if not turn_check.ok:
		return turn_check
	if not _is_integer_in_range(value.expected_world_revision, 0):
		return _failure("AgentTurnCreateRequest.expected_world_revision must be a non-negative integer")
	var input_check := _validate_agent_turn_input(value.input)
	if not input_check.ok:
		return input_check
	if not value.skill_bindings is Array or value.skill_bindings.size() > 32:
		return _failure("AgentTurnCreateRequest.skill_bindings must contain at most 32 values")
	var seen_bindings: Array = []
	for binding in value.skill_bindings:
		if binding in seen_bindings:
			return _failure("AgentTurnCreateRequest.skill_bindings must be unique")
		seen_bindings.append(binding)
		var binding_shape := _require_shape(binding, [
			"skill_id", "skill_version_id", "artifact_sha256", "certification_id",
		], [], "AgentTurnCreateRequest.skill_binding")
		if not binding_shape.ok:
			return binding_shape
		for field in ["skill_id", "skill_version_id", "certification_id"]:
			var id_check := validate_identifier(binding[field], "AgentTurnCreateRequest.skill_binding.%s" % field)
			if not id_check.ok:
				return id_check
		var hash_check := _validate_pattern(binding.artifact_sha256, "^[a-f0-9]{64}$", "AgentTurnCreateRequest.skill_binding.artifact_sha256")
		if not hash_check.ok:
			return hash_check
	var state_shape := _require_shape(value.client_state, ["last_event_sequence", "client_turn_sequence"], [], "AgentTurnCreateRequest.client_state")
	if not state_shape.ok:
		return state_shape
	if not _is_integer_in_range(value.client_state.last_event_sequence, 0):
		return _failure("AgentTurnCreateRequest.client_state.last_event_sequence must be a non-negative integer")
	if not _is_integer_in_range(value.client_state.client_turn_sequence, 1):
		return _failure("AgentTurnCreateRequest.client_state.client_turn_sequence must be a positive integer")
	return _success()


static func validate_client_event_batch_request(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"batch_id", "session_id", "world_id", "first_sequence", "last_sequence", "events",
	], [], "ClientEventBatchRequest")
	if not shape.ok:
		return shape
	for field in ["batch_id", "session_id", "world_id"]:
		var id_check := validate_identifier(value[field], "ClientEventBatchRequest.%s" % field)
		if not id_check.ok:
			return id_check
	if not _is_integer_in_range(value.first_sequence, 1) or not _is_integer_in_range(value.last_sequence, 1):
		return _failure("ClientEventBatchRequest boundaries must be positive integers")
	if not value.events is Array or value.events.is_empty() or value.events.size() > 500:
		return _failure("ClientEventBatchRequest.events must contain 1 to 500 events")
	var expected: int = floori(value.first_sequence)
	var seen := {}
	for event in value.events:
		var event_shape := _require_shape(event, [
			"event_id", "sequence", "occurred_at", "event_type", "world_revision", "payload",
		], [], "ClientEvent")
		if not event_shape.ok:
			return event_shape
		for check in [
			_validate_pattern(event.event_id, "^client_evt_[A-Za-z0-9_-]{8,128}$", "ClientEvent.event_id"),
			_validate_date_time(event.occurred_at, "ClientEvent.occurred_at"),
		]:
			if not check.ok:
				return check
		if event.event_id in seen:
			return _failure("ClientEventBatchRequest contains duplicate event_id")
		seen[event.event_id] = true
		if not _is_integer_in_range(event.sequence, 1) or event.sequence != expected:
			return _sequence_gap(expected, event.sequence)
		if not _is_integer_in_range(event.world_revision, 0):
			return _failure("ClientEvent.world_revision must be a non-negative integer")
		if typeof(event.event_type) != TYPE_STRING or event.event_type not in CLIENT_EVENT_TYPES:
			return _failure("ClientEvent.event_type is invalid")
		var payload_check := _validate_client_event_payload(event.event_type, event.payload)
		if not payload_check.ok:
			return payload_check
		expected += 1
	if value.last_sequence != expected - 1:
		return _sequence_gap(expected - 1, value.last_sequence)
	return _success()


static func validate_contract_error(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"code", "category", "retryable", "user_message_key", "stage",
	], ["message", "details", "evidence_ids"], "ContractError")
	if not shape.ok:
		return shape
	if typeof(value.code) != TYPE_STRING or not ERROR_DEFINITIONS.has(value.code):
		return _failure("ContractError.code is not in the error catalog")
	var definition: Array = ERROR_DEFINITIONS[value.code]
	if value.category != definition[0] or value.retryable != definition[1] or value.user_message_key != definition[2]:
		return _failure("ContractError fields disagree with catalog entry %s" % value.code)
	var stage_check := _validate_pattern(value.stage, "^[A-Z][A-Z0-9_]{2,63}$", "ContractError.stage")
	if not stage_check.ok:
		return stage_check
	if value.has("message") and not _string_with_length(value.message, 1, 512):
		return _failure("ContractError.message must contain 1 to 512 characters")
	if value.has("details") and not value.details is Dictionary:
		return _failure("ContractError.details must be a Dictionary")
	if value.has("evidence_ids"):
		if not value.evidence_ids is Array or value.evidence_ids.size() > 64:
			return _failure("ContractError.evidence_ids must be an Array with at most 64 items")
		var seen := {}
		for evidence_id in value.evidence_ids:
			var id_check := _validate_pattern(evidence_id, "^evidence_[A-Za-z0-9_-]{8,128}$", "ContractError.evidence_ids[]")
			if not id_check.ok or evidence_id in seen:
				return _failure("ContractError.evidence_ids contains an invalid or duplicate value")
			seen[evidence_id] = true
	return _success()


static func validate_error_response(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"request_id", "trace_id", "status", "data", "error",
	], ["command_id", "versions"], "ErrorResponse")
	if not shape.ok:
		return shape
	for check in [
		_validate_pattern(value.request_id, "^req_[A-Za-z0-9_-]{8,96}$", "ErrorResponse.request_id"),
		_validate_pattern(value.trace_id, "^trace_[A-Za-z0-9_-]{8,96}$", "ErrorResponse.trace_id"),
		validate_contract_error(value.error),
	]:
		if not check.ok:
			return check
	if value.has("command_id"):
		var command_check := _validate_pattern(value.command_id, "^cmd_[A-Za-z0-9_-]{8,96}$", "ErrorResponse.command_id")
		if not command_check.ok:
			return command_check
	if typeof(value.status) != TYPE_STRING or value.status not in ["REJECTED", "FAILED", "UNKNOWN"]:
		return _failure("ErrorResponse.status is invalid")
	var is_unknown_commit: bool = value.error.code == "UNKNOWN_COMMIT_STATE"
	if value.status == "UNKNOWN":
		if not is_unknown_commit or value.error.stage != "WORLD_COMMIT":
			return _failure("UNKNOWN ErrorResponse must carry UNKNOWN_COMMIT_STATE at WORLD_COMMIT")
		if not value.has("command_id"):
			return _failure("UNKNOWN ErrorResponse must carry command_id for reconciliation")
	elif is_unknown_commit:
		return _failure("UNKNOWN_COMMIT_STATE requires ErrorResponse.status UNKNOWN")
	if value.data != null:
		return _failure("ErrorResponse.data must be null")
	if value.has("versions"):
		return _validate_version_set(value.versions)
	return _success()


static func validate_identifier(value: Variant, label: String = "identifier") -> Dictionary:
	return _validate_pattern(value, "^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$", label)


static func is_terminal_command_status(status: String) -> bool:
	return status in TERMINAL_COMMAND_STATUSES


static func http_status_for_error_code(code: String) -> int:
	return ERROR_HTTP_STATUSES.get(code, 0)


static func _validate_command_result_payload(value: Variant, command_type: String) -> Dictionary:
	if not value is Dictionary or not value.has("result_type") or typeof(value.result_type) != TYPE_STRING:
		return _failure("CommandResult.result must have a string result_type")
	match value.result_type:
		"WORLD_COMMIT":
			var shape := _require_shape(value, [
				"result_type", "world_id", "previous_revision", "world_revision",
				"first_event_sequence", "last_event_sequence",
			], [], "WorldCommitResult")
			if not shape.ok:
				return shape
			if command_type != "EXECUTE_AGENT_TURN":
				return _failure("WORLD_COMMIT is only valid for EXECUTE_AGENT_TURN")
			var id_check := validate_identifier(value.world_id, "WorldCommitResult.world_id")
			if not id_check.ok:
				return id_check
			if not _is_integer_in_range(value.previous_revision, 0) or not _is_integer_in_range(value.world_revision, 1):
				return _failure("WorldCommitResult revisions must be integers in range")
			if value.world_revision != value.previous_revision + 1:
				return _failure("WorldCommitResult must advance exactly one world revision")
			if not _is_integer_in_range(value.first_event_sequence, 1) or not _is_integer_in_range(value.last_event_sequence, 1):
				return _failure("WorldCommitResult sequences must be positive integers")
			if value.first_event_sequence > value.last_event_sequence:
				return _failure("WorldCommitResult event sequence range is reversed")
		"RESOURCE_CREATED":
			var shape := _require_shape(value, ["result_type", "resource_type", "resource_id", "resource_url"], [], "ResourceCreatedResult")
			if not shape.ok:
				return shape
			var expected: String = {
				"CREATE_SKILL_BUILD": "SKILL_BUILD",
				"ACTIVATE_SKILL_VERSION": "SKILL_ACTIVATION",
				"CREATE_AGENT_SESSION": "AGENT_SESSION",
			}.get(command_type, "")
			if expected == "" or value.resource_type != expected:
				return _failure("ResourceCreatedResult.resource_type disagrees with command_type")
			var id_check := validate_identifier(value.resource_id, "ResourceCreatedResult.resource_id")
			if (
				not id_check.ok
				or not _string_with_length(value.resource_url, 1, 2048)
				or not _is_rfc3986_reference(value.resource_url, false)
			):
				return _failure("ResourceCreatedResult resource fields are invalid")
		"CLIENT_EVENTS_ACCEPTED":
			var shape := _require_shape(value, [
				"result_type", "batch_id", "accepted_count", "duplicate_count", "rejected_count",
			], [], "ClientEventsAcceptedResult")
			if not shape.ok:
				return shape
			if command_type != "INGEST_CLIENT_EVENTS":
				return _failure("CLIENT_EVENTS_ACCEPTED is only valid for INGEST_CLIENT_EVENTS")
			var id_check := validate_identifier(value.batch_id, "ClientEventsAcceptedResult.batch_id")
			if not id_check.ok:
				return id_check
			for field in ["accepted_count", "duplicate_count", "rejected_count"]:
				if not _is_integer_in_range(value[field], 0):
					return _failure("ClientEventsAcceptedResult.%s must be a non-negative integer" % field)
		"NO_EFFECT":
			var shape := _require_shape(value, ["result_type", "reason_code"], [], "NoEffectResult")
			if not shape.ok:
				return shape
			if command_type != "EXECUTE_AGENT_TURN":
				return _failure("NO_EFFECT is only valid for EXECUTE_AGENT_TURN")
			return _validate_pattern(value.reason_code, "^[A-Z][A-Z0-9_]{2,95}$", "NoEffectResult.reason_code")
		_:
			return _failure("CommandResult.result_type is unknown")
	return _success()


static func _validate_agent_turn_input(value: Variant) -> Dictionary:
	if not value is Dictionary or not value.has("type") or typeof(value.type) != TYPE_STRING:
		return _failure("AgentTurnCreateRequest.input must contain a string type")
	match value.type:
		"MESSAGE":
			var shape := _require_shape(value, ["type", "text", "locale"], [], "AgentTurnCreateRequest.message_input")
			if not shape.ok:
				return shape
			if typeof(value.text) != TYPE_STRING or value.text.is_empty() or value.text.length() > 4000:
				return _failure("AgentTurnCreateRequest.message_input.text must contain 1 to 4000 characters")
			return _validate_pattern(value.locale, "^[a-z]{2,3}(?:-[A-Z]{2})?$", "AgentTurnCreateRequest.message_input.locale")
		"ASSIGNED_TASK":
			var shape := _require_shape(value, ["type", "task_id"], [], "AgentTurnCreateRequest.task_input")
			if not shape.ok:
				return shape
			return validate_identifier(value.task_id, "AgentTurnCreateRequest.task_input.task_id")
		"UI_ACTION":
			var shape := _require_shape(value, ["type", "action_id", "selection_id"], [], "AgentTurnCreateRequest.ui_action_input")
			if not shape.ok:
				return shape
			var action_check := _validate_pattern(value.action_id, "^[a-z][a-z0-9_.-]{1,95}$", "AgentTurnCreateRequest.ui_action_input.action_id")
			if not action_check.ok:
				return action_check
			return validate_identifier(value.selection_id, "AgentTurnCreateRequest.ui_action_input.selection_id")
		_:
			return _failure("AgentTurnCreateRequest.input.type is invalid")


static func _validate_client_event_payload(event_type: String, value: Variant) -> Dictionary:
	match event_type:
		"UI_ACTION":
			var shape := _require_shape(value, ["action_id", "component_id"], [], "ClientEvent.UI_ACTION.payload")
			if not shape.ok:
				return shape
			for check in [
				_validate_pattern(value.action_id, "^[a-z][a-z0-9_.-]{1,95}$", "ClientEvent.UI_ACTION.payload.action_id"),
				_validate_pattern(value.component_id, "^[A-Za-z0-9][A-Za-z0-9_.-]{1,95}$", "ClientEvent.UI_ACTION.payload.component_id"),
			]:
				if not check.ok:
					return check
		"CODE_EDITED":
			var shape := _require_shape(value, ["skill_id", "draft_revision", "source_sha256", "changed_file_count"], [], "ClientEvent.CODE_EDITED.payload")
			if not shape.ok:
				return shape
			for check in [
				validate_identifier(value.skill_id, "ClientEvent.CODE_EDITED.payload.skill_id"),
				_validate_pattern(value.source_sha256, "^[a-f0-9]{64}$", "ClientEvent.CODE_EDITED.payload.source_sha256"),
			]:
				if not check.ok:
					return check
			if not _is_integer_in_range(value.draft_revision, 0) or not _is_integer_in_range(value.changed_file_count, 1, 64):
				return _failure("ClientEvent.CODE_EDITED payload revisions or counts are invalid")
		"BUILD_REQUESTED":
			var shape := _require_shape(value, ["build_id", "skill_id"], [], "ClientEvent.BUILD_REQUESTED.payload")
			if not shape.ok:
				return shape
			for field in ["build_id", "skill_id"]:
				var id_check := validate_identifier(value[field], "ClientEvent.BUILD_REQUESTED.payload.%s" % field)
				if not id_check.ok:
					return id_check
		"HINT_VIEWED":
			var shape := _require_shape(value, ["hint_id", "hint_level"], [], "ClientEvent.HINT_VIEWED.payload")
			if not shape.ok:
				return shape
			var id_check := validate_identifier(value.hint_id, "ClientEvent.HINT_VIEWED.payload.hint_id")
			if not id_check.ok:
				return id_check
			if not _is_integer_in_range(value.hint_level, 1, 10):
				return _failure("ClientEvent.HINT_VIEWED.payload.hint_level must be between 1 and 10")
		"FEEDBACK_SHOWN":
			var shape := _require_shape(value, ["message_key", "source", "degraded"], [], "ClientEvent.FEEDBACK_SHOWN.payload")
			if not shape.ok:
				return shape
			var key_check := _validate_pattern(value.message_key, "^[a-z][a-z0-9_.-]{2,127}$", "ClientEvent.FEEDBACK_SHOWN.payload.message_key")
			if not key_check.ok:
				return key_check
			if typeof(value.source) != TYPE_STRING or value.source not in ["AGENT", "POLICY", "SYSTEM"] or typeof(value.degraded) != TYPE_BOOL:
				return _failure("ClientEvent.FEEDBACK_SHOWN payload source or degraded is invalid")
		"ANIMATION_COMPLETED":
			var shape := _require_shape(value, ["action_event_id", "animation_id", "result"], [], "ClientEvent.ANIMATION_COMPLETED.payload")
			if not shape.ok:
				return shape
			for check in [
				_validate_pattern(value.action_event_id, "^evt_[A-Za-z0-9_-]{8,128}$", "ClientEvent.ANIMATION_COMPLETED.payload.action_event_id"),
				_validate_pattern(value.animation_id, "^[a-z][a-z0-9_.-]{1,95}$", "ClientEvent.ANIMATION_COMPLETED.payload.animation_id"),
			]:
				if not check.ok:
					return check
			if typeof(value.result) != TYPE_STRING or value.result not in ["COMPLETED", "SKIPPED", "FAILED"]:
				return _failure("ClientEvent.ANIMATION_COMPLETED.payload.result is invalid")
		"CLIENT_ERROR":
			var shape := _require_shape(value, ["code", "message_sha256", "screen", "handled"], [], "ClientEvent.CLIENT_ERROR.payload")
			if not shape.ok:
				return shape
			for check in [
				_validate_pattern(value.code, "^[A-Z][A-Z0-9_]{2,95}$", "ClientEvent.CLIENT_ERROR.payload.code"),
				_validate_pattern(value.message_sha256, "^[a-f0-9]{64}$", "ClientEvent.CLIENT_ERROR.payload.message_sha256"),
				_validate_pattern(value.screen, "^[a-z][a-z0-9_.-]{1,95}$", "ClientEvent.CLIENT_ERROR.payload.screen"),
			]:
				if not check.ok:
					return check
			if typeof(value.handled) != TYPE_BOOL:
				return _failure("ClientEvent.CLIENT_ERROR.payload.handled must be boolean")
		"SESSION_HEARTBEAT":
			var shape := _require_shape(value, ["last_received_event_sequence"], [], "ClientEvent.SESSION_HEARTBEAT.payload")
			if not shape.ok:
				return shape
			if not _is_integer_in_range(value.last_received_event_sequence, 0):
				return _failure("ClientEvent.SESSION_HEARTBEAT.payload.last_received_event_sequence must be non-negative")
		_:
			return _failure("ClientEvent.event_type is invalid")
	return _success()


static func _validate_runtime_event_payload(event_type: String, value: Dictionary, envelope: Dictionary) -> Dictionary:
	var identifier_fields: Array = {
		"agent.turn.feedback_ready": ["session_id", "turn_id"],
		"skill.build.requested": ["build_id", "skill_id"],
		"skill.build.started": ["build_id", "worker_id"],
		"skill.build.completed": ["build_id"],
		"skill.build.failed": ["build_id"],
		"skill.certification.granted": ["build_id", "certification_id", "skill_id", "skill_version_id"],
		"skill.certification.rejected": ["build_id", "skill_id"],
		"skill.activation.applied": ["skill_id", "skill_version_id", "certification_id"],
		"skill.activation.rejected": ["skill_version_id"],
		"sandbox.run.started": ["run_id", "skill_version_id", "world_id", "worker_id"],
		"sandbox.run.completed": ["run_id"],
		"sandbox.run.failed": ["run_id"],
		"world.committed": ["commit_id", "run_id", "world_id"],
		"world.rejected": ["run_id", "world_id"],
		"learner.evidence.recorded": ["learner_id"],
		"learner.model.updated": ["learner_id"],
		"learner.projection.failed": ["learner_id"],
		"feishu.sync.requested": ["sync_id"],
		"feishu.sync.succeeded": ["sync_id"],
		"feishu.sync.failed": ["sync_id"],
		"feishu.sync.dead_lettered": ["sync_id"],
	}.get(event_type, [])
	for field in identifier_fields:
		var id_check := validate_identifier(value[field], "RuntimeEvent.%s.payload.%s" % [event_type, field])
		if not id_check.ok:
			return id_check
	var timestamp_fields: Array = {
		"command.accepted": ["accepted_at"],
		"command.terminal": ["terminal_at"],
		"agent.turn.feedback_ready": ["completed_at"],
		"skill.build.started": ["started_at"],
		"skill.build.completed": ["completed_at"],
		"skill.build.failed": ["failed_at"],
		"skill.certification.granted": ["certified_at"],
		"skill.certification.rejected": ["rejected_at"],
		"skill.activation.applied": ["activated_at"],
		"skill.activation.rejected": ["rejected_at"],
		"sandbox.run.started": ["started_at"],
		"sandbox.run.completed": ["finished_at"],
		"sandbox.run.failed": ["failed_at"],
		"world.committed": ["committed_at"],
		"world.rejected": ["rejected_at"],
		"learner.evidence.recorded": ["recorded_at"],
		"learner.model.updated": ["updated_at"],
		"learner.projection.failed": ["failed_at"],
		"feishu.sync.requested": ["requested_at"],
		"feishu.sync.succeeded": ["succeeded_at"],
		"feishu.sync.failed": ["failed_at"],
		"feishu.sync.dead_lettered": ["dead_lettered_at"],
	}.get(event_type, [])
	for field in timestamp_fields:
		var time_check := _validate_date_time(value[field], "RuntimeEvent.%s.payload.%s" % [event_type, field])
		if not time_check.ok:
			return time_check
	var error_fields: Array = {
		"skill.build.failed": ["error"],
		"skill.certification.rejected": ["error"],
		"skill.activation.rejected": ["error"],
		"sandbox.run.failed": ["error"],
		"world.rejected": ["error"],
		"learner.projection.failed": ["error"],
		"feishu.sync.failed": ["error"],
		"feishu.sync.dead_lettered": ["error"],
	}.get(event_type, [])
	for field in error_fields:
		var error_check := validate_contract_error(value[field])
		if not error_check.ok:
			return error_check
	var evidence_fields: Array = {
		"agent.turn.feedback_ready": ["evidence_refs"],
		"skill.certification.rejected": ["evidence_refs"],
		"sandbox.run.completed": ["evidence_refs"],
		"sandbox.run.failed": ["evidence_refs"],
		"world.committed": ["evidence_refs"],
		"learner.evidence.recorded": ["evidence_refs"],
		"learner.model.updated": ["evidence_refs"],
	}.get(event_type, [])
	for field in evidence_fields:
		var evidence_check := _validate_evidence_refs(value[field], "RuntimeEvent.%s.payload.%s" % [event_type, field])
		if not evidence_check.ok:
			return evidence_check

	match event_type:
		"command.accepted":
			if typeof(value.command_type) != TYPE_STRING or value.command_type not in COMMAND_TYPES or value.status != "ACCEPTED":
				return _failure("command.accepted payload command_type or status is invalid")
		"command.stage_changed":
			if typeof(value.from_status) != TYPE_STRING or value.from_status not in ALL_COMMAND_STATUSES or typeof(value.to_status) != TYPE_STRING or value.to_status not in ALL_COMMAND_STATUSES:
				return _failure("command.stage_changed payload status is invalid")
			if not COMMAND_STATUS_SUCCESSORS.has(value.from_status) or value.to_status not in COMMAND_STATUS_SUCCESSORS[value.from_status]:
				return _failure("command.stage_changed payload transition %s -> %s is invalid" % [value.from_status, value.to_status])
			if not _is_integer_in_range(value.command_revision, 1) or not _is_integer_in_range(value.attempt, 1):
				return _failure("command.stage_changed payload revision or attempt is invalid")
		"command.terminal":
			if typeof(value.status) != TYPE_STRING or value.status not in TERMINAL_COMMAND_STATUSES:
				return _failure("command.terminal payload status is invalid")
			if value.status == "APPLIED":
				if not _is_non_empty_string(value.result_ref) or value.result_ref.length() > 1024 or value.error != null:
					return _failure("APPLIED command.terminal requires result_ref and no error")
			elif value.status in ["REJECTED", "FAILED", "UNKNOWN"]:
				if value.result_ref != null or not value.error is Dictionary:
					return _failure("Rejected, failed or unknown command.terminal requires an error and null result_ref")
				var terminal_error_check := validate_contract_error(value.error)
				if not terminal_error_check.ok:
					return terminal_error_check
				if value.status == "UNKNOWN" and (value.error.code != "UNKNOWN_COMMIT_STATE" or value.error.stage != "WORLD_COMMIT"):
					return _failure("UNKNOWN command.terminal must carry UNKNOWN_COMMIT_STATE at WORLD_COMMIT")
			elif value.result_ref != null:
				return _failure("CANCELLED command.terminal requires null result_ref")
			if value.error != null:
				var cancelled_error_check := validate_contract_error(value.error)
				if not cancelled_error_check.ok:
					return cancelled_error_check
		"agent.turn.feedback_ready":
			var feedback_check := validate_agent_turn_feedback(value, envelope.command_id)
			if not feedback_check.ok:
				return feedback_check
		"skill.build.requested":
			var hash_check := _validate_pattern(value.source_sha256, "^[a-f0-9]{64}$", "skill.build.requested.source_sha256")
			if not hash_check.ok:
				return hash_check
			if not _string_with_length(value.compiler_profile, 1, 64) or not _string_with_length(value.test_suite_version, 1, 96):
				return _failure("skill.build.requested profile or test suite is invalid")
		"skill.build.started":
			if not _is_integer_in_range(value.attempt, 1):
				return _failure("skill.build.started attempt must be positive")
		"skill.build.completed":
			var artifact_check := _validate_runtime_build_artifact(value.artifact)
			if not artifact_check.ok:
				return artifact_check
			if not value.tests is Array or value.tests.is_empty():
				return _failure("skill.build.completed tests must be non-empty")
			for test in value.tests:
				var test_check := _validate_runtime_test_result(test)
				if not test_check.ok:
					return test_check
		"skill.certification.granted":
			var hash_check := _validate_pattern(value.artifact_sha256, "^[a-f0-9]{64}$", "skill.certification.granted.artifact_sha256")
			if not hash_check.ok:
				return hash_check
			var capabilities_check := _validate_unique_string_array(value.capabilities, 2147483647, 64, "skill.certification.granted.capabilities")
			if not capabilities_check.ok:
				return capabilities_check
		"skill.activation.applied":
			var scope_check := _validate_runtime_activation_scope(value.activation_scope)
			if not scope_check.ok:
				return scope_check
			var hash_check := _validate_pattern(value.artifact_sha256, "^[a-f0-9]{64}$", "skill.activation.applied.artifact_sha256")
			if not hash_check.ok:
				return hash_check
			if not _is_integer_in_range(value.previous_registry_revision, 0) or not _is_integer_in_range(value.registry_revision, 1):
				return _failure("skill.activation.applied revisions are invalid")
			if value.registry_revision != value.previous_registry_revision + 1:
				return _failure("skill.activation.applied must advance exactly one registry revision")
		"skill.activation.rejected":
			var scope_check := _validate_runtime_activation_scope(value.activation_scope)
			if not scope_check.ok:
				return scope_check
			if not _is_integer_in_range(value.expected_registry_revision, 0) or not _is_integer_in_range(value.current_registry_revision, 0):
				return _failure("skill.activation.rejected revisions are invalid")
		"sandbox.run.started":
			if not _is_integer_in_range(value.expected_world_revision, 0):
				return _failure("sandbox.run.started expected_world_revision is invalid")
		"sandbox.run.completed":
			if value.exit_code != 0 or not value.action_intents is Array or value.action_intents.size() > 1000:
				return _failure("sandbox.run.completed exit_code or action_intents is invalid")
			for intent in value.action_intents:
				var intent_check := _validate_action_intent(intent)
				if not intent_check.ok:
					return intent_check
		"world.committed":
			if not _is_integer_in_range(value.previous_world_revision, 0) or not _is_integer_in_range(value.world_revision, 1):
				return _failure("world.committed revisions are invalid")
			if value.world_revision != value.previous_world_revision + 1:
				return _failure("world.committed must advance exactly one world revision")
			var hash_check := _validate_pattern(value.state_hash, "^[a-f0-9]{64}$", "world.committed.state_hash")
			if not hash_check.ok:
				return hash_check
			var ids_check := _validate_unique_identifier_array(value.applied_intent_ids, "world.committed.applied_intent_ids")
			if not ids_check.ok:
				return ids_check
		"world.rejected":
			if not _is_integer_in_range(value.expected_world_revision, 0) or not _is_integer_in_range(value.current_world_revision, 0):
				return _failure("world.rejected revisions are invalid")
			var ids_check := _validate_unique_identifier_array(value.rejected_intent_ids, "world.rejected.rejected_intent_ids")
			if not ids_check.ok:
				return ids_check
		"learner.evidence.recorded":
			var competency_check := _validate_unique_string_array(value.competency_ids, 2147483647, 128, "learner.evidence.recorded.competency_ids")
			if not competency_check.ok:
				return competency_check
		"learner.model.updated":
			if not _is_integer_in_range(value.previous_revision, 0) or not _is_integer_in_range(value.learner_revision, 1) or not _is_integer_in_range(value.projected_through_sequence, 1):
				return _failure("learner.model.updated revisions are invalid")
			if value.learner_revision != value.previous_revision + 1:
				return _failure("learner.model.updated must advance exactly one learner revision")
			var competency_check := _validate_unique_string_array(value.changed_competency_ids, 2147483647, 128, "learner.model.updated.changed_competency_ids")
			if not competency_check.ok:
				return competency_check
		"learner.projection.failed":
			var source_check := _validate_pattern(value.source_event_id, "^evt_[A-Za-z0-9_-]{8,128}$", "learner.projection.failed.source_event_id")
			if not source_check.ok:
				return source_check
		"feishu.sync.requested":
			if typeof(value.sync_kind) != TYPE_STRING or value.sync_kind not in ["TEACHER_PROJECTION", "REPORT_DRAFT", "REVIEW_CARD", "OPERATION_ALERT"]:
				return _failure("feishu.sync.requested sync_kind is invalid")
			if not _string_with_length(value.target_ref, 1, 256) or not _is_integer_in_range(value.attempt, 1):
				return _failure("feishu.sync.requested target_ref or attempt is invalid")
		"feishu.sync.succeeded":
			if not _string_with_length(value.remote_object_id, 1, 256) or not _is_integer_in_range(value.attempt, 1):
				return _failure("feishu.sync.succeeded remote_object_id or attempt is invalid")
		"feishu.sync.failed":
			if not _is_integer_in_range(value.attempt, 1):
				return _failure("feishu.sync.failed attempt is invalid")
			if value.next_attempt_at != null:
				var next_check := _validate_date_time(value.next_attempt_at, "feishu.sync.failed.next_attempt_at")
				if not next_check.ok:
					return next_check
		"feishu.sync.dead_lettered":
			if not _is_integer_in_range(value.attempts, 1):
				return _failure("feishu.sync.dead_lettered attempts is invalid")
	return _success()


static func _validate_runtime_activation_scope(value: Variant) -> Dictionary:
	var shape := _require_shape(value, ["world_id", "agent_profile_id"], [], "RuntimeEvent.activation_scope")
	if not shape.ok:
		return shape
	for field in ["world_id", "agent_profile_id"]:
		var id_check := validate_identifier(value[field], "RuntimeEvent.activation_scope.%s" % field)
		if not id_check.ok:
			return id_check
	return _success()


static func _validate_runtime_build_artifact(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"artifact_sha256", "source_sha256", "compiler_profile", "compiler_version",
		"sandbox_image_digest", "test_suite_version", "artifact_uri",
	], [], "RuntimeEvent.BuildArtifact")
	if not shape.ok:
		return shape
	for field in ["artifact_sha256", "source_sha256"]:
		var hash_check := _validate_pattern(value[field], "^[a-f0-9]{64}$", "RuntimeEvent.BuildArtifact.%s" % field)
		if not hash_check.ok:
			return hash_check
	var limits := {"compiler_profile": 64, "compiler_version": 96, "sandbox_image_digest": 256, "test_suite_version": 96, "artifact_uri": 1024}
	for field in limits:
		if not _string_with_length(value[field], 1, limits[field]):
			return _failure("RuntimeEvent.BuildArtifact.%s has an invalid length" % field)
	return _success()


static func _validate_runtime_test_result(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"test_case_id", "visibility", "status", "duration_ms", "diagnostic_codes", "evidence_refs",
	], [], "RuntimeEvent.TestCaseResult")
	if not shape.ok:
		return shape
	if not _string_with_length(value.test_case_id, 1, 128):
		return _failure("RuntimeEvent.TestCaseResult.test_case_id is invalid")
	if typeof(value.visibility) != TYPE_STRING or value.visibility not in ["PUBLIC", "HIDDEN"]:
		return _failure("RuntimeEvent.TestCaseResult.visibility is invalid")
	if typeof(value.status) != TYPE_STRING or value.status not in ["PASSED", "FAILED", "ERROR", "TIMEOUT"]:
		return _failure("RuntimeEvent.TestCaseResult.status is invalid")
	if not _is_integer_in_range(value.duration_ms, 0):
		return _failure("RuntimeEvent.TestCaseResult.duration_ms is invalid")
	if not value.diagnostic_codes is Array:
		return _failure("RuntimeEvent.TestCaseResult.diagnostic_codes must be an Array")
	for code in value.diagnostic_codes:
		if typeof(code) != TYPE_STRING:
			return _failure("RuntimeEvent.TestCaseResult.diagnostic_codes must contain strings")
	return _validate_evidence_refs(value.evidence_refs, "RuntimeEvent.TestCaseResult.evidence_refs")


static func _validate_evidence_refs(value: Variant, label: String) -> Dictionary:
	if not value is Array or value.size() > 64:
		return _failure("%s must be an Array with at most 64 items" % label)
	var seen_ids := {}
	for ref in value:
		var ref_check := _validate_evidence_ref(ref)
		if not ref_check.ok:
			return ref_check
		if ref.evidence_id in seen_ids:
			return _failure("%s must contain unique evidence_id values" % label)
		seen_ids[ref.evidence_id] = true
	return _success()


static func _evidence_refs_signature(value: Array) -> Array[String]:
	var result: Array[String] = []
	for ref in value:
		result.append(JSON.stringify([
			ref.evidence_id,
			ref.evidence_type,
			ref.created_at,
			ref.get("sha256", null),
			ref.get("uri", null),
		]))
	result.sort()
	return result


static func _validate_unique_identifier_array(value: Variant, label: String) -> Dictionary:
	if not value is Array:
		return _failure("%s must be an Array" % label)
	var seen := {}
	for identifier in value:
		var id_check := validate_identifier(identifier, "%s[]" % label)
		if not id_check.ok or identifier in seen:
			return _failure("%s contains an invalid or duplicate identifier" % label)
		seen[identifier] = true
	return _success()


static func _string_with_length(value: Variant, minimum: int, maximum: int) -> bool:
	return typeof(value) == TYPE_STRING and value.length() >= minimum and value.length() <= maximum


static func canonical_json_sha256_v1(value: Variant) -> String:
	var canonical := _canonical_json_v1(value)
	return "" if canonical.is_empty() else canonical.sha256_text()


static func _canonical_json_v1(value: Variant) -> String:
	match typeof(value):
		TYPE_NIL:
			return "null"
		TYPE_BOOL:
			return "true" if value else "false"
		TYPE_STRING:
			if not _contains_only_unicode_scalars(value):
				return ""
			return JSON.stringify(value)
		TYPE_INT:
			if value < -9007199254740991 or value > 9007199254740991:
				return ""
			return String.num_int64(value)
		TYPE_FLOAT:
			if not is_finite(value) or value != floor(value) or value < -9007199254740991.0 or value > 9007199254740991.0:
				return ""
			return String.num(value, 0)
		TYPE_ARRAY:
			var array_parts: PackedStringArray = []
			for item in value:
				var encoded_item := _canonical_json_v1(item)
				if encoded_item.is_empty():
					return ""
				array_parts.append(encoded_item)
			return "[%s]" % ",".join(array_parts)
		TYPE_DICTIONARY:
			var keys: Array[String] = []
			for key in value:
				if typeof(key) != TYPE_STRING:
					return ""
				if not _contains_only_unicode_scalars(key):
					return ""
				keys.append(key)
			keys.sort()
			var object_parts: PackedStringArray = []
			for key in keys:
				var encoded_value := _canonical_json_v1(value[key])
				if encoded_value.is_empty():
					return ""
				object_parts.append("%s:%s" % [JSON.stringify(key), encoded_value])
			return "{%s}" % ",".join(object_parts)
		_:
			return ""


static func _contains_only_unicode_scalars(value: String) -> bool:
	for index in range(value.length()):
		var codepoint := value.unicode_at(index)
		if codepoint >= 0xD800 and codepoint <= 0xDFFF:
			return false
	return true


static func _validate_actor(value: Variant) -> Dictionary:
	var shape := _require_shape(value, ["tenant_id", "actor_id", "actor_type", "roles"], [], "ActorRef")
	if not shape.ok:
		return shape
	for check in [
		_validate_pattern(value.tenant_id, "^[A-Za-z0-9_-]{3,96}$", "ActorRef.tenant_id"),
		_validate_pattern(value.actor_id, "^[A-Za-z0-9_-]{3,128}$", "ActorRef.actor_id"),
	]:
		if not check.ok:
			return check
	if typeof(value.actor_type) != TYPE_STRING or value.actor_type not in ACTOR_TYPES:
		return _failure("ActorRef.actor_type is invalid")
	if not value.roles is Array or value.roles.size() > 16:
		return _failure("ActorRef.roles must be an Array with at most 16 items")
	var seen := {}
	for role in value.roles:
		var role_check := _validate_pattern(role, "^[a-z][a-z0-9:_-]{1,63}$", "ActorRef.roles[]")
		if not role_check.ok or role in seen:
			return _failure("ActorRef.roles contains an invalid or duplicate role")
		seen[role] = true
	return _success()


static func _validate_content_ref(value: Variant) -> Dictionary:
	var shape := _require_shape(value, ["unit_id", "version", "content_hash"], [], "ContentRef")
	if not shape.ok:
		return shape
	for check in [
		_validate_pattern(value.unit_id, "^[A-Z0-9][A-Z0-9_-]{2,79}$", "ContentRef.unit_id"),
		_validate_pattern(value.version, "^[0-9]+\\.[0-9]+\\.[0-9]+(-[0-9A-Za-z.-]+)?$", "ContentRef.version"),
		_validate_pattern(value.content_hash, "^[a-f0-9]{64}$", "ContentRef.content_hash"),
	]:
		if not check.ok:
			return check
	return _success()


static func _validate_version_set(value: Variant) -> Dictionary:
	var shape := _require_shape(value, VERSION_REQUIRED, VERSION_OPTIONAL, "VersionSet")
	if not shape.ok:
		return shape
	for field in VERSION_MAX_LENGTHS:
		if value.has(field) and not _string_with_length(value[field], 1, VERSION_MAX_LENGTHS[field]):
			return _failure("VersionSet.%s length is invalid" % field)
	if value.has("artifact_sha256"):
		return _validate_pattern(value.artifact_sha256, "^[a-f0-9]{64}$", "VersionSet.artifact_sha256")
	return _success()


static func _validate_evidence_ref(value: Variant) -> Dictionary:
	var shape := _require_shape(value, ["evidence_id", "evidence_type", "created_at"], ["sha256", "uri"], "EvidenceRef")
	if not shape.ok:
		return shape
	for check in [
		_validate_pattern(value.evidence_id, "^evidence_[A-Za-z0-9_-]{8,128}$", "EvidenceRef.evidence_id"),
		_validate_date_time(value.created_at, "EvidenceRef.created_at"),
	]:
		if not check.ok:
			return check
	if typeof(value.evidence_type) != TYPE_STRING or value.evidence_type not in EVIDENCE_TYPES:
		return _failure("EvidenceRef.evidence_type is invalid")
	if value.has("sha256"):
		var hash_check := _validate_pattern(value.sha256, "^[a-f0-9]{64}$", "EvidenceRef.sha256")
		if not hash_check.ok:
			return hash_check
	if value.has("uri") and not _string_with_length(value.uri, 1, 1024):
		return _failure("EvidenceRef.uri must contain 1 to 1024 characters")
	return _success()


static func _validate_evidence_payload(value: Variant) -> Dictionary:
	if not value is Dictionary or not value.has("evidence_kind") or typeof(value.evidence_kind) != TYPE_STRING:
		return _failure("Evidence.payload must contain evidence_kind")
	match value.evidence_kind:
		"BUILD_CERTIFICATION":
			var shape := _require_shape(value, [
				"evidence_kind", "build_id", "skill_id", "skill_version_id", "artifact_sha256",
				"test_suite_version", "outcome",
			], [], "BuildEvidence")
			if not shape.ok:
				return shape
			for field in ["build_id", "skill_id", "skill_version_id"]:
				var id_check := validate_identifier(value[field], "BuildEvidence.%s" % field)
				if not id_check.ok:
					return id_check
			var hash_check := _validate_pattern(value.artifact_sha256, "^[a-f0-9]{64}$", "BuildEvidence.artifact_sha256")
			if not hash_check.ok:
				return hash_check
			if not _is_non_empty_string(value.test_suite_version) or value.test_suite_version.length() > 96:
				return _failure("BuildEvidence.test_suite_version is invalid")
			if typeof(value.outcome) != TYPE_STRING or value.outcome not in ["CERTIFIED", "REJECTED"]:
				return _failure("BuildEvidence.outcome is invalid")
		"SKILL_RUN":
			var shape := _require_shape(value, ["evidence_kind", "run_id", "sandbox_status", "world_status", "intent_count"], [], "RunEvidence")
			if not shape.ok:
				return shape
			var run_check := validate_identifier(value.run_id, "RunEvidence.run_id")
			if not run_check.ok:
				return run_check
			if typeof(value.sandbox_status) != TYPE_STRING or value.sandbox_status not in ["SUCCEEDED", "REJECTED", "TIMED_OUT", "FAILED"]:
				return _failure("RunEvidence.sandbox_status is invalid")
			if typeof(value.world_status) != TYPE_STRING or value.world_status not in ["NOT_ATTEMPTED", "COMMITTED", "REJECTED", "FAILED", "UNKNOWN"]:
				return _failure("RunEvidence.world_status is invalid")
			if not _is_integer_in_range(value.intent_count, 0):
				return _failure("RunEvidence.intent_count must be non-negative")
		"WORLD_COMMIT":
			var shape := _require_shape(value, [
				"evidence_kind", "world_id", "previous_revision", "world_revision",
				"first_event_sequence", "last_event_sequence", "state_hash",
			], [], "WorldCommitEvidence")
			if not shape.ok:
				return shape
			var world_check := validate_identifier(value.world_id, "WorldCommitEvidence.world_id")
			if not world_check.ok:
				return world_check
			if not _is_integer_in_range(value.previous_revision, 0) or not _is_integer_in_range(value.world_revision, 1):
				return _failure("WorldCommitEvidence revisions are invalid")
			if value.world_revision != value.previous_revision + 1:
				return _failure("WorldCommitEvidence must advance exactly one world revision")
			if not _is_integer_in_range(value.first_event_sequence, 1) or not _is_integer_in_range(value.last_event_sequence, 1) or value.first_event_sequence > value.last_event_sequence:
				return _failure("WorldCommitEvidence event range is invalid")
			return _validate_pattern(value.state_hash, "^[a-f0-9]{64}$", "WorldCommitEvidence.state_hash")
		"LEARNER_OBSERVATION":
			var shape := _require_shape(value, ["evidence_kind", "observation_type", "task_id", "outcome", "assistance_level"], [], "LearnerObservationEvidence")
			if not shape.ok:
				return shape
			if typeof(value.observation_type) != TYPE_STRING or value.observation_type not in ["CODE_ATTEMPT", "DEBUG_ATTEMPT", "TASK_COMPLETION", "HINT_USE"]:
				return _failure("LearnerObservationEvidence.observation_type is invalid")
			var task_check := validate_identifier(value.task_id, "LearnerObservationEvidence.task_id")
			if not task_check.ok:
				return task_check
			if typeof(value.outcome) != TYPE_STRING or value.outcome not in ["SUCCESS", "PARTIAL", "FAILED"]:
				return _failure("LearnerObservationEvidence.outcome is invalid")
			if not _is_integer_in_range(value.assistance_level, 0, 10):
				return _failure("LearnerObservationEvidence.assistance_level is invalid")
		_:
			return _failure("Evidence.payload.evidence_kind is unknown")
	return _success()


static func _validate_unique_string_array(value: Variant, max_items: int, max_length: int, label: String) -> Dictionary:
	if not value is Array or value.size() > max_items:
		return _failure("%s must be an Array with at most %s items" % [label, max_items])
	var seen := {}
	for item in value:
		if typeof(item) != TYPE_STRING or item.is_empty() or item.length() > max_length:
			return _failure("%s contains invalid text" % label)
		if item in seen:
			return _failure("%s contains duplicates" % label)
		seen[item] = true
	return _success()


static func _validate_world_state(value: Variant) -> Dictionary:
	var shape := _require_shape(value, ["clock", "avatar", "inventory", "plots", "agents"], [], "WorldState")
	if not shape.ok:
		return shape
	var clock_shape := _require_shape(value.clock, ["day", "minute_of_day", "tick"], [], "WorldState.clock")
	if not clock_shape.ok:
		return clock_shape
	if not _is_integer_in_range(value.clock.day, 1) or not _is_integer_in_range(value.clock.minute_of_day, 0, 1439) or not _is_integer_in_range(value.clock.tick, 0):
		return _failure("WorldState.clock contains an invalid integer")
	var avatar_shape := _require_shape(value.avatar, ["entity_id", "position", "energy"], [], "WorldState.avatar")
	if not avatar_shape.ok:
		return avatar_shape
	for check in [
		validate_identifier(value.avatar.entity_id, "WorldState.avatar.entity_id"),
		_validate_position(value.avatar.position, "WorldState.avatar.position"),
	]:
		if not check.ok:
			return check
	if not _is_integer_in_range(value.avatar.energy, 0, 10000):
		return _failure("WorldState.avatar.energy must be an integer from 0 to 10000")
	if not value.inventory is Array or value.inventory.size() > 1000:
		return _failure("WorldState.inventory must be an Array with at most 1000 items")
	for item in value.inventory:
		var item_shape := _require_shape(item, ["item_id", "quantity"], [], "InventoryItem")
		if not item_shape.ok:
			return item_shape
		var item_id_check := _validate_pattern(item.item_id, "^[a-z][a-z0-9_.-]{1,63}$", "InventoryItem.item_id")
		if not item_id_check.ok or not _is_integer_in_range(item.quantity, 0, 1000000):
			return _failure("InventoryItem is invalid")
	if not value.plots is Array or value.plots.size() > 10000:
		return _failure("WorldState.plots must be an Array with at most 10000 items")
	for plot in value.plots:
		var plot_check := _validate_plot(plot)
		if not plot_check.ok:
			return plot_check
	if not value.agents is Array or value.agents.size() > 256:
		return _failure("WorldState.agents must be an Array with at most 256 items")
	for agent in value.agents:
		var agent_check := _validate_world_agent(agent)
		if not agent_check.ok:
			return agent_check
	return _success()


static func _validate_run_sandbox(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"invocation_id", "status", "started_at", "finished_at", "limits", "usage", "action_intents", "failure",
	], [], "Run.sandbox")
	if not shape.ok:
		return shape
	var invocation_check := validate_identifier(value.invocation_id, "Run.sandbox.invocation_id")
	if not invocation_check.ok:
		return invocation_check
	if typeof(value.status) != TYPE_STRING or value.status not in ["QUEUED", "RUNNING", "SUCCEEDED", "REJECTED", "TIMED_OUT", "FAILED"]:
		return _failure("Run.sandbox.status is invalid")
	for field in ["started_at", "finished_at"]:
		if value[field] != null:
			var date_check := _validate_date_time(value[field], "Run.sandbox.%s" % field)
			if not date_check.ok:
				return date_check
	var limits_shape := _require_shape(value.limits, ["cpu_ms", "wall_ms", "memory_bytes", "max_intents"], [], "Run.sandbox.limits")
	if not limits_shape.ok:
		return limits_shape
	for field in value.limits:
		if not _is_integer_in_range(value.limits[field], 1):
			return _failure("Run.sandbox.limits.%s must be positive" % field)
	if value.usage != null:
		var usage_shape := _require_shape(value.usage, ["cpu_ms", "wall_ms", "peak_memory_bytes"], [], "Run.sandbox.usage")
		if not usage_shape.ok:
			return usage_shape
		for field in value.usage:
			if not _is_integer_in_range(value.usage[field], 0):
				return _failure("Run.sandbox.usage.%s must be non-negative" % field)
	if not value.action_intents is Array:
		return _failure("Run.sandbox.action_intents must be an Array")
	for intent in value.action_intents:
		var intent_check := _validate_action_intent(intent)
		if not intent_check.ok:
			return intent_check
	if value.failure != null:
		var failure_check := validate_contract_error(value.failure)
		if not failure_check.ok:
			return failure_check
		if value.failure.stage != "SANDBOX":
			return _failure("Run.sandbox.failure.stage must be SANDBOX")
	match value.status:
		"QUEUED":
			if value.started_at != null or value.finished_at != null or value.usage != null or not value.action_intents.is_empty() or value.failure != null:
				return _failure("QUEUED sandbox contains execution output")
		"RUNNING":
			if value.started_at == null or value.finished_at != null or value.failure != null:
				return _failure("RUNNING sandbox timestamps/failure are contradictory")
		"SUCCEEDED":
			if value.started_at == null or value.finished_at == null or value.usage == null or value.failure != null:
				return _failure("SUCCEEDED sandbox requires timestamps and usage without failure")
		"REJECTED", "TIMED_OUT", "FAILED":
			if value.finished_at == null or value.failure == null:
				return _failure("Failed sandbox state requires finished_at and failure")
	return _success()


static func _validate_run_world_application(value: Variant) -> Dictionary:
	var shape := _require_shape(value, ["status", "receipt", "failure"], [], "Run.world_application")
	if not shape.ok:
		return shape
	if typeof(value.status) != TYPE_STRING or value.status not in ["NOT_ATTEMPTED", "VALIDATING", "COMMITTED", "REJECTED", "FAILED", "UNKNOWN"]:
		return _failure("Run.world_application.status is invalid")
	if value.receipt != null:
		var receipt_check := _validate_world_receipt(value.receipt)
		if not receipt_check.ok:
			return receipt_check
	if value.failure != null:
		var failure_check := validate_contract_error(value.failure)
		if not failure_check.ok:
			return failure_check
		if value.failure.stage not in ["WORLD_VALIDATE", "WORLD_COMMIT"]:
			return _failure("Run.world_application.failure stage is invalid")
	if value.status in ["NOT_ATTEMPTED", "VALIDATING"] and (value.receipt != null or value.failure != null):
		return _failure("Pending world application cannot contain receipt or failure")
	if value.status == "COMMITTED" and (value.receipt == null or value.failure != null):
		return _failure("COMMITTED world application requires receipt without failure")
	if value.status in ["REJECTED", "FAILED", "UNKNOWN"] and (value.receipt != null or value.failure == null):
		return _failure("Failed/unknown world application requires failure without receipt")
	return _success()


static func _validate_world_receipt(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"world_id", "previous_revision", "world_revision", "first_event_sequence",
		"last_event_sequence", "state_hash", "committed_at",
	], [], "WorldReceipt")
	if not shape.ok:
		return shape
	for check in [
		validate_identifier(value.world_id, "WorldReceipt.world_id"),
		_validate_pattern(value.state_hash, "^[a-f0-9]{64}$", "WorldReceipt.state_hash"),
		_validate_date_time(value.committed_at, "WorldReceipt.committed_at"),
	]:
		if not check.ok:
			return check
	if not _is_integer_in_range(value.previous_revision, 0) or not _is_integer_in_range(value.world_revision, 1):
		return _failure("WorldReceipt revisions are invalid")
	if value.world_revision != value.previous_revision + 1:
		return _failure("WorldReceipt must advance exactly one world revision")
	if not _is_integer_in_range(value.first_event_sequence, 1) or not _is_integer_in_range(value.last_event_sequence, 1) or value.first_event_sequence > value.last_event_sequence:
		return _failure("WorldReceipt event sequence range is invalid")
	return _success()


static func _validate_action_intent(value: Variant) -> Dictionary:
	if not value is Dictionary or not value.has("action_type") or typeof(value.action_type) != TYPE_STRING:
		return _failure("ActionIntent must contain action_type")
	var required: Array = ["intent_id", "action_type", "actor_entity_id", "expected_world_revision"]
	match value.action_type:
		"MOVE": required += ["destination"]
		"PLANT": required += ["plot_id", "crop_type"]
		"WATER": required += ["plot_id", "amount_ml"]
		"HARVEST": required += ["plot_id"]
		"INTERACT": required += ["target_entity_id", "interaction"]
		"SPEAK": required += ["text", "audience"]
		_: return _failure("ActionIntent.action_type is unknown")
	var shape := _require_shape(value, required, [], "ActionIntent")
	if not shape.ok:
		return shape
	for field in ["intent_id", "actor_entity_id"]:
		var id_check := validate_identifier(value[field], "ActionIntent.%s" % field)
		if not id_check.ok:
			return id_check
	if not _is_integer_in_range(value.expected_world_revision, 0):
		return _failure("ActionIntent.expected_world_revision must be non-negative")
	match value.action_type:
		"MOVE": return _validate_position(value.destination, "ActionIntent.destination")
		"PLANT":
			for field in ["plot_id"]:
				var id_check := validate_identifier(value[field], "ActionIntent.%s" % field)
				if not id_check.ok: return id_check
			return _validate_pattern(value.crop_type, "^[a-z][a-z0-9_.-]{1,63}$", "ActionIntent.crop_type")
		"WATER":
			var id_check := validate_identifier(value.plot_id, "ActionIntent.plot_id")
			if not id_check.ok: return id_check
			if not _is_integer_in_range(value.amount_ml, 1, 10000): return _failure("ActionIntent.amount_ml is invalid")
		"HARVEST": return validate_identifier(value.plot_id, "ActionIntent.plot_id")
		"INTERACT":
			var id_check := validate_identifier(value.target_entity_id, "ActionIntent.target_entity_id")
			if not id_check.ok: return id_check
			return _validate_pattern(value.interaction, "^[a-z][a-z0-9_.-]{1,63}$", "ActionIntent.interaction")
		"SPEAK":
			if not _is_non_empty_string(value.text) or value.text.length() > 500: return _failure("ActionIntent.text is invalid")
			if typeof(value.audience) != TYPE_STRING or value.audience not in ["LEARNER", "NEARBY_ENTITIES"]: return _failure("ActionIntent.audience is invalid")
	return _success()


static func _validate_plot(value: Variant) -> Dictionary:
	var shape := _require_shape(value, [
		"plot_id", "position", "soil_state", "hydration", "crop", "last_updated_event_sequence",
	], [], "WorldPlot")
	if not shape.ok:
		return shape
	for check in [
		validate_identifier(value.plot_id, "WorldPlot.plot_id"),
		_validate_position(value.position, "WorldPlot.position"),
	]:
		if not check.ok:
			return check
	if typeof(value.soil_state) != TYPE_STRING or value.soil_state not in ["UNTILLED", "TILLED"]:
		return _failure("WorldPlot.soil_state is invalid")
	if not _is_integer_in_range(value.hydration, 0, 10000) or not _is_integer_in_range(value.last_updated_event_sequence, 0):
		return _failure("WorldPlot hydration or event sequence is invalid")
	if value.crop != null:
		var crop_shape := _require_shape(value.crop, ["crop_type", "growth_stage", "planted_at_tick", "ready_to_harvest"], [], "WorldCrop")
		if not crop_shape.ok:
			return crop_shape
		var crop_type_check := _validate_pattern(value.crop.crop_type, "^[a-z][a-z0-9_.-]{1,63}$", "WorldCrop.crop_type")
		if not crop_type_check.ok or not _is_integer_in_range(value.crop.growth_stage, 0, 100) or not _is_integer_in_range(value.crop.planted_at_tick, 0) or typeof(value.crop.ready_to_harvest) != TYPE_BOOL:
			return _failure("WorldCrop contains an invalid value")
	return _success()


static func _validate_world_agent(value: Variant) -> Dictionary:
	var shape := _require_shape(value, ["entity_id", "agent_profile_id", "position", "activity"], [], "WorldAgent")
	if not shape.ok:
		return shape
	for check in [
		validate_identifier(value.entity_id, "WorldAgent.entity_id"),
		validate_identifier(value.agent_profile_id, "WorldAgent.agent_profile_id"),
		_validate_position(value.position, "WorldAgent.position"),
	]:
		if not check.ok:
			return check
	if typeof(value.activity) != TYPE_STRING or value.activity not in ["IDLE", "THINKING", "EXECUTING", "BLOCKED"]:
		return _failure("WorldAgent.activity is invalid")
	return _success()


static func _validate_position(value: Variant, label: String) -> Dictionary:
	var shape := _require_shape(value, ["x", "y"], [], label)
	if not shape.ok:
		return shape
	if not _is_integer_in_range(value.x, -100000, 100000) or not _is_integer_in_range(value.y, -100000, 100000):
		return _failure("%s coordinates must be integers in range" % label)
	return _success()


static func _validate_links(value: Variant, required: Array, optional: Array, label: String) -> Dictionary:
	var shape := _require_shape(value, required, optional, label)
	if not shape.ok:
		return shape
	for field in required + optional:
		if value.has(field) and (
			not _string_with_length(value[field], 1, 2048)
			or not _is_rfc3986_reference(value[field], false)
		):
			return _failure("%s.%s must be a non-empty URI reference" % [label, field])
	return _success()


static func _is_rfc3986_component(value: String, extra_characters: String = "", allow_percent_encoding: bool = true) -> bool:
	var index := 0
	while index < value.length():
		var character := value.substr(index, 1)
		if character == "%":
			if (
				not allow_percent_encoding
				or index + 2 >= value.length()
				or not _is_hex_pair(value.substr(index + 1, 2))
			):
				return false
			index += 3
			continue
		if (
			URI_UNRESERVED.find(character) < 0
			and URI_SUB_DELIMITERS.find(character) < 0
			and extra_characters.find(character) < 0
		):
			return false
		index += 1
	return true


static func _is_hex_pair(value: String) -> bool:
	if value.length() != 2:
		return false
	for index in range(2):
		if "0123456789abcdefABCDEF".find(value.substr(index, 1)) < 0:
			return false
	return true


static func _is_ascii_digits(value: String) -> bool:
	for index in range(value.length()):
		if "0123456789".find(value.substr(index, 1)) < 0:
			return false
	return true


static func _is_rfc3986_ip_literal(value: String) -> bool:
	if value.contains(":") and value.is_valid_ip_address():
		return true
	if value.length() < 4 or value.substr(0, 1).to_lower() != "v":
		return false
	var dot_index := value.find(".")
	if dot_index < 2:
		return false
	var version := value.substr(1, dot_index - 1)
	if not _is_rfc3986_component(version, "", false):
		return false
	for character_index in range(version.length()):
		if "0123456789abcdefABCDEF".find(version.substr(character_index, 1)) < 0:
			return false
	var address := value.substr(dot_index + 1)
	return not address.is_empty() and _is_rfc3986_component(address, ":", false)


static func _is_rfc3986_authority(value: String) -> bool:
	var first_at := value.find("@")
	var last_at := value.rfind("@")
	if first_at != last_at:
		return false
	var host_and_port := value
	if last_at >= 0:
		if not _is_rfc3986_component(value.substr(0, last_at), ":"):
			return false
		host_and_port = value.substr(last_at + 1)
	if host_and_port.begins_with("["):
		var closing_bracket := host_and_port.find("]")
		if closing_bracket < 0 or not _is_rfc3986_ip_literal(host_and_port.substr(1, closing_bracket - 1)):
			return false
		var suffix := host_and_port.substr(closing_bracket + 1)
		if suffix.is_empty():
			return true
		if not suffix.begins_with(":"):
			return false
		var port := suffix.substr(1)
		return port.is_empty() or _is_ascii_digits(port)
	if host_and_port.contains("[") or host_and_port.contains("]"):
		return false
	var first_colon := host_and_port.find(":")
	var last_colon := host_and_port.rfind(":")
	if first_colon != last_colon:
		return false
	var host := host_and_port if last_colon < 0 else host_and_port.substr(0, last_colon)
	var port := "" if last_colon < 0 else host_and_port.substr(last_colon + 1)
	return (
		_is_rfc3986_component(host)
		and (last_colon < 0 or port.is_empty() or _is_ascii_digits(port))
	)


static func _is_rfc3986_path(value: String, absolute_uri: bool) -> bool:
	if value.begins_with("//"):
		var path_index := value.find("/", 2)
		var authority := value.substr(2) if path_index < 0 else value.substr(2, path_index - 2)
		var path := "" if path_index < 0 else value.substr(path_index)
		return _is_rfc3986_authority(authority) and _is_rfc3986_component(path, ":@/")
	if value.is_empty() or value.begins_with("/"):
		return _is_rfc3986_component(value, ":@/")
	if not _is_rfc3986_component(value, ":@/"):
		return false
	if absolute_uri:
		return true
	var slash_index := value.find("/")
	var first_segment := value if slash_index < 0 else value.substr(0, slash_index)
	return not first_segment.is_empty() and _is_rfc3986_component(first_segment, "@")


static func _is_rfc3986_reference(value: Variant, require_scheme: bool) -> bool:
	if typeof(value) != TYPE_STRING:
		return false
	var text: String = value
	var hash_index := text.find("#")
	if hash_index >= 0 and text.find("#", hash_index + 1) >= 0:
		return false
	var fragment := "" if hash_index < 0 else text.substr(hash_index + 1)
	var without_fragment := text if hash_index < 0 else text.substr(0, hash_index)
	var query_index := without_fragment.find("?")
	var query := "" if query_index < 0 else without_fragment.substr(query_index + 1)
	var path_and_authority := without_fragment if query_index < 0 else without_fragment.substr(0, query_index)
	if query_index >= 0 and not _is_rfc3986_component(query, ":@/?"):
		return false
	if hash_index >= 0 and not _is_rfc3986_component(fragment, ":@/?"):
		return false
	var scheme_regex := RegEx.new()
	if scheme_regex.compile("^([A-Za-z][A-Za-z0-9+.-]*):") != OK:
		return false
	var scheme_match := scheme_regex.search(path_and_authority)
	if require_scheme and scheme_match == null:
		return false
	var path := path_and_authority
	if scheme_match != null:
		path = path_and_authority.substr(scheme_match.get_end(0))
	return _is_rfc3986_path(path, scheme_match != null)


static func _require_shape(value: Variant, required: Array, optional: Array, contract_name: String) -> Dictionary:
	if not value is Dictionary:
		return _failure("%s must be a Dictionary" % contract_name)
	var allowed := {}
	for field in required + optional:
		allowed[field] = true
	var missing: Array[String] = []
	for field in required:
		if not value.has(field):
			missing.append(String(field))
	if not missing.is_empty():
		return _failure("%s is missing required fields: %s" % [contract_name, ", ".join(missing)])
	var unexpected: Array[String] = []
	for field in value.keys():
		if not allowed.has(field):
			unexpected.append(String(field))
	if not unexpected.is_empty():
		return _failure("%s contains unknown fields: %s" % [contract_name, ", ".join(unexpected)])
	return _success()


static func _validate_pattern(value: Variant, pattern: String, label: String) -> Dictionary:
	if typeof(value) != TYPE_STRING:
		return _failure("%s must be a string" % label)
	var regex := RegEx.new()
	if regex.compile(pattern) != OK or regex.search(value) == null:
		return _failure("%s has an invalid format" % label)
	return _success()


static func _validate_date_time(value: Variant, label: String) -> Dictionary:
	var format_check := _validate_pattern(value, "^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})$", label)
	if not format_check.ok:
		return format_check

	var text: String = value
	var year := text.substr(0, 4).to_int()
	var month := text.substr(5, 2).to_int()
	var day := text.substr(8, 2).to_int()
	var hour := text.substr(11, 2).to_int()
	var minute := text.substr(14, 2).to_int()
	var second := text.substr(17, 2).to_int()
	if year < 1 or month < 1 or month > 12:
		return _failure("%s has an invalid calendar date" % label)
	var days_in_month: Array[int] = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
	if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
		days_in_month[1] = 29
	if day < 1 or day > days_in_month[month - 1]:
		return _failure("%s has an invalid calendar date" % label)
	if hour > 23 or minute > 59 or second > 59:
		return _failure("%s has an invalid time of day" % label)

	var zone_index := text.find("Z", 19)
	if zone_index == -1:
		zone_index = text.find("z", 19)
	if zone_index == -1:
		zone_index = text.find("+", 19)
	if zone_index == -1:
		zone_index = text.find("-", 19)
	if zone_index >= 0 and text.substr(zone_index, 1).to_upper() != "Z":
		var offset_hour := text.substr(zone_index + 1, 2).to_int()
		var offset_minute := text.substr(zone_index + 4, 2).to_int()
		if offset_hour > 23 or offset_minute > 59:
			return _failure("%s has an invalid UTC offset" % label)
	return _success()


static func _is_non_empty_string(value: Variant) -> bool:
	return typeof(value) == TYPE_STRING and not value.is_empty()


static func _is_integer_in_range(value: Variant, minimum: int, maximum: int = 9223372036854775807) -> bool:
	if typeof(value) == TYPE_INT:
		return value >= minimum and value <= maximum
	# Godot's JSON parser represents JSON numbers as float. Accept only finite,
	# mathematically integral values; strings and fractional values still fail.
	if typeof(value) == TYPE_FLOAT:
		return is_finite(value) and value == floor(value) and value >= minimum and value <= maximum
	return false


static func _sequence_gap(expected_sequence: Variant, actual_sequence: Variant) -> Dictionary:
	return {
		"ok": false,
		"error": {
			"scope": "CLIENT_LOCAL",
			"code": "LOCAL_EVENT_SEQUENCE_GAP",
			"category": "CONCURRENCY",
			"retryable": true,
			"expected_sequence": expected_sequence,
			"actual_sequence": actual_sequence,
			"message": "Event page must be discarded and a fresh snapshot fetched.",
		},
	}


static func _success() -> Dictionary:
	return {"ok": true, "error": null}


static func _failure(message: String) -> Dictionary:
	return {
		"ok": false,
		"error": {
			"scope": "CLIENT_LOCAL",
			"code": "LOCAL_CONTRACT_RESPONSE_INVALID",
			"category": "VALIDATION",
			"retryable": false,
			"message": message,
		},
	}
