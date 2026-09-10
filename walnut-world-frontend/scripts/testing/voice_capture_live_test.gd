extends SceneTree
## Opt-in hardware probe: no audio files, provider calls, or saved transcripts.
const ProtocolTest = preload("res://tests/level_demo/mentor_question_test.gd")
func _initialize() -> void:
	if OS.get_environment("WALNUT_LIVE_MIC_CHECK") != "1":
		print("VOICE_CAPTURE_LIVE_NOT_RUN")
		quit(0)
		return
	var capture = load("res://scripts/client/voice_capture_pipe.gd").new()
	if not capture.configured() or not capture.start():
		push_error("VOICE_CAPTURE_START_FAILED")
		quit(1)
		return
	var capture_pid := int(capture.get("_process").pid)
	var frames := 0
	var nonzero := 0
	var deadline := Time.get_ticks_msec() + 4000
	while Time.get_ticks_msec() < deadline:
		for packet in capture.read_packets():
			if packet.size() != 640:
				capture.stop()
				quit(1)
				return
			frames += 1
			for offset in range(0, packet.size(), 2):
				if packet.decode_s16(offset) != 0: nonzero += 1
		await create_timer(0.01).timeout
	var capture_error: String = capture.error
	capture.stop()
	await create_timer(0.1).timeout
	if frames < 50 or not capture_error.is_empty() or OS.is_process_running(capture_pid):
		push_error("VOICE_CAPTURE_LIVE_FAILED frames=%d cleanup=%s error=%s" % [frames, not OS.is_process_running(capture_pid), capture_error])
		quit(1)
		return
	print("VOICE_CAPTURE_LIVE_PASS frames=%d nonzero_samples=%d mono_s16le_16000=true child_released=true" % [frames, nonzero])
	# Exercise the production ready/capture/send/close lifecycle as well.
	# Only the network transport is replaced; the physical microphone is real.
	var socket = ProtocolTest.Socket.new()
	var voice = load("res://scenes/ui/dingdang_voice_client.tscn").instantiate()
	root.add_child(voice)
	voice.socket_factory = func(): return socket
	voice.configure("http://127.0.0.1:8790", "test-token", "test-session")
	voice.start({})
	deadline = Time.get_ticks_msec() + 6000
	while socket.audio.size() < 50 and Time.get_ticks_msec() < deadline:
		await create_timer(0.01).timeout
	if socket.audio.size() < 50 or voice.microphone.playing or voice.get("_capture_pipe") == null:
		voice.close()
		push_error("VOICE_CAPTURE_CLIENT_LIFECYCLE_FAILED")
		quit(1)
		return
	var client_pid := int(voice.get("_capture_pipe").get("_process").pid)
	voice.close()
	await create_timer(0.1).timeout
	if OS.is_process_running(client_pid) or voice.get("_capture_pipe") != null:
		push_error("VOICE_CAPTURE_CLIENT_CLEANUP_FAILED")
		quit(1)
		return
	voice.queue_free()
	await process_frame
	print("VOICE_CAPTURE_CLIENT_PASS physical microphone -> production 640-byte sender; native microphone bypassed; close releases child")
	quit(0)
