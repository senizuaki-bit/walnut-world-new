class_name WaterCandidateEvaluator
extends RefCounted

## Pure frontend compatibility evaluator for a rejected WATER-only Run.
## It never writes ClientStore, never creates World authority fields, and only
## mutates a duplicate of the minimum plot data needed for local diagnostics.

const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const SOURCE := "SANDBOX_ACTION_INTENT_CANDIDATE"
const EXPECTED_PLOT_COUNT := 8
const MAX_HYDRATION := 10000
const CONTENT_REF_FIELDS := ["unit_id", "version", "content_hash"]
const CONFIG_FIELDS := ["enabled", "content_ref", "plot_rules"]
const PLOT_RULE_FIELDS := ["ui_index", "accepted_min", "accepted_max"]
const RUN_SKILL_FIELDS := [
	"skill_id", "skill_version_id", "artifact_sha256", "certification_id",
]


static func evaluate(
	config: Dictionary,
	content_ref: Dictionary,
	base_snapshot: Dictionary,
	current_snapshot: Dictionary,
	run: Dictionary,
	active_skill_tuple: Dictionary,
	objective_result: Dictionary,
) -> Dictionary:
	var gate := _validate_gate(
		config,
		content_ref,
		base_snapshot,
		current_snapshot,
		run,
		active_skill_tuple,
		objective_result,
	)
	if not bool(gate.get("ok", false)):
		return gate
	var plot_rules: Dictionary = config.plot_rules
	var snapshot_state: Dictionary = base_snapshot.state
	var avatar: Dictionary = snapshot_state.avatar
	var plots: Array = snapshot_state.plots
	var candidate_by_plot: Dictionary = {}
	for raw_plot: Variant in plots:
		var plot: Dictionary = raw_plot
		candidate_by_plot[str(plot.plot_id)] = {
			"plot_id": str(plot.plot_id),
			"hydration": int(plot.hydration),
			"soil_state": str(plot.soil_state),
			"crop": plot.crop.duplicate(true) if plot.crop is Dictionary else null,
		}
	var actions: Array[Dictionary] = []
	var seen_intent_ids: Dictionary = {}
	for raw_intent: Variant in run.sandbox.action_intents:
		if not raw_intent is Dictionary:
			return _failure("CANDIDATE_INTENT_INVALID", "Sandbox ActionIntent is not an object.", true)
		var intent: Dictionary = raw_intent
		var intent_validation := _validate_water_intent(
			intent,
			seen_intent_ids,
			candidate_by_plot,
			str(avatar.entity_id),
			int(base_snapshot.revision),
		)
		if not bool(intent_validation.get("ok", false)):
			return intent_validation
		var plot_id := str(intent.plot_id)
		var candidate_plot: Dictionary = candidate_by_plot[plot_id]
		var crop: Dictionary = candidate_plot.crop
		var hydration_before := int(candidate_plot.hydration)
		var growth_before := int(crop.growth_stage)
		var hydration_after := mini(MAX_HYDRATION, hydration_before + int(intent.amount_ml))
		var growth_after := mini(100, growth_before + 1)
		candidate_plot.hydration = hydration_after
		crop.growth_stage = growth_after
		candidate_plot.crop = crop
		candidate_by_plot[plot_id] = candidate_plot
		actions.append({
			"intent_id": str(intent.intent_id),
			"plot_id": plot_id,
			"ui_index": int(plot_rules[plot_id].ui_index),
			"amount_ml": int(intent.amount_ml),
			"hydration_before": hydration_before,
			"hydration_after": hydration_after,
			"growth_stage_before": growth_before,
			"growth_stage_after": growth_after,
		})
		seen_intent_ids[str(intent.intent_id)] = true

	var plot_results: Array[Dictionary] = []
	var failed_count := 0
	var ordered_ids: Array[String] = []
	for plot_id_value: Variant in plot_rules:
		ordered_ids.append(str(plot_id_value))
	ordered_ids.sort_custom(func(left: String, right: String) -> bool:
		return int(plot_rules[left].ui_index) < int(plot_rules[right].ui_index)
	)
	var candidate_plots: Array[Dictionary] = []
	for plot_id: String in ordered_ids:
		var rule: Dictionary = plot_rules[plot_id]
		var candidate_plot: Dictionary = candidate_by_plot[plot_id]
		var hydration := int(candidate_plot.hydration)
		var status := "CORRECT"
		if hydration < int(rule.accepted_min):
			status = "UNDERWATERED"
		elif hydration > int(rule.accepted_max):
			status = "OVERWATERED"
		if status != "CORRECT":
			failed_count += 1
		plot_results.append({
			"plot_id": plot_id,
			"ui_index": int(rule.ui_index),
			"hydration": hydration,
			"accepted_min": int(rule.accepted_min),
			"accepted_max": int(rule.accepted_max),
			"status": status,
		})
		candidate_plots.append({
			"plot_id": plot_id,
			"hydration": hydration,
			"growth_stage": int(candidate_plot.crop.growth_stage),
		})
	var objective_succeeded := failed_count == 0
	var failure_key := "" if objective_succeeded else "PLOT_TARGET_NOT_MET"
	var summary := (
		"8 块土地均进入本关允许湿度范围。"
		if objective_succeeded
		else "%d 块土地未进入本关允许湿度范围。" % failed_count
	)
	var digest_basis := {
		"source": SOURCE,
		"run_id": str(run.run_id),
		"base_snapshot_revision": int(base_snapshot.revision),
		"base_snapshot_state_hash": str(base_snapshot.state_hash),
		"actions": actions,
		"plot_results": plot_results,
	}
	return {
		"ok": true,
		"eligible": true,
		"source": SOURCE,
		"objective_succeeded": objective_succeeded,
		"failure_key": failure_key,
		"summary": summary,
		"actions": actions,
		"plot_results": plot_results,
		"candidate_state": {"plots": candidate_plots},
		"provenance": {
			"run_id": str(run.run_id),
			"invocation_id": str(run.sandbox.invocation_id),
			"base_snapshot_revision": int(base_snapshot.revision),
			"base_snapshot_state_hash": str(base_snapshot.state_hash),
			"candidate_digest": ContractValidator.canonical_json_sha256_v1(digest_basis),
			"authority_committed": false,
		},
	}


static func _validate_gate(
	config: Dictionary,
	content_ref: Dictionary,
	base_snapshot: Dictionary,
	current_snapshot: Dictionary,
	run: Dictionary,
	active_skill_tuple: Dictionary,
	objective_result: Dictionary,
) -> Dictionary:
	if not _exact_shape(config, CONFIG_FIELDS) or typeof(config.enabled) != TYPE_BOOL:
		return _failure("CANDIDATE_CONFIG_INVALID", "WATER candidate configuration is not closed.")
	if not bool(config.enabled):
		return _failure("CANDIDATE_MODE_DISABLED", "WATER candidate compatibility is disabled.")
	if not _valid_content_ref(config.content_ref) or not _valid_content_ref(content_ref):
		return _failure("CANDIDATE_CONTENT_REF_INVALID", "WATER candidate ContentRef is invalid.")
	if content_ref != config.content_ref:
		return _failure("CANDIDATE_CONTENT_REF_MISMATCH", "This ContentRef is not allowed to use WATER candidate compatibility.")
	if not _valid_plot_rules(config.plot_rules):
		return _failure("CANDIDATE_PLOT_RULES_INVALID", "WATER candidate plot rules are incomplete or invalid.")
	var base_validation := ContractValidator.validate_world_snapshot(base_snapshot)
	var current_validation := ContractValidator.validate_world_snapshot(current_snapshot)
	if not bool(base_validation.get("ok", false)) or not bool(current_validation.get("ok", false)):
		return _failure("CANDIDATE_SNAPSHOT_INVALID", "WATER candidate evaluation requires two valid authoritative Snapshots.")
	if base_snapshot != current_snapshot:
		return _failure("CANDIDATE_WORLD_CHANGED", "Authoritative World changed during the rejected Run.")
	if base_snapshot.request_context.content_ref != content_ref:
		return _failure("CANDIDATE_SNAPSHOT_CONTENT_MISMATCH", "Base Snapshot belongs to another ContentRef.")
	if not run is Dictionary or not _required_fields(run, [
		"run_id", "status", "terminal", "skill", "sandbox", "world_application",
	]):
		return _failure("CANDIDATE_RUN_INVALID", "Rejected Run lacks required candidate authority.")
	if str(run.status) != "REJECTED" or typeof(run.terminal) != TYPE_BOOL or not bool(run.terminal):
		return _failure("CANDIDATE_RUN_STATUS_UNSUPPORTED", "Only a terminal REJECTED Run can enter candidate compatibility.")
	if not run.sandbox is Dictionary or not _required_fields(run.sandbox, ["invocation_id", "status", "action_intents"]):
		return _failure("CANDIDATE_SANDBOX_INVALID", "Rejected Run lacks Sandbox output.")
	if str(run.sandbox.status) != "SUCCEEDED" or not run.sandbox.action_intents is Array or run.sandbox.action_intents.is_empty():
		return _failure("CANDIDATE_SANDBOX_UNSUPPORTED", "Candidate compatibility requires non-empty SUCCEEDED Sandbox ActionIntents.")
	if not run.world_application is Dictionary or not _required_fields(run.world_application, ["status", "receipt", "failure"]):
		return _failure("CANDIDATE_WORLD_APPLICATION_INVALID", "Rejected Run lacks WorldApplication authority.")
	var world_application: Dictionary = run.world_application
	if str(world_application.status) != "REJECTED" or world_application.receipt != null:
		return _failure("CANDIDATE_WORLD_APPLICATION_UNSUPPORTED", "Candidate compatibility requires an uncommitted REJECTED WorldApplication.")
	var failure: Variant = world_application.failure
	if not failure is Dictionary:
		return _failure("CANDIDATE_WORLD_FAILURE_INVALID", "Rejected WorldApplication lacks a structured failure.")
	var details: Variant = failure.get("details")
	if (
		str(failure.get("code", "")) != "WORLD_RULE_REJECTED"
		or str(failure.get("stage", "")) != "WORLD_VALIDATE"
		or not details is Dictionary
		or str(details.get("reason", "")) != "TASK_INCOMPLETE"
	):
		return _failure("CANDIDATE_REJECTION_REASON_UNSUPPORTED", "Only WORLD_RULE_REJECTED/WORLD_VALIDATE/TASK_INCOMPLETE can enter candidate compatibility.")
	if not _skill_matches(run.skill, active_skill_tuple):
		return _failure("CANDIDATE_SKILL_MISMATCH", "Rejected Run does not match the active exact Skill tuple.")
	if str(objective_result.get("run_id", "")) != str(run.run_id) or objective_result.get("objective_succeeded") != false:
		return _failure("CANDIDATE_OBJECTIVE_MISMATCH", "Client objective result does not identify the same failed Run.")
	var snapshot_plots: Array = base_snapshot.state.plots
	var snapshot_plot_ids: Dictionary = {}
	for raw_plot: Variant in snapshot_plots:
		if raw_plot is Dictionary:
			snapshot_plot_ids[str(raw_plot.get("plot_id", ""))] = true
	for plot_id: Variant in config.plot_rules:
		if not snapshot_plot_ids.has(str(plot_id)):
			return _failure("CANDIDATE_RULE_PLOT_MISSING", "Configured candidate plot does not exist in the authoritative Snapshot.", true)
	return {"ok": true, "eligible": true}


static func _validate_water_intent(
	intent: Dictionary,
	seen_intent_ids: Dictionary,
	candidate_by_plot: Dictionary,
	actor_entity_id: String,
	expected_revision: int,
) -> Dictionary:
	if not _exact_shape(intent, [
		"intent_id", "action_type", "actor_entity_id", "expected_world_revision",
		"plot_id", "amount_ml",
	]):
		return _failure("CANDIDATE_INTENT_SHAPE_INVALID", "WATER ActionIntent is not byte-closed.", true)
	if str(intent.action_type) != "WATER":
		return _failure("CANDIDATE_ACTION_TYPE_UNSUPPORTED", "Candidate Run contains a non-WATER ActionIntent.")
	var intent_id := str(intent.intent_id)
	if not _identifier(intent_id) or seen_intent_ids.has(intent_id):
		return _failure("CANDIDATE_INTENT_ID_INVALID", "WATER ActionIntent identity is invalid or duplicated.", true)
	if str(intent.actor_entity_id) != actor_entity_id:
		return _failure("CANDIDATE_ACTOR_MISMATCH", "WATER ActionIntent targets another avatar.", true)
	if typeof(intent.expected_world_revision) != TYPE_INT or int(intent.expected_world_revision) != expected_revision:
		return _failure("CANDIDATE_WORLD_REVISION_MISMATCH", "WATER ActionIntent targets another World revision.", true)
	var plot_id := str(intent.plot_id)
	if not _identifier(plot_id) or not candidate_by_plot.has(plot_id):
		return _failure("CANDIDATE_PLOT_UNKNOWN", "WATER ActionIntent targets an unknown plot.", true)
	if typeof(intent.amount_ml) != TYPE_INT or int(intent.amount_ml) < 1 or int(intent.amount_ml) > MAX_HYDRATION:
		return _failure("CANDIDATE_AMOUNT_INVALID", "WATER amount_ml is outside the contract range.", true)
	var plot: Dictionary = candidate_by_plot[plot_id]
	if str(plot.soil_state) != "TILLED" or not plot.crop is Dictionary:
		return _failure("CANDIDATE_WATER_ILLEGAL", "WATER ActionIntent targets an untilled or empty plot.", true)
	return {"ok": true}


static func _valid_plot_rules(value: Variant) -> bool:
	if not value is Dictionary or value.size() != EXPECTED_PLOT_COUNT:
		return false
	var seen_ui_indices: Dictionary = {}
	for raw_plot_id: Variant in value:
		var plot_id := str(raw_plot_id)
		var rule: Variant = value[raw_plot_id]
		if not _identifier(plot_id) or not _exact_shape(rule, PLOT_RULE_FIELDS):
			return false
		if (
			typeof(rule.ui_index) != TYPE_INT
			or int(rule.ui_index) < 0
			or int(rule.ui_index) >= EXPECTED_PLOT_COUNT
			or seen_ui_indices.has(int(rule.ui_index))
			or typeof(rule.accepted_min) != TYPE_INT
			or typeof(rule.accepted_max) != TYPE_INT
			or int(rule.accepted_min) < 0
			or int(rule.accepted_max) > MAX_HYDRATION
			or int(rule.accepted_min) > int(rule.accepted_max)
		):
			return false
		seen_ui_indices[int(rule.ui_index)] = true
	return seen_ui_indices.size() == EXPECTED_PLOT_COUNT


static func _skill_matches(run_skill: Variant, active_skill: Dictionary) -> bool:
	if not _exact_shape(run_skill, RUN_SKILL_FIELDS):
		return false
	for field: String in RUN_SKILL_FIELDS:
		if run_skill.get(field) != active_skill.get(field):
			return false
	return true


static func _valid_content_ref(value: Variant) -> bool:
	return (
		_exact_shape(value, CONTENT_REF_FIELDS)
		and _identifier(str(value.unit_id))
		and typeof(value.version) == TYPE_STRING
		and not str(value.version).is_empty()
		and typeof(value.content_hash) == TYPE_STRING
		and _matches(str(value.content_hash), "^[a-f0-9]{64}$")
	)


static func _exact_shape(value: Variant, fields: Array) -> bool:
	if not value is Dictionary or value.size() != fields.size():
		return false
	return _required_fields(value, fields)


static func _required_fields(value: Dictionary, fields: Array) -> bool:
	for field: Variant in fields:
		if not value.has(field):
			return false
	return true


static func _identifier(value: String) -> bool:
	return _matches(value, "^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


static func _matches(value: String, pattern: String) -> bool:
	var regex := RegEx.new()
	return regex.compile(pattern) == OK and regex.search(value) != null


static func _failure(code: String, message: String, eligible := false) -> Dictionary:
	return {
		"ok": false,
		"eligible": eligible,
		"code": code,
		"message": message,
	}
