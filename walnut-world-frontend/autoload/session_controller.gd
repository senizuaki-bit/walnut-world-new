extends Node

## Coordinates the task screen through validated public Gateways. It never
## invents Session/Build/Activation/World authority and never marks a Turn
## complete before the HTTP closure has been verified.

signal capability_unavailable(capability: String, message: String)
signal interactions_recovered(interactions: Array[Dictionary])
signal build_resolved(build: Dictionary)
signal run_resolved(run: Dictionary)
signal world_playback_state_changed(state: String)
signal patch_decision_resolved(interaction_id: String, patch_id: String, decision: String)

const CommandPoller = preload("res://scripts/client/command_poller.gd")
const ContractValidator = preload("res://addons/yaya_contract_client/contract_validator.gd")
const ProductInteractionGateway = preload("res://scripts/client/product_interaction_gateway.gd")
const DEFAULT_INTERACTION_DEADLINE_SECONDS := 15.0
const DEFAULT_INTERACTION_DELAY_SECONDS := 0.25
const PENDING_TURN_SLOTS := ["agent_turn", "agent_hint"]
const PENDING_DRAFT_SAVE_SLOT := "draft_save"
const RETRYABLE_HTTP_STATUSES := [429, 502, 503, 504]
const RETRYABLE_LOCAL_TRANSPORT_CODES := [
	"LOCAL_TRANSPORT_BUSY",
	"LOCAL_TRANSPORT_NETWORK_ERROR",
	"LOCAL_TRANSPORT_REQUEST_START_FAILED",
	"LOCAL_TRANSPORT_TIMEOUT",
]

var game_gateway: RefCounted
var product_gateway: RefCounted
var world_presentation_gateway: RefCounted
var world_event_player: Node
var world_presentation_renderer: Node
var draft_context: Dictionary = {}
var build_context: Dictionary = {}
var activation_context: Dictionary = {}
var certified_build: Dictionary = {}
var certified_build_draft_authority: Dictionary = {}
var active_activation: Dictionary = {}
var active_skill_tuple: Dictionary = {}
var authority_context: Dictionary = {}
var authoritative_session: Dictionary = {}
var polling_settings: Dictionary = {}
var patch_decisions_enabled := false
var world_presentation_enabled := false
var skill_patch_capability: Dictionary = {}
var visible_patch_failure: Dictionary = {}
var patch_failure_recovery_status: Dictionary = {}
var _pending_turn_recovery_active := false
var _startup_authority_revalidation_pending := false
var _startup_authority_guard_enabled := false
var _last_presentation_pre_snapshot: Dictionary = {}
var _last_presentation_final_snapshot: Dictionary = {}


func configure(
	gateway: RefCounted,
	product: RefCounted = null,
	_enable_patch_decisions_legacy := false,
) -> void:
	game_gateway = gateway
	product_gateway = product
	# Runtime dependency injection is not rollout authority. Only a validated
	# v0.6 capability response may enable the Patch request/decision surface.
	patch_decisions_enabled = false
	skill_patch_capability.clear()
	visible_patch_failure.clear()
	patch_failure_recovery_status.clear()


func configure_world_presentation(
	gateway: RefCounted,
	player: Node,
	renderer: Node,
	enabled := false,
) -> void:
	world_presentation_gateway = gateway
	world_event_player = player
	world_presentation_renderer = renderer
	world_presentation_enabled = enabled
	_last_presentation_pre_snapshot.clear()
	_last_presentation_final_snapshot.clear()
	world_playback_state_changed.emit("CONFIGURED")


func configure_skill_patch_capability(value: Dictionary) -> void:
	skill_patch_capability.clear()
	patch_decisions_enabled = false
	visible_patch_failure.clear()
	patch_failure_recovery_status.clear()
	var constraints: Variant = value.get("skill_patch_constraints")
	# INT2 M2 is downstream of the authoritative M1 presentation closure. Both
	# local composition and Backend rollout authority must prove M1 enabled.
	if (
		not world_presentation_enabled
		or not bool(value.get("world_presentation_enabled", false))
		or not bool(value.get("skill_patch_enabled", false))
		or not constraints is Dictionary
	):
		return
	if (
		str(constraints.get("request_mode", "")) != "EXPLICIT_UI_ACTION"
		or str(constraints.get("selection_target", "")) != "FAILED_INTERACTION"
		or str(constraints.get("agent_role", "")) != "teaching_agent"
		or str(constraints.get("scenario", "")) != "RECTIFICATION"
		or int(constraints.get("required_hint_level", -1)) != 4
		or str(constraints.get("operation", "")) != "UPSERT_FILE"
		or str(constraints.get("target", "")) != "CURRENT_ENTRYPOINT"
		or int(constraints.get("max_files", -1)) != 1
		or int(constraints.get("max_operations", -1)) != 1
		or not bool(constraints.get("requires_failed_evidence", false))
		or not bool(constraints.get("cas_required", false))
		or not bool(constraints.get("requires_student_confirmation", false))
		or bool(constraints.get("auto_build", true))
		or bool(constraints.get("auto_activate", true))
		or bool(constraints.get("auto_run", true))
	):
		return
	skill_patch_capability = value.duplicate(true)
	patch_decisions_enabled = true


func register_visible_patch_failure(interaction: Dictionary) -> bool:
	visible_patch_failure.clear()
	patch_failure_recovery_status.clear()
	if not patch_decisions_enabled:
		return false
	var store := _client_store()
	var feedback: Variant = interaction.get("feedback")
	var response_type := str(interaction.get("response_type", ""))
	var role := str(interaction.get("role", ""))
	var evidence_refs: Variant = feedback.get("evidence_refs") if feedback is Dictionary else null
	var interaction_id := str(interaction.get("interaction_id", ""))
	var session_id := str(authoritative_session.get("session_id", ""))
	var contract_validation := ProductInteractionGateway.new(null)._validate_interaction(
		interaction, session_id, interaction_id,
	)
	var candidate_invalid: bool = (
		store == null
		or session_id.is_empty()
		or str(interaction.get("session_id", "")) != session_id
		or int(interaction.get("sequence", -1)) < 1
		or int(interaction.get("sequence", -1)) > store.last_interaction_sequence
		or role not in ["teaching_agent", "bug_agent"]
		or response_type not in ["question", "hint", "message"]
		or interaction.get("skill_patch") != null
		or interaction.get("patch_decision") != null
		or not feedback is Dictionary
		or str(feedback.get("source", "")) != "provider"
		or bool(feedback.get("degraded", true))
		or feedback.get("fallback_reason") != null
		or str(feedback.get("run_id", "")).is_empty()
		or not evidence_refs is Array
		or evidence_refs.is_empty()
		or not contract_validation.get("ok", false)
		or interaction.get("request_context", {}).get("actor") != authority_context.get("actor")
		or interaction.get("request_context", {}).get("content_ref") != authority_context.get("content_ref")
	)
	if candidate_invalid:
		return false
	if (
		bool(store.objective_result.get("objective_succeeded", true))
		or str(store.objective_result.get("run_id", "")) != str(feedback.get("run_id", ""))
	):
		patch_failure_recovery_status = _local_failure(
			"SKILL_PATCH_BUILD_PROVENANCE_NOT_PROVEN_AFTER_RESTART",
			"The failed Interaction was recovered, but the current public Workspace/Interaction wire does not prove its exact Build provenance after restart; no Patch request was sent.",
		)
		return false
	visible_patch_failure = interaction.duplicate(true)
	return true


func can_request_ai_patch() -> bool:
	return patch_decisions_enabled and not visible_patch_failure.is_empty()


func patch_failure_recovery_result() -> Dictionary:
	return patch_failure_recovery_status.duplicate(true)


func request_ai_patch() -> Dictionary:
	if not can_request_ai_patch():
		capability_unavailable.emit("Skill Patch", "AI code changes require one visible contract-valid objective failure; Backend owns threshold eligibility.")
		return _local_failure("SKILL_PATCH_REQUEST_UNAVAILABLE", "No contract-valid visible objective failure is selected.")
	var interaction_id := str(visible_patch_failure.interaction_id)
	return await _request_skill_patch_proposal({
		"type": "UI_ACTION",
		"action_id": "request_ai_patch",
		"selection_id": interaction_id,
	})


func validate_minimal_skill_patch_interaction(interaction: Dictionary) -> Dictionary:
	if not patch_decisions_enabled:
		return _local_failure("PATCH_DECISION_EXCLUDED", "Skill Patch capability is disabled.")
	var store := _client_store()
	var patch: Variant = interaction.get("skill_patch")
	var feedback: Variant = interaction.get("feedback")
	var session_id := str(authoritative_session.get("session_id", ""))
	var interaction_id := str(interaction.get("interaction_id", ""))
	var contract_validation := ProductInteractionGateway.new(null)._validate_interaction(
		interaction, session_id, interaction_id,
	)
	if (
		store == null
		or store.draft.is_empty()
		or session_id.is_empty()
		or str(interaction.get("session_id", "")) != session_id
		or str(interaction.get("role", "")) != "teaching_agent"
		or str(interaction.get("response_type", "")) != "skill_patch"
		or int(interaction.get("hint_level", -1)) != 4
		or interaction.get("patch_decision") != null
		or not patch is Dictionary
		or not feedback is Dictionary
		or not contract_validation.get("ok", false)
		or interaction.get("request_context", {}).get("actor") != authority_context.get("actor")
		or interaction.get("request_context", {}).get("content_ref") != authority_context.get("content_ref")
	):
		return _local_failure("PATCH_PROPOSAL_AUTHORITY_INVALID", "Skill Patch proposal does not close through one undecided teaching interaction.")
	var draft: Dictionary = store.draft
	var bundle: Variant = draft.get("source_bundle")
	var operations: Variant = patch.get("operations")
	var evidence_refs: Variant = patch.get("evidence_refs")
	if (
		str(patch.get("interaction_id", "")) != str(interaction.get("interaction_id", ""))
		or str(patch.get("session_id", "")) != str(interaction.get("session_id", ""))
		or str(patch.get("turn_id", "")) != str(interaction.get("turn_id", ""))
		or str(patch.get("draft_id", "")) != str(draft.get("draft_id", ""))
		or str(patch.get("skill_id", "")) != str(draft.get("skill_id", ""))
		or int(patch.get("base_draft_revision", -1)) != int(draft.get("revision", -2))
		or str(patch.get("base_draft_sha256", "")) != str(draft.get("draft_sha256", ""))
		or not bool(patch.get("requires_student_confirmation", false))
		or not operations is Array
		or operations.size() != 1
		or not evidence_refs is Array
		or evidence_refs.is_empty()
		or evidence_refs != feedback.get("evidence_refs")
		or not bundle is Dictionary
		or not bundle.get("files") is Array
	):
		return _local_failure("PATCH_PROPOSAL_CAS_INVALID", "Skill Patch proposal Draft/Evidence/CAS identity is stale or incomplete.")
	var operation: Variant = operations[0]
	var entrypoint := str(bundle.get("entrypoint", ""))
	if not operation is Dictionary or operation.size() != 5:
		return _local_failure("PATCH_PROPOSAL_OPERATION_INVALID", "Skill Patch must contain exactly one closed UPSERT_FILE.")
	for field in ["operation", "path", "previous_content_sha256", "content", "content_sha256"]:
		if not operation.has(field):
			return _local_failure("PATCH_PROPOSAL_OPERATION_INVALID", "Skill Patch UPSERT_FILE is not closed.")
	var base_file: Dictionary = {}
	for file in bundle.files:
		if file is Dictionary and str(file.get("path", "")) == entrypoint:
			base_file = file
			break
	if (
		base_file.is_empty()
		or str(operation.operation) != "UPSERT_FILE"
		or str(operation.path) != entrypoint
		or str(operation.previous_content_sha256) != str(base_file.get("content_sha256", ""))
		or str(base_file.get("content", "")).sha256_text() != str(operation.previous_content_sha256)
		or typeof(operation.content) != TYPE_STRING
		or str(operation.content).sha256_text() != str(operation.content_sha256)
	):
		return _local_failure("PATCH_PROPOSAL_OPERATION_INVALID", "Skill Patch UPSERT_FILE does not target the exact current entrypoint bytes.")
	return {"ok": true, "status": 200, "headers": {}, "value": interaction.duplicate(true)}


func set_world_playback_speed(speed: float) -> bool:
	return (
		world_presentation_enabled
		and world_event_player != null
		and world_event_player.has_method("set_speed_multiplier")
		and bool(world_event_player.call("set_speed_multiplier", speed))
	)


func skip_world_playback() -> void:
	if world_presentation_enabled and world_event_player != null and world_event_player.has_method("skip"):
		world_event_player.call("skip")


func can_replay_world_result() -> bool:
	return (
		world_presentation_enabled
		and world_event_player != null
		and not _last_presentation_pre_snapshot.is_empty()
		and not _last_presentation_final_snapshot.is_empty()
	)


func replay_world_result() -> Dictionary:
	var store := _client_store()
	if store == null or not can_replay_world_result() or store.flow_state != WalnutClientStore.FlowState.COMPLETED:
		return _local_failure("PRESENTATION_REPLAY_UNAVAILABLE", "The current verified World result is not available to replay.")
	# Replay is a temporary renderer concern. ClientStore remains the final
	# PostgreSQL-backed authority for the whole replay, including cancellation.
	var final_authority: Dictionary = store.world_snapshot.duplicate(true)
	if final_authority != _last_presentation_final_snapshot:
		return _local_failure("PRESENTATION_REPLAY_AUTHORITY_MISMATCH", "The cached result is no longer the current authoritative World Snapshot.")
	if not _project_replay_snapshot(_last_presentation_pre_snapshot):
		_project_replay_snapshot(final_authority)
		return _local_failure("PRESENTATION_REPLAY_RESET_FAILED", "The verified pre-Run Snapshot could not be projected for replay.")
	store.set_flow(WalnutClientStore.FlowState.PLAYING)
	world_playback_state_changed.emit("PLAYING")
	var replay: Dictionary = await world_event_player.call("replay_current_result", world_presentation_renderer)
	var final_projection_restored := _project_replay_snapshot(final_authority)
	if not final_projection_restored or store.world_snapshot != final_authority:
		store.report_error(_local_error("PRESENTATION_REPLAY_RECOVERY_FAILED", "Replay could not restore the authoritative final Snapshot."))
		return _local_failure("PRESENTATION_REPLAY_RECOVERY_FAILED", "Replay could not restore the authoritative final Snapshot.")
	if not replay.get("ok", false):
		store.report_error(replay.get("error", _local_error("PRESENTATION_REPLAY_FAILED", "World replay failed closed.")))
		return replay
	store.set_flow(WalnutClientStore.FlowState.COMPLETED)
	world_playback_state_changed.emit("COMPLETED")
	return replay


## Cold-start and process-restart recovery never replays historical actions.
## It performs only the additive GET, verifies the page Snapshot head against
## the already recovered authority, then advances the in-memory display cursor.
func synchronize_world_presentation_cursor() -> Dictionary:
	if not world_presentation_enabled:
		return {"ok": true, "status": 200, "headers": {}, "value": {"enabled": false, "high_watermark": 0}}
	var store := _client_store()
	if (
		store == null
		or store.world_snapshot.is_empty()
		or world_presentation_gateway == null
		or not world_presentation_gateway.has_method("get_world_presentation_events")
		or world_event_player == null
		or not world_event_player.has_method("set_cursor")
	):
		return _local_failure("PRESENTATION_SYNCHRONIZATION_UNAVAILABLE", "World presentation startup authority is unavailable.")
	var snapshot: Dictionary = store.world_snapshot
	var cursor := 0
	var high_watermark := -1
	var pages := 0
	var previous_event: Dictionary = {}
	var last_headers: Dictionary = {}
	while pages < 1000:
		pages += 1
		var page_result: Dictionary = await world_presentation_gateway.get_world_presentation_events(
			RequestContextFactory.new_wire_attempt(),
			str(snapshot.world_id),
			cursor,
			500,
		)
		if not page_result.get("ok", false):
			return page_result
		last_headers = page_result.get("headers", {}).duplicate(true)
		var page: Dictionary = page_result.value
		if not _presentation_page_matches_authority(page):
			return _local_failure("PRESENTATION_STARTUP_AUTHORITY_MISMATCH", "Presentation page actor/content authority disagrees with Bootstrap and AgentSession.")
		if not _presentation_page_matches_snapshot(page, snapshot):
			return _local_failure("PRESENTATION_STARTUP_SNAPSHOT_MISMATCH", "Presentation high watermark disagrees with the recovered authoritative Snapshot.")
		if high_watermark < 0:
			high_watermark = int(page.presentation_high_watermark)
		elif high_watermark != int(page.presentation_high_watermark):
			return _local_failure("PRESENTATION_HIGH_WATERMARK_CHANGED", "Presentation high watermark changed during cold synchronization.")
		var batch_validation: Dictionary = world_event_player.call(
			"validate_batch", page.events, cursor, previous_event,
		)
		if not batch_validation.get("ok", false):
			return batch_validation
		if not page.events.is_empty():
			previous_event = page.events[-1].duplicate(true)
		var next_cursor := int(page.next_after_sequence)
		if bool(page.has_more):
			if page.events.is_empty() or next_cursor <= cursor:
				return _local_failure("PRESENTATION_CURSOR_STALLED", "Cold presentation synchronization made no progress.")
			cursor = next_cursor
			continue
		cursor = next_cursor
		break
	if pages >= 1000 and cursor < high_watermark:
		return _local_failure("PRESENTATION_PAGE_LIMIT", "Cold presentation synchronization exceeded its safety bound.")
	if high_watermark < 0 or cursor != high_watermark:
		return _local_failure("PRESENTATION_SEQUENCE_GAP", "Cold presentation synchronization did not validate every historical row.")
	world_event_player.call("set_cursor", high_watermark, previous_event)
	return {
		"ok": true,
		"status": 200,
		"headers": last_headers,
		"value": {"enabled": true, "high_watermark": high_watermark, "pages": pages},
	}


func configure_polling(value: Dictionary) -> void:
	polling_settings = value.duplicate(true)


## StudentBootstrap is the only source of build policy, registry scope/revision
## and the active immutable Skill tuple. UI state is never consulted.
func configure_authority(bootstrap: Dictionary, session: Dictionary = {}) -> void:
	var previous_certified_build := certified_build.duplicate(true)
	var previous_certified_build_draft_authority := certified_build_draft_authority.duplicate(true)
	var previous_authoritative_session := authoritative_session.duplicate(true)
	var previous_active_skill_tuple := active_skill_tuple.duplicate(true)
	authority_context.clear()
	draft_context.clear()
	build_context.clear()
	activation_context.clear()
	certified_build.clear()
	certified_build_draft_authority.clear()
	active_activation.clear()
	active_skill_tuple.clear()
	var actor: Variant = bootstrap.get("actor")
	var content_ref: Variant = bootstrap.get("content")
	if actor is Dictionary and content_ref is Dictionary:
		authority_context = {
			"actor": actor.duplicate(true),
			"content_ref": content_ref.duplicate(true),
		}
		draft_context = authority_context.duplicate(true)
	var build: Variant = bootstrap.get("build")
	if build is Dictionary:
		build_context = {
			"build_policy_id": build.get("build_policy_id"),
			"compiler_profile": build.get("compiler_profile"),
			"compiler_version": build.get("compiler_version"),
			"sandbox_image_digest": build.get("sandbox_image_digest"),
			"test_suite_version": build.get("test_suite_version"),
			"requested_capabilities": build.get("allowed_capabilities", []).duplicate(true),
			"max_source_files": build.get("max_source_files"),
			"max_source_bytes": build.get("max_source_bytes"),
		}
	var activation: Variant = bootstrap.get("activation")
	if activation is Dictionary and activation.get("scope") is Dictionary:
		activation_context = {
			"expected_registry_revision": int(activation.get("registry_revision", -1)),
			"world_id": activation.scope.get("world_id"),
			"agent_profile_id": activation.scope.get("agent_profile_id"),
		}
		var active: Variant = activation.get("active")
		var store := _client_store()
		var patch_invalidated := store != null and not store.patch_activation_invalidation.is_empty()
		active_skill_tuple = active.duplicate(true) if active is Dictionary and not patch_invalidated else {}
		active_activation = active_skill_tuple.duplicate(true)
	authoritative_session = session.duplicate(true)
	if _can_preserve_certified_build_after_authority_refresh(
		previous_certified_build,
		previous_certified_build_draft_authority,
		previous_authoritative_session,
		previous_active_skill_tuple,
	):
		certified_build = previous_certified_build
		certified_build_draft_authority = previous_certified_build_draft_authority


func begin_startup_authority_revalidation() -> void:
	_startup_authority_guard_enabled = true
	_startup_authority_revalidation_pending = true


func set_startup_authority_ready(value: bool) -> void:
	_startup_authority_revalidation_pending = not value


func configure_draft_context(value: Dictionary) -> void:
	draft_context = value.duplicate(true)


func configure_build_context(value: Dictionary) -> void:
	build_context = value.duplicate(true)


func configure_activation_context(value: Dictionary) -> void:
	activation_context = value.duplicate(true)


func request_save() -> Dictionary:
	var readiness := _student_action_readiness("Save")
	if not readiness.get("ok", false):
		return readiness
	if product_gateway == null or not product_gateway.has_method("upsert_draft"):
		capability_unavailable.emit("Product Draft", "Product Draft gateway is not configured.")
		return _local_failure("PRODUCT_GATEWAY_UNAVAILABLE", "Product Draft gateway is not configured.")
	var store := _client_store()
	if store == null or draft_context.is_empty():
		capability_unavailable.emit("Product Draft", "Canonical Draft context has not been recovered.")
		return _local_failure("DRAFT_CONTEXT_UNAVAILABLE", "Canonical Draft context is not available.")
	# A prior response-loss envelope owns this slot. Reconcile it before even
	# deriving a new CAS identity from the current editor/Draft state.
	var prior_envelope := store.get_pending_operation(PENDING_DRAFT_SAVE_SLOT)
	if not prior_envelope.is_empty():
		var local_source_before_recovery := store.local_source
		var pending_source := _source_bundle_entrypoint_source(
			prior_envelope.get("request", {}).get("source_bundle", {}),
		)
		var has_newer_local_edit := (
			not pending_source.is_empty()
			and local_source_before_recovery != pending_source
		)
		var pending_recovery: Dictionary = await recover_pending_draft_save_operations()
		if not pending_recovery.get("ok", false):
			return pending_recovery
		var terminal_error: Variant = pending_recovery.get("value", {}).get("terminal_error")
		if terminal_error is Dictionary and not terminal_error.is_empty():
			store.record_draft_save_failed(terminal_error)
		elif has_newer_local_edit and not store.draft.is_empty():
			# Reconciliation closes only the older durable identity. A later editor
			# change must remain DIRTY so a subsequent save can persist a new slot
			# identity instead of being overwritten by the old canonical response.
			store.set_draft_preserving_local_source(
				store.draft,
				local_source_before_recovery,
			)
		return pending_recovery
	var current_draft: Dictionary = store.draft
	if current_draft.is_empty():
		return _local_failure("DRAFT_UNAVAILABLE", "Canonical Draft is not available.")
	var submitted_source := store.local_source
	var source_bundle := _build_source_bundle(current_draft, submitted_source)
	if source_bundle.is_empty():
		return _local_failure("DRAFT_SOURCE_INVALID", "Canonical Draft has no editable entrypoint source file.")
	var request := {
		"session_id": current_draft.session_id,
		"draft_id": current_draft.draft_id,
		"skill_id": current_draft.skill_id,
		"content_ref": current_draft.content_ref.duplicate(true),
		"base_revision": current_draft.revision,
		"base_draft_sha256": current_draft.draft_sha256,
		"display_name": current_draft.display_name,
		"source_bundle": source_bundle,
		"client_saved_at": RequestContextFactory.utc_now(),
	}
	var identity := "%s:%s:%s" % [
		current_draft.draft_id,
		current_draft.revision,
		_source_identity(source_bundle),
	]
	var envelope_result := store.ensure_pending_operation("draft_save", identity, {
		"idempotency_key": RequestContextFactory.idempotency_key_for("upsertProductSkillDraft", identity),
		"request": request,
	})
	if not envelope_result.get("ok", false):
		store.record_draft_save_failed(envelope_result.get("error", _local_error(
			"PENDING_OPERATION_PERSISTENCE_FAILED",
			"The Draft save envelope could not be persisted.",
		)))
		return envelope_result
	var envelope: Dictionary = envelope_result.value
	request = envelope.request
	store.mark_draft_saving()
	var result: Dictionary = await product_gateway.upsert_draft(
		_new_request_context(),
		str(current_draft.session_id),
		str(current_draft.draft_id),
		str(envelope.idempotency_key),
		request,
	)
	var reconciliation := _product_write_reconciliation(
		result,
		"SKILL_DRAFT",
		str(current_draft.session_id),
		str(current_draft.draft_id),
	)
	if not reconciliation.is_empty():
		var canonical: Dictionary = await product_gateway.get_draft(
			_new_request_context(),
			str(current_draft.session_id),
			str(current_draft.draft_id),
		)
		if canonical.get("ok", false) and _canonical_draft_matches_write(canonical.value, request):
			store.clear_pending_operation("draft_save")
			_apply_saved_draft(store, canonical.value, submitted_source)
			return canonical
		result = _local_failure(
			"PRODUCT_DRAFT_RECONCILIATION_FAILED",
			"The durable Draft write could not be reconciled with its canonical resource.",
		)
	if not result.get("ok", false):
		if int(result.get("status", 0)) == 409:
			store.clear_pending_operation("draft_save")
			store.record_draft_conflict(current_draft)
		elif store.local_source != submitted_source:
			store.report_error(result.get("error", {}))
		else:
			store.record_draft_save_failed(result.get("error", {}))
		return result
	store.clear_pending_operation("draft_save")
	_apply_saved_draft(store, result.value, submitted_source)
	return result


## A Draft PUT can commit before the HTTP response is lost.  AppRoot calls this
## only after it has recovered the current canonical Workspace/Draft, so the
## first operation is always a GET: an already-applied write is never sent
## again.  A non-matching Draft is retried solely with the persisted request
## Dictionary and Idempotency-Key; neither its timestamp nor its CAS base is
## reconstructed from the post-restart ClientStore state.
func recover_pending_draft_save_operations() -> Dictionary:
	var store := _client_store()
	if store == null:
		return _local_failure("PENDING_DRAFT_STORE_UNAVAILABLE", "ClientStore is unavailable for pending Draft recovery.")
	var integrity: Dictionary = store.validate_pending_operation(PENDING_DRAFT_SAVE_SLOT)
	if not integrity.get("ok", false):
		return integrity
	var envelope: Dictionary = integrity.get("value", {})
	if envelope.is_empty():
		return {
			"ok": true,
			"status": 200,
			"headers": {},
			"value": {"had_pending": false, "outcome": "NONE", "terminal_error": null},
		}
	if product_gateway == null or not product_gateway.has_method("get_draft") or not product_gateway.has_method("upsert_draft"):
		return _local_failure("PENDING_DRAFT_GATEWAY_UNAVAILABLE", "Product Draft gateway is unavailable for pending Draft recovery.")
	var context_result := _pending_draft_save_envelope_context(envelope)
	if not context_result.get("ok", false):
		return context_result
	var context: Dictionary = context_result.value

	# The canonical GET is the sole authority for recognizing a write whose
	# response disappeared.  It is intentionally issued before any replay.
	var canonical_result: Dictionary = await product_gateway.get_draft(
		_new_request_context(), str(context.session_id), str(context.draft_id),
	)
	if canonical_result.get("ok", false):
		if _canonical_draft_matches_write(canonical_result.value, context.request):
			store.clear_pending_operation(PENDING_DRAFT_SAVE_SLOT)
			store.set_draft(canonical_result.value)
			return _pending_draft_recovery_success("RECONCILED_EXISTING", canonical_result.value)
	elif _retryable_result(canonical_result):
		# The envelope is deliberately retained for the next process/startup; a
		# failure to read authority is never grounds for issuing a changed PUT.
		return canonical_result
	else:
		return _pending_draft_terminal_result(
			"DRAFT_RECOVERY_GET_TERMINAL_FAILURE",
			"The canonical Draft could not be read while reconciling a persisted save.",
		)

	# The prior GET was authoritative but did not contain this request.  Replay
	# exactly the JSON-origin Dictionary and Idempotency-Key that were persisted
	# before the original HTTP attempt.
	var write_result: Dictionary = await product_gateway.upsert_draft(
		_new_request_context(),
		str(context.session_id),
		str(context.draft_id),
		str(context.idempotency_key),
		context.request,
	)
	var reconciliation := _product_write_reconciliation(
		write_result,
		"SKILL_DRAFT",
		str(context.session_id),
		str(context.draft_id),
	)
	if not write_result.get("ok", false) and reconciliation.is_empty():
		if _retryable_result(write_result):
			return write_result
		return _pending_draft_terminal_result(
			"DRAFT_RECOVERY_PUT_TERMINAL_FAILURE",
			"The persisted Draft save reached a terminal Gateway failure.",
		)
	if write_result.get("ok", false) and not _canonical_draft_matches_write(write_result.value, context.request):
		return _local_failure(
			"DRAFT_RECOVERY_PUT_MISMATCH",
			"The Draft PUT response does not match its persisted immutable request.",
		)

	# A completed/reconciled PUT must still agree with an independent canonical
	# GET before the persisted envelope can be discarded.
	canonical_result = await product_gateway.get_draft(
		_new_request_context(), str(context.session_id), str(context.draft_id),
	)
	if not canonical_result.get("ok", false):
		if _retryable_result(canonical_result):
			return canonical_result
		return _pending_draft_terminal_result(
			"DRAFT_RECOVERY_VERIFICATION_TERMINAL_FAILURE",
			"The canonical Draft could not verify a completed persisted save.",
		)
	if not _canonical_draft_matches_write(canonical_result.value, context.request):
		return _local_failure(
			"DRAFT_RECOVERY_CANONICAL_MISMATCH",
			"Canonical Draft does not match the persisted immutable save request after reconciliation.",
		)
	store.clear_pending_operation(PENDING_DRAFT_SAVE_SLOT)
	store.set_draft(canonical_result.value)
	return _pending_draft_recovery_success("REPLAYED_AND_VERIFIED", canonical_result.value)


func _pending_draft_save_envelope_context(envelope: Dictionary) -> Dictionary:
	var request_value: Variant = envelope.get("request")
	if not request_value is Dictionary:
		return _local_failure("PENDING_DRAFT_ENVELOPE_INVALID", "Pending Draft envelope has no original request body.")
	var request: Dictionary = request_value
	var required := [
		"session_id", "draft_id", "skill_id", "content_ref", "base_revision",
		"base_draft_sha256", "display_name", "source_bundle", "client_saved_at",
	]
	if request.size() != required.size():
		return _local_failure("PENDING_DRAFT_ENVELOPE_INVALID", "Pending Draft request body is not closed.")
	for field in required:
		if not request.has(field):
			return _local_failure("PENDING_DRAFT_ENVELOPE_INVALID", "Pending Draft request body is incomplete.")
	var session_id := str(request.get("session_id", ""))
	var draft_id := str(request.get("draft_id", ""))
	var skill_id := str(request.get("skill_id", ""))
	var idempotency_key := str(envelope.get("idempotency_key", ""))
	var source_bundle: Variant = request.get("source_bundle")
	if (
		not ContractValidator.validate_identifier(session_id).ok
		or not ContractValidator.validate_identifier(draft_id).ok
		or not ContractValidator.validate_identifier(skill_id).ok
		or idempotency_key.length() < 16
		or not request.get("content_ref") is Dictionary
		or typeof(request.get("display_name")) != TYPE_STRING
		or str(request.get("display_name", "")).is_empty()
		or typeof(request.get("client_saved_at")) != TYPE_STRING
		or str(request.get("client_saved_at", "")).is_empty()
		or int(request.get("base_revision", -1)) < 0
		or not source_bundle is Dictionary
		or not source_bundle.get("files") is Array
	):
		return _local_failure("PENDING_DRAFT_ENVELOPE_INVALID", "Pending Draft identity or immutable request body is invalid.")
	var base_hash: Variant = request.get("base_draft_sha256")
	if (
		(int(request.base_revision) == 0 and base_hash != null)
		or (int(request.base_revision) > 0 and (typeof(base_hash) != TYPE_STRING or str(base_hash).length() != 64))
	):
		return _local_failure("PENDING_DRAFT_ENVELOPE_INVALID", "Pending Draft CAS base is invalid.")
	if not authoritative_session.is_empty() and session_id != str(authoritative_session.get("session_id", "")):
		return _local_failure("PENDING_DRAFT_SESSION_MISMATCH", "Pending Draft does not belong to the authoritative Session.")
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {
			"session_id": session_id,
			"draft_id": draft_id,
			"idempotency_key": idempotency_key,
			"request": request,
		},
	}


func _pending_draft_recovery_success(outcome: String, draft: Dictionary) -> Dictionary:
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {
			"had_pending": true,
			"outcome": outcome,
			"draft": draft.duplicate(true),
			"terminal_error": null,
		},
	}


func _pending_draft_terminal_result(code: String, message: String) -> Dictionary:
	var store := _client_store()
	if store != null:
		store.clear_pending_operation(PENDING_DRAFT_SAVE_SLOT)
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {
			"had_pending": true,
			"outcome": "TERMINAL_FAILURE",
			"draft": {},
			"terminal_error": _local_error(code, message),
		},
	}


## This is the direct Run action for an already-active tuple. It may save a
## Draft, but it never invents or replaces activation authority.
func request_submit_and_run() -> Dictionary:
	var store := _client_store()
	if store == null:
		return {"ok": false, "stage": "SAVE", "message": "Client state is unavailable."}
	if not store.patch_activation_invalidation.is_empty():
		return {
			"ok": false,
			"stage": "ACTIVATE",
			"message": "The accepted Draft invalidated the previous Activation. Build and Activate the new Draft before Run.",
		}
	var readiness := _student_action_readiness("Run")
	if not readiness.get("ok", false):
		return {"ok": false, "stage": "AUTHORITY", "message": str(readiness.get("error", {}).get("message", "Startup authority is not ready."))}
	if store.draft_state != WalnutClientStore.DraftState.CLEAN:
		var save_result: Dictionary = await request_save()
		if not save_result.get("ok", false) or store.draft_state != WalnutClientStore.DraftState.CLEAN:
			return {"ok": false, "stage": "SAVE", "message": "Draft save did not close; Run was not submitted."}
	if active_skill_tuple.is_empty():
		return {"ok": false, "stage": "ACTIVATE", "message": "No publicly active exact Skill tuple is available."}
	await request_turn()
	if store.flow_state != WalnutClientStore.FlowState.COMPLETED:
		return {"ok": false, "stage": "RUN", "message": "Run closure did not complete."}
	return {"ok": true, "stage": "RUN", "message": "Run, Evidence, HTTP world recovery and feedback are closed."}


func request_build() -> void:
	if not _student_action_readiness("Build").get("ok", false):
		return
	if game_gateway == null or not game_gateway.has_method("submit_skill_build") or not game_gateway.has_method("get_skill_build"):
		capability_unavailable.emit("Build", "Game gateway lacks Build support.")
		return
	var store := _client_store()
	if store == null or draft_context.is_empty() or not _has_build_context():
		capability_unavailable.emit("Build", "Build requires recovered Draft and public build authority.")
		return
	if not store.clear_patch_failure_recovery_authority():
		store.report_error(_local_error(
			"SKILL_PATCH_FAILURE_AUTHORITY_CLEAR_FAILED",
			"A new explicit Build cannot start until the prior failed-Run authority is durably retired.",
		))
		return
	visible_patch_failure.clear()
	# Each explicit Build action must produce its own certified terminal
	# authority; a previous resource cannot be activated after this action fails.
	certified_build.clear()
	certified_build_draft_authority.clear()
	if store.draft_state != WalnutClientStore.DraftState.CLEAN:
		var save_result: Dictionary = await request_save()
		if not save_result.get("ok", false) or store.draft_state != WalnutClientStore.DraftState.CLEAN:
			return
	var source_bundle := _build_source_bundle(store.draft, store.local_source)
	if source_bundle.is_empty():
		store.report_error(_local_error("BUILD_SOURCE_INVALID", "Canonical Draft has no editable entrypoint source file."))
		return
	var request := {
		"skill_id": store.draft.skill_id,
		"display_name": store.draft.display_name,
		"client_draft_revision": store.draft.revision,
		"source_bundle": source_bundle,
		"compiler_profile": build_context.compiler_profile,
		"test_suite_version": build_context.test_suite_version,
		"requested_capabilities": build_context.requested_capabilities.duplicate(true),
	}
	store.set_flow(WalnutClientStore.FlowState.BUILDING)
	var key := RequestContextFactory.idempotency_key_for(
		"createSkillBuild",
		"%s:%s:%s" % [store.draft.draft_id, store.draft.revision, _source_identity(source_bundle)],
	)
	var submission: Dictionary = await game_gateway.submit_skill_build(
		_new_request_context(), key, request,
	)
	var poller := _new_poller()
	var command_result: Dictionary = await poller.reconcile({}, submission)
	if not command_result.get("ok", false):
		store.report_error(command_result.get("error", _local_error("BUILD_COMMAND_FAILED", "Build command reconciliation failed.")))
		return
	var command: Dictionary = command_result.value
	if str(command.get("status", "")) != "APPLIED":
		store.set_flow(WalnutClientStore.FlowState.BUILD_FAILED)
		return
	var resource: Variant = command.get("result")
	if not resource is Dictionary or str(resource.get("resource_type", "")) != "SKILL_BUILD":
		store.report_error(_local_error("BUILD_COMMAND_INVALID", "Build command did not resolve to a SkillBuild resource."))
		return
	var build_result: Dictionary = await poller.poll_resource(
		{}, "get_skill_build", str(resource.get("resource_id", "")), "build_id",
	)
	if not build_result.get("ok", false):
		store.report_error(build_result.get("error", _local_error("BUILD_READ_FAILED", "SkillBuild could not be recovered.")))
		return
	var build: Dictionary = build_result.value
	if str(build.get("skill_id", "")) != str(store.draft.skill_id):
		store.report_error(_local_error("BUILD_IDENTITY_MISMATCH", "SkillBuild skill_id does not match the saved Draft."))
		return
	var source_hash_guard := _verify_build_source_hash(build, source_bundle)
	if not source_hash_guard.get("ok", false):
		store.report_error(source_hash_guard.error)
		return
	build_resolved.emit(build.duplicate(true))
	if str(build.get("status", "")) == "CERTIFIED":
		certified_build = build.duplicate(true)
		certified_build_draft_authority = {
			"build_id": str(build.build_id),
			"session_id": str(store.draft.session_id),
			"draft_id": str(store.draft.draft_id),
			"skill_id": str(store.draft.skill_id),
			"draft_revision": int(store.draft.revision),
			"draft_sha256": str(store.draft.draft_sha256),
			"source_bundle_sha256": _canonical_source_bundle_sha256(source_bundle),
		}
		store.set_flow(WalnutClientStore.FlowState.CERTIFIED)
		store.set_objective_result({"summary": "Build and certification completed. Activation remains an explicit action."})
	else:
		store.set_flow(WalnutClientStore.FlowState.BUILD_FAILED)


func request_activation() -> void:
	if not _student_action_readiness("Activation").get("ok", false):
		return
	if game_gateway == null or not game_gateway.has_method("activate_skill_version") or not game_gateway.has_method("get_skill_activation"):
		capability_unavailable.emit("Activation", "Game gateway lacks Activation support.")
		return
	var store := _client_store()
	if store == null or certified_build.is_empty() or not _has_activation_context():
		capability_unavailable.emit("Activation", "Activation requires a certified Build and public registry authority.")
		return
	var artifact: Variant = certified_build.get("artifact")
	var certification: Variant = certified_build.get("certification")
	if not artifact is Dictionary or not certification is Dictionary:
		store.report_error(_local_error("ACTIVATION_BUILD_INVALID", "Certified Build lacks immutable artifact/certification identity."))
		return
	var request := {
		"expected_registry_revision": int(activation_context.expected_registry_revision),
		"activation_scope": {
			"world_id": activation_context.world_id,
			"agent_profile_id": activation_context.agent_profile_id,
		},
	}
	if activation_context.has("reason"):
		request["reason"] = activation_context.reason
	store.set_flow(WalnutClientStore.FlowState.ACTIVATING)
	var key := RequestContextFactory.idempotency_key_for(
		"activateSkillVersion",
		"%s:%s:%s" % [certified_build.skill_version_id, activation_context.world_id, activation_context.expected_registry_revision],
	)
	var submission: Dictionary = await game_gateway.activate_skill_version(
		_new_request_context(), str(certified_build.skill_version_id), key, request,
	)
	var command_result: Dictionary = await _new_poller().reconcile({}, submission)
	if not command_result.get("ok", false):
		store.report_error(command_result.get("error", _local_error("ACTIVATION_COMMAND_FAILED", "Activation reconciliation failed.")))
		return
	var command: Dictionary = command_result.value
	if str(command.get("status", "")) != "APPLIED":
		store.report_error(_local_error("ACTIVATION_COMMAND_REJECTED", "Activation command did not reach APPLIED."))
		return
	var resource: Variant = command.get("result")
	if not resource is Dictionary or str(resource.get("resource_type", "")) != "SKILL_ACTIVATION":
		store.report_error(_local_error("ACTIVATION_COMMAND_INVALID", "Activation command did not name a SkillActivation."))
		return
	var activation_result: Dictionary = await game_gateway.get_skill_activation(
		_new_request_context(), str(resource.get("resource_id", "")),
	)
	if not activation_result.get("ok", false):
		store.report_error(activation_result.get("error", _local_error("ACTIVATION_READ_FAILED", "SkillActivation could not be recovered.")))
		return
	var activation: Dictionary = activation_result.value
	if not _activation_matches_build(activation):
		store.report_error(_local_error("ACTIVATION_IDENTITY_MISMATCH", "SkillActivation does not match the certified Build and public scope."))
		return
	var next_active_tuple := _tuple_from_activation(activation)
	if not store.update_activation_authority(
		activation.activation_scope,
		int(activation.registry_revision),
		next_active_tuple,
		certified_build_draft_authority,
	):
		store.report_error(_local_error(
			"ACTIVATION_PATCH_PROVENANCE_MISMATCH",
			"Activation cannot clear the accepted-Draft invalidation without the exact fresh Build provenance.",
		))
		return
	active_activation = activation.duplicate(true)
	active_skill_tuple = next_active_tuple
	activation_context.expected_registry_revision = int(activation.registry_revision)
	store.set_flow(WalnutClientStore.FlowState.ACTIVE)
	store.set_objective_result({"summary": "The exact Skill tuple is active in the public registry."})


func request_hint(message: String = "Please give me the next hint.") -> void:
	if not _student_action_readiness("Hint").get("ok", false):
		return
	await request_turn({"type": "MESSAGE", "text": message, "locale": "zh-CN"}, false)


func _request_skill_patch_proposal(turn_input: Dictionary) -> Dictionary:
	var readiness := _student_action_readiness("Skill Patch request")
	if not readiness.get("ok", false):
		return readiness
	var store := _client_store()
	if (
		store == null
		or game_gateway == null
		or not game_gateway.has_method("submit_agent_turn")
		or not game_gateway.has_method("get_command")
		or product_gateway == null
		or not product_gateway.has_method("get_interaction")
		or not product_gateway.has_method("list_interactions")
	):
		return _local_failure("SKILL_PATCH_REQUEST_GATEWAY_UNAVAILABLE", "Skill Patch request requires public Turn/Command/Interaction GET Gateways.")
	var existing := await recover_pending_patch_request()
	if not existing.get("ok", false):
		return existing
	if bool(existing.get("value", {}).get("had_pending", false)):
		return existing
	var workspace: Dictionary = store.workspace
	var session: Variant = workspace.get("session")
	var world: Dictionary = store.world_snapshot
	if (
		not session is Dictionary
		or str(session.get("status", "")) != "ACTIVE"
		or str(session.get("session_id", "")) != str(authoritative_session.get("session_id", ""))
		or world.is_empty()
		or not _valid_active_tuple(active_skill_tuple)
		or not _closed_dictionary(turn_input, ["type", "action_id", "selection_id"])
		or str(turn_input.type) != "UI_ACTION"
		or str(turn_input.action_id) != "request_ai_patch"
		or str(turn_input.selection_id) != str(visible_patch_failure.get("interaction_id", ""))
	):
		return _local_failure("SKILL_PATCH_REQUEST_AUTHORITY_INVALID", "Skill Patch request does not close through the visible failure and current Session/World/Skill authority.")
	var failure_authority_result := _build_patch_failure_authority(visible_patch_failure)
	if not failure_authority_result.get("ok", false):
		return failure_authority_result
	var selected_failure_authority: Dictionary = failure_authority_result.value
	var client_turn_sequence := int(session.get("last_turn_sequence", 0)) + 1
	var skill_bindings: Array = [_binding_from_active_tuple(active_skill_tuple)]
	var turn_id := _new_turn_id()
	var request := {
		"turn_id": turn_id,
		"expected_world_revision": int(world.revision),
		"input": turn_input.duplicate(true),
		"skill_bindings": skill_bindings,
		"client_state": {
			"last_event_sequence": int(world.last_event_sequence),
			"client_turn_sequence": client_turn_sequence,
		},
	}
	var identity := ContractValidator.canonical_json_sha256_v1({
		"session_id": str(session.session_id),
		"world_revision": int(world.revision),
		"last_event_sequence": int(world.last_event_sequence),
		"client_turn_sequence": client_turn_sequence,
		"input": turn_input,
		"skill_bindings": skill_bindings,
		"selected_failure_authority": selected_failure_authority,
	})
	var presentation_cursor := 0
	if world_presentation_enabled:
		if world_event_player == null or not world_event_player.has_method("get_cursor"):
			return _local_failure("SKILL_PATCH_PRESENTATION_CURSOR_UNAVAILABLE", "Skill Patch request cannot bind the current presentation cursor.")
		presentation_cursor = int(world_event_player.call("get_cursor"))
	var pending := store.ensure_pending_operation("agent_patch_request", identity, {
		"session_id": str(session.session_id),
		"turn_id": turn_id,
		"idempotency_key": RequestContextFactory.idempotency_key_for(
			"createAgentTurn", "%s:%s" % [session.session_id, turn_id],
		),
		"request": request,
		"pre_world": world.duplicate(true),
		"interaction_cursor_before": store.last_interaction_sequence,
		"selection_interaction_id": str(turn_input.selection_id),
		"selected_failure_authority": selected_failure_authority,
		"presentation_cursor_before": presentation_cursor,
	})
	if not pending.get("ok", false):
		return pending
	return await _execute_pending_patch_request(pending.value, false)


func recover_pending_patch_request() -> Dictionary:
	var store := _client_store()
	if store == null:
		return _local_failure("PENDING_PATCH_REQUEST_STORE_UNAVAILABLE", "ClientStore is unavailable for Skill Patch request recovery.")
	var integrity: Dictionary = store.validate_pending_operation("agent_patch_request")
	if not integrity.get("ok", false):
		return integrity
	var envelope: Dictionary = integrity.get("value", {})
	if envelope.is_empty():
		return {"ok": true, "status": 200, "headers": {}, "value": {"had_pending": false, "outcome": "NONE"}}
	if not patch_decisions_enabled:
		return _local_failure("PENDING_PATCH_REQUEST_CAPABILITY_DISABLED", "A pending Skill Patch request exists while its capability is disabled.")
	var result := await _execute_pending_patch_request(envelope, true)
	if result.get("ok", false):
		result.value["had_pending"] = true
	return result


func _execute_pending_patch_request(envelope: Dictionary, recovery_mode: bool) -> Dictionary:
	var store := _client_store()
	if store == null:
		return _local_failure("PENDING_PATCH_REQUEST_STORE_UNAVAILABLE", "ClientStore disappeared during Skill Patch request recovery.")
	var integrity: Dictionary = store.validate_pending_operation("agent_patch_request")
	if not integrity.get("ok", false) or integrity.get("value", {}) != envelope:
		return integrity if not integrity.get("ok", false) else _local_failure("PENDING_PATCH_REQUEST_DRIFT", "Skill Patch request envelope changed before execution.")
	var request: Dictionary = envelope.request
	var pre_world: Dictionary = envelope.pre_world
	var selected_failure_authority: Dictionary = envelope.selected_failure_authority
	# The durable selection is re-read before every possible mutation. Recovery
	# never trusts whichever Interaction happens to be latest in the UI or a
	# controller signal retained from the previous process.
	var selected_result: Dictionary = await product_gateway.get_interaction(
		_new_request_context(), str(envelope.session_id),
		str(envelope.selection_interaction_id),
	)
	if not selected_result.get("ok", false):
		return selected_result
	if not _patch_failure_authority_matches_interaction(
		selected_failure_authority, selected_result.value,
	):
		return _local_failure(
			"SKILL_PATCH_SELECTED_AUTHORITY_DRIFT",
			"The selected objective-failure Interaction no longer matches the durable request authority.",
		)
	var post_read_integrity: Dictionary = store.validate_pending_operation("agent_patch_request")
	if (
		not post_read_integrity.get("ok", false)
		or post_read_integrity.get("value", {}) != envelope
	):
		return post_read_integrity if not post_read_integrity.get("ok", false) else _local_failure(
			"PENDING_PATCH_REQUEST_DRIFT",
			"Skill Patch request authority changed during canonical selected-failure recovery.",
		)
	if not recovery_mode and (
		str(certified_build.get("build_id", "")) != str(selected_failure_authority.get("build_id", ""))
		or ContractValidator.canonical_json_sha256_v1(certified_build) != str(selected_failure_authority.get("build_resource_sha256", ""))
	):
		return _local_failure(
			"SKILL_PATCH_BUILD_AUTHORITY_DRIFT",
			"The current certified Build changed before the explicit Patch request mutation.",
		)
	var command_result: Dictionary
	var recovery: Variant = envelope.get("recovery")
	store.set_flow(WalnutClientStore.FlowState.TURN_RUNNING)
	if recovery_mode and recovery is Dictionary:
		command_result = await _new_poller().poll_resource({}, "get_command", str(recovery.command_id), "command_id")
	else:
		var submission: Dictionary = await game_gateway.submit_agent_turn(
			_new_request_context(), str(envelope.session_id),
			str(envelope.idempotency_key), request,
		)
		var accepted_command_id := _command_id_from_submission(submission)
		if not accepted_command_id.is_empty() and not store.set_pending_patch_request_recovery(
			accepted_command_id, "COMMAND_ACCEPTED",
		):
			return _local_failure(
				"PENDING_PATCH_REQUEST_ACCEPTED_PERSIST_FAILED",
				"Accepted Skill Patch Command identity could not be persisted before terminal polling.",
			)
		command_result = await _new_poller().reconcile({}, submission)
	if not command_result.get("ok", false):
		return command_result
	var command: Dictionary = command_result.value
	var result: Variant = command.get("result")
	var links: Variant = command.get("links")
	if (
		str(command.get("command_type", "")) != "EXECUTE_AGENT_TURN"
		or str(command.get("status", "")) != "APPLIED"
		or not bool(command.get("terminal", false))
		or not result is Dictionary
		or not _closed_dictionary(result, ["result_type", "reason_code"])
		or str(result.result_type) != "NO_EFFECT"
		or str(result.reason_code) != "SKILL_PATCH_PROPOSED"
		or links is Dictionary and links.has("run")
		or not _run_id_from_command(command).is_empty()
	):
		return _local_failure("SKILL_PATCH_COMMAND_CORRUPT", "Skill Patch request must terminate as exact NO_EFFECT/SKILL_PATCH_PROPOSED with no Run/World link.")
	if not store.set_pending_patch_request_recovery(str(command.command_id)):
		return _local_failure("PENDING_PATCH_REQUEST_RECOVERY_PERSIST_FAILED", "Terminal Skill Patch Command identity could not be persisted before Interaction recovery.")
	var proposal_result := await _wait_for_patch_proposal(
		str(envelope.session_id), str(envelope.turn_id), str(command.command_id),
		int(envelope.interaction_cursor_before), selected_failure_authority,
	)
	if not proposal_result.get("ok", false):
		return proposal_result
	if store.world_snapshot != pre_world:
		return _local_failure("SKILL_PATCH_REQUEST_WORLD_MUTATED", "Skill Patch proposal request mutated authoritative World state.")
	if world_presentation_enabled and (
		world_event_player == null
		or not world_event_player.has_method("get_cursor")
		or int(world_event_player.call("get_cursor")) != int(envelope.presentation_cursor_before)
	):
		return _local_failure("SKILL_PATCH_REQUEST_PRESENTATION_MUTATED", "Skill Patch proposal request mutated the presentation cursor.")
	if not store.clear_patch_failure_recovery_authority():
		return _local_failure(
			"SKILL_PATCH_FAILURE_AUTHORITY_CLEAR_FAILED",
			"The consumed failed-Run authority could not be durably cleared after proposal creation.",
		)
	if not store.clear_pending_operation("agent_patch_request"):
		return _local_failure("PENDING_PATCH_REQUEST_CLEAR_FAILED", "Closed Skill Patch request envelope could not be durably cleared.")
	visible_patch_failure.clear()
	store.set_flow(WalnutClientStore.FlowState.COMPLETED)
	return {"ok": true, "status": 200, "headers": {}, "value": {
		"had_pending": recovery_mode,
		"outcome": "SKILL_PATCH_PROPOSED",
		"command": command.duplicate(true),
		"interaction": proposal_result.value.duplicate(true),
	}}


func _wait_for_patch_proposal(
	session_id: String,
	turn_id: String,
	command_id: String,
	after_sequence: int,
	selected_failure_authority: Dictionary,
) -> Dictionary:
	var cursor := after_sequence
	var pages := 0
	var recovered: Array[Dictionary] = []
	while pages < 100:
		pages += 1
		var page: Dictionary = await product_gateway.list_interactions(
			_new_request_context(), session_id, cursor, 50,
		)
		if not page.get("ok", false):
			return page
		var value: Dictionary = page.value
		for interaction_value: Variant in value.get("interactions", []):
			if not interaction_value is Dictionary:
				return _local_failure("SKILL_PATCH_INTERACTION_CORRUPT", "Skill Patch Interaction page contains a non-object record.")
			var interaction: Dictionary = interaction_value
			recovered.append(interaction.duplicate(true))
			var proposal_feedback: Variant = interaction.get("feedback")
			var proposal_patch: Variant = interaction.get("skill_patch")
			if (
				_interaction_matches_turn(interaction, session_id, turn_id, command_id, "")
				and proposal_feedback is Dictionary
				and proposal_feedback.get("run_id") == null
				and proposal_patch is Dictionary
				and proposal_feedback.get("evidence_refs") == selected_failure_authority.get("evidence_refs")
				and proposal_patch.get("evidence_refs") == selected_failure_authority.get("evidence_refs")
				and validate_minimal_skill_patch_interaction(interaction).get("ok", false)
			):
				var store := _client_store()
				store.set_interaction_cursor(int(interaction.sequence))
				interactions_recovered.emit(recovered)
				return {"ok": true, "status": 200, "headers": {}, "value": interaction.duplicate(true)}
		var next_cursor := int(value.get("next_after_sequence", cursor))
		if bool(value.get("has_more", false)):
			if next_cursor <= cursor:
				return _local_failure("SKILL_PATCH_INTERACTION_CURSOR_STALLED", "Skill Patch Interaction pagination made no progress.")
			cursor = next_cursor
			continue
		return _local_failure("SKILL_PATCH_PROPOSAL_MISSING", "Terminal Skill Patch Command did not project one valid proposal.", true)
	return _local_failure("SKILL_PATCH_INTERACTION_PAGE_LIMIT", "Skill Patch Interaction pagination exceeded its safety bound.")


func _build_patch_failure_authority(interaction: Dictionary) -> Dictionary:
	var store := _client_store()
	var feedback: Variant = interaction.get("feedback")
	var feedback_event: Variant = interaction.get("feedback_event")
	var projection_source: Variant = interaction.get("projection_source")
	var artifact: Variant = certified_build.get("artifact")
	var certification: Variant = certified_build.get("certification")
	var session_id := str(authoritative_session.get("session_id", ""))
	var interaction_id := str(interaction.get("interaction_id", ""))
	var interaction_validation := ProductInteractionGateway.new(null)._validate_interaction(
		interaction, session_id, interaction_id,
	)
	if (
		store == null
		or not interaction_validation.get("ok", false)
		or not feedback is Dictionary
		or not feedback_event is Dictionary
		or not projection_source is Dictionary
		or not artifact is Dictionary
		or not certification is Dictionary
		or str(certified_build.get("status", "")) != "CERTIFIED"
		or not bool(certified_build.get("terminal", false))
		or str(certified_build.get("build_id", "")).is_empty()
		or str(certified_build.get("skill_id", "")) != str(active_skill_tuple.get("skill_id", ""))
		or str(certified_build.get("skill_version_id", "")) != str(active_skill_tuple.get("skill_version_id", ""))
		or str(artifact.get("artifact_sha256", "")) != str(active_skill_tuple.get("artifact_sha256", ""))
		or str(certification.get("certification_id", "")) != str(active_skill_tuple.get("certification_id", ""))
		or bool(store.objective_result.get("objective_succeeded", true))
		or str(store.objective_result.get("run_id", "")) != str(feedback.get("run_id", ""))
		or str(feedback.get("run_id", "")).is_empty()
		or not feedback.get("evidence_refs") is Array
		or feedback.evidence_refs.is_empty()
		or str(feedback_event.get("feedback_sha256", "")) != ContractValidator.canonical_json_sha256_v1(feedback)
		or str(projection_source.get("feedback_sha256", "")) != str(feedback_event.get("feedback_sha256", ""))
		or interaction.get("request_context", {}).get("actor") != authority_context.get("actor")
		or interaction.get("request_context", {}).get("content_ref") != authority_context.get("content_ref")
	):
		return _local_failure(
			"SKILL_PATCH_FAILURE_AUTHORITY_INVALID",
			"The selected failed Interaction is not closed through its exact Build, Run, Evidence, and projection hashes.",
		)
	var authority := {
		"interaction_id": interaction_id,
		"interaction_revision": int(interaction.interaction_revision),
		"sequence": int(interaction.sequence),
		"session_id": session_id,
		"turn_id": str(interaction.turn_id),
		"command_id": str(feedback.command_id),
		"run_id": str(feedback.run_id),
		"role": str(interaction.role),
		"response_type": str(interaction.response_type),
		"feedback_event_id": str(feedback_event.event_id),
		"feedback_sha256": ContractValidator.canonical_json_sha256_v1(feedback),
		"projection_source_sha256": str(projection_source.source_sha256),
		"evidence_refs": feedback.evidence_refs.duplicate(true),
		"build_id": str(certified_build.build_id),
		"build_resource_sha256": ContractValidator.canonical_json_sha256_v1(certified_build),
		"skill_binding": _binding_from_active_tuple(active_skill_tuple),
	}
	authority["failure_identity_sha256"] = ContractValidator.canonical_json_sha256_v1(authority)
	return {"ok": true, "status": 200, "headers": {}, "value": authority}


func _patch_failure_authority_matches_interaction(
	authority: Dictionary,
	interaction: Dictionary,
) -> bool:
	var session_id := str(authority.get("session_id", ""))
	var interaction_id := str(authority.get("interaction_id", ""))
	var validation := ProductInteractionGateway.new(null)._validate_interaction(
		interaction, session_id, interaction_id,
	)
	var feedback: Variant = interaction.get("feedback")
	var feedback_event: Variant = interaction.get("feedback_event")
	var projection_source: Variant = interaction.get("projection_source")
	if (
		not validation.get("ok", false)
		or not feedback is Dictionary
		or not feedback_event is Dictionary
		or not projection_source is Dictionary
		or interaction.get("skill_patch") != null
		or interaction.get("patch_decision") != null
		or interaction.get("request_context", {}).get("actor") != authority_context.get("actor")
		or interaction.get("request_context", {}).get("content_ref") != authority_context.get("content_ref")
	):
		return false
	return (
		int(interaction.get("interaction_revision", -1)) == int(authority.get("interaction_revision", -2))
		and int(interaction.get("sequence", -1)) == int(authority.get("sequence", -2))
		and str(interaction.get("session_id", "")) == session_id
		and str(interaction.get("turn_id", "")) == str(authority.get("turn_id", ""))
		and str(interaction.get("role", "")) == str(authority.get("role", ""))
		and str(interaction.get("response_type", "")) == str(authority.get("response_type", ""))
		and str(feedback.get("command_id", "")) == str(authority.get("command_id", ""))
		and str(feedback.get("run_id", "")) == str(authority.get("run_id", ""))
		and feedback.get("evidence_refs") == authority.get("evidence_refs")
		and ContractValidator.canonical_json_sha256_v1(feedback) == str(authority.get("feedback_sha256", ""))
		and str(feedback_event.get("event_id", "")) == str(authority.get("feedback_event_id", ""))
		and str(feedback_event.get("feedback_sha256", "")) == str(authority.get("feedback_sha256", ""))
		and str(projection_source.get("feedback_sha256", "")) == str(authority.get("feedback_sha256", ""))
		and str(projection_source.get("source_sha256", "")) == str(authority.get("projection_source_sha256", ""))
		and authority.get("skill_binding") == _binding_from_active_tuple(active_skill_tuple)
	)


func request_turn(input_override: Dictionary = {}, requires_skill_binding: bool = true) -> void:
	if not _student_action_readiness("Run").get("ok", false):
		return
	if game_gateway == null or not game_gateway.has_method("submit_agent_turn") or not game_gateway.has_method("get_run"):
		capability_unavailable.emit("Run", "Game gateway lacks Agent Turn or Run support.")
		return
	var store := _client_store()
	if store == null:
		capability_unavailable.emit("Run", "Agent Turn requires ClientStore authority.")
		return
	if requires_skill_binding and not store.patch_activation_invalidation.is_empty():
		store.report_error(_local_error(
			"RUN_PATCH_ACTIVATION_INVALIDATED",
			"The accepted Draft invalidated the old Activation; an explicit fresh Build and Activation are required.",
		))
		return
	# A persisted Turn is older and therefore authoritative over any identity
	# that could be derived from the recovered Workspace high-water marks. One
	# invocation only reconciles that envelope; a new Turn requires a later,
	# explicit invocation after the pending identity has closed.
	var pending_recovery: Dictionary = await recover_pending_turn_operations()
	if not pending_recovery.get("ok", false):
		store.report_error(pending_recovery.get("error", _local_error("PENDING_TURN_RECOVERY_FAILED", "Pending Agent Turn reconciliation failed.")))
		return
	var pending_value: Dictionary = pending_recovery.get("value", {})
	if bool(pending_value.get("had_pending", false)):
		var terminal_error: Variant = pending_value.get("terminal_error")
		if terminal_error is Dictionary and not terminal_error.is_empty():
			store.report_error(terminal_error)
		return
	if requires_skill_binding:
		if not store.clear_patch_failure_recovery_authority():
			store.report_error(_local_error(
				"SKILL_PATCH_FAILURE_AUTHORITY_CLEAR_FAILED",
				"A new explicit Run cannot start until the prior failed-Run authority is durably retired.",
			))
			return
		visible_patch_failure.clear()
	var workspace: Dictionary = {} if store == null else store.workspace
	var world: Dictionary = {} if store == null else store.world_snapshot
	var session: Variant = workspace.get("session")
	var task: Variant = workspace.get("current_task")
	if store == null or not session is Dictionary or not task is Dictionary or world.is_empty():
		capability_unavailable.emit("Run", "Agent Turn requires recovered SessionWorkspace and Snapshot.")
		return
	if str(session.get("status", "")) != "ACTIVE" or str(task.get("task_id", "")).is_empty():
		capability_unavailable.emit("Run", "The recovered AgentSession or task is not runnable.")
		return
	if not authoritative_session.is_empty() and str(session.get("session_id", "")) != str(authoritative_session.get("session_id", "")):
		store.report_error(_local_error("TURN_SESSION_AUTHORITY_MISMATCH", "Workspace Session is not the authoritative Session."))
		return
	var skill_bindings: Array = []
	if requires_skill_binding:
		if not _valid_active_tuple(active_skill_tuple):
			capability_unavailable.emit("Run", "Run requires StudentBootstrap.active exact tuple.")
			return
		skill_bindings = [_binding_from_active_tuple(active_skill_tuple)]
	var turn_input: Dictionary = (
		input_override.duplicate(true)
		if not input_override.is_empty()
		else {"type": "ASSIGNED_TASK", "task_id": task.task_id}
	)
	var client_turn_sequence := int(session.get("last_turn_sequence", 0)) + 1
	var identity := JSON.stringify({
		"session_id": session.session_id,
		"world_revision": world.revision,
		"last_event_sequence": store.last_applied_sequence,
		"client_turn_sequence": client_turn_sequence,
		"input": turn_input,
		"skill_bindings": skill_bindings,
	}).sha256_text()
	var slot := "agent_turn" if requires_skill_binding else "agent_hint"
	var turn_id := _new_turn_id()
	var pre_world := world.duplicate(true)
	var interaction_cursor_before := store.last_interaction_sequence
	var request := {
		"turn_id": turn_id,
		"expected_world_revision": int(world.revision),
		"input": turn_input,
		"skill_bindings": skill_bindings,
		"client_state": {
			"last_event_sequence": int(store.last_applied_sequence),
			"client_turn_sequence": client_turn_sequence,
		},
	}
	var pending_envelope := {
		"session_id": str(session.session_id),
		"turn_id": turn_id,
		"idempotency_key": RequestContextFactory.idempotency_key_for(
			"createAgentTurn", "%s:%s" % [session.session_id, turn_id],
		),
		"request": request,
		"pre_world": pre_world,
		"interaction_cursor_before": interaction_cursor_before,
	}
	if world_presentation_enabled:
		if world_event_player == null or not world_event_player.has_method("get_cursor"):
			store.report_error(_local_error("PRESENTATION_CURSOR_UNAVAILABLE", "The authoritative presentation cursor is unavailable before Run submission."))
			return
		pending_envelope["presentation_after_sequence"] = int(world_event_player.call("get_cursor"))
	var envelope_result := store.ensure_pending_operation(slot, identity, pending_envelope)
	if not envelope_result.get("ok", false):
		store.report_error(envelope_result.get("error", _local_error(
			"PENDING_OPERATION_PERSISTENCE_FAILED",
			"The Agent Turn envelope could not be persisted.",
		)))
		return
	var envelope: Dictionary = envelope_result.value
	var execution: Dictionary = await _execute_pending_turn_envelope(slot, envelope, false)
	if not execution.get("ok", false):
		store.report_error(execution.get("error", _local_error("TURN_RECOVERY_FAILED", "Agent Turn reconciliation failed.")))
		return
	var execution_value: Dictionary = execution.get("value", {})
	var terminal_error: Variant = execution_value.get("error")
	if bool(execution_value.get("terminal_failure", false)) and terminal_error is Dictionary:
		store.report_error(terminal_error)


## Reconcile every persisted Turn identity before callers are allowed to derive
## a new identity from Workspace. The original request Dictionary and key are
## passed through unchanged, including after ClientStore JSON restoration.
func recover_pending_turn_operations(recovery_mode := true) -> Dictionary:
	var store := _client_store()
	if store == null:
		return _local_failure("PENDING_TURN_STORE_UNAVAILABLE", "ClientStore is unavailable for pending Turn recovery.")
	var pending_slots: Array[String] = []
	for slot in PENDING_TURN_SLOTS:
		var integrity: Dictionary = store.validate_pending_operation(slot)
		if not integrity.get("ok", false):
			return integrity
		if not integrity.get("value", {}).is_empty():
			pending_slots.append(slot)
	if pending_slots.is_empty():
		return {
			"ok": true,
			"status": 200,
			"headers": {},
			"value": {"had_pending": false, "outcomes": [], "terminal_error": null},
		}
	if _pending_turn_recovery_active:
		return _local_failure("PENDING_TURN_RECOVERY_ACTIVE", "A pending Turn recovery is already in progress.", true)
	_pending_turn_recovery_active = true
	var outcomes: Array[Dictionary] = []
	var terminal_error: Variant = null
	for slot in pending_slots:
		var integrity: Dictionary = store.validate_pending_operation(slot)
		if not integrity.get("ok", false):
			_pending_turn_recovery_active = false
			return integrity
		var envelope: Dictionary = integrity.get("value", {})
		if envelope.is_empty():
			continue
		var result: Dictionary = await _execute_pending_turn_envelope(slot, envelope, recovery_mode)
		if not result.get("ok", false):
			_pending_turn_recovery_active = false
			return result
		var outcome: Dictionary = result.get("value", {})
		outcomes.append(outcome.duplicate(true))
		if terminal_error == null and bool(outcome.get("terminal_failure", false)):
			var candidate: Variant = outcome.get("error")
			if candidate is Dictionary:
				terminal_error = candidate.duplicate(true)
	_pending_turn_recovery_active = false
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {
			"had_pending": true,
			"outcomes": outcomes,
			"terminal_error": terminal_error,
		},
	}


func _execute_pending_turn_envelope(slot: String, envelope: Dictionary, recovery_mode := false) -> Dictionary:
	var context_result := _pending_turn_envelope_context(slot, envelope)
	if not context_result.get("ok", false):
		return context_result
	var context: Dictionary = context_result.value
	var store := _client_store()
	if store == null:
		return _local_failure("PENDING_TURN_STORE_UNAVAILABLE", "ClientStore disappeared during pending Turn recovery.")
	var request: Dictionary = context.request
	var session_id := str(context.session_id)
	var turn_id := str(context.turn_id)
	var interaction_cursor_before := int(context.interaction_cursor_before)
	var poller := _new_poller()
	store.set_flow(WalnutClientStore.FlowState.TURN_RUNNING)
	var recovery: Dictionary = context.get("recovery", {})
	var command_result: Dictionary
	if recovery_mode and not recovery.is_empty():
		# A terminal identity persisted before playback makes restart GET-only.
		command_result = await poller.poll_resource(
			{}, "get_command", str(recovery.command_id), "command_id",
		)
	else:
		# This is deliberately the persisted object, not a request reconstructed
		# from current Workspace/world high-water marks.  An ACK-unknown attempt
		# reuses the exact stable identity and Idempotency-Key.
		var submission: Dictionary = await game_gateway.submit_agent_turn(
			_new_request_context(),
			session_id,
			str(context.idempotency_key),
			request,
		)
		command_result = await poller.reconcile({}, submission)
	if not command_result.get("ok", false):
		return command_result
	var command: Dictionary = command_result.value
	var command_status := str(command.get("status", ""))
	if command_status not in ["APPLIED", "REJECTED"] or (
		command_status == "REJECTED" and slot != "agent_turn"
	):
		return _pending_turn_terminal_result(
			slot,
			command,
			{},
			"TURN_COMMAND_FAILED" if command_status == "FAILED" else "TURN_COMMAND_REJECTED",
			"Agent Turn command reached terminal status %s." % str(command.get("status", "UNKNOWN")),
		)
	var run_id := _run_id_from_command(command)
	if recovery_mode and not recovery.is_empty() and run_id != str(recovery.run_id):
		return _local_failure("PRESENTATION_RECOVERY_RUN_MISMATCH", "Persisted playback recovery points to another terminal Run.")
	if run_id.is_empty():
		if slot == "agent_turn" or command_status == "REJECTED":
			return _pending_turn_terminal_result(
				slot,
				command,
				{},
				"TURN_OBJECTIVE_RUN_LINK_MISSING" if command_status == "REJECTED" else "TURN_RUN_LINK_MISSING",
				"A bound Agent Turn reached a terminal objective status without one exact Run link.",
			)
		var hint_interactions := await _wait_for_interaction(
			session_id,
			turn_id,
			str(command.command_id),
			"",
			interaction_cursor_before,
		)
		if not hint_interactions.get("ok", false):
			return hint_interactions
		store.clear_pending_operation(slot)
		store.set_flow(WalnutClientStore.FlowState.ACTIVE)
		store.set_objective_result({"summary": "Hint feedback was recovered from the canonical AgentInteraction."})
		return {
			"ok": true,
			"status": 200,
			"headers": {},
			"value": {
				"slot": slot,
				"turn_id": turn_id,
				"command": command.duplicate(true),
				"outcome": "HINT_COMPLETED",
				"terminal_failure": false,
			},
		}
	var run_result: Dictionary = await poller.poll_resource({}, "get_run", run_id, "run_id")
	if not run_result.get("ok", false):
		return run_result
	var run: Dictionary = run_result.value
	if not _run_matches_turn(run, session_id, turn_id, str(command.command_id)):
		return _pending_turn_terminal_result(
			slot,
			command,
			run,
			"RUN_IDENTITY_MISMATCH",
			"Terminal Run identity or exact Skill tuple does not match the persisted Turn.",
		)
	var run_status := str(run.get("status", ""))
	if command_status == "REJECTED":
		if run_status not in ["REJECTED", "FAILED"]:
			return _pending_turn_terminal_result(
				slot,
				command,
				run,
				"OBJECTIVE_RUN_STATUS_MISMATCH",
				"A rejected objective Command does not close through an objective-failed Run.",
			)
		var failed_closure := await _close_failed_objective_run(
			run,
			command,
			context.pre_world,
			interaction_cursor_before,
		)
		if not failed_closure.get("ok", false):
			return failed_closure
		var failure_result := {
			"summary": "The objective was not completed; verified teaching feedback was recovered without a World commit.",
			"objective_succeeded": false,
			"run_id": run_id,
		}
		store.set_objective_result(failure_result)
		if patch_decisions_enabled:
			var failure_interaction := _matching_failed_interaction(
				failed_closure.get("value", {}).get("interactions", []),
				run,
			)
			if (
				failure_interaction.is_empty()
				or not store.record_patch_failure_recovery_authority(
					certified_build,
					run,
					failure_interaction,
					failed_closure.get("value", {}).get("evidence", []),
					failure_result,
				)
			):
				var persistence_error := _local_error(
					"SKILL_PATCH_FAILURE_AUTHORITY_PERSIST_FAILED",
					"The exact Build/Run/Interaction/Evidence authority could not be persisted for restart-safe Patch eligibility.",
				)
				store.report_error(persistence_error)
				return {"ok": false, "status": 0, "headers": {}, "error": persistence_error}
			register_visible_patch_failure(failure_interaction)
		if not store.clear_pending_operation(slot):
			return _local_failure("PENDING_TURN_CLEAR_FAILED", "Objective-failed Turn envelope could not be durably cleared.")
		run_resolved.emit(run.duplicate(true))
		store.set_flow(WalnutClientStore.FlowState.COMPLETED)
		return {
			"ok": true,
			"status": 200,
			"headers": {},
			"value": {
				"slot": slot,
				"turn_id": turn_id,
				"command": command.duplicate(true),
				"run": run.duplicate(true),
				"outcome": "OBJECTIVE_FAILED_WITH_FEEDBACK",
				"terminal_failure": false,
			},
		}
	if run_status != "SUCCEEDED":
		return _pending_turn_terminal_result(
			slot,
			command,
			run,
			"RUN_TERMINAL_FAILURE",
			"Run reached terminal status %s." % str(run.get("status", "UNKNOWN")),
		)
	var closure := await _close_successful_run(
		run,
		command,
		context.pre_world,
		interaction_cursor_before,
		int(context.presentation_after_sequence),
	)
	if not closure.get("ok", false):
		return closure
	var closure_value: Dictionary = closure.value
	if world_presentation_enabled:
		if recovery_mode:
			if not _commit_authoritative_snapshot(closure_value.snapshot):
				return _local_failure("PLAYBACK_RECOVERY_SNAPSHOT_FAILED", "Restart could not restore the authoritative final Snapshot.")
			if world_event_player != null and world_event_player.has_method("set_cursor"):
				world_event_player.call(
					"set_cursor",
					int(closure_value.presentation_high_watermark),
					closure_value.presentation_events[-1],
				)
			store.set_objective_result({
				"summary": "PLAYBACK_RECOVERED_BY_SNAPSHOT: committed Run restored with GET-only presentation/Snapshot authority.",
				"recovery_code": "PLAYBACK_RECOVERED_BY_SNAPSHOT",
			})
		else:
			var recovery_authority := {
				"phase": "RUN_TERMINAL",
				"command_id": str(command.command_id),
				"run_id": str(run.run_id),
				"presentation_after_sequence": int(context.presentation_after_sequence),
				"final_snapshot": {
					"world_id": str(closure_value.snapshot.world_id),
					"revision": int(closure_value.snapshot.revision),
					"last_event_sequence": int(closure_value.snapshot.last_event_sequence),
					"state_hash": str(closure_value.snapshot.state_hash),
				},
			}
			if not store.set_pending_turn_recovery(slot, recovery_authority):
				return _local_failure("PRESENTATION_RECOVERY_PERSIST_FAILED", "Terminal Run identity could not be persisted before playback.")
			_last_presentation_pre_snapshot = context.pre_world.duplicate(true)
			_last_presentation_final_snapshot = closure_value.snapshot.duplicate(true)
			store.set_flow(WalnutClientStore.FlowState.PLAYING)
			world_playback_state_changed.emit("PLAYING")
			var playback: Dictionary = await world_event_player.call(
				"play", closure_value.presentation_events, world_presentation_renderer,
			)
			if not playback.get("ok", false):
				# Renderer/stream failure can never suppress final World authority.
				if not _commit_authoritative_snapshot(closure_value.snapshot):
					return _local_failure("PRESENTATION_FAILURE_SNAPSHOT_FAILED", "Playback failed and authoritative Snapshot recovery also failed.")
				# The complete batch was preflight-verified before the renderer ran.
				# Closing its display cursor prevents the next Run from refetching and
				# replaying an already committed business result.
				world_event_player.call(
					"set_cursor",
					int(closure_value.presentation_high_watermark),
					closure_value.presentation_events[-1],
				)
				store.clear_pending_operation(slot)
				return playback
			if not _commit_authoritative_snapshot(closure_value.snapshot):
				return _local_failure("PRESENTATION_FINAL_SNAPSHOT_FAILED", "Playback completed but final Snapshot authority could not be committed.")
	elif not _commit_authoritative_snapshot(closure_value.snapshot):
		return _local_failure("RUN_SNAPSHOT_COMMIT_FAILED", "Verified Snapshot could not atomically replace client world state.")
	if not store.clear_pending_operation(slot):
		return _local_failure("PENDING_TURN_CLEAR_FAILED", "The closed Turn envelope could not be durably cleared.")
	run_resolved.emit(run.duplicate(true))
	if not (world_presentation_enabled and recovery_mode):
		store.set_objective_result({
			"summary": "Run %s closed through Evidence, authoritative presentation, Snapshot and AgentInteraction." % run_id,
		})
	store.set_flow(WalnutClientStore.FlowState.COMPLETED)
	world_playback_state_changed.emit("COMPLETED")
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {
			"slot": slot,
			"turn_id": turn_id,
			"command": command.duplicate(true),
			"run": run.duplicate(true),
			"outcome": "PLAYBACK_RECOVERED_BY_SNAPSHOT" if world_presentation_enabled and recovery_mode else "RUN_COMPLETED",
			"terminal_failure": false,
		},
	}


func _pending_turn_envelope_context(slot: String, envelope: Dictionary) -> Dictionary:
	if slot not in PENDING_TURN_SLOTS:
		return _local_failure("PENDING_TURN_SLOT_INVALID", "Pending Turn slot is not recognized.")
	var request_value: Variant = envelope.get("request")
	var pre_world_value: Variant = envelope.get("pre_world")
	if not request_value is Dictionary:
		return _local_failure("PENDING_TURN_ENVELOPE_INVALID", "Pending Turn envelope has no original request body.")
	var request: Dictionary = request_value
	var turn_id := str(envelope.get("turn_id", ""))
	var idempotency_key := str(envelope.get("idempotency_key", ""))
	var client_state: Variant = request.get("client_state")
	var skill_bindings: Variant = request.get("skill_bindings")
	if (
		turn_id.is_empty()
		or str(request.get("turn_id", "")) != turn_id
		or idempotency_key.length() < 16
		or not client_state is Dictionary
		or not skill_bindings is Array
		or (slot == "agent_turn" and skill_bindings.size() != 1)
		or (slot == "agent_hint" and not skill_bindings.is_empty())
	):
		return _local_failure("PENDING_TURN_ENVELOPE_INVALID", "Pending Turn envelope identity or closed request body is invalid.")
	var session_id := str(envelope.get("session_id", authoritative_session.get("session_id", "")))
	if session_id.is_empty() or (
		not authoritative_session.is_empty()
		and session_id != str(authoritative_session.get("session_id", ""))
	):
		return _local_failure("PENDING_TURN_SESSION_MISMATCH", "Pending Turn does not belong to the authoritative Session.")
	if slot == "agent_turn" and (
		not _valid_active_tuple(active_skill_tuple)
		or skill_bindings != [_binding_from_active_tuple(active_skill_tuple)]
	):
		return _local_failure(
			"PENDING_TURN_ACTIVE_SKILL_MISMATCH",
			"Pending Turn does not bind the current exact active Skill tuple.",
		)
	var pre_world: Dictionary
	if pre_world_value is Dictionary:
		pre_world = pre_world_value.duplicate(true)
	else:
		# Compatibility for envelopes created before the restart-closure fields
		# were introduced. Request cursors remain authoritative; never substitute
		# the post-recovery ClientStore high-water marks.
		var world_id := str(authoritative_session.get("world_id", ""))
		if world_id.is_empty():
			world_id = str(_bootstrap_world_authority().get("world_id", ""))
		pre_world = {
			"world_id": world_id,
			"revision": int(request.get("expected_world_revision", -1)),
			"last_event_sequence": int(client_state.get("last_event_sequence", -1)),
		}
	if (
		str(pre_world.get("world_id", "")).is_empty()
		or int(pre_world.get("revision", -1)) < 0
		or int(pre_world.get("last_event_sequence", -1)) < 0
		or int(request.get("expected_world_revision", -2)) != int(pre_world.get("revision", -1))
		or int(client_state.get("last_event_sequence", -2)) != int(pre_world.get("last_event_sequence", -1))
	):
		return _local_failure("PENDING_TURN_WORLD_CURSOR_INVALID", "Pending Turn pre-world cursor disagrees with its original request body.")
	var authority_world_id := str(authoritative_session.get("world_id", ""))
	if authority_world_id.is_empty():
		authority_world_id = str(_bootstrap_world_authority().get("world_id", ""))
	if authority_world_id.is_empty() or str(pre_world.get("world_id", "")) != authority_world_id:
		return _local_failure(
			"PENDING_TURN_WORLD_AUTHORITY_MISMATCH",
			"Pending Turn pre-world is not bound to the authoritative Session World.",
		)
	var interaction_cursor_before := int(envelope.get("interaction_cursor_before", 0))
	if interaction_cursor_before < 0:
		return _local_failure("PENDING_TURN_INTERACTION_CURSOR_INVALID", "Pending Turn Interaction cursor is invalid.")
	var presentation_after_sequence := int(envelope.get("presentation_after_sequence", 0))
	if presentation_after_sequence < 0:
		return _local_failure("PENDING_TURN_PRESENTATION_CURSOR_INVALID", "Pending Turn presentation cursor is invalid.")
	var recovery_value: Variant = envelope.get("recovery")
	var recovery: Dictionary = recovery_value.duplicate(true) if recovery_value is Dictionary else {}
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {
			"session_id": session_id,
			"turn_id": turn_id,
			"idempotency_key": idempotency_key,
			"request": request,
			"pre_world": pre_world,
			"interaction_cursor_before": interaction_cursor_before,
			"presentation_after_sequence": presentation_after_sequence,
			"recovery": recovery,
		},
	}


func _pending_turn_terminal_result(
	slot: String,
	command: Dictionary,
	run: Dictionary,
	code: String,
	message: String,
) -> Dictionary:
	var store := _client_store()
	if store != null:
		store.clear_pending_operation(slot)
	var error := _local_error(code, message)
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {
			"slot": slot,
			"turn_id": str(command.get("turn_id", "")),
			"command": command.duplicate(true),
			"run": run.duplicate(true),
			"outcome": "TERMINAL_FAILURE",
			"terminal_failure": true,
			"error": error,
		},
	}


func recover_interactions(_attempt: Dictionary, session_id: String) -> Dictionary:
	var recovered_result := await _fetch_interactions(session_id)
	if not recovered_result.get("ok", false):
		return recovered_result
	var recovered: Array[Dictionary] = recovered_result.value
	interactions_recovered.emit(recovered)
	return recovered_result


func _fetch_interactions(session_id: String) -> Dictionary:
	if product_gateway == null or not product_gateway.has_method("list_interactions"):
		return _local_failure("PRODUCT_GATEWAY_UNAVAILABLE", "Product AgentInteraction gateway is not configured.")
	var after_sequence := 0
	var recovered: Array[Dictionary] = []
	var pages := 0
	while pages < 1000:
		pages += 1
		var page: Dictionary = await product_gateway.list_interactions(
			_new_request_context(), session_id, after_sequence, 50,
		)
		if not page.get("ok", false):
			return page
		var value: Dictionary = page.value
		for interaction in value.interactions:
			recovered.append(interaction.duplicate(true))
		var next_cursor := int(value.next_after_sequence)
		if bool(value.has_more) and next_cursor <= after_sequence:
			return _local_failure("INTERACTION_CURSOR_STALLED", "AgentInteraction pagination made no progress.")
		after_sequence = next_cursor
		if not bool(value.has_more):
			var store := _client_store()
			if store != null:
				store.set_interaction_cursor(maxi(after_sequence, int(value.get("high_watermark_sequence", after_sequence))))
			return {"ok": true, "status": 200, "headers": {}, "value": recovered}
	return _local_failure("INTERACTION_PAGE_LIMIT", "AgentInteraction pagination exceeded its safety bound.")


## Rebuild page state only after every resource identity has closed. No partial
## Draft/Snapshot group is committed to ClientStore.
func recover_workspace(_attempt: Dictionary, session_id: String) -> Dictionary:
	if (
		product_gateway == null
		or game_gateway == null
		or not product_gateway.has_method("get_workspace")
		or not product_gateway.has_method("get_draft")
		or not game_gateway.has_method("get_world_snapshot")
	):
		return _local_failure("WORKSPACE_RECOVERY_GATEWAY_UNAVAILABLE", "Workspace recovery gateways are not configured.")
	var store := _client_store()
	if store == null:
		return _local_failure("WORKSPACE_RECOVERY_STORE_UNAVAILABLE", "ClientStore is not available.")
	var workspace_result: Dictionary = await product_gateway.get_workspace(
		_new_request_context(), session_id,
	)
	if not workspace_result.get("ok", false):
		return workspace_result
	var workspace: Dictionary = workspace_result.value
	var workspace_session: Variant = workspace.get("session")
	if not workspace_session is Dictionary or str(workspace_session.get("session_id", "")) != session_id:
		return _local_failure("WORKSPACE_RECOVERY_SESSION_MISMATCH", "Workspace does not embed the exact requested Session.")
	if not authoritative_session.is_empty() and not _workspace_session_matches_authority(workspace_session):
		return _local_failure("WORKSPACE_RECOVERY_SESSION_MISMATCH", "Workspace Session disagrees with StudentBootstrap Session authority.")
	var refs: Variant = workspace.get("skill_draft_refs")
	if not refs is Array or refs.size() != 1 or not refs[0] is Dictionary:
		return _local_failure("WORKSPACE_RECOVERY_DRAFT_AMBIGUOUS", "Workspace must select exactly one canonical Draft.")
	var reference: Dictionary = refs[0]
	var draft_result: Dictionary = await product_gateway.get_draft(
		_new_request_context(), session_id, str(reference.draft_id),
	)
	if not draft_result.get("ok", false):
		return draft_result
	var draft: Dictionary = draft_result.value
	var checkpoint: Variant = workspace.get("world_checkpoint")
	if (
		not checkpoint is Dictionary
		or str(draft.get("draft_id", "")) != str(reference.draft_id)
		or str(draft.get("skill_id", "")) != str(reference.skill_id)
		or int(draft.get("revision", -1)) != int(reference.revision)
		or str(draft.get("draft_sha256", "")) != str(reference.draft_sha256)
	):
		return _local_failure("WORKSPACE_RECOVERY_DRAFT_MISMATCH", "Canonical Draft does not match Workspace reference.")
	var snapshot_result: Dictionary = await game_gateway.get_world_snapshot(
		_new_request_context(), str(checkpoint.get("world_id", "")),
	)
	if not snapshot_result.get("ok", false):
		return snapshot_result
	var snapshot: Dictionary = snapshot_result.value
	if (
		str(snapshot.get("world_id", "")) != str(checkpoint.world_id)
		or int(snapshot.get("revision", -1)) < int(checkpoint.world_revision)
		or int(snapshot.get("last_event_sequence", -1)) < int(checkpoint.last_event_sequence)
	):
		return _local_failure("WORKSPACE_RECOVERY_WORLD_MISMATCH", "Snapshot is older than the Workspace checkpoint.")
	var bootstrap_world := _bootstrap_world_authority()
	if not bootstrap_world.is_empty() and not _snapshot_matches_bootstrap(snapshot, bootstrap_world):
		return _local_failure("WORKSPACE_RECOVERY_BOOTSTRAP_WORLD_MISMATCH", "Snapshot disagrees with StudentBootstrap world authority.")
	var interactions := await _fetch_interactions(session_id)
	if not interactions.get("ok", false):
		return interactions
	store.set_workspace(workspace)
	store.set_draft(draft)
	if not store.replace_world(snapshot):
		return _local_failure("WORKSPACE_RECOVERY_WORLD_INVALID", "Snapshot could not be committed to ClientStore.")
	var patch_failure_recovery := await recover_patch_failure_authority(interactions.value)
	if not patch_failure_recovery.get("ok", false):
		return patch_failure_recovery
	interactions_recovered.emit(interactions.value)
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {
			"workspace": workspace,
			"draft": draft,
			"snapshot": snapshot,
			"interactions": interactions.value,
		},
	}


func recover_patch_failure_authority(interactions: Array[Dictionary]) -> Dictionary:
	var store := _client_store()
	if store == null:
		return _local_failure("SKILL_PATCH_FAILURE_RECOVERY_STORE_UNAVAILABLE", "ClientStore is unavailable for failed-Run authority recovery.")
	var marker: Dictionary = store.patch_failure_recovery_authority.duplicate(true)
	if marker.is_empty() or not patch_decisions_enabled:
		return {"ok": true, "status": 200, "headers": {}, "value": {"recovered": false}}
	visible_patch_failure.clear()
	patch_failure_recovery_status.clear()
	if (
		game_gateway == null
		or not game_gateway.has_method("get_skill_build")
		or not game_gateway.has_method("get_run")
		or not game_gateway.has_method("get_evidence")
	):
		return _patch_failure_recovery_failure(
			"SKILL_PATCH_FAILURE_RECOVERY_GATEWAY_UNAVAILABLE",
			"Restart recovery requires public SkillBuild, Run, and Evidence GET Gateways.",
		)
	var interaction: Dictionary = {}
	for candidate: Dictionary in interactions:
		if str(candidate.get("interaction_id", "")) == str(marker.interaction_id):
			interaction = candidate.duplicate(true)
			break
	if (
		interaction.is_empty()
		or int(interaction.get("interaction_revision", -1)) != int(marker.interaction_revision)
		or int(interaction.get("sequence", -1)) != int(marker.interaction_sequence)
		or ContractValidator.canonical_json_sha256_v1(interaction) != str(marker.interaction_sha256)
	):
		return _patch_failure_recovery_failure(
			"SKILL_PATCH_FAILURE_INTERACTION_DRIFT",
			"The persisted objective-failure Interaction is missing or differs from its canonical restart authority.",
		)
	var build_result: Dictionary = await game_gateway.get_skill_build(
		_new_request_context(), str(marker.build_id),
	)
	if not build_result.get("ok", false):
		return _patch_failure_recovery_failure_from_result("SKILL_PATCH_FAILURE_BUILD_READ_FAILED", build_result)
	var build: Dictionary = build_result.value
	if (
		not ContractValidator.validate_skill_build(build).ok
		or build != marker.certified_build
		or ContractValidator.canonical_json_sha256_v1(build) != str(marker.certified_build_sha256)
		or not _recovered_build_matches_active(build)
	):
		return _patch_failure_recovery_failure(
			"SKILL_PATCH_FAILURE_BUILD_DRIFT",
			"The canonical certified SkillBuild differs from the exact persisted failed-Run authority.",
		)
	var run_result: Dictionary = await game_gateway.get_run(
		_new_request_context(), str(marker.run_id),
	)
	if not run_result.get("ok", false):
		return _patch_failure_recovery_failure_from_result("SKILL_PATCH_FAILURE_RUN_READ_FAILED", run_result)
	var run: Dictionary = run_result.value
	if (
		not ContractValidator.validate_run(run).ok
		or ContractValidator.canonical_json_sha256_v1(run) != str(marker.run_sha256)
		or str(run.get("status", "")) not in ["REJECTED", "FAILED"]
		or not bool(run.get("terminal", false))
		or run.get("world_application", {}).get("receipt") != null
		or run.get("skill") != _binding_from_active_tuple(active_skill_tuple)
		or str(run.get("session_id", "")) != str(marker.session_id)
	):
		return _patch_failure_recovery_failure(
			"SKILL_PATCH_FAILURE_RUN_DRIFT",
			"The canonical failed Run differs from the exact persisted restart authority.",
		)
	var evidence_result := await _recover_failed_run_evidence(run)
	if not evidence_result.get("ok", false):
		return _patch_failure_recovery_failure_from_result("SKILL_PATCH_FAILURE_EVIDENCE_READ_FAILED", evidence_result)
	if (
		run.get("evidence_refs") != marker.evidence_refs
		or evidence_result.value != marker.evidence_resources
		or ContractValidator.canonical_json_sha256_v1(evidence_result.value) != str(marker.evidence_resources_sha256)
	):
		return _patch_failure_recovery_failure(
			"SKILL_PATCH_FAILURE_EVIDENCE_DRIFT",
			"Canonical failed-Run Evidence differs from the exact persisted restart authority.",
		)
	var feedback: Variant = interaction.get("feedback")
	if (
		not feedback is Dictionary
		or str(feedback.get("run_id", "")) != str(run.run_id)
		or str(feedback.get("command_id", "")) != str(run.command_id)
		or feedback.get("evidence_refs") != run.evidence_refs
	):
		return _patch_failure_recovery_failure(
			"SKILL_PATCH_FAILURE_INTERACTION_RUN_DRIFT",
			"Recovered Interaction does not bind the exact failed Run and Evidence set.",
		)
	certified_build = build.duplicate(true)
	store.set_objective_result(marker.objective_result)
	if not register_visible_patch_failure(interaction):
		return _patch_failure_recovery_failure(
			"SKILL_PATCH_FAILURE_SELECTION_INVALID",
			"Recovered failed authority did not produce one contract-valid visible Patch selection.",
		)
	return {"ok": true, "status": 200, "headers": {}, "value": {"recovered": true, "interaction_id": marker.interaction_id}}


## Patch remains outside the INT1 primary path, but if invoked by an existing
## surface its immutable request envelope survives response-loss reconciliation.
func decide_patch(interaction: Dictionary, decision: String, reason_code: String = "STUDENT_REJECTED") -> Dictionary:
	var readiness := _student_action_readiness("PatchDecision")
	if not readiness.get("ok", false):
		return readiness
	if not patch_decisions_enabled:
		return _local_failure(
			"PATCH_DECISION_EXCLUDED",
			"PatchDecision is not installed in the formal INT1 application composition.",
		)
	var proposal_validation := validate_minimal_skill_patch_interaction(interaction)
	if not proposal_validation.get("ok", false):
		return proposal_validation
	if product_gateway == null or not product_gateway.has_method("record_patch_decision") or not product_gateway.has_method("get_draft"):
		return _local_failure("PATCH_GATEWAY_UNAVAILABLE", "Product PatchDecision gateway is not configured.")
	var store := _client_store()
	var patch: Variant = interaction.get("skill_patch")
	if store == null or not patch is Dictionary or decision not in ["ACCEPT", "REJECT"]:
		return _local_failure("PATCH_DECISION_INVALID", "PatchDecision requires a canonical interaction and Draft.")
	var draft: Dictionary = store.draft
	if (
		draft.is_empty()
		or str(patch.get("draft_id", "")) != str(draft.get("draft_id", ""))
		or int(patch.get("base_draft_revision", -1)) != int(draft.get("revision", -2))
		or str(patch.get("base_draft_sha256", "")) != str(draft.get("draft_sha256", ""))
	):
		return _local_failure("PATCH_DRAFT_BASE_MISMATCH", "Patch is not based on the current canonical Draft.")
	if decision == "ACCEPT" and (
		store.draft_state != WalnutClientStore.DraftState.CLEAN
		or store.local_source != _source_bundle_entrypoint_source(draft.get("source_bundle"))
	):
		return _local_failure(
			"PATCH_LOCAL_EDIT_CONFLICT",
			"Accept is blocked because the editor has unsaved bytes that are not the reviewed canonical Draft.",
		)
	var business_id := "%s:%s:%s" % [interaction.interaction_id, patch.patch_id, decision]
	var decision_id := "decision_%s" % RequestContextFactory.idempotency_key_for("recordProductPatchDecision", business_id).sha256_text().left(24)
	var request := {
		"decision_id": decision_id,
		"session_id": interaction.session_id,
		"turn_id": interaction.turn_id,
		"interaction_id": interaction.interaction_id,
		"expected_interaction_revision": interaction.interaction_revision,
		"patch_id": patch.patch_id,
		"patch_sha256": patch.patch_sha256,
		"draft_id": draft.draft_id,
		"skill_id": draft.skill_id,
		"base_draft_revision": draft.revision,
		"base_draft_sha256": draft.draft_sha256,
		"result_draft_sha256": patch.result_draft_sha256,
		"decision": decision,
		"reason_code": null if decision == "ACCEPT" else reason_code,
		"decided_at": RequestContextFactory.utc_now(),
	}
	var slot := "patch_decision:%s" % patch.patch_id
	var proposed_request_body := JSON.stringify(request)
	var envelope_result := store.ensure_pending_operation(slot, business_id, {
		"idempotency_key": RequestContextFactory.idempotency_key_for("recordProductPatchDecision", business_id),
		"request": request,
		"request_body": proposed_request_body,
		"request_body_sha256": proposed_request_body.sha256_text(),
	})
	if not envelope_result.get("ok", false):
		return envelope_result
	var envelope: Dictionary = envelope_result.value
	var stable_request_projection := request.duplicate(true)
	stable_request_projection.erase("decided_at")
	var persisted_request_projection: Dictionary = envelope.request.duplicate(true)
	persisted_request_projection.erase("decided_at")
	if stable_request_projection != persisted_request_projection:
		return _local_failure(
			"PATCH_DECISION_PAYLOAD_CONFLICT",
			"The stable PatchDecision identity is already bound to different immutable request fields.",
		)
	request = envelope.request
	var receipt: Dictionary = await product_gateway.record_patch_decision(
		_new_request_context(),
		str(interaction.session_id),
		str(interaction.interaction_id),
		str(patch.patch_id),
		str(envelope.idempotency_key),
		request,
		str(envelope.request_body),
	)
	var reconciliation := _product_write_reconciliation(
		receipt, "AGENT_INTERACTION", str(interaction.session_id), str(interaction.interaction_id),
	)
	if not reconciliation.is_empty():
		var canonical_interaction: Dictionary = await product_gateway.get_interaction(
			_new_request_context(), str(interaction.session_id), str(interaction.interaction_id),
		)
		var canonical_receipt: Variant = canonical_interaction.get("value", {}).get("patch_decision") if canonical_interaction.get("ok", false) else null
		if canonical_receipt is Dictionary and _patch_receipt_matches_request(canonical_receipt, request):
			receipt = {"ok": true, "status": 200, "headers": {}, "value": canonical_receipt.duplicate(true)}
		else:
			return _local_failure("PRODUCT_PATCH_RECONCILIATION_FAILED", "PatchDecision could not be reconciled.")
	if not receipt.get("ok", false):
		return receipt
	if not _patch_receipt_matches_request(receipt.value, request):
		return _local_failure("PATCH_DECISION_RECEIPT_MISMATCH", "PatchDecision receipt does not match its immutable request.")
	var finalized := await _finalize_patch_decision_receipt(slot, request, receipt.value)
	return receipt if finalized.get("ok", false) else finalized


func recover_pending_patch_decisions() -> Dictionary:
	var store := _client_store()
	if store == null:
		return _local_failure("PENDING_PATCH_STORE_UNAVAILABLE", "ClientStore is unavailable for pending PatchDecision recovery.")
	var slots: Array[String] = []
	for slot_value: Variant in store.pending_operations.keys():
		var slot := str(slot_value)
		if slot.begins_with("patch_decision:"):
			slots.append(slot)
	slots.sort()
	if slots.is_empty():
		return {"ok": true, "status": 200, "headers": {}, "value": {"had_pending": false, "outcomes": []}}
	if not patch_decisions_enabled:
		return _local_failure("PENDING_PATCH_CAPABILITY_DISABLED", "A pending PatchDecision exists while its authoritative capability is disabled.")
	if product_gateway == null or not product_gateway.has_method("get_interaction") or not product_gateway.has_method("record_patch_decision") or not product_gateway.has_method("get_draft"):
		return _local_failure("PENDING_PATCH_GATEWAY_UNAVAILABLE", "Product PatchDecision recovery Gateway is unavailable.")
	var outcomes: Array[Dictionary] = []
	for slot in slots:
		var integrity: Dictionary = store.validate_pending_operation(slot)
		if not integrity.get("ok", false):
			return integrity
		var envelope: Dictionary = integrity.get("value", {})
		var request: Variant = envelope.get("request")
		if not request is Dictionary:
			return _local_failure("PENDING_PATCH_ENVELOPE_INVALID", "Pending PatchDecision has no validated request.")
		var canonical: Dictionary = await product_gateway.get_interaction(
			_new_request_context(), str(request.session_id), str(request.interaction_id),
		)
		if not canonical.get("ok", false):
			return canonical
		var canonical_receipt: Variant = canonical.get("value", {}).get("patch_decision")
		var receipt: Dictionary
		var outcome := "RECONCILED_EXISTING"
		if canonical_receipt is Dictionary:
			if not _patch_receipt_matches_request(canonical_receipt, request):
				return _local_failure("PENDING_PATCH_CANONICAL_MISMATCH", "Canonical PatchDecision differs from the persisted immutable request.")
			receipt = canonical_receipt.duplicate(true)
		else:
			var write: Dictionary = await product_gateway.record_patch_decision(
				_new_request_context(), str(request.session_id), str(request.interaction_id),
				str(request.patch_id), str(envelope.idempotency_key), request,
				str(envelope.request_body),
			)
			if not write.get("ok", false):
				return write
			if not _patch_receipt_matches_request(write.value, request):
				return _local_failure("PENDING_PATCH_RECEIPT_MISMATCH", "Replayed PatchDecision receipt differs from its persisted immutable request.")
			receipt = write.value.duplicate(true)
			outcome = "REPLAYED_EXACT_BODY"
		var finalized := await _finalize_patch_decision_receipt(slot, request, receipt)
		if not finalized.get("ok", false):
			return finalized
		outcomes.append({"slot": slot, "outcome": outcome, "receipt": receipt})
	return {"ok": true, "status": 200, "headers": {}, "value": {"had_pending": true, "outcomes": outcomes}}


func _finalize_patch_decision_receipt(slot: String, request: Dictionary, receipt: Dictionary) -> Dictionary:
	var store := _client_store()
	if store == null or not _patch_receipt_matches_request(receipt, request):
		return _local_failure("PATCH_DECISION_RECEIPT_MISMATCH", "PatchDecision receipt does not match its immutable request.")
	if str(request.decision) == "ACCEPT":
		if store.draft_state != WalnutClientStore.DraftState.CLEAN:
			return _local_failure("PATCH_LOCAL_EDIT_CONFLICT", "Canonical accepted Draft cannot replace dirty local editor bytes.")
		var canonical: Dictionary = await product_gateway.get_draft(
			_new_request_context(), str(request.session_id), str(request.draft_id),
		)
		if not canonical.get("ok", false):
			return canonical
		var draft_validation := ProductInteractionGateway.new(null)._validate_draft(
			canonical.value,
			str(request.session_id),
			str(request.draft_id),
		)
		if (
			not draft_validation.get("ok", false)
			or canonical.value.get("request_context", {}).get("actor") != authority_context.get("actor")
			or canonical.value.get("request_context", {}).get("content_ref") != authority_context.get("content_ref")
			or str(canonical.value.get("last_applied_patch_id", "")) != str(receipt.get("patch_id", ""))
		):
			return _local_failure(
				"PATCH_DECISION_DRAFT_CORRUPT",
				"Accepted canonical Draft violates its frozen source bundle, UTF-8 content hash, path, entrypoint, or x-draft-hash authority.",
			)
		if not _canonical_draft_matches_patch_receipt(canonical.value, receipt):
			return _local_failure("PATCH_DECISION_DRAFT_MISMATCH", "Accepted PatchDecision does not match canonical Draft.")
		if not store.commit_accepted_patch_draft(canonical.value, request, receipt):
			return _local_failure(
				"PATCH_ACTIVATION_INVALIDATION_PERSIST_FAILED",
				"Accepted Draft could not durably invalidate the previous Activation.",
			)
		certified_build.clear()
		certified_build_draft_authority.clear()
		active_activation.clear()
		active_skill_tuple.clear()
		store.set_flow(WalnutClientStore.FlowState.READY)
	if not store.clear_pending_operation(slot):
		return _local_failure("PATCH_DECISION_CLEAR_FAILED", "PatchDecision envelope could not be durably cleared.")
	patch_decision_resolved.emit(
		str(request.interaction_id),
		str(request.patch_id),
		str(request.decision),
	)
	return {"ok": true, "status": 200, "headers": {}, "value": receipt.duplicate(true)}


func _close_successful_run(
	run: Dictionary,
	command: Dictionary,
	pre_world: Dictionary,
	interaction_cursor_before: int,
	presentation_after_sequence: int = 0,
) -> Dictionary:
	if not bool(run.get("terminal", false)) or str(run.get("status", "")) != "SUCCEEDED":
		return _local_failure("RUN_NOT_SUCCEEDED", "Only a terminal SUCCEEDED Run can enter world closure.")
	var feedback: Variant = run.get("agent_feedback")
	if (
		not feedback is Dictionary
		or str(feedback.get("source", "")) != "provider"
		or bool(feedback.get("degraded", true))
		or feedback.get("fallback_reason") != null
	):
		return _local_failure("RUN_PROVIDER_FEEDBACK_INVALID", "INT1 requires non-degraded provider feedback.")
	var world_application: Variant = run.get("world_application")
	var receipt: Variant = world_application.get("receipt") if world_application is Dictionary else null
	if not receipt is Dictionary or not _receipt_matches_pre_world(receipt, pre_world):
		return _local_failure("RUN_WORLD_RECEIPT_MISMATCH", "Run receipt does not advance the exact pre-Turn world cursor.")
	if not _command_result_matches_receipt(command.get("result"), receipt):
		return _local_failure("RUN_COMMAND_RECEIPT_MISMATCH", "Command WORLD_COMMIT result disagrees with Run receipt.")
	var evidence_result := await _recover_run_evidence(run, receipt)
	if not evidence_result.get("ok", false):
		return evidence_result
	var events_result := await _recover_receipt_events(receipt, int(pre_world.last_event_sequence), str(run.command_id))
	if not events_result.get("ok", false):
		return events_result
	var snapshot_result: Dictionary = await game_gateway.get_world_snapshot(
		_new_request_context(), str(receipt.world_id),
	)
	if not snapshot_result.get("ok", false):
		return snapshot_result
	var snapshot: Dictionary = snapshot_result.value
	if not _snapshot_matches_receipt(snapshot, receipt):
		return _local_failure("RUN_SNAPSHOT_RECEIPT_MISMATCH", "Snapshot does not exactly match the committed receipt.")
	var presentation_events: Array[Dictionary] = []
	var presentation_high_watermark := presentation_after_sequence
	if world_presentation_enabled:
		var presentation_result := await _recover_committed_presentation(
			run,
			command,
			receipt,
			pre_world,
			snapshot,
			presentation_after_sequence,
		)
		if not presentation_result.get("ok", false):
			# The Run is already committed. Restore its final Snapshot explicitly,
			# but propagate corruption so the UI never reports pseudo-success.
			_commit_authoritative_snapshot(snapshot)
			return presentation_result
		presentation_events = presentation_result.value.events.duplicate(true)
		presentation_high_watermark = int(presentation_result.value.high_watermark)
	var interactions := await _wait_for_interaction(
		str(run.session_id),
		str(run.turn_id),
		str(run.command_id),
		str(run.run_id),
		interaction_cursor_before,
		[],
		feedback,
	)
	if not interactions.get("ok", false):
		return interactions
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {
			"run": run.duplicate(true),
			"evidence": evidence_result.value,
			"events": events_result.value,
			"snapshot": snapshot,
			"presentation_events": presentation_events,
			"presentation_high_watermark": presentation_high_watermark,
			"interactions": interactions.value,
		},
	}


## An objective failure is a completed student-domain outcome, not an
## infrastructure failure. It must prove the exact failed Run and Evidence,
## prove that World authority did not move, then recover the final teaching or
## bug Interaction. A FAILED Command never enters this path.
func _close_failed_objective_run(
	run: Dictionary,
	command: Dictionary,
	pre_world: Dictionary,
	interaction_cursor_before: int,
) -> Dictionary:
	if (
		not bool(run.get("terminal", false))
		or str(run.get("status", "")) not in ["REJECTED", "FAILED"]
		or str(command.get("status", "")) != "REJECTED"
		or command.get("result") != null
		or command.get("evidence_refs") != run.get("evidence_refs")
	):
		return _local_failure(
			"OBJECTIVE_FAILURE_AUTHORITY_INVALID",
			"The rejected Command and objective-failed Run do not share one exact terminal authority.",
		)
	var feedback: Variant = run.get("agent_feedback")
	if (
		not feedback is Dictionary
		or str(feedback.get("source", "")) != "provider"
		or bool(feedback.get("degraded", true))
		or feedback.get("fallback_reason") != null
	):
		return _local_failure(
			"OBJECTIVE_FAILURE_PROVIDER_FEEDBACK_INVALID",
			"Objective failure requires final non-degraded Provider feedback.",
		)
	var world_application: Variant = run.get("world_application")
	if (
		not world_application is Dictionary
		or str(world_application.get("status", "")) not in ["NOT_ATTEMPTED", "REJECTED", "FAILED"]
		or world_application.get("receipt") != null
	):
		return _local_failure(
			"OBJECTIVE_FAILURE_WORLD_APPLICATION_INVALID",
			"Objective failure unexpectedly contains a World commit receipt or non-terminal World state.",
		)
	var evidence_result := await _recover_failed_run_evidence(run)
	if not evidence_result.get("ok", false):
		return evidence_result
	var world_result := await _prove_world_unchanged(pre_world)
	if not world_result.get("ok", false):
		return world_result
	var objective_roles: Array[String] = ["teaching_agent", "bug_agent"]
	var interactions := await _wait_for_interaction(
		str(run.session_id),
		str(run.turn_id),
		str(run.command_id),
		str(run.run_id),
		interaction_cursor_before,
		objective_roles,
		feedback,
	)
	if not interactions.get("ok", false):
		return interactions
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {
			"run": run.duplicate(true),
			"evidence": evidence_result.value,
			"events": world_result.value.events,
			"snapshot": world_result.value.snapshot,
			"interactions": interactions.value,
		},
	}


func _recover_failed_run_evidence(run: Dictionary) -> Dictionary:
	if not game_gateway.has_method("get_evidence"):
		return _local_failure("EVIDENCE_GATEWAY_UNAVAILABLE", "Game gateway lacks Evidence query support.")
	var refs: Variant = run.get("evidence_refs")
	if not refs is Array or refs.is_empty():
		return _local_failure("RUN_EVIDENCE_MISSING", "Objective-failed Run exposes no objective Evidence.")
	var recovered: Array[Dictionary] = []
	var skill_run_found := false
	for reference in refs:
		if not reference is Dictionary or str(reference.get("evidence_type", "")) == "WORLD_COMMIT":
			return _local_failure(
				"OBJECTIVE_FAILURE_WORLD_EVIDENCE_PRESENT",
				"Objective-failed Run must not reference WORLD_COMMIT Evidence.",
			)
		var result: Dictionary = await game_gateway.get_evidence(
			_new_request_context(), str(reference.get("evidence_id", "")),
		)
		if not result.get("ok", false):
			return result
		var evidence: Dictionary = result.value
		if evidence.get("evidence_ref") != reference:
			return _local_failure("RUN_EVIDENCE_IDENTITY_MISMATCH", "Evidence resource does not match its failed Run reference.")
		var payload: Variant = evidence.get("payload")
		var source: Variant = evidence.get("source")
		if not payload is Dictionary or not source is Dictionary:
			return _local_failure("RUN_EVIDENCE_IDENTITY_MISMATCH", "Objective Evidence has no canonical payload/source authority.")
		if str(payload.get("evidence_kind", "")) == "WORLD_COMMIT":
			return _local_failure(
				"OBJECTIVE_FAILURE_WORLD_EVIDENCE_PRESENT",
				"Objective-failed Run unexpectedly resolved WORLD_COMMIT Evidence.",
			)
		if str(payload.get("evidence_kind", "")) == "SKILL_RUN":
			if (
				str(payload.get("run_id", "")) != str(run.run_id)
				or str(source.get("source_type", "")) != "SKILL_RUN"
				or str(source.get("source_id", "")) != str(run.run_id)
				or str(source.get("command_id", "")) != str(run.command_id)
			):
				return _local_failure("RUN_EVIDENCE_IDENTITY_MISMATCH", "SKILL_RUN Evidence names another Run or Command.")
			skill_run_found = true
		recovered.append(evidence.duplicate(true))
	if not skill_run_found:
		return _local_failure("SKILL_RUN_EVIDENCE_MISSING", "Objective failure requires exact SKILL_RUN Evidence.")
	return {"ok": true, "status": 200, "headers": {}, "value": recovered}


func _prove_world_unchanged(pre_world: Dictionary) -> Dictionary:
	if (
		not game_gateway.has_method("get_world_events")
		or not game_gateway.has_method("get_world_snapshot")
	):
		return _local_failure(
			"WORLD_RECOVERY_GATEWAY_UNAVAILABLE",
			"Objective failure requires HTTP Events and Snapshot authority.",
		)
	var initial_cursor := int(pre_world.get("last_event_sequence", -1))
	var world_id := str(pre_world.get("world_id", ""))
	if world_id.is_empty() or initial_cursor < 0:
		return _local_failure("OBJECTIVE_FAILURE_PRE_WORLD_INVALID", "The objective failure has no exact pre-Turn World authority.")
	var events_result: Dictionary = await game_gateway.get_world_events(
		_new_request_context(), world_id, initial_cursor, 100,
	)
	if not events_result.get("ok", false):
		return events_result
	var page: Dictionary = events_result.value
	if (
		str(page.get("world_id", "")) != world_id
		or int(page.get("snapshot_revision", -1)) != int(pre_world.get("revision", -2))
		or not page.get("events") is Array
		or not page.events.is_empty()
		or int(page.get("from_sequence", initial_cursor)) != initial_cursor
		or int(page.get("to_sequence", initial_cursor)) != initial_cursor
		or int(page.get("next_after_sequence", -1)) != initial_cursor
		or bool(page.get("has_more", true))
	):
		return _local_failure(
			"OBJECTIVE_FAILURE_WORLD_EVENTS_CHANGED",
			"Objective-failed Run advanced or exposed World events.",
		)
	var snapshot_result: Dictionary = await game_gateway.get_world_snapshot(
		_new_request_context(), world_id,
	)
	if not snapshot_result.get("ok", false):
		return snapshot_result
	var snapshot: Dictionary = snapshot_result.value
	if not _same_world_authority(snapshot, pre_world):
		return _local_failure(
			"OBJECTIVE_FAILURE_WORLD_SNAPSHOT_CHANGED",
			"Objective-failed Run changed the authoritative World Snapshot.",
		)
	var store := _client_store()
	if store == null or not store.replace_world(snapshot):
		return _local_failure("RUN_SNAPSHOT_COMMIT_FAILED", "Verified unchanged Snapshot could not replace client World state.")
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {"events": [], "snapshot": snapshot.duplicate(true)},
	}


func _recover_run_evidence(run: Dictionary, receipt: Dictionary) -> Dictionary:
	if not game_gateway.has_method("get_evidence"):
		return _local_failure("EVIDENCE_GATEWAY_UNAVAILABLE", "Game gateway lacks Evidence query support.")
	var refs: Variant = run.get("evidence_refs")
	if not refs is Array or refs.is_empty():
		return _local_failure("RUN_EVIDENCE_MISSING", "Run exposes no Evidence references.")
	var recovered: Array[Dictionary] = []
	var world_commit_found := false
	for reference in refs:
		if not reference is Dictionary:
			return _local_failure("RUN_EVIDENCE_REF_INVALID", "Run contains an invalid Evidence reference.")
		var result: Dictionary = await game_gateway.get_evidence(
			_new_request_context(), str(reference.get("evidence_id", "")),
		)
		if not result.get("ok", false):
			return result
		var evidence: Dictionary = result.value
		if evidence.get("evidence_ref") != reference:
			return _local_failure("RUN_EVIDENCE_IDENTITY_MISMATCH", "Evidence resource does not match its Run reference.")
		var payload: Variant = evidence.get("payload")
		if payload is Dictionary and str(payload.get("evidence_kind", "")) == "SKILL_RUN":
			if str(payload.get("run_id", "")) != str(run.run_id):
				return _local_failure("RUN_EVIDENCE_IDENTITY_MISMATCH", "SKILL_RUN Evidence names another Run.")
		if payload is Dictionary and str(payload.get("evidence_kind", "")) == "WORLD_COMMIT":
			if not _world_evidence_matches_receipt(payload, receipt):
				return _local_failure("WORLD_EVIDENCE_RECEIPT_MISMATCH", "WORLD_COMMIT Evidence disagrees with receipt.")
			if str(evidence.get("source", {}).get("command_id", "")) != str(run.command_id):
				return _local_failure("WORLD_EVIDENCE_COMMAND_MISMATCH", "WORLD_COMMIT Evidence names another Command.")
			world_commit_found = true
		recovered.append(evidence.duplicate(true))
	if not world_commit_found:
		return _local_failure("WORLD_EVIDENCE_MISSING", "Run closure requires exact WORLD_COMMIT Evidence.")
	return {"ok": true, "status": 200, "headers": {}, "value": recovered}


func _recover_receipt_events(receipt: Dictionary, initial_cursor: int, command_id: String) -> Dictionary:
	if not game_gateway.has_method("get_world_events"):
		return _local_failure("WORLD_EVENTS_GATEWAY_UNAVAILABLE", "Game gateway lacks HTTP Events query support.")
	var cursor := initial_cursor
	var events: Array[Dictionary] = []
	var pages := 0
	while cursor < int(receipt.last_event_sequence) and pages < 1000:
		pages += 1
		var result: Dictionary = await game_gateway.get_world_events(
			_new_request_context(), str(receipt.world_id), cursor, 100,
		)
		if not result.get("ok", false):
			return result
		var page: Dictionary = result.value
		if (
			str(page.get("world_id", "")) != str(receipt.world_id)
			or int(page.get("snapshot_revision", -1)) != int(receipt.world_revision)
			or not page.get("events") is Array
			or page.events.is_empty()
		):
			return _local_failure("WORLD_EVENTS_RECEIPT_MISMATCH", "HTTP Events page does not cover the receipt.")
		var expected_sequence := cursor + 1
		for event in page.events:
			if int(event.get("sequence", -1)) != expected_sequence:
				return _local_failure("WORLD_EVENTS_GAP", "HTTP Events are not contiguous from the requested cursor.")
			if int(event.get("sequence", -1)) > int(receipt.last_event_sequence):
				return _local_failure("WORLD_EVENTS_OVERRUN", "HTTP Events crossed the receipt sequence boundary.")
			if str(event.get("command_id", "")) != command_id:
				return _local_failure("WORLD_EVENT_COMMAND_MISMATCH", "Receipt event belongs to another Command.")
			events.append(event.duplicate(true))
			expected_sequence += 1
		var next_cursor := int(page.get("next_after_sequence", -1))
		if next_cursor != expected_sequence - 1 or next_cursor <= cursor:
			return _local_failure("WORLD_EVENTS_CURSOR_STALLED", "HTTP Events pagination made no progress.")
		cursor = next_cursor
		if not bool(page.get("has_more", false)) and cursor < int(receipt.last_event_sequence):
			return _local_failure("WORLD_EVENTS_INCOMPLETE", "HTTP Events ended before the receipt cursor.")
	if cursor != int(receipt.last_event_sequence):
		return _local_failure("WORLD_EVENTS_INCOMPLETE", "HTTP Events did not close the exact receipt cursor.")
	return {"ok": true, "status": 200, "headers": {}, "value": events}


func _recover_committed_presentation(
	run: Dictionary,
	command: Dictionary,
	receipt: Dictionary,
	pre_world: Dictionary,
	snapshot: Dictionary,
	after_sequence: int,
) -> Dictionary:
	if (
		world_presentation_gateway == null
		or not world_presentation_gateway.has_method("get_world_presentation_events")
		or world_event_player == null
	):
		return _local_failure("PRESENTATION_GATEWAY_UNAVAILABLE", "Committed World presentation authority is not configured.")
	var cursor := after_sequence
	var high_watermark := -1
	var events: Array[Dictionary] = []
	var pages := 0
	while pages < 1000:
		pages += 1
		var page_result: Dictionary = await world_presentation_gateway.get_world_presentation_events(
			RequestContextFactory.new_wire_attempt(),
			str(receipt.world_id),
			cursor,
			100,
		)
		if not page_result.get("ok", false):
			return page_result
		var page: Dictionary = page_result.value
		if not _presentation_page_matches_authority(page):
			return _local_failure("PRESENTATION_RUN_AUTHORITY_MISMATCH", "Presentation page actor/content authority disagrees with the successful Run Session.")
		if not _presentation_page_matches_snapshot(page, snapshot):
			return _local_failure("PRESENTATION_FINAL_SNAPSHOT_MISMATCH", "Presentation page does not bind the exact terminal Snapshot.")
		if high_watermark < 0:
			high_watermark = int(page.presentation_high_watermark)
		elif high_watermark != int(page.presentation_high_watermark):
			return _local_failure("PRESENTATION_HIGH_WATERMARK_CHANGED", "Presentation high watermark changed during one closure read.")
		for event in page.events:
			if (
				str(event.session_id) != str(run.session_id)
				or str(event.turn_id) != str(run.turn_id)
				or str(event.command_id) != str(command.command_id)
				or str(event.run_id) != str(run.run_id)
				or str(event.world_id) != str(receipt.world_id)
				or int(event.world_revision) != int(receipt.world_revision)
				or int(event.final_snapshot_revision) != int(snapshot.revision)
				or int(event.final_world_event_sequence) != int(snapshot.last_event_sequence)
				or str(event.final_snapshot_state_hash) != str(snapshot.state_hash)
			):
				return _local_failure("PRESENTATION_RUN_IDENTITY_MISMATCH", "Presentation event belongs to another committed Run or Snapshot.")
			events.append(event.duplicate(true))
		var next_cursor := int(page.next_after_sequence)
		if bool(page.has_more):
			if page.events.is_empty() or next_cursor <= cursor:
				return _local_failure("PRESENTATION_CURSOR_STALLED", "Presentation pagination made no progress.")
			cursor = next_cursor
			continue
		cursor = next_cursor
		break
	if pages >= 1000 and cursor < high_watermark:
		return _local_failure("PRESENTATION_PAGE_LIMIT", "Presentation pagination exceeded its safety bound.")
	if (
		events.is_empty()
		or high_watermark < 1
		or cursor != high_watermark
		or int(events[0].sequence) != after_sequence + 1
		or int(events[0].action_index) != 0
		or str(events[0].state_hash_before) != str(pre_world.state_hash)
		or int(events[-1].action_index) != int(events[-1].action_count) - 1
		or str(events[-1].state_hash_after) != str(snapshot.state_hash)
	):
		return _local_failure("PRESENTATION_COMMIT_INCOMPLETE", "Presentation events do not cover the exact successful World transition.")
	if world_event_player.has_method("validate_batch"):
		var batch_validation: Dictionary = world_event_player.call("validate_batch", events, after_sequence)
		if not batch_validation.get("ok", false):
			return batch_validation
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {"events": events, "high_watermark": high_watermark},
	}


func _presentation_page_matches_snapshot(page: Dictionary, snapshot: Dictionary) -> bool:
	return (
		str(page.get("world_id", "")) == str(snapshot.get("world_id", ""))
		and int(page.get("snapshot_revision", -1)) == int(snapshot.get("revision", -2))
		and int(page.get("snapshot_last_event_sequence", -1)) == int(snapshot.get("last_event_sequence", -2))
		and str(page.get("snapshot_state_hash", "")) == str(snapshot.get("state_hash", ""))
	)


func _presentation_page_matches_authority(page: Dictionary) -> bool:
	var origin: Variant = page.get("request_context")
	var expected_actor: Variant = authority_context.get("actor")
	var expected_content: Variant = authority_context.get("content_ref")
	var session_origin: Variant = authoritative_session.get("request_context")
	return (
		origin is Dictionary
		and expected_actor is Dictionary
		and expected_content is Dictionary
		and session_origin is Dictionary
		and origin.get("actor") == expected_actor
		and origin.get("content_ref") == expected_content
		and session_origin.get("actor") == expected_actor
		and session_origin.get("content_ref") == expected_content
	)


func _project_replay_snapshot(snapshot: Dictionary) -> bool:
	return (
		world_presentation_renderer != null
		and world_presentation_renderer.has_method("project_replay_snapshot")
		and bool(world_presentation_renderer.call("project_replay_snapshot", snapshot.duplicate(true)))
	)


func _commit_authoritative_snapshot(snapshot: Dictionary) -> bool:
	var store := _client_store()
	if store == null:
		return false
	if (
		world_presentation_renderer != null
		and world_presentation_renderer.has_method("can_project_authoritative_snapshot")
		and not bool(world_presentation_renderer.call("can_project_authoritative_snapshot", snapshot))
	):
		return false
	if not store.replace_world(snapshot):
		return false
	if (
		world_presentation_renderer != null
		and world_presentation_renderer.has_method("last_authoritative_projection_succeeded")
		and not bool(world_presentation_renderer.call("last_authoritative_projection_succeeded", snapshot))
	):
		return false
	return true


func _wait_for_interaction(
	session_id: String,
	turn_id: String,
	command_id: String,
	run_id: String,
	after_sequence: int,
	required_roles: Array[String] = [],
	expected_feedback: Dictionary = {},
) -> Dictionary:
	if product_gateway == null or not product_gateway.has_method("list_interactions"):
		return _local_failure("PRODUCT_GATEWAY_UNAVAILABLE", "Product AgentInteraction gateway is required for closure.")
	var deadline_seconds := float(polling_settings.get("interaction_deadline_seconds", DEFAULT_INTERACTION_DEADLINE_SECONDS))
	var delay_seconds := float(polling_settings.get("interaction_delay_seconds", DEFAULT_INTERACTION_DELAY_SECONDS))
	if deadline_seconds <= 0.0 or delay_seconds < 0.0:
		return _local_failure("INTERACTION_POLL_CONFIGURATION_INVALID", "AgentInteraction polling settings are invalid.")
	var deadline_msec := Time.get_ticks_msec() + ceili(deadline_seconds * 1000.0)
	var cursor := after_sequence
	var recovered: Array[Dictionary] = []
	while Time.get_ticks_msec() < deadline_msec:
		var page: Dictionary = await product_gateway.list_interactions(
			_new_request_context(), session_id, cursor, 50,
		)
		if not page.get("ok", false):
			if not _retryable_result(page):
				return page
			var retry_delay := _retry_delay(page, delay_seconds)
			var retry_remaining := float(deadline_msec - Time.get_ticks_msec()) / 1000.0
			if retry_delay >= retry_remaining:
				break
			await _wait_seconds(retry_delay)
			continue
		var value: Dictionary = page.value
		for interaction in value.interactions:
			recovered.append(interaction.duplicate(true))
			if (
				_interaction_matches_turn(interaction, session_id, turn_id, command_id, run_id)
				and (
					expected_feedback.is_empty()
					or interaction.get("feedback") == expected_feedback
				)
				and (
					required_roles.is_empty()
					or _objective_failure_interaction_matches(
						interaction,
						required_roles,
						expected_feedback,
					)
				)
			):
				var store := _client_store()
				if store != null:
					store.set_interaction_cursor(int(interaction.sequence))
				interactions_recovered.emit(recovered)
				return {"ok": true, "status": 200, "headers": {}, "value": recovered}
		var next_cursor := int(value.next_after_sequence)
		if bool(value.has_more):
			if next_cursor <= cursor:
				return _local_failure("INTERACTION_CURSOR_STALLED", "AgentInteraction pagination made no progress.")
			cursor = next_cursor
			continue
		cursor = maxi(cursor, next_cursor)
		var store := _client_store()
		if store != null:
			store.set_interaction_cursor(cursor)
		if delay_seconds > 0.0:
			var remaining := float(deadline_msec - Time.get_ticks_msec()) / 1000.0
			if delay_seconds >= remaining:
				break
			await _wait_seconds(delay_seconds)
	return _local_failure("INTERACTION_RECONCILIATION_TIMEOUT", "Matching AgentInteraction did not appear before the deadline.", true)


func _new_poller() -> RefCounted:
	return CommandPoller.new(
		game_gateway,
		Callable(self, "_new_request_context"),
		polling_settings,
	)


func _new_request_context() -> Dictionary:
	var actor: Variant = authority_context.get("actor")
	var content_ref: Variant = authority_context.get("content_ref")
	if actor is Dictionary and content_ref is Dictionary:
		return RequestContextFactory.new_attempt(actor, content_ref)
	if draft_context.has("actor") and draft_context.get("actor") is Dictionary and draft_context.get("content_ref") is Dictionary:
		return RequestContextFactory.new_attempt(draft_context.actor, draft_context.content_ref)
	var legacy: Variant = draft_context.get("attempt")
	if legacy is Dictionary:
		var legacy_actor: Variant = legacy.get("actor")
		var legacy_content: Variant = legacy.get("content_ref")
		if legacy_actor is Dictionary and legacy_content is Dictionary:
			return RequestContextFactory.new_attempt(legacy_actor, legacy_content)
		return legacy.duplicate(true)
	return {}


func _apply_saved_draft(store: WalnutClientStore, value: Dictionary, submitted_source: String) -> void:
	if store.local_source == submitted_source:
		store.set_draft(value)
	else:
		store.set_draft_preserving_local_source(value, store.local_source)


func _has_activation_context() -> bool:
	for field in ["expected_registry_revision", "world_id", "agent_profile_id"]:
		if not activation_context.has(field):
			return false
	return (
		int(activation_context.expected_registry_revision) >= 0
		and not str(activation_context.world_id).is_empty()
		and not str(activation_context.agent_profile_id).is_empty()
	)


func _activation_matches_build(activation: Dictionary) -> bool:
	var artifact: Variant = certified_build.get("artifact")
	var certification: Variant = certified_build.get("certification")
	var scope: Variant = activation.get("activation_scope")
	if not artifact is Dictionary or not certification is Dictionary or not scope is Dictionary:
		return false
	return (
		str(activation.get("skill_id", "")) == str(certified_build.get("skill_id", ""))
		and str(activation.get("skill_version_id", "")) == str(certified_build.get("skill_version_id", ""))
		and str(activation.get("certification_id", "")) == str(certification.get("certification_id", ""))
		and str(activation.get("artifact_sha256", "")) == str(artifact.get("artifact_sha256", ""))
		and str(scope.get("world_id", "")) == str(activation_context.world_id)
		and str(scope.get("agent_profile_id", "")) == str(activation_context.agent_profile_id)
		and int(activation.get("previous_registry_revision", -1)) == int(activation_context.expected_registry_revision)
		and int(activation.get("registry_revision", -1)) == int(activation_context.expected_registry_revision) + 1
	)


func _tuple_from_activation(activation: Dictionary) -> Dictionary:
	return {
		"activation_id": activation.activation_id,
		"skill_id": activation.skill_id,
		"skill_version_id": activation.skill_version_id,
		"artifact_sha256": activation.artifact_sha256,
		"certification_id": activation.certification_id,
		"registry_revision": activation.registry_revision,
		"activated_at": activation.activated_at,
	}


func _valid_active_tuple(value: Dictionary) -> bool:
	var required := [
		"activation_id", "skill_id", "skill_version_id", "artifact_sha256",
		"certification_id", "registry_revision", "activated_at",
	]
	if value.size() != required.size():
		return false
	for field in required:
		if not value.has(field):
			return false
	return (
		ContractValidator.validate_identifier(value.skill_id).ok
		and ContractValidator.validate_identifier(value.skill_version_id).ok
		and ContractValidator.validate_identifier(value.certification_id).ok
		and str(value.artifact_sha256).length() == 64
		and int(value.registry_revision) >= 1
	)


func _binding_from_active_tuple(value: Dictionary) -> Dictionary:
	return {
		"skill_id": value.skill_id,
		"skill_version_id": value.skill_version_id,
		"artifact_sha256": value.artifact_sha256,
		"certification_id": value.certification_id,
	}


func _run_matches_turn(run: Dictionary, session_id: String, turn_id: String, command_id: String) -> bool:
	var skill: Variant = run.get("skill")
	if not skill is Dictionary or not _valid_active_tuple(active_skill_tuple):
		return false
	return (
		str(run.get("session_id", "")) == session_id
		and str(run.get("turn_id", "")) == turn_id
		and str(run.get("command_id", "")) == command_id
		and skill == _binding_from_active_tuple(active_skill_tuple)
	)


func _receipt_matches_pre_world(receipt: Dictionary, pre_world: Dictionary) -> bool:
	return (
		str(receipt.get("world_id", "")) == str(pre_world.get("world_id", ""))
		and int(receipt.get("previous_revision", -1)) == int(pre_world.get("revision", -2))
		and int(receipt.get("world_revision", -1)) == int(pre_world.get("revision", -2)) + 1
		and int(receipt.get("first_event_sequence", -1)) == int(pre_world.get("last_event_sequence", -2)) + 1
		and int(receipt.get("last_event_sequence", -1)) >= int(receipt.get("first_event_sequence", 0))
	)


func _command_result_matches_receipt(value: Variant, receipt: Dictionary) -> bool:
	if not value is Dictionary or str(value.get("result_type", "")) != "WORLD_COMMIT":
		return false
	for field in ["world_id", "previous_revision", "world_revision", "first_event_sequence", "last_event_sequence"]:
		if value.get(field) != receipt.get(field):
			return false
	return true


func _world_evidence_matches_receipt(value: Dictionary, receipt: Dictionary) -> bool:
	for field in ["world_id", "previous_revision", "world_revision", "first_event_sequence", "last_event_sequence", "state_hash"]:
		if value.get(field) != receipt.get(field):
			return false
	return true


func _snapshot_matches_receipt(snapshot: Dictionary, receipt: Dictionary) -> bool:
	return (
		str(snapshot.get("world_id", "")) == str(receipt.get("world_id", ""))
		and int(snapshot.get("revision", -1)) == int(receipt.get("world_revision", -2))
		and int(snapshot.get("last_event_sequence", -1)) == int(receipt.get("last_event_sequence", -2))
		and str(snapshot.get("state_hash", "")) == str(receipt.get("state_hash", ""))
	)


func _same_world_authority(snapshot: Dictionary, pre_world: Dictionary) -> bool:
	for field in [
		"world_id", "revision", "last_event_sequence", "state_schema_version",
		"state_hash", "world_rules_version", "state",
	]:
		if snapshot.get(field) != pre_world.get(field):
			return false
	return true


func _interaction_matches_turn(interaction: Dictionary, session_id: String, turn_id: String, command_id: String, run_id: String) -> bool:
	var feedback: Variant = interaction.get("feedback")
	if not feedback is Dictionary:
		return false
	return (
		str(interaction.get("session_id", "")) == session_id
		and str(interaction.get("turn_id", "")) == turn_id
		and str(feedback.get("turn_id", "")) == turn_id
		and str(feedback.get("command_id", "")) == command_id
		and (
			feedback.get("run_id") == null
			if run_id.is_empty()
			else str(feedback.get("run_id", "")) == run_id
		)
		and str(feedback.get("source", "")) == "provider"
		and not bool(feedback.get("degraded", true))
		and feedback.get("fallback_reason") == null
	)


func _objective_failure_interaction_matches(
	interaction: Dictionary,
	required_roles: Array[String],
	expected_feedback: Dictionary,
) -> bool:
	var source: Variant = interaction.get("projection_source")
	return (
		str(interaction.get("role", "")) in required_roles
		and interaction.get("feedback") == expected_feedback
		and source is Dictionary
		and str(source.get("source_type", "")) == "AGENT_TURN_PRODUCT_PROJECTION"
		and interaction.get("skill_patch") == null
	)


func _matching_failed_interaction(interactions: Variant, run: Dictionary) -> Dictionary:
	if not interactions is Array:
		return {}
	for index in range(interactions.size() - 1, -1, -1):
		var interaction_value: Variant = interactions[index]
		if not interaction_value is Dictionary:
			continue
		var interaction: Dictionary = interaction_value
		var feedback: Variant = interaction.get("feedback")
		if (
			feedback is Dictionary
			and str(interaction.get("session_id", "")) == str(run.get("session_id", ""))
			and str(interaction.get("turn_id", "")) == str(run.get("turn_id", ""))
			and str(feedback.get("command_id", "")) == str(run.get("command_id", ""))
			and str(feedback.get("run_id", "")) == str(run.get("run_id", ""))
			and feedback.get("evidence_refs") == run.get("evidence_refs")
		):
			return interaction.duplicate(true)
	return {}


func _recovered_build_matches_active(build: Dictionary) -> bool:
	var artifact: Variant = build.get("artifact")
	var certification: Variant = build.get("certification")
	return (
		_valid_active_tuple(active_skill_tuple)
		and str(build.get("status", "")) == "CERTIFIED"
		and bool(build.get("terminal", false))
		and artifact is Dictionary
		and certification is Dictionary
		and str(build.get("skill_id", "")) == str(active_skill_tuple.get("skill_id", ""))
		and str(build.get("skill_version_id", "")) == str(active_skill_tuple.get("skill_version_id", ""))
		and str(artifact.get("artifact_sha256", "")) == str(active_skill_tuple.get("artifact_sha256", ""))
		and str(certification.get("certification_id", "")) == str(active_skill_tuple.get("certification_id", ""))
		and build.get("request_context", {}).get("actor") == authority_context.get("actor")
		and build.get("request_context", {}).get("content_ref") == authority_context.get("content_ref")
	)


func _can_preserve_certified_build_after_authority_refresh(
	build: Dictionary,
	draft_authority: Dictionary,
	previous_session: Dictionary,
	previous_active: Dictionary,
) -> bool:
	var store := _client_store()
	var draft: Variant = store.draft if store != null else null
	var artifact: Variant = build.get("artifact")
	if (
		store == null
		or store.draft_state != WalnutClientStore.DraftState.CLEAN
		or not draft is Dictionary
		or not artifact is Dictionary
		or not ContractValidator.validate_skill_build(build).ok
		or not _recovered_build_matches_active(build)
		or not _valid_active_tuple(previous_active)
		or previous_active != active_skill_tuple
		or not _closed_dictionary(draft_authority, [
			"build_id", "session_id", "draft_id", "skill_id", "draft_revision",
			"draft_sha256", "source_bundle_sha256",
		])
		or not _sessions_allow_certified_build_preservation(
			previous_session, store.authoritative_session,
		)
	):
		return false
	var session_id := str(authoritative_session.get("session_id", ""))
	var draft_id := str(draft.get("draft_id", ""))
	var draft_validation := ProductInteractionGateway.new(null)._validate_draft(
		draft, session_id, draft_id,
	)
	var source_bundle: Variant = draft.get("source_bundle")
	var source_bundle_sha256 := (
		_canonical_source_bundle_sha256(source_bundle)
		if source_bundle is Dictionary
		else ""
	)
	return (
		draft_validation.get("ok", false)
		and not source_bundle_sha256.is_empty()
		and str(draft_authority.get("build_id", "")) == str(build.get("build_id", ""))
		and str(draft_authority.get("session_id", "")) == session_id
		and str(draft_authority.get("draft_id", "")) == draft_id
		and str(draft_authority.get("skill_id", "")) == str(build.get("skill_id", ""))
		and str(draft_authority.get("skill_id", "")) == str(draft.get("skill_id", ""))
		and int(draft_authority.get("draft_revision", -1)) == int(draft.get("revision", -2))
		and str(draft_authority.get("draft_sha256", "")) == str(draft.get("draft_sha256", ""))
		and str(draft_authority.get("source_bundle_sha256", "")) == source_bundle_sha256
		and str(artifact.get("source_sha256", "")) == source_bundle_sha256
		and draft.get("content_ref") == authority_context.get("content_ref")
		and draft.get("request_context", {}).get("actor") == authority_context.get("actor")
		and draft.get("request_context", {}).get("content_ref") == authority_context.get("content_ref")
	)


func _sessions_allow_certified_build_preservation(
	previous_session: Dictionary,
	stored_session: Dictionary,
) -> bool:
	var expected_projection: Dictionary = {}
	for session_value in [previous_session, authoritative_session, stored_session]:
		if not ContractValidator.validate_agent_session(session_value).ok:
			return false
		var origin: Variant = session_value.get("request_context")
		if (
			str(session_value.get("status", "")) != "ACTIVE"
			or str(session_value.get("world_id", "")) != str(activation_context.get("world_id", ""))
			or str(session_value.get("agent_profile_id", "")) != str(activation_context.get("agent_profile_id", ""))
			or str(session_value.get("learner_id", "")) != str(authority_context.get("actor", {}).get("actor_id", ""))
			or str(session_value.get("channel", "")) != "GAME"
			or session_value.get("content") != authority_context.get("content_ref")
			or not origin is Dictionary
			or origin.get("actor") != authority_context.get("actor")
			or origin.get("content_ref") != authority_context.get("content_ref")
		):
			return false
		var projection: Dictionary = session_value.duplicate(true)
		projection.erase("last_turn_sequence")
		projection.erase("updated_at")
		if expected_projection.is_empty():
			expected_projection = projection
		elif projection != expected_projection:
			return false
	return true


func _patch_failure_recovery_failure(code: String, message: String) -> Dictionary:
	var failure := _local_failure(code, message)
	patch_failure_recovery_status = failure.duplicate(true)
	return failure


func _patch_failure_recovery_failure_from_result(code: String, result: Dictionary) -> Dictionary:
	var error: Variant = result.get("error")
	var message := "A canonical restart authority GET failed closed."
	if error is Dictionary and not str(error.get("message", "")).is_empty():
		message = str(error.message)
	return _patch_failure_recovery_failure(code, message)


func _run_id_from_command(command: Dictionary) -> String:
	var links: Variant = command.get("links")
	if not links is Dictionary:
		return ""
	var run_link := str(links.get("run", ""))
	var prefix := "/v1/runs/"
	if not run_link.begins_with(prefix):
		return ""
	var run_id := run_link.substr(prefix.length())
	if run_id.is_empty() or run_id.contains("/") or run_id.contains("?") or run_id.contains("#"):
		return ""
	return run_id


func _command_id_from_submission(submission: Dictionary) -> String:
	if submission.get("ok", false):
		var accepted: Variant = submission.get("value")
		if accepted is Dictionary:
			return str(accepted.get("command_id", ""))
	var error: Variant = submission.get("error")
	if error is Dictionary:
		var nested: Variant = error.get("error")
		var code := str(error.get("code", nested.get("code", "") if nested is Dictionary else ""))
		if code == "UNKNOWN_COMMIT_STATE":
			return str(error.get("command_id", ""))
	return ""


func _has_build_context() -> bool:
	for field in ["compiler_profile", "test_suite_version", "requested_capabilities"]:
		if not build_context.has(field):
			return false
	return build_context.requested_capabilities is Array


func _build_source_bundle(canonical_draft: Dictionary, editor_source: String) -> Dictionary:
	var source_bundle: Variant = canonical_draft.get("source_bundle")
	if not source_bundle is Dictionary or not source_bundle.get("files") is Array:
		return {}
	var bundle: Dictionary = source_bundle.duplicate(true)
	var entrypoint := str(bundle.get("entrypoint", ""))
	for index in range(bundle.files.size()):
		var file: Variant = bundle.files[index]
		if file is Dictionary and str(file.get("path", "")) == entrypoint:
			file["content"] = editor_source
			file["content_sha256"] = editor_source.sha256_text()
			bundle.files[index] = file
			return bundle
	return {}


func _source_identity(bundle: Dictionary) -> String:
	var parts: Array[String] = []
	for file in bundle.get("files", []):
		parts.append("%s:%s" % [str(file.get("path", "")), str(file.get("content_sha256", ""))])
	parts.sort()
	return "|".join(parts).sha256_text()


func _source_bundle_entrypoint_source(value: Variant) -> String:
	if not value is Dictionary or not value.get("files") is Array:
		return ""
	var entrypoint := str(value.get("entrypoint", ""))
	for file in value.files:
		if file is Dictionary and str(file.get("path", "")) == entrypoint:
			return str(file.get("content", ""))
	return ""


func _product_write_reconciliation(result: Dictionary, resource_type: String, session_id: String, resource_id: String) -> Dictionary:
	if result.get("ok", false) or int(result.get("status", 0)) != 503 or not result.get("headers") is Dictionary:
		return {}
	var error: Variant = result.get("error")
	if not error is Dictionary or str(error.get("status", "")) != "RECONCILE" or error.get("data") != null:
		return {}
	var reconciliation: Variant = error.get("reconciliation")
	if not reconciliation is Dictionary:
		return {}
	if (
		str(reconciliation.get("resource_type", "")) != resource_type
		or str(reconciliation.get("session_id", "")) != session_id
		or str(reconciliation.get("resource_id", "")) != resource_id
		or str(result.headers.get("location", "")) != str(reconciliation.get("resource_url", ""))
	):
		return {}
	return reconciliation.duplicate(true)


func _canonical_draft_matches_write(canonical: Variant, request: Dictionary) -> bool:
	if not canonical is Dictionary:
		return false
	return (
		str(canonical.get("session_id", "")) == str(request.get("session_id", ""))
		and str(canonical.get("draft_id", "")) == str(request.get("draft_id", ""))
		and str(canonical.get("skill_id", "")) == str(request.get("skill_id", ""))
		and canonical.get("content_ref") == request.get("content_ref")
		and str(canonical.get("display_name", "")) == str(request.get("display_name", ""))
		and canonical.get("source_bundle") == request.get("source_bundle")
	)


func _patch_receipt_matches_request(receipt: Variant, request: Dictionary) -> bool:
	if not receipt is Dictionary:
		return false
	var fields := {
		"decision_id": "decision_id", "session_id": "session_id", "turn_id": "turn_id",
		"interaction_id": "interaction_id", "patch_id": "patch_id", "patch_sha256": "patch_sha256",
		"draft_id": "draft_id", "skill_id": "skill_id", "decision": "decision",
		"reason_code": "reason_code", "draft_revision_before": "base_draft_revision",
		"draft_sha256_before": "base_draft_sha256",
	}
	for receipt_field in fields:
		if receipt.get(receipt_field) != request.get(fields[receipt_field]):
			return false
	var accepted := str(request.get("decision", "")) == "ACCEPT"
	return (
		int(receipt.get("interaction_revision_before", -1)) == int(request.get("expected_interaction_revision", -2))
		and int(receipt.get("interaction_revision_after", -1)) == int(request.get("expected_interaction_revision", -2)) + 1
		and bool(receipt.get("draft_updated", false)) == accepted
		and int(receipt.get("draft_revision_after", -1)) == int(request.get("base_draft_revision", -1)) + (1 if accepted else 0)
		and str(receipt.get("draft_sha256_after", "")) == (str(request.get("result_draft_sha256", "")) if accepted else str(request.get("base_draft_sha256", "")))
	)


func _canonical_draft_matches_patch_receipt(canonical: Variant, receipt: Variant) -> bool:
	if not canonical is Dictionary or not receipt is Dictionary:
		return false
	return (
		str(canonical.get("session_id", "")) == str(receipt.get("session_id", ""))
		and str(canonical.get("draft_id", "")) == str(receipt.get("draft_id", ""))
		and str(canonical.get("skill_id", "")) == str(receipt.get("skill_id", ""))
		and int(canonical.get("revision", -1)) == int(receipt.get("draft_revision_after", -2))
		and str(canonical.get("draft_sha256", "")) == str(receipt.get("draft_sha256_after", ""))
	)


func _verify_build_source_hash(build: Dictionary, submitted_bundle: Dictionary) -> Dictionary:
	var artifact: Variant = build.get("artifact")
	if artifact == null:
		return {"ok": true}
	if not artifact is Dictionary or not artifact.get("source_sha256") is String:
		return {"ok": false, "error": _local_error("BUILD_SOURCE_HASH_INVALID", "Terminal SkillBuild artifact has no source_sha256.")}
	var calculated := _canonical_source_bundle_sha256(submitted_bundle)
	if calculated.is_empty():
		return {"ok": false, "error": _local_error("BUILD_SOURCE_HASH_UNVERIFIABLE", "Submitted source_bundle cannot be projected under the frozen aggregate hash algorithm.")}
	if str(artifact.source_sha256) != calculated:
		return {"ok": false, "error": _local_error("BUILD_SOURCE_HASH_MISMATCH", "SkillBuild source hash does not match submitted source.")}
	return {"ok": true}


func _canonical_source_bundle_sha256(bundle: Dictionary) -> String:
	if str(bundle.get("language", "")) != "CPP20" or not bundle.get("files") is Array:
		return ""
	var entrypoint := str(bundle.get("entrypoint", ""))
	var seen_paths := {}
	var entrypoint_matches := 0
	var projection: Array = []
	for value in bundle.files:
		if not value is Dictionary or value.size() != 3:
			return ""
		var path: Variant = value.get("path")
		var content: Variant = value.get("content")
		var content_sha256: Variant = value.get("content_sha256")
		if (
			typeof(path) != TYPE_STRING
			or path.is_empty()
			or seen_paths.has(path)
			or typeof(content) != TYPE_STRING
			or typeof(content_sha256) != TYPE_STRING
			or content_sha256.length() != 64
			or content.sha256_text() != content_sha256
		):
			return ""
		seen_paths[path] = true
		if path == entrypoint:
			entrypoint_matches += 1
		projection.push_back([path, content_sha256])
	if projection.is_empty() or entrypoint_matches != 1:
		return ""
	# Frozen Agent semantics: SHA-256 of compact UTF-8 JSON for
	# files.map(file => [file.path, file.content_sha256]); file order matters.
	return JSON.stringify(projection).sha256_text()


func _workspace_session_matches_authority(value: Dictionary) -> bool:
	for field in ["session_id", "world_id", "learner_id", "agent_profile_id", "channel", "content"]:
		if value.get(field) != authoritative_session.get(field):
			return false
	return true


func _bootstrap_world_authority() -> Dictionary:
	var store := _client_store()
	if store == null:
		return {}
	var world: Variant = store.authoritative_bootstrap.get("world")
	return world.duplicate(true) if world is Dictionary else {}


func _snapshot_matches_bootstrap(snapshot: Dictionary, authority: Dictionary) -> bool:
	return (
		str(snapshot.get("world_id", "")) == str(authority.get("world_id", ""))
		and int(snapshot.get("revision", -1)) == int(authority.get("revision", -2))
		and int(snapshot.get("last_event_sequence", -1)) == int(authority.get("last_event_sequence", -2))
		and str(snapshot.get("state_hash", "")) == str(authority.get("state_hash", ""))
	)


func _retryable_result(result: Dictionary) -> bool:
	if result.get("ok") == true:
		return false
	var status: Variant = result.get("status")
	if typeof(status) == TYPE_INT and int(status) in RETRYABLE_HTTP_STATUSES:
		return true
	var error: Variant = result.get("error")
	if not error is Dictionary:
		return false
	var retryable: Variant = error.get("retryable")
	if typeof(retryable) == TYPE_BOOL and retryable:
		return true
	return (
		typeof(status) == TYPE_INT
		and int(status) == 0
		and str(error.get("scope", "")) == "CLIENT_LOCAL"
		and str(error.get("code", "")) in RETRYABLE_LOCAL_TRANSPORT_CODES
	)


func _retry_delay(result: Dictionary, fallback: float) -> float:
	var headers: Variant = result.get("headers")
	if headers is Dictionary:
		var value := str(headers.get("retry-after", ""))
		if value.is_valid_int() and int(value) >= 0:
			return float(int(value))
	return fallback


func _wait_seconds(seconds: float) -> void:
	if seconds <= 0.0:
		await Engine.get_main_loop().process_frame
		return
	await Engine.get_main_loop().create_timer(seconds).timeout


func _new_turn_id() -> String:
	var stamp := RequestContextFactory.utc_now().replace("-", "").replace(":", "").replace("T", "").replace("Z", "").replace(".", "")
	return "turn_client_%s_%s" % [stamp, randi_range(100000, 999999)]


func _client_store() -> WalnutClientStore:
	if not is_inside_tree() or get_tree() == null or get_tree().root == null:
		return null
	return get_tree().root.get_node_or_null("ClientStore") as WalnutClientStore


func _student_action_readiness(action: String) -> Dictionary:
	# AppRoot enables this guard before it starts Bootstrap recovery.  Keeping
	# the guard opt-in preserves the controller's isolated test/demo composition,
	# which has no startup authority lifecycle of its own.
	if not _startup_authority_guard_enabled:
		return {"ok": true}
	var store := _client_store()
	if store == null:
		return _local_failure(
			"STUDENT_ACTION_AUTHORITY_UNAVAILABLE",
			"%s requires ClientStore authority." % action,
		)
	var ready_states := [
		WalnutClientStore.FlowState.READY,
		WalnutClientStore.FlowState.BUILD_FAILED,
		WalnutClientStore.FlowState.CERTIFIED,
		WalnutClientStore.FlowState.ACTIVE,
		WalnutClientStore.FlowState.COMPLETED,
	]
	if (
		_startup_authority_revalidation_pending
		or store.flow_state not in ready_states
		or authority_context.is_empty()
		or authoritative_session.is_empty()
		or str(authoritative_session.get("session_id", "")).is_empty()
	):
		var message := "%s is disabled until AppRoot completes exact Bootstrap and Session recovery." % action
		capability_unavailable.emit(action, message)
		return _local_failure("STUDENT_ACTION_AUTHORITY_NOT_READY", message)
	return {"ok": true}


func _local_failure(code: String, message: String, retryable: bool = false) -> Dictionary:
	return {
		"ok": false,
		"status": 0,
		"headers": {},
		"error": {
			"scope": "CLIENT_LOCAL",
			"code": code,
			"message": message,
			"retryable": retryable,
		},
	}


func _local_error(code: String, message: String) -> Dictionary:
	return {"scope": "CLIENT_LOCAL", "code": code, "message": message}


func _closed_dictionary(value: Variant, fields: Array) -> bool:
	if not value is Dictionary or value.size() != fields.size():
		return false
	for field in fields:
		if not value.has(field):
			return false
	return true
