class_name WorldPresentationGateway
extends RefCounted

## Additive v0.5 read client for the committed World presentation projection.
## The byte-pinned v0.4 AgentApiGateway remains untouched.  This boundary
## rejects the whole page before any item is exposed to presentation code.

const ContractValidator := preload("res://addons/yaya_contract_client/contract_validator.gd")
const MAX_SAFE_INTEGER := 9007199254740991
const EVENT_TYPE := "world.action.harvested"
const EVENT_VERSION := 1
const SCHEMA_VERSION := "1.0.0"
const PRODUCER := "walnut_world_engine"

const PAGE_FIELDS := [
	"request_context", "world_id", "snapshot_revision",
	"snapshot_last_event_sequence", "snapshot_state_hash",
	"presentation_high_watermark", "from_sequence", "to_sequence",
	"has_more", "next_after_sequence", "events",
]
const EVENT_FIELDS := [
	"event_id", "event_type", "event_version", "schema_version", "stream_id",
	"sequence", "occurred_at", "producer", "tenant_id", "session_id",
	"turn_id", "command_id", "run_id", "world_id", "commit_id",
	"world_revision", "action_index", "action_count", "intent_id",
	"state_hash_before", "state_hash_after", "final_snapshot_revision",
	"final_world_event_sequence", "final_snapshot_state_hash", "payload",
	"payload_sha256", "integrity_sha256",
]
const PAYLOAD_FIELDS := [
	"actor_entity_id", "plot_id", "position", "crop_type", "growth_stage",
	"ready_to_harvest",
]
const POSITION_FIELDS := ["x", "y"]

var _transport: RefCounted


func _init(transport: RefCounted) -> void:
	_transport = transport


func get_world_presentation_events(
	attempt_context: Dictionary,
	world_id: String,
	after_sequence: int,
	limit: int = 100,
) -> Dictionary:
	if (
		not _valid_attempt_context(attempt_context)
		or not _valid_identifier(world_id)
		or after_sequence < 0
		or after_sequence > MAX_SAFE_INTEGER
		or limit < 1
		or limit > 500
	):
		return _failure("PRESENTATION_REQUEST_INVALID", "The presentation read request is invalid.")
	if _transport == null or not _transport.has_method("execute"):
		return _failure("PRESENTATION_TRANSPORT_UNAVAILABLE", "The presentation transport is not configured.")
	var result: Variant = await _transport.execute("get_world_presentation_events", {
		"attempt_context": attempt_context.duplicate(true),
		"world_id": world_id,
		"after_sequence": after_sequence,
		"limit": limit,
	})
	if not result is Dictionary or typeof(result.get("ok")) != TYPE_BOOL:
		return _failure("PRESENTATION_TRANSPORT_INVALID", "The presentation transport returned an invalid result union.")
	if not result.ok:
		return result if result.get("error") is Dictionary else _failure(
			"PRESENTATION_TRANSPORT_INVALID",
			"The presentation transport failure lacks a structured error.",
		)
	if int(result.get("status", 0)) != 200 or not result.get("value") is Dictionary:
		return _failure("PRESENTATION_RESPONSE_INVALID", "The presentation read must return one 200 JSON object.")
	var validation := validate_page(result.value, world_id, after_sequence, limit)
	if not validation.ok:
		return _failure(str(validation.code), str(validation.message))
	if not _valid_response_headers(result.get("headers"), attempt_context):
		return _failure("PRESENTATION_RESPONSE_HEADERS_INVALID", "Presentation response attempt headers are missing or mismatched.")
	return {
		"ok": true,
		"status": 200,
		"headers": result.get("headers", {}).duplicate(true),
		"value": result.value.duplicate(true),
	}


static func validate_page(
	value: Variant,
	expected_world_id: String,
	after_sequence: int,
	limit: int = 100,
) -> Dictionary:
	if not _exact_shape(value, PAGE_FIELDS):
		return _invalid("PRESENTATION_PAGE_SHAPE_INVALID", "WorldPresentationEventPage is not byte-closed.")
	var page: Dictionary = value
	var context_validation := ContractValidator.validate_request_context(page.request_context)
	if not context_validation.ok:
		return _invalid("PRESENTATION_PAGE_CONTEXT_INVALID", "WorldPresentationEventPage has an invalid origin RequestContext.")
	if str(page.world_id) != expected_world_id or not _valid_identifier(str(page.world_id)):
		return _invalid("PRESENTATION_WORLD_MISMATCH", "WorldPresentationEventPage belongs to another World.")
	if (
		not _safe_integer(page.snapshot_revision, 0)
		or not _safe_integer(page.snapshot_last_event_sequence, 0)
		or not _hash(page.snapshot_state_hash)
		or not _safe_integer(page.presentation_high_watermark, 0)
		or not _safe_integer(page.from_sequence, 0)
		or not _safe_integer(page.to_sequence, 0)
		or not _safe_integer(page.next_after_sequence, 0)
		or typeof(page.has_more) != TYPE_BOOL
		or not page.events is Array
		or page.events.size() > limit
		or int(page.presentation_high_watermark) < after_sequence
	):
		return _invalid("PRESENTATION_PAGE_CURSOR_INVALID", "WorldPresentationEventPage cursor or Snapshot fingerprint is invalid.")
	var events: Array = page.events
	if events.is_empty():
		if (
			int(page.from_sequence) != after_sequence
			or int(page.to_sequence) != after_sequence
			or int(page.next_after_sequence) != after_sequence
			or bool(page.has_more)
			or int(page.presentation_high_watermark) != after_sequence
		):
			return _invalid("PRESENTATION_SEQUENCE_GAP", "An empty presentation page did not close at the requested cursor.")
		return _valid()

	var expected_sequence := after_sequence + 1
	var previous: Dictionary = {}
	var seen_ids := {}
	for item in events:
		var event_validation := validate_event(item)
		if not event_validation.ok:
			return event_validation
		var event: Dictionary = item
		if int(event.sequence) != expected_sequence:
			return _invalid("PRESENTATION_SEQUENCE_GAP", "Presentation sequence is missing, duplicated, or out of order.")
		if after_sequence == 0 and expected_sequence == 1 and int(event.action_index) != 0:
			return _invalid("PRESENTATION_ACTION_CHAIN_MISMATCH", "Cold presentation sequence 1 must begin at action_index 0.")
		if str(event.event_id) in seen_ids:
			return _invalid("PRESENTATION_SEQUENCE_GAP", "Presentation page repeats a stable event identity.")
		if str(event.world_id) != expected_world_id or str(event.stream_id) != "world-presentation:%s" % expected_world_id:
			return _invalid("PRESENTATION_WORLD_MISMATCH", "Presentation event World/stream identity is inconsistent.")
		if (
			str(event.tenant_id) != str(page.request_context.actor.tenant_id)
			or int(event.final_snapshot_revision) > int(page.snapshot_revision)
			or int(event.final_world_event_sequence) > int(page.snapshot_last_event_sequence)
		):
			return _invalid("PRESENTATION_FINAL_SNAPSHOT_MISMATCH", "Presentation event points beyond page Snapshot/tenant authority.")
		if not previous.is_empty():
			if str(previous.state_hash_after) != str(event.state_hash_before):
				return _invalid("PRESENTATION_STATE_CHAIN_MISMATCH", "Presentation event state hashes do not form one committed chain.")
			if str(previous.commit_id) == str(event.commit_id):
				if (
					int(event.action_index) != int(previous.action_index) + 1
					or int(event.action_count) != int(previous.action_count)
					or int(event.world_revision) != int(previous.world_revision)
					or not _same_commit_authority(previous, event)
				):
					return _invalid("PRESENTATION_ACTION_CHAIN_MISMATCH", "Presentation actions for one commit are not contiguous.")
			elif (
				int(previous.action_index) != int(previous.action_count) - 1
				or int(event.action_index) != 0
				or int(event.world_revision) != int(previous.world_revision) + 1
				or int(event.final_world_event_sequence) <= int(previous.final_world_event_sequence)
			):
				return _invalid("PRESENTATION_ACTION_CHAIN_MISMATCH", "A presentation commit boundary is incomplete.")
		seen_ids[str(event.event_id)] = true
		previous = event
		expected_sequence += 1
	if (
		int(page.from_sequence) != after_sequence + 1
		or int(page.to_sequence) != expected_sequence - 1
		or int(page.next_after_sequence) != expected_sequence - 1
		or int(page.presentation_high_watermark) < expected_sequence - 1
		or bool(page.has_more) != (int(page.presentation_high_watermark) > expected_sequence - 1)
	):
		return _invalid("PRESENTATION_SEQUENCE_GAP", "WorldPresentationEventPage terminal cursors are inconsistent.")
	if not bool(page.has_more) and (
		int(previous.action_index) != int(previous.action_count) - 1
		or int(previous.final_snapshot_revision) != int(page.snapshot_revision)
		or int(previous.final_world_event_sequence) != int(page.snapshot_last_event_sequence)
		or str(previous.final_snapshot_state_hash) != str(page.snapshot_state_hash)
	):
		return _invalid("PRESENTATION_FINAL_SNAPSHOT_MISMATCH", "Presentation high watermark does not close to the authoritative Snapshot fingerprint.")
	return _valid()


static func validate_event(value: Variant) -> Dictionary:
	if not _exact_shape(value, EVENT_FIELDS):
		return _invalid("PRESENTATION_EVENT_SHAPE_INVALID", "WorldPresentationEvent is not byte-closed.")
	var event: Dictionary = value
	if str(event.event_type) != EVENT_TYPE:
		return _invalid("PRESENTATION_EVENT_UNSUPPORTED", "The client has no preauthored renderer for this presentation event type.")
	if (
		typeof(event.event_version) != TYPE_INT
		or int(event.event_version) != EVENT_VERSION
		or str(event.schema_version) != SCHEMA_VERSION
		or str(event.producer) != PRODUCER
	):
		return _invalid("PRESENTATION_EVENT_UNSUPPORTED", "The presentation event type/version/producer is unsupported.")
	if (
		not _presentation_event_id(str(event.event_id))
		or not _valid_identifier(str(event.tenant_id))
		or not _valid_identifier(str(event.session_id))
		or not _valid_identifier(str(event.turn_id))
		or not _valid_identifier(str(event.command_id))
		or not _valid_identifier(str(event.run_id))
		or not _valid_identifier(str(event.world_id))
		or not _valid_identifier(str(event.commit_id))
		or not _valid_identifier(str(event.intent_id))
		or str(event.stream_id) != "world-presentation:%s" % str(event.world_id)
		or not _date_time(str(event.occurred_at))
		or not _safe_integer(event.sequence, 1)
		or not _safe_integer(event.world_revision, 1)
		or not _safe_integer(event.action_index, 0)
		or not _safe_integer(event.action_count, 1)
		or int(event.action_count) > 10000
		or int(event.action_index) >= int(event.action_count)
		or not _safe_integer(event.final_snapshot_revision, 1)
		or int(event.final_snapshot_revision) != int(event.world_revision)
		or not _safe_integer(event.final_world_event_sequence, 1)
		or not _hash(event.state_hash_before)
		or not _hash(event.state_hash_after)
		or str(event.state_hash_before) == str(event.state_hash_after)
		or not _hash(event.final_snapshot_state_hash)
		or not _hash(event.payload_sha256)
		or not _hash(event.integrity_sha256)
	):
		return _invalid("PRESENTATION_EVENT_IDENTITY_INVALID", "WorldPresentationEvent identity, cursor, or hash fields are invalid.")
	if not _exact_shape(event.payload, PAYLOAD_FIELDS):
		return _invalid("PRESENTATION_PAYLOAD_SHAPE_INVALID", "Harvest presentation payload is not byte-closed.")
	var payload: Dictionary = event.payload
	if (
		not _exact_shape(payload.position, POSITION_FIELDS)
		or not _valid_identifier(str(payload.actor_entity_id))
		or not _valid_identifier(str(payload.plot_id))
		or typeof(payload.crop_type) != TYPE_STRING
		or not _matches(str(payload.crop_type), "^[a-z][a-z0-9_.-]{1,63}$")
		or not _safe_integer(payload.growth_stage, 0)
		or int(payload.growth_stage) > 100
		or not _safe_integer(payload.position.x, -100000)
		or int(payload.position.x) > 100000
		or not _safe_integer(payload.position.y, -100000)
		or int(payload.position.y) > 100000
		or typeof(payload.ready_to_harvest) != TYPE_BOOL
		or not bool(payload.ready_to_harvest)
	):
		return _invalid("PRESENTATION_PAYLOAD_INVALID", "Harvest presentation payload values are invalid.")
	var payload_hash := ContractValidator.canonical_json_sha256_v1(payload)
	if payload_hash.is_empty() or payload_hash != str(event.payload_sha256):
		return _invalid("PRESENTATION_PAYLOAD_HASH_MISMATCH", "Harvest presentation payload hash does not verify.")
	var position: Dictionary = payload.position
	var integrity_projection := [
		event.event_type, event.event_version, event.schema_version, event.stream_id,
		event.sequence, event.occurred_at, event.producer, event.tenant_id,
		event.session_id, event.turn_id, event.command_id, event.run_id,
		event.world_id, event.commit_id, event.world_revision, event.action_index,
		event.action_count, event.intent_id, event.state_hash_before,
		event.state_hash_after, event.final_snapshot_revision,
		event.final_world_event_sequence, event.final_snapshot_state_hash,
		event.payload_sha256, payload.actor_entity_id, payload.plot_id,
		position.x, position.y, payload.crop_type, payload.growth_stage,
		payload.ready_to_harvest,
	]
	var integrity_hash := ContractValidator.canonical_json_sha256_v1(integrity_projection)
	if integrity_hash.is_empty() or integrity_hash != str(event.integrity_sha256):
		return _invalid("PRESENTATION_INTEGRITY_MISMATCH", "WorldPresentationEvent integrity hash does not verify.")
	if str(event.event_id) != "presentation_%s" % integrity_hash.left(32):
		return _invalid("PRESENTATION_EVENT_ID_MISMATCH", "WorldPresentationEvent stable identity does not derive from integrity.")
	if int(event.action_index) == int(event.action_count) - 1 and str(event.state_hash_after) != str(event.final_snapshot_state_hash):
		return _invalid("PRESENTATION_FINAL_SNAPSHOT_MISMATCH", "The final action does not close to its declared Snapshot hash.")
	return _valid()


static func _valid_attempt_context(value: Variant) -> bool:
	if not _exact_shape(value, ["schema_version", "request_id", "correlation_id", "trace_id"]):
		return false
	return (
		str(value.schema_version) == SCHEMA_VERSION
		and _matches(str(value.request_id), "^req_[A-Za-z0-9_-]{8,96}$")
		and _matches(str(value.correlation_id), "^corr_[A-Za-z0-9_-]{8,96}$")
		and _matches(str(value.trace_id), "^trace_[A-Za-z0-9_-]{8,96}$")
	)


static func _same_commit_authority(left: Dictionary, right: Dictionary) -> bool:
	for field in [
		"tenant_id", "session_id", "turn_id", "command_id", "run_id", "world_id",
		"commit_id", "world_revision", "action_count", "final_snapshot_revision",
		"final_world_event_sequence", "final_snapshot_state_hash",
	]:
		if left.get(field) != right.get(field):
			return false
	return true


static func _valid_response_headers(value: Variant, attempt: Dictionary) -> bool:
	if not value is Dictionary:
		return false
	for field in ["request_id", "trace_id", "correlation_id"]:
		var header_name := "x-%s" % field.replace("_", "-")
		if typeof(value.get(header_name)) != TYPE_STRING or str(value.get(header_name)) != str(attempt.get(field)):
			return false
	return true


static func _exact_shape(value: Variant, fields: Array) -> bool:
	if not value is Dictionary or value.size() != fields.size():
		return false
	for field in fields:
		if not value.has(field):
			return false
	return true


static func _safe_integer(value: Variant, minimum: int) -> bool:
	return typeof(value) == TYPE_INT and int(value) >= minimum and int(value) <= MAX_SAFE_INTEGER


static func _hash(value: Variant) -> bool:
	return typeof(value) == TYPE_STRING and _matches(str(value), "^[a-f0-9]{64}$")


static func _presentation_event_id(value: String) -> bool:
	return _matches(value, "^presentation_[a-f0-9]{32}$")


static func _valid_identifier(value: String) -> bool:
	return _matches(value, "^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


static func _date_time(value: String) -> bool:
	if not _matches(value, "^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})$"):
		return false
	var year := value.substr(0, 4).to_int()
	var month := value.substr(5, 2).to_int()
	var day := value.substr(8, 2).to_int()
	var hour := value.substr(11, 2).to_int()
	var minute := value.substr(14, 2).to_int()
	var second := value.substr(17, 2).to_int()
	if year < 1 or month < 1 or month > 12 or hour > 23 or minute > 59 or second > 59:
		return false
	var days: Array[int] = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
	if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
		days[1] = 29
	if day < 1 or day > days[month - 1]:
		return false
	var zone_index := value.find("Z", 19)
	if zone_index < 0:
		zone_index = value.find("z", 19)
	if zone_index < 0:
		zone_index = value.find("+", 19)
	if zone_index < 0:
		zone_index = value.find("-", 19)
	if zone_index >= 0 and value.substr(zone_index, 1).to_upper() != "Z":
		return value.substr(zone_index + 1, 2).to_int() <= 23 and value.substr(zone_index + 4, 2).to_int() <= 59
	return true


static func _matches(value: String, pattern: String) -> bool:
	var regex := RegEx.new()
	return regex.compile(pattern) == OK and regex.search(value) != null


static func _valid() -> Dictionary:
	return {"ok": true}


static func _invalid(code: String, message: String) -> Dictionary:
	return {"ok": false, "code": code, "message": message}


static func _failure(code: String, message: String) -> Dictionary:
	return {
		"ok": false,
		"status": 0,
		"headers": {},
		"error": {
			"code": code,
			"message": message,
			"retryable": false,
			"scope": "CLIENT_LOCAL",
			"data": null,
		},
	}
