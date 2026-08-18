extends PanelContainer

@onready var result_text: RichTextLabel = $ResultText


func show_summary(summary: String) -> void:
	result_text.text = "[b]运行结果[/b]\n%s" % _escape(summary)


func show_error(message: String) -> void:
	result_text.text = "[color=#b44][b]错误[/b][/color]\n%s" % _escape(message)


func show_build(build: Dictionary) -> void:
	result_text.text = format_build(build)


func show_run(run: Dictionary) -> void:
	result_text.text = format_run(run)


static func format_build(build: Dictionary) -> String:
	var lines: Array[String] = ["[b]构建结果 · %s[/b]" % _escape(str(build.get("status", "UNKNOWN")))]
	var artifact: Variant = build.get("artifact")
	if artifact is Dictionary:
		lines.append("编译配置：%s · 课程测试：%s" % [_escape(str(artifact.get("compiler_profile", "未提供"))), _escape(str(artifact.get("test_suite_version", "未提供")))])
	lines.append("")
	for phase_value: Variant in build.get("phases", []):
		if not phase_value is Dictionary:
			continue
		var phase: Dictionary = phase_value
		var phase_name := _phase_label(str(phase.get("name", "UNKNOWN")))
		var phase_status := _escape(str(phase.get("status", "UNKNOWN")))
		var diagnostics: Variant = phase.get("diagnostic_codes", [])
		var suffix := ""
		if diagnostics is Array and not diagnostics.is_empty():
			suffix = "（%s）" % _escape(", ".join(PackedStringArray(diagnostics)))
		lines.append("- %s：%s%s" % [phase_name, phase_status, suffix])
	var failure: Variant = build.get("failure")
	if failure is Dictionary:
		lines.append("失败详情：%s" % _escape(str(failure.get("message", failure.get("code", "未提供")))))
	_append_evidence(lines, build.get("evidence_refs", []))
	return "\n".join(lines)


static func format_run(run: Dictionary) -> String:
	var lines: Array[String] = ["[b]运行结果 · %s[/b]" % _escape(str(run.get("status", "UNKNOWN")))]
	var sandbox: Variant = run.get("sandbox")
	if sandbox is Dictionary:
		lines.append("Sandbox：%s" % _escape(str(sandbox.get("status", "UNKNOWN"))))
		var usage: Variant = sandbox.get("usage")
		if usage is Dictionary:
			lines.append("资源：CPU %s ms · Wall %s ms · 峰值内存 %s B" % [str(usage.get("cpu_ms", "?")), str(usage.get("wall_ms", "?")), str(usage.get("peak_memory_bytes", "?"))])
	var world_application: Variant = run.get("world_application")
	if world_application is Dictionary:
		lines.append("世界提交：%s" % _escape(str(world_application.get("status", "UNKNOWN"))))
		var receipt: Variant = world_application.get("receipt")
		if receipt is Dictionary:
			lines.append("世界版本：%s → %s · 事件 %s—%s" % [str(receipt.get("previous_revision", "?")), str(receipt.get("world_revision", "?")), str(receipt.get("first_event_sequence", "?")), str(receipt.get("last_event_sequence", "?"))])
	var feedback: Variant = run.get("agent_feedback")
	if feedback is Dictionary:
		var source := _escape(str(feedback.get("source", "unknown")))
		var message := _escape(str(feedback.get("message", "")))
		lines.append("教学反馈（%s）：%s" % [source, message])
		if bool(feedback.get("degraded", false)):
			lines.append("[color=#8a5b00]已降级：%s[/color]" % _escape(str(feedback.get("fallback_reason", "原因未提供"))))
	_append_evidence(lines, run.get("evidence_refs", []))
	return "\n".join(lines)


static func _phase_label(name: String) -> String:
	return {
		"VALIDATE_SOURCE": "源码校验",
		"COMPILE": "编译",
		"PUBLIC_TEST": "课程测试",
		"HIDDEN_TEST": "隐藏测试",
		"CERTIFY": "认证",
	}.get(name, name)


static func _append_evidence(lines: Array[String], references: Variant) -> void:
	if not references is Array or references.is_empty():
		return
	lines.append("Evidence：")
	for reference_value: Variant in references:
		if reference_value is Dictionary:
			lines.append("- %s（%s）" % [_escape(str(reference_value.get("evidence_id", ""))), _escape(str(reference_value.get("evidence_type", "")))])


static func _escape(value: String) -> String:
	return value.replace("[", "[lb]")
