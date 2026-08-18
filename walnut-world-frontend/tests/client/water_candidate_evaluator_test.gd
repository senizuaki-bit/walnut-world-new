extends SceneTree

const Evaluator := preload("res://scripts/client/water_candidate_evaluator.gd")
const CONTENT_REF := {
	"unit_id": "YAYA_FARM_001",
	"version": "1.4.0",
	"content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
}


func _initialize() -> void:
	var failures: Array[String] = []
	var snapshot := _snapshot()
	var original := snapshot.duplicate(true)
	var run := _run(_water_intents())
	var result := Evaluator.evaluate(
		_config(), CONTENT_REF, snapshot, snapshot.duplicate(true), run,
		_active_skill(), {"objective_succeeded": false, "run_id": run.run_id},
	)
	if not bool(result.get("ok", false)) or not bool(result.get("objective_succeeded", false)):
		failures.append("8 个合法 WATER intents 应得到本地候选完成。")
	if snapshot != original:
		failures.append("候选评估不得修改原始权威 Snapshot。")
	if result.get("candidate_state", {}).has("revision") or result.get("candidate_state", {}).has("state_hash"):
		failures.append("候选状态不得生成权威 revision/state_hash。")
	if bool(result.get("provenance", {}).get("authority_committed", true)):
		failures.append("候选来源必须明确标记世界未提交。")

	var missing := _run(_water_intents().slice(0, 7))
	result = _evaluate(missing, snapshot)
	if not bool(result.get("ok", false)) or bool(result.get("objective_succeeded", true)) or str(result.get("failure_key", "")) != "PLOT_TARGET_NOT_MET":
		failures.append("漏掉一块土地必须得到本地候选失败。")

	var overwatered_intents := _water_intents()
	overwatered_intents[0].amount_ml = 250
	result = _evaluate(_run(overwatered_intents), snapshot)
	if not _has_plot_status(result, "farm_plot_0001", "OVERWATERED"):
		failures.append("超过 accepted_max 必须标记为 OVERWATERED。")

	var repeated := _water_intents()
	repeated[0].amount_ml = 40
	repeated.insert(1, _intent(9, 0, 60))
	result = _evaluate(_run(repeated), snapshot)
	if not bool(result.get("objective_succeeded", false)) or result.get("actions", []).size() != 9:
		failures.append("同一地块的多个 intent 必须按原顺序逐次计算且不得合并。")
	elif int(result.actions[0].hydration_after) != 40 or int(result.actions[1].hydration_before) != 40:
		failures.append("重复 WATER intent 的 before/after 链不连续。")

	var mixed := _water_intents()
	mixed[0] = {
		"intent_id": "intent_harvest_0001", "action_type": "HARVEST",
		"actor_entity_id": "avatar_demo_0001", "expected_world_revision": 4,
		"plot_id": "farm_plot_0001",
	}
	result = _evaluate(_run(mixed), snapshot)
	if bool(result.get("ok", false)) or str(result.get("code", "")) != "CANDIDATE_INTENT_SHAPE_INVALID":
		failures.append("混入非 WATER intent 必须失败关闭。")

	var changed_snapshot := snapshot.duplicate(true)
	changed_snapshot.revision = 5
	result = Evaluator.evaluate(
		_config(), CONTENT_REF, snapshot, changed_snapshot, run,
		_active_skill(), {"objective_succeeded": false, "run_id": run.run_id},
	)
	if bool(result.get("ok", false)) or str(result.get("code", "")) != "CANDIDATE_WORLD_CHANGED":
		failures.append("Run 前后权威 Snapshot 发生变化时必须拒绝候选模式。")

	var wrong_content := CONTENT_REF.duplicate(true)
	wrong_content.content_hash = "b".repeat(64)
	result = Evaluator.evaluate(
		_config(), wrong_content, snapshot, snapshot.duplicate(true), run,
		_active_skill(), {"objective_succeeded": false, "run_id": run.run_id},
	)
	if bool(result.get("ok", false)) or str(result.get("code", "")) != "CANDIDATE_CONTENT_REF_MISMATCH":
		failures.append("ContentRef 不匹配时必须拒绝候选模式。")

	var wrong_reason := run.duplicate(true)
	wrong_reason.world_application.failure.details.reason = "OTHER_REASON"
	result = _evaluate(wrong_reason, snapshot)
	if bool(result.get("ok", false)) or str(result.get("code", "")) != "CANDIDATE_REJECTION_REASON_UNSUPPORTED":
		failures.append("非 TASK_INCOMPLETE 的世界拒绝不得进入候选模式。")

	if failures.is_empty():
		print("WATER_CANDIDATE_EVALUATOR_TEST_PASS")
		quit(0)
		return
	for failure: String in failures:
		push_error(failure)
	quit(1)


func _evaluate(run: Dictionary, snapshot: Dictionary) -> Dictionary:
	return Evaluator.evaluate(
		_config(), CONTENT_REF, snapshot, snapshot.duplicate(true), run,
		_active_skill(), {"objective_succeeded": false, "run_id": run.run_id},
	)


func _config() -> Dictionary:
	var rules: Dictionary = {}
	for index: int in range(8):
		rules["farm_plot_%04d" % (index + 1)] = {
			"ui_index": index,
			"accepted_min": 100,
			"accepted_max": 199,
		}
	return {"enabled": true, "content_ref": CONTENT_REF.duplicate(true), "plot_rules": rules}


func _snapshot() -> Dictionary:
	var plots: Array[Dictionary] = []
	for index: int in range(8):
		plots.append({
			"plot_id": "farm_plot_%04d" % (index + 1),
			"position": {"x": index, "y": 0},
			"soil_state": "TILLED",
			"hydration": 0,
			"crop": {
				"crop_type": ["carrot", "tomato", "potato", "corn"][index % 4],
				"growth_stage": 1,
				"planted_at_tick": 1,
				"ready_to_harvest": false,
			},
			"last_updated_event_sequence": 3,
		})
	return {
		"request_context": {
			"schema_version": "1.0.0",
			"request_id": "req_candidate_0001",
			"correlation_id": "corr_candidate_0001",
			"trace_id": "trace_candidate_0001",
			"requested_at": "2026-08-17T00:00:00Z",
			"actor": {
				"tenant_id": "tenant_demo", "actor_id": "student_demo",
				"actor_type": "student", "roles": ["game:player"],
			},
			"content_ref": CONTENT_REF.duplicate(true),
		},
		"world_id": "world_demo_0001",
		"revision": 4,
		"last_event_sequence": 3,
		"state_schema_version": "1.0.0",
		"state_hash": "f".repeat(64),
		"generated_at": "2026-08-17T00:00:00Z",
		"world_rules_version": "farm-rules-12",
		"state": {
			"clock": {"day": 1, "minute_of_day": 10, "tick": 10},
			"avatar": {"entity_id": "avatar_demo_0001", "position": {"x": 0, "y": 0}, "energy": 10000},
			"inventory": [],
			"plots": plots,
			"agents": [],
		},
	}


func _run(intents: Array[Dictionary]) -> Dictionary:
	return {
		"run_id": "run_candidate_0001",
		"status": "REJECTED",
		"terminal": true,
		"skill": _active_skill(),
		"sandbox": {
			"invocation_id": "invocation_candidate_0001",
			"status": "SUCCEEDED",
			"action_intents": intents.duplicate(true),
		},
		"world_application": {
			"status": "REJECTED",
			"receipt": null,
			"failure": {
				"code": "WORLD_RULE_REJECTED",
				"stage": "WORLD_VALIDATE",
				"details": {"reason": "TASK_INCOMPLETE"},
			},
		},
	}


func _water_intents() -> Array[Dictionary]:
	var intents: Array[Dictionary] = []
	for index: int in range(8):
		intents.append(_intent(index + 1, index, 100))
	return intents


func _intent(identity: int, plot_index: int, amount_ml: int) -> Dictionary:
	return {
		"intent_id": "intent_water_%04d" % identity,
		"action_type": "WATER",
		"actor_entity_id": "avatar_demo_0001",
		"expected_world_revision": 4,
		"plot_id": "farm_plot_%04d" % (plot_index + 1),
		"amount_ml": amount_ml,
	}


func _active_skill() -> Dictionary:
	return {
		"skill_id": "skill_candidate_0001",
		"skill_version_id": "skillver_candidate_0001",
		"artifact_sha256": "c".repeat(64),
		"certification_id": "cert_candidate_0001",
	}


func _has_plot_status(result: Dictionary, plot_id: String, status: String) -> bool:
	for item: Variant in result.get("plot_results", []):
		if item is Dictionary and str(item.get("plot_id", "")) == plot_id:
			return str(item.get("status", "")) == status
	return false
