extends SceneTree

const SessionControllerScript := preload("res://autoload/session_controller.gd")


func _initialize() -> void:
	var controller := SessionControllerScript.new()
	root.add_child(controller)
	await process_frame
	if not controller.has_method("configure_world_presentation"):
		return _fail("SessionController has no formal authoritative presentation composition seam.")
	for method in ["set_world_playback_speed", "skip_world_playback", "replay_world_result"]:
		if not controller.has_method(method):
			return _fail("SessionController lacks formal playback control: %s" % method)
	var source := FileAccess.get_file_as_string("res://autoload/session_controller.gd")
	var closure_start := source.find("func _close_successful_run")
	var closure_end := source.find("func _close_failed_objective_run", closure_start)
	var closure_source := source.substr(closure_start, closure_end - closure_start)
	if closure_source.contains("store.replace_world(snapshot)"):
		return _fail("Successful Run closure still replaces the final Snapshot before authoritative playback.")
	if not source.contains("WalnutClientStore.FlowState.PLAYING"):
		return _fail("Formal controller never enters PLAYING.")
	if not source.contains("PLAYBACK_RECOVERED_BY_SNAPSHOT"):
		return _fail("Restart recovery lacks an explicit Snapshot-only presentation outcome.")
	var playback_failure_start := source.find("if not playback.get(\"ok\", false):")
	var playback_failure_end := source.find("return playback", playback_failure_start)
	var playback_failure_source := source.substr(
		playback_failure_start,
		playback_failure_end - playback_failure_start,
	)
	if (
		playback_failure_start < 0
		or playback_failure_end < 0
		or not playback_failure_source.contains("set_cursor")
		or not playback_failure_source.contains("presentation_high_watermark")
	):
		return _fail("Playback failure restores Snapshot authority but leaves the presentation cursor stale for the next Run.")
	print("WORLD_PRESENTATION_ASSEMBLY_TEST_PASS")
	quit(0)


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
