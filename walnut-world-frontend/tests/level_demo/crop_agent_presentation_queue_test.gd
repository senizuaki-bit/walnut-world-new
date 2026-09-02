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
