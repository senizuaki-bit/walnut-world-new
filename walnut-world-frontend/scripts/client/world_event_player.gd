class_name WorldEventPlayer
extends Node

## Plays only fully closed v0.5 committed presentation events.  It never sorts
## input, derives business state, or treats a renderer error as success.

const PresentationGateway := preload("res://scripts/client/world_presentation_gateway.gd")

signal playback_started
signal event_started(event: Dictionary)
signal event_projection_requested(event: Dictionary)
signal event_finished(event: Dictionary)
signal playback_finished
signal playback_cancelled
signal playback_recovery_required(after_sequence: int)

var _seen_by_sequence: Dictionary = {}
var _last_applied_sequence := 0
var _last_applied_event: Dictionary = {}
var _current_result: Array[Dictionary] = []
var _speed_multiplier := 1.0
var _skip_requested := false
var _cancel_requested := false
var _playing := false


func set_cursor(sequence: int, last_event: Dictionary = {}) -> void:
	_last_applied_sequence = maxi(sequence, 0)
	_seen_by_sequence.clear()
	_last_applied_event = last_event.duplicate(true)


func get_cursor() -> int:
	return _last_applied_sequence


func set_speed_multiplier(value: float) -> bool:
	if value not in [1.0, 2.0]:
		return false
	_speed_multiplier = value
	return true


func get_speed_multiplier() -> float:
	return _speed_multiplier


func validate_batch(
	events: Array,
	after_sequence: int,
	previous_event: Dictionary = {},
) -> Dictionary:
	if after_sequence < 0:
		return _failure("PRESENTATION_SEQUENCE_GAP", "Presentation preflight cursor is invalid.")
	if not previous_event.is_empty():
		var previous_validation := PresentationGateway.validate_event(previous_event)
		if not previous_validation.ok or int(previous_event.sequence) != after_sequence:
			return _failure("PRESENTATION_SEQUENCE_GAP", "Presentation preflight previous-event receipt is invalid.")
	var expected := after_sequence + 1
	var previous: Dictionary = previous_event.duplicate(true)
	for event in events:
		var validation := PresentationGateway.validate_event(event)
		if not validation.ok:
			return _failure(str(validation.code), str(validation.message))
		if int(event.sequence) != expected:
			return _failure("PRESENTATION_SEQUENCE_GAP", "Presentation batch is missing, duplicated, or out of order.")
		if after_sequence == 0 and expected == 1 and int(event.action_index) != 0:
			return _failure("PRESENTATION_ACTION_CHAIN_MISMATCH", "Cold presentation sequence 1 must begin at action_index 0.")
		if not previous.is_empty():
			var chain := _validate_chain(previous, event)
			if not chain.ok:
				return chain
		previous = event
		expected += 1
	return {"ok": true, "status": 200, "headers": {}, "value": {"to_sequence": expected - 1}}


func skip() -> void:
	_skip_requested = true


func stop() -> void:
	_cancel_requested = true


func play(events: Array[Dictionary], renderer: Object = null) -> Dictionary:
	if _playing:
		return _failure("PRESENTATION_PLAYBACK_ACTIVE", "A presentation playback is already active.")
	var prepared := _prepare(events, false)
	if not prepared.ok:
		playback_recovery_required.emit(_last_applied_sequence)
		return prepared
	var playable: Array[Dictionary] = prepared.events
	var verified_result: Array[Dictionary] = prepared.result
	_playing = true
	_skip_requested = false
	_cancel_requested = false
	playback_started.emit()
	var rendered := 0
	var skipped := false
	for event in playable:
		if _cancel_requested:
			_playing = false
			playback_cancelled.emit()
			return _failure("PRESENTATION_PLAYBACK_CANCELLED", "Presentation playback was cancelled before authority could close.")
		var event_skipped := _skip_requested
		if not event_skipped:
			var render_result := _begin_renderer(renderer, event)
			if not render_result.ok:
				_playing = false
				playback_recovery_required.emit(_last_applied_sequence)
				return render_result
			event_started.emit(event.duplicate(true))
			event_projection_requested.emit(event.duplicate(true))
			var duration_seconds := maxf(float(render_result.duration_seconds), 0.0) / _speed_multiplier
			var started_at := Time.get_ticks_usec()
			while not _skip_requested and not _cancel_requested and (
				float(Time.get_ticks_usec() - started_at) / 1000000.0 < duration_seconds
			):
				await get_tree().process_frame
			event_skipped = _skip_requested
			if _cancel_requested:
				_finish_renderer(renderer, event, true)
				_playing = false
				playback_cancelled.emit()
				return _failure("PRESENTATION_PLAYBACK_CANCELLED", "Presentation playback was cancelled before authority could close.")
			if not _finish_renderer(renderer, event, event_skipped):
				_playing = false
				playback_recovery_required.emit(_last_applied_sequence)
				return _failure("PRESENTATION_RENDERER_FAILED", "The preauthored presentation renderer failed to finish.")
			if not event_skipped:
				rendered += 1
		_seen_by_sequence[int(event.sequence)] = {
			"event_id": str(event.event_id),
			"integrity_sha256": str(event.integrity_sha256),
		}
		_last_applied_sequence = int(event.sequence)
		_last_applied_event = event.duplicate(true)
		event_finished.emit(event.duplicate(true))
		skipped = skipped or event_skipped
	# A duplicate-only GET proves no new projection. It must not replace the
	# complete current-result cache with an arbitrary historical subset.
	if not playable.is_empty():
		_current_result = verified_result.duplicate(true)
	_playing = false
	playback_finished.emit()
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {"cursor": _last_applied_sequence, "rendered": rendered},
		"skipped": skipped,
	}


func replay_current_result(renderer: Object = null) -> Dictionary:
	if _current_result.is_empty():
		return _failure("PRESENTATION_REPLAY_UNAVAILABLE", "No verified presentation result is available to replay.")
	if _playing:
		return _failure("PRESENTATION_PLAYBACK_ACTIVE", "A presentation playback is already active.")
	var prepared := _prepare(_current_result, true)
	if not prepared.ok:
		playback_recovery_required.emit(_last_applied_sequence)
		return prepared
	_playing = true
	_skip_requested = false
	_cancel_requested = false
	playback_started.emit()
	var rendered := 0
	for event in prepared.events:
		if _cancel_requested:
			_playing = false
			playback_cancelled.emit()
			return _failure("PRESENTATION_PLAYBACK_CANCELLED", "Presentation replay was cancelled.")
		var render_result := _begin_renderer(renderer, event)
		if not render_result.ok:
			_playing = false
			playback_recovery_required.emit(_last_applied_sequence)
			return render_result
		event_started.emit(event.duplicate(true))
		event_projection_requested.emit(event.duplicate(true))
		var duration_seconds := maxf(float(render_result.duration_seconds), 0.0) / _speed_multiplier
		var started_at := Time.get_ticks_usec()
		while not _skip_requested and not _cancel_requested and (
			float(Time.get_ticks_usec() - started_at) / 1000000.0 < duration_seconds
		):
			await get_tree().process_frame
		if not _finish_renderer(renderer, event, _skip_requested or _cancel_requested):
			_playing = false
			playback_recovery_required.emit(_last_applied_sequence)
			return _failure("PRESENTATION_RENDERER_FAILED", "The preauthored presentation renderer failed to finish replay.")
		if not _skip_requested:
			rendered += 1
		event_finished.emit(event.duplicate(true))
	_playing = false
	playback_finished.emit()
	return {
		"ok": true,
		"status": 200,
		"headers": {},
		"value": {"cursor": _last_applied_sequence, "rendered": rendered},
		"skipped": _skip_requested,
	}


func _prepare(events: Array[Dictionary], replay: bool) -> Dictionary:
	var playable: Array[Dictionary] = []
	var result_events: Array[Dictionary] = []
	var expected := int(events[0].sequence) if replay and not events.is_empty() else _last_applied_sequence + 1
	var input_expected := int(events[0].sequence) if not events.is_empty() else expected
	var previous: Dictionary = {}
	for event in events:
		var validation := PresentationGateway.validate_event(event)
		if not validation.ok:
			return _failure(str(validation.code), str(validation.message))
		var sequence := int(event.sequence)
		if sequence != input_expected:
			return _failure("PRESENTATION_SEQUENCE_GAP", "Presentation input is missing, duplicated, or out of order.")
		input_expected += 1
		if replay:
			if sequence != expected:
				return _failure("PRESENTATION_SEQUENCE_GAP", "Replay input is not in exact authoritative order.")
		else:
			if sequence <= _last_applied_sequence:
				var known: Variant = _seen_by_sequence.get(sequence)
				if not known is Dictionary or (
					str(known.get("event_id", "")) != str(event.event_id)
					or str(known.get("integrity_sha256", "")) != str(event.integrity_sha256)
				):
					return _failure("PRESENTATION_DUPLICATE_IDENTITY_MISMATCH", "An old sequence returned with unknown or changed identity.")
				if not previous.is_empty():
					var duplicate_chain := _validate_chain(previous, event)
					if not duplicate_chain.ok:
						return duplicate_chain
				result_events.append(event.duplicate(true))
				previous = event
				continue
			if sequence != expected:
				return _failure("PRESENTATION_SEQUENCE_GAP", "Presentation input is missing, duplicated, or out of order.")
			if _last_applied_sequence == 0 and sequence == 1 and int(event.action_index) != 0:
				return _failure("PRESENTATION_ACTION_CHAIN_MISMATCH", "Cold presentation sequence 1 must begin at action_index 0.")
		if not previous.is_empty():
			var chain := _validate_chain(previous, event)
			if not chain.ok:
				return chain
		elif not replay and not _last_applied_event.is_empty() and sequence == _last_applied_sequence + 1:
			var chain := _validate_chain(_last_applied_event, event)
			if not chain.ok:
				return chain
		playable.append(event.duplicate(true))
		result_events.append(event.duplicate(true))
		previous = event
		expected += 1
	return {"ok": true, "events": playable, "result": result_events}


func _validate_chain(previous: Dictionary, event: Dictionary) -> Dictionary:
	if str(previous.stream_id) != str(event.stream_id) or str(previous.state_hash_after) != str(event.state_hash_before):
		return _failure("PRESENTATION_STATE_CHAIN_MISMATCH", "Presentation events do not form one authoritative state chain.")
	if str(previous.commit_id) == str(event.commit_id):
		if int(event.action_index) != int(previous.action_index) + 1 or int(event.action_count) != int(previous.action_count):
			return _failure("PRESENTATION_ACTION_CHAIN_MISMATCH", "Actions in one committed presentation set are not contiguous.")
		for field in [
			"tenant_id", "session_id", "turn_id", "command_id", "run_id", "world_id",
			"commit_id", "world_revision", "action_count", "final_snapshot_revision",
			"final_world_event_sequence", "final_snapshot_state_hash",
		]:
			if previous.get(field) != event.get(field):
				return _failure("PRESENTATION_ACTION_CHAIN_MISMATCH", "One committed presentation set changes immutable authority identity.")
	elif (
		int(previous.action_index) != int(previous.action_count) - 1
		or int(event.action_index) != 0
		or int(event.world_revision) != int(previous.world_revision) + 1
		or int(event.final_world_event_sequence) <= int(previous.final_world_event_sequence)
	):
		return _failure("PRESENTATION_ACTION_CHAIN_MISMATCH", "A presentation commit boundary is incomplete or non-monotonic.")
	return {"ok": true}


func _begin_renderer(renderer: Object, event: Dictionary) -> Dictionary:
	if renderer == null:
		return {"ok": true, "duration_seconds": 0.0}
	if not is_instance_valid(renderer) or not renderer.has_method("begin_presentation_event"):
		return _failure("PRESENTATION_RENDERER_UNAVAILABLE", "The formal preauthored renderer is unavailable.")
	var value: Variant = renderer.call("begin_presentation_event", event.duplicate(true), _speed_multiplier)
	if not value is Dictionary or not bool(value.get("ok", false)) or typeof(value.get("duration_seconds")) not in [TYPE_FLOAT, TYPE_INT]:
		return _failure("PRESENTATION_RENDERER_FAILED", "The preauthored renderer rejected the event.")
	return value


func _finish_renderer(renderer: Object, event: Dictionary, skipped: bool) -> bool:
	if renderer == null:
		return true
	return (
		is_instance_valid(renderer)
		and renderer.has_method("finish_presentation_event")
		and bool(renderer.call("finish_presentation_event", event.duplicate(true), skipped))
	)


func _failure(code: String, message: String) -> Dictionary:
	return {
		"ok": false,
		"status": 0,
		"headers": {},
		"error": {
			"code": code, "message": message, "retryable": false,
			"scope": "CLIENT_LOCAL", "data": null,
		},
	}
