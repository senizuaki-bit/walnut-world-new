class_name WalnutClientStore
extends Node

## Single owner for client-visible state. Wire data enters only after a Gateway
## validates it; UI nodes consume this state and never retain arbitrary API JSON.

const ContractValidator = preload("res://addons/yaya_contract_client/contract_validator.gd")
const RequestContextFactoryScript = preload("res://autoload/request_context_factory.gd")

signal workspace_changed(workspace: Dictionary)
signal content_changed(content: Dictionary)
signal draft_changed(source: String, draft_state: DraftState)
signal flow_changed(flow_state: FlowState)
signal world_replaced(snapshot: Dictionary)
signal objective_result_changed(result: Dictionary)
signal error_reported(error: Dictionary)
signal authority_changed(bootstrap: Dictionary, session: Dictionary, active_skill: Dictionary)

enum DraftState { CLEAN, DIRTY, SAVING, CONFLICT, SAVE_FAILED }
enum FlowState { BOOTSTRAPPING, READY, BUILDING, BUILD_FAILED, CERTIFIED, ACTIVATING, ACTIVE, TURN_RUNNING, PLAYING, COMPLETED, ERROR }

const PERSISTENCE_SCHEMA_VERSION := "1.2.0"
const LEGACY_PERSISTENCE_SCHEMA_VERSION := "1.1.0"
const DEFAULT_PERSISTENCE_PATH := "user://int1_client_authority.json"

var workspace: Dictionary = {}
var content: Dictionary = {}
var draft: Dictionary = {}
var local_source := ""
var draft_state: DraftState = DraftState.CLEAN
var flow_state: FlowState = FlowState.BOOTSTRAPPING
var world_snapshot: Dictionary = {}
var last_applied_sequence := 0
var applied_event_ids: Dictionary = {}
var objective_result: Dictionary = {}
var last_error: Dictionary = {}

## Public INT1 authorities. These are persisted as one closed record so restart
## recovery never reconstructs a Session or Skill tuple from UI state.
var authoritative_bootstrap: Dictionary = {}
var authoritative_session: Dictionary = {}
var activation_authority: Dictionary = {}
var active_skill_tuple: Dictionary = {}
## Durable local safety authority created only after an ACCEPT receipt and its
## next canonical Draft have both validated. Bootstrap may continue to expose
## the previously active tuple until the student explicitly Builds+Activates the
## new Draft; this marker prevents that stale tuple from becoming runnable.
var patch_activation_invalidation: Dictionary = {}
## Restart-only proof seed for an unconsumed objective failure. It contains the
## exact already-validated SkillBuild plus hashes/identities for the failed Run
## and canonical Interaction; startup re-GETs every public resource before the
## request button is restored.
var patch_failure_recovery_authority: Dictionary = {}
var authority_binding: Dictionary = {}
var pending_operations: Dictionary = {}
var last_interaction_sequence := 0

var persistence_path := DEFAULT_PERSISTENCE_PATH
var persistence_enabled := true
var _persistence_ready := false
var _persistence_integrity_error: Dictionary = {}
var _authority_revalidation_active := false
var _quarantined_world_snapshot: Dictionary = {}
var _quarantined_last_applied_sequence := 0
var _quarantined_authority_binding: Dictionary = {}
var _quarantined_bootstrap: Dictionary = {}
var _quarantined_session: Dictionary = {}


func _ready() -> void:
	if (
		authority_binding.is_empty()
		and authoritative_bootstrap.is_empty()
		and authoritative_session.is_empty()
		and world_snapshot.is_empty()
		and pending_operations.is_empty()
	):
		_load_persisted_state()
	_persistence_ready = true


func configure_persistence(path: String, enabled: bool = true, load_existing: bool = true) -> bool:
	if path.is_empty() or not path.begins_with("user://"):
		return false
	persistence_path = path
	persistence_enabled = enabled
	_persistence_integrity_error.clear()
	var loaded := true
	if load_existing:
		_clear_authority_payload()
		authority_binding.clear()
		loaded = _load_persisted_state()
	_persistence_ready = true
	return loaded


## A malformed authority file is not equivalent to an absent cache. AppRoot
## checks this result before its first network request so corrupted retry
## identities can never be silently replaced by newly derived operations.
func persistence_integrity_result() -> Dictionary:
	if _persistence_integrity_error.is_empty():
		return {"ok": true}
	return {
		"ok": false,
		"error": _persistence_integrity_error.duplicate(true),
	}


## AppRoot calls this from _enter_tree(), before TaskWorkspace children become
## ready. Persisted World state stays quarantined and therefore cannot be
## projected until a newly fetched Bootstrap proves the same origin/identity.
func begin_authority_revalidation() -> void:
	if _authority_revalidation_active:
		return
	_authority_revalidation_active = true
	_quarantined_world_snapshot = world_snapshot.duplicate(true)
	_quarantined_last_applied_sequence = last_applied_sequence
	_quarantined_authority_binding = authority_binding.duplicate(true)
	_quarantined_bootstrap = authoritative_bootstrap.duplicate(true)
	_quarantined_session = authoritative_session.duplicate(true)
	world_snapshot.clear()
	last_applied_sequence = 0
	applied_event_ids.clear()


## The persistence namespace is the normalized API base URL plus the exact
## Bootstrap actor/content authority. A change invalidates every previously
## persisted client authority instead of allowing cross-origin projection.
func bind_authority(api_base_url: String, bootstrap: Dictionary) -> Dictionary:
	var next_binding := make_authority_binding(api_base_url, bootstrap)
	if next_binding.is_empty():
		return _pending_operation_failure(
			"CLIENT_AUTHORITY_BINDING_INVALID",
			"Authority binding requires a normalized API base URL and exact actor/content dictionaries.",
		)
	var had_previous_authority := (
		not authority_binding.is_empty()
		or not authoritative_bootstrap.is_empty()
	)
	var binding_changed := had_previous_authority and authority_binding != next_binding
	authority_binding = next_binding
	if binding_changed:
		_clear_authority_payload()
	return {"ok": true, "changed": binding_changed, "binding": authority_binding.duplicate(true)}


## Restore a quarantined World only after AppRoot has validated the exact
## Bootstrap world and canonical Session. Origin/actor/content alone are not
## sufficient authority for projecting persisted world state.
func complete_authority_revalidation(bootstrap: Dictionary, session: Dictionary) -> Dictionary:
	if not _authority_revalidation_active:
		return {"ok": true, "restored": false}
	var restore := (
		not _quarantined_world_snapshot.is_empty()
		and authority_binding == _quarantined_authority_binding
		and bootstrap == authoritative_bootstrap
		and session == authoritative_session
		and _bootstrap_world_and_session_match_quarantine(bootstrap, session)
	)
	if restore:
		world_snapshot = _quarantined_world_snapshot.duplicate(true)
		last_applied_sequence = _quarantined_last_applied_sequence
		world_replaced.emit(world_snapshot.duplicate(true))
	_clear_authority_revalidation_quarantine()
	return {"ok": true, "restored": restore}


func _bootstrap_world_and_session_match_quarantine(bootstrap: Dictionary, session: Dictionary) -> bool:
	var world: Variant = bootstrap.get("world")
	var old_world: Variant = _quarantined_bootstrap.get("world")
	var session_authority: Variant = bootstrap.get("session")
	if not world is Dictionary or not old_world is Dictionary or not session_authority is Dictionary:
		return false
	var current_session_id: Variant = session_authority.get("current_session_id")
	return (
		world == old_world
		and str(session.get("session_id", "")) == str(_quarantined_session.get("session_id", ""))
		and current_session_id != null
		and str(current_session_id) == str(session.get("session_id", ""))
		and str(world.get("world_id", "")) == str(session.get("world_id", ""))
		and str(_quarantined_world_snapshot.get("world_id", "")) == str(world.get("world_id", ""))
		and int(_quarantined_world_snapshot.get("revision", -1)) == int(world.get("revision", -2))
		and int(_quarantined_world_snapshot.get("last_event_sequence", -1)) == int(world.get("last_event_sequence", -2))
		and str(_quarantined_world_snapshot.get("state_hash", "")) == str(world.get("state_hash", ""))
	)


func _clear_authority_revalidation_quarantine() -> void:
	_quarantined_world_snapshot.clear()
	_quarantined_last_applied_sequence = 0
	_quarantined_authority_binding.clear()
	_quarantined_bootstrap.clear()
	_quarantined_session.clear()
	_authority_revalidation_active = false


static func make_authority_binding(api_base_url: String, bootstrap: Dictionary) -> Dictionary:
	var normalized_base_url := normalize_api_base_url(api_base_url)
	var actor: Variant = bootstrap.get("actor")
	var content_ref: Variant = bootstrap.get("content")
	if (
		(normalized_base_url.is_empty() or not (normalized_base_url.begins_with("https://") or normalized_base_url.begins_with("http://")))
		or not actor is Dictionary
		or actor.is_empty()
		or not content_ref is Dictionary
		or content_ref.is_empty()
	):
		return {}
	return {
		"api_base_url": normalized_base_url,
		"actor": actor.duplicate(true),
		"content": content_ref.duplicate(true),
	}


static func normalize_api_base_url(value: String) -> String:
	var normalized := value.strip_edges()
	while normalized.ends_with("/"):
		normalized = normalized.left(-1)
	var scheme_end := normalized.find("://")
	if scheme_end <= 0:
		return ""
	var scheme := normalized.left(scheme_end).to_lower()
	var remainder := normalized.substr(scheme_end + 3)
	var path_start := remainder.find("/")
	var authority := (remainder if path_start < 0 else remainder.left(path_start)).to_lower()
	var path := "" if path_start < 0 else remainder.substr(path_start)
	if authority.is_empty():
		return ""
	if scheme == "https" and authority.ends_with(":443"):
		authority = authority.left(-4)
	elif scheme == "http" and authority.ends_with(":80"):
		authority = authority.left(-3)
	return "%s://%s%s" % [scheme, authority, path]


func set_workspace(value: Dictionary) -> void:
	workspace = value.duplicate(true)
	last_interaction_sequence = max(
		last_interaction_sequence,
		int(workspace.get("last_interaction_sequence", 0)),
	)
	_persist_state()
	workspace_changed.emit(workspace.duplicate(true))


func set_content(value: Dictionary) -> void:
	content = value.duplicate(true)
	content_changed.emit(content.duplicate(true))


func set_authoritative_bootstrap(value: Dictionary) -> void:
	var previous_actor: Variant = authoritative_bootstrap.get("actor")
	var previous_content: Variant = authoritative_bootstrap.get("content")
	if (
		not authoritative_bootstrap.is_empty()
		and (previous_actor != value.get("actor") or previous_content != value.get("content"))
	):
		_clear_authority_payload()
	authoritative_bootstrap = value.duplicate(true)
	var activation: Variant = authoritative_bootstrap.get("activation")
	activation_authority = activation.duplicate(true) if activation is Dictionary else {}
	var active: Variant = activation_authority.get("active")
	active_skill_tuple = active.duplicate(true) if active is Dictionary else {}
	if not patch_activation_invalidation.is_empty():
		active_skill_tuple.clear()
	var session_authority: Variant = authoritative_bootstrap.get("session")
	var bootstrap_session_id: Variant = session_authority.get("current_session_id") if session_authority is Dictionary else null
	if (
		authoritative_session.is_empty()
		and typeof(bootstrap_session_id) == TYPE_STRING
		and not str(bootstrap_session_id).is_empty()
	):
		authoritative_session = {"session_id": str(bootstrap_session_id)}
	_persist_state()
	_emit_authority_changed()


func set_authoritative_session(value: Dictionary) -> void:
	var previous_session_id := str(authoritative_session.get("session_id", ""))
	var next_session_id := str(value.get("session_id", ""))
	if (
		not previous_session_id.is_empty()
		and not next_session_id.is_empty()
		and previous_session_id != next_session_id
	):
		# Interaction cursors and retry envelopes are scoped to one durable
		# Session. Reusing either after Bootstrap creates a replacement Session
		# can skip that Session's first projection or replay an old operation.
		pending_operations.clear()
		last_interaction_sequence = 0
		patch_activation_invalidation.clear()
		patch_failure_recovery_authority.clear()
		_clear_world_authority()
	authoritative_session = value.duplicate(true)
	_persist_state()
	_emit_authority_changed()


func update_activation_authority(
	scope: Dictionary,
	registry_revision: int,
	active: Variant,
	built_draft_authority: Dictionary = {},
) -> bool:
	if (
		not patch_activation_invalidation.is_empty()
		and not _activation_closes_patch_invalidation(active, built_draft_authority)
	):
		return false
	var previous_activation := activation_authority.duplicate(true)
	var previous_active := active_skill_tuple.duplicate(true)
	var previous_bootstrap := authoritative_bootstrap.duplicate(true)
	var previous_invalidation := patch_activation_invalidation.duplicate(true)
	activation_authority = {
		"scope": scope.duplicate(true),
		"registry_revision": registry_revision,
		"active": active.duplicate(true) if active is Dictionary else null,
	}
	active_skill_tuple = active.duplicate(true) if active is Dictionary else {}
	if not authoritative_bootstrap.is_empty():
		authoritative_bootstrap["activation"] = activation_authority.duplicate(true)
	if not patch_activation_invalidation.is_empty():
		patch_activation_invalidation.clear()
	if not _persist_state(not previous_invalidation.is_empty()):
		activation_authority = previous_activation
		active_skill_tuple = previous_active
		authoritative_bootstrap = previous_bootstrap
		patch_activation_invalidation = previous_invalidation
		return false
	_emit_authority_changed()
	return true


## Atomically project the accepted canonical Draft and persist the authority
## that invalidates any pre-Patch Activation. Draft bytes remain server-owned
## and are recovered by GET on restart; the closed identity below is sufficient
## to prevent Bootstrap from reviving the old runnable tuple in the meantime.
func commit_accepted_patch_draft(
	canonical_draft: Dictionary,
	request: Dictionary,
	receipt: Dictionary,
) -> bool:
	var marker := _make_patch_activation_invalidation(canonical_draft, request, receipt)
	if marker.is_empty():
		return false
	var previous_draft := draft.duplicate(true)
	var previous_source := local_source
	var previous_state := draft_state
	var previous_active := active_skill_tuple.duplicate(true)
	var previous_invalidation := patch_activation_invalidation.duplicate(true)
	draft = canonical_draft.duplicate(true)
	local_source = _entrypoint_source(draft, "")
	draft_state = DraftState.CLEAN
	patch_activation_invalidation = marker
	active_skill_tuple.clear()
	if local_source.is_empty() or not _persist_state(true):
		draft = previous_draft
		local_source = previous_source
		draft_state = previous_state
		active_skill_tuple = previous_active
		patch_activation_invalidation = previous_invalidation
		return false
	draft_changed.emit(local_source, draft_state)
	_emit_authority_changed()
	return true


func record_patch_failure_recovery_authority(
	build: Dictionary,
	run: Dictionary,
	interaction: Dictionary,
	evidence: Array,
	result: Dictionary,
) -> bool:
	var marker := _make_patch_failure_recovery_authority(build, run, interaction, evidence, result)
	if marker.is_empty():
		return false
	var previous := patch_failure_recovery_authority.duplicate(true)
	patch_failure_recovery_authority = marker
	if not _persist_state(true):
		patch_failure_recovery_authority = previous
		return false
	return true


func clear_patch_failure_recovery_authority() -> bool:
	if patch_failure_recovery_authority.is_empty():
		return true
	var previous := patch_failure_recovery_authority.duplicate(true)
	patch_failure_recovery_authority.clear()
	if not _persist_state(true):
		patch_failure_recovery_authority = previous
		return false
	return true


## Persist a write-ahead operation envelope before its network side effect is
## allowed to start.  Callers must inspect `ok`; returning the envelope alone
## would make a storage failure indistinguishable from a durable write.
func ensure_pending_operation(slot: String, identity: String, envelope: Dictionary) -> Dictionary:
	if slot.is_empty() or identity.is_empty() or envelope.is_empty():
		return _pending_operation_failure(
			"PENDING_OPERATION_INVALID",
			"A pending operation requires a non-empty slot, identity, and envelope.",
		)
	var existing: Variant = pending_operations.get(slot)
	if pending_operations.has(slot) and (
		not existing is Dictionary or not existing.get("envelope") is Dictionary
	):
		return _pending_operation_failure(
			"PENDING_OPERATION_SLOT_CORRUPT",
			"The pending operation slot contains invalid authority and cannot be overwritten.",
		)
	if existing is Dictionary and existing.get("envelope") is Dictionary:
		var existing_integrity := _pending_operation_integrity(
			slot,
			existing,
			authoritative_bootstrap,
			authoritative_session,
			active_skill_tuple,
			world_snapshot,
			last_interaction_sequence,
		)
		if not existing_integrity.ok:
			return _pending_operation_failure(
				"PENDING_OPERATION_SLOT_CORRUPT",
				str(existing_integrity.message),
			)
		if str(existing.get("identity", "")) != identity:
			return _pending_operation_failure(
				"PENDING_OPERATION_IDENTITY_CONFLICT",
				"The pending operation slot already belongs to another durable identity and must be reconciled first.",
			)
		return {
			"ok": true,
			"value": existing.envelope.duplicate(true),
			"identity": identity,
		}
	var proposed := {
		"identity": identity,
		"envelope": envelope.duplicate(true),
	}
	var proposed_integrity := _pending_operation_integrity(
		slot,
		proposed,
		authoritative_bootstrap,
		authoritative_session,
		active_skill_tuple,
		world_snapshot,
		last_interaction_sequence,
	)
	if not proposed_integrity.ok:
		return _pending_operation_failure(
			"PENDING_OPERATION_SEMANTIC_INVALID",
			str(proposed_integrity.message),
		)
	pending_operations[slot] = proposed
	if not _persist_state(true):
		# The network must never observe an envelope which exists only in RAM.
		pending_operations.erase(slot)
		return _pending_operation_failure(
			"PENDING_OPERATION_PERSISTENCE_FAILED",
			"The pending operation could not be atomically persisted before its network write.",
		)
	return {"ok": true, "value": envelope.duplicate(true), "identity": identity}


func get_pending_operation(slot: String) -> Dictionary:
	var existing: Variant = pending_operations.get(slot)
	if not existing is Dictionary or not existing.get("envelope") is Dictionary:
		return {}
	return existing.envelope.duplicate(true)


func validate_pending_operation(slot: String) -> Dictionary:
	var existing: Variant = pending_operations.get(slot)
	if existing == null and not pending_operations.has(slot):
		return {"ok": true, "value": {}}
	var integrity := _pending_operation_integrity(
		slot,
		existing,
		authoritative_bootstrap,
		authoritative_session,
		active_skill_tuple,
		world_snapshot,
		last_interaction_sequence,
	)
	if not integrity.ok:
		return _pending_operation_failure(
			"PENDING_OPERATION_SLOT_CORRUPT",
			str(integrity.message),
		)
	return {
		"ok": true,
		"value": existing.envelope.duplicate(true),
		"identity": str(existing.identity),
	}


func clear_pending_operation(slot: String) -> bool:
	var existing: Variant = pending_operations.get(slot)
	if not existing is Dictionary:
		return true
	pending_operations.erase(slot)
	if _persist_state(true):
		return true
	# Keeping the old envelope is safe: reconciliation reuses its exact bytes and
	# idempotency key. Losing it after a committed side effect would not be safe.
	pending_operations[slot] = existing
	return false


## Persist reconciliation identities after a Turn's terminal Command/Run are
## known.  Playback restart can then use GET-only closure instead of replaying
## a product mutation.  The original logical identity, request bytes and
## Idempotency-Key are immutable.
func set_pending_turn_recovery(slot: String, recovery: Dictionary) -> bool:
	if slot not in ["agent_turn", "agent_hint"] or recovery.is_empty():
		return false
	var existing: Variant = pending_operations.get(slot)
	if not existing is Dictionary or not existing.get("envelope") is Dictionary:
		return false
	var candidate: Dictionary = existing.duplicate(true)
	candidate.envelope["recovery"] = recovery.duplicate(true)
	var integrity := _pending_operation_integrity(
		slot,
		candidate,
		authoritative_bootstrap,
		authoritative_session,
		active_skill_tuple,
		world_snapshot,
		last_interaction_sequence,
	)
	if not integrity.ok:
		return false
	pending_operations[slot] = candidate
	if _persist_state(true):
		return true
	pending_operations[slot] = existing
	return false


func set_pending_patch_request_recovery(command_id: String, phase: String = "COMMAND_TERMINAL") -> bool:
	var slot := "agent_patch_request"
	if phase not in ["COMMAND_ACCEPTED", "COMMAND_TERMINAL"]:
		return false
	var existing: Variant = pending_operations.get(slot)
	if not existing is Dictionary or not existing.get("envelope") is Dictionary:
		return false
	var candidate: Dictionary = existing.duplicate(true)
	candidate.envelope["recovery"] = {
		"phase": phase, "command_id": command_id,
	}
	var integrity := _pending_operation_integrity(
		slot, candidate, authoritative_bootstrap, authoritative_session,
		active_skill_tuple, world_snapshot, last_interaction_sequence,
	)
	if not integrity.ok:
		return false
	pending_operations[slot] = candidate
	if _persist_state(true):
		return true
	pending_operations[slot] = existing
	return false


func set_interaction_cursor(sequence: int) -> void:
	if sequence < last_interaction_sequence:
		return
	last_interaction_sequence = sequence
	_persist_state()


func set_draft(value: Dictionary) -> void:
	draft = value.duplicate(true)
	local_source = _entrypoint_source(draft, local_source)
	draft_state = DraftState.CLEAN
	draft_changed.emit(local_source, draft_state)


## A save receipt advances Draft metadata, but a later local edit must remain
## dirty. The next save then uses the receipt's revision/hash as its CAS base.
func set_draft_preserving_local_source(value: Dictionary, source: String) -> void:
	draft = value.duplicate(true)
	local_source = source
	draft_state = DraftState.DIRTY
	draft_changed.emit(local_source, draft_state)


func mark_draft_dirty(source: String) -> void:
	local_source = source
	draft_state = DraftState.DIRTY
	draft_changed.emit(local_source, draft_state)


func mark_draft_saving() -> void:
	draft_state = DraftState.SAVING
	draft_changed.emit(local_source, draft_state)


func record_draft_conflict(server_draft: Dictionary) -> void:
	draft = server_draft.duplicate(true)
	draft_state = DraftState.CONFLICT
	draft_changed.emit(local_source, draft_state)


func record_draft_save_failed(error: Dictionary) -> void:
	draft_state = DraftState.SAVE_FAILED
	report_error(error)
	draft_changed.emit(local_source, draft_state)


func set_flow(value: FlowState) -> void:
	flow_state = value
	flow_changed.emit(flow_state)


func replace_world(snapshot: Dictionary) -> bool:
	if not _is_authoritative_snapshot(snapshot):
		report_error(_local_error("WORLD_SNAPSHOT_INVALID", "The authoritative world snapshot is incomplete."))
		return false
	world_snapshot = snapshot.duplicate(true)
	last_applied_sequence = int(world_snapshot.last_event_sequence)
	applied_event_ids.clear()
	_persist_state()
	world_replaced.emit(world_snapshot.duplicate(true))
	return true


func record_applied_event(event: Dictionary) -> bool:
	if not event.has("event_id") or not event.has("sequence"):
		report_error(_local_error("WORLD_EVENT_INVALID", "World event lacks event_id or sequence."))
		return false
	var event_id := str(event.event_id)
	if applied_event_ids.has(event_id):
		return false
	if int(event.sequence) != last_applied_sequence + 1:
		report_error(_local_error("WORLD_EVENT_GAP", "World event sequence is not contiguous."))
		return false
	applied_event_ids[event_id] = true
	last_applied_sequence = int(event.sequence)
	return true


func set_objective_result(value: Dictionary) -> void:
	objective_result = value.duplicate(true)
	objective_result_changed.emit(objective_result.duplicate(true))


func report_error(error: Dictionary) -> void:
	last_error = error.duplicate(true)
	flow_state = FlowState.ERROR
	flow_changed.emit(flow_state)
	error_reported.emit(last_error.duplicate(true))


func _is_authoritative_snapshot(snapshot: Dictionary) -> bool:
	for field in ["world_id", "revision", "last_event_sequence", "state_schema_version", "state_hash", "world_rules_version", "state"]:
		if not snapshot.has(field):
			return false
	return snapshot.state is Dictionary


func _entrypoint_source(value: Dictionary, fallback: String) -> String:
	if value.has("source") and value.source is String:
		return value.source
	var bundle: Variant = value.get("source_bundle")
	if not bundle is Dictionary or not bundle.get("files") is Array:
		return fallback
	var entrypoint := str(bundle.get("entrypoint", ""))
	for file in bundle.files:
		if file is Dictionary and str(file.get("path", "")) == entrypoint:
			return str(file.get("content", fallback))
	return fallback


func _emit_authority_changed() -> void:
	authority_changed.emit(
		authoritative_bootstrap.duplicate(true),
		authoritative_session.duplicate(true),
		active_skill_tuple.duplicate(true),
	)


func _clear_world_authority() -> void:
	world_snapshot.clear()
	last_applied_sequence = 0
	applied_event_ids.clear()
	_clear_authority_revalidation_quarantine()


func _clear_authority_payload() -> void:
	workspace.clear()
	content.clear()
	draft.clear()
	local_source = ""
	draft_state = DraftState.CLEAN
	objective_result.clear()
	last_error.clear()
	authoritative_bootstrap.clear()
	authoritative_session.clear()
	activation_authority.clear()
	active_skill_tuple.clear()
	patch_activation_invalidation.clear()
	patch_failure_recovery_authority.clear()
	pending_operations.clear()
	last_interaction_sequence = 0
	_clear_world_authority()


func _persist_state(require_durable_write := false) -> bool:
	if (
		not persistence_enabled
	):
		return true
	if not _persistence_ready or not is_inside_tree() or authoritative_bootstrap.is_empty():
		return not require_durable_write
	var payload := {
		"schema_version": PERSISTENCE_SCHEMA_VERSION,
		"authority_binding": authority_binding.duplicate(true),
		"bootstrap": authoritative_bootstrap.duplicate(true),
		"session": authoritative_session.duplicate(true),
		"activation": activation_authority.duplicate(true),
		"active_skill_tuple": active_skill_tuple.duplicate(true),
		"patch_activation_invalidation": patch_activation_invalidation.duplicate(true),
		"patch_failure_recovery_authority": patch_failure_recovery_authority.duplicate(true),
		"pending_operations": pending_operations.duplicate(true),
		"last_interaction_sequence": last_interaction_sequence,
		"world_snapshot": world_snapshot.duplicate(true),
	}
	if not _valid_persisted_state(payload):
		_set_persistence_integrity_error(
			"CLIENT_PERSISTENCE_STATE_INVALID",
			"The in-memory client authority cannot be serialized as a valid closed record.",
		)
		return false

	# Windows does not provide portable overwrite-by-rename semantics through
	# Godot. Keep the protocol bounded to three exact siblings and retain one
	# previously validated authority at every destructive boundary:
	#
	#   write+flush tmp -> target to bak -> tmp to target -> remove bak
	#
	# If installing tmp fails, bak is moved back to target. A failed restore
	# still leaves the validated bak in place for startup recovery.
	var temporary_path := "%s.tmp" % persistence_path
	var backup_path := "%s.bak" % persistence_path
	var target_candidate := _read_persistence_candidate(persistence_path)
	var backup_candidate := _read_persistence_candidate(backup_path)
	if target_candidate.exists and not target_candidate.valid:
		_set_persistence_integrity_error(
			"CLIENT_PERSISTENCE_CORRUPT",
			"The current client authority is corrupt and cannot be overwritten.",
		)
		return false
	if backup_candidate.exists and not backup_candidate.valid:
		_set_persistence_integrity_error(
			"CLIENT_PERSISTENCE_BACKUP_CORRUPT",
			"The recovery backup is corrupt and cannot be treated as absent.",
		)
		return false

	var file := FileAccess.open(temporary_path, FileAccess.WRITE)
	if file == null:
		return false
	file.store_string(JSON.stringify(payload))
	file.flush()
	var write_error := file.get_error()
	file.close()
	var absolute_temporary := ProjectSettings.globalize_path(temporary_path)
	if write_error != OK:
		_persistence_remove_absolute(absolute_temporary)
		return false
	var temporary_candidate := _read_persistence_candidate(temporary_path)
	if (
		not temporary_candidate.exists
		or not temporary_candidate.valid
		or temporary_candidate.value != payload
	):
		_persistence_remove_absolute(absolute_temporary)
		return false

	var absolute_target := ProjectSettings.globalize_path(persistence_path)
	var absolute_backup := ProjectSettings.globalize_path(backup_path)
	if target_candidate.exists:
		if backup_candidate.exists and _persistence_remove_absolute(absolute_backup) != OK:
			_persistence_remove_absolute(absolute_temporary)
			return false
		if _persistence_rename_absolute(absolute_target, absolute_backup) != OK:
			_persistence_remove_absolute(absolute_temporary)
			return false
	elif backup_candidate.exists:
		# A previous install lost its acknowledgement after moving target to bak.
		# Keep that valid backup until the new target has been installed.
		pass

	if _persistence_rename_absolute(absolute_temporary, absolute_target) != OK:
		_restore_persistence_backup(absolute_backup, absolute_target)
		return false
	var installed_candidate := _read_persistence_candidate(persistence_path)
	if (
		not installed_candidate.exists
		or not installed_candidate.valid
		or installed_candidate.value != payload
	):
		# Never allow an invalid target to hide a known-good backup.
		_persistence_remove_absolute(absolute_target)
		_restore_persistence_backup(absolute_backup, absolute_target)
		return false
	if _persistence_path_exists(backup_path):
		# Once target is independently validated, backup cleanup is best-effort.
		# Leaving a valid backup behind is safe and is handled on the next load.
		_persistence_remove_absolute(absolute_backup)
	_persistence_integrity_error.clear()
	return true


func _load_persisted_state() -> bool:
	if not persistence_enabled:
		_persistence_integrity_error.clear()
		return true
	var temporary_path := "%s.tmp" % persistence_path
	var backup_path := "%s.bak" % persistence_path
	var target_candidate := _read_persistence_candidate(persistence_path)
	var backup_candidate := _read_persistence_candidate(backup_path)
	if target_candidate.exists and target_candidate.valid and backup_candidate.exists and not backup_candidate.valid:
		_set_persistence_integrity_error(
			"CLIENT_PERSISTENCE_BACKUP_CORRUPT",
			"A corrupt recovery backup exists beside the valid target and cannot be treated as absent.",
		)
		return false

	var selected: Variant = null
	var recovered_from_backup := false
	if target_candidate.exists and target_candidate.valid:
		selected = target_candidate.value
	elif backup_candidate.exists and backup_candidate.valid:
		selected = backup_candidate.value
		recovered_from_backup = true
		var absolute_target := ProjectSettings.globalize_path(persistence_path)
		var absolute_backup := ProjectSettings.globalize_path(backup_path)
		# A corrupt target is explicitly detected, never interpreted as absence.
		# Remove it only while a separately validated backup still exists, then
		# restore the exact backup. If repair cannot finish, the backup remains the
		# sole validated authority and is still safe to load in this process.
		if target_candidate.exists:
			_persistence_remove_absolute(absolute_target)
		_restore_persistence_backup(absolute_backup, absolute_target)
	elif target_candidate.exists:
		_set_persistence_integrity_error(
			"CLIENT_PERSISTENCE_CORRUPT",
			"The persisted target is corrupt and no valid recovery backup exists.",
		)
		return false
	elif backup_candidate.exists:
		_set_persistence_integrity_error(
			"CLIENT_PERSISTENCE_BACKUP_CORRUPT",
			"The persisted recovery backup is corrupt and cannot be treated as absent.",
		)
		return false
	elif _persistence_path_exists(temporary_path):
		_set_persistence_integrity_error(
			"CLIENT_PERSISTENCE_INCOMPLETE",
			"A staging authority exists without a committed target or recovery backup.",
		)
		return false
	else:
		_persistence_integrity_error.clear()
		return true

	# A valid committed target/backup always wins over an unacknowledged tmp.
	# Cleanup is deliberately limited to the exact protocol siblings.
	if _persistence_path_exists(temporary_path):
		_persistence_remove_absolute(ProjectSettings.globalize_path(temporary_path))
	if not recovered_from_backup and target_candidate.exists and backup_candidate.exists:
		_persistence_remove_absolute(ProjectSettings.globalize_path(backup_path))
	var parsed: Dictionary = selected
	_persistence_integrity_error.clear()
	authority_binding = parsed.authority_binding.duplicate(true)
	authoritative_bootstrap = parsed.bootstrap.duplicate(true)
	authoritative_session = parsed.session.duplicate(true)
	activation_authority = parsed.activation.duplicate(true)
	active_skill_tuple = parsed.active_skill_tuple.duplicate(true)
	patch_activation_invalidation = parsed.patch_activation_invalidation.duplicate(true)
	patch_failure_recovery_authority = parsed.patch_failure_recovery_authority.duplicate(true)
	pending_operations = parsed.pending_operations.duplicate(true)
	last_interaction_sequence = int(parsed.last_interaction_sequence)
	world_snapshot = parsed.world_snapshot.duplicate(true)
	if not world_snapshot.is_empty():
		last_applied_sequence = int(world_snapshot.get("last_event_sequence", 0))
	return true


func _read_persistence_candidate(path: String) -> Dictionary:
	if not _persistence_path_exists(path):
		return {"exists": false, "valid": false, "value": null}
	var absolute_path := ProjectSettings.globalize_path(path)
	if DirAccess.dir_exists_absolute(absolute_path):
		return {"exists": true, "valid": false, "value": null}
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		return {"exists": true, "valid": false, "value": null}
	var contents := file.get_as_text()
	var read_error := file.get_error()
	file.close()
	var parser := JSON.new()
	var parse_error := parser.parse(contents)
	var parsed: Variant = (
		_normalize_json_numbers(parser.data)
		if parse_error == OK
		else null
	)
	if parsed is Dictionary:
		parsed = _migrate_persisted_state(parsed)
	return {
		"exists": true,
		"valid": read_error == OK and parse_error == OK and _valid_persisted_state(parsed),
		"value": parsed if read_error == OK and parse_error == OK and parsed is Dictionary else null,
	}


func _persistence_path_exists(path: String) -> bool:
	var absolute_path := ProjectSettings.globalize_path(path)
	return FileAccess.file_exists(path) or DirAccess.dir_exists_absolute(absolute_path)


func _restore_persistence_backup(absolute_backup: String, absolute_target: String) -> bool:
	if not FileAccess.file_exists(absolute_backup):
		return false
	if FileAccess.file_exists(absolute_target) or DirAccess.dir_exists_absolute(absolute_target):
		return false
	return _persistence_rename_absolute(absolute_backup, absolute_target) == OK


## Narrow filesystem seams keep failure-boundary tests deterministic without
## replacing Godot's persistence implementation in production.
func _persistence_rename_absolute(source: String, destination: String) -> Error:
	return DirAccess.rename_absolute(source, destination)


func _persistence_remove_absolute(path: String) -> Error:
	return DirAccess.remove_absolute(path)


func _migrate_persisted_state(value: Dictionary) -> Dictionary:
	if str(value.get("schema_version", "")) == PERSISTENCE_SCHEMA_VERSION:
		var pre_failure_authority_fields := [
			"schema_version", "authority_binding", "bootstrap", "session", "activation",
			"active_skill_tuple", "patch_activation_invalidation", "pending_operations",
			"last_interaction_sequence", "world_snapshot",
		]
		if _closed_dictionary(value, pre_failure_authority_fields):
			var current_migrated := value.duplicate(true)
			current_migrated["patch_failure_recovery_authority"] = {}
			return current_migrated
		return value
	var legacy_fields := [
		"schema_version", "authority_binding", "bootstrap", "session", "activation",
		"active_skill_tuple", "pending_operations", "last_interaction_sequence", "world_snapshot",
	]
	if str(value.get("schema_version", "")) != LEGACY_PERSISTENCE_SCHEMA_VERSION or not _closed_dictionary(value, legacy_fields):
		return value
	var migrated := value.duplicate(true)
	migrated.schema_version = PERSISTENCE_SCHEMA_VERSION
	migrated["patch_activation_invalidation"] = {}
	migrated["patch_failure_recovery_authority"] = {}
	return migrated


func _valid_persisted_state(value: Variant) -> bool:
	if not value is Dictionary:
		return false
	var required := [
		"schema_version", "authority_binding", "bootstrap", "session", "activation", "active_skill_tuple",
		"patch_activation_invalidation", "pending_operations", "last_interaction_sequence", "world_snapshot",
		"patch_failure_recovery_authority",
	]
	if value.size() != required.size():
		return false
	for field in required:
		if not value.has(field):
			return false
	if value.schema_version != PERSISTENCE_SCHEMA_VERSION:
		return false
	for field in ["authority_binding", "bootstrap", "session", "activation", "active_skill_tuple", "patch_activation_invalidation", "patch_failure_recovery_authority", "pending_operations", "world_snapshot"]:
		if not value[field] is Dictionary:
			return false
	if typeof(value.last_interaction_sequence) != TYPE_INT or value.last_interaction_sequence < 0:
		return false
	if not value.bootstrap.is_empty():
		if not _valid_authority_binding(value.authority_binding, value.bootstrap):
			return false
		var persisted_activation: Variant = value.bootstrap.get("activation")
		if not persisted_activation is Dictionary or persisted_activation != value.activation:
			return false
		var active: Variant = persisted_activation.get("active")
		var expected_active: Dictionary = active if active is Dictionary else {}
		if not expected_active.is_empty() and not _valid_active_skill_tuple(expected_active):
			return false
		if not _valid_activation_binding(persisted_activation, expected_active):
			return false
		if value.patch_activation_invalidation.is_empty():
			if expected_active != value.active_skill_tuple:
				return false
		elif (
			not value.active_skill_tuple.is_empty()
			or not _valid_patch_activation_invalidation(value.patch_activation_invalidation, value.session)
		):
			return false
	if not _valid_session_authority_binding(value.session, value.bootstrap):
		return false
	if (
		not value.patch_failure_recovery_authority.is_empty()
		and not _valid_patch_failure_recovery_authority(
			value.patch_failure_recovery_authority,
			value.session,
			value.active_skill_tuple,
		)
	):
		return false
	if not value.world_snapshot.is_empty() and not _is_authoritative_snapshot(value.world_snapshot):
		return false
	var authority_world_id := _authority_world_id(value.bootstrap, value.session)
	if (
		not value.world_snapshot.is_empty()
		and not authority_world_id.is_empty()
		and str(value.world_snapshot.get("world_id", "")) != authority_world_id
	):
		return false
	for slot in value.pending_operations:
		var pending: Variant = value.pending_operations[slot]
		var integrity := _pending_operation_integrity(
			slot,
			pending,
			value.bootstrap,
			value.session,
			value.active_skill_tuple,
			value.world_snapshot,
			int(value.last_interaction_sequence),
		)
		if not integrity.ok:
			return false
	return true


func _valid_authority_binding(binding: Dictionary, bootstrap: Dictionary) -> bool:
	if (
		binding.size() != 3
		or typeof(binding.get("api_base_url")) != TYPE_STRING
		or not binding.get("actor") is Dictionary
		or not binding.get("content") is Dictionary
	):
		return false
	var normalized_base_url := normalize_api_base_url(str(binding.api_base_url))
	return (
		not normalized_base_url.is_empty()
		and normalized_base_url == str(binding.api_base_url)
		and (normalized_base_url.begins_with("https://") or normalized_base_url.begins_with("http://"))
		and binding.actor == bootstrap.get("actor")
		and binding.content == bootstrap.get("content")
	)


func _pending_operation_integrity(
	slot_value: Variant,
	pending: Variant,
	bootstrap: Dictionary,
	session: Dictionary,
	active_skill: Dictionary,
	persisted_world: Dictionary,
	interaction_cursor: int,
) -> Dictionary:
	if (
		typeof(slot_value) != TYPE_STRING
		or str(slot_value).is_empty()
		or not pending is Dictionary
		or pending.size() != 2
		or typeof(pending.get("identity")) != TYPE_STRING
		or str(pending.get("identity", "")).is_empty()
		or not pending.get("envelope") is Dictionary
	):
		return _integrity_failure("Pending operation record shape is invalid.")
	var slot := str(slot_value)
	var identity := str(pending.identity)
	var envelope: Dictionary = pending.envelope
	if slot == "agent_session_create":
		return _session_create_envelope_integrity(identity, envelope, bootstrap)
	if slot == "draft_save":
		return _draft_save_envelope_integrity(identity, envelope, bootstrap, session)
	if slot in ["agent_turn", "agent_hint"]:
		return _turn_envelope_integrity(
			slot,
			identity,
			envelope,
			bootstrap,
			session,
			active_skill,
			persisted_world,
			interaction_cursor,
		)
	if slot == "agent_patch_request":
		return _patch_request_envelope_integrity(
			identity, envelope, bootstrap, session, active_skill,
			persisted_world, interaction_cursor,
		)
	if slot.begins_with("patch_decision:"):
		return _patch_decision_envelope_integrity(slot, identity, envelope, session)
	return _integrity_failure("Pending operation slot is not recognized.")


func _patch_request_envelope_integrity(
	identity: String,
	envelope: Dictionary,
	bootstrap: Dictionary,
	session: Dictionary,
	active_skill: Dictionary,
	persisted_world: Dictionary,
	interaction_cursor: int,
) -> Dictionary:
	var required := [
		"session_id", "turn_id", "idempotency_key", "request", "pre_world",
		"interaction_cursor_before", "selection_interaction_id",
		"presentation_cursor_before", "selected_failure_authority",
	]
	var allowed := required + ["recovery"]
	if not _dictionary_has_exact_allowed_fields(envelope, required, allowed):
		return _integrity_failure("Pending Skill Patch request envelope is not closed.")
	var request: Variant = envelope.get("request")
	var input: Variant = request.get("input") if request is Dictionary else null
	var pre_world: Variant = envelope.get("pre_world")
	var selection_id := str(envelope.get("selection_interaction_id", ""))
	var selected_failure_authority: Variant = envelope.get("selected_failure_authority")
	if (
		not request is Dictionary
		or not ContractValidator.validate_agent_turn_create_request(request).ok
		or not input is Dictionary
		or not _closed_dictionary(input, ["type", "action_id", "selection_id"])
		or str(input.type) != "UI_ACTION"
		or str(input.action_id) != "request_ai_patch"
		or str(input.selection_id) != selection_id
		or not _valid_local_identifier(selection_id)
		or not selected_failure_authority is Dictionary
		or not _valid_patch_failure_authority(
			selected_failure_authority, selection_id, str(envelope.get("session_id", "")),
			active_skill,
		)
		or not pre_world is Dictionary
		or not _is_authoritative_snapshot(pre_world)
		or not persisted_world.is_empty() and persisted_world != pre_world
		or typeof(envelope.get("interaction_cursor_before")) != TYPE_INT
		or int(envelope.interaction_cursor_before) < 1
		or int(envelope.interaction_cursor_before) > interaction_cursor
		or typeof(envelope.get("presentation_cursor_before")) != TYPE_INT
		or int(envelope.presentation_cursor_before) < 0
		or not request.get("skill_bindings") is Array
		or not _valid_active_skill_tuple(active_skill)
		or request.skill_bindings != [_binding_from_active(active_skill)]
	):
		return _integrity_failure("Pending Skill Patch request authority/input/world binding is invalid.")
	var session_id := str(envelope.get("session_id", ""))
	var turn_id := str(envelope.get("turn_id", ""))
	if (
		session_id != _authority_session_id(bootstrap, session)
		or str(request.turn_id) != turn_id
		or str(pre_world.world_id) != _authority_world_id(bootstrap, session)
		or int(request.expected_world_revision) != int(pre_world.revision)
		or int(request.client_state.last_event_sequence) != int(pre_world.last_event_sequence)
	):
		return _integrity_failure("Pending Skill Patch request Session/Turn/World cursor is invalid.")
	var expected_identity := ContractValidator.canonical_json_sha256_v1({
		"session_id": session_id,
		"world_revision": int(pre_world.revision),
		"last_event_sequence": int(pre_world.last_event_sequence),
		"client_turn_sequence": int(request.client_state.client_turn_sequence),
		"input": input,
		"skill_bindings": request.skill_bindings,
		"selected_failure_authority": selected_failure_authority,
	})
	var expected_key := RequestContextFactoryScript.idempotency_key_for(
		"createAgentTurn", "%s:%s" % [session_id, turn_id],
	)
	if identity != expected_identity or str(envelope.idempotency_key) != expected_key:
		return _integrity_failure("Pending Skill Patch request identity or Idempotency-Key is inconsistent.")
	if envelope.has("recovery") and (
		not _closed_dictionary(envelope.recovery, ["phase", "command_id"])
		or str(envelope.recovery.phase) not in ["COMMAND_ACCEPTED", "COMMAND_TERMINAL"]
		or not _valid_local_identifier(str(envelope.recovery.command_id))
	):
		return _integrity_failure("Pending Skill Patch request GET-only recovery identity is invalid.")
	return {"ok": true}


func _valid_patch_failure_authority(
	value: Dictionary,
	selection_id: String,
	session_id: String,
	active_skill: Dictionary,
) -> bool:
	var fields := [
		"interaction_id", "interaction_revision", "sequence", "session_id",
		"turn_id", "command_id", "run_id", "role", "response_type",
		"feedback_event_id", "feedback_sha256", "projection_source_sha256",
		"evidence_refs", "build_id", "build_resource_sha256",
		"skill_binding", "failure_identity_sha256",
	]
	if not _closed_dictionary(value, fields):
		return false
	for field in [
		"interaction_id", "turn_id", "command_id", "run_id",
		"feedback_event_id", "build_id",
	]:
		if not _valid_local_identifier(str(value.get(field, ""))):
			return false
	if (
		str(value.interaction_id) != selection_id
		or str(value.session_id) != session_id
		or typeof(value.interaction_revision) != TYPE_INT
		or int(value.interaction_revision) < 1
		or typeof(value.sequence) != TYPE_INT
		or int(value.sequence) < 1
		or str(value.role) not in ["teaching_agent", "bug_agent"]
		or str(value.response_type) not in ["question", "hint", "message"]
		or not _valid_sha256(value.feedback_sha256)
		or not _valid_sha256(value.projection_source_sha256)
		or not _valid_sha256(value.build_resource_sha256)
		or not _valid_sha256(value.failure_identity_sha256)
		or not value.evidence_refs is Array
		or value.evidence_refs.is_empty()
		or value.evidence_refs.size() > 64
		or not value.skill_binding is Dictionary
		or not _valid_active_skill_tuple(active_skill)
		or value.skill_binding != _binding_from_active(active_skill)
	):
		return false
	var evidence_ids := {}
	for reference in value.evidence_refs:
		if not reference is Dictionary or not _valid_local_identifier(str(reference.get("evidence_id", ""))):
			return false
		var evidence_id := str(reference.evidence_id)
		if evidence_ids.has(evidence_id):
			return false
		evidence_ids[evidence_id] = true
	var identity_payload := value.duplicate(true)
	identity_payload.erase("failure_identity_sha256")
	return (
		ContractValidator.canonical_json_sha256_v1(identity_payload)
		== str(value.failure_identity_sha256)
	)


func _session_create_envelope_integrity(
	identity: String,
	envelope: Dictionary,
	bootstrap: Dictionary,
) -> Dictionary:
	if not _closed_dictionary(envelope, ["idempotency_key", "request"]):
		return _integrity_failure("Pending Session create envelope is not closed.")
	var request: Variant = envelope.get("request")
	if not request is Dictionary or not ContractValidator.validate_agent_session_create_request(request).ok:
		return _integrity_failure("Pending Session create request violates its frozen contract.")
	var expected_identity := _session_create_request_identity(request)
	if identity != expected_identity or str(envelope.idempotency_key) != RequestContextFactoryScript.idempotency_key_for("createAgentSession", expected_identity):
		return _integrity_failure("Pending Session create identity or Idempotency-Key is inconsistent.")
	var bootstrap_session: Variant = bootstrap.get("session")
	if not bootstrap_session is Dictionary or bootstrap_session.get("create_request") != request:
		return _integrity_failure("Pending Session create request is not the current Bootstrap authority.")
	return {"ok": true}


func _draft_save_envelope_integrity(
	identity: String,
	envelope: Dictionary,
	bootstrap: Dictionary,
	session: Dictionary,
) -> Dictionary:
	if not _closed_dictionary(envelope, ["idempotency_key", "request"]):
		return _integrity_failure("Pending Draft save envelope is not closed.")
	var request_value: Variant = envelope.get("request")
	if not request_value is Dictionary:
		return _integrity_failure("Pending Draft save request is absent.")
	var request: Dictionary = request_value
	var required := [
		"session_id", "draft_id", "skill_id", "content_ref", "base_revision",
		"base_draft_sha256", "display_name", "source_bundle", "client_saved_at",
	]
	if (
		not _closed_dictionary(request, required)
		or not ContractValidator.validate_identifier(request.get("session_id")).ok
		or not ContractValidator.validate_identifier(request.get("draft_id")).ok
		or not ContractValidator.validate_identifier(request.get("skill_id")).ok
		or typeof(request.get("base_revision")) != TYPE_INT
		or int(request.base_revision) < 0
		or not _valid_source_bundle(request.get("source_bundle"))
		or not ContractValidator._validate_date_time(request.get("client_saved_at"), "PendingDraft.client_saved_at").ok
	):
		return _integrity_failure("Pending Draft save request identity or source bundle is invalid.")
	var base_hash: Variant = request.base_draft_sha256
	if (
		(int(request.base_revision) == 0 and base_hash != null)
		or (int(request.base_revision) > 0 and not _valid_sha256(base_hash))
	):
		return _integrity_failure("Pending Draft save CAS base is invalid.")
	var authority_session_id := _authority_session_id(bootstrap, session)
	if authority_session_id.is_empty() or str(request.session_id) != authority_session_id:
		return _integrity_failure("Pending Draft save is bound to another Session.")
	if not bootstrap.is_empty() and request.get("content_ref") != bootstrap.get("content"):
		return _integrity_failure("Pending Draft save is bound to another ContentRef.")
	var expected_identity := "%s:%s:%s" % [
		str(request.draft_id),
		str(request.base_revision),
		_source_bundle_identity(request.source_bundle),
	]
	if identity != expected_identity or str(envelope.idempotency_key) != RequestContextFactoryScript.idempotency_key_for("upsertProductSkillDraft", expected_identity):
		return _integrity_failure("Pending Draft save identity or Idempotency-Key is inconsistent.")
	return {"ok": true}


func _turn_envelope_integrity(
	slot: String,
	identity: String,
	envelope: Dictionary,
	bootstrap: Dictionary,
	session: Dictionary,
	active_skill: Dictionary,
	persisted_world: Dictionary,
	interaction_cursor: int,
) -> Dictionary:
	var required := [
		"session_id", "turn_id", "idempotency_key", "request", "pre_world",
		"interaction_cursor_before",
	]
	var allowed := required + ["presentation_after_sequence", "recovery"]
	if not _dictionary_has_exact_allowed_fields(envelope, required, allowed):
		return _integrity_failure("Pending Turn envelope is not closed.")
	var request: Variant = envelope.get("request")
	var pre_world: Variant = envelope.get("pre_world")
	if (
		not request is Dictionary
		or not ContractValidator.validate_agent_turn_create_request(request).ok
		or not pre_world is Dictionary
		or not _is_authoritative_snapshot(pre_world)
		or typeof(envelope.get("interaction_cursor_before")) != TYPE_INT
		or int(envelope.interaction_cursor_before) < 0
		or int(envelope.interaction_cursor_before) > interaction_cursor
	):
		return _integrity_failure("Pending Turn request, pre-World, or Interaction cursor is invalid.")
	if envelope.has("presentation_after_sequence") and (
		typeof(envelope.presentation_after_sequence) != TYPE_INT
		or int(envelope.presentation_after_sequence) < 0
	):
		return _integrity_failure("Pending Turn presentation cursor is invalid.")
	if envelope.has("recovery") and not _valid_turn_recovery(envelope.recovery, envelope):
		return _integrity_failure("Pending Turn GET-only recovery authority is invalid.")
	var session_id := str(envelope.session_id)
	var turn_id := str(envelope.turn_id)
	var authority_session_id := _authority_session_id(bootstrap, session)
	if (
		not _valid_local_identifier(session_id)
		or not _valid_local_identifier(turn_id)
		or str(request.turn_id) != turn_id
		or authority_session_id.is_empty()
		or session_id != authority_session_id
	):
		return _integrity_failure(
			"Pending Turn Session/Turn binding is invalid (session=%s authority=%s turn=%s request_turn=%s)." % [
				session_id,
				authority_session_id,
				turn_id,
				str(request.get("turn_id", "")),
			],
		)
	var expected_world_id := _authority_world_id(bootstrap, session)
	if (
		expected_world_id.is_empty()
		or str(pre_world.get("world_id", "")) != expected_world_id
		or int(request.expected_world_revision) != int(pre_world.get("revision", -1))
		or int(request.client_state.last_event_sequence) != int(pre_world.get("last_event_sequence", -1))
	):
		return _integrity_failure("Pending Turn pre-World does not match its authority and request cursors.")
	if (
		not persisted_world.is_empty()
		and str(persisted_world.get("world_id", "")) != str(pre_world.get("world_id", ""))
	):
		return _integrity_failure("Pending Turn and persisted World belong to different authorities.")
	var bindings: Array = request.skill_bindings
	if slot == "agent_turn":
		if not _valid_active_skill_tuple(active_skill) or bindings != [_binding_from_active(active_skill)]:
			return _integrity_failure("Pending bound Turn does not use the exact active Skill tuple.")
	elif not bindings.is_empty():
		return _integrity_failure("Pending hint Turn must not carry a Skill binding.")
	var expected_identity := JSON.stringify({
		"session_id": session_id,
		"world_revision": int(pre_world.revision),
		"last_event_sequence": int(pre_world.last_event_sequence),
		"client_turn_sequence": int(request.client_state.client_turn_sequence),
		"input": request.input,
		"skill_bindings": bindings,
	}).sha256_text()
	var expected_key := RequestContextFactoryScript.idempotency_key_for(
		"createAgentTurn",
		"%s:%s" % [session_id, turn_id],
	)
	if identity != expected_identity or str(envelope.idempotency_key) != expected_key:
		return _integrity_failure("Pending Turn logical identity or Idempotency-Key is inconsistent.")
	return {"ok": true}


func _valid_turn_recovery(value: Variant, envelope: Dictionary) -> bool:
	if not _closed_dictionary(value, [
		"phase", "command_id", "run_id", "presentation_after_sequence",
		"final_snapshot",
	]):
		return false
	var final_snapshot: Variant = value.get("final_snapshot")
	return (
		str(value.phase) == "RUN_TERMINAL"
		and _valid_local_identifier(str(value.command_id))
		and _valid_local_identifier(str(value.run_id))
		and typeof(value.presentation_after_sequence) == TYPE_INT
		and int(value.presentation_after_sequence) >= 0
		and int(value.presentation_after_sequence) == int(envelope.get("presentation_after_sequence", 0))
		and final_snapshot is Dictionary
		and _closed_dictionary(final_snapshot, [
			"world_id", "revision", "last_event_sequence", "state_hash",
		])
		and str(final_snapshot.world_id) == str(envelope.pre_world.world_id)
		and typeof(final_snapshot.revision) == TYPE_INT
		and int(final_snapshot.revision) >= int(envelope.pre_world.revision)
		and typeof(final_snapshot.last_event_sequence) == TYPE_INT
		and int(final_snapshot.last_event_sequence) >= int(envelope.pre_world.last_event_sequence)
		and _valid_sha256(final_snapshot.state_hash)
	)


func _dictionary_has_exact_allowed_fields(value: Variant, required: Array, allowed: Array) -> bool:
	if not value is Dictionary:
		return false
	for field in required:
		if not value.has(field):
			return false
	for field in value:
		if field not in allowed:
			return false
	return true


func _patch_decision_envelope_integrity(
	slot: String,
	identity: String,
	envelope: Dictionary,
	session: Dictionary,
) -> Dictionary:
	if not _closed_dictionary(envelope, [
		"idempotency_key", "request", "request_body", "request_body_sha256",
	]):
		return _integrity_failure("Pending PatchDecision envelope is not closed.")
	var request: Variant = envelope.get("request")
	var request_body: Variant = envelope.get("request_body")
	var parsed_body: Variant = _normalize_json_numbers(JSON.parse_string(request_body)) if typeof(request_body) == TYPE_STRING else null
	if (
		not request is Dictionary
		or typeof(request_body) != TYPE_STRING
		or str(request_body).is_empty()
		or not parsed_body is Dictionary
		or parsed_body != request
		or str(request_body) != JSON.stringify(request)
		or not _valid_sha256(envelope.get("request_body_sha256"))
		or str(request_body).sha256_text() != str(envelope.request_body_sha256)
	):
		return _integrity_failure("Pending PatchDecision request is absent.")
	var patch_id := slot.trim_prefix("patch_decision:")
	var expected_identity := "%s:%s:%s" % [
		str(request.get("interaction_id", "")),
		str(request.get("patch_id", "")),
		str(request.get("decision", "")),
	]
	if (
		patch_id.is_empty()
		or str(request.get("patch_id", "")) != patch_id
		or str(request.get("session_id", "")) != str(session.get("session_id", ""))
		or identity != expected_identity
		or str(envelope.idempotency_key) != RequestContextFactoryScript.idempotency_key_for("recordProductPatchDecision", expected_identity)
	):
		return _integrity_failure("Pending PatchDecision identity is inconsistent.")
	return {"ok": true}


func _valid_activation_binding(activation: Dictionary, active: Dictionary) -> bool:
	var scope: Variant = activation.get("scope")
	if (
		not scope is Dictionary
		or not ContractValidator.validate_identifier(scope.get("world_id")).ok
		or not ContractValidator.validate_identifier(scope.get("agent_profile_id")).ok
		or typeof(activation.get("registry_revision")) != TYPE_INT
		or int(activation.registry_revision) < 0
	):
		return false
	return active.is_empty() or int(active.registry_revision) == int(activation.registry_revision)


func _valid_session_authority_binding(session: Dictionary, bootstrap: Dictionary) -> bool:
	if session.is_empty():
		return true
	if not _valid_local_identifier(str(session.get("session_id", ""))):
		return false
	var bootstrap_session: Variant = bootstrap.get("session")
	if bootstrap_session is Dictionary:
		var current_id: Variant = bootstrap_session.get("current_session_id")
		if current_id != null and str(current_id) != str(session.session_id):
			return false
	if session.has("content") and not bootstrap.is_empty() and session.content != bootstrap.get("content"):
		return false
	var expected_world_id := _authority_world_id(bootstrap, session)
	return not session.has("world_id") or expected_world_id.is_empty() or str(session.world_id) == expected_world_id


func _valid_active_skill_tuple(value: Dictionary) -> bool:
	var required := [
		"activation_id", "skill_id", "skill_version_id", "artifact_sha256",
		"certification_id", "registry_revision", "activated_at",
	]
	if not _closed_dictionary(value, required):
		return false
	for field in ["activation_id", "skill_id", "skill_version_id", "certification_id"]:
		if not ContractValidator.validate_identifier(str(value.get(field, ""))).ok:
			return false
	return (
		_valid_sha256(value.artifact_sha256)
		and typeof(value.registry_revision) == TYPE_INT
		and int(value.registry_revision) >= 1
		and ContractValidator._validate_date_time(value.activated_at, "ActiveSkill.activated_at").ok
	)


func _binding_from_active(value: Dictionary) -> Dictionary:
	return {
		"skill_id": value.get("skill_id"),
		"skill_version_id": value.get("skill_version_id"),
		"artifact_sha256": value.get("artifact_sha256"),
		"certification_id": value.get("certification_id"),
	}


func _authority_world_id(bootstrap: Dictionary, session: Dictionary) -> String:
	var session_world_id := str(session.get("world_id", ""))
	if not session_world_id.is_empty():
		return session_world_id
	var world: Variant = bootstrap.get("world")
	if world is Dictionary and not str(world.get("world_id", "")).is_empty():
		return str(world.world_id)
	var activation: Variant = bootstrap.get("activation")
	var scope: Variant = activation.get("scope") if activation is Dictionary else null
	return str(scope.get("world_id", "")) if scope is Dictionary else ""


func _authority_session_id(bootstrap: Dictionary, session: Dictionary) -> String:
	var session_id := str(session.get("session_id", ""))
	if not session_id.is_empty():
		return session_id
	var bootstrap_session: Variant = bootstrap.get("session")
	return str(bootstrap_session.get("current_session_id", "")) if bootstrap_session is Dictionary else ""


func _session_create_request_identity(request: Dictionary) -> String:
	var content: Variant = request.get("content")
	if not content is Dictionary:
		return ""
	return JSON.stringify([
		request.get("world_id"),
		request.get("learner_id"),
		request.get("agent_profile_id"),
		request.get("channel"),
		request.get("locale"),
		content.get("unit_id"),
		content.get("version"),
		content.get("content_hash"),
		request.get("expected_world_revision"),
	]).sha256_text()


func _source_bundle_identity(bundle: Dictionary) -> String:
	var parts: Array[String] = []
	for file in bundle.get("files", []):
		parts.append("%s:%s" % [str(file.get("path", "")), str(file.get("content_sha256", ""))])
	parts.sort()
	return "|".join(parts).sha256_text()


func _valid_source_bundle(value: Variant) -> bool:
	if not value is Dictionary or not _closed_dictionary(value, ["language", "entrypoint", "files"]):
		return false
	if typeof(value.language) != TYPE_STRING or str(value.language).is_empty() or not value.files is Array or value.files.is_empty():
		return false
	var entrypoint := str(value.entrypoint)
	var entrypoint_count := 0
	var seen := {}
	for file in value.files:
		if (
			not file is Dictionary
			or not _closed_dictionary(file, ["path", "content", "content_sha256"])
			or typeof(file.path) != TYPE_STRING
			or str(file.path).is_empty()
			or seen.has(str(file.path))
			or typeof(file.content) != TYPE_STRING
			or not _valid_sha256(file.content_sha256)
			or str(file.content).sha256_text() != str(file.content_sha256)
		):
			return false
		seen[str(file.path)] = true
		if str(file.path) == entrypoint:
			entrypoint_count += 1
	return entrypoint_count == 1


func _make_patch_activation_invalidation(
	canonical_draft: Dictionary,
	request: Dictionary,
	receipt: Dictionary,
) -> Dictionary:
	if (
		str(request.get("decision", "")) != "ACCEPT"
		or str(receipt.get("decision", "")) != "ACCEPT"
		or not bool(receipt.get("draft_updated", false))
		or str(canonical_draft.get("session_id", "")) != str(request.get("session_id", ""))
		or str(canonical_draft.get("draft_id", "")) != str(request.get("draft_id", ""))
		or str(canonical_draft.get("skill_id", "")) != str(request.get("skill_id", ""))
		or int(canonical_draft.get("revision", -1)) != int(receipt.get("draft_revision_after", -2))
		or str(canonical_draft.get("draft_sha256", "")) != str(receipt.get("draft_sha256_after", ""))
		or str(canonical_draft.get("last_applied_patch_id", "")) != str(receipt.get("patch_id", ""))
	):
		return {}
	var marker := {
		"session_id": canonical_draft.session_id,
		"draft_id": canonical_draft.draft_id,
		"skill_id": canonical_draft.skill_id,
		"draft_revision": int(canonical_draft.revision),
		"draft_sha256": canonical_draft.draft_sha256,
		"patch_id": receipt.patch_id,
		"decision_id": receipt.decision_id,
		"decided_at": request.get("decided_at"),
		"invalidated_active_skill_tuple": active_skill_tuple.duplicate(true),
	}
	marker["authority_sha256"] = ContractValidator.canonical_json_sha256_v1(marker)
	return marker if _valid_patch_activation_invalidation(marker, authoritative_session) else {}


func _valid_patch_activation_invalidation(value: Variant, session: Dictionary) -> bool:
	if not value is Dictionary or not _closed_dictionary(value, [
		"session_id", "draft_id", "skill_id", "draft_revision", "draft_sha256",
		"patch_id", "decision_id", "decided_at", "invalidated_active_skill_tuple",
		"authority_sha256",
	]):
		return false
	for field in ["session_id", "draft_id", "skill_id", "patch_id", "decision_id"]:
		if not _valid_local_identifier(str(value.get(field, ""))):
			return false
	var invalidated: Variant = value.invalidated_active_skill_tuple
	if not invalidated is Dictionary or (not invalidated.is_empty() and not _valid_active_skill_tuple(invalidated)):
		return false
	if (
		typeof(value.draft_revision) != TYPE_INT
		or int(value.draft_revision) < 1
		or not _valid_sha256(value.draft_sha256)
		or not _valid_sha256(value.authority_sha256)
		or not ContractValidator._validate_date_time(value.decided_at, "PatchActivationInvalidation.decided_at").ok
		or (not session.is_empty() and str(value.session_id) != str(session.get("session_id", "")))
	):
		return false
	var projection: Dictionary = value.duplicate(true)
	projection.erase("authority_sha256")
	return ContractValidator.canonical_json_sha256_v1(projection) == str(value.authority_sha256)


func _activation_closes_patch_invalidation(active: Variant, built_draft_authority: Dictionary) -> bool:
	if (
		not active is Dictionary
		or not _valid_active_skill_tuple(active)
		or not _closed_dictionary(built_draft_authority, [
			"build_id", "session_id", "draft_id", "skill_id", "draft_revision",
			"draft_sha256", "source_bundle_sha256",
		])
		or not _valid_local_identifier(str(built_draft_authority.build_id))
		or not _valid_sha256(built_draft_authority.draft_sha256)
		or not _valid_sha256(built_draft_authority.source_bundle_sha256)
	):
		return false
	return (
		str(built_draft_authority.session_id) == str(patch_activation_invalidation.get("session_id", ""))
		and str(built_draft_authority.draft_id) == str(patch_activation_invalidation.get("draft_id", ""))
		and str(built_draft_authority.skill_id) == str(patch_activation_invalidation.get("skill_id", ""))
		and int(built_draft_authority.draft_revision) == int(patch_activation_invalidation.get("draft_revision", -1))
		and str(built_draft_authority.draft_sha256) == str(patch_activation_invalidation.get("draft_sha256", ""))
		and str(active.skill_id) == str(built_draft_authority.skill_id)
		and str(draft.get("session_id", "")) == str(built_draft_authority.session_id)
		and str(draft.get("draft_id", "")) == str(built_draft_authority.draft_id)
		and str(draft.get("skill_id", "")) == str(built_draft_authority.skill_id)
		and int(draft.get("revision", -1)) == int(built_draft_authority.draft_revision)
		and str(draft.get("draft_sha256", "")) == str(built_draft_authority.draft_sha256)
	)


func _make_patch_failure_recovery_authority(
	build: Dictionary,
	run: Dictionary,
	interaction: Dictionary,
	evidence: Array,
	result: Dictionary,
) -> Dictionary:
	var build_validation := ContractValidator.validate_skill_build(build)
	var run_validation := ContractValidator.validate_run(run)
	var feedback: Variant = interaction.get("feedback")
	if (
		not build_validation.ok
		or not run_validation.ok
		or not feedback is Dictionary
		or not _valid_active_skill_tuple(active_skill_tuple)
		or str(run.get("status", "")) not in ["REJECTED", "FAILED"]
		or not bool(run.get("terminal", false))
		or run.get("world_application", {}).get("receipt") != null
		or str(run.get("session_id", "")) != str(authoritative_session.get("session_id", ""))
		or str(interaction.get("session_id", "")) != str(run.get("session_id", ""))
		or str(interaction.get("turn_id", "")) != str(run.get("turn_id", ""))
		or str(feedback.get("command_id", "")) != str(run.get("command_id", ""))
		or str(feedback.get("run_id", "")) != str(run.get("run_id", ""))
		or feedback.get("evidence_refs") != run.get("evidence_refs")
		or evidence.is_empty()
		or bool(result.get("objective_succeeded", true))
		or str(result.get("run_id", "")) != str(run.get("run_id", ""))
		or str(build.get("skill_id", "")) != str(active_skill_tuple.get("skill_id", ""))
		or str(build.get("skill_version_id", "")) != str(active_skill_tuple.get("skill_version_id", ""))
		or build.get("artifact", {}).get("artifact_sha256") != active_skill_tuple.get("artifact_sha256")
		or build.get("certification", {}).get("certification_id") != active_skill_tuple.get("certification_id")
		or run.get("skill") != _binding_from_active(active_skill_tuple)
		or build.get("request_context", {}).get("actor") != authoritative_bootstrap.get("actor")
		or build.get("request_context", {}).get("content_ref") != authoritative_bootstrap.get("content")
		or run.get("request_context", {}).get("actor") != authoritative_bootstrap.get("actor")
		or run.get("request_context", {}).get("content_ref") != authoritative_bootstrap.get("content")
	):
		return {}
	var evidence_by_id := {}
	for resource_value: Variant in evidence:
		if not resource_value is Dictionary:
			return {}
		var resource: Dictionary = resource_value
		var evidence_validation := ContractValidator.validate_evidence(resource)
		var reference: Variant = resource.get("evidence_ref")
		if not evidence_validation.ok or not reference is Dictionary:
			return {}
		var evidence_id := str(reference.get("evidence_id", ""))
		if evidence_id.is_empty() or evidence_by_id.has(evidence_id):
			return {}
		evidence_by_id[evidence_id] = resource.duplicate(true)
	for reference_value: Variant in run.evidence_refs:
		if not reference_value is Dictionary or not evidence_by_id.has(str(reference_value.get("evidence_id", ""))):
			return {}
		if evidence_by_id[str(reference_value.evidence_id)].get("evidence_ref") != reference_value:
			return {}
	if evidence_by_id.size() != run.evidence_refs.size():
		return {}
	var marker := {
		"session_id": str(run.session_id),
		"build_id": str(build.build_id),
		"certified_build": build.duplicate(true),
		"certified_build_sha256": ContractValidator.canonical_json_sha256_v1(build),
		"run_id": str(run.run_id),
		"run_sha256": ContractValidator.canonical_json_sha256_v1(run),
		"interaction_id": str(interaction.get("interaction_id", "")),
		"interaction_revision": int(interaction.get("interaction_revision", -1)),
		"interaction_sequence": int(interaction.get("sequence", -1)),
		"interaction_sha256": ContractValidator.canonical_json_sha256_v1(interaction),
		"evidence_refs": run.evidence_refs.duplicate(true),
		"evidence_resources": evidence.duplicate(true),
		"evidence_resources_sha256": ContractValidator.canonical_json_sha256_v1(evidence),
		"objective_result": result.duplicate(true),
	}
	marker["authority_sha256"] = ContractValidator.canonical_json_sha256_v1(marker)
	return marker if _valid_patch_failure_recovery_authority(marker, authoritative_session, active_skill_tuple) else {}


func _valid_patch_failure_recovery_authority(
	value: Variant,
	session: Dictionary,
	active: Dictionary,
) -> bool:
	if not value is Dictionary or not _closed_dictionary(value, [
		"session_id", "build_id", "certified_build", "certified_build_sha256",
		"run_id", "run_sha256", "interaction_id", "interaction_revision",
		"interaction_sequence", "interaction_sha256", "evidence_refs",
		"evidence_resources", "evidence_resources_sha256", "objective_result",
		"authority_sha256",
	]):
		return false
	for field in ["session_id", "build_id", "run_id", "interaction_id"]:
		if not _valid_local_identifier(str(value.get(field, ""))):
			return false
	for field in ["certified_build_sha256", "run_sha256", "interaction_sha256", "evidence_resources_sha256", "authority_sha256"]:
		if not _valid_sha256(value.get(field)):
			return false
	var build: Variant = value.certified_build
	var result: Variant = value.objective_result
	if (
		not build is Dictionary
		or not ContractValidator.validate_skill_build(build).ok
		or ContractValidator.canonical_json_sha256_v1(build) != str(value.certified_build_sha256)
		or str(build.get("build_id", "")) != str(value.build_id)
		or str(build.get("status", "")) != "CERTIFIED"
		or not bool(build.get("terminal", false))
		or not value.evidence_refs is Array
		or value.evidence_refs.is_empty()
		or not value.evidence_resources is Array
		or value.evidence_resources.is_empty()
		or ContractValidator.canonical_json_sha256_v1(value.evidence_resources) != str(value.evidence_resources_sha256)
		or not result is Dictionary
		or not _closed_dictionary(result, ["summary", "objective_succeeded", "run_id"])
		or typeof(result.get("summary")) != TYPE_STRING
		or str(result.get("summary", "")).is_empty()
		or str(result.get("summary", "")).length() > 2000
		or typeof(result.get("objective_succeeded")) != TYPE_BOOL
		or bool(result.get("objective_succeeded", true))
		or typeof(result.get("run_id")) != TYPE_STRING
		or str(result.get("run_id", "")) != str(value.run_id)
		or typeof(value.interaction_revision) != TYPE_INT
		or int(value.interaction_revision) < 1
		or typeof(value.interaction_sequence) != TYPE_INT
		or int(value.interaction_sequence) < 1
		or (not session.is_empty() and str(value.session_id) != str(session.get("session_id", "")))
		or not _valid_active_skill_tuple(active)
		or str(build.get("skill_id", "")) != str(active.get("skill_id", ""))
		or str(build.get("skill_version_id", "")) != str(active.get("skill_version_id", ""))
		or build.get("artifact", {}).get("artifact_sha256") != active.get("artifact_sha256")
		or build.get("certification", {}).get("certification_id") != active.get("certification_id")
	):
		return false
	var evidence_ids := {}
	for resource_value: Variant in value.evidence_resources:
		if not resource_value is Dictionary or not ContractValidator.validate_evidence(resource_value).ok:
			return false
		var reference: Variant = resource_value.get("evidence_ref")
		if not reference is Dictionary or reference not in value.evidence_refs:
			return false
		var evidence_id := str(reference.get("evidence_id", ""))
		if evidence_id.is_empty() or evidence_ids.has(evidence_id):
			return false
		evidence_ids[evidence_id] = true
	if evidence_ids.size() != value.evidence_refs.size():
		return false
	var projection: Dictionary = value.duplicate(true)
	projection.erase("authority_sha256")
	return ContractValidator.canonical_json_sha256_v1(projection) == str(value.authority_sha256)


func _valid_sha256(value: Variant) -> bool:
	if typeof(value) != TYPE_STRING or value.length() != 64:
		return false
	for index in range(value.length()):
		if "0123456789abcdef".find(value.substr(index, 1)) < 0:
			return false
	return true


func _valid_local_identifier(value: String) -> bool:
	if value.length() < 8 or value.length() > 128:
		return false
	for index in range(value.length()):
		var character := value.substr(index, 1)
		var allowed := "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
		if allowed.find(character) < 0 or (index == 0 and character in ["_", "-"]):
			return false
	return true


func _closed_dictionary(value: Dictionary, fields: Array) -> bool:
	if value.size() != fields.size():
		return false
	for field in fields:
		if not value.has(field):
			return false
	return true


func _integrity_failure(message: String) -> Dictionary:
	return {"ok": false, "message": message}


func _set_persistence_integrity_error(code: String, message: String) -> void:
	_persistence_integrity_error = _local_error(code, message)


func _normalize_json_numbers(value: Variant) -> Variant:
	if typeof(value) == TYPE_FLOAT and is_finite(value) and value == floor(value):
		return int(value)
	if value is Array:
		var normalized_array: Array = []
		for item in value:
			normalized_array.append(_normalize_json_numbers(item))
		return normalized_array
	if value is Dictionary:
		var normalized_dictionary := {}
		for key in value:
			normalized_dictionary[key] = _normalize_json_numbers(value[key])
		return normalized_dictionary
	return value


func _local_error(code: String, message: String) -> Dictionary:
	return {"scope": "CLIENT_LOCAL", "code": code, "message": message}


func _pending_operation_failure(code: String, message: String) -> Dictionary:
	return {
		"ok": false,
		"status": 0,
		"headers": {},
		"error": _local_error(code, message),
	}
