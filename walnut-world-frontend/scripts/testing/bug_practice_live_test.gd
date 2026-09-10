extends SceneTree
## Opt-in: real public gateway, real C++ builds, model and speech dependencies.
var app: Node
var level: CropAdaptiveWateringDemo
var done := false
var result: Dictionary = {}

func _initialize() -> void:
	if OS.get_environment("WALNUT_PRACTICE_LIVE") != "1":
		quit(2)
		return
	call_deferred("run")

func run() -> void:
	create_timer(1000).timeout.connect(func(): fail("TOTAL_TIMEOUT"))
	var store: WalnutClientStore = root.get_node("ClientStore")
	store.persistence_enabled = false
	app = load("res://scenes/app/app_root.tscn").instantiate()
	app.poller_settings_override = {"deadline_seconds": 360.0, "interaction_deadline_seconds": 180.0}
	app.startup_finished.connect(func(value): done = true; result = value)
	root.add_child(app)
	while not done: await process_frame
	if not result.get("ok", false):
		fail("STARTUP", result)
		return
	level = app.get_node("GameFlow").crop_adaptive_watering_demo
	app.get_node("GameFlow").start_screen.hide()
	level.show()
	level.story_dialogue.skip_sequence()
	var resume_path := OS.get_environment("WALNUT_PRACTICE_RESUME")
	if not resume_path.is_empty():
		level.practice._path = resume_path
		level.practice.state = JSON.parse_string(FileAccess.get_file_as_string(resume_path))
		level.practice._ready_for_main = false
		await level.practice.ensure_entry()
		await verify_challenge(store)
		return
	level.practice.state = {}
	level.practice._path = "user://practice-live-" + str(Time.get_unix_time_from_system()).replace(".", "") + ".json"
	if not await level.practice.ensure_entry():
		fail("ENTRY_START")
		return
	print("PRACTICE_LIVE entry-start PASS")
	level.open_formal_run_workspace()
	level.code_editor.text = CropAdaptiveWateringDemo.CORRECT_CODE
	var bridge: Node = app.get_node("CropAgentBridge")
	done = false
	bridge.submit_action_finished.connect(func(value): done = true; result = value)
	level.agent_submit_requested.emit(level.code_editor.text)
	while not done: await process_frame
	if not result.get("objective_succeeded", false):
		fail("MAIN_SUBMISSION", result)
		return
	print("PRACTICE_LIVE main-build-activation-run PASS")
	await verify_challenge(store)

func verify_challenge(store: WalnutClientStore) -> void:
	while level.practice.busy: await process_frame
	if level.practice.state.get("phase") != "CHALLENGE_READY":
		fail("CHALLENGE_PREPARE", {"error": level.practice.feedback.tooltip_text})
		return
	var challenge: Dictionary = level.practice.state.challenge
	var main_source := store.local_source
	var snapshot := store.world_snapshot.duplicate(true)
	print("PRACTICE_LIVE provider-challenge PASS " + JSON.stringify({"challenge_id": challenge.challenge_id, "run_id": challenge.run_id}))
	level.practice.editor.text = "int main( {"
	await level.practice._answer()
	if level.practice.state.phase != "CHALLENGE_READY" or not level.practice.state.pending_answer.is_empty() or not level.practice.feedback.text.contains("COMPILE"):
		fail("COMPILE_REJECTION")
		return
	print("PRACTICE_LIVE compile-rejection PASS")
	level.practice.editor.text = "#include <iostream>\nusing namespace std;\nint main(){ int moisture[8], target[8]; for(int i=0;i<8;i++)cin>>moisture[i]; for(int i=0;i<8;i++)cin>>target[i]; for(int i=0;i<8;i++){ int gap=target[i]-moisture[i]; if(gap>=30)cout<<\"WATER \"<<i<<\" 2\\n\"; else if(gap>0)cout<<\"WATER \"<<i<<\" 1\\n\"; } return 0; }"
	await level.practice._answer()
	if level.practice.state.phase not in ["SUMMARY_PENDING", "COMPLETED"]:
		fail("CORRECT_ANSWER", {"feedback": level.practice.feedback.text})
		return
	if store.local_source != main_source or store.world_snapshot != snapshot:
		fail("PRACTICE_MUTATED_MAIN_STATE")
		return
	print("PRACTICE_LIVE correct-answer-main-state-isolation PASS")
	if level.practice.state.phase == "SUMMARY_PENDING":
		print("PRACTICE_LIVE SUMMARY_NOT_PROVEN " + level.practice.feedback.tooltip_text)
		finish(3)
		return
	print("PRACTICE_LIVE summary-text-audio PASS")
	if not OS.get_environment("WALNUT_PRACTICE_CAPTURE").is_empty():
		await process_frame
		await RenderingServer.frame_post_draw
		root.get_texture().get_image().save_png(OS.get_environment("WALNUT_PRACTICE_CAPTURE"))
	finish(0)

func fail(stage: String, detail: Dictionary = {}) -> void:
	push_error("PRACTICE_LIVE_FAIL " + stage + " " + JSON.stringify(detail))
	finish(1)

func finish(code: int) -> void:
	if is_instance_valid(app): app.queue_free()
	await process_frame
	quit(code)
