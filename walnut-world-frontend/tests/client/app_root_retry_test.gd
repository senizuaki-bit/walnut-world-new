extends SceneTree
const Fixture = preload("res://tests/client/app_root_local_http_e2e_test.gd")

class RecoveringServer extends Fixture.LocalGatewayServer:
	var unavailable := true
	func _route(method: String, path: String, headers: Dictionary, body: Dictionary) -> Dictionary:
		if unavailable:
			return _response({"error": "temporarily unavailable"}, [], 503)
		return super._route(method, path, headers, body)

func _initialize() -> void:
	create_timer(40).timeout.connect(func(): fail("TOTAL_TIMEOUT"))
	var server := RecoveringServer.new()
	root.add_child(server)
	if not server.start():
		fail("Loopback fixture unavailable")
		return
	server.current_session_id = "session_demo_0001"
	var store = root.get_node("ClientStore")
	store.persistence_enabled = false
	var app = load("res://scenes/app/app_root.tscn").instantiate()
	app.runtime_environment_override = {"YAYA_API_BASE_URL": server.base_url(), "YAYA_AUTH_TOKEN": "fixture-token"}
	var results: Array[Dictionary] = []
	app.startup_finished.connect(func(value): results.append(value))
	root.add_child(app)
	await process_frame
	var flow = app.get_node("GameFlow")
	var screen = flow.start_screen
	if not screen.enter_button.disabled:
		fail("Entry must be disabled while bootstrap is pending")
		return
	var deadline := Time.get_ticks_msec() + 20000
	while results.is_empty() and Time.get_ticks_msec() < deadline:
		await process_frame
	if results.is_empty() or results[0].get("ok", true) or screen.can_enter() or screen.enter_button.text != "重新连接":
		fail("Connection failure must offer retry without unlocking entry")
		return
	flow.start_screen.enter_farm_requested.emit()
	await process_frame
	if flow.crop_adaptive_watering_demo.visible:
		fail("Failed bootstrap must not enter the farm even via a stale entry signal")
		return
	server.unavailable = false
	screen.enter_button.pressed.emit()
	screen.enter_button.pressed.emit()
	deadline = Time.get_ticks_msec() + 15000
	while results.size() < 2 and Time.get_ticks_msec() < deadline:
		await process_frame
	if results.size() != 2 or not results[1].get("ok", false) or not screen.can_enter() or screen.enter_button.disabled:
		fail("Retry must recover the existing session exactly once: " + str(results))
		return
	for path in server.paths:
		if not path.begins_with("GET "):
			fail("Recovery of an existing session must not create writes")
			return
	screen.enter_button.pressed.emit()
	await create_timer(1.0).timeout
	if not flow.crop_adaptive_watering_demo.visible:
		fail("Successful retry must allow entering the farm")
		return
	app.queue_free()
	await process_frame
	server.queue_free()
	await process_frame
	print("APP_ROOT_RETRY_PASS")
	quit(0)

func fail(message: String) -> void:
	push_error(message)
	quit(1)
