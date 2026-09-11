extends SceneTree
const Guard := preload("res://scripts/testing/practice_live_guard.gd")
const Transport := preload("res://scripts/client/audited_http_agent_api_transport.gd")

func _initialize() -> void:
	var player := {"api_base_url": "http://127.0.0.1:18990"}
	for origin in ["http://127.0.0.1:8790", "http://localhost:8790/", "http://127.0.0.1:08790", "http://127.0.0.1:18990", "http://localhost:18990/", "https://example.com", "http://127.0.0.1:18790@evil.example", "http://127.0.0.1:18790/path", "http://127.0.0.1:99999"]:
		if Guard.allowed(origin, player, true):
			push_error("Online practice validation must reject the player gateway and external services.")
			quit(1)
			return
	if Guard.allowed("http://127.0.0.1:18890", player, false) or not Guard.allowed("http://127.0.0.1:18890", player, true):
		push_error("A separate gateway must be explicitly marked as dedicated to testing.")
		quit(1)
		return
	OS.set_environment("YAYA_API_BASE_URL", "http://127.0.0.1:18890")
	OS.set_environment("WALNUT_PRACTICE_LIVE", "1")
	OS.set_environment("WALNUT_PRACTICE_ISOLATED_GATEWAY", "1")
	var transport := Transport.new(root, "http://127.0.0.1:18890", "test-token")
	if not transport._configuration_error.is_empty():
		push_error("An explicitly isolated practice test must reach its real dedicated gateway: " + transport._configuration_error)
		quit(1)
		return
	transport.shutdown()
	for pair in [["http://127.0.0.1:18890", "bad\ntoken"], ["http://127.0.0.1:18891", "test-token"], ["http://example.com:18890", "test-token"]]:
		transport = Transport.new(root, pair[0], pair[1])
		if transport._configuration_error.is_empty():
			push_error("Isolated opt-in must preserve token, exact-origin and host validation.")
			quit(1)
			return
		transport.shutdown()
	OS.unset_environment("WALNUT_PRACTICE_ISOLATED_GATEWAY")
	transport = Transport.new(root, "http://127.0.0.1:18890", "test-token")
	if transport._configuration_error.is_empty():
		push_error("Ordinary launches must retain the fixed loopback port policy.")
		quit(1)
		return
	transport.shutdown()
	print("PRACTICE_LIVE_ISOLATION_PASS")
	quit(0)
