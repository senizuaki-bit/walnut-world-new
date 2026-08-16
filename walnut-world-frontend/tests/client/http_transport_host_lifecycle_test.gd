extends SceneTree

const HttpTransport := preload("res://scripts/client/audited_http_agent_api_transport.gd")


func _initialize() -> void:
	var host := Node.new()
	host.name = "OfflineHttpTransportLifecycleHost"
	root.add_child(host)
	var transport := HttpTransport.new(host, "http://127.0.0.1:8790", "offline-lifecycle-token")
	var before_frame: Dictionary = await transport.execute("get_student_bootstrap", {
		"attempt_context": {
			"schema_version": "1.0.0",
			"request_id": "req_transport_lifecycle_0001",
			"trace_id": "trace_transport_lifecycle_0001",
			"correlation_id": "corr_transport_lifecycle_0001",
		},
	})
	if (
		host.is_inside_tree()
		or before_frame.get("ok", true)
		or str(before_frame.get("error", {}).get("code", "")) != "LOCAL_TRANSPORT_HOST_UNAVAILABLE"
		or host.get_child_count() != 0
	):
		_abort("HTTP transport did not fail closed before its host entered the scene tree.")
		return
	transport.reset_attempt_audit()

	await process_frame
	if not is_instance_valid(host) or not host.is_inside_tree() or host.get_child_count() != 0:
		_abort("HTTP transport host did not become available after the first process frame.")
		return

	for index in range(HttpTransport.ATTEMPT_AUDIT_HISTORY_LIMIT + 1):
		var method := HTTPClient.METHOD_PUT if index == 0 else HTTPClient.METHOD_GET
		var operation := "upsert_product_skill_draft" if index == 0 else "get_world_snapshot"
		var path := "/resource/%d" % index
		var sequence: int = transport._record_attempt_started(operation, method, path)
		transport._record_attempt_completed(sequence, 200, true)
	var audit: Dictionary = transport.get_attempt_audit()
	var serialized := JSON.stringify(audit)
	if (
		int(audit.get("total_started", -1)) != HttpTransport.ATTEMPT_AUDIT_HISTORY_LIMIT + 1
		or int(audit.get("total_completed", -1)) != HttpTransport.ATTEMPT_AUDIT_HISTORY_LIMIT + 1
		or not bool(audit.get("history_truncated", false))
		or audit.get("recent_attempts", []).size() != HttpTransport.ATTEMPT_AUDIT_HISTORY_LIMIT
		or int(audit.get("method_counts", {}).get("PUT", 0)) != 1
		or int(audit.get("method_counts", {}).get("GET", 0)) != HttpTransport.ATTEMPT_AUDIT_HISTORY_LIMIT
		or int(audit.get("operation_counts", {}).get("upsert_product_skill_draft", 0)) != 1
		or serialized.contains("offline-lifecycle-token")
		or serialized.contains("Authorization")
	):
		_abort("HTTP attempt audit is not bounded, aggregated, or credential-free.")
		return
	var last_attempt: Dictionary = audit.recent_attempts.back()
	if (
		str(last_attempt.get("operation", "")) != "get_world_snapshot"
		or str(last_attempt.get("method", "")) != "GET"
		or str(last_attempt.get("path", "")) != "/resource/%d" % HttpTransport.ATTEMPT_AUDIT_HISTORY_LIMIT
		or not bool(last_attempt.get("completed", false))
		or int(last_attempt.get("response_status", -1)) != 200
		or not bool(last_attempt.get("ok", false))
	):
		_abort("HTTP attempt audit lost its non-sensitive completion metadata.")
		return
	transport.reset_attempt_audit()
	audit = transport.get_attempt_audit()
	if (
		int(audit.get("total_started", -1)) != 0
		or int(audit.get("total_completed", -1)) != 0
		or not audit.get("method_counts", {}).is_empty()
		or not audit.get("operation_counts", {}).is_empty()
		or not audit.get("recent_attempts", []).is_empty()
	):
		_abort("HTTP attempt audit reset did not clear every bounded counter and entry.")
		return

	transport.shutdown()
	root.remove_child(host)
	host.free()
	print("HTTP_TRANSPORT_HOST_LIFECYCLE_TEST_PASS")
	quit(0)


func _abort(message: String) -> void:
	push_error(message)
	quit(1)
