extends SceneTree

const PRESENTER_SCENE := preload("res://scenes/ui/agent_interaction_presenter.tscn")


func _initialize() -> void:
	var presenter := PRESENTER_SCENE.instantiate()
	root.add_child(presenter)
	await process_frame
	var bug := _interaction("interaction_bug_0001", "bug_agent", "message", "边界问题出现了。", null, null)
	if not presenter.enqueue_interaction(bug):
		push_error("A valid Bug AgentInteraction must enter the presentation queue.")
		quit(1)
		return
	await process_frame
	var overlay := presenter.get_node("StoryDialogueOverlay") as StoryDialogueOverlay
	if (
		not overlay.visible
		or overlay.speaker_label.text != "Bug 先生"
		or overlay.portrait.texture == null
		or not overlay.portrait.texture.resource_path.ends_with("pest_bug.png")
		or overlay.body_label.text != "边界问题出现了。"
		or overlay.response_badge.text != "任务说明"
	):
		push_error("Bug AgentInteraction must resolve to the Bug 先生 portrait presentation.")
		quit(1)
		return
	var teaching := _interaction("interaction_teaching_0001", "teaching_agent", "hint", "先观察循环条件。", "循环何时停止？", 2)
	if not presenter.enqueue_interaction(teaching) or presenter.pending_count() != 1:
		push_error("A second AgentInteraction must wait while the current presentation is active.")
		quit(1)
		return
	if presenter.enqueue_interaction(bug):
		push_error("An already presented interaction_id must not be queued twice.")
		quit(1)
		return
	overlay.skip_sequence()
	await process_frame
	if (
		overlay.speaker_label.text != "叮当师傅"
		or not overlay.portrait.texture.resource_path.ends_with("master_ding_dang.png")
		or overlay.response_badge.text != "概念提示"
		or not overlay.question_label.visible
		or not overlay.question_label.text.contains("循环何时停止")
	):
		push_error("Queued teaching feedback must preserve role, portrait, hint level, and question.")
		quit(1)
		return
	var patch := _interaction("interaction_patch_0001", "teaching_agent", "skill_patch", "查看修改建议。", null, 4)
	if presenter.enqueue_interaction(patch):
		push_error("Skill Patch must remain in its dedicated confirmation dialog instead of the story overlay.")
		quit(1)
		return
	print("AGENT_INTERACTION_PRESENTER_TEST_PASS")
	quit(0)


func _interaction(
	interaction_id: String,
	role: String,
	response_type: String,
	message: String,
	question: Variant,
	hint_level: Variant,
) -> Dictionary:
	return {
		"interaction_id": interaction_id,
		"role": role,
		"response_type": response_type,
		"question": question,
		"hint_level": hint_level,
		"feedback": {"message": message, "degraded": false, "evidence_refs": []},
	}
