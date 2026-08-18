class_name CommandPoller
extends RefCounted

## Deadline-bound reconciliation for Commands and terminal resources. Every GET
## receives a fresh RequestContext; a 202 or a non-terminal body is never
## interpreted as completed work.

const RequestContextFactoryScript = preload("res://autoload/request_context_factory.gd")

const DEFAULT_DEADLINE_SECONDS := 30.0
const DEFAULT_INITIAL_DELAY_SECONDS := 0.25
const DEFAULT_BASE_DELAY_SECONDS := 0.25
const DEFAULT_MAX_DELAY_SECONDS := 4.0
const DEFAULT_MAX_ATTEMPTS := 512

signal command_observed(command: Dictionary)
signal command_failed(error: Dictionary)
signal resource_observed(resource: Dictionary)

var _gateway: RefCounted
var _context_factory: Callable
var _waiter: Callable
var _clock_msec: Callable
var _random_unit: Callable
var _settings: Dictionary


func _init(
	gateway: RefCounted,
	context_factory: Callable = Callable(),
	settings: Dictionary = {},
	waiter: Callable = Callable(),
	clock_msec: Callable = Callable(),
	random_unit: Callable = Callable(),
) -> void:
	_gateway = gateway
	_context_factory = context_factory
	_settings = {
		"deadline_seconds": float(settings.get("deadline_seconds", DEFAULT_DEADLINE_SECONDS)),
		"initial_delay_seconds": float(settings.get("initial_delay_seconds", DEFAULT_INITIAL_DELAY_SECONDS)),
		"base_delay_seconds": float(settings.get("base_delay_seconds", DEFAULT_BASE_DELAY_SECONDS)),
		"max_delay_seconds": float(settings.get("max_delay_seconds", DEFAULT_MAX_DELAY_SECONDS)),
		"max_attempts": int(settings.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
		"jitter_ratio": float(settings.get("jitter_ratio", 0.15)),
	}
	_waiter = waiter if waiter.is_valid() else Callable(self, "_wait_seconds")
	_clock_msec = clock_msec if clock_msec.is_valid() else Callable(self, "_now_msec")
	_random_unit = random_unit if random_unit.is_valid() else Callable(self, "_rand_unit")


func reconcile(request_context_seed: Dictionary, submission: Dictionary) -> Dictionary:
	var command_id := _command_id_from_submission(submission)
	if command_id.is_empty():
		return _local_failure(
			"COMMAND_RECONCILIATION_INVALID",
			"The submission has no canonical command_id to reconcile.",
		)
	if _gateway == null or not _gateway.has_method("get_command"):
		return _local_failure(
			"COMMAND_GATEWAY_UNAVAILABLE",
			"The Game Gateway does not expose get_command.",
		)
	var result := await _poll_terminal_resource(
		request_context_seed,
		"get_command",
		command_id,
		"command_id",
		_delay_from_headers(submission.get("headers", {})),
	)
	if result.get("ok", false):
		command_observed.emit(result.value.duplicate(true))
	else:
		command_failed.emit(result.get("error", {}).duplicate(true))
	return result


func poll_resource(
	request_context_seed: Dictionary,
	method_name: String,
	resource_id: String,
	identity_field: String,
	initial_retry_after_seconds: float = -1.0,
) -> Dictionary:
	if resource_id.is_empty() or identity_field.is_empty():
		return _local_failure("RESOURCE_RECONCILIATION_INVALID", "Resource polling identity is incomplete.")
	if _gateway == null or not _gateway.has_method(method_name):
		return _local_failure("RESOURCE_GATEWAY_UNAVAILABLE", "The Game Gateway cannot poll %s." % method_name)
	return await _poll_terminal_resource(
		request_context_seed,
		method_name,
		resource_id,
		identity_field,
		initial_retry_after_seconds,
	)


func _poll_terminal_resource(
	request_context_seed: Dictionary,
	method_name: String,
	resource_id: String,
	identity_field: String,
	initial_retry_after_seconds: float,
) -> Dictionary:
	if (
		_settings.deadline_seconds <= 0.0
		or _settings.base_delay_seconds < 0.0
		or _settings.max_delay_seconds < _settings.base_delay_seconds
		or _settings.max_attempts < 1
	):
		return _local_failure("POLL_CONFIGURATION_INVALID", "Polling settings are invalid.")
	var started_msec := int(_clock_msec.call())
	var deadline_msec := started_msec + ceili(_settings.deadline_seconds * 1000.0)
	var next_delay: float = (
		initial_retry_after_seconds
		if initial_retry_after_seconds >= 0.0
		else _settings.initial_delay_seconds
	)
	var attempts := 0
	while attempts < _settings.max_attempts:
		var before_wait := int(_clock_msec.call())
		if before_wait >= deadline_msec:
			break
		if next_delay > 0.0:
			var remaining_seconds := float(deadline_msec - before_wait) / 1000.0
			if next_delay >= remaining_seconds:
				break
			await _waiter.call(next_delay)
		if int(_clock_msec.call()) >= deadline_msec:
			break

		attempts += 1
		var request_context := _fresh_request_context(request_context_seed)
		var raw_result: Variant = await _gateway.call(method_name, request_context, resource_id)
		if int(_clock_msec.call()) >= deadline_msec:
			break
		if not raw_result is Dictionary:
			return _local_failure("POLL_RESPONSE_INVALID", "The polled Gateway result is not a Dictionary.")
		var result: Dictionary = raw_result
		if not result.get("ok", false):
			if not _retryable(result):
				return result
			next_delay = _next_delay(attempts, result.get("headers", {}))
			continue
		var resource: Variant = result.get("value")
		if (
			not resource is Dictionary
			or str(resource.get(identity_field, "")) != resource_id
			or typeof(resource.get("terminal")) != TYPE_BOOL
		):
			return _local_failure(
				"RESOURCE_RECONCILIATION_INVALID",
				"The polled resource identity or terminal marker is invalid.",
			)
		resource_observed.emit(resource.duplicate(true))
		if method_name == "get_command":
			command_observed.emit(resource.duplicate(true))
		if bool(resource.terminal):
			return {
				"ok": true,
				"status": int(result.get("status", 200)),
				"headers": result.get("headers", {}).duplicate(true),
				"value": resource.duplicate(true),
			}
		next_delay = _next_delay(attempts, result.get("headers", {}))
	return _local_failure(
		"RESOURCE_RECONCILIATION_TIMEOUT",
		"The resource did not reach a terminal state before the total polling deadline.",
		true,
	)


func _fresh_request_context(seed: Dictionary) -> Dictionary:
	if _context_factory.is_valid():
		var provided: Variant = _context_factory.call()
		if provided is Dictionary:
			return provided.duplicate(true)
	var actor: Variant = seed.get("actor")
	var content_ref: Variant = seed.get("content_ref")
	if actor is Dictionary and content_ref is Dictionary:
		return RequestContextFactoryScript.new_attempt(actor, content_ref)
	return seed.duplicate(true)


func _retryable(result: Dictionary) -> bool:
	var status := int(result.get("status", 0))
	var error: Variant = result.get("error")
	if not error is Dictionary:
		return false
	return bool(error.get("retryable", false)) or status in [429, 502, 503, 504] or status == 0


func _next_delay(attempt: int, headers: Variant) -> float:
	var retry_after := _delay_from_headers(headers)
	if retry_after >= 0.0:
		return retry_after
	var exponent := mini(maxi(attempt - 1, 0), 12)
	var delay: float = min(
		_settings.max_delay_seconds,
		_settings.base_delay_seconds * pow(2.0, exponent),
	)
	var jitter_ratio: float = clampf(_settings.jitter_ratio, 0.0, 1.0)
	if delay > 0.0 and jitter_ratio > 0.0:
		var unit := clampf(float(_random_unit.call()), 0.0, 1.0)
		delay *= 1.0 + jitter_ratio * (unit * 2.0 - 1.0)
	return maxf(delay, 0.0)


func _delay_from_headers(headers: Variant) -> float:
	if not headers is Dictionary:
		return -1.0
	var value: Variant = headers.get("retry-after", headers.get("Retry-After"))
	if typeof(value) != TYPE_STRING or value.is_empty():
		return -1.0
	for index in range(value.length()):
		if "0123456789".find(value.substr(index, 1)) < 0:
			return -1.0
	return float(int(value))


func _command_id_from_submission(submission: Dictionary) -> String:
	if submission.get("ok", false):
		var accepted: Variant = submission.get("value")
		if accepted is Dictionary:
			return str(accepted.get("command_id", ""))
	var error: Variant = submission.get("error")
	if error is Dictionary:
		var nested: Variant = error.get("error")
		var code := str(error.get("code", nested.get("code", "") if nested is Dictionary else ""))
		if code == "UNKNOWN_COMMIT_STATE":
			return str(error.get("command_id", ""))
	return ""


func _wait_seconds(seconds: float) -> void:
	await Engine.get_main_loop().create_timer(seconds).timeout


func _now_msec() -> int:
	return Time.get_ticks_msec()


func _rand_unit() -> float:
	return randf()


func _local_failure(code: String, message: String, retryable: bool = false) -> Dictionary:
	return {
		"ok": false,
		"status": 0,
		"headers": {},
		"error": {
			"scope": "CLIENT_LOCAL",
			"code": code,
			"message": message,
			"retryable": retryable,
		},
	}
