extends SceneTree

const ControllerScript := preload("res://autoload/session_controller.gd")


func _initialize() -> void:
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null:
		controller = ControllerScript.new()
		controller.name = "SessionController"
		root.add_child(controller)

	var terminal_local_failures: Array[Dictionary] = [
		_local("PRODUCT_RESPONSE_INVALID"),
		_local("LOCAL_TRANSPORT_JSON_INVALID"),
		_local("LOCAL_TRANSPORT_NOT_CONFIGURED"),
		_local("LOCAL_TRANSPORT_TIMEOUT", false, "SERVER"),
		{"ok": false, "status": 0, "headers": {}, "error": {"retryable": "true"}},
		{"ok": false, "status": "503", "headers": {}, "error": {}},
		{"ok": false, "status": 400, "headers": {}, "error": {"retryable": false}},
		{"ok": true, "status": 503, "headers": {}, "value": {}},
	]
	for result in terminal_local_failures:
		if controller._retryable_result(result):
			push_error("Contract, validation, configuration, or malformed failures must fail closed: %s" % str(result))
			quit(1)
			return

	var explicitly_retryable: Array[Dictionary] = [
		_local("PRODUCT_RESPONSE_INVALID", true),
		_local("LOCAL_TRANSPORT_BUSY"),
		_local("LOCAL_TRANSPORT_NETWORK_ERROR"),
		_local("LOCAL_TRANSPORT_REQUEST_START_FAILED"),
		_local("LOCAL_TRANSPORT_TIMEOUT"),
	]
	for result in explicitly_retryable:
		if not controller._retryable_result(result):
			push_error("Explicit retryability or a bounded transport code must remain retryable: %s" % str(result))
			quit(1)
			return

	for status in [429, 502, 503, 504]:
		var result := {
			"ok": false,
			"status": status,
			"headers": {},
			"error": {"retryable": false},
		}
		if not controller._retryable_result(result):
			push_error("Explicit transient HTTP status must remain retryable: %d" % status)
			quit(1)
			return

	print("SESSION_RETRY_CLASSIFICATION_TEST_PASS")
	quit(0)


func _local(code: String, retryable: Variant = null, scope := "CLIENT_LOCAL") -> Dictionary:
	var error := {
		"scope": scope,
		"code": code,
		"message": "fixture",
	}
	if retryable != null:
		error["retryable"] = retryable
	return {"ok": false, "status": 0, "headers": {}, "error": error}
