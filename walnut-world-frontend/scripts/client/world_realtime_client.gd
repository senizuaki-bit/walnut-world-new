class_name WorldRealtimeClient
extends Node

## Contract-bound client for /v1/realtime. It never treats a received event as
## durable: ACK is emitted only after the presentation/state owner confirms the
## exact contiguous event was applied.

const ContractValidator = preload("res://addons/yaya_contract_client/contract_validator.gd")
const PROTOCOL_VERSION := "1.0.0"

signal connection_state_changed(state: String)
signal frame_outbound(frame: Dictionary)
signal event_available(event: Dictionary)
signal recovery_required(after_sequence: int)
signal protocol_rejected(reason: String)
signal server_error(error: Dictionary)

## A platform adapter must provide open_stream(url, headers, subprotocol), poll(),
## has_next_frame(), next_frame(), send_frame(frame), and close(). Godot's
## built-in WebSocketPeer cannot set the contract-required Upgrade headers.
var _transport: RefCounted
var _stream_url := ""
var _stream_id := ""
var _request_context: Dictionary = {}
var _subscription_id := ""
var _durable_sequence := 0
var _durable_event_id := ""
var _awaiting_recovery := false
var _sent_open_frame := false


func start(stream_url: String, stream_id: String, request_context: Dictionary, bearer_token: String, after_sequence: int) -> bool:
	if not _valid_stream_setup(stream_url, stream_id, request_context, after_sequence):
		return false
	if _transport == null or not _transport.has_method("open_stream"):
		protocol_rejected.emit("Realtime transport with authenticated Upgrade-header support is not configured.")
		return false
	_stream_url = stream_url
	_stream_id = stream_id
	_request_context = request_context.duplicate(true)
	_durable_sequence = after_sequence
	_durable_event_id = ""
	_subscription_id = ""
	_awaiting_recovery = false
	_sent_open_frame = false
	var headers := PackedStringArray([
		"Authorization: Bearer %s" % bearer_token,
		"X-Request-Id: %s" % request_context.request_id,
		"X-Trace-Id: %s" % request_context.trace_id,
		"X-Correlation-Id: %s" % request_context.correlation_id,
		"X-Schema-Version: %s" % PROTOCOL_VERSION,
		"X-Stream-Protocol-Version: %s" % PROTOCOL_VERSION,
	])
	var connected: Variant = _transport.open_stream(stream_url, headers, "yaya.runtime.v1")
	if connected != true:
		protocol_rejected.emit("WebSocket connection could not be started.")
		return false
	connection_state_changed.emit("CONNECTING")
	return true


func _process(_delta: float) -> void:
	if _transport == null:
		return
	_transport.poll()
	if not _sent_open_frame:
		_sent_open_frame = true
		_send_open_frame()
	while _transport.has_next_frame():
		accept_server_frame(_transport.next_frame())


func close() -> void:
	if _transport != null and _transport.has_method("close"):
		_transport.close()
	connection_state_changed.emit("CLOSED")


## Public for deterministic protocol tests and non-network replay adapters.
func accept_server_frame(frame: Variant) -> void:
	if not frame is Dictionary:
		_reject("Realtime frame is not a JSON object.")
		return
	if not frame.has("frame_type"):
		_accept_world_event(frame)
		return
	match str(frame.frame_type):
		"subscribed": _accept_subscribed(frame)
		"heartbeat": _accept_heartbeat(frame)
		"error": _accept_error(frame)
		_:
			_accept_world_event(frame)


func mark_event_durably_applied(event: Dictionary) -> bool:
	if _awaiting_recovery or _subscription_id.is_empty():
		return false
	if str(event.get("stream_id", "")) != _stream_id or int(event.get("sequence", 0)) != _durable_sequence + 1:
		_reject("Attempted to ACK an event outside the contiguous durable checkpoint.")
		return false
	_durable_sequence = int(event.sequence)
	_durable_event_id = str(event.event_id)
	_send_frame({
		"frame_type": "ack", "protocol_version": PROTOCOL_VERSION,
		"subscription_id": _subscription_id, "stream_id": _stream_id,
		"sequence": _durable_sequence, "event_id": _durable_event_id,
	})
	return true


func complete_snapshot_recovery(snapshot_last_sequence: int) -> void:
	if snapshot_last_sequence < 0:
		_reject("Snapshot recovery returned a negative sequence.")
		return
	_durable_sequence = snapshot_last_sequence
	_durable_event_id = ""
	_awaiting_recovery = false
	if _transport != null:
		_send_open_frame()


func _send_open_frame() -> void:
	var frame := {
		"frame_type": "resume" if not _subscription_id.is_empty() else "subscribe",
		"protocol_version": PROTOCOL_VERSION,
		"request_id": _request_context.request_id,
		"stream_id": _stream_id,
		"after_sequence": _durable_sequence,
	}
	if not _subscription_id.is_empty():
		frame["subscription_id"] = _subscription_id
	_send_frame(frame)


func _accept_subscribed(frame: Dictionary) -> void:
	var required := ["frame_type", "protocol_version", "request_id", "subscription_id", "stream_id", "accepted_after_sequence", "high_watermark_sequence", "heartbeat_interval_ms", "max_unacked_events"]
	if not _closed(frame, required) or frame.protocol_version != PROTOCOL_VERSION or frame.request_id != _request_context.get("request_id") or frame.stream_id != _stream_id:
		_reject("Subscribed frame violates the negotiated stream identity.")
		return
	if not _matches(str(frame.subscription_id), "^sub_[A-Za-z0-9_-]{8,96}$") or int(frame.accepted_after_sequence) != _durable_sequence or int(frame.high_watermark_sequence) < _durable_sequence or int(frame.heartbeat_interval_ms) < 1000 or int(frame.max_unacked_events) < 1:
		_reject("Subscribed frame contains an invalid replay boundary.")
		return
	_subscription_id = frame.subscription_id
	connection_state_changed.emit("SUBSCRIBED")


func _accept_heartbeat(frame: Dictionary) -> void:
	var required := ["frame_type", "protocol_version", "subscription_id", "stream_id", "nonce", "server_time", "high_watermark_sequence"]
	if not _closed(frame, required) or frame.protocol_version != PROTOCOL_VERSION or frame.subscription_id != _subscription_id or frame.stream_id != _stream_id or not _matches(str(frame.nonce), "^hb_[A-Za-z0-9_-]{8,96}$"):
		_reject("Heartbeat frame violates the active subscription.")
		return
	_send_frame({"frame_type": "heartbeat_ack", "protocol_version": PROTOCOL_VERSION, "subscription_id": _subscription_id, "nonce": frame.nonce, "received_at": Time.get_datetime_string_from_system(true)})


func _accept_world_event(event: Dictionary) -> void:
	if _awaiting_recovery:
		return
	var valid := ContractValidator.validate_event(event, _durable_sequence)
	if not valid.ok or str(event.get("stream_id", "")) != _stream_id:
		_awaiting_recovery = true
		recovery_required.emit(_durable_sequence)
		return
	event_available.emit(event.duplicate(true))


func _accept_error(frame: Dictionary) -> void:
	if not frame.has("error") or not frame.error is Dictionary:
		_reject("Realtime error frame has no structured error.")
		return
	server_error.emit(frame.error.duplicate(true))
	if bool(frame.get("fatal", false)):
		connection_state_changed.emit("ERROR")


func _send_frame(frame: Dictionary) -> void:
	frame_outbound.emit(frame.duplicate(true))
	if _transport != null and _transport.has_method("send_frame"):
		_transport.send_frame(frame.duplicate(true))


func configure_transport(value: RefCounted) -> void:
	_transport = value


func _valid_stream_setup(stream_url: String, stream_id: String, request_context: Dictionary, after_sequence: int) -> bool:
	if not _matches(stream_url, "^wss://[^@/?#]+(/[^?#]*)?$") or not _matches(stream_id, "^[A-Za-z][A-Za-z0-9:_-]{2,159}$") or after_sequence < 0:
		_reject("Realtime stream URL, stream_id, or cursor is invalid.")
		return false
	if not ContractValidator.validate_request_context(request_context).ok:
		_reject("Realtime stream requires a valid request context.")
		return false
	return true


func _closed(value: Dictionary, required: Array) -> bool:
	if value.size() != required.size():
		return false
	for field in required:
		if not value.has(field):
			return false
	return true


func _matches(value: String, pattern: String) -> bool:
	var regex := RegEx.new()
	return regex.compile(pattern) == OK and regex.search(value) != null


func _reject(reason: String) -> void:
	protocol_rejected.emit(reason)
