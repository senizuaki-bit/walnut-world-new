class_name WorldRecoveryCoordinator
extends RefCounted

## HTTP authority for reconnect/gap recovery. It returns only a gap-free event
## segment; any broken chain is discarded in favour of an atomic Snapshot.

signal events_recovered(events: Array[Dictionary])
signal snapshot_recovered(snapshot: Dictionary)

var _gateway: RefCounted
var _store: WalnutClientStore


func _init(gateway: RefCounted, store: WalnutClientStore = null) -> void:
	_gateway = gateway
	_store = store


func recover(request_context: Dictionary, world_id: String, after_sequence: int) -> Dictionary:
	if _gateway == null or not _gateway.has_method("get_world_events") or not _gateway.has_method("get_world_snapshot"):
		return _failure("WORLD_RECOVERY_GATEWAY_UNAVAILABLE", "Game gateway does not support world recovery.")
	if after_sequence < 0 or world_id.is_empty():
		return _failure("WORLD_RECOVERY_REQUEST_INVALID", "World recovery request is invalid.")
	var cursor := after_sequence
	var recovered: Array[Dictionary] = []
	while true:
		var page: Dictionary = await _gateway.get_world_events(request_context, world_id, cursor, 100)
		if not page.get("ok", false):
			return await _recover_snapshot(request_context, world_id)
		var value: Variant = page.get("value")
		if not value is Dictionary or str(value.get("world_id", "")) != world_id or not value.get("events") is Array:
			return await _recover_snapshot(request_context, world_id)
		for event in value.events:
			if not event is Dictionary or int(event.get("sequence", 0)) != cursor + 1 or str(event.get("stream_id", "")) != "world:%s" % world_id:
				return await _recover_snapshot(request_context, world_id)
			recovered.append(event.duplicate(true))
			cursor += 1
		if not bool(value.get("has_more", false)):
			events_recovered.emit(recovered)
			return {"ok": true, "status": 200, "headers": {}, "value": {"mode": "events", "events": recovered}}
	return _failure("WORLD_RECOVERY_UNREACHABLE", "World recovery ended without a result.")


func _recover_snapshot(request_context: Dictionary, world_id: String) -> Dictionary:
	var result: Dictionary = await _gateway.get_world_snapshot(request_context, world_id)
	if not result.get("ok", false):
		return result
	var snapshot: Variant = result.get("value")
	if not snapshot is Dictionary or str(snapshot.get("world_id", "")) != world_id:
		return _failure("WORLD_SNAPSHOT_IDENTITY_INVALID", "Snapshot does not match the requested world.")
	if _store != null:
		_store.replace_world(snapshot)
	snapshot_recovered.emit(snapshot.duplicate(true))
	return {"ok": true, "status": int(result.get("status", 200)), "headers": result.get("headers", {}).duplicate(true), "value": {"mode": "snapshot", "snapshot": snapshot.duplicate(true)}}


func _failure(code: String, message: String) -> Dictionary:
	return {"ok": false, "status": 0, "headers": {}, "error": {"scope": "CLIENT_LOCAL", "code": code, "message": message}}
