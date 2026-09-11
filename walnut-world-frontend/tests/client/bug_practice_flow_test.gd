extends SceneTree

const PanelScene := preload("res://scenes/ui/bug_practice_panel.tscn")
const Gateway := preload("res://scripts/client/bug_practice_gateway.gd")
var failures: Array[String] = []

class FakeGateway:
	extends Node
	var calls: Array[Dictionary] = []
	var fail_action := ""
	var transient_action := ""
	var transient_remaining := 0
	var phase := "WAITING_MAIN"
	var correct := false
	var challenge := {
		"challenge_id": "challenge_test", "run_id": "run_test", "title": "边界灌溉",
		"brief": "请使用循环处理新的八块土地。", "focus": "零和三十边界",
		"moisture": [0, 30, 60, 90, 20, 40, 60, 80], "target": [30, 30, 70, 80, 50, 50, 60, 80],
		"starter_skill": {"source_bundle": {"files": [{"content": "int main() {}", "content_sha256": "int main() {}".sha256_text()}]}},
	}
	func send(action: String, entry: String, body: Dictionary) -> Dictionary:
		calls.append({"action": action, "entry": entry, "body": body.duplicate(true)})
		await get_tree().process_frame
		if transient_action == action and transient_remaining > 0:
			transient_remaining -= 1
			return {"ok": false, "code": "PRACTICE_CONNECTION_FAILED", "retryable": true}
		if fail_action == action:
			fail_action = ""
			return {"ok": false, "code": "PRACTICE_CONNECTION_FAILED"}
		match action:
			"start", "status": return {"ok": true, "value": {"entry_id": entry, "phase": phase, "run_id": null if phase == "WAITING_MAIN" else "run_test", "challenge": null if phase == "WAITING_MAIN" else challenge}}
			"prepare":
				phase = "CHALLENGE_READY"
				return {"ok": true, "value": challenge}
			"answer": return {"ok": true, "value": {"challenge_id": "challenge_test", "correct": correct, "status": "SUCCEEDED" if correct else "REJECTED", "stage": "PASSED" if correct else "COMPILE", "attempts": 1, "message": "判题反馈", "diagnostics": []}}
			"summary": return {"ok": true, "value": summary()}
		return {"ok": false}
	func summary() -> Dictionary:
		var message := "本次你完成了主关，并在新的土地数据上练习了循环与边界判断。"
		return {"challenge_id": "challenge_test", "message": message, "text_sha256": message.sha256_text(), "source": "provider", "speaker": "ICL_uranus_zh_male_bujiqingnian_tob", "format": "pcm_s16le", "sample_rate": 24000, "audio_base64": "AQABAA=="}

func _initialize() -> void:
	call_deferred("run")

func check(condition: bool, message: String) -> void:
	if not condition: failures.append(message)

func run() -> void:
	var panel = PanelScene.instantiate()
	root.add_child(panel)
	var fake := FakeGateway.new()
	panel.add_child(fake)
	panel.api = fake
	panel.enabled = true
	fake.fail_action = "start"
	check(not await panel.ensure_entry(), "Start failure must gate the main submission")
	var entry: String = panel.state.entry_id
	check(await panel.ensure_entry(), "Start can retry")
	check(fake.calls[0].entry == fake.calls[1].entry, "Start retry retains entry ID")
	check(entry.length() == 32 and not panel.visible, "Entry ready releases main UI")
	await panel.complete_main({"run_id": "run_failed", "status": "REJECTED"})
	check(fake.calls.size() == 2, "Rejected main run cannot prepare a challenge")
	await panel.complete_main({"run_id": "run_test", "status": "SUCCEEDED"})
	check(panel.state.phase == "CHALLENGE_READY" and panel.editor.text == "int main() {}", "Challenge initializes its independent editor")
	var count := fake.calls.size()
	await panel.complete_main({"run_id": "run_test", "status": "SUCCEEDED"})
	check(fake.calls.size() == count, "Repeated main success does not generate another challenge")
	panel.editor.text = "broken source"
	fake.fail_action = "answer"
	await panel._answer()
	var pending: Dictionary = panel.state.pending_answer.duplicate(true)
	check(not pending.is_empty() and not panel.editor.editable, "Response loss retains and locks original answer")
	await panel._answer()
	check(fake.calls[-2].body == fake.calls[-1].body, "Answer retry preserves ID, source and hash")
	check(panel.state.pending_answer.is_empty() and panel.editor.text == "broken source" and panel.editor.editable, "Compile rejection preserves editable source")
	panel.editor.text = "int main() { return 0; }"
	fake.correct = true
	fake.fail_action = "summary"
	await panel._answer()
	check(fake.calls[-2].body.answer_id != pending.answer_id, "New attempt gets a new answer ID")
	check(panel.state.phase == "SUMMARY_PENDING" and not panel.editor.visible, "Summary failure retains authoritative pass and disables answering")
	for code in ["BOOK_SPEECH_CONFIGURATION_INVALID", "BOOK_SPEECH_DISABLED", "BOOK_SPEECH_AUTH_FAILED", "BOOK_SPEECH_RESOURCE_NOT_GRANTED"]:
		panel._error({"code": code, "retryable": false})
		check(panel.feedback.text.contains("已通过") and panel.feedback.text.contains("配音"), "Configuration feedback distinguishes passed code from speech failure")
		check(not panel.feedback.text.contains("稍后重试") and panel.action_button.text == "配置修复后重试", "Configuration failures do not suggest waiting will fix them")
		check(panel.state.phase == "SUMMARY_PENDING" and panel._next_action == "summary", "Configuration failure retains the summary-only retry")
	panel._error({"code": "BOOK_SPEECH_PROVIDER_UNAVAILABLE", "retryable": true})
	check(panel.action_button.text == "重试本步", "A later temporary failure restores the normal retry button")
	count = fake.calls.size()
	await panel._answer()
	check(fake.calls.size() == count, "No new answer after pass")
	await panel._load_summary()
	check(panel.state.phase == "COMPLETED" and panel.speaker.stream != null and panel.details.text == fake.summary().message, "Full summary text and PCM are published together")
	var damaged := fake.summary()
	damaged.text_sha256 = "wrong"
	check(not Gateway.summary_audio(damaged, "challenge_test").ok, "Text hash mismatch rejected")
	damaged = fake.summary()
	damaged.audio_base64 = "AQ=="
	check(not Gateway.summary_audio(damaged, "challenge_test").ok, "Truncated PCM rejected")
	check(not Gateway.summary_audio(fake.summary(), "another_challenge").ok, "Wrong challenge audio rejected")
	panel._error({"code": "PRACTICE_ENTRY_EXPIRED"})
	check(panel.restart_button.visible and panel.action_button.disabled, "Expired entry requires explicit restart")
	# Resume an unresolved answer without regenerating the entry, challenge or source.
	panel.state.phase = "CHALLENGE_READY"
	panel.state.pending_answer = pending
	panel.state.source = "broken source"
	panel._ready_for_main = false
	fake.phase = "CHALLENGE_READY"
	await panel.ensure_entry()
	check(panel.state.entry_id == entry and panel.state.pending_answer == pending and not panel.editor.editable, "Recovery preserves unresolved write envelope")
	fake.transient_action = "answer"
	fake.transient_remaining = 1
	await panel._answer()
	check(fake.calls[-3].body == fake.calls[-2].body and fake.calls[-1].action == "summary", "Transient answer recovery must finish without a second user click")
	# Summary transport failures must reuse the same passed challenge automatically.
	panel.state.phase = "SUMMARY_PENDING"
	fake.transient_action = "summary"
	fake.transient_remaining = 2
	count = fake.calls.size()
	await panel._load_summary()
	check(panel.state.phase == "COMPLETED" and fake.calls.size() == count + 3, "Two temporary summary failures recover automatically")
	check(fake.calls[-1].body == fake.calls[-2].body and fake.calls[-2].body == fake.calls[-3].body, "Automatic retries keep the exact challenge identity")
	fake.transient_remaining = 5
	count = fake.calls.size()
	await panel._load_summary()
	check(fake.calls.size() == count + 3 and not panel.busy and panel.action_button.text == "重试本步", "Persistent outage has a bounded retry count and restores manual recovery")
	panel.queue_free()
	await process_frame
	if not failures.is_empty():
		for message in failures: push_error(message)
		quit(1)
	else:
		print("BUG_PRACTICE_FLOW_TEST PASS")
		quit(0)
