extends SceneTree

const LEVEL_SCENE := preload("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")


func _initialize() -> void:
	var level := LEVEL_SCENE.instantiate() as CropAdaptiveWateringDemo
	level.timing_scale = 0.05
	root.add_child(level)
	await process_frame
	var overlay := level.get_node("StoryDialogueOverlay") as StoryDialogueOverlay
	var presenter := level.get_node("AgentInteractionPresenter") as AgentInteractionPresenter
	var legion := level.get_node("BugLegion2D") as Control
	overlay.skip_sequence()
	await process_frame
	overlay.play_sequence("芽芽", null, ["关卡叙事必须先完整播完。"])
	var teaching := _interaction(
		"interaction_teaching_fifo_0001",
		"teaching_agent",
		"hint",
		"先检查同一个下标是否读取了两张表。",
		2,
	)
	var bug := _interaction(
		"interaction_bug_fifo_0002",
		"bug_agent",
		"message",
		"第三次同类失败已经由后端权威确认。",
		null,
	)
	level.present_agent_interactions([teaching, bug])
	level.present_agent_interactions([teaching, bug])
	await process_frame
	if (
		presenter.is_presenting()
		or presenter.pending_count() != 2
		or overlay.speaker_label.text != "芽芽"
		or legion.visible
	):
		_abort("批量 Interaction 必须去重并等待现有叙事，不得提前显示军团。")
		return
	overlay.skip_sequence()
	await process_frame
	await process_frame
	if (
		str(presenter.active_interaction().get("interaction_id", "")) != str(teaching.interaction_id)
		or str(level.get("_last_agent_interaction").get("interaction_id", "")) != str(teaching.interaction_id)
		or legion.visible
	):
		_abort("FIFO 第一项必须由叮当师傅先展示，且不能触发 Bug 军团。")
		return
	overlay.skip_sequence()
	await process_frame
	if (
		str(presenter.active_interaction().get("interaction_id", "")) != str(bug.interaction_id)
		or str(level.get("_last_agent_interaction").get("interaction_id", "")) != str(bug.interaction_id)
		or not legion.visible
	):
		_abort("FIFO 第二项 Bug 先生开始展示时必须同步显示 2D 军团。")
		return
	overlay.skip_sequence()
	await create_timer(0.22).timeout
	if presenter.is_presenting() or presenter.pending_count() != 0 or legion.visible:
		_abort("队列结束后必须关闭 Bug 军团且不残留待展示 Interaction。")
		return
	var stale := _interaction(
		"interaction_restart_scoped_0003",
		"bug_agent",
		"message",
		"这条反馈不应跨重开残留。",
		null,
	)
	stale["session_id"] = "session_restart_0001"
	level.present_agent_interactions([stale])
	await process_frame
	level.update_agent_submission_stage("临时阶段文案")
	level.set("_last_chain_error_detail", "RAW_INTERNAL_DETAIL")
	level.set("_candidate_playing", true)
	level.set("_candidate_skip_requested", true)
	level.restart_level()
	await process_frame
	if (
		presenter.is_presenting()
		or presenter.pending_count() != 0
		or legion.visible
		or not level.get("_last_agent_interaction").is_empty()
		or bool(level.get("_agent_stage_message_visible"))
		or not str(level.get("_last_chain_error_detail")).is_empty()
		or bool(level.get("_candidate_playing"))
		or bool(level.get("_candidate_skip_requested"))
	):
		_abort("重开必须清除旧 Interaction、军团、临时文案与候选演示状态。")
		return
	level.present_agent_interactions([stale])
	if presenter.pending_count() != 0:
		_abort("同一 Session 重开后不得重复播放已经展示过的 interaction_id。")
		return
	var next_session := stale.duplicate(true)
	next_session["session_id"] = "session_restart_0002"
	level.present_agent_interactions([next_session])
	if presenter.pending_count() != 1 or presenter.presentation_session_id() != "session_restart_0002":
		_abort("新 Session 不得继承上一局的 interaction_id 去重账本。")
		return
	presenter.clear_queue()
	overlay.skip_sequence()
	level.configure_agent_mode(true)
	level.set("_same_failure_count", 2)
	level.set("_build_result", {
		"failure_key": "FIXED_TARGET_VALUE",
		"message": "LOCAL_FAILURE_MUST_NOT_SELECT_FORMAL_ROLE",
	})
	level.call("_fail_run")
	await process_frame
	if (
		int(level.get("_same_failure_count")) != 2
		or (level.get_node("BugChallengeOverlay") as Control).visible
		or legion.visible
		or (level.get_node("Hud/EvidencePanel/Margin/Content/EvidenceBody") as RichTextLabel).text.contains("LOCAL_FAILURE_MUST_NOT_SELECT_FORMAL_ROLE")
	):
		_abort("正式 Agent 模式不得用本地失败计数选择 Bug 角色或泄露内部原因。")
		return
	print("CROP_AGENT_PRESENTATION_QUEUE_TEST_PASS")
	quit(0)


func _interaction(
	interaction_id: String,
	role: String,
	response_type: String,
	message: String,
	hint_level: Variant,
) -> Dictionary:
	return {
		"interaction_id": interaction_id,
		"role": role,
		"response_type": response_type,
		"question": null,
		"hint_level": hint_level,
		"feedback": {
			"message": message,
			"degraded": false,
			"evidence_refs": [],
		},
	}


func _abort(message: String) -> void:
	push_error(message)
	quit(1)
