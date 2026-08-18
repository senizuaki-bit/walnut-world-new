class_name WalnutAuditedHttpAgentApiTransport
extends "res://addons/yaya_contract_client/http_agent_api_transport.gd"

## Production Frontend audit boundary around the pinned canonical HTTP transport.
## Entries are intentionally bounded and contain no request ids, credentials,
## headers, bodies, response bodies or error messages.

const ATTEMPT_AUDIT_HISTORY_LIMIT := 512

var _attempt_audit_next_sequence := 1
var _attempt_audit_total_started := 0
var _attempt_audit_total_completed := 0
var _attempt_audit_method_counts: Dictionary = {}
var _attempt_audit_operation_counts: Dictionary = {}
var _attempt_audit_history: Array[Dictionary] = []


func execute(operation: String, arguments: Dictionary) -> Dictionary:
	var spec_result: Dictionary = _build_request_spec(operation, arguments)
	var sequence := 0
	if bool(spec_result.get("ok", false)):
		var spec: Dictionary = spec_result.spec
		sequence = _record_attempt_started(operation, int(spec.method), str(spec.path))
	var result: Dictionary = await super.execute(operation, arguments)
	if sequence > 0:
		_record_attempt_completed(
			sequence,
			int(result.get("status", 0)),
			bool(result.get("ok", false)),
		)
	return result


## v0.5 is additive.  Override only the request-spec seam so both audit and
## the inherited hardened HTTP execution path recognize the presentation GET;
## the pinned v0.4 transport file and AgentApiGateway remain byte-identical.
func _build_request_spec(operation: String, arguments: Dictionary) -> Dictionary:
	if operation == "get_product_capabilities":
		return {
			"ok": true,
			"spec": {
				"method": HTTPClient.METHOD_GET,
				"path": "/product-experience/v1/capabilities",
				"has_body": false,
				"body": "",
			},
		}
	if operation == "record_product_patch_decision":
		var base_spec: Dictionary = super._build_request_spec(operation, arguments)
		if not base_spec.get("ok", false):
			return base_spec
		var request_body: Variant = arguments.get("request_body")
		var request: Variant = arguments.get("request")
		var parsed: Variant = _normalize_json_integers(JSON.parse_string(request_body)) if typeof(request_body) == TYPE_STRING else null
		if (
			typeof(request_body) != TYPE_STRING
			or str(request_body).is_empty()
			or not request is Dictionary
			or not parsed is Dictionary
			or parsed != request
			or str(request_body) != JSON.stringify(request)
		):
			return {
				"ok": false,
				"result": _local_failure(
					operation,
					"LOCAL_TRANSPORT_BODY_IDENTITY_INVALID",
					"PatchDecision must send the exact persisted first-attempt JSON body bytes.",
				),
			}
		base_spec.spec["body"] = str(request_body)
		return base_spec
	if operation != "get_world_presentation_events":
		return super._build_request_spec(operation, arguments)
	var path := "/v1/worlds/%s/presentation-events?after_sequence=%s&limit=%s" % [
		_path_argument(arguments, "world_id"),
		_query_argument(arguments, "after_sequence"),
		_query_argument(arguments, "limit"),
	]
	if path.contains("/__invalid_argument__") or path.contains("=__invalid_argument__"):
		return {
			"ok": false,
			"result": _local_failure(
				operation,
				"LOCAL_TRANSPORT_ARGUMENTS_INVALID",
				"The presentation GET is missing a required path or query argument.",
			),
		}
	return {
		"ok": true,
		"spec": {
			"method": HTTPClient.METHOD_GET,
			"path": path,
			"has_body": false,
			"body": "",
		},
	}


func get_attempt_audit() -> Dictionary:
	return {
		"history_limit": ATTEMPT_AUDIT_HISTORY_LIMIT,
		"total_started": _attempt_audit_total_started,
		"total_completed": _attempt_audit_total_completed,
		"history_truncated": _attempt_audit_total_started > _attempt_audit_history.size(),
		"method_counts": _attempt_audit_method_counts.duplicate(true),
		"operation_counts": _attempt_audit_operation_counts.duplicate(true),
		"recent_attempts": _attempt_audit_history.duplicate(true),
	}


func reset_attempt_audit() -> void:
	_attempt_audit_next_sequence = 1
	_attempt_audit_total_started = 0
	_attempt_audit_total_completed = 0
	_attempt_audit_method_counts.clear()
	_attempt_audit_operation_counts.clear()
	_attempt_audit_history.clear()


func _record_attempt_started(operation: String, method: int, path: String) -> int:
	var method_name := _http_method_name(method)
	var sequence := _attempt_audit_next_sequence
	_attempt_audit_next_sequence += 1
	_attempt_audit_total_started += 1
	_attempt_audit_method_counts[method_name] = int(_attempt_audit_method_counts.get(method_name, 0)) + 1
	_attempt_audit_operation_counts[operation] = int(_attempt_audit_operation_counts.get(operation, 0)) + 1
	_attempt_audit_history.append({
		"sequence": sequence,
		"operation": operation,
		"method": method_name,
		"path": path,
		"completed": false,
		"response_status": null,
		"ok": false,
	})
	if _attempt_audit_history.size() > ATTEMPT_AUDIT_HISTORY_LIMIT:
		_attempt_audit_history.pop_front()
	return sequence


func _record_attempt_completed(sequence: int, response_status: int, ok: bool) -> void:
	_attempt_audit_total_completed += 1
	for index in range(_attempt_audit_history.size() - 1, -1, -1):
		if int(_attempt_audit_history[index].get("sequence", 0)) != sequence:
			continue
		_attempt_audit_history[index]["completed"] = true
		_attempt_audit_history[index]["response_status"] = response_status
		_attempt_audit_history[index]["ok"] = ok
		return


func _http_method_name(method: int) -> String:
	match method:
		HTTPClient.METHOD_GET:
			return "GET"
		HTTPClient.METHOD_POST:
			return "POST"
		HTTPClient.METHOD_PUT:
			return "PUT"
		HTTPClient.METHOD_PATCH:
			return "PATCH"
		HTTPClient.METHOD_DELETE:
			return "DELETE"
		HTTPClient.METHOD_HEAD:
			return "HEAD"
		HTTPClient.METHOD_OPTIONS:
			return "OPTIONS"
		_:
			return "METHOD_%d" % method
