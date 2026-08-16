extends PanelContainer

func show_message(role: String, message: String) -> void:
	show_interaction(role, message, "", "message", 0)


func show_interaction(role: String, message: String, question: String, response_type: String, hint_level: int) -> void:
	%Speaker.text = role
	%ResponseType.text = _response_type_label(response_type, hint_level)
	%DialogueText.text = message
	%Question.visible = not question.is_empty()
	%Question.text = "" if question.is_empty() else "想一想：%s" % question


func _response_type_label(response_type: String, hint_level: int) -> String:
	match response_type:
		"question": return "追问"
		"hint":
			return {0: "观察", 1: "方向提示", 2: "概念提示", 3: "修改建议", 4: "AI 协助修改"}.get(clampi(hint_level, 0, 4), "提示")
		"skill_patch": return "AI 协助修改"
		"growth_summary": return "成长总结"
		_: return "任务说明"
