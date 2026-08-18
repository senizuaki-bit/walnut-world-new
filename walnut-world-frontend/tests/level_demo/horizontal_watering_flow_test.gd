extends SceneTree

const LEVEL_PATH := "res://scenes/level_demo/horizontal_watering_demo.tscn"


func _initialize() -> void:
	var failures: Array[String] = []
	var submitted: Array[String] = []
	var hinted: Array[String] = []
	var changed: Array[String] = []
	var level := (load(LEVEL_PATH) as PackedScene).instantiate()
	level.demo_timing_scale = 0.0
	root.add_child(level)
	await process_frame
	level.submit_requested.connect(func(source: String) -> void: submitted.append(source))
	level.hint_requested.connect(func(message: String) -> void: hinted.append(message))
	level.draft_changed.connect(func(source: String) -> void: changed.append(source))
	level.configure_agent_mode(true)
	var submit := level.get_node("Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/Actions/SubmitButton") as Button
	var hint := level.get_node("Hud/SafeArea/ToolRail/HintButton") as Button
	if not submit.disabled or not hint.disabled:
		failures.append("权威 Draft 恢复前运行与提示必须禁用。")
	level.load_agent_content({"task": {
		"name": "合同标题", "goal": "合同目标", "instructions": ["第一步", "第二步"],
		"story": {"opening": "合同开场", "success": "合同收束"},
	}})
	if (level.get_node("Hud/SafeArea/TaskCard/Margin/Content/TaskTitle") as Label).text != "合同标题":
		failures.append("任务标题必须绑定权威 Content。")
	var status_body := level.get_node("Hud/AgentStatusPanel/Margin/Content/Body") as Label
	if status_body.text != "第一步\n第二步\n合同开场":
		failures.append("task.instructions 必须校验为字符串数组后逐行展示，不得显示数组括号：%s" % status_body.text)
	level.load_agent_draft("authoritative source")
	if submit.disabled or hint.disabled:
		failures.append("权威 Draft 恢复后运行与提示必须解锁。")
	var editor := level.get_node("Hud/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeEditor") as CodeEdit
	editor.text = "student edited full source"
	await process_frame
	level.call("_emit_draft_change")
	submit.pressed.emit()
	hint.pressed.emit()
	var expected := "student edited full source"
	if changed != [expected] or submitted != [expected] or hinted != [expected]:
		failures.append("完整源码编辑器意图不一致：changed=%s submitted=%s hinted=%s" % [changed, submitted, hinted])
	var interactions: Array[Dictionary] = [{
		"role": "teaching_agent", "response_type": "hint", "hint_level": 2,
		"question": null,
		"feedback": {"message": "合同反馈"},
	}]
	level.present_agent_interactions(interactions)
	if status_body.text != "合同反馈":
		failures.append("AgentInteraction 必须显示合同 message。")
	var question_interactions: Array[Dictionary] = [{
		"role": "teaching_agent", "response_type": "question", "question": "你会从哪块土地开始？",
		"hint_level": null, "feedback": {"message": "请先想一想。"},
	}]
	level.present_agent_interactions(question_interactions)
	if status_body.text != "请先想一想。":
		failures.append("question 交互必须校验结构并原样展示 feedback.message。")
	var invalid_question: Array[Dictionary] = [{
		"role": "teaching_agent", "response_type": "question", "question": "",
		"hint_level": null, "feedback": {"message": "不得展示"},
	}]
	level.present_agent_interactions(invalid_question)
	if status_body.text != "请先想一想。":
		failures.append("question 为空时必须拒绝展示。")
	var growth_interactions: Array[Dictionary] = [{
		"role": "teaching_agent", "response_type": "growth_summary", "question": null,
		"hint_level": null, "feedback": {"message": "成长总结"},
	}]
	level.present_agent_interactions(growth_interactions)
	if status_body.text != "成长总结":
		failures.append("growth_summary 必须接受 null hint_level 并原样展示。")
	var invalid_interactions: Array[Dictionary] = [{"role": "teaching_agent", "message": "旧结构", "hint_level": 2}]
	level.present_agent_interactions(invalid_interactions)
	if status_body.text != "成长总结":
		failures.append("场景不得从非合同 feedback 结构补 Agent 回复。")
	level.begin_agent_submission("权威构建中")
	if level.get_phase_name() != "SUBMITTING" or status_body.text != "权威构建中":
		failures.append("提交阶段必须展示权威 Run + Evidence 状态。")
	level.complete_agent_submission("权威结果")
	if level.get_phase_name() != "COMPLETED" or status_body.text != "权威结果":
		failures.append("完成状态必须来自权威结果。")
	var completion_title := level.get_node("Hud/SafeArea/CompletionCard/Margin/Content/Title") as Label
	var completion_summary := level.get_node("Hud/SafeArea/CompletionCard/Margin/Content/SkillSaved") as Label
	if completion_title.text != "合同收束" or completion_summary.text != "权威结果":
		failures.append("完成卡必须绑定 task.story.success 与权威 objective summary。")
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("HORIZONTAL_WATERING_FLOW_TEST_PASS: Content、Draft、AgentInteraction 与 Run/Evidence 合同通过")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
