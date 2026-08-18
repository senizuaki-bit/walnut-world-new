extends SceneTree

const AppRootScript := preload("res://scenes/app/app_root.gd")
const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const HASH_CONTENT := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const HASH_WORLD := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
const HASH_SKILL := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


class LocalGatewayServer:
	extends Node

	var listener := TCPServer.new()
	var connections: Array[Dictionary] = []
	var paths: Array[String] = []
	var failure := ""
	var current_session_id: Variant = null
	var advertised_host := "127.0.0.1"

	func start() -> bool:
		if listener.listen(8790, "127.0.0.1") != OK:
			listener = TCPServer.new()
			if listener.listen(8790, "::1") != OK:
				set_process(false)
				return false
			advertised_host = "localhost"
		set_process(true)
		return true

	func base_url() -> String:
		return "http://%s:8790" % advertised_host

	func _exit_tree() -> void:
		listener.stop()

	func _process(_delta: float) -> void:
		while listener.is_connection_available():
			var peer := listener.take_connection()
			connections.append({"peer": peer, "buffer": PackedByteArray()})
		for index in range(connections.size() - 1, -1, -1):
			var connection: Dictionary = connections[index]
			var peer: StreamPeerTCP = connection.peer
			peer.poll()
			var available := peer.get_available_bytes()
			if available > 0:
				var chunk := peer.get_data(available)
				if chunk[0] != OK:
					failure = "local server could not read request bytes"
					connections.remove_at(index)
					continue
				connection.buffer.append_array(chunk[1])
			if _request_complete(connection.buffer):
				_handle(peer, connection.buffer.get_string_from_utf8())
				connections.remove_at(index)
			elif peer.get_status() in [StreamPeerTCP.STATUS_NONE, StreamPeerTCP.STATUS_ERROR]:
				connections.remove_at(index)

	func _request_complete(bytes: PackedByteArray) -> bool:
		var text := bytes.get_string_from_utf8()
		var header_end := text.find("\r\n\r\n")
		if header_end < 0:
			return false
		var content_length := 0
		for line in text.substr(0, header_end).split("\r\n"):
			if line.to_lower().begins_with("content-length:"):
				content_length = int(line.get_slice(":", 1).strip_edges())
		return bytes.size() >= header_end + 4 + content_length

	func _handle(peer: StreamPeerTCP, request_text: String) -> void:
		var header_end := request_text.find("\r\n\r\n")
		var lines := request_text.substr(0, header_end).split("\r\n")
		var request_line := lines[0].split(" ")
		if request_line.size() < 2:
			failure = "malformed local HTTP request"
			return
		var method := str(request_line[0])
		var path := str(request_line[1])
		paths.append("%s %s" % [method, path])
		var request_body: Variant = {}
		var request_body_text := request_text.substr(header_end + 4)
		if not request_body_text.is_empty():
			request_body = JSON.parse_string(request_body_text)
			if not request_body is Dictionary:
				failure = "local server received an invalid JSON request body"
				request_body = {}
		var headers := {}
		for line in lines.slice(1):
			var separator := line.find(":")
			if separator > 0:
				headers[line.substr(0, separator).to_lower()] = line.substr(separator + 1).strip_edges()
		var routed := _route(method, path, headers, request_body)
		var body := JSON.stringify(routed.body)
		var response_headers := [
			"HTTP/1.1 %d %s" % [int(routed.status), "OK" if int(routed.status) < 300 else "ERROR"],
			"Content-Type: application/json",
			"Content-Length: %d" % body.to_utf8_buffer().size(),
			"Connection: close",
			"X-Request-Id: %s" % str(headers.get("x-request-id", "missing")),
			"X-Trace-Id: %s" % str(headers.get("x-trace-id", "missing")),
			"X-Correlation-Id: %s" % str(headers.get("x-correlation-id", "missing")),
			"X-Schema-Version: 1.0.0",
		]
		for header in routed.headers:
			response_headers.append(header)
		var wire := "\r\n".join(response_headers) + "\r\n\r\n" + body
		peer.put_data(wire.to_utf8_buffer())
		peer.disconnect_from_host()

	func _route(method: String, path: String, headers: Dictionary, request_body: Dictionary) -> Dictionary:
		if method == "GET" and path == "/v1/student-bootstrap":
			return _response(_student_bootstrap())
		if method == "GET" and path.begins_with("/product-experience/v1/content-units/TASK_DEMO_001/versions/1.0.0?content_hash="):
			return _response(_content_unit())
		if method == "POST" and path == "/v1/agent-sessions":
			if not _exact_session_create_request(request_body):
				failure = "AppRoot did not POST StudentBootstrap.session.create_request verbatim"
				return _response({"error": "request body mismatch"}, [], 400)
			return _response(_accepted(str(headers.get("x-trace-id", ""))), [
				"Location: /v1/commands/cmd_session_create_0001",
				"Retry-After: 1",
				"Idempotency-Replayed: false",
			], 202)
		if method == "GET" and path == "/v1/commands/cmd_session_create_0001":
			return _response(_session_command())
		if method == "GET" and path == "/v1/agent-sessions/session_demo_0001":
			return _response(_agent_session())
		if method == "GET" and path == "/product-experience/v1/sessions/session_demo_0001/workspace":
			return _response(_workspace())
		if method == "GET" and path == "/product-experience/v1/sessions/session_demo_0001/skill-drafts/draft_demo_0001":
			return _response(_draft())
		if method == "GET" and path == "/v1/worlds/world_demo_0001/snapshot":
			return _response(_snapshot(), ["ETag: \"snapshot_world_0001\"", "X-World-Revision: 0"])
		if method == "GET" and path == "/v1/worlds/world_demo_0001/presentation-events?after_sequence=0&limit=500":
			return _response({
				"request_context": _context(),
				"world_id": "world_demo_0001",
				"snapshot_revision": 0,
				"snapshot_last_event_sequence": 0,
				"snapshot_state_hash": HASH_WORLD,
				"presentation_high_watermark": 0,
				"from_sequence": 0,
				"to_sequence": 0,
				"has_more": false,
				"next_after_sequence": 0,
				"events": [],
			})
		if method == "GET" and path == "/product-experience/v1/sessions/session_demo_0001/agent-interactions?after_sequence=0&limit=50":
			return _response(_interaction_page(), ["X-Interaction-High-Watermark: 0"])
		failure = "unexpected route: %s %s" % [method, path]
		return _response({"error": "not found"}, [], 404)

	func _exact_session_create_request(value: Dictionary) -> bool:
		var expected: Dictionary = _student_bootstrap().session.create_request
		return (
			value.size() == expected.size()
			and str(value.get("world_id", "")) == str(expected.world_id)
			and str(value.get("learner_id", "")) == str(expected.learner_id)
			and str(value.get("agent_profile_id", "")) == str(expected.agent_profile_id)
			and str(value.get("channel", "")) == str(expected.channel)
			and str(value.get("locale", "")) == str(expected.locale)
			and value.get("content") == expected.content
			and int(value.get("expected_world_revision", -1)) == int(expected.expected_world_revision)
		)

	func _response(body: Dictionary, headers: Array[String] = [], status: int = 200) -> Dictionary:
		return {"status": status, "headers": headers, "body": body}

	func _actor() -> Dictionary:
		return {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]}

	func _content_ref() -> Dictionary:
		return {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": HASH_CONTENT}

	func _context() -> Dictionary:
		return {"schema_version": "1.0.0", "request_id": "req_server_context_0001", "correlation_id": "corr_server_context_0001", "trace_id": "trace_server_context_0001", "requested_at": "2026-08-12T00:00:00Z", "actor": _actor(), "content_ref": _content_ref()}

	func _versions() -> Dictionary:
		return {"api_version": "1.0.0", "event_version": "1.0.0", "policy_version": "policy-v1", "world_rules_version": "world-v1", "teaching_spec_version": "agent-teaching-v1"}

	func _student_bootstrap() -> Dictionary:
		return {"request_context": _context(), "api_version": "1.1.0", "contract_version": "0.4.0", "server_time": "2026-08-12T00:00:00Z", "actor": _actor(), "content": _content_ref(), "capabilities": {"skill_builds": true, "skill_activations": true, "agent_sessions": true, "http_world_recovery": true, "evidence_query": true}, "session": {"current_session_id": current_session_id, "teaching_spec_version": "agent-teaching-v1", "create_request": {"world_id": "world_demo_0001", "learner_id": "learner_demo_0001", "agent_profile_id": "profile_demo_0001", "channel": "GAME", "locale": "zh-CN", "content": _content_ref(), "expected_world_revision": 0}}, "build": {"build_policy_id": "policy_demo_0001", "compiler_profile": "YAYA_CPP20_SAFE_V1", "compiler_version": "clang-20.1.0", "sandbox_image_digest": "sha256:" + HASH_WORLD, "test_suite_version": "farm-water-v3", "allowed_capabilities": ["WORLD_READ", "WATER"], "max_source_files": 32, "max_source_bytes": 1048576}, "activation": {"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"}, "registry_revision": 1, "active": {"activation_id": "activation_demo_0001", "skill_id": "skill_demo_0001", "skill_version_id": "skillver_demo_0001", "artifact_sha256": HASH_SKILL, "certification_id": "cert_demo_0001", "registry_revision": 1, "activated_at": "2026-08-12T00:00:00Z"}}, "world": {"world_id": "world_demo_0001", "revision": 0, "last_event_sequence": 0, "state_hash": HASH_WORLD, "snapshot_url": "/v1/worlds/world_demo_0001/snapshot", "events_url": "/v1/worlds/world_demo_0001/events"}}

	func _content_unit() -> Dictionary:
		return {"content_ref": _content_ref(), "status": "PUBLISHED", "unit_type": "TASK", "audiences": ["LEARNER"], "task": {"task_id": "task_demo_0001", "name": "Water one plot", "goal": "Use a loop", "instructions": ["Write code"], "knowledge_points": ["loop"], "allowed_capabilities": ["WORLD_READ", "WATER"], "starter_skill": null, "hint_policy": {"max_level": 4, "levels": [0, 1, 2, 3, 4]}, "story": {"opening": "Start", "success": "Done"}}, "published_at": "2026-08-12T00:00:00Z", "links": {"self": "/product-experience/v1/content-units/TASK_DEMO_001/versions/1.0.0"}}

	func _accepted(trace_id: String) -> Dictionary:
		return {"job_id": "job_session_create_0001", "job_type": "CREATE_AGENT_SESSION", "status": "ACCEPTED", "created_at": "2026-08-12T00:00:00Z", "updated_at": "2026-08-12T00:00:00Z", "command_id": "cmd_session_create_0001", "trace_id": trace_id, "error": null}

	func _session_command() -> Dictionary:
		return {"request_context": _context(), "command_id": "cmd_session_create_0001", "revision": 1, "command_type": "CREATE_AGENT_SESSION", "status": "APPLIED", "stage": "COMPLETE", "terminal": true, "accepted_at": "2026-08-12T00:00:00Z", "updated_at": "2026-08-12T00:00:01Z", "result": {"result_type": "RESOURCE_CREATED", "resource_type": "AGENT_SESSION", "resource_id": "session_demo_0001", "resource_url": "/v1/agent-sessions/session_demo_0001"}, "error": null, "evidence_refs": [], "versions": _versions(), "links": {"self": "/v1/commands/cmd_session_create_0001"}}

	func _agent_session() -> Dictionary:
		return {"request_context": _context(), "session_id": "session_demo_0001", "world_id": "world_demo_0001", "learner_id": "learner_demo_0001", "agent_profile_id": "profile_demo_0001", "channel": "GAME", "status": "ACTIVE", "created_at": "2026-08-12T00:00:00Z", "updated_at": "2026-08-12T00:00:01Z", "last_turn_sequence": 0, "content": _content_ref(), "versions": _versions(), "links": {"self": "/v1/agent-sessions/session_demo_0001", "turns": "/v1/agent-sessions/session_demo_0001/turns", "world_snapshot": "/v1/worlds/world_demo_0001/snapshot"}}

	func _workspace() -> Dictionary:
		return {"request_context": _context(), "workspace_id": "workspace_demo_0001", "workspace_revision": 1, "session": _agent_session(), "content_ref": _content_ref(), "current_task": {"task_id": "task_demo_0001"}, "world_checkpoint": {"world_id": "world_demo_0001", "world_revision": 0, "last_event_sequence": 0}, "skill_draft_refs": [{"draft_id": "draft_demo_0001", "skill_id": "skill_demo_0001", "revision": 1, "draft_sha256": str(_draft().draft_sha256), "is_current": true}], "last_interaction_sequence": 0, "created_at": "2026-08-12T00:00:00Z", "updated_at": "2026-08-12T00:00:01Z", "links": {"self": "/product-experience/v1/sessions/session_demo_0001/workspace"}}

	func _draft() -> Dictionary:
		var source := "int main(){return 0;}"
		var draft := {"request_context": _context(), "session_id": "session_demo_0001", "draft_id": "draft_demo_0001", "skill_id": "skill_demo_0001", "revision": 1, "content_ref": _content_ref(), "display_name": "Demo", "source_bundle": {"language": "CPP20", "entrypoint": "src/main.cpp", "files": [{"path": "src/main.cpp", "content": source, "content_sha256": source.sha256_text()}]}, "draft_sha256": "", "created_at": "2026-08-12T00:00:00Z", "updated_at": "2026-08-12T00:00:01Z", "last_applied_patch_id": null, "links": {"self": "/product-experience/v1/sessions/session_demo_0001/skill-drafts/draft_demo_0001", "session_workspace": "/product-experience/v1/sessions/session_demo_0001/workspace", "builds": "/v1/skill-builds"}}
		draft.draft_sha256 = ContractValidator.canonical_json_sha256_v1({
			"session_id": draft.session_id, "draft_id": draft.draft_id,
			"skill_id": draft.skill_id, "content_ref": draft.content_ref,
			"display_name": draft.display_name, "source_bundle": draft.source_bundle,
		})
		return draft

	func _snapshot() -> Dictionary:
		return {"request_context": _context(), "world_id": "world_demo_0001", "revision": 0, "last_event_sequence": 0, "state_schema_version": "1.0.0", "state_hash": HASH_WORLD, "generated_at": "2026-08-12T00:00:01Z", "world_rules_version": "world-v1", "state": {"clock": {"day": 1, "minute_of_day": 0, "tick": 0}, "avatar": {"entity_id": "avatar_demo_0001", "position": {"x": 0, "y": 0}, "energy": 100}, "inventory": [], "plots": [_plot("plot_demo_0000", 0), _plot("plot_demo_0001", 1), _plot("plot_demo_0002", 2), _plot("plot_demo_0003", 3), _plot("plot_demo_0004", 4)], "agents": []}}

	func _plot(plot_id: String, x: int) -> Dictionary:
		return {
			"plot_id": plot_id,
			"position": {"x": x, "y": 0},
			"soil_state": "TILLED",
			"hydration": 0,
			"crop": null,
			"last_updated_event_sequence": 0,
		}

	func _interaction_page() -> Dictionary:
		return {"request_context": _context(), "session_id": "session_demo_0001", "requested_after_sequence": 0, "requested_limit": 50, "high_watermark_sequence": 0, "from_sequence": null, "to_sequence": null, "has_more": false, "next_after_sequence": 0, "interactions": []}


func _initialize() -> void:
	var server := LocalGatewayServer.new()
	server.name = "LocalGatewayServer"
	root.add_child(server)
	if not server.start():
		push_error("Could not bind the contract-declared local Gateway E2E port on IPv4 or IPv6 loopback.")
		quit(1)
		return
	var store := root.get_node("ClientStore") as WalnutClientStore
	store.persistence_enabled = false
	var packed := load("res://scenes/app/app_root.tscn") as PackedScene
	var app := packed.instantiate()
	app.world_presentation_enabled = true
	app.runtime_environment_override = {"YAYA_API_BASE_URL": server.base_url(), "YAYA_AUTH_TOKEN": "e2e-token"}
	app.poller_settings_override = {"deadline_seconds": 5.0, "initial_delay_seconds": 0.0, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_ratio": 0.0}
	var completion := {"done": false, "result": {}}
	app.startup_finished.connect(func(result: Dictionary) -> void:
		completion.done = true
		completion.result = result.duplicate(true)
	)
	root.add_child(app)
	var deadline := Time.get_ticks_msec() + 12000
	while not bool(completion.done) and Time.get_ticks_msec() < deadline:
		await process_frame
	var finished: Dictionary = completion.result
	if (
		finished.get("ok", false) != true
		or not server.failure.is_empty()
		or str(store.authoritative_session.get("session_id", "")) != "session_demo_0001"
		or str(store.active_skill_tuple.get("skill_version_id", "")) != "skillver_demo_0001"
		or str(store.draft.get("draft_id", "")) != "draft_demo_0001"
		or "POST /v1/agent-sessions" not in server.paths
		or "GET /v1/agent-sessions/session_demo_0001" not in server.paths
		or "GET /v1/worlds/world_demo_0001/presentation-events?after_sequence=0&limit=500" not in server.paths
	):
		push_error("Real app_root local HTTP E2E failed: result=%s server=%s paths=%s error=%s" % [str(finished), server.failure, str(server.paths), str(store.last_error)])
		quit(1)
		return
	app.queue_free()
	await process_frame
	await process_frame
	server.paths.clear()
	server.current_session_id = "session_demo_0001"
	store.set_authoritative_bootstrap(server._student_bootstrap())
	var create_request: Dictionary = server._student_bootstrap().session.create_request
	var create_identity: String = AppRootScript._session_create_identity(create_request)
	var lost_response_envelope := {
		"idempotency_key": RequestContextFactory.idempotency_key_for("createAgentSession", create_identity),
		"request": create_request.duplicate(true),
	}
	var persisted_lost_response := store.ensure_pending_operation(
		"agent_session_create",
		create_identity,
		lost_response_envelope,
	)
	if not persisted_lost_response.get("ok", false):
		push_error("Could not establish the Session-create response-loss envelope: %s" % str(persisted_lost_response))
		quit(1)
		return
	var resumed := packed.instantiate()
	resumed.world_presentation_enabled = true
	resumed.runtime_environment_override = {"YAYA_API_BASE_URL": server.base_url(), "YAYA_AUTH_TOKEN": "e2e-token"}
	resumed.poller_settings_override = {"deadline_seconds": 5.0, "initial_delay_seconds": 0.0, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_ratio": 0.0}
	var resumed_completion := {"done": false, "result": {}}
	resumed.startup_finished.connect(func(result: Dictionary) -> void:
		resumed_completion.done = true
		resumed_completion.result = result.duplicate(true)
	)
	root.add_child(resumed)
	deadline = Time.get_ticks_msec() + 8000
	while not bool(resumed_completion.done) and Time.get_ticks_msec() < deadline:
		await process_frame
	if (
		not bool(resumed_completion.result.get("ok", false))
		or "POST /v1/agent-sessions" in server.paths
		or "GET /v1/agent-sessions/session_demo_0001" not in server.paths
		or not store.get_pending_operation("agent_session_create").is_empty()
	):
		push_error("Non-null current_session_id must reconcile and clear the stale Session-create envelope by exact GET only: %s %s pending=%s" % [str(resumed_completion.result), str(server.paths), str(store.pending_operations)])
		quit(1)
		return
	resumed.queue_free()
	server.queue_free()
	print("APP_ROOT_LOCAL_HTTP_E2E_TEST_PASS")
	quit(0)
