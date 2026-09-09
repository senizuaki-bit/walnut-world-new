extends SceneTree
## Explicit real-service acceptance of the existing player's normal UI path.
## No fixtures, fake transports, database resets, or direct world writes.
var store: WalnutClientStore
var controller: Node
var app: Node
var level: CropAdaptiveWateringDemo
var bridge: Node
var interactions: Array[Dictionary] = []
var stages: Array[Dictionary] = []
var action_done := false
var action_result: Dictionary = {}

func _initialize() -> void:
	if OS.get_environment("WALNUT_LIVE_ALIGNMENT") != "1":
		quit(2)
		return
	create_timer(1000).timeout.connect(func(): fail("TOTAL_TIMEOUT"))
	store = root.get_node("ClientStore")
	controller = root.get_node("SessionController")
	# Keep the user's normal frontend cache untouched during this live check.
	store.persistence_enabled = false
	app = load("res://scenes/app/app_root.tscn").instantiate()
	# Use the shipped AppRoot feature flags; optional HARVEST presentation is
	# a different rollout from this Crop/WATER scene's production path.
	app.poller_settings_override = {"deadline_seconds": 360.0, "interaction_deadline_seconds": 180.0}
	var startup := {"done": false, "result": {}}
	app.startup_finished.connect(func(result): startup.done = true; startup.result = result)
	root.add_child(app)
	while not startup.done: await process_frame
	if not startup.result.get("ok", false):
		fail("STARTUP", startup.result)
		return
	if OS.get_environment("WALNUT_LIVE_ALIGNMENT_MODE") in ["snapshot", "recovery"]:
		verify_recovery()
		return
	level = app.get_node("GameFlow/CropAdaptiveWateringDemo")
	bridge = app.get_node("CropAgentBridge")
	controller.interactions_recovered.connect(func(values): interactions.append_array(values))
	bridge.build_action_finished.connect(func(result): stages.append(result.duplicate(true)))
	bridge.activation_action_finished.connect(func(result): stages.append(result.duplicate(true)))
	bridge.submit_action_finished.connect(func(result): action_result = result.duplicate(true); action_done = true)
	var enter := app.get_node("GameFlow/GameStartScreen/HeroCard/Margin/Content/Copy/EnterButton") as Button
	while enter.disabled: await process_frame
	enter.pressed.emit()
	while not level.visible: await process_frame
	print("LIVE_ALIGNMENT startup PASS")
	# Verify an ordinary text request against the actual model first.
	await controller.request_hint("请根据当前关卡记录，简短说明我下一步应该检查什么。")
	if interactions.is_empty() or not store.get_pending_operation("agent_hint").is_empty():
		fail("TEXT_HINT", store.last_error)
		return
	print("LIVE_ALIGNMENT current-context text-hint PASS")
	var stamp := str(Time.get_unix_time_from_system())
	for attempt in range(3):
		close_dialogues()
		await process_frame
		var rejected_open: Dictionary = level.open_formal_run_workspace()
		if not rejected_open.get("ok", false):
			fail("OPEN_REJECTION_WORKSPACE", rejected_open)
			return
		var prior_count := interactions.size()
		var prior_world := int(store.world_snapshot.revision)
		level.code_editor.text = CropAdaptiveWateringDemo.CORRECT_CODE + "\ninvalid syntax " + stamp + " " + str(attempt) + "\n"
		action_done = false
		level.get_node("CodeDrawer/Surface/Margin/Content/Actions/RunButton").pressed.emit()
		while not action_done: await process_frame
		if action_result.get("ok", true) or interactions.size() <= prior_count or int(store.world_snapshot.revision) != prior_world:
			fail("COMPILE_REJECTION_FEEDBACK", {"result": action_result, "error": store.last_error})
			return
		var feedback: Dictionary = interactions.back()
		if feedback.get("feedback", {}).get("source") != "provider" or feedback.get("feedback", {}).get("degraded", true):
			fail("COMPILE_FEEDBACK_NOT_PROVIDER")
			return
		print("LIVE_ALIGNMENT compile-rejection-%d PASS role=%s" % [attempt + 1, feedback.get("role", "")])
		if attempt == 2 and feedback.get("role") != "bug_agent":
			fail("THREE_FAILURES_BUG_AGENT_MISSING")
			return
	close_dialogues()
	await process_frame
	var opened: Dictionary = level.open_formal_run_workspace()
	if not opened.get("ok", false):
		fail("OPEN_WORKSPACE", opened)
		return
	stages.clear()
	action_done = false
	level.code_editor.text = CropAdaptiveWateringDemo.CORRECT_CODE + "\n// live alignment " + stamp + "\n"
	var before_revision := int(store.world_snapshot.get("revision", -1))
	var run_button := level.get_node("CodeDrawer/Surface/Margin/Content/Actions/RunButton") as Button
	run_button.pressed.emit()
	while not action_done: await process_frame
	if not action_result.get("ok", false) or not store.objective_result.get("objective_succeeded", false):
		fail("BUILD_ACTIVATE_RUN", {"result": action_result, "error": store.last_error})
		return
	if int(store.world_snapshot.get("revision", -1)) <= before_revision:
		fail("WORLD_NOT_ADVANCED")
		return
	for stage in stages:
		if not stage.get("ok", false):
			fail("STAGE", stage)
			return
	print("LIVE_ALIGNMENT UI-build-activate-run-world-feedback PASS")
	close_dialogues()
	var cursor := store.last_interaction_sequence
	for question in ["刚才运行成功了吗？", "根据这次运行，用一句话总结我做对的地方。"]:
		await controller.request_hint(question)
		if not store.get_pending_operation("agent_hint").is_empty() or store.last_interaction_sequence <= cursor:
			fail("POST_SUCCESS_HINT", store.last_error)
			return
		cursor = store.last_interaction_sequence
		close_dialogues()
	print("LIVE_ALIGNMENT consecutive-post-success-hints PASS")
	print("LIVE_ALIGNMENT_PASS " + JSON.stringify({"session_id": store.authoritative_session.session_id, "world_revision": store.world_snapshot.revision, "run_id": store.objective_result.get("run_id"), "interaction_sequence": cursor, "stage_count": stages.size()}))
	app.queue_free()
	await process_frame
	quit(0)

func close_dialogues() -> void:
	level.story_dialogue.skip_sequence()
	var presenter := level.get_node("AgentInteractionPresenter")
	for _index in range(20):
		if not presenter.is_presenting(): break
		presenter.overlay.skip_sequence()

func verify_recovery() -> void:
	var fingerprint := {
		"session_id": store.authoritative_session.session_id,
		"active_skill": store.active_skill_tuple,
		"draft_source_sha256": store.local_source.sha256_text(),
		"draft_revision": store.draft.get("revision"),
		"world_id": store.world_snapshot.world_id,
		"world_revision": store.world_snapshot.revision,
		"world_hash": store.world_snapshot.state_hash,
		"interaction_sequence": store.last_interaction_sequence,
	}
	var audit: Dictionary = app.get("_transport").get_attempt_audit()
	for method in ["POST", "PUT", "PATCH", "DELETE"]:
		if int(audit.method_counts.get(method, 0)) != 0:
			fail("RECOVERY_MUTATED_SERVICE")
			return
	var path := OS.get_environment("WALNUT_LIVE_ALIGNMENT_FINGERPRINT")
	if path.is_empty():
		fail("RECOVERY_FINGERPRINT_PATH_MISSING")
		return
	if OS.get_environment("WALNUT_LIVE_ALIGNMENT_MODE") == "snapshot":
		var file := FileAccess.open(path, FileAccess.WRITE)
		if file == null:
			fail("FINGERPRINT_WRITE_FAILED")
			return
		file.store_string(JSON.stringify(fingerprint))
		file.close()
		print("LIVE_ALIGNMENT_RECOVERY_SNAPSHOT_PASS")
	else:
		var expected: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
		# JSON parses integers as floats in Godot; compare normalized values,
		# not the textual spelling of 5 versus 5.0.
		if not expected is Dictionary or expected != JSON.parse_string(JSON.stringify(fingerprint)):
			fail("RECOVERY_AUTHORITY_CHANGED", {"actual": fingerprint})
			return
		print("LIVE_ALIGNMENT_RESTART_RECOVERY_PASS: exact session, skill, draft, world, interaction cursor; zero HTTP writes")
	app.queue_free()
	quit(0)

func fail(stage: String, details: Dictionary = {}) -> void:
	push_error("LIVE_ALIGNMENT_FAIL " + stage + " " + JSON.stringify(details))
	quit(1)
