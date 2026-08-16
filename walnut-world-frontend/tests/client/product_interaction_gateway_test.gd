extends SceneTree

const GatewayScript := preload("res://scripts/client/product_interaction_gateway.gd")
const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const MAX_SAFE_INTEGER := 9007199254740991

class FixtureTransport:
	extends RefCounted
	var response: Dictionary
	func _init(value: Dictionary) -> void: response = value
	func execute(_operation: String, _arguments: Dictionary) -> Dictionary:
		await Engine.get_main_loop().process_frame
		return response.duplicate(true)

func _initialize() -> void:
	var fixture: Dictionary = _read_fixture("product-agent-interaction-page.json")
	var gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": _list_headers(fixture), "value": fixture}))
	var result: Dictionary = await gateway.list_interactions(fixture.request_context, fixture.session_id, 0, 50)
	if not result.get("ok", false):
		push_error("合法 Product interaction fixture 必须通过 Godot 读取边界。")
		quit(1)
		return
	var valid_bug_page := fixture.duplicate(true)
	var bug: Dictionary = valid_bug_page.interactions[0]
	bug.role = "bug_agent"
	bug.response_type = "message"
	bug.question = null
	bug.hint_level = null
	bug.skill_patch = null
	bug.patch_decision = null
	bug.links.skill_draft = null
	bug.projection_source.role = "bug_agent"
	bug.projection_source.response_type = "message"
	bug.projection_source.question = null
	bug.projection_source.hint_level = null
	bug.projection_source.skill_patch_sha256 = null
	var source_payload: Dictionary = bug.projection_source.duplicate(true)
	source_payload.erase("source_sha256")
	bug.projection_source.source_sha256 = ContractValidator.canonical_json_sha256_v1(source_payload)
	var bug_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": _list_headers(valid_bug_page), "value": valid_bug_page}))
	if not (await bug_gateway.list_interactions(valid_bug_page.request_context, valid_bug_page.session_id, 0, 50)).get("ok", false):
		push_error("A contract-valid Bug message AgentInteraction must pass the Product boundary.")
		quit(1)
		return
	# Mirror the Backend worker's production projection: message responses carry no
	# SkillPatch, feedback/event times are the same UTC-Z instant, and projection,
	# created, and updated timestamps are one later UTC-Z instant.
	var production_timestamp_page := valid_bug_page.duplicate(true)
	var production_interaction: Dictionary = production_timestamp_page.interactions[0]
	production_interaction.feedback.completed_at = "2026-08-12T07:25:24.123456Z"
	production_interaction.feedback_event.occurred_at = "2026-08-12T07:25:24.123456Z"
	production_interaction.projection_source.committed_at = "2026-08-12T07:25:25.654321Z"
	production_interaction.created_at = "2026-08-12T07:25:25.654321Z"
	production_interaction.updated_at = "2026-08-12T07:25:25.654321Z"
	_rehash_feedback(production_interaction)
	var production_timestamp_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": _list_headers(production_timestamp_page), "value": production_timestamp_page}))
	var production_timestamp_result: Dictionary = await production_timestamp_gateway.list_interactions(production_timestamp_page.request_context, production_timestamp_page.session_id, 0, 50)
	if not production_timestamp_result.get("ok", false):
		push_error("A production-shaped Product interaction with identical UTC-Z feedback/event timestamps must pass: %s" % str(production_timestamp_result))
		quit(1)
		return
	var offset_drift_page := production_timestamp_page.duplicate(true)
	offset_drift_page.interactions[0].feedback_event.occurred_at = "2026-08-12T07:25:24.123456+00:00"
	var offset_drift_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": _list_headers(offset_drift_page), "value": offset_drift_page}))
	var offset_drift_result: Dictionary = await offset_drift_gateway.list_interactions(offset_drift_page.request_context, offset_drift_page.session_id, 0, 50)
	if (
		offset_drift_result.get("ok", true)
		or int(offset_drift_result.get("status", -1)) != 0
		or str(offset_drift_result.get("error", {}).get("code", "")) != "PRODUCT_RESPONSE_INVALID"
	):
		push_error("Semantically equal but byte-drifted feedback/event timestamps must fail closed as PRODUCT_RESPONSE_INVALID: %s" % str(offset_drift_result))
		quit(1)
		return
	var corrupted_pages: Array[Dictionary] = []
	var changed_feedback := fixture.duplicate(true)
	changed_feedback.interactions[0].feedback.message = "substituted feedback bytes"
	corrupted_pages.append({"label": "feedback hash substitution", "value": changed_feedback})
	var changed_source_hash := fixture.duplicate(true)
	changed_source_hash.interactions[0].projection_source.source_sha256 = "0".repeat(64)
	corrupted_pages.append({"label": "projection source hash", "value": changed_source_hash})
	var string_sequence := fixture.duplicate(true)
	string_sequence.interactions[0].sequence = "1"
	corrupted_pages.append({"label": "string sequence", "value": string_sequence})
	var reversed_timestamp := fixture.duplicate(true)
	reversed_timestamp.interactions[0].updated_at = "2026-08-07T00:00:00Z"
	corrupted_pages.append({"label": "timestamp reversal", "value": reversed_timestamp})
	var wrong_link := fixture.duplicate(true)
	wrong_link.interactions[0].links.self = "/product-experience/v1/sessions/session_agent_001/agent-interactions/interaction_other_001"
	corrupted_pages.append({"label": "canonical self link", "value": wrong_link})
	var evil_prefixed_link := fixture.duplicate(true)
	evil_prefixed_link.interactions[0].links.self = "https://evil.example/product-experience/v1/sessions/session_agent_001/agent-interactions/interaction_water_001"
	corrupted_pages.append({"label": "evil absolute self-link prefix", "value": evil_prefixed_link})
	var evil_workspace_prefix := fixture.duplicate(true)
	evil_workspace_prefix.interactions[0].links.session_workspace = "/evil/product-experience/v1/sessions/session_agent_001/workspace"
	corrupted_pages.append({"label": "evil workspace prefix", "value": evil_workspace_prefix})
	var invalid_event_id := fixture.duplicate(true)
	invalid_event_id.interactions[0].feedback_event.event_id = "event_feedback_game_00000001"
	invalid_event_id.interactions[0].projection_source.feedback_event_id = "event_feedback_game_00000001"
	_rehash_projection_source(invalid_event_id.interactions[0])
	corrupted_pages.append({"label": "EventEnvelope event_id pattern", "value": invalid_event_id})
	var invalid_stream := fixture.duplicate(true)
	invalid_stream.interactions[0].feedback_event.stream_id = "@evil"
	corrupted_pages.append({"label": "EventEnvelope stream_id pattern", "value": invalid_stream})
	var invalid_producer := fixture.duplicate(true)
	invalid_producer.interactions[0].feedback_event.producer = "Agent Hub"
	corrupted_pages.append({"label": "EventEnvelope producer pattern", "value": invalid_producer})
	var invalid_event_time := fixture.duplicate(true)
	invalid_event_time.interactions[0].feedback.completed_at = "2026-02-30T10:02:54Z"
	invalid_event_time.interactions[0].feedback_event.occurred_at = "2026-02-30T10:02:54Z"
	_rehash_feedback(invalid_event_time.interactions[0])
	corrupted_pages.append({"label": "RFC3339 calendar date", "value": invalid_event_time})
	var unsafe_event_sequence := fixture.duplicate(true)
	unsafe_event_sequence.interactions[0].feedback_event.sequence = MAX_SAFE_INTEGER + 1
	corrupted_pages.append({"label": "EventEnvelope max-safe sequence", "value": unsafe_event_sequence})
	var invalid_evidence_uri := fixture.duplicate(true)
	invalid_evidence_uri.interactions[0].feedback.evidence_refs[0]["uri"] = ""
	_rehash_feedback(invalid_evidence_uri.interactions[0])
	corrupted_pages.append({"label": "EvidenceRef URI bounds", "value": invalid_evidence_uri})
	for corruption: Dictionary in corrupted_pages:
		var corrupt_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": _list_headers(corruption.value), "value": corruption.value}))
		if (await corrupt_gateway.list_interactions(corruption.value.request_context, corruption.value.session_id, 0, 50)).get("ok", false):
			push_error("Product interaction corruption passed validation: %s" % str(corruption.label))
			quit(1)
			return
	var optional_evidence_fields := fixture.duplicate(true)
	optional_evidence_fields.interactions[0].feedback.evidence_refs[0].erase("sha256")
	optional_evidence_fields.interactions[0].feedback.evidence_refs[0]["uri"] = "/v1/evidence/evidence_world_00000001"
	optional_evidence_fields.interactions[0].skill_patch.evidence_refs[0].erase("sha256")
	optional_evidence_fields.interactions[0].skill_patch.evidence_refs[0]["uri"] = "/v1/evidence/evidence_world_00000001"
	# Round-trip the synthetic mutation so new keys have the same String key
	# representation as a real JSON response before canonical hashing.
	optional_evidence_fields = _normalize_numbers(JSON.parse_string(JSON.stringify(optional_evidence_fields)))
	_rehash_feedback(optional_evidence_fields.interactions[0])
	_rehash_skill_patch(optional_evidence_fields.interactions[0])
	_rehash_projection_source(optional_evidence_fields.interactions[0])
	var optional_evidence_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": _list_headers(optional_evidence_fields), "value": optional_evidence_fields}))
	var optional_evidence_result: Dictionary = await optional_evidence_gateway.list_interactions(optional_evidence_fields.request_context, optional_evidence_fields.session_id, 0, 50)
	if not optional_evidence_result.get("ok", false):
		push_error("EvidenceRef must accept the frozen optional sha256/uri shape in nested feedback and SkillPatch arrays: %s" % str(optional_evidence_result))
		quit(1)
		return
	var optional_patch_reference := {
		"evidence_id": "evidence_world_00000001",
		"evidence_type": "WORLD_COMMIT",
		"created_at": "2026-08-06T10:02:54Z",
		"uri": "/v1/evidence/evidence_world_00000001",
	}
	if not optional_evidence_gateway._valid_patch_evidence_ref(optional_patch_reference):
		push_error("SkillPatch EvidenceRef must also honor optional sha256 and URI fields from the frozen schema.")
		quit(1)
		return
	var exact_interaction: Dictionary = fixture.interactions[0]
	var exact_get_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": _get_headers(exact_interaction), "value": exact_interaction}))
	if not (await exact_get_gateway.get_interaction(fixture.request_context, fixture.session_id, exact_interaction.interaction_id)).get("ok", false):
		push_error("Product interaction GET must accept exact revision and canonical-projection ETag Headers.")
		quit(1)
		return
	var wrong_get_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": _get_headers(exact_interaction), "value": exact_interaction}))
	if (await wrong_get_gateway.get_interaction(fixture.request_context, fixture.session_id, "interaction_other_001")).get("ok", false):
		push_error("Product interaction GET must bind the exact requested interaction_id.")
		quit(1)
		return
	var missing_watermark_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": {}, "value": fixture}))
	if (await missing_watermark_gateway.list_interactions(fixture.request_context, fixture.session_id, 0, 50)).get("ok", false):
		push_error("Product interaction list must require X-Interaction-High-Watermark authority.")
		quit(1)
		return
	var drifted_revision_headers := _get_headers(exact_interaction)
	drifted_revision_headers["x-interaction-revision"] = "2"
	var drifted_revision_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": drifted_revision_headers, "value": exact_interaction}))
	if (await drifted_revision_gateway.get_interaction(fixture.request_context, fixture.session_id, exact_interaction.interaction_id)).get("ok", false):
		push_error("Product interaction GET revision Header must equal the body revision.")
		quit(1)
		return
	var weak_etag_headers := _get_headers(exact_interaction)
	weak_etag_headers["etag"] = "W/%s" % str(weak_etag_headers.etag)
	var weak_etag_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": weak_etag_headers, "value": exact_interaction}))
	if (await weak_etag_gateway.get_interaction(fixture.request_context, fixture.session_id, exact_interaction.interaction_id)).get("ok", false):
		push_error("Product interaction GET must reject weak or projection-drifted ETags.")
		quit(1)
		return
	var malformed_patch := fixture.duplicate(true)
	for interaction in malformed_patch.interactions:
		if interaction.skill_patch != null:
			interaction.skill_patch.operations[0].content_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
			break
	var malformed_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": _list_headers(malformed_patch), "value": malformed_patch}))
	if (await malformed_gateway.list_interactions(malformed_patch.request_context, malformed_patch.session_id, 0, 50)).get("ok", false):
		push_error("Malformed structured SkillPatch must be rejected before the UI can render it.")
		quit(1)
		return
	fixture.extra = true
	var invalid_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": _list_headers(fixture), "value": fixture}))
	if (await invalid_gateway.list_interactions(fixture.request_context, fixture.session_id, 0, 50)).get("ok", false):
		push_error("Product interaction page 的未知字段必须 fail-closed。")
		quit(1)
		return
	var draft: Dictionary = _read_fixture("product-skill-draft.json")
	var draft_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": {}, "value": draft}))
	var draft_result: Dictionary = await draft_gateway.get_draft(draft.request_context, draft.session_id, draft.draft_id)
	if not draft_result.get("ok", false):
		push_error("合法 Product draft fixture 必须通过 Godot 读取边界：%s" % str(draft_result.get("error", {})))
		quit(1)
		return
	# The frozen Product SourceBundle and x-draft-hash-contract are one closed
	# authority. Even a self-consistently rehashed response must fail if its path,
	# case-fold uniqueness, entrypoint, UTF-8 content hash, or Draft projection is
	# corrupt.
	var corrupt_drafts: Array[Dictionary] = []
	var changed_content := draft.duplicate(true)
	changed_content.source_bundle.files[0].content += "// response tamper\n"
	corrupt_drafts.append({"label": "UTF-8 content hash", "value": changed_content})
	var noncanonical_path := draft.duplicate(true)
	noncanonical_path.source_bundle.files[0].path = "src/../main.cpp"
	noncanonical_path.source_bundle.entrypoint = "src/../main.cpp"
	_rehash_draft(noncanonical_path)
	corrupt_drafts.append({"label": "canonical source path", "value": noncanonical_path})
	var case_collision := draft.duplicate(true)
	var duplicate_file: Dictionary = case_collision.source_bundle.files[0].duplicate(true)
	duplicate_file.path = str(duplicate_file.path).to_upper()
	case_collision.source_bundle.files.append(duplicate_file)
	_rehash_draft(case_collision)
	corrupt_drafts.append({"label": "ASCII case-fold path collision", "value": case_collision})
	var entrypoint_case_drift := draft.duplicate(true)
	entrypoint_case_drift.source_bundle.entrypoint = str(entrypoint_case_drift.source_bundle.entrypoint).to_upper()
	_rehash_draft(entrypoint_case_drift)
	corrupt_drafts.append({"label": "exact entrypoint path", "value": entrypoint_case_drift})
	var draft_hash_tamper := draft.duplicate(true)
	draft_hash_tamper.draft_sha256 = "0".repeat(64)
	corrupt_drafts.append({"label": "x-draft-hash-contract projection", "value": draft_hash_tamper})
	for corruption: Dictionary in corrupt_drafts:
		var validation: Dictionary = draft_gateway._validate_draft(
			corruption.value,
			str(draft.session_id),
			str(draft.draft_id),
		)
		if validation.get("ok", false):
			push_error("Corrupt canonical Draft passed the frozen validator: %s" % str(corruption.label))
			quit(1)
			return
	var reconciliation: Dictionary = _read_fixture("product-write-reconciliation.json")
	var reconciliation_gateway := GatewayScript.new(FixtureTransport.new({"ok": false, "status": 503, "headers": {"location": reconciliation.reconciliation.resource_url}, "error": reconciliation}))
	var decision_request := {"decision_id": "decision_demo_0001", "session_id": reconciliation.reconciliation.session_id, "turn_id": "turn_demo_0001", "interaction_id": reconciliation.reconciliation.resource_id, "expected_interaction_revision": 1, "patch_id": "patch_demo_0001", "patch_sha256": "a".repeat(64), "draft_id": draft.draft_id, "skill_id": draft.skill_id, "base_draft_revision": draft.revision, "base_draft_sha256": draft.draft_sha256, "result_draft_sha256": "b".repeat(64), "decision": "ACCEPT", "reason_code": null, "decided_at": "2026-08-09T00:00:00Z"}
	var reconciliation_result: Dictionary = await reconciliation_gateway.record_patch_decision(draft.request_context, reconciliation.reconciliation.session_id, reconciliation.reconciliation.resource_id, "patch_demo_0001", "key_product_reconcile_0001", decision_request, JSON.stringify(decision_request))
	if reconciliation_result.get("ok", true) or str(reconciliation_result.get("error", {}).get("status", "")) != "RECONCILE":
		push_error("合法 Product durable-write reconciliation 必须完整透传给协调器。")
		quit(1)
		return
	reconciliation.extra = true
	var invalid_reconciliation_gateway := GatewayScript.new(FixtureTransport.new({"ok": false, "status": 503, "headers": {"location": reconciliation.reconciliation.resource_url}, "error": reconciliation}))
	var invalid_reconciliation_result: Dictionary = await invalid_reconciliation_gateway.record_patch_decision(draft.request_context, reconciliation.reconciliation.session_id, reconciliation.reconciliation.resource_id, "patch_demo_0001", "key_product_reconcile_0001", decision_request, JSON.stringify(decision_request))
	if str(invalid_reconciliation_result.get("error", {}).get("code", "")) != "PRODUCT_RECONCILIATION_INVALID":
		push_error("未知 Product reconciliation 字段必须在 Gateway fail-closed。")
		quit(1)
		return
	var content: Dictionary = _read_fixture("product-content-unit.json")
	var content_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": {}, "value": content}))
	var content_result: Dictionary = await content_gateway.get_content(fixture.request_context, content.content_ref)
	if not content_result.get("ok", false):
		push_error("合法 Product ContentUnit fixture 必须通过 Godot 读取边界。")
		quit(1)
		return
	content.extra = true
	var invalid_content_gateway := GatewayScript.new(FixtureTransport.new({"ok": true, "status": 200, "headers": {}, "value": content}))
	if (await invalid_content_gateway.get_content(fixture.request_context, content.content_ref)).get("ok", false):
		push_error("Product ContentUnit 的未知字段必须 fail-closed。")
		quit(1)
		return
	print("PRODUCT_INTERACTION_GATEWAY_TEST_PASS")
	quit(0)


func _read_fixture(file_name: String) -> Dictionary:
	var examples := ProjectSettings.globalize_path("res://../agent/contracts/examples").simplify_path()
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(examples.path_join(file_name)))
	return _normalize_numbers(parsed.value)


func _list_headers(page: Dictionary) -> Dictionary:
	return {"x-interaction-high-watermark": str(page.high_watermark_sequence)}


func _get_headers(interaction: Dictionary) -> Dictionary:
	return {
		"x-interaction-revision": str(interaction.interaction_revision),
		"etag": "\"interaction:%s:%s\"" % [
			str(interaction.interaction_revision),
			ContractValidator.canonical_json_sha256_v1(interaction),
		],
	}


func _rehash_feedback(interaction: Dictionary) -> void:
	var feedback_hash := ContractValidator.canonical_json_sha256_v1(interaction.feedback)
	interaction.feedback_event.feedback_sha256 = feedback_hash
	interaction.projection_source.feedback_sha256 = feedback_hash
	_rehash_projection_source(interaction)


func _rehash_skill_patch(interaction: Dictionary) -> void:
	var patch_payload: Dictionary = interaction.skill_patch.duplicate(true)
	patch_payload.erase("patch_sha256")
	interaction.skill_patch.patch_sha256 = ContractValidator.canonical_json_sha256_v1(patch_payload)
	interaction.projection_source.skill_patch_sha256 = interaction.skill_patch.patch_sha256
	_rehash_projection_source(interaction)


func _rehash_projection_source(interaction: Dictionary) -> void:
	var source_payload: Dictionary = interaction.projection_source.duplicate(true)
	source_payload.erase("source_sha256")
	interaction.projection_source.source_sha256 = ContractValidator.canonical_json_sha256_v1(source_payload)


func _rehash_draft(draft: Dictionary) -> void:
	draft.draft_sha256 = ContractValidator.canonical_json_sha256_v1({
		"session_id": draft.session_id,
		"draft_id": draft.draft_id,
		"skill_id": draft.skill_id,
		"content_ref": draft.content_ref,
		"display_name": draft.display_name,
		"source_bundle": draft.source_bundle,
	})


func _normalize_numbers(value: Variant) -> Variant:
	if typeof(value) == TYPE_FLOAT and value == floor(value):
		return int(value)
	if value is Array:
		var normalized: Array = []
		for item in value:
			normalized.append(_normalize_numbers(item))
		return normalized
	if value is Dictionary:
		var normalized := {}
		for key in value:
			normalized[key] = _normalize_numbers(value[key])
		return normalized
	return value
