extends SceneTree

const DialoguePanelScene := preload("res://scenes/task/dialogue_panel.tscn")


func _initialize() -> void:
	var panel := DialoguePanelScene.instantiate() as PanelContainer
	root.add_child(panel)
	await process_frame
	panel.show_interaction("教学角色", "循环停止得太早了。", "循环什么时候应该停止？", "hint", 2)
	var speaker := panel.get_node_or_null("Margin/Content/Speaker") as Label
	var badge := panel.get_node_or_null("Margin/Content/ResponseType") as Label
	var question := panel.get_node_or_null("Margin/Content/Question") as Label
	var message := panel.get_node_or_null("Margin/Content/DialogueText") as Label
	if speaker == null or badge == null or question == null or message == null or speaker.text != "教学角色" or badge.text != "概念提示" or not question.visible or question.text != "想一想：循环什么时候应该停止？" or message.text != "循环停止得太早了。":
		push_error("DialoguePanel must render the verified role, feedback, question, response type, and hint level.")
		quit(1)
		return
	panel.show_interaction("书书", "你完成了任务。", "", "growth_summary", 0)
	if badge.text != "成长总结" or question.visible:
		push_error("DialoguePanel must hide an absent question and label the verified response type.")
		quit(1)
		return
	print("DIALOGUE_PANEL_TEST_PASS")
	quit(0)
