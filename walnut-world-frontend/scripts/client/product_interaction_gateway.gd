class_name ProductInteractionGateway
extends RefCounted

## Product gateway for AgentInteraction, Workspace, SkillDraft CAS, and
## PatchDecision. Responses are rejected before reaching ClientStore when
## their closed contract shape or cross-resource identity is invalid.

const ContractValidator = preload("res://addons/yaya_contract_client/contract_validator.gd")
const MAX_SAFE_INTEGER := 9007199254740991

var _transport: RefCounted


func _init(transport: RefCounted) -> void:
	_transport = transport


func get_content(attempt: Dictionary, content_ref: Dictionary) -> Dictionary:
	var attempt_validation := ContractValidator.validate_request_context(attempt)
	if not attempt_validation.ok:
		return _local_failure("PRODUCT_REQUEST_INVALID", "Product ContentUnit RequestContext is invalid: %s" % str(attempt_validation.error.get("message", "unknown violation")))
	if not _valid_content_ref(content_ref):
		return _local_failure("PRODUCT_REQUEST_INVALID", "Product ContentUnit content_ref is invalid.")
	if _transport == null or not _transport.has_method("execute"):
		return _local_failure("PRODUCT_TRANSPORT_UNAVAILABLE", "Product transport is not configured.")
	var result: Variant = await _transport.execute("get_product_content_unit", {"attempt_context": attempt, "content_ref": content_ref})
	if not result is Dictionary or typeof(result.get("ok")) != TYPE_BOOL:
		return _local_failure("PRODUCT_TRANSPORT_INVALID", "Product transport returned an invalid result union.")
	if not result.ok:
		return result if result.get("error") is Dictionary else _local_failure("PRODUCT_TRANSPORT_INVALID", "Product transport failure lacks a structured error.")
	if int(result.get("status", 0)) != 200 or not result.get("value") is Dictionary or not _valid_content_unit(result.value, content_ref):
		return _local_failure("PRODUCT_RESPONSE_INVALID", "Product ContentUnit violates its contract identity.")
	return {"ok": true, "status": 200, "headers": result.get("headers", {}).duplicate(true), "value": result.value.duplicate(true)}


func list_interactions(attempt: Dictionary, session_id: String, after_sequence: int, limit: int = 50) -> Dictionary:
	if (
		not _valid_attempt(attempt)
		or not _valid_id(session_id)
		or after_sequence < 0
		or after_sequence > MAX_SAFE_INTEGER
		or limit < 1
		or limit > 100
	):
		return _local_failure("PRODUCT_REQUEST_INVALID", "Product interaction list request is invalid.")
	return await _dispatch("list_product_agent_interactions", {"attempt_context": attempt, "session_id": session_id, "after_sequence": after_sequence, "limit": limit}, session_id, after_sequence, limit)


func get_interaction(attempt: Dictionary, session_id: String, interaction_id: String) -> Dictionary:
	if not _valid_attempt(attempt) or not _valid_id(session_id) or not _valid_id(interaction_id):
		return _local_failure("PRODUCT_REQUEST_INVALID", "Product interaction get request is invalid.")
	return await _dispatch(
		"get_product_agent_interaction",
		{"attempt_context": attempt, "session_id": session_id, "interaction_id": interaction_id},
		session_id,
		-1,
		-1,
		interaction_id,
	)


func get_workspace(attempt: Dictionary, session_id: String) -> Dictionary:
	if not _valid_attempt(attempt) or not _valid_id(session_id):
		return _local_failure("PRODUCT_REQUEST_INVALID", "Product workspace request is invalid.")
	if _transport == null or not _transport.has_method("execute"):
		return _local_failure("PRODUCT_TRANSPORT_UNAVAILABLE", "Product transport is not configured.")
	var result: Variant = await _transport.execute("get_product_session_workspace", {"attempt_context": attempt, "session_id": session_id})
	if not result is Dictionary or typeof(result.get("ok")) != TYPE_BOOL:
		return _local_failure("PRODUCT_TRANSPORT_INVALID", "Product transport returned an invalid result union.")
	if not result.ok:
		return result if result.get("error") is Dictionary else _local_failure("PRODUCT_TRANSPORT_INVALID", "Product transport failure lacks a structured error.")
	if int(result.get("status", 0)) != 200 or not result.get("value") is Dictionary or not _valid_workspace(result.value, session_id):
		return _local_failure("PRODUCT_RESPONSE_INVALID", "Product workspace violates its contract identity.")
	return {"ok": true, "status": 200, "headers": result.get("headers", {}).duplicate(true), "value": result.value.duplicate(true)}


func get_draft(attempt: Dictionary, session_id: String, draft_id: String) -> Dictionary:
	if not _valid_attempt(attempt) or not _valid_id(session_id) or not _valid_id(draft_id):
		return _local_failure("PRODUCT_REQUEST_INVALID", "Product draft get request is invalid.")
	return await _dispatch_draft("get_product_skill_draft", {"attempt_context": attempt, "session_id": session_id, "draft_id": draft_id}, session_id, draft_id)


func upsert_draft(attempt: Dictionary, session_id: String, draft_id: String, idempotency_key: String, request: Dictionary) -> Dictionary:
	if not _valid_attempt(attempt) or not _valid_id(session_id) or not _valid_id(draft_id) or idempotency_key.length() < 16 or not _valid_upsert(request, session_id, draft_id):
		return _local_failure("PRODUCT_REQUEST_INVALID", "Product draft CAS upsert request is invalid.")
	var result: Dictionary = await _dispatch_draft("upsert_product_skill_draft", {"attempt_context": attempt, "session_id": session_id, "draft_id": draft_id, "idempotency_key": idempotency_key, "request": request}, session_id, draft_id)
	return _validated_write_reconciliation(result, "SKILL_DRAFT", session_id, draft_id)


func record_patch_decision(attempt: Dictionary, session_id: String, interaction_id: String, patch_id: String, idempotency_key: String, request: Dictionary, request_body: String) -> Dictionary:
	var parsed_body: Variant = _normalize_json_integers(JSON.parse_string(request_body))
	if not _valid_attempt(attempt) or not _valid_id(session_id) or not _valid_id(interaction_id) or not _valid_id(patch_id) or idempotency_key.length() < 16 or not _valid_patch_decision(request, session_id, interaction_id, patch_id) or request_body.is_empty() or not parsed_body is Dictionary or parsed_body != request or request_body != JSON.stringify(request):
		return _local_failure("PRODUCT_REQUEST_INVALID", "Product PatchDecision request is invalid.")
	if _transport == null or not _transport.has_method("execute"):
		return _local_failure("PRODUCT_TRANSPORT_UNAVAILABLE", "Product transport is not configured.")
	var result: Variant = await _transport.execute("record_product_patch_decision", {"attempt_context": attempt, "session_id": session_id, "interaction_id": interaction_id, "patch_id": patch_id, "idempotency_key": idempotency_key, "request": request, "request_body": request_body})
	if not result is Dictionary or typeof(result.get("ok")) != TYPE_BOOL:
		return _local_failure("PRODUCT_TRANSPORT_INVALID", "Product transport returned an invalid result union.")
	if not result.ok:
		return _validated_write_reconciliation(result if result.get("error") is Dictionary else _local_failure("PRODUCT_TRANSPORT_INVALID", "Product transport failure lacks a structured error."), "AGENT_INTERACTION", session_id, interaction_id)
	if int(result.get("status", 0)) != 200 or not result.get("value") is Dictionary or not _valid_patch_receipt(result.value, session_id, interaction_id, patch_id):
		return _local_failure("PRODUCT_RESPONSE_INVALID", "Product PatchDecision receipt violates its contract identity or decision invariants.")
	return {"ok": true, "status": 200, "headers": result.get("headers", {}).duplicate(true), "value": result.value.duplicate(true)}


func _dispatch(
	operation: String,
	arguments: Dictionary,
	session_id: String,
	after_sequence: int = -1,
	limit: int = -1,
	interaction_id: String = "",
) -> Dictionary:
	if _transport == null or not _transport.has_method("execute"):
		return _local_failure("PRODUCT_TRANSPORT_UNAVAILABLE", "Product transport is not configured.")
	var result: Variant = await _transport.execute(operation, arguments)
	if not result is Dictionary or typeof(result.get("ok")) != TYPE_BOOL:
		return _local_failure("PRODUCT_TRANSPORT_INVALID", "Product transport returned an invalid result union.")
	if not result.ok:
		if result.get("error") is Dictionary:
			return result
		return _local_failure("PRODUCT_TRANSPORT_INVALID", "Product transport failure lacks a structured error.")
	if int(result.get("status", 0)) != 200 or not result.get("value") is Dictionary:
		return _local_failure("PRODUCT_RESPONSE_INVALID", "Product read must return a 200 JSON object.")
	var value: Dictionary = result.value
	var valid := (
		_validate_page(value, session_id, after_sequence, limit)
		if operation == "list_product_agent_interactions"
		else _validate_interaction(value, session_id, interaction_id)
	)
	if not valid.ok:
		return _local_failure("PRODUCT_RESPONSE_INVALID", str(valid.message))
	var header_validation := _validate_interaction_headers(
		operation,
		result.get("headers"),
		value,
	)
	if not header_validation.ok:
		return _local_failure("PRODUCT_RESPONSE_INVALID", str(header_validation.message))
	var attempt: Dictionary = arguments.get("attempt_context", {})
	if (
		value.request_context.actor != attempt.get("actor")
		or value.request_context.content_ref != attempt.get("content_ref")
	):
		return _local_failure("PRODUCT_RESPONSE_INVALID", "Product response actor/content authority differs from the request.")
	return {"ok": true, "status": 200, "headers": result.get("headers", {}).duplicate(true), "value": value.duplicate(true)}


func _dispatch_draft(operation: String, arguments: Dictionary, session_id: String, draft_id: String) -> Dictionary:
	if _transport == null or not _transport.has_method("execute"):
		return _local_failure("PRODUCT_TRANSPORT_UNAVAILABLE", "Product transport is not configured.")
	var result: Variant = await _transport.execute(operation, arguments)
	if not result is Dictionary or typeof(result.get("ok")) != TYPE_BOOL:
		return _local_failure("PRODUCT_TRANSPORT_INVALID", "Product transport returned an invalid result union.")
	if not result.ok:
		if result.get("error") is Dictionary:
			return result
		return _local_failure("PRODUCT_TRANSPORT_INVALID", "Product transport failure lacks a structured error.")
	if int(result.get("status", 0)) not in [200, 201] or not result.get("value") is Dictionary:
		return _local_failure("PRODUCT_RESPONSE_INVALID", "Product draft operation returned an unsupported status or body.")
	var value: Dictionary = result.value
	var valid := _validate_draft(value, session_id, draft_id)
	if not valid.ok:
		return _local_failure("PRODUCT_RESPONSE_INVALID", str(valid.message))
	var attempt: Variant = arguments.get("attempt_context")
	if (
		attempt is Dictionary
		and (
			value.request_context.actor != attempt.get("actor")
			or value.request_context.content_ref != attempt.get("content_ref")
		)
	):
		return _local_failure("PRODUCT_RESPONSE_INVALID", "Product Draft actor/content authority differs from the request.")
	return {"ok": true, "status": int(result.status), "headers": result.get("headers", {}).duplicate(true), "value": value.duplicate(true)}


## A 503 RECONCILE is not an ordinary retryable failure: it proves the write is
## durable and tells the caller which canonical GET must be read before replay.
## Validate this exceptional union at the Gateway boundary before business code
## is allowed to inspect it.
func _validated_write_reconciliation(result: Dictionary, resource_type: String, session_id: String, resource_id: String) -> Dictionary:
	if result.get("ok", false):
		return result
	var error: Variant = result.get("error")
	if not error is Dictionary or str(error.get("status", "")) != "RECONCILE":
		return result
	if int(result.get("status", 0)) != 503 or not result.get("headers") is Dictionary or error.size() != 7 or error.get("data") != null:
		return _local_failure("PRODUCT_RECONCILIATION_INVALID", "Product write reconciliation violates its HTTP response contract.")
	for field in ["request_id", "trace_id", "correlation_id"]:
		if not error.get(field) is String or str(error.get(field, "")).is_empty():
			return _local_failure("PRODUCT_RECONCILIATION_INVALID", "Product reconciliation attempt identity is invalid.")
	var details: Variant = error.get("error")
	var reconciliation: Variant = error.get("reconciliation")
	if not details is Dictionary or not reconciliation is Dictionary or reconciliation.size() != 5:
		return _local_failure("PRODUCT_RECONCILIATION_INVALID", "Product reconciliation fields are incomplete.")
	for field in ["resource_type", "session_id", "resource_id", "resource_url", "original_trace_id"]:
		if not reconciliation.get(field) is String or str(reconciliation.get(field, "")).is_empty():
			return _local_failure("PRODUCT_RECONCILIATION_INVALID", "Product reconciliation resource identity is invalid.")
	var expected_resource_url := (
		"/product-experience/v1/sessions/%s/agent-interactions/%s" % [session_id, resource_id]
		if resource_type == "AGENT_INTERACTION"
		else "/product-experience/v1/sessions/%s/skill-drafts/%s" % [session_id, resource_id]
	)
	if (
		str(reconciliation.resource_type) != resource_type
		or str(reconciliation.session_id) != session_id
		or str(reconciliation.resource_id) != resource_id
		or str(reconciliation.resource_url) != expected_resource_url
		or str(result.headers.get("location", "")) != expected_resource_url
	):
		return _local_failure("PRODUCT_RECONCILIATION_INVALID", "Product reconciliation does not name the requested canonical resource.")
	return result


func _validate_page(value: Dictionary, session_id: String, after_sequence: int, limit: int) -> Dictionary:
	var required := ["request_context", "session_id", "requested_after_sequence", "requested_limit", "high_watermark_sequence", "from_sequence", "to_sequence", "has_more", "next_after_sequence", "interactions"]
	if (
		not _closed(value, required)
		or not ContractValidator.validate_request_context(value.request_context).ok
		or str(value.session_id) != session_id
		or typeof(value.requested_after_sequence) != TYPE_INT
		or value.requested_after_sequence != after_sequence
		or value.requested_after_sequence > MAX_SAFE_INTEGER
		or typeof(value.requested_limit) != TYPE_INT
		or value.requested_limit != limit
		or typeof(value.high_watermark_sequence) != TYPE_INT
		or value.high_watermark_sequence < after_sequence
		or value.high_watermark_sequence > MAX_SAFE_INTEGER
		or typeof(value.next_after_sequence) != TYPE_INT
		or value.next_after_sequence < after_sequence
		or value.next_after_sequence > MAX_SAFE_INTEGER
		or typeof(value.has_more) != TYPE_BOOL
		or not value.interactions is Array
		or value.interactions.size() > limit
	):
		return {"ok": false, "message": "Product interaction page violates its closed contract surface."}
	var cursor := after_sequence
	var interaction_ids := {}
	for interaction in value.interactions:
		if (
			not interaction is Dictionary
			or not _validate_interaction(interaction, session_id).ok
			or int(interaction.sequence) != cursor + 1
			or interaction_ids.has(str(interaction.interaction_id))
			or interaction.request_context.actor != value.request_context.actor
			or interaction.request_context.content_ref != value.request_context.content_ref
		):
			return {"ok": false, "message": "Product interaction page is not gap-free or contains an invalid interaction."}
		interaction_ids[str(interaction.interaction_id)] = true
		cursor = int(interaction.sequence)
	if value.interactions.is_empty():
		if (
			value.from_sequence != null
			or value.to_sequence != null
			or value.has_more
			or value.next_after_sequence != after_sequence
			or value.high_watermark_sequence != after_sequence
		):
			return {"ok": false, "message": "Empty Product interaction page has an invalid cursor projection."}
		return {"ok": true}
	if (
		typeof(value.from_sequence) != TYPE_INT
		or typeof(value.to_sequence) != TYPE_INT
		or value.from_sequence < 1
		or value.from_sequence > MAX_SAFE_INTEGER
		or value.to_sequence < 1
		or value.to_sequence > MAX_SAFE_INTEGER
		or value.from_sequence != int(value.interactions[0].sequence)
		or value.to_sequence != cursor
		or value.next_after_sequence != cursor
		or value.high_watermark_sequence < cursor
		or value.has_more != (cursor < value.high_watermark_sequence)
	):
		return {"ok": false, "message": "Product interaction page cursors disagree with its records/high watermark."}
	return {"ok": true}


func _validate_interaction(value: Dictionary, session_id: String, interaction_id: String = "") -> Dictionary:
	var required := ["request_context", "interaction_id", "session_id", "turn_id", "sequence", "interaction_revision", "projection_source", "role", "response_type", "question", "hint_level", "feedback", "feedback_event", "skill_patch", "patch_decision", "created_at", "updated_at", "links"]
	if (
		not _closed(value, required)
		or str(value.session_id) != session_id
		or not _valid_id(str(value.interaction_id))
		or (not interaction_id.is_empty() and str(value.interaction_id) != interaction_id)
		or not _valid_id(str(value.turn_id))
		or typeof(value.sequence) != TYPE_INT
		or int(value.sequence) < 1
		or int(value.sequence) > MAX_SAFE_INTEGER
		or typeof(value.interaction_revision) != TYPE_INT
		or int(value.interaction_revision) < 1
		or int(value.interaction_revision) > MAX_SAFE_INTEGER
	):
		return {"ok": false, "message": "Product interaction identity is invalid."}
	var feedback_validation := ContractValidator.validate_agent_turn_feedback(value.feedback)
	if not ContractValidator.validate_request_context(value.request_context).ok or not feedback_validation.ok:
		return {"ok": false, "message": "Product interaction embeds an invalid request context or Game feedback."}
	if str(value.feedback.session_id) != session_id or str(value.feedback.turn_id) != str(value.turn_id):
		return {"ok": false, "message": "Product interaction feedback identity is inconsistent."}
	if str(value.role) not in ["world_agent", "xiaohutao", "teaching_agent", "bug_agent", "book_agent", "system"] or str(value.response_type) not in ["message", "question", "hint", "skill_patch", "growth_summary"]:
		return {"ok": false, "message": "Product interaction uses an unknown role or response type."}
	if not _valid_response_projection(value):
		return {"ok": false, "message": "Product interaction response fields contradict response_type."}
	if value.skill_patch != null and not _valid_skill_patch(value.skill_patch, value):
		return {"ok": false, "message": "Product interaction contains an invalid structured SkillPatch."}
	if not _validate_feedback_event(value.feedback_event, value).ok:
		return {"ok": false, "message": "Product interaction feedback EventEnvelope is invalid or misbound."}
	if not _validate_projection_source(value.projection_source, value).ok:
		return {"ok": false, "message": "Product interaction projection receipt is invalid or misbound."}
	var calculated_feedback_hash := ContractValidator.canonical_json_sha256_v1(value.feedback)
	if (
		calculated_feedback_hash.is_empty()
		or str(value.feedback_event.feedback_sha256) != calculated_feedback_hash
		or str(value.projection_source.feedback_sha256) != calculated_feedback_hash
	):
		return {"ok": false, "message": "Product interaction feedback hash does not bind the exact Game feedback."}
	if not _valid_interaction_revision(value):
		return {"ok": false, "message": "Product interaction revision and PatchDecision are inconsistent."}
	if not _valid_interaction_timestamps(value):
		return {"ok": false, "message": "Product interaction timestamps are invalid or causally reversed."}
	if not _valid_interaction_links(value):
		return {"ok": false, "message": "Product interaction links do not identify the canonical resources."}
	return {"ok": true}


func _validate_interaction_headers(
	operation: String,
	headers_value: Variant,
	value: Dictionary,
) -> Dictionary:
	if not headers_value is Dictionary:
		return {"ok": false, "message": "Product interaction response headers are absent."}
	var headers: Dictionary = headers_value
	if operation == "list_product_agent_interactions":
		var high_watermark: Variant = headers.get("x-interaction-high-watermark")
		if (
			typeof(high_watermark) != TYPE_STRING
			or str(high_watermark) != str(value.high_watermark_sequence)
		):
			return {"ok": false, "message": "Product interaction high-watermark Header disagrees with the canonical page."}
		return {"ok": true}
	if operation == "get_product_agent_interaction":
		var revision: Variant = headers.get("x-interaction-revision")
		var etag: Variant = headers.get("etag")
		var projection_sha256 := ContractValidator.canonical_json_sha256_v1(value)
		var expected_etag := "\"interaction:%s:%s\"" % [
			str(value.interaction_revision),
			projection_sha256,
		]
		if (
			typeof(revision) != TYPE_STRING
			or str(revision) != str(value.interaction_revision)
			or projection_sha256.is_empty()
			or typeof(etag) != TYPE_STRING
			or str(etag) != expected_etag
		):
			return {"ok": false, "message": "Product interaction revision/ETag Headers do not bind the canonical projection."}
		return {"ok": true}
	return {"ok": false, "message": "Product interaction response operation is unsupported."}


func _validate_projection_source(value: Variant, interaction: Dictionary) -> Dictionary:
	var required := [
		"receipt_id", "source_type", "source_revision", "actor", "content_ref",
		"interaction_id", "session_id", "turn_id", "sequence", "command_id",
		"feedback_event_id", "feedback_sha256", "role", "response_type", "question",
		"hint_level", "skill_patch_sha256", "committed_at", "source_sha256",
	]
	if not value is Dictionary or not _closed(value, required):
		return {"ok": false}
	if (
		not _valid_id(str(value.receipt_id))
		or str(value.source_type) != "AGENT_TURN_PRODUCT_PROJECTION"
		or typeof(value.source_revision) != TYPE_INT
		or int(value.source_revision) != 1
		or value.actor != interaction.request_context.actor
		or value.content_ref != interaction.request_context.content_ref
		or str(value.interaction_id) != str(interaction.interaction_id)
		or str(value.session_id) != str(interaction.session_id)
		or str(value.turn_id) != str(interaction.turn_id)
		or typeof(value.sequence) != TYPE_INT
		or int(value.sequence) != int(interaction.sequence)
		or int(value.sequence) > MAX_SAFE_INTEGER
		or str(value.command_id) != str(interaction.feedback.command_id)
		or str(value.feedback_event_id) != str(interaction.feedback_event.event_id)
		or str(value.role) != str(interaction.role)
		or str(value.response_type) != str(interaction.response_type)
		or value.question != interaction.question
		or value.hint_level != interaction.hint_level
		or not _valid_sha256(str(value.feedback_sha256))
		or not ContractValidator._validate_date_time(value.committed_at, "AgentInteraction.projection_source.committed_at").ok
		or not _valid_sha256(str(value.source_sha256))
	):
		return {"ok": false}
	var patch_hash: Variant = null
	if interaction.skill_patch is Dictionary:
		patch_hash = interaction.skill_patch.patch_sha256
	if value.skill_patch_sha256 != patch_hash:
		return {"ok": false}
	var source_payload: Dictionary = value.duplicate(true)
	source_payload.erase("source_sha256")
	return {
		"ok": ContractValidator.canonical_json_sha256_v1(source_payload) == str(value.source_sha256),
	}


func _validate_feedback_event(value: Variant, interaction: Dictionary) -> Dictionary:
	var required := [
		"event_id", "event_type", "event_version", "schema_version", "stream_id",
		"sequence", "occurred_at", "producer", "trace_id", "command_id",
		"correlation_id", "causation_id", "content_ref", "feedback_sha256",
	]
	if not value is Dictionary or not _closed(value, required):
		return {"ok": false}
	# Reconstruct the frozen EventEnvelope (the Product projection retains every
	# envelope field except payload) so identifier patterns, null causation,
	# ContentRef closure, and RFC3339 timestamps stay owned by the canonical
	# Game contract validator.
	var envelope: Dictionary = value.duplicate(true)
	envelope.erase("feedback_sha256")
	envelope["payload"] = {}
	var event_validation := ContractValidator.validate_event(envelope)
	if not event_validation.ok:
		return {"ok": false}
	if (
		str(value.event_type) != "agent.turn.feedback_ready"
		or typeof(value.event_version) != TYPE_INT
		or int(value.event_version) != 1
		or str(value.schema_version) != "1.0.0"
		or typeof(value.sequence) != TYPE_INT
		or int(value.sequence) < 1
		or int(value.sequence) > MAX_SAFE_INTEGER
		or str(value.command_id) != str(interaction.feedback.command_id)
		or value.content_ref != interaction.request_context.content_ref
		or not _valid_sha256(str(value.feedback_sha256))
	):
		return {"ok": false}
	return {"ok": true}


func _valid_response_projection(value: Dictionary) -> bool:
	match str(value.response_type):
		"question":
			return (
				typeof(value.question) == TYPE_STRING
				and not str(value.question).is_empty()
				and str(value.question).length() <= 1000
				and value.hint_level == null
				and value.skill_patch == null
				and value.patch_decision == null
			)
		"hint":
			return (
				value.question == null
				and typeof(value.hint_level) == TYPE_INT
				and int(value.hint_level) >= 0
				and int(value.hint_level) <= 3
				and value.skill_patch == null
				and value.patch_decision == null
			)
		"skill_patch":
			return (
				str(value.role) == "teaching_agent"
				and value.question == null
				and typeof(value.hint_level) == TYPE_INT
				and int(value.hint_level) == 4
				and value.skill_patch is Dictionary
			)
		"message", "growth_summary":
			return (
				value.question == null
				and value.hint_level == null
				and value.skill_patch == null
				and value.patch_decision == null
			)
	return false


func _valid_interaction_revision(value: Dictionary) -> bool:
	if value.patch_decision == null:
		return int(value.interaction_revision) == 1 and value.updated_at == value.created_at
	if not value.skill_patch is Dictionary or not value.patch_decision is Dictionary:
		return false
	var receipt: Dictionary = value.patch_decision
	if not _valid_patch_receipt(
		receipt,
		str(value.session_id),
		str(value.interaction_id),
		str(value.skill_patch.patch_id),
	):
		return false
	return (
		int(value.interaction_revision) == int(receipt.interaction_revision_after)
		and int(receipt.interaction_revision_before) == 1
		and int(receipt.interaction_revision_after) == 2
		and str(receipt.turn_id) == str(value.turn_id)
		and str(receipt.patch_sha256) == str(value.skill_patch.patch_sha256)
		and str(receipt.draft_id) == str(value.skill_patch.draft_id)
		and str(receipt.skill_id) == str(value.skill_patch.skill_id)
		and receipt.request_context.actor == value.request_context.actor
		and receipt.request_context.content_ref == value.request_context.content_ref
		and int(receipt.draft_revision_before) == int(value.skill_patch.base_draft_revision)
		and str(receipt.draft_sha256_before) == str(value.skill_patch.base_draft_sha256)
		and (
			str(receipt.draft_sha256_after) == str(value.skill_patch.result_draft_sha256)
			if str(receipt.decision) == "ACCEPT"
			else str(receipt.draft_sha256_after) == str(value.skill_patch.base_draft_sha256)
		)
	)


func _valid_interaction_timestamps(value: Dictionary) -> bool:
	for field in ["created_at", "updated_at"]:
		if not ContractValidator._validate_date_time(value[field], "AgentInteraction.%s" % field).ok:
			return false
	var occurred := _timestamp_seconds(str(value.feedback_event.occurred_at))
	var committed := _timestamp_seconds(str(value.projection_source.committed_at))
	var created := _timestamp_seconds(str(value.created_at))
	var updated := _timestamp_seconds(str(value.updated_at))
	if str(value.feedback_event.occurred_at) != str(value.feedback.completed_at):
		return false
	if str(value.projection_source.committed_at) != str(value.created_at):
		return false
	if value.patch_decision is Dictionary and str(value.patch_decision.get("decided_at", "")) != str(value.updated_at):
		return false
	return occurred <= committed and committed <= created and created <= updated


func _valid_interaction_links(value: Dictionary) -> bool:
	var links: Variant = value.get("links")
	if not links is Dictionary or not _closed(links, ["self", "session_workspace", "skill_draft"]):
		return false
	var session_path := "/product-experience/v1/sessions/%s" % str(value.session_id)
	var expected_self := "%s/agent-interactions/%s" % [
		session_path,
		str(value.interaction_id),
	]
	var expected_workspace := "%s/workspace" % session_path
	if (
		typeof(links.self) != TYPE_STRING
		or str(links.self) != expected_self
		or typeof(links.session_workspace) != TYPE_STRING
		or str(links.session_workspace) != expected_workspace
	):
		return false
	if value.skill_patch is Dictionary:
		var expected_draft := "%s/skill-drafts/%s" % [
			session_path,
			str(value.skill_patch.draft_id),
		]
		return (
			typeof(links.skill_draft) == TYPE_STRING
			and str(links.skill_draft) == expected_draft
		)
	return links.skill_draft == null


func _timestamp_seconds(value: String) -> float:
	var result := float(Time.get_unix_time_from_datetime_string(value.substr(0, 19)))
	var zone_index := value.find("Z", 19)
	if zone_index < 0:
		zone_index = value.find("z", 19)
	if zone_index < 0:
		zone_index = value.find("+", 19)
	if zone_index < 0:
		zone_index = value.find("-", 19)
	var fraction_index := value.find(".", 19)
	if fraction_index >= 0 and zone_index > fraction_index:
		result += float("0.%s" % value.substr(fraction_index + 1, zone_index - fraction_index - 1))
	if zone_index >= 0 and value.substr(zone_index, 1).to_upper() != "Z":
		var offset := float(value.substr(zone_index + 1, 2).to_int() * 3600 + value.substr(zone_index + 4, 2).to_int() * 60)
		result += -offset if value.substr(zone_index, 1) == "+" else offset
	return result


func _validate_draft(value: Dictionary, session_id: String, draft_id: String) -> Dictionary:
	var required := ["request_context", "session_id", "draft_id", "skill_id", "revision", "content_ref", "display_name", "source_bundle", "draft_sha256", "created_at", "updated_at", "last_applied_patch_id", "links"]
	if (
		not _closed(value, required)
		or str(value.session_id) != session_id
		or str(value.draft_id) != draft_id
		or not _valid_id(session_id)
		or not _valid_id(draft_id)
		or typeof(value.revision) != TYPE_INT
		or int(value.revision) < 1
		or int(value.revision) > MAX_SAFE_INTEGER
	):
		return {"ok": false, "message": "Product draft identity or revision is invalid."}
	if (
		not ContractValidator.validate_request_context(value.request_context).ok
		or value.request_context.get("content_ref") != value.content_ref
		or not ContractValidator._validate_content_ref(value.content_ref).ok
		or not _valid_id(str(value.skill_id))
		or not _valid_sha256(str(value.draft_sha256))
		or typeof(value.display_name) != TYPE_STRING
		or str(value.display_name).is_empty()
		or str(value.display_name).length() > 80
	):
		return {"ok": false, "message": "Product draft context, skill identity, or hash is invalid."}
	if not _valid_product_source_bundle(value.source_bundle):
		return {"ok": false, "message": "Product draft source bundle is invalid."}
	for field in ["created_at", "updated_at"]:
		if not ContractValidator._validate_date_time(value[field], "SkillDraft.%s" % field).ok:
			return {"ok": false, "message": "Product Draft timestamps are invalid."}
	if (
		_timestamp_seconds(str(value.request_context.requested_at)) > _timestamp_seconds(str(value.created_at))
		or _timestamp_seconds(str(value.created_at)) > _timestamp_seconds(str(value.updated_at))
		or (value.last_applied_patch_id != null and not _valid_id(str(value.last_applied_patch_id)))
		or not _valid_draft_links(value)
	):
		return {"ok": false, "message": "Product Draft origin, Patch identity, timestamps, or links are invalid."}
	var expected_draft_sha256 := ContractValidator.canonical_json_sha256_v1({
		"session_id": value.session_id,
		"draft_id": value.draft_id,
		"skill_id": value.skill_id,
		"content_ref": value.content_ref,
		"display_name": value.display_name,
		"source_bundle": value.source_bundle,
	})
	if expected_draft_sha256.is_empty() or str(value.draft_sha256) != expected_draft_sha256:
		return {"ok": false, "message": "Product Draft violates the frozen x-draft-hash-contract."}
	return {"ok": true}


func _valid_product_source_bundle(value: Variant) -> bool:
	if (
		not value is Dictionary
		or not _closed(value, ["language", "entrypoint", "files"])
		or str(value.language) != "CPP20"
		or not _valid_source_path(value.entrypoint)
		or not value.files is Array
		or value.files.is_empty()
		or value.files.size() > 32
	):
		return false
	var folded_paths := {}
	var entrypoint_matches := 0
	var total_utf8_bytes := 0
	for file_value: Variant in value.files:
		if not file_value is Dictionary or not _closed(file_value, ["path", "content", "content_sha256"]):
			return false
		var path: Variant = file_value.path
		var content: Variant = file_value.content
		var content_sha256: Variant = file_value.content_sha256
		if (
			not _valid_source_path(path)
			or typeof(content) != TYPE_STRING
			or str(content).length() > 1048576
			or not _valid_sha256(str(content_sha256))
			or str(content).sha256_text() != str(content_sha256)
		):
			return false
		var folded_path := str(path).to_lower()
		if folded_paths.has(folded_path):
			return false
		folded_paths[folded_path] = true
		total_utf8_bytes += str(content).to_utf8_buffer().size()
		if total_utf8_bytes > 1048576:
			return false
		if str(path) == str(value.entrypoint):
			entrypoint_matches += 1
	return entrypoint_matches == 1


func _valid_draft_links(value: Dictionary) -> bool:
	var links: Variant = value.get("links")
	if not links is Dictionary or not _closed(links, ["self", "session_workspace", "builds"]):
		return false
	var session_path := "/product-experience/v1/sessions/%s" % str(value.session_id)
	return (
		typeof(links.self) == TYPE_STRING
		and str(links.self) == "%s/skill-drafts/%s" % [session_path, str(value.draft_id)]
		and typeof(links.session_workspace) == TYPE_STRING
		and str(links.session_workspace) == "%s/workspace" % session_path
		and typeof(links.builds) == TYPE_STRING
		and str(links.builds) == "/v1/skill-builds"
	)


func _valid_upsert(value: Dictionary, session_id: String, draft_id: String) -> bool:
	var required := ["session_id", "draft_id", "skill_id", "content_ref", "base_revision", "base_draft_sha256", "display_name", "source_bundle", "client_saved_at"]
	if not _closed(value, required) or str(value.session_id) != session_id or str(value.draft_id) != draft_id or not _valid_id(str(value.skill_id)) or int(value.base_revision) < 0:
		return false
	if int(value.base_revision) == 0:
		return value.base_draft_sha256 == null
	return _valid_sha256(str(value.base_draft_sha256))


func _valid_patch_decision(value: Dictionary, session_id: String, interaction_id: String, patch_id: String) -> bool:
	var required := ["decision_id", "session_id", "turn_id", "interaction_id", "expected_interaction_revision", "patch_id", "patch_sha256", "draft_id", "skill_id", "base_draft_revision", "base_draft_sha256", "result_draft_sha256", "decision", "reason_code", "decided_at"]
	if not _closed(value, required) or str(value.session_id) != session_id or str(value.interaction_id) != interaction_id or str(value.patch_id) != patch_id:
		return false
	for field in ["decision_id", "turn_id", "draft_id", "skill_id"]:
		if not _valid_id(str(value[field])): return false
	for field in ["patch_sha256", "base_draft_sha256", "result_draft_sha256"]:
		if not _valid_sha256(str(value[field])): return false
	if (
		typeof(value.expected_interaction_revision) != TYPE_INT
		or int(value.expected_interaction_revision) < 1
		or int(value.expected_interaction_revision) > MAX_SAFE_INTEGER
		or typeof(value.base_draft_revision) != TYPE_INT
		or int(value.base_draft_revision) < 1
		or int(value.base_draft_revision) > MAX_SAFE_INTEGER
		or str(value.decision) not in ["ACCEPT", "REJECT"]
		or not ContractValidator._validate_date_time(value.decided_at, "PatchDecisionRequest.decided_at").ok
	):
		return false
	return value.reason_code == null if value.decision == "ACCEPT" else _valid_reason_code(value.reason_code)


func _valid_skill_patch(value: Variant, interaction: Dictionary) -> bool:
	if not value is Dictionary:
		return false
	var required := ["patch_id", "interaction_id", "session_id", "turn_id", "draft_id", "skill_id", "base_draft_revision", "base_draft_sha256", "operations", "result_draft_sha256", "patch_sha256", "rationale", "requires_student_confirmation", "evidence_refs", "created_at"]
	if not _closed(value, required) or str(value.interaction_id) != str(interaction.interaction_id) or str(value.session_id) != str(interaction.session_id) or str(value.turn_id) != str(interaction.turn_id) or not bool(value.requires_student_confirmation):
		return false
	for field in ["patch_id", "draft_id", "skill_id"]:
		if not _valid_id(str(value.get(field, ""))):
			return false
	for field in ["base_draft_sha256", "result_draft_sha256", "patch_sha256"]:
		if not _valid_sha256(str(value.get(field, ""))):
			return false
	if (
		typeof(value.base_draft_revision) != TYPE_INT
		or int(value.base_draft_revision) < 1
		or int(value.base_draft_revision) > MAX_SAFE_INTEGER
		or typeof(value.rationale) != TYPE_STRING
		or value.rationale.is_empty()
		or value.rationale.length() > 2000
		or typeof(value.requires_student_confirmation) != TYPE_BOOL
		or not value.requires_student_confirmation
		or not value.operations is Array
		or value.operations.is_empty()
		or value.operations.size() > 35
		or not value.evidence_refs is Array
		or value.evidence_refs.size() > 64
	):
		return false
	var changed_paths := {}
	var singleton_operations := {}
	for operation in value.operations:
		if not _valid_patch_operation(operation, changed_paths):
			return false
		var kind := str(operation.operation)
		if kind in ["SET_ENTRYPOINT", "SET_DISPLAY_NAME"]:
			if singleton_operations.has(kind):
				return false
			singleton_operations[kind] = true
	var evidence_ids := {}
	for reference in value.evidence_refs:
		if not _valid_patch_evidence_ref(reference):
			return false
		if evidence_ids.has(str(reference.evidence_id)):
			return false
		evidence_ids[str(reference.evidence_id)] = true
	if (
		not ContractValidator._validate_date_time(value.created_at, "SkillPatch.created_at").ok
		or str(value.created_at) != str(interaction.created_at)
	):
		return false
	if not _unique_exact_values(value.operations):
		return false
	var patch_payload: Dictionary = value.duplicate(true)
	patch_payload.erase("patch_sha256")
	var calculated_patch_hash := ContractValidator.canonical_json_sha256_v1(patch_payload)
	if calculated_patch_hash != str(value.patch_sha256):
		return false
	return true


func _valid_patch_operation(value: Variant, changed_paths: Dictionary) -> bool:
	if not value is Dictionary or not value.has("operation"):
		return false
	var operation := str(value.operation)
	match operation:
		"UPSERT_FILE":
			if not _closed(value, ["operation", "path", "previous_content_sha256", "content", "content_sha256"]) or not _valid_source_path(value.path) or typeof(value.content) != TYPE_STRING or value.content.length() > 1048576 or not _valid_sha256(str(value.content_sha256)) or (value.previous_content_sha256 != null and not _valid_sha256(str(value.previous_content_sha256))):
				return false
			if str(value.content).sha256_text() != str(value.content_sha256):
				return false
			return _record_patch_path(changed_paths, str(value.path))
		"DELETE_FILE":
			if not _closed(value, ["operation", "path", "previous_content_sha256"]) or not _valid_source_path(value.path) or not _valid_sha256(str(value.previous_content_sha256)):
				return false
			return _record_patch_path(changed_paths, str(value.path))
		"SET_ENTRYPOINT":
			return _closed(value, ["operation", "path"]) and _valid_source_path(value.path)
		"SET_DISPLAY_NAME":
			return _closed(value, ["operation", "display_name"]) and typeof(value.display_name) == TYPE_STRING and not value.display_name.is_empty() and value.display_name.length() <= 80
		_:
			return false


func _record_patch_path(changed_paths: Dictionary, path: String) -> bool:
	var canonical := path.to_lower()
	if changed_paths.has(canonical):
		return false
	changed_paths[canonical] = true
	return true


func _valid_source_path(value: Variant) -> bool:
	if typeof(value) != TYPE_STRING or value.is_empty() or value.length() > 240:
		return false
	for segment in value.split("/", true):
		if segment.is_empty():
			return false
		for index in range(segment.length()):
			var character: String = segment.substr(index, 1)
			var allowed := "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
			if allowed.find(character) < 0:
				return false
			if index == 0 and "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_".find(character) < 0:
				return false
			if index == segment.length() - 1 and "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-".find(character) < 0:
				return false
	return true


func _valid_patch_evidence_ref(value: Variant) -> bool:
	# EvidenceRef.sha256 and EvidenceRef.uri are independently optional in the
	# frozen schema. Reuse the common validator rather than narrowing that shape.
	if not value is Dictionary:
		return false
	var required := ["evidence_id", "evidence_type", "created_at"]
	var allowed := required + ["sha256", "uri"]
	for field in required:
		if not value.has(field):
			return false
	for field in value:
		if field not in allowed:
			return false
	return ContractValidator._validate_evidence_ref(value).ok


func _unique_exact_values(values: Array) -> bool:
	var canonical_values := {}
	for value in values:
		var canonical := ContractValidator.canonical_json_sha256_v1(value)
		if canonical.is_empty() or canonical_values.has(canonical):
			return false
		canonical_values[canonical] = true
	return true


func _valid_workspace(value: Dictionary, session_id: String) -> bool:
	var required := ["request_context", "workspace_id", "workspace_revision", "session", "content_ref", "current_task", "world_checkpoint", "skill_draft_refs", "last_interaction_sequence", "created_at", "updated_at", "links"]
	if not _closed(value, required) or not ContractValidator.validate_request_context(value.request_context).ok or not value.session is Dictionary or str(value.session.get("session_id", "")) != session_id or value.request_context.actor != value.session.get("request_context", {}).get("actor"):
		return false
	if int(value.workspace_revision) < 1 or not value.world_checkpoint is Dictionary or not _valid_id(str(value.world_checkpoint.get("world_id", ""))) or not value.skill_draft_refs is Array:
		return false
	for reference in value.skill_draft_refs:
		if not reference is Dictionary or reference.size() != 5 or not _valid_id(str(reference.get("draft_id", ""))) or not _valid_id(str(reference.get("skill_id", ""))) or int(reference.get("revision", 0)) < 1 or not _valid_sha256(str(reference.get("draft_sha256", ""))):
			return false
	return true


func _valid_content_unit(value: Dictionary, expected_ref: Dictionary) -> bool:
	var required := ["content_ref", "status", "unit_type", "audiences", "task", "published_at", "links"]
	if not _closed(value, required) or value.get("content_ref") != expected_ref or str(value.get("status", "")) != "PUBLISHED" or str(value.get("unit_type", "")) != "TASK" or not value.get("audiences") is Array or not value.get("task") is Dictionary or not value.get("links") is Dictionary:
		return false
	var audiences: Array = value.audiences
	if audiences.is_empty() or audiences.size() > 2:
		return false
	for audience in audiences:
		if str(audience) not in ["LEARNER", "TEACHER_PREVIEW"]:
			return false
	var task: Dictionary = value.task
	var task_required := ["task_id", "name", "goal", "instructions", "knowledge_points", "allowed_capabilities", "starter_skill", "hint_policy", "story"]
	if not _closed(task, task_required) or not _valid_id(str(task.get("task_id", ""))) or str(task.get("name", "")).is_empty() or str(task.get("goal", "")).is_empty() or not task.get("instructions") is Array or not task.get("knowledge_points") is Array or not task.get("allowed_capabilities") is Array:
		return false
	for capability in task.allowed_capabilities:
		if str(capability) not in ["WORLD_READ", "MOVE", "PLANT", "WATER", "HARVEST", "INTERACT", "SPEAK"]:
			return false
	var starter: Variant = task.get("starter_skill")
	if starter != null and not _valid_starter_skill(starter):
		return false
	var hint_policy: Variant = task.get("hint_policy")
	var story: Variant = task.get("story")
	return hint_policy is Dictionary and _closed(hint_policy, ["max_level", "levels"]) and int(hint_policy.get("max_level", -1)) == 4 and hint_policy.get("levels") is Array and hint_policy.levels.size() == 5 and story is Dictionary and _closed(story, ["opening", "success"]) and not str(story.get("opening", "")).is_empty() and not str(story.get("success", "")).is_empty() and value.links.size() == 1 and typeof(value.links.get("self")) == TYPE_STRING


func _valid_starter_skill(value: Variant) -> bool:
	if not value is Dictionary or not _closed(value, ["skill_id", "display_name", "source_bundle", "compiler_profile", "test_suite_version"]):
		return false
	if not _valid_id(str(value.get("skill_id", ""))) or str(value.get("display_name", "")).is_empty() or str(value.get("compiler_profile", "")) != "YAYA_CPP20_SAFE_V1" or str(value.get("test_suite_version", "")).is_empty() or not value.get("source_bundle") is Dictionary:
		return false
	var bundle: Dictionary = value.source_bundle
	if not _closed(bundle, ["language", "entrypoint", "files"]) or str(bundle.get("language", "")) != "CPP20" or not bundle.get("files") is Array or bundle.files.is_empty() or bundle.files.size() > 32:
		return false
	var entrypoint_count := 0
	var paths: Dictionary = {}
	for file in bundle.files:
		if not file is Dictionary or not _closed(file, ["path", "content", "content_sha256"]) or not _valid_sha256(str(file.get("content_sha256", ""))) or str(file.get("content", "")).sha256_text() != str(file.get("content_sha256", "")):
			return false
		var path := str(file.get("path", ""))
		if path.is_empty() or paths.has(path):
			return false
		paths[path] = true
		if path == str(bundle.get("entrypoint", "")):
			entrypoint_count += 1
	return entrypoint_count == 1


func _valid_patch_receipt(value: Dictionary, session_id: String, interaction_id: String, patch_id: String) -> bool:
	var required := ["request_context", "decision_id", "session_id", "turn_id", "interaction_id", "interaction_revision_before", "interaction_revision_after", "patch_id", "patch_sha256", "draft_id", "skill_id", "decision", "reason_code", "draft_updated", "draft_revision_before", "draft_sha256_before", "draft_revision_after", "draft_sha256_after", "decided_at", "links"]
	if not _closed(value, required) or str(value.session_id) != session_id or str(value.interaction_id) != interaction_id or str(value.patch_id) != patch_id or not ContractValidator.validate_request_context(value.request_context).ok:
		return false
	for field in ["decision_id", "session_id", "turn_id", "interaction_id", "patch_id", "draft_id", "skill_id"]:
		if not _valid_id(str(value.get(field, ""))):
			return false
	for field in ["patch_sha256", "draft_sha256_before", "draft_sha256_after"]:
		if not _valid_sha256(str(value.get(field, ""))):
			return false
	if (
		typeof(value.interaction_revision_before) != TYPE_INT
		or typeof(value.interaction_revision_after) != TYPE_INT
		or int(value.interaction_revision_before) < 1
		or int(value.interaction_revision_before) > MAX_SAFE_INTEGER - 1
		or int(value.interaction_revision_after) != int(value.interaction_revision_before) + 1
		or int(value.interaction_revision_after) > MAX_SAFE_INTEGER
		or typeof(value.draft_updated) != TYPE_BOOL
		or typeof(value.draft_revision_before) != TYPE_INT
		or typeof(value.draft_revision_after) != TYPE_INT
		or int(value.draft_revision_before) < 1
		or int(value.draft_revision_before) > MAX_SAFE_INTEGER - 1
		or int(value.draft_revision_after) < 1
		or int(value.draft_revision_after) > MAX_SAFE_INTEGER
		or not ContractValidator._validate_date_time(value.decided_at, "PatchDecisionReceipt.decided_at").ok
		or not value.links is Dictionary
		or not _closed(value.links, ["interaction", "skill_draft"])
		or typeof(value.links.interaction) != TYPE_STRING
		or str(value.links.interaction) != "/product-experience/v1/sessions/%s/agent-interactions/%s" % [session_id, interaction_id]
		or typeof(value.links.skill_draft) != TYPE_STRING
		or str(value.links.skill_draft) != "/product-experience/v1/sessions/%s/skill-drafts/%s" % [session_id, str(value.draft_id)]
	):
		return false
	if str(value.decision) == "ACCEPT":
		return value.draft_updated and value.reason_code == null and int(value.draft_revision_after) == int(value.draft_revision_before) + 1
	if str(value.decision) == "REJECT":
		return not value.draft_updated and _valid_reason_code(value.reason_code) and int(value.draft_revision_after) == int(value.draft_revision_before) and str(value.draft_sha256_after) == str(value.draft_sha256_before)
	return false


func _valid_reason_code(value: Variant) -> bool:
	if typeof(value) != TYPE_STRING or value.length() < 3 or value.length() > 96:
		return false
	for index in range(value.length()):
		var character: String = value.substr(index, 1)
		if index == 0:
			if "ABCDEFGHIJKLMNOPQRSTUVWXYZ".find(character) < 0:
				return false
		elif "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_".find(character) < 0:
				return false
	return true


func _closed(value: Dictionary, required: Array) -> bool:
	if value.size() != required.size():
		return false
	for field in required:
		if not value.has(field):
			return false
	return true


func _valid_attempt(value: Dictionary) -> bool:
	return ContractValidator.validate_request_context(value).ok


func _valid_id(value: String) -> bool:
	return ContractValidator.validate_identifier(value).ok


func _valid_content_ref(value: Dictionary) -> bool:
	return value.size() == 3 and _valid_id(str(value.get("unit_id", ""))) and not str(value.get("version", "")).is_empty() and _valid_sha256(str(value.get("content_hash", "")))


func _valid_sha256(value: String) -> bool:
	if value.length() != 64:
		return false
	for index in range(value.length()):
		if "0123456789abcdef".find(value.substr(index, 1)) < 0:
			return false
	return true


func _normalize_json_integers(value: Variant) -> Variant:
	if typeof(value) == TYPE_FLOAT and is_finite(value) and value == floor(value) and absf(value) <= MAX_SAFE_INTEGER:
		return int(value)
	if value is Array:
		var normalized_array: Array = []
		for item in value:
			normalized_array.append(_normalize_json_integers(item))
		return normalized_array
	if value is Dictionary:
		var normalized_dictionary := {}
		for key in value:
			normalized_dictionary[key] = _normalize_json_integers(value[key])
		return normalized_dictionary
	return value


func _local_failure(code: String, message: String) -> Dictionary:
	return {"ok": false, "status": 0, "headers": {}, "error": {"scope": "CLIENT_LOCAL", "code": code, "message": message}}
