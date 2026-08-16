class_name YayaHttpAgentApiTransport
extends "res://agent_api_transport.gd"

## Production REST adapter for the Game API.
##
## One HTTPRequest node is created per attempt so independent calls can overlap.
## The adapter owns wire concerns only: operation mapping, authentication,
## attempt headers, JSON decoding and network lifecycle. Contract validation
## remains exclusively in YayaAgentApiGateway/YayaAgentContractValidator.

const DEFAULT_TIMEOUT_SECONDS := 15.0
const DEFAULT_MAX_IN_FLIGHT := 8
const DEFAULT_MAX_RESPONSE_BYTES := 8 * 1024 * 1024
const MAX_SAFE_JSON_INTEGER := 9007199254740991.0
const PRODUCTION_SCHEME := "https"
const LOOPBACK_HTTP_SCHEME := "http"
const LOOPBACK_HTTP_HOSTS := ["127.0.0.1", "localhost"]
const LOOPBACK_HTTP_PORT := 8790
const StrictJsonObjectScanner = preload("res://strict_json_object_scanner.gd")

class PendingAttempt:
	extends RefCounted
	signal finished(result: Dictionary)
	var request_node: HTTPRequest
	var operation: String
	var completed := false


var _host: Node
var _base_url_input: String
var _base_url: String
var _bearer_token: String
var _timeout_seconds: float
var _max_in_flight: int
var _max_response_bytes: int
var _configuration_error := ""
var _pending: Dictionary = {}
var _shutting_down := false


func _init(
	host: Node,
	base_url: String,
	bearer_token: String,
	timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
	max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
	max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> void:
	_host = host
	_base_url_input = base_url
	_base_url = _normalize_base_url(base_url)
	_bearer_token = bearer_token.strip_edges()
	_timeout_seconds = timeout_seconds
	_max_in_flight = max_in_flight
	_max_response_bytes = max_response_bytes
	_configuration_error = _validate_configuration(_base_url_input, bearer_token)
	if is_instance_valid(_host) and not _host.tree_exiting.is_connected(_on_host_tree_exiting):
		_host.tree_exiting.connect(_on_host_tree_exiting, CONNECT_ONE_SHOT)


func execute(operation: String, arguments: Dictionary) -> Dictionary:
	if not _configuration_error.is_empty():
		return _local_failure(operation, "LOCAL_TRANSPORT_NOT_CONFIGURED", _configuration_error)
	if _shutting_down:
		return _local_failure(operation, "LOCAL_TRANSPORT_SHUTDOWN", "The HTTP transport is shutting down.")
	if not is_instance_valid(_host) or not _host.is_inside_tree():
		return _local_failure(operation, "LOCAL_TRANSPORT_HOST_UNAVAILABLE", "The HTTP transport host is not inside the scene tree.")
	if _pending.size() >= _max_in_flight:
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_BUSY",
			"The HTTP transport reached its configured in-flight request limit.",
			true,
			"DEPENDENCY",
		)

	var context_result := _extract_attempt_context(operation, arguments)
	if not context_result.ok:
		return context_result.result
	var attempt_context: Dictionary = context_result.context
	var request_id: String = attempt_context.request_id
	if _pending.has(request_id):
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_DUPLICATE_REQUEST",
			"An HTTP attempt with the same request_id is already in flight.",
		)

	var spec_result := _build_request_spec(operation, arguments)
	if not spec_result.ok:
		return spec_result.result
	var spec: Dictionary = spec_result.spec
	var headers_result := _build_headers(operation, arguments, attempt_context, spec.has_body)
	if not headers_result.ok:
		return headers_result.result

	var pending := PendingAttempt.new()
	pending.operation = operation
	pending.request_node = HTTPRequest.new()
	pending.request_node.timeout = _timeout_seconds
	pending.request_node.body_size_limit = _max_response_bytes
	pending.request_node.accept_gzip = false
	# Never let an allowed origin forward Authorization to a redirect target.
	# Redirects are returned as an explicit local failure and must be resolved by
	# trusted configuration before a new authenticated attempt is constructed.
	pending.request_node.max_redirects = 0
	# HTTPRequest is asynchronous with or without worker threads. Keeping this
	# disabled avoids a second threading model while still never blocking a frame.
	pending.request_node.use_threads = false
	_host.add_child(pending.request_node)
	_pending[request_id] = pending
	pending.request_node.request_completed.connect(
		_on_request_completed.bind(request_id),
		CONNECT_ONE_SHOT,
	)

	var start_error := pending.request_node.request(
		_base_url + spec.path,
		headers_result.headers,
		spec.method,
		spec.body,
	)
	if start_error != OK:
		_pending.erase(request_id)
		pending.request_node.queue_free()
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_REQUEST_START_FAILED",
			"Godot could not start the HTTP request (error %d)." % start_error,
			true,
			"DEPENDENCY",
		)

	var result: Dictionary = await pending.finished
	return result


func cancel(request_id: String) -> bool:
	var pending: Variant = _pending.get(request_id)
	if not pending is PendingAttempt or pending.completed:
		return false
	pending.request_node.cancel_request()
	_complete(
		request_id,
		_local_failure(
			pending.operation,
			"LOCAL_TRANSPORT_CANCELLED",
			"The HTTP attempt was cancelled by the caller.",
		),
	)
	return true


func shutdown() -> void:
	if _shutting_down:
		return
	_shutting_down = true
	for request_id in _pending.keys():
		var pending: PendingAttempt = _pending[request_id]
		pending.request_node.cancel_request()
		_complete(
			request_id,
			_local_failure(
				pending.operation,
				"LOCAL_TRANSPORT_SHUTDOWN",
				"The HTTP transport shut down before the request completed.",
			),
		)


func set_bearer_token(bearer_token: String) -> bool:
	var normalized := bearer_token.strip_edges()
	if normalized.is_empty() or _contains_header_break(normalized):
		return false
	_bearer_token = normalized
	_configuration_error = _validate_configuration(_base_url_input, normalized)
	return true


func in_flight_count() -> int:
	return _pending.size()


func _on_request_completed(
	result_code: int,
	response_code: int,
	response_headers: PackedStringArray,
	body: PackedByteArray,
	request_id: String,
) -> void:
	var pending: Variant = _pending.get(request_id)
	if not pending is PendingAttempt or pending.completed:
		return
	var result := _decode_response(
		pending.operation,
		result_code,
		response_code,
		response_headers,
		body,
	)
	_complete(request_id, result)


func _complete(request_id: String, result: Dictionary) -> void:
	var pending: Variant = _pending.get(request_id)
	if not pending is PendingAttempt or pending.completed:
		return
	pending.completed = true
	_pending.erase(request_id)
	if is_instance_valid(pending.request_node):
		pending.request_node.queue_free()
	pending.finished.emit(result)


func _decode_response(
	operation: String,
	result_code: int,
	response_code: int,
	response_headers: PackedStringArray,
	body: PackedByteArray,
) -> Dictionary:
	if result_code == HTTPRequest.RESULT_TIMEOUT:
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_TIMEOUT",
			"The HTTP attempt exceeded the configured timeout.",
			true,
			"DEPENDENCY",
		)
	if result_code == HTTPRequest.RESULT_BODY_SIZE_LIMIT_EXCEEDED:
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_RESPONSE_TOO_LARGE",
			"The HTTP response exceeded the configured byte limit.",
		)
	if result_code != HTTPRequest.RESULT_SUCCESS:
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_NETWORK_ERROR",
			"The HTTP attempt failed before a complete response was received (result %d)." % result_code,
			true,
			"DEPENDENCY",
		)
	if response_code < 200 or (response_code > 299 and response_code < 400) or response_code > 599:
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_UNEXPECTED_STATUS",
			"The server returned an unsupported HTTP status (%d)." % response_code,
		)

	var normalized_headers_result := _normalize_response_headers(operation, response_headers)
	if not normalized_headers_result.ok:
		return normalized_headers_result.result
	var json := JSON.new()
	var response_text := body.get_string_from_utf8()
	if response_text.to_utf8_buffer() != body:
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_JSON_INVALID",
			"The server response body is not valid UTF-8.",
		)
	var strict_json_result := StrictJsonObjectScanner.new().inspect(response_text)
	if strict_json_result.ill_formed_unicode_found:
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_JSON_INVALID",
			"The server response contains an unpaired UTF-16 surrogate.",
		)
	if not strict_json_result.ok:
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_JSON_INVALID",
			"The server response could not be inspected as strict JSON.",
		)
	if strict_json_result.duplicate_found:
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_JSON_DUPLICATE_KEY",
			"The server response contains a duplicate JSON object key (%s)." % strict_json_result.duplicate_key,
		)
	var parse_error := json.parse(response_text)
	if parse_error != OK:
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_JSON_INVALID",
			"The server response is not valid JSON (line %d: %s)." % [json.get_error_line(), json.get_error_message()],
		)
	if not json.data is Dictionary:
		return _local_failure(
			operation,
			"LOCAL_TRANSPORT_JSON_SHAPE_INVALID",
			"The server response JSON must be an object.",
		)
	var normalized_json_data: Dictionary = _normalize_json_integers(json.data)

	if response_code >= 200 and response_code <= 299:
		return {
			"ok": true,
			"status": response_code,
			"headers": normalized_headers_result.headers,
			"value": normalized_json_data,
		}
	return {
		"ok": false,
		"status": response_code,
		"headers": normalized_headers_result.headers,
		"error": normalized_json_data,
	}


func _normalize_json_integers(value: Variant) -> Variant:
	match typeof(value):
		TYPE_FLOAT:
			if (
				is_finite(value)
				and value == floor(value)
				and value >= -MAX_SAFE_JSON_INTEGER
				and value <= MAX_SAFE_JSON_INTEGER
			):
				return int(value)
		TYPE_ARRAY:
			var normalized_array: Array = []
			for item in value:
				normalized_array.push_back(_normalize_json_integers(item))
			return normalized_array
		TYPE_DICTIONARY:
			var normalized_dictionary: Dictionary = {}
			for key in value:
				normalized_dictionary[key] = _normalize_json_integers(value[key])
			return normalized_dictionary
	return value


func _normalize_response_headers(operation: String, raw_headers: PackedStringArray) -> Dictionary:
	var normalized := {}
	for raw_header in raw_headers:
		var separator := raw_header.find(":")
		if separator <= 0:
			return {
				"ok": false,
				"result": _local_failure(
					operation,
					"LOCAL_TRANSPORT_RESPONSE_HEADERS_INVALID",
					"The server returned a malformed HTTP response header.",
				),
			}
		var name := raw_header.substr(0, separator).strip_edges().to_lower()
		var value := raw_header.substr(separator + 1).strip_edges()
		if name.is_empty() or _contains_header_break(name) or _contains_header_break(value):
			return {
				"ok": false,
				"result": _local_failure(
					operation,
					"LOCAL_TRANSPORT_RESPONSE_HEADERS_INVALID",
					"The server returned an unsafe HTTP response header.",
				),
			}
		if normalized.has(name):
			# Preserve repeated values instead of silently taking the last one.
			normalized[name] = "%s, %s" % [normalized[name], value]
		else:
			normalized[name] = value
	return {"ok": true, "headers": normalized}


func _extract_attempt_context(operation: String, arguments: Dictionary) -> Dictionary:
	var context: Variant = arguments.get("attempt_context", arguments.get("request_context"))
	if not context is Dictionary:
		return {
			"ok": false,
			"result": _local_failure(operation, "LOCAL_TRANSPORT_ARGUMENTS_INVALID", "The attempt context is missing."),
		}
	for field in ["request_id", "trace_id", "correlation_id", "schema_version"]:
		if not context.has(field) or typeof(context[field]) != TYPE_STRING or context[field].is_empty():
			return {
				"ok": false,
				"result": _local_failure(
					operation,
					"LOCAL_TRANSPORT_ARGUMENTS_INVALID",
					"The attempt context field %s is missing or invalid." % field,
				),
			}
		if _contains_header_break(context[field]):
			return {
				"ok": false,
				"result": _local_failure(
					operation,
					"LOCAL_TRANSPORT_ARGUMENTS_INVALID",
					"The attempt context contains an unsafe header value.",
				),
			}
	return {"ok": true, "context": context}


func _build_headers(
	operation: String,
	arguments: Dictionary,
	attempt_context: Dictionary,
	has_body: bool,
) -> Dictionary:
	var headers := PackedStringArray([
		"Authorization: Bearer %s" % _bearer_token,
		"Accept: application/json",
		"X-Request-Id: %s" % attempt_context.request_id,
		"X-Trace-Id: %s" % attempt_context.trace_id,
		"X-Correlation-Id: %s" % attempt_context.correlation_id,
		"X-Schema-Version: %s" % attempt_context.schema_version,
	])
	if has_body:
		headers.push_back("Content-Type: application/json; charset=utf-8")
		var idempotency_key: Variant = arguments.get("idempotency_key")
		if typeof(idempotency_key) != TYPE_STRING or idempotency_key.is_empty() or _contains_header_break(idempotency_key):
			return {
				"ok": false,
				"result": _local_failure(
					operation,
					"LOCAL_TRANSPORT_ARGUMENTS_INVALID",
					"A safe Idempotency-Key is required for write operations.",
				),
			}
		headers.push_back("Idempotency-Key: %s" % idempotency_key)
	return {"ok": true, "headers": headers}


func _build_request_spec(operation: String, arguments: Dictionary) -> Dictionary:
	var method := HTTPClient.METHOD_GET
	var path := ""
	var body_value: Variant = null
	match operation:
		"get_bootstrap":
			path = "/v1/bootstrap"
		"get_student_bootstrap":
			path = "/v1/student-bootstrap"
		"submit_skill_build":
			method = HTTPClient.METHOD_POST
			path = "/v1/skill-builds"
			body_value = arguments.get("request")
		"get_skill_build":
			path = "/v1/skill-builds/%s" % _path_argument(arguments, "build_id")
		"activate_skill_version":
			method = HTTPClient.METHOD_POST
			path = "/v1/skill-versions/%s/activations" % _path_argument(arguments, "skill_version_id")
			body_value = arguments.get("request")
		"get_skill_activation":
			path = "/v1/skill-activations/%s" % _path_argument(arguments, "activation_id")
		"create_agent_session":
			method = HTTPClient.METHOD_POST
			path = "/v1/agent-sessions"
			body_value = arguments.get("request")
		"get_agent_session":
			path = "/v1/agent-sessions/%s" % _path_argument(arguments, "session_id")
		"submit_agent_turn":
			method = HTTPClient.METHOD_POST
			path = "/v1/agent-sessions/%s/turns" % _path_argument(arguments, "session_id")
			body_value = arguments.get("request")
		"get_command":
			path = "/v1/commands/%s" % _path_argument(arguments, "command_id")
		"get_run":
			path = "/v1/runs/%s" % _path_argument(arguments, "run_id")
		"get_world_snapshot":
			path = "/v1/worlds/%s/snapshot" % _path_argument(arguments, "world_id")
		"get_world_events":
			path = "/v1/worlds/%s/events?after_sequence=%s&limit=%s" % [
				_path_argument(arguments, "world_id"),
				_query_argument(arguments, "after_sequence"),
				_query_argument(arguments, "limit"),
			]
		"upload_client_events":
			method = HTTPClient.METHOD_POST
			path = "/v1/client-events:batch"
			body_value = arguments.get("batch")
		"get_evidence":
			path = "/v1/evidence/%s" % _path_argument(arguments, "evidence_id")
		_:
			return {
				"ok": false,
				"result": _local_failure(
					operation,
					"LOCAL_TRANSPORT_OPERATION_UNSUPPORTED",
					"The HTTP transport does not recognize this operation.",
				),
			}

	if path.contains("/__invalid_argument__") or path.contains("=__invalid_argument__"):
		return {
			"ok": false,
			"result": _local_failure(
				operation,
				"LOCAL_TRANSPORT_ARGUMENTS_INVALID",
				"The HTTP transport is missing a required path or query argument.",
			),
		}
	var has_body := method == HTTPClient.METHOD_POST
	if has_body and not body_value is Dictionary:
		return {
			"ok": false,
			"result": _local_failure(
				operation,
				"LOCAL_TRANSPORT_ARGUMENTS_INVALID",
				"The HTTP transport write body must be a Dictionary.",
			),
		}
	return {
		"ok": true,
		"spec": {
			"method": method,
			"path": path,
			"has_body": has_body,
			"body": JSON.stringify(body_value) if has_body else "",
		},
	}


func _path_argument(arguments: Dictionary, name: String) -> String:
	var value: Variant = arguments.get(name)
	if typeof(value) != TYPE_STRING or value.is_empty():
		return "__invalid_argument__"
	return value.uri_encode()


func _query_argument(arguments: Dictionary, name: String) -> String:
	var value: Variant = arguments.get(name)
	if typeof(value) == TYPE_INT:
		return String.num_int64(value).uri_encode()
	if typeof(value) == TYPE_STRING and not value.is_empty():
		return value.uri_encode()
	return "__invalid_argument__"


func _validate_configuration(original_base_url: String, original_bearer_token: String) -> String:
	if not is_instance_valid(_host):
		return "A live scene-tree host Node is required."
	var base_url_result := _parse_base_url(original_base_url)
	if not base_url_result.ok:
		return base_url_result.error
	var parsed: Dictionary = base_url_result.value
	if parsed.scheme == LOOPBACK_HTTP_SCHEME:
		if parsed.host not in LOOPBACK_HTTP_HOSTS:
			return "Plaintext HTTP is restricted to an explicit loopback Mock host."
		if not parsed.has_port or parsed.port != LOOPBACK_HTTP_PORT:
			return "The loopback HTTP Mock must use its contract-declared port."
		if not parsed.path.is_empty() and parsed.path != "/":
			return "The loopback HTTP Mock base_url must not contain a path."
	elif parsed.scheme != PRODUCTION_SCHEME:
		return "base_url must use HTTPS, except for the contract-declared loopback HTTP Mock."
	if _bearer_token.is_empty() or _contains_header_break(original_bearer_token):
		return "A safe non-empty bearer token is required."
	if _timeout_seconds <= 0.0:
		return "timeout_seconds must be greater than zero."
	if _max_in_flight < 1:
		return "max_in_flight must be at least one."
	if _max_response_bytes < 1:
		return "max_response_bytes must be at least one."
	return ""


func _parse_base_url(original_value: String) -> Dictionary:
	if original_value.is_empty() or original_value != original_value.strip_edges():
		return _invalid_base_url("base_url must be non-empty and contain no surrounding whitespace.")
	if _contains_unsafe_url_character(original_value):
		return _invalid_base_url("base_url contains whitespace, a control character, or a backslash.")
	if original_value.contains("?") or original_value.contains("#"):
		return _invalid_base_url("base_url must not contain a query or fragment.")

	var scheme := ""
	if original_value.begins_with("%s://" % PRODUCTION_SCHEME):
		scheme = PRODUCTION_SCHEME
	elif original_value.begins_with("%s://" % LOOPBACK_HTTP_SCHEME):
		scheme = LOOPBACK_HTTP_SCHEME
	else:
		return _invalid_base_url("base_url must use a canonical http:// or https:// scheme.")

	var authority_start := scheme.length() + 3
	var path_start := original_value.find("/", authority_start)
	var authority := original_value.substr(
		authority_start,
		original_value.length() - authority_start if path_start < 0 else path_start - authority_start,
	)
	var path := "" if path_start < 0 else original_value.substr(path_start)
	if authority.is_empty():
		return _invalid_base_url("base_url must include a host.")
	if authority.contains("@"):
		return _invalid_base_url("base_url userinfo is forbidden.")

	var authority_result := _parse_authority(authority)
	if not authority_result.ok:
		return authority_result
	var value: Dictionary = authority_result.value
	value.scheme = scheme
	value.path = path
	return {"ok": true, "value": value}


func _parse_authority(authority: String) -> Dictionary:
	var host := ""
	var port_text := ""
	var has_port := false
	if authority.begins_with("["):
		var closing_bracket := authority.find("]")
		if closing_bracket <= 1 or authority.find("[", 1) >= 0 or authority.find("]", closing_bracket + 1) >= 0:
			return _invalid_base_url("base_url contains an invalid IP-literal authority.")
		host = authority.substr(1, closing_bracket - 1)
		if not host.contains(":") or not host.is_valid_ip_address():
			return _invalid_base_url("base_url contains an invalid IPv6 host.")
		var suffix := authority.substr(closing_bracket + 1)
		if not suffix.is_empty():
			if not suffix.begins_with(":"):
				return _invalid_base_url("base_url contains characters after its IP literal.")
			has_port = true
			port_text = suffix.substr(1)
	else:
		if authority.contains("[") or authority.contains("]"):
			return _invalid_base_url("base_url contains unmatched authority brackets.")
		var colon := authority.rfind(":")
		if colon >= 0:
			if authority.find(":") != colon:
				return _invalid_base_url("An IPv6 base_url host must use brackets.")
			host = authority.substr(0, colon)
			has_port = true
			port_text = authority.substr(colon + 1)
		else:
			host = authority
		if not _is_valid_ascii_host(host):
			return _invalid_base_url("base_url contains an invalid or ambiguous host.")

	var port := -1
	if has_port:
		if port_text.is_empty() or port_text.length() > 5 or not _is_ascii_digits(port_text):
			return _invalid_base_url("base_url contains an invalid port.")
		if port_text.length() > 1 and port_text.begins_with("0"):
			return _invalid_base_url("base_url port must use canonical decimal form.")
		port = int(port_text)
		if port < 1 or port > 65535:
			return _invalid_base_url("base_url port must be between 1 and 65535.")
	return {
		"ok": true,
		"value": {
			"host": host.to_lower(),
			"has_port": has_port,
			"port": port,
		},
	}


func _is_valid_ascii_host(host: String) -> bool:
	if host.is_empty() or host.length() > 253 or host.begins_with(".") or host.ends_with("."):
		return false
	if host.is_valid_ip_address():
		return not host.contains(":")
	for label in host.split(".", true):
		if label.is_empty() or label.length() > 63 or label.begins_with("-") or label.ends_with("-"):
			return false
		for index in range(label.length()):
			if "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-".find(label.substr(index, 1)) < 0:
				return false
	return true


func _contains_unsafe_url_character(value: String) -> bool:
	for index in range(value.length()):
		var codepoint := value.unicode_at(index)
		if codepoint <= 32 or codepoint == 127 or value.substr(index, 1) == "\\":
			return true
	return false


func _is_ascii_digits(value: String) -> bool:
	if value.is_empty():
		return false
	for index in range(value.length()):
		if "0123456789".find(value.substr(index, 1)) < 0:
			return false
	return true


func _invalid_base_url(message: String) -> Dictionary:
	return {"ok": false, "error": message}


func _normalize_base_url(value: String) -> String:
	var normalized := value.strip_edges()
	while normalized.ends_with("/"):
		normalized = normalized.left(-1)
	return normalized


func _contains_header_break(value: String) -> bool:
	return value.contains("\r") or value.contains("\n")


func _on_host_tree_exiting() -> void:
	shutdown()
