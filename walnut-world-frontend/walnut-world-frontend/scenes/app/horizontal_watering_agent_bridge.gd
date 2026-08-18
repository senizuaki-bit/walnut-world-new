class_name HorizontalWateringAgentBridge
extends Node

## Adapts HorizontalWateringDemo UI intent to the existing INT2 client
## authority.  This node never evaluates source, invents Agent feedback, or
## projects World mutations.  SessionController owns the durable
## Draft -> Build -> Activation -> Agent Turn closure; WorldEventPlayer is the
## only source of verified presentation events.

const HARVEST_EVENT_TYPE := "world.action.harvested"
const HARVEST_EVENT_VERSION := 1

var _store: Node
var _session: Node
var _level: HorizontalWateringDemo
var _world_event_player: Node
var _submission_running := false
var _hint_running := false
var _last_error_message := ""
var _configuration_generation := 0
var _projection_active := false
var _pending_interactions: Array[Dictionary] = []
var _last_projected_snapshot_fingerprint := ""


func configure(
	store: Node,
	session_controller: Node,
	level: HorizontalWateringDemo,
	world_event_player: Node,
) -> void:
	_disconnect_dependencies()
	_store = store
	_session = session_controller
	_level = level
	_world_event_player = world_event_player
	if _store == null or _session == null or _level == null or _world_event_player == null:
		push_error("HorizontalWateringAgentBridge requires Store, SessionController, level, and WorldEventPlayer dependencies.")
		return

	_connect_signal(_level, &"draft_changed", _on_draft_intent_changed)
	_connect_signal(_level, &"submit_requested", _on_submit_requested)
	_connect_signal(_level, &"hint_requested", _on_hint_requested)
	_connect_signal(_store, &"content_changed", _on_content_changed)
	_connect_signal(_store, &"draft_changed", _on_draft_changed)
	_connect_signal(_store, &"world_replaced", _on_world_replaced)
	_connect_signal(_store, &"error_reported", _on_error_reported)
	_connect_signal(_session, &"interactions_recovered", _on_interactions_recovered)
	_connect_signal(_session, &"capability_unavailable", _on_capability_unavailable)
	_connect_signal(_session, &"world_playback_state_changed", _on_world_playback_state_changed)
	_connect_signal(_world_event_player, &"playback_recovery_required", _on_playback_recovery_required)

	_call_level(&"configure_agent_mode", [true])


## AppRoot is the sole caller.  Configuration only wires signals and leaves the
## level in CONNECTING; projection is opened after every startup authority and
## persisted-operation recovery has completed successfully.
func activate_initial_projection() -> Dictionary:
	if _projection_active:
		return {"ok": true, "already_active": true}
	if (
		not is_instance_valid(_store)
		or not is_instance_valid(_session)
		or not is_instance_valid(_level)
		or not is_instance_valid(_world_event_player)
	):
		return _reject_initial_projection("启动权威依赖不可用，未展示缓存内容。")
	var content_value: Variant = _store.get("content")
	var snapshot_value: Variant = _store.get("world_snapshot")
	var source_value: Variant = _store.get("local_source")
	var draft_state_value: Variant = _store.get("draft_state")
	if not content_value is Dictionary or content_value.is_empty():
		return _reject_initial_projection("正式任务内容尚未完成权威恢复。")
	if not snapshot_value is Dictionary or snapshot_value.is_empty():
		return _reject_initial_projection("正式世界快照尚未完成权威恢复。")
	if (
		typeof(source_value) != TYPE_STRING
		or String(source_value).is_empty()
		or typeof(draft_state_value) != TYPE_INT
	):
		return _reject_initial_projection("正式草稿尚未完成权威恢复。")
	var content_projection: Variant = _level.call(
		"load_agent_content",
		(content_value as Dictionary).duplicate(true),
	) if _level.has_method("load_agent_content") else false
	if content_projection != true:
		return _reject_initial_projection("正式任务内容无法映射到预置演示关卡。")
	if not _replace_authoritative_world((snapshot_value as Dictionary).duplicate(true)):
		return _reject_initial_projection("正式世界快照无法映射到预置演示关卡。")
	_call_level(&"load_agent_draft", [str(source_value)])
	_call_level(&"update_agent_draft_state", [int(draft_state_value)])
	_projection_active = true
	if not _pending_interactions.is_empty():
		_call_level(&"present_agent_interactions", [_pending_interactions.duplicate(true)])
		_pending_interactions.clear()
	return {"ok": true}


func reject_initial_projection(message: String) -> Dictionary:
	return _reject_initial_projection(message)


func _reject_initial_projection(message: String) -> Dictionary:
	_projection_active = false
	_submission_running = false
	_hint_running = false
	_pending_interactions.clear()
	_call_level(&"present_agent_error", [message])
	return {
		"ok": false,
		"error_code": "HORIZONTAL_INITIAL_PROJECTION_REJECTED",
		"message": message,
	}


func _exit_tree() -> void:
	_disconnect_dependencies()


func _on_draft_intent_changed(source: String) -> void:
	if _projection_active and _store != null and _store.has_method("mark_draft_dirty"):
		_store.call("mark_draft_dirty", source)


func _on_content_changed(content: Dictionary) -> void:
	if not _projection_active:
		return
	_call_level(&"load_agent_content", [content.duplicate(true)])


func _on_draft_changed(source: String, state: int) -> void:
	if not _projection_active:
		return
	if state == WalnutClientStore.DraftState.CLEAN:
		_call_level(&"load_agent_draft", [source])
	_call_level(&"update_agent_draft_state", [state])


func _on_world_replaced(snapshot: Dictionary) -> void:
	if not _projection_active:
		return
	_replace_authoritative_world(snapshot.duplicate(true))


func _on_submit_requested(source: String) -> void:
	if not _projection_active or _submission_running or _level == null:
		return
	_run_submission(source)


func _run_submission(source: String) -> void:
	var generation := _configuration_generation
	_submission_running = true
	_last_error_message = ""
	_call_level(&"begin_agent_submission", ["正在保存草稿并请求正式构建……"])
	if _store == null or not _store.has_method("mark_draft_dirty"):
		_finish_failure("保存", "Draft 权威不可用，未发起 Build。")
		return
	_store.call("mark_draft_dirty", source)
	if _session == null or not _session.has_method("request_build"):
		_finish_failure("构建", "SessionController 未提供正式 Build 能力。")
		return
	await _session.call("request_build")
	if not _submission_context_is_current(generation):
		return
	if not _has_flow_state(WalnutClientStore.FlowState.CERTIFIED):
		_finish_failure("构建", "正式构建未产生 CERTIFIED 结果，请查看错误信息后重试。")
		return

	_call_level(&"update_agent_submission_stage", ["构建已认证，正在激活精确技能版本……"])
	if not _session.has_method("request_activation"):
		_finish_failure("激活", "SessionController 未提供正式 Activation 能力。")
		return
	await _session.call("request_activation")
	if not _submission_context_is_current(generation):
		return
	var active: Variant = _store.get("active_skill_tuple")
	if (
		not _has_flow_state(WalnutClientStore.FlowState.ACTIVE)
		or not active is Dictionary
		or active.is_empty()
	):
		_finish_failure("激活", "正式激活未发布可运行的精确技能版本，请查看错误信息后重试。")
		return

	_call_level(&"update_agent_submission_stage", ["技能已激活，正在交给小核桃进行 Agent Turn……"])
	if not _session.has_method("request_submit_and_run"):
		_finish_failure("验证", "SessionController 未提供正式 Agent Turn 能力。")
		return
	var result: Variant = await _session.call("request_submit_and_run")
	if not _submission_context_is_current(generation):
		return
	if not result is Dictionary or not bool(result.get("ok", false)):
		var message := (
			str(result.get("message", "Agent Turn 未能闭环。"))
			if result is Dictionary
			else "Agent Turn 未能闭环。"
		)
		_finish_failure(str(result.get("stage", "验证")) if result is Dictionary else "验证", message)
		return
	if not _has_flow_state(WalnutClientStore.FlowState.COMPLETED):
		_finish_failure("验证", "Agent Turn 尚未闭合 Run、Evidence、Snapshot 与 Interaction。")
		return
	var objective: Variant = _store.get("objective_result")
	if not objective is Dictionary or objective.is_empty():
		_finish_failure("验证", "权威闭环没有提供 Objective 结果。")
		return
	if objective.get("objective_succeeded") == false:
		_finish_failure("验证", str(objective.get("summary", "权威验证未通过。")))
		return
	_submission_running = false
	_call_level(&"complete_agent_submission", [str(objective.get("summary", result.get("message", "权威 Run 已完成。")))])


func _on_hint_requested(message: String) -> void:
	if not _projection_active or _hint_running or _submission_running or _level == null:
		return
	_request_hint(message)


func _request_hint(message: String) -> void:
	var generation := _configuration_generation
	_hint_running = true
	_call_level(&"update_agent_submission_stage", ["正在向叮当师傅请求正式提示……", false])
	if _session == null or not _session.has_method("request_hint"):
		_hint_running = false
		_on_capability_unavailable("Hint", "SessionController 未提供正式 Hint Turn 能力。")
		return
	await _session.call("request_hint", message)
	if not _submission_context_is_current(generation):
		return
	_hint_running = false


func _on_interactions_recovered(interactions: Array[Dictionary]) -> void:
	if not _projection_active:
		_pending_interactions = interactions.duplicate(true)
		return
	_call_level(&"present_agent_interactions", [interactions.duplicate(true)])


func _on_error_reported(error: Dictionary) -> void:
	_last_error_message = str(error.get("message", "正式服务发生错误。"))
	if not _projection_active:
		return
	_call_level(&"present_agent_error", [_last_error_message])


func _on_capability_unavailable(_capability: String, message: String) -> void:
	_last_error_message = message
	if not _projection_active:
		return
	_call_level(&"present_agent_error", [message])


func _on_world_playback_state_changed(state: String) -> void:
	if not _projection_active:
		return
	_call_level(&"present_world_playback_state", [state])


func begin_presentation_event(event: Dictionary, speed: float = 1.0) -> Dictionary:
	if not _projection_active or not _is_harvest_v1(event) or speed not in [1.0, 2.0]:
		return {"ok": false, "duration_seconds": 0.0}
	if _level == null or not is_instance_valid(_level) or not _level.has_method("present_verified_world_event"):
		return {"ok": false, "duration_seconds": 0.0}
	var result: Variant = _level.call("present_verified_world_event", event.duplicate(true), speed)
	if not result is Dictionary:
		return {"ok": false, "duration_seconds": 0.0}
	return result


func finish_presentation_event(event: Dictionary, skipped: bool = false) -> bool:
	return (
		_projection_active
		and _is_harvest_v1(event)
		and _level != null
		and is_instance_valid(_level)
		and _level.has_method("finish_verified_world_event")
		and bool(_level.call("finish_verified_world_event", event.duplicate(true), skipped))
	)


func project_replay_snapshot(snapshot: Dictionary) -> bool:
	return _projection_active and _replace_authoritative_world(snapshot.duplicate(true))


func last_authoritative_projection_succeeded(snapshot: Dictionary) -> bool:
	return (
		not _last_projected_snapshot_fingerprint.is_empty()
		and _last_projected_snapshot_fingerprint == _snapshot_fingerprint(snapshot)
	)


func _on_playback_recovery_required(after_sequence: int) -> void:
	if not _projection_active:
		return
	if not _call_level(&"require_world_playback_recovery", [after_sequence]):
		_call_level(&"present_agent_error", ["权威世界演出校验失败，正在从 Snapshot 恢复。"])


func _finish_failure(stage: String, message: String) -> void:
	_submission_running = false
	var visible_message := _last_error_message if not _last_error_message.is_empty() else message
	_call_level(&"fail_agent_submission", [stage, visible_message])


func _has_flow_state(expected: int) -> bool:
	return _store != null and int(_store.get("flow_state")) == expected


func _submission_context_is_current(generation: int) -> bool:
	return (
		generation == _configuration_generation
		and is_instance_valid(_store)
		and is_instance_valid(_session)
		and is_instance_valid(_level)
	)


func _is_harvest_v1(event: Dictionary) -> bool:
	return (
		str(event.get("event_type", "")) == HARVEST_EVENT_TYPE
		and typeof(event.get("event_version")) == TYPE_INT
		and int(event.get("event_version", -1)) == HARVEST_EVENT_VERSION
	)


func _call_level(method: StringName, arguments: Array = []) -> bool:
	if _level == null or not is_instance_valid(_level) or not _level.has_method(method):
		return false
	_level.callv(method, arguments)
	return true


func _replace_authoritative_world(snapshot: Dictionary) -> bool:
	_last_projected_snapshot_fingerprint = ""
	if _level == null or not is_instance_valid(_level) or not _level.has_method("replace_authoritative_world"):
		return false
	if not bool(_level.call("replace_authoritative_world", snapshot)):
		return false
	_last_projected_snapshot_fingerprint = _snapshot_fingerprint(snapshot)
	return true


func _snapshot_fingerprint(snapshot: Dictionary) -> String:
	return "%s:%s:%s:%s" % [
		str(snapshot.get("world_id", "")),
		int(snapshot.get("revision", -1)),
		int(snapshot.get("last_event_sequence", -1)),
		str(snapshot.get("state_hash", "")),
	]


func _connect_signal(source: Variant, signal_name: StringName, callable: Callable) -> void:
	if not is_instance_valid(source):
		return
	if source.has_signal(signal_name) and not source.is_connected(signal_name, callable):
		source.connect(signal_name, callable)


func _disconnect_signal(source: Variant, signal_name: StringName, callable: Callable) -> void:
	if not is_instance_valid(source):
		return
	if source.has_signal(signal_name) and source.is_connected(signal_name, callable):
		source.disconnect(signal_name, callable)


func _disconnect_dependencies() -> void:
	_configuration_generation += 1
	_disconnect_signal(_level, &"draft_changed", _on_draft_intent_changed)
	_disconnect_signal(_level, &"submit_requested", _on_submit_requested)
	_disconnect_signal(_level, &"hint_requested", _on_hint_requested)
	_disconnect_signal(_store, &"content_changed", _on_content_changed)
	_disconnect_signal(_store, &"draft_changed", _on_draft_changed)
	_disconnect_signal(_store, &"world_replaced", _on_world_replaced)
	_disconnect_signal(_store, &"error_reported", _on_error_reported)
	_disconnect_signal(_session, &"interactions_recovered", _on_interactions_recovered)
	_disconnect_signal(_session, &"capability_unavailable", _on_capability_unavailable)
	_disconnect_signal(_session, &"world_playback_state_changed", _on_world_playback_state_changed)
	_disconnect_signal(_world_event_player, &"playback_recovery_required", _on_playback_recovery_required)
	_store = null
	_session = null
	_level = null
	_world_event_player = null
	_submission_running = false
	_hint_running = false
	_projection_active = false
	_pending_interactions.clear()
	_last_projected_snapshot_fingerprint = ""
