extends SceneTree

## A Draft response-loss regression must cross the real persistence and HTTP
## boundaries.  The second ClientStore reloads the exact JSON envelope written
## by the first, then the production transport sends its original raw body and
## Idempotency-Key after the server's first canonical GET proves it is absent.

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")
const HttpTransport := preload("res://addons/yaya_contract_client/http_agent_api_transport.gd")
const ProductGateway := preload("res://scripts/client/product_interaction_gateway.gd")
const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")

const HASH_CONTENT := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const HASH_OLD := "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
const HASH_NEW := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


class DraftRecoveryServer:
	extends Node

	var listener := TCPServer.new()
	var connections: Array[Dictionary] = []
	var get_count := 0
	var put_bodies: Array[String] = []
	var put_idempotency_keys: Array[String] = []
	var failure := ""
	var put_seen := false
	var recovery_mode := ""

	func start() -> bool:
		var error := listener.listen(8790, "127.0.0.1")
		set_process(error == OK)
		return error == OK

	func _exit_tree() -> void:
		listener.stop()

	func _process(_delta: float) -> void:
		while listener.is_connection_available():
			connections.append({"peer": listener.take_connection(), "buffer": PackedByteArray()})
		for index in range(connections.size() - 1, -1, -1):
			var connection: Dictionary = connections[index]
			var peer: StreamPeerTCP = connection.peer
			peer.poll()
			var available := peer.get_available_bytes()
			if available > 0:
				var chunk := peer.get_data(available)
				if chunk[0] != OK:
					failure = "loopback server could not read request bytes"
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
		var parts := lines[0].split(" ")
		if parts.size() < 2:
			failure = "loopback server received a malformed request line"
			return
		var headers := {}
		for line in lines.slice(1):
			var separator := line.find(":")
			if separator > 0:
				headers[line.substr(0, separator).to_lower()] = line.substr(separator + 1).strip_edges()
		var method := str(parts[0])
		var path := str(parts[1])
		var raw_body := request_text.substr(header_end + 4)
		var response := _route(method, path, headers, raw_body)
		var body := JSON.stringify(response.body)
		var wire := "\r\n".join([
			"HTTP/1.1 %d %s" % [int(response.status), "OK" if int(response.status) < 300 else "ERROR"],
			"Content-Type: application/json",
			"Content-Length: %d" % body.to_utf8_buffer().size(),
			"Connection: close",
			"X-Request-Id: %s" % str(headers.get("x-request-id", "missing")),
			"X-Trace-Id: %s" % str(headers.get("x-trace-id", "missing")),
			"X-Correlation-Id: %s" % str(headers.get("x-correlation-id", "missing")),
			"X-Schema-Version: 1.0.0",
		]) + "\r\n\r\n" + body
		peer.put_data(wire.to_utf8_buffer())
		peer.disconnect_from_host()

	func _route(method: String, path: String, headers: Dictionary, raw_body: String) -> Dictionary:
		var expected_path := "/product-experience/v1/sessions/session_demo_0001/skill-drafts/draft_demo_0001"
		if method == "GET" and path == expected_path:
			get_count += 1
			if recovery_mode == "TRANSIENT_GET":
				return {"status": 503, "body": {"code": "DEPENDENCY_UNAVAILABLE"}}
			return {"status": 200, "body": _draft(put_seen)}
		if method == "PUT" and path == expected_path:
			put_bodies.append(raw_body)
			put_idempotency_keys.append(str(headers.get("idempotency-key", "")))
			var parsed: Variant = JSON.parse_string(raw_body)
			if not parsed is Dictionary:
				failure = "loopback server received a non-object Draft PUT body"
				return {"status": 400, "body": {"code": "INVALID"}}
			if recovery_mode == "TERMINAL_PUT":
				return {"status": 409, "body": {"code": "DRAFT_CONFLICT"}}
			put_seen = true
			return {"status": 200, "body": _draft(true)}
		failure = "unexpected Draft recovery route: %s %s" % [method, path]
		return {"status": 404, "body": {"code": "NOT_FOUND"}}

	func _actor() -> Dictionary:
		return {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]}

	func _content_ref() -> Dictionary:
		return {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": HASH_CONTENT}

	func _context() -> Dictionary:
		return {
			"schema_version": "1.0.0",
			"request_id": "req_draft_recovery_server_0001",
			"correlation_id": "corr_draft_recovery_server_0001",
			"trace_id": "trace_draft_recovery_server_0001",
			"requested_at": "2026-08-12T00:00:00Z",
			"actor": _actor(),
			"content_ref": _content_ref(),
		}

	func _draft(saved: bool) -> Dictionary:
		var source := "int main(){return 7;}\n" if saved else "int main(){return 0;}\n"
		var draft := {
			"request_context": _context(),
			"session_id": "session_demo_0001",
			"draft_id": "draft_demo_0001",
			"skill_id": "skill_demo_0001",
			"revision": 2 if saved else 1,
			"content_ref": _content_ref(),
			"display_name": "Demo",
			"source_bundle": {
				"language": "CPP20",
				"entrypoint": "src/main.cpp",
				"files": [{"path": "src/main.cpp", "content": source, "content_sha256": source.sha256_text()}],
			},
			"draft_sha256": "",
			"created_at": "2026-08-12T00:00:00Z",
			"updated_at": "2026-08-12T00:00:01Z",
			"last_applied_patch_id": null,
			"links": {
				"self": "/product-experience/v1/sessions/session_demo_0001/skill-drafts/draft_demo_0001",
				"session_workspace": "/product-experience/v1/sessions/session_demo_0001/workspace",
				"builds": "/v1/skill-builds",
			},
		}
		draft.draft_sha256 = ContractValidator.canonical_json_sha256_v1({
			"session_id": draft.session_id,
			"draft_id": draft.draft_id,
			"skill_id": draft.skill_id,
			"content_ref": draft.content_ref,
			"display_name": draft.display_name,
			"source_bundle": draft.source_bundle,
		})
		return draft


func _initialize() -> void:
	var server := DraftRecoveryServer.new()
	root.add_child(server)
	if not server.start():
		_abort("Could not bind the loopback HTTP Draft recovery server.")
		return
	var persistence_path := "user://pending_draft_restart_recovery_http_test.json"
	var absolute_path := ProjectSettings.globalize_path(persistence_path)
	if FileAccess.file_exists(persistence_path):
		DirAccess.remove_absolute(absolute_path)
	var original_request := _original_request()
	var original_body := JSON.stringify(original_request)
	var original_identity := _draft_identity(original_request)
	var original_key := RequestContextFactory.idempotency_key_for("upsertProductSkillDraft", original_identity)

	var first := await _replace_store(StoreScript.new())
	first.configure_persistence(persistence_path, true, false)
	first.bind_authority("http://127.0.0.1:8790", _bootstrap())
	first.set_authoritative_bootstrap(_bootstrap())
	first.set_authoritative_session(_session())
	first.ensure_pending_operation("draft_save", original_identity, {
		"idempotency_key": original_key,
		"request": original_request,
	})
	root.remove_child(first)
	first.free()

	var restored := await _replace_store(StoreScript.new())
	if not restored.configure_persistence(persistence_path, true, true):
		_abort("The second ClientStore could not load the persisted Draft envelope.")
		return
	var restored_envelope := restored.get_pending_operation("draft_save")
	if restored_envelope.is_empty() or JSON.stringify(restored_envelope.request) != original_body:
		_abort("The second ClientStore did not restore the exact persisted Draft request body.")
		return

	var controller := await _replace_controller(ControllerScript.new())
	var transport := HttpTransport.new(controller, "http://127.0.0.1:8790", "draft-recovery-token")
	controller.configure(null, ProductGateway.new(transport))
	controller.configure_authority(_bootstrap(), _session())
	var recovery: Dictionary = await controller.recover_pending_draft_save_operations()
	if (
		not recovery.get("ok", false)
		or str(recovery.get("value", {}).get("outcome", "")) != "REPLAYED_AND_VERIFIED"
		or not restored.get_pending_operation("draft_save").is_empty()
		or server.failure != ""
		or server.get_count != 2
		or server.put_bodies != [original_body]
		or server.put_idempotency_keys != [original_key]
		or int(restored.draft.get("revision", -1)) != 2
		or str(restored.local_source) != "int main(){return 7;}\n"
	):
		_abort("Cross-process Draft recovery did not send the exact raw PUT/key or close the canonical save: %s" % str(recovery))
		return
	# A response may have been lost after the server committed.  The first GET
	# must recognize that state and clear the envelope without a second PUT.
	restored.ensure_pending_operation("draft_save", original_identity, {
		"idempotency_key": original_key,
		"request": original_request,
	})
	var existing_recovery: Dictionary = await controller.recover_pending_draft_save_operations()
	if (
		not existing_recovery.get("ok", false)
		or str(existing_recovery.get("value", {}).get("outcome", "")) != "RECONCILED_EXISTING"
		or not restored.get_pending_operation("draft_save").is_empty()
		or server.get_count != 3
		or server.put_bodies.size() != 1
	):
		_abort("Committed Draft response-loss recovery did not clear from the first canonical GET: %s" % str(existing_recovery))
		return

	# A temporary authority read keeps the original envelope untouched.  A later
	# authoritative 409 is terminal: it clears that exact envelope and returns a
	# typed terminal error for AppRoot to surface before READY.
	restored.ensure_pending_operation("draft_save", original_identity, {
		"idempotency_key": original_key,
		"request": original_request,
	})
	server.recovery_mode = "TRANSIENT_GET"
	var transient_recovery: Dictionary = await controller.recover_pending_draft_save_operations()
	if transient_recovery.get("ok", true) or restored.get_pending_operation("draft_save").is_empty():
		_abort("Transient Draft authority failure must preserve the original envelope.")
		return
	server.recovery_mode = "TERMINAL_PUT"
	server.put_seen = false
	var terminal_recovery: Dictionary = await controller.recover_pending_draft_save_operations()
	var terminal_error: Variant = terminal_recovery.get("value", {}).get("terminal_error")
	if (
		not terminal_recovery.get("ok", false)
		or not terminal_error is Dictionary
		or str(terminal_error.get("code", "")) != "DRAFT_RECOVERY_PUT_TERMINAL_FAILURE"
		or not restored.get_pending_operation("draft_save").is_empty()
		or server.put_bodies.size() != 2
		or server.put_idempotency_keys.back() != original_key
		or server.put_bodies.back() != original_body
	):
		_abort("Terminal Draft authority failure must clear and report the exact persisted envelope: %s" % str(terminal_recovery))
		return

	DirAccess.remove_absolute(absolute_path)
	server.queue_free()
	print("PENDING_DRAFT_RESTART_RECOVERY_HTTP_TEST_PASS")
	quit(0)


func _replace_store(store: WalnutClientStore) -> WalnutClientStore:
	var existing := root.get_node_or_null("ClientStore")
	if existing != null:
		root.remove_child(existing)
		existing.free()
	store.name = "ClientStore"
	store.persistence_enabled = false
	root.add_child(store)
	await process_frame
	return store


func _replace_controller(controller: Node) -> Node:
	var existing := root.get_node_or_null("SessionController")
	if existing != null:
		root.remove_child(existing)
		existing.free()
	controller.name = "SessionController"
	root.add_child(controller)
	await process_frame
	return controller


func _bootstrap() -> Dictionary:
	return {
		"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]},
		"content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": HASH_CONTENT},
		"activation": {
			"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"},
			"registry_revision": 0,
			"active": null,
		},
	}


func _session() -> Dictionary:
	return {"session_id": "session_demo_0001", "world_id": "world_demo_0001", "learner_id": "learner_demo_0001", "agent_profile_id": "profile_demo_0001", "channel": "GAME", "content": _bootstrap().content}


func _original_request() -> Dictionary:
	var source := "int main(){return 7;}\n"
	var original_source := "int main(){return 0;}\n"
	return {
		"session_id": "session_demo_0001",
		"draft_id": "draft_demo_0001",
		"skill_id": "skill_demo_0001",
		"content_ref": _bootstrap().content,
		"base_revision": 1,
		"base_draft_sha256": _draft_hash(original_source),
		"display_name": "Demo",
		"source_bundle": {
			"language": "CPP20",
			"entrypoint": "src/main.cpp",
			"files": [{"path": "src/main.cpp", "content": source, "content_sha256": source.sha256_text()}],
		},
		"client_saved_at": "2026-08-12T00:00:00Z",
	}


func _draft_hash(source: String) -> String:
	return ContractValidator.canonical_json_sha256_v1({
		"session_id": "session_demo_0001",
		"draft_id": "draft_demo_0001",
		"skill_id": "skill_demo_0001",
		"content_ref": _bootstrap().content,
		"display_name": "Demo",
		"source_bundle": {
			"language": "CPP20",
			"entrypoint": "src/main.cpp",
			"files": [{"path": "src/main.cpp", "content": source, "content_sha256": source.sha256_text()}],
		},
	})


func _draft_identity(request: Dictionary) -> String:
	var parts: Array[String] = []
	for file: Dictionary in request.source_bundle.files:
		parts.append("%s:%s" % [str(file.path), str(file.content_sha256)])
	parts.sort()
	return "%s:%s:%s" % [
		str(request.draft_id),
		str(request.base_revision),
		"|".join(parts).sha256_text(),
	]


func _abort(message: String) -> void:
	push_error("PENDING_DRAFT_RESTART_RECOVERY_HTTP_TEST_FAIL: %s" % message)
	quit(1)
