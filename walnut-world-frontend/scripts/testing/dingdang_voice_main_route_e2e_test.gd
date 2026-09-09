extends SceneTree
## Explicit opt-in: unchanged main FastAPI route + fake external Provider I/O.
const Scene := preload("res://scenes/ui/dingdang_voice_client.tscn")
const Pcm := preload("res://scripts/client/voice_pcm.gd")
var text := ""
var transcript := ""
var cancelled := false
var error := ""

func _initialize() -> void:
	if OS.get_environment("WALNUT_VOICE_PROTOCOL_E2E") != "1":
		print("VOICE_MAIN_ROUTE_OPT_IN_EXCLUDED_NOT_RUN")
		quit(0)
		return
	var voice := Scene.instantiate()
	voice.capture_enabled = false
	root.add_child(voice)
	await process_frame
	voice.text_received.connect(func(value: String, complete: bool): text = value if complete else text + value)
	voice.transcript_received.connect(func(value: String, complete: bool): transcript = value if complete else transcript + value)
	voice.response_cancelled.connect(func(): cancelled = true)
	voice.failed.connect(func(value: String): error = value)
	voice.configure("http://127.0.0.1:8790", "tenant_yaya:student_voice", "session_voice_demo")
	voice.start({"code": "old code"})
	var deadline := Time.get_ticks_msec() + 5000
	while voice.state != "READY" and Time.get_ticks_msec() < deadline and error.is_empty():
		await process_frame
	if voice.state != "READY":
		push_error("Real main voice.ready not received: " + error)
		quit(1)
		return
	voice.update_context({"code": "new frontend code", "observation": "local test"})
	await create_timer(0.7).timeout
	var codec := Pcm.new()
	var frames := PackedVector2Array()
	frames.resize(961)
	frames.fill(Vector2(0.1, 0.1))
	var packets := codec.encode(frames, 48000)
	voice.get("_socket").put_packet(packets[0])
	deadline = Time.get_ticks_msec() + 5000
	while (text.is_empty() or transcript.is_empty()) and Time.get_ticks_msec() < deadline and error.is_empty():
		await process_frame
	if not text.contains("new frontend code") or transcript != "代码怎么改？" or not error.is_empty():
		push_error("Main route context/transcript/text exchange failed: " + error)
		quit(1)
		return
	voice.interrupt()
	cancelled = false # Require the server acknowledgement, beyond local cleanup.
	deadline = Time.get_ticks_msec() + 2000
	while not cancelled and Time.get_ticks_msec() < deadline:
		await process_frame
	voice.close()
	if not cancelled or voice.state != "IDLE" or voice.microphone.playing or voice.speaker.playing:
		push_error("Main route cancellation/close cleanup failed.")
		quit(1)
		return
	voice.queue_free()
	await process_frame
	print("VOICE_MAIN_ROUTE_E2E_PASS: real WebSocket, main route, context, PCM, transcript, reply, interrupt, close; fake external Provider")
	quit(0)
