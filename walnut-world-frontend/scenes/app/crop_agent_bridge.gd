class_name CropAgentBridge
extends Node

## Adapts the story-first crop level to the existing formal client authority.
## The level emits UI intent only; this bridge sequences the public
## Draft -> Build -> Activation -> Agent Turn controller operations.

signal build_action_finished(result: Dictionary)
signal activation_action_finished(result: Dictionary)
signal submit_action_finished(result: Dictionary)

const WaterCandidateEvaluatorScript := preload("res://scripts/client/water_candidate_evaluator.gd")

var _store: Node
var _session: Node
var _level: CropAdaptiveWateringDemo
var _submission_running := false
var _hint_running := false
var _build_running := false
var _activation_running := false
var _last_error_message := ""
var _last_error_code := ""
var _projection_active := false
var _pending_interactions: Array[Dictionary] = []
var _pending_submission_interactions: Array[Dictionary] = []
var _candidate_config: Dictionary = {"enabled": false, "content_ref": {}, "plot_rules": {}}
var _pre_run_snapshot: Dictionary = {}
var _last_run: Dictionary = {}
var _local_candidate_result: Dictionary = {}
var _certified_source := ""
var _active_source := ""


func configure(
	store: Node,
	session: Node,
	level: CropAdaptiveWateringDemo,
	candidate_config: Dictionary = {},
) -> void:
	_disconnect_dependencies()
	_store = store
	_session = session
	_level = level
	_projection_active = false
	_pending_interactions.clear()
	_pending_submission_interactions.clear()
	_pre_run_snapshot.clear()
	_last_run.clear()
	_local_candidate_result.clear()
	_certified_source = ""
	_active_source = ""
	_candidate_config = (
		candidate_config.duplicate(true)
		if not candidate_config.is_empty()
		else {"enabled": false, "content_ref": {}, "plot_rules": {}}
	)
	if _store == null or _session == null or _level == null:
		push_error("CropAgentBridge requires store, session and level dependencies.")
		return
	_level.configure_agent_mode(true)
	_level.configure_candidate_compatibility_available(bool(_candidate_config.get("enabled", false)))
	_level.agent_submit_requested.connect(_on_submit_requested)
	_level.agent_build_requested.connect(_on_build_requested)
	_level.agent_activation_requested.connect(_on_activation_requested)
	_level.agent_hint_requested.connect(_on_hint_requested)
	_level.agent_draft_changed.connect(_on_agent_draft_changed)
	_store.connect("draft_changed", _on_draft_changed)
	_store.connect("error_reported", _on_error_reported)
	_session.connect("capability_unavailable", _on_capability_unavailable)
	_session.connect("interactions_recovered", _on_interactions_recovered)
	_session.connect("run_resolved", _on_run_resolved)


func activate_initial_projection() -> Dictionary:
	if _projection_active:
		return {"ok": true, "already_active": true}
	if not is_instance_valid(_store) or not is_instance_valid(_session) or not is_instance_valid(_level):
		return _projection_failure("作物适配关卡缺少首次投影依赖。")
	var content: Variant = _store.get("content")
	var world_snapshot: Variant = _store.get("world_snapshot")
	var source: Variant = _store.get("local_source")
	var draft_state: Variant = _store.get("draft_state")
	if not content is Dictionary or content.is_empty():
		return _projection_failure("作物适配关卡缺少权威内容，已拒绝展示缓存。")
	if not world_snapshot is Dictionary or world_snapshot.is_empty():
		return _projection_failure("作物适配关卡缺少权威世界快照，已拒绝展示缓存。")
	if typeof(source) != TYPE_STRING or source.is_empty():
		return _projection_failure("作物适配关卡缺少权威草稿，已拒绝展示缓存。")
	if typeof(draft_state) != TYPE_INT:
		return _projection_failure("作物适配关卡草稿状态无效，已拒绝展示缓存。")
	var authority_projection: Variant = _level.load_authoritative_projection(content, world_snapshot)
	if not authority_projection is Dictionary or not bool(authority_projection.get("ok", false)):
		return _projection_failure(
			str(authority_projection.get("message", "作物适配关卡无法投影权威 Content/Snapshot。"))
			if authority_projection is Dictionary
			else "作物适配关卡无法投影权威 Content/Snapshot。"
		)
	_level.load_agent_draft(source)
	_level.update_agent_draft_state(draft_state)
	_projection_active = true
	if not _pending_interactions.is_empty():
		var visible := _candidate_hints_only(_pending_interactions) if _candidate_mode_enabled() else _pending_interactions
		_level.present_agent_interactions(visible)
		_pending_interactions.clear()
	return {"ok": true}


func reject_initial_projection(message: String) -> Dictionary:
	_projection_active = false
	_submission_running = false
	_hint_running = false
	_build_running = false
	_activation_running = false
	_pending_interactions.clear()
	if is_instance_valid(_level):
		_level.present_agent_error(message)
	return {"ok": false, "message": message}


func _projection_failure(message: String) -> Dictionary:
	reject_initial_projection(message)
	return {"ok": false, "message": message}


func _on_agent_draft_changed(source: String) -> void:
	if source != _certified_source:
		_certified_source = ""
		_active_source = ""
	if _projection_active and _store != null and _store.has_method("mark_draft_dirty"):
		_store.call("mark_draft_dirty", source)


func _on_draft_changed(source: String, state: int) -> void:
	if not _projection_active or _level == null:
		return
	_level.load_agent_draft(source)
	_level.update_agent_draft_state(state)


func _on_submit_requested(source: String) -> void:
	if not _projection_active or _submission_running or _build_running or _activation_running or _level == null:
		return
	_submission_running = true
	_last_error_message = ""
	_last_error_code = ""
	_pre_run_snapshot.clear()
	_last_run.clear()
	_local_candidate_result.clear()
	_pending_submission_interactions.clear()
	_level.begin_agent_submission("正在保存代码并准备直接运行……")
	if str(_store.get("local_source")) != source:
		_store.call("mark_draft_dirty", source)
	if _certified_source != source:
		await _session.call("request_build")
		if not is_instance_valid(self):
			return
		var build_ok := int(_store.get("flow_state")) == WalnutClientStore.FlowState.CERTIFIED
		build_action_finished.emit(_stage_result(
			build_ok,
			"BUILD",
			source,
			"正式 Build/Certification 已闭环。" if build_ok else "代码检查未通过，请查看提示后重试。",
		))
		if not build_ok:
			_finish_failure("代码检查", "代码检查未通过，请查看提示后重试。")
			submit_action_finished.emit(_stage_result(false, "SUBMIT", source, "Build/Certification did not close."))
			return
		_certified_source = source
		_active_source = ""
	var activation_requested := _active_source != source
	if activation_requested:
		_level.update_agent_submission_stage("代码检查通过，正在准备可运行版本……")
		await _session.call("request_activation")
		if not is_instance_valid(self):
			return
	var active: Variant = _store.get("active_skill_tuple")
	var activation_ok: bool = not (
		(activation_requested and int(_store.get("flow_state")) != WalnutClientStore.FlowState.ACTIVE)
		or not active is Dictionary
		or active.is_empty()
	)
	if activation_requested:
		activation_action_finished.emit(_stage_result(
			activation_ok,
			"ACTIVATION",
			source,
			"正式 SkillActivation 已发布。" if activation_ok else "可运行版本未准备完成，请查看提示后重试。",
		))
	if not activation_ok:
		_finish_failure("运行准备", "可运行版本未准备完成，请查看提示后重试。")
		submit_action_finished.emit(_stage_result(false, "SUBMIT", source, "SkillActivation did not close."))
		return
	_active_source = source
	_level.update_agent_submission_stage("准备完成，正在交给小核桃运行……")
	_pre_run_snapshot = _store.get("world_snapshot").duplicate(true)
	_last_run.clear()
	var result: Variant = await _session.call("request_submit_and_run")
	if not is_instance_valid(self):
		return
	if not result is Dictionary or not bool(result.get("ok", false)):
		var controller_message := str(result.get("message", "Agent Turn 未能闭环。")) if result is Dictionary else "Agent Turn 未能闭环。"
		var diagnostic_message := _last_error_message if not _last_error_message.is_empty() else controller_message
		var diagnostic_code := _last_error_code if not _last_error_code.is_empty() else "RUN_CLOSURE_FAILED"
		_finish_failure("验证", controller_message)
		submit_action_finished.emit(_stage_result(false, "SUBMIT", source, diagnostic_message, diagnostic_code))
		return
	_refresh_level_authority_projection()
	var objective: Variant = _store.get("objective_result")
	var objective_result: Dictionary = objective if objective is Dictionary else {}
	if objective_result.get("objective_succeeded") == false:
		if _candidate_mode_enabled():
			var evaluation := WaterCandidateEvaluatorScript.evaluate(
				_candidate_config,
				_current_content_ref(),
				_pre_run_snapshot,
				_store.get("world_snapshot"),
				_last_run,
				_store.get("active_skill_tuple"),
				objective_result,
			)
			if bool(evaluation.get("ok", false)):
				_local_candidate_result = evaluation.duplicate(true)
				var presentation: Dictionary = await _level.present_candidate_evaluation(evaluation)
				if not is_instance_valid(self):
					return
				_submission_running = false
				if not bool(presentation.get("ok", false)):
					_level.present_candidate_chain_error(str(presentation.get("code", "CANDIDATE_PRESENTATION_FAILED")))
				submit_action_finished.emit(_submit_result(result, source, false))
				return
			if bool(evaluation.get("eligible", false)):
				_submission_running = false
				_level.present_candidate_chain_error(str(evaluation.get("code", "CANDIDATE_EVALUATION_FAILED")))
				submit_action_finished.emit(_submit_result(result, source, false))
				return
		_finish_failure("验证", str(objective_result.get("summary", "权威验证未通过。")))
		submit_action_finished.emit(_submit_result(result, source, false))
		return
	_submission_running = false
	if not _pending_submission_interactions.is_empty():
		_level.present_agent_interactions(_pending_submission_interactions)
		_pending_submission_interactions.clear()
	_level.complete_agent_submission(str(objective_result.get("summary", "权威 Run 已完成。")))
	submit_action_finished.emit(_submit_result(result, source, true))


func _on_build_requested(source: String) -> void:
	if not _projection_active or _submission_running or _build_running or _activation_running or _level == null:
		return
	_build_running = true
	_last_error_message = ""
	_level.begin_agent_build()
	_store.call("mark_draft_dirty", source)
	await _session.call("request_build")
	if not is_instance_valid(self):
		return
	_build_running = false
	var build_ok := int(_store.get("flow_state")) == WalnutClientStore.FlowState.CERTIFIED
	build_action_finished.emit(_stage_result(
		build_ok,
		"BUILD",
		source,
		"正式 Build/Certification 已闭环。" if build_ok else "正式构建未产生 CERTIFIED 结果。",
	))
	if not build_ok:
		_level.fail_agent_submission("构建", _last_error_message if not _last_error_message.is_empty() else "正式构建未产生 CERTIFIED 结果。")
		return
	_certified_source = source
	_active_source = ""
	_level.complete_agent_build()


func _on_activation_requested() -> void:
	if not _projection_active or _submission_running or _build_running or _activation_running or _level == null:
		return
	_activation_running = true
	_last_error_message = ""
	_level.begin_agent_activation()
	await _session.call("request_activation")
	if not is_instance_valid(self):
		return
	_activation_running = false
	var active: Variant = _store.get("active_skill_tuple")
	var activation_ok: bool = int(_store.get("flow_state")) == WalnutClientStore.FlowState.ACTIVE and active is Dictionary and not active.is_empty()
	activation_action_finished.emit(_stage_result(
		activation_ok,
		"ACTIVATION",
		_certified_source,
		"正式 SkillActivation 已发布。" if activation_ok else "正式激活未发布精确 Skill tuple。",
	))
	if not activation_ok:
		_level.fail_agent_submission("激活", _last_error_message if not _last_error_message.is_empty() else "正式激活未发布精确 Skill tuple。")
		return
	_active_source = _certified_source
	_level.complete_agent_activation()


func _on_hint_requested(message: String) -> void:
	if not _projection_active or _hint_running or _submission_running or _level == null:
		return
	_hint_running = true
	_level.update_agent_submission_stage("正在向叮当师傅请求正式提示……", false)
	await _session.call("request_hint", _candidate_hint_message(message))
	if not is_instance_valid(self):
		return
	_hint_running = false


func _on_interactions_recovered(interactions: Array[Dictionary]) -> void:
	if not _projection_active:
		_pending_interactions = interactions.duplicate(true)
	elif _level == null:
		return
	elif _hint_running:
		_level.present_agent_interactions(_teaching_responses_only(interactions))
	elif _submission_running:
		_pending_submission_interactions = interactions.duplicate(true)
	elif _candidate_mode_enabled():
		_level.present_agent_interactions(_candidate_hints_only(interactions))
	else:
		_level.present_agent_interactions(interactions)


func _on_run_resolved(run: Dictionary) -> void:
	if _submission_running:
		_last_run = run.duplicate(true)


func _candidate_mode_enabled() -> bool:
	return bool(_candidate_config.get("enabled", false))


func _current_content_ref() -> Dictionary:
	var content: Variant = _store.get("content") if is_instance_valid(_store) else {}
	if not content is Dictionary:
		return {}
	var nested: Variant = content.get("content_ref")
	return nested.duplicate(true) if nested is Dictionary else content.duplicate(true)


func _candidate_hints_only(interactions: Array[Dictionary]) -> Array[Dictionary]:
	var visible: Array[Dictionary] = []
	for interaction: Dictionary in interactions:
		if str(interaction.get("response_type", "")) == "hint":
			visible.append(interaction.duplicate(true))
	return visible


func _teaching_responses_only(interactions: Array[Dictionary]) -> Array[Dictionary]:
	# 叮当对一次提示请求可以回问题，也可以回分级提示，两者都是教学反馈。
	# 只留 "hint" 会把苏格拉底式提问整条丢掉，学生点了按钮却什么都看不到。
	var visible: Array[Dictionary] = []
	for interaction: Dictionary in interactions:
		if str(interaction.get("response_type", "")) in ["hint", "question"]:
			visible.append(interaction.duplicate(true))
	return visible


func _candidate_hint_message(fallback: String) -> String:
	if _local_candidate_result.is_empty():
		return fallback
	var action_lines: Array[String] = []
	for action: Dictionary in _local_candidate_result.get("actions", []):
		action_lines.append("%s:%dml" % [str(action.get("plot_id", "unknown")), int(action.get("amount_ml", 0))])
	var failed_lines: Array[String] = []
	for plot: Dictionary in _local_candidate_result.get("plot_results", []):
		if str(plot.get("status", "")) != "CORRECT":
			failed_lines.append("%s=%s" % [str(plot.get("plot_id", "unknown")), str(plot.get("status", "UNKNOWN"))])
	var provenance: Dictionary = _local_candidate_result.get("provenance", {})
	return (
		"你是教学提示助手，不是判题器。只给下一层提示，不要宣布成功或失败，也不要给完整代码。\n"
		+ "真实状态：Sandbox SUCCEEDED；WorldApplication REJECTED；原因 TASK_INCOMPLETE；世界未提交。\n"
		+ "Sandbox WATER：%s\n" % ", ".join(action_lines)
		+ "前端候选：%s\n" % str(_local_candidate_result.get("summary", ""))
		+ "失败地块：%s\n" % (", ".join(failed_lines) if not failed_lines.is_empty() else "无")
		+ "基础 Snapshot：revision=%d state_hash=%s。候选结果不是后端 objective。" % [
			int(provenance.get("base_snapshot_revision", -1)),
			str(provenance.get("base_snapshot_state_hash", "")),
		]
	)


func _on_error_reported(error: Dictionary) -> void:
	if _projection_active and _level != null:
		_last_error_code = str(error.get("code", ""))
		_last_error_message = str(error.get("message", "正式服务发生错误。"))
		_level.present_agent_error(_last_error_message)


func _on_capability_unavailable(_capability: String, message: String) -> void:
	if _projection_active and _level != null:
		_last_error_code = "CAPABILITY_UNAVAILABLE"
		_last_error_message = message
		_level.present_agent_error(message)


func _finish_failure(stage: String, message: String) -> void:
	_submission_running = false
	_refresh_level_authority_projection()
	_level.fail_agent_submission(stage, _last_error_message if not _last_error_message.is_empty() else message)
	if not _pending_submission_interactions.is_empty():
		_level.present_agent_interactions(_pending_submission_interactions)
		_pending_submission_interactions.clear()


func _refresh_level_authority_projection() -> Dictionary:
	if not is_instance_valid(_store) or not is_instance_valid(_level):
		return {"ok": false, "message": "Authority projection dependencies are unavailable."}
	var content: Variant = _store.get("content")
	var snapshot: Variant = _store.get("world_snapshot")
	if not content is Dictionary or not snapshot is Dictionary:
		return {"ok": false, "message": "Authority projection values are invalid."}
	return _level.load_authoritative_projection(content, snapshot)


func _stage_result(ok: bool, stage: String, source: String, message: String, code := "") -> Dictionary:
	var result := {
		"ok": ok,
		"stage": stage,
		"message": message,
		"source_sha256": source.sha256_text(),
	}
	if not code.is_empty():
		result["code"] = code
	return result


func _submit_result(controller_result: Dictionary, source: String, objective_succeeded: bool) -> Dictionary:
	var result := controller_result.duplicate(true)
	result["ok"] = bool(controller_result.get("ok", false))
	result["stage"] = "SUBMIT"
	result["source_sha256"] = source.sha256_text()
	result["objective_succeeded"] = objective_succeeded
	return result


func _exit_tree() -> void:
	_disconnect_dependencies()


func _disconnect_dependencies() -> void:
	if is_instance_valid(_level):
		if _level.agent_submit_requested.is_connected(_on_submit_requested):
			_level.agent_submit_requested.disconnect(_on_submit_requested)
		if _level.agent_build_requested.is_connected(_on_build_requested):
			_level.agent_build_requested.disconnect(_on_build_requested)
		if _level.agent_activation_requested.is_connected(_on_activation_requested):
			_level.agent_activation_requested.disconnect(_on_activation_requested)
		if _level.agent_hint_requested.is_connected(_on_hint_requested):
			_level.agent_hint_requested.disconnect(_on_hint_requested)
		if _level.agent_draft_changed.is_connected(_on_agent_draft_changed):
			_level.agent_draft_changed.disconnect(_on_agent_draft_changed)
	if is_instance_valid(_store):
		if _store.is_connected("draft_changed", _on_draft_changed):
			_store.disconnect("draft_changed", _on_draft_changed)
		if _store.is_connected("error_reported", _on_error_reported):
			_store.disconnect("error_reported", _on_error_reported)
	if is_instance_valid(_session):
		if _session.is_connected("capability_unavailable", _on_capability_unavailable):
			_session.disconnect("capability_unavailable", _on_capability_unavailable)
		if _session.is_connected("interactions_recovered", _on_interactions_recovered):
			_session.disconnect("interactions_recovered", _on_interactions_recovered)
		if _session.is_connected("run_resolved", _on_run_resolved):
			_session.disconnect("run_resolved", _on_run_resolved)
