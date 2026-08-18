class_name ProductCapabilityGateway
extends RefCounted

## Additive v0.6 read client for rollout/capability authority. The older
## Product interaction and byte-pinned Game clients keep their wire semantics.

const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const PAGE_FIELDS := [
	"request_context", "api_version", "contract_version",
	"world_presentation_enabled", "skill_patch_enabled",
	"skill_patch_constraints",
]
const CONSTRAINT_FIELDS := [
	"request_mode", "selection_target", "agent_role", "scenario", "required_hint_level",
	"operation", "target", "max_files", "max_operations",
	"requires_failed_evidence", "cas_required", "requires_student_confirmation",
	"auto_build", "auto_activate", "auto_run",
]

var _transport: RefCounted


func _init(transport: RefCounted) -> void:
	_transport = transport


func get_product_capabilities(
	attempt_context: Dictionary,
	expected_actor: Dictionary,
	expected_content_ref: Dictionary,
) -> Dictionary:
	if not _valid_attempt(attempt_context):
		return _failure("PRODUCT_CAPABILITY_REQUEST_INVALID", "Product capability attempt identity is invalid.")
	if _transport == null or not _transport.has_method("execute"):
		return _failure("PRODUCT_CAPABILITY_TRANSPORT_UNAVAILABLE", "Product capability transport is unavailable.")
	var result: Variant = await _transport.execute("get_product_capabilities", {
		"attempt_context": attempt_context.duplicate(true),
	})
	if not result is Dictionary or typeof(result.get("ok")) != TYPE_BOOL:
		return _failure("PRODUCT_CAPABILITY_RESPONSE_INVALID", "Product capability transport returned an invalid result union.")
	if not result.ok:
		return result if result.get("error") is Dictionary else _failure(
			"PRODUCT_CAPABILITY_RESPONSE_INVALID", "Product capability transport failure is not structured.",
		)
	if int(result.get("status", 0)) != 200 or not result.get("value") is Dictionary:
		return _failure("PRODUCT_CAPABILITY_RESPONSE_INVALID", "Product capability read must return one 200 object.")
	var validation := validate_capabilities(result.value, expected_actor, expected_content_ref)
	if not validation.ok:
		return _failure(str(validation.code), str(validation.message))
	if not _matching_attempt_headers(result.get("headers"), attempt_context):
		return _failure("PRODUCT_CAPABILITY_HEADERS_INVALID", "Product capability attempt headers are missing or mismatched.")
	return {
		"ok": true, "status": 200,
		"headers": result.get("headers", {}).duplicate(true),
		"value": result.value.duplicate(true),
	}


static func validate_capabilities(
	value: Variant,
	expected_actor: Dictionary,
	expected_content_ref: Dictionary,
) -> Dictionary:
	if not _closed(value, PAGE_FIELDS):
		return _invalid("PRODUCT_CAPABILITY_SHAPE_INVALID", "Product capability response is not closed.")
	var capability: Dictionary = value
	var origin_validation := ContractValidator.validate_request_context(capability.request_context)
	if not origin_validation.ok:
		return _invalid("PRODUCT_CAPABILITY_CONTEXT_INVALID", "Product capability response has an invalid origin context.")
	if capability.request_context.actor != expected_actor or capability.request_context.content_ref != expected_content_ref:
		return _invalid("PRODUCT_CAPABILITY_AUTHORITY_MISMATCH", "Product capability actor/content authority does not match Bootstrap.")
	if (
		str(capability.api_version) != "1.2.0"
		or str(capability.contract_version) != "0.6.0"
		or typeof(capability.world_presentation_enabled) != TYPE_BOOL
		or typeof(capability.skill_patch_enabled) != TYPE_BOOL
		or not _closed(capability.skill_patch_constraints, CONSTRAINT_FIELDS)
	):
		return _invalid("PRODUCT_CAPABILITY_VALUE_INVALID", "Product capability version, flags, or constraints are invalid.")
	var constraints: Dictionary = capability.skill_patch_constraints
	if (
		str(constraints.request_mode) != "EXPLICIT_UI_ACTION"
		or str(constraints.selection_target) != "FAILED_INTERACTION"
		or str(constraints.agent_role) != "teaching_agent"
		or str(constraints.scenario) != "RECTIFICATION"
		or typeof(constraints.required_hint_level) != TYPE_INT
		or int(constraints.required_hint_level) != 4
		or str(constraints.operation) != "UPSERT_FILE"
		or str(constraints.target) != "CURRENT_ENTRYPOINT"
		or typeof(constraints.max_files) != TYPE_INT
		or int(constraints.max_files) != 1
		or typeof(constraints.max_operations) != TYPE_INT
		or int(constraints.max_operations) != 1
	):
		return _invalid("PRODUCT_CAPABILITY_CONSTRAINT_INVALID", "Skill Patch scope is broader than the v0.6 minimal closure.")
	for field in ["requires_failed_evidence", "cas_required", "requires_student_confirmation"]:
		if typeof(constraints.get(field)) != TYPE_BOOL or not bool(constraints.get(field)):
			return _invalid("PRODUCT_CAPABILITY_CONSTRAINT_INVALID", "Skill Patch confirmation/evidence/CAS safeguards are disabled.")
	for field in ["auto_build", "auto_activate", "auto_run"]:
		if typeof(constraints.get(field)) != TYPE_BOOL or bool(constraints.get(field)):
			return _invalid("PRODUCT_CAPABILITY_AUTOMATION_FORBIDDEN", "Skill Patch capability permits a forbidden automatic action.")
	return {"ok": true}


static func _valid_attempt(value: Variant) -> bool:
	if not _closed(value, ["schema_version", "request_id", "correlation_id", "trace_id"]):
		return false
	return (
		str(value.schema_version) == "1.0.0"
		and str(value.request_id).begins_with("req_")
		and str(value.correlation_id).begins_with("corr_")
		and str(value.trace_id).begins_with("trace_")
	)


static func _matching_attempt_headers(value: Variant, attempt: Dictionary) -> bool:
	if not value is Dictionary:
		return false
	return (
		str(value.get("x-request-id", "")) == str(attempt.request_id)
		and str(value.get("x-trace-id", "")) == str(attempt.trace_id)
		and str(value.get("x-correlation-id", "")) == str(attempt.correlation_id)
	)


static func _closed(value: Variant, fields: Array) -> bool:
	if not value is Dictionary or value.size() != fields.size():
		return false
	for field in fields:
		if not value.has(field):
			return false
	return true


static func _invalid(code: String, message: String) -> Dictionary:
	return {"ok": false, "code": code, "message": message}


static func _failure(code: String, message: String) -> Dictionary:
	return {
		"ok": false, "status": 0, "headers": {},
		"error": {
			"scope": "CLIENT_LOCAL", "code": code, "message": message,
			"retryable": false, "data": null,
		},
	}
