extends SceneTree

const AgentApiGateway = preload("res://agent_api_gateway.gd")
const HttpAgentApiTransport = preload("res://http_agent_api_transport.gd")
const StrictJsonObjectScanner = preload("res://strict_json_object_scanner.gd")

signal second_request_finished

var _second_request_result: Dictionary = {}
var _frame_ticks := 0


class LocalHttpServer:
	extends Node

	var tcp_server: TCPServer
	var port := 0
	var requests: Array[Dictionary] = []
	var queued_responses: Array[Dictionary] = []
	var clients: Array[Dictionary] = []

	func start(requested_port: int = 8790) -> bool:
		tcp_server = TCPServer.new()
		if tcp_server.listen(requested_port, "127.0.0.1") == OK:
			port = requested_port
			set_process(true)
			return true
		return false

	func start_any() -> bool:
		for candidate in range(18871, 18921):
			if start(candidate):
				return true
		return false

	func enqueue_json(status: int, value: Dictionary, delay_msec: int = 0, headers: Dictionary = {}) -> void:
		queued_responses.push_back({
			"status": status,
			"body": JSON.stringify(value),
			"delay_msec": delay_msec,
			"headers": headers,
		})

	func enqueue_raw(status: int, body: String, delay_msec: int = 0, headers: Dictionary = {}) -> void:
		queued_responses.push_back({
			"status": status,
			"body": body,
			"delay_msec": delay_msec,
			"headers": headers,
		})

	func stop() -> void:
		set_process(false)
		for state in clients:
			var peer: StreamPeerTCP = state.peer
			peer.disconnect_from_host()
		clients.clear()
		if tcp_server != null:
			tcp_server.stop()

	func _process(_delta: float) -> void:
		while tcp_server != null and tcp_server.is_connection_available():
			var peer := tcp_server.take_connection()
			clients.push_back({
				"peer": peer,
				"buffer": PackedByteArray(),
				"parsed": false,
				"ready_at": 0,
				"response": {},
			})

		var index := clients.size() - 1
		while index >= 0:
			var state: Dictionary = clients[index]
			var peer: StreamPeerTCP = state.peer
			peer.poll()
			if not state.parsed:
				var available := peer.get_available_bytes()
				if available > 0:
					var read_result := peer.get_data(available)
					if read_result[0] != OK:
						clients.remove_at(index)
						index -= 1
						continue
					state.buffer.append_array(read_result[1])
				var parsed_request := _try_parse_request(state.buffer)
				if parsed_request.ok:
					assert(not queued_responses.is_empty(), "Local HTTP test server has no queued response.")
					var response: Dictionary = queued_responses.pop_front()
					requests.push_back(parsed_request.request)
					state.parsed = true
					state.response = response
					state.ready_at = Time.get_ticks_msec() + response.delay_msec
					clients[index] = state
			elif Time.get_ticks_msec() >= state.ready_at:
				if peer.get_status() == StreamPeerTCP.STATUS_CONNECTED:
					_send_response(peer, requests.back(), state.response)
				peer.disconnect_from_host()
				clients.remove_at(index)
			index -= 1

	func _try_parse_request(buffer: PackedByteArray) -> Dictionary:
		var header_end := _find_header_end(buffer)
		if header_end < 0:
			return {"ok": false}
		var header_text := buffer.slice(0, header_end).get_string_from_utf8()
		var lines := header_text.split("\r\n")
		if lines.is_empty():
			return {"ok": false}
		var request_line := lines[0].split(" ")
		assert(request_line.size() == 3, "Malformed local HTTP request line.")
		var headers := {}
		for line_index in range(1, lines.size()):
			var line: String = lines[line_index]
			var separator := line.find(":")
			assert(separator > 0, "Malformed local HTTP request header.")
			headers[line.substr(0, separator).strip_edges().to_lower()] = line.substr(separator + 1).strip_edges()
		var content_length := int(headers.get("content-length", "0"))
		var body_start := header_end + 4
		if buffer.size() < body_start + content_length:
			return {"ok": false}
		var body_text := buffer.slice(body_start, body_start + content_length).get_string_from_utf8()
		var body: Variant = null
		if content_length > 0:
			body = JSON.parse_string(body_text)
		return {
			"ok": true,
			"request": {
				"method": request_line[0],
				"path": request_line[1],
				"headers": headers,
				"body": body,
			},
		}

	func _find_header_end(buffer: PackedByteArray) -> int:
		for offset in range(0, buffer.size() - 3):
			if buffer[offset] == 13 and buffer[offset + 1] == 10 and buffer[offset + 2] == 13 and buffer[offset + 3] == 10:
				return offset
		return -1

	func _send_response(peer: StreamPeerTCP, request: Dictionary, response: Dictionary) -> void:
		var response_body: String = response.body
		var body_bytes: PackedByteArray = response_body.to_utf8_buffer()
		var reason := "OK" if response.status >= 200 and response.status <= 299 else "Error"
		var response_headers := {
			"Content-Type": "application/json; charset=utf-8",
			"Content-Length": String.num_int64(body_bytes.size()),
			"Connection": "close",
			"X-Request-Id": request.headers.get("x-request-id", "missing"),
			"X-Trace-Id": request.headers.get("x-trace-id", "missing"),
			"X-Correlation-Id": request.headers.get("x-correlation-id", "missing"),
			"X-Schema-Version": request.headers.get("x-schema-version", "missing"),
		}
		response_headers.merge(response.headers, true)
		var head := "HTTP/1.1 %d %s\r\n" % [response.status, reason]
		for header_name in response_headers:
			head += "%s: %s\r\n" % [header_name, response_headers[header_name]]
		head += "\r\n"
		var write_result := peer.put_data(head.to_utf8_buffer())
		assert(write_result == OK, "Failed to write local HTTP response headers.")
		if not body_bytes.is_empty():
			write_result = peer.put_data(body_bytes)
			assert(write_result == OK, "Failed to write local HTTP response body.")


func _initialize() -> void:
	call_deferred("_run_tests")


func _process(_delta: float) -> bool:
	_frame_ticks += 1
	return false


func _run_tests() -> void:
	var escaped_duplicate := StrictJsonObjectScanner.new().inspect("{\"field\":1,\"\\u0066ield\":2}")
	assert(escaped_duplicate.ok and escaped_duplicate.duplicate_found)
	var nested_duplicate := StrictJsonObjectScanner.new().inspect("{\"items\":[{\"\":1,\"\":2}]}")
	assert(nested_duplicate.ok and nested_duplicate.duplicate_found)
	var unique_json := StrictJsonObjectScanner.new().inspect("{\"left\":1,\"right\":[true,null,\"ok\"]}")
	assert(unique_json.ok and not unique_json.duplicate_found)
	var valid_surrogate_pair := StrictJsonObjectScanner.new().inspect("{\"value\":\"\\ud83c\\udf31\"}")
	assert(valid_surrogate_pair.ok and not valid_surrogate_pair.ill_formed_unicode_found)
	for ill_formed_json in ["{\"value\":\"\\ud800\"}", "{\"\\udc00\":true}"]:
		var ill_formed_unicode := StrictJsonObjectScanner.new().inspect(ill_formed_json)
		assert(not ill_formed_unicode.ok and ill_formed_unicode.ill_formed_unicode_found)
	var server := LocalHttpServer.new()
	get_root().add_child(server)
	assert(server.start(8790), "Could not bind the contract-declared local HTTP Mock port 8790.")
	var credential_sink := LocalHttpServer.new()
	get_root().add_child(credential_sink)
	assert(credential_sink.start_any(), "Could not bind the credential-leak test sink.")
	var command: Dictionary = _example("game-command.json")
	var build_request: Dictionary = _example("game-skill-build-create-request.json")
	var context: Dictionary = command.request_context.duplicate(true)
	context.request_id = "req_http_real_000001"
	context.trace_id = "trace_http_real_000001"
	context.correlation_id = "corr_http_real_000001"
	context.requested_at = "2026-08-07T12:00:00Z"
	await _assert_base_url_policy(context, command.command_id, credential_sink)

	var transport := HttpAgentApiTransport.new(
		get_root(),
		"http://127.0.0.1:%d/" % server.port,
		"student-dev-token",
		1.0,
		2,
	)
	var gateway := AgentApiGateway.new(transport)
	server.enqueue_json(200, command)
	var ticks_before := _frame_ticks
	var command_result: Dictionary = await gateway.get_command(context, command.command_id)
	assert(command_result.ok)
	assert(_frame_ticks > ticks_before, "A real HTTP call blocked the SceneTree main loop.")
	assert(server.requests.size() == 1)
	var get_request: Dictionary = server.requests[0]
	assert(get_request.method == "GET")
	assert(get_request.path == "/v1/commands/%s" % command.command_id)
	assert(get_request.headers.authorization == "Bearer student-dev-token")
	_assert_attempt_headers(get_request.headers, context)
	assert(not get_request.headers.has("idempotency-key"))
	assert(not get_request.headers.has("content-type"))

	var accepted := {
		"job_id": "job_http_real_000001",
		"job_type": "CREATE_SKILL_BUILD",
		"status": "ACCEPTED",
		"created_at": "2026-08-07T12:00:00Z",
		"updated_at": "2026-08-07T12:00:00Z",
		"command_id": "cmd_http_real_000001",
		"trace_id": context.trace_id,
		"error": null,
	}
	server.enqueue_json(202, accepted, 0, {
		"Location": "/v1/commands/%s" % accepted.command_id,
		"Retry-After": "1",
		"Idempotency-Replayed": "false",
	})
	var build_result: Dictionary = await gateway.submit_skill_build(
		context,
		"idem_http_real_000001",
		build_request,
	)
	assert(build_result.ok)
	assert(build_result.status == 202)
	var post_request: Dictionary = server.requests[1]
	assert(post_request.method == "POST")
	assert(post_request.path == "/v1/skill-builds")
	assert(post_request.headers["idempotency-key"] == "idem_http_real_000001")
	assert(post_request.headers["content-type"] == "application/json; charset=utf-8")
	assert(post_request.body == build_request)

	var float_command := command.duplicate(true)
	float_command.revision = float(float_command.revision)
	for field in [
		"previous_revision", "world_revision", "first_event_sequence", "last_event_sequence",
	]:
		float_command.result[field] = float(float_command.result[field])
	server.enqueue_raw(200, JSON.stringify(float_command))
	var float_command_context := _next_context(context, "float_integer_json")
	var float_command_result: Dictionary = await gateway.get_command(
		float_command_context, command.command_id
	)
	assert(float_command_result.ok)
	assert(typeof(float_command_result.value.revision) == TYPE_INT)
	assert(typeof(float_command_result.value.result.previous_revision) == TYPE_INT)
	assert(typeof(float_command_result.value.result.last_event_sequence) == TYPE_INT)
	var normalized_probe: Dictionary = transport.call("_normalize_json_integers", {
		"integer": 7.0,
		"nested": [8.0, {"integer": -9.0, "fraction": 1.5}],
	})
	assert(typeof(normalized_probe.integer) == TYPE_INT)
	assert(typeof(normalized_probe.nested[0]) == TYPE_INT)
	assert(typeof(normalized_probe.nested[1].integer) == TYPE_INT)
	assert(typeof(normalized_probe.nested[1].fraction) == TYPE_FLOAT)

	server.enqueue_raw(200, "{broken json")
	var invalid_json_context := _next_context(context, "invalid_json")
	var invalid_json_result: Dictionary = await gateway.get_command(invalid_json_context, command.command_id)
	assert(not invalid_json_result.ok)
	assert(invalid_json_result.status == 0)
	assert(invalid_json_result.headers.is_empty())
	assert(invalid_json_result.error.code == "LOCAL_TRANSPORT_JSON_INVALID")
	var command_json := JSON.stringify(command)
	var duplicate_json := "{\"command_id\":\"cmd_forged_duplicate\",%s" % command_json.substr(1)
	server.enqueue_raw(200, duplicate_json)
	var duplicate_json_context := _next_context(context, "duplicate_json")
	var duplicate_json_result: Dictionary = await gateway.get_command(duplicate_json_context, command.command_id)
	assert(not duplicate_json_result.ok)
	assert(duplicate_json_result.status == 0)
	assert(duplicate_json_result.headers.is_empty())
	assert(duplicate_json_result.error.code == "LOCAL_TRANSPORT_JSON_DUPLICATE_KEY")
	server.enqueue_raw(200, "{\"value\":\"\\ud800\"}")
	var unicode_context := _next_context(context, "invalid_unicode")
	var unicode_result: Dictionary = await gateway.get_command(unicode_context, command.command_id)
	assert(not unicode_result.ok)
	assert(unicode_result.status == 0)
	assert(unicode_result.headers.is_empty())
	assert(unicode_result.error.code == "LOCAL_TRANSPORT_JSON_INVALID")

	credential_sink.enqueue_json(200, command)
	server.enqueue_raw(302, "", 0, {
		"Location": "http://127.0.0.1:%d/credential-sink" % credential_sink.port,
	})
	var redirect_context := _next_context(context, "redirect_credential_sink")
	var redirect_result: Dictionary = await gateway.get_command(redirect_context, command.command_id)
	assert(not redirect_result.ok)
	assert(redirect_result.status == 0)
	assert(redirect_result.error.code == "LOCAL_TRANSPORT_NETWORK_ERROR")
	for unused_frame in range(3):
		await process_frame
	assert(credential_sink.requests.is_empty(), "Bearer credential followed a redirect to a disallowed target.")

	var timeout_transport := HttpAgentApiTransport.new(
		get_root(),
		"http://127.0.0.1:%d" % server.port,
		"student-dev-token",
		0.05,
		2,
	)
	var timeout_gateway := AgentApiGateway.new(timeout_transport)
	server.enqueue_json(200, command, 250)
	var timeout_context := _next_context(context, "timeout_0001")
	var timeout_result: Dictionary = await timeout_gateway.get_command(timeout_context, command.command_id)
	assert(not timeout_result.ok)
	assert(timeout_result.status == 0)
	assert(timeout_result.headers.is_empty())
	assert(timeout_result.error.code == "LOCAL_TRANSPORT_TIMEOUT")

	var cancel_transport := HttpAgentApiTransport.new(
		get_root(),
		"http://127.0.0.1:%d" % server.port,
		"student-dev-token",
		2.0,
		2,
	)
	var cancel_gateway := AgentApiGateway.new(cancel_transport)
	server.enqueue_json(200, command, 500)
	var cancel_context := _next_context(context, "cancel_0001")
	get_root().get_tree().create_timer(0.03).timeout.connect(
		func() -> void: assert(cancel_gateway.cancel_attempt(cancel_context.request_id)),
		CONNECT_ONE_SHOT,
	)
	var cancel_result: Dictionary = await cancel_gateway.get_command(cancel_context, command.command_id)
	assert(not cancel_result.ok)
	assert(cancel_result.status == 0)
	assert(cancel_result.headers.is_empty())
	assert(cancel_result.error.code == "LOCAL_TRANSPORT_CANCELLED")
	assert(cancel_transport.in_flight_count() == 0)

	var limited_transport := HttpAgentApiTransport.new(
		get_root(),
		"http://127.0.0.1:%d" % server.port,
		"student-dev-token",
		2.0,
		1,
	)
	var limited_gateway := AgentApiGateway.new(limited_transport)
	server.enqueue_json(200, command, 120)
	var first_context := _next_context(context, "limit_first")
	var second_context := _next_context(context, "limit_second")
	get_root().get_tree().create_timer(0.02).timeout.connect(
		_start_second_request.bind(limited_gateway, second_context, command.command_id),
		CONNECT_ONE_SHOT,
	)
	var first_result: Dictionary = await limited_gateway.get_command(first_context, command.command_id)
	assert(first_result.ok)
	if _second_request_result.is_empty():
		await second_request_finished
	assert(not _second_request_result.ok)
	assert(_second_request_result.error.code == "LOCAL_TRANSPORT_BUSY")
	assert(limited_transport.in_flight_count() == 0)
	var size_limited_transport := HttpAgentApiTransport.new(
		get_root(),
		"http://127.0.0.1:%d" % server.port,
		"student-dev-token",
		1.0,
		1,
		32,
	)
	var size_limited_gateway := AgentApiGateway.new(size_limited_transport)
	server.enqueue_json(200, command)
	var size_context := _next_context(context, "size_limit")
	var size_result: Dictionary = await size_limited_gateway.get_command(size_context, command.command_id)
	assert(not size_result.ok)
	assert(size_result.status == 0)
	assert(size_result.headers.is_empty())
	assert(size_result.error.code == "LOCAL_TRANSPORT_RESPONSE_TOO_LARGE")

	_assert_operation_mapping(transport, build_request)
	server.stop()
	credential_sink.stop()
	var network_transport := HttpAgentApiTransport.new(
		get_root(),
		"http://127.0.0.1:%d" % server.port,
		"student-dev-token",
		0.5,
		1,
	)
	var network_gateway := AgentApiGateway.new(network_transport)
	var network_context := _next_context(context, "network_down")
	var network_result: Dictionary = await network_gateway.get_command(network_context, command.command_id)
	assert(not network_result.ok)
	assert(network_result.status == 0)
	assert(network_result.headers.is_empty())
	# Depending on the host OS, a closed loopback port is reported immediately
	# as CANT_CONNECT or expires at the configured connect timeout. Both are
	# explicit retryable dependency failures and never become a false success.
	assert(network_result.error.code in ["LOCAL_TRANSPORT_NETWORK_ERROR", "LOCAL_TRANSPORT_TIMEOUT"])
	transport.shutdown()
	timeout_transport.shutdown()
	cancel_transport.shutdown()
	limited_transport.shutdown()
	size_limited_transport.shutdown()
	network_transport.shutdown()
	server.queue_free()
	credential_sink.queue_free()
	print("AGENT_GODOT_HTTP_TRANSPORT_TEST_OK")
	quit(0)


func _start_second_request(gateway: RefCounted, context: Dictionary, command_id: String) -> void:
	_second_request_result = await gateway.get_command(context, command_id)
	second_request_finished.emit()


func _assert_base_url_policy(context: Dictionary, command_id: String, credential_sink: LocalHttpServer) -> void:
	for allowed_base_url in [
		"http://127.0.0.1:8790",
		"http://localhost:8790/",
		"https://api.yaya.example",
		"https://api.yaya.example:8443/root",
		"https://127.0.0.1",
		"https://[::1]:443",
	]:
		var allowed_transport := HttpAgentApiTransport.new(
			get_root(), allowed_base_url, "policy-test-token", 0.1, 1,
		)
		assert(
			allowed_transport.get("_configuration_error").is_empty(),
			"Expected an allowed base URL: %s" % allowed_base_url,
		)
		allowed_transport.shutdown()

	var rejected_base_urls := [
		["remote_plaintext", "http://api.yaya.example:8790"],
		["wrong_loopback_port", "http://127.0.0.1:%d" % credential_sink.port],
		["implicit_loopback_port", "http://127.0.0.1"],
		["loopback_path_confusion", "http://127.0.0.1:8790/api"],
		["userinfo_before_loopback", "http://attacker.example@127.0.0.1:8790"],
		["userinfo_before_https", "https://user:secret@api.yaya.example"],
		["loopback_suffix", "http://127.0.0.1.evil.example:8790"],
		["localhost_suffix", "http://localhost.evil.example:8790"],
		["localhost_trailing_dot", "http://localhost.:8790"],
		["numeric_loopback", "http://2130706433:8790"],
		["encoded_loopback", "http://127%2e0%2e0%2e1:8790"],
		["noncanonical_port", "http://127.0.0.1:08790"],
		["authority_double_port", "https://api.yaya.example:443:444"],
		["empty_dns_label", "https://api..yaya.example"],
		["unmatched_authority_bracket", "https://[::1"],
		["uppercase_scheme", "HTTP://127.0.0.1:8790"],
		["authority_backslash", "http://127.0.0.1:8790\\@attacker.example"],
		["authority_fragment", "http://127.0.0.1:8790#@attacker.example"],
	]
	var sink_request_count := credential_sink.requests.size()
	for rejected_case in rejected_base_urls:
		var rejected_context := _next_context(context, rejected_case[0])
		var rejected_transport := HttpAgentApiTransport.new(
			get_root(), rejected_case[1], "credential-must-not-leak", 0.1, 1,
		)
		var result: Dictionary = await rejected_transport.execute("get_command", {
			"attempt_context": rejected_context,
			"command_id": command_id,
		})
		assert(not result.ok, "Rejected base URL was accepted: %s" % rejected_case[0])
		assert(result.status == 0)
		assert(result.headers.is_empty())
		assert(result.error.code == "LOCAL_TRANSPORT_NOT_CONFIGURED")
		assert(rejected_transport.in_flight_count() == 0)
		rejected_transport.shutdown()
	for unused_frame in range(3):
		await process_frame
	assert(
		credential_sink.requests.size() == sink_request_count,
		"A rejected base URL sent the Bearer credential to the sink.",
	)


func _assert_operation_mapping(transport: RefCounted, request_body: Dictionary) -> void:
	var content_hash := "a".repeat(64)
	assert(
		transport.call("_query_argument", {"content_hash": content_hash}, "content_hash")
		== content_hash
	)
	assert(
		transport.call("_query_argument", {"content_hash": ""}, "content_hash")
		== "__invalid_argument__"
	)
	var cases := [
		["get_bootstrap", {}, HTTPClient.METHOD_GET, "/v1/bootstrap"],
		["get_student_bootstrap", {}, HTTPClient.METHOD_GET, "/v1/student-bootstrap"],
		["submit_skill_build", {"request": request_body}, HTTPClient.METHOD_POST, "/v1/skill-builds"],
		["get_skill_build", {"build_id": "build_mapping_0001"}, HTTPClient.METHOD_GET, "/v1/skill-builds/build_mapping_0001"],
		["activate_skill_version", {"skill_version_id": "skillver_mapping_0001", "request": {}}, HTTPClient.METHOD_POST, "/v1/skill-versions/skillver_mapping_0001/activations"],
		["get_skill_activation", {"activation_id": "activation_mapping_0001"}, HTTPClient.METHOD_GET, "/v1/skill-activations/activation_mapping_0001"],
		["create_agent_session", {"request": {}}, HTTPClient.METHOD_POST, "/v1/agent-sessions"],
		["get_agent_session", {"session_id": "session_mapping_0001"}, HTTPClient.METHOD_GET, "/v1/agent-sessions/session_mapping_0001"],
		["submit_agent_turn", {"session_id": "session_mapping_0001", "request": {}}, HTTPClient.METHOD_POST, "/v1/agent-sessions/session_mapping_0001/turns"],
		["get_command", {"command_id": "cmd_mapping_0001"}, HTTPClient.METHOD_GET, "/v1/commands/cmd_mapping_0001"],
		["get_run", {"run_id": "run_mapping_0001"}, HTTPClient.METHOD_GET, "/v1/runs/run_mapping_0001"],
		["get_world_snapshot", {"world_id": "world_mapping_0001"}, HTTPClient.METHOD_GET, "/v1/worlds/world_mapping_0001/snapshot"],
		["get_world_events", {"world_id": "world_mapping_0001", "after_sequence": 7, "limit": 100}, HTTPClient.METHOD_GET, "/v1/worlds/world_mapping_0001/events?after_sequence=7&limit=100"],
		["upload_client_events", {"batch": {}}, HTTPClient.METHOD_POST, "/v1/client-events:batch"],
		["get_evidence", {"evidence_id": "evidence_mapping_0001"}, HTTPClient.METHOD_GET, "/v1/evidence/evidence_mapping_0001"],
	]
	for test_case in cases:
		var result: Dictionary = transport.call("_build_request_spec", test_case[0], test_case[1])
		assert(result.ok, "Operation mapping failed for %s" % test_case[0])
		assert(result.spec.method == test_case[2], "HTTP method drift for %s" % test_case[0])
		assert(result.spec.path == test_case[3], "HTTP path drift for %s" % test_case[0])


func _assert_attempt_headers(headers: Dictionary, context: Dictionary) -> void:
	assert(headers["x-request-id"] == context.request_id)
	assert(headers["x-trace-id"] == context.trace_id)
	assert(headers["x-correlation-id"] == context.correlation_id)
	assert(headers["x-schema-version"] == context.schema_version)


func _next_context(source: Dictionary, suffix: String) -> Dictionary:
	var context := source.duplicate(true)
	context.request_id = "req_%s_00000001" % suffix
	context.trace_id = "trace_%s_00000001" % suffix
	context.correlation_id = "corr_%s_00000001" % suffix
	context.requested_at = "2026-08-07T12:00:01Z"
	return context


func _example(name: String) -> Dictionary:
	var path := ProjectSettings.globalize_path("res://../../contracts/examples/%s" % name)
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	assert(parsed is Dictionary and parsed.has("value"))
	return parsed.value
