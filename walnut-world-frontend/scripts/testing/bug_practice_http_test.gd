extends SceneTree

var panel: Control

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var url := OS.get_environment("WALNUT_PRACTICE_HTTP_URL")
	if not url.begins_with("http://127.0.0.1:"):
		quit(2)
		return
	panel = load("res://scenes/ui/bug_practice_panel.tscn").instantiate()
	root.add_child(panel)
	panel.configure(url, "tenant_yaya:student_0001", "session_test")
	panel._path = "user://practice-http-test.json"
	panel.state = {}
	if not await panel.ensure_entry(): return fail("START")
	await panel.complete_main({"run_id": "run_http_test", "status": "SUCCEEDED"})
	if panel.state.phase != "CHALLENGE_READY": return fail("PREPARE")
	panel.editor.text = "incorrect"
	await panel._answer()
	if not panel.state.pending_answer.is_empty() or panel.state.phase != "CHALLENGE_READY": return fail("REJECTED_ANSWER")
	var capture := OS.get_environment("WALNUT_PRACTICE_CAPTURE")
	if not capture.is_empty():
		await process_frame
		await RenderingServer.frame_post_draw
		root.get_texture().get_image().save_png(capture + "-challenge.png")
	panel.editor.text = "correct"
	await panel._answer()
	if panel.state.phase != "COMPLETED" or panel.speaker.stream == null: return fail("SUMMARY_AUDIO")
	if not capture.is_empty():
		await process_frame
		await RenderingServer.frame_post_draw
		root.get_texture().get_image().save_png(capture + "-summary.png")
	var entry: String = panel.state.entry_id
	var status: Dictionary = await panel.api.send("status", entry)
	if not status.ok or status.value.phase != "COMPLETED": return fail("STATUS")
	var expired: Dictionary = await panel.api.send("status", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
	if expired.get("code") != "PRACTICE_ENTRY_EXPIRED": return fail("EXPIRED_ERROR")
	panel.api.token = "invalid"
	var denied: Dictionary = await panel.api.send("status", entry)
	if denied.ok: return fail("AUTHENTICATION")
	panel.queue_free()
	await process_frame
	print("PRACTICE_HTTP_TEST PASS: real route, auth, source hash, entry, challenge, answer, summary PCM, status, expiry; deterministic external ports")
	quit(0)

func fail(stage: String) -> void:
	push_error("PRACTICE_HTTP_FAIL " + stage + " " + panel.feedback.tooltip_text)
	panel.queue_free()
	await process_frame
	quit(1)
