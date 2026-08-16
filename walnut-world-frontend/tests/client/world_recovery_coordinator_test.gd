extends SceneTree

const Coordinator := preload("res://scripts/client/world_recovery_coordinator.gd")

class FakeGateway:
	extends RefCounted
	var corrupt := false
	func get_world_events(_context: Dictionary, world_id: String, after: int, _limit: int) -> Dictionary:
		await Engine.get_main_loop().process_frame
		if corrupt:
			return {"ok": true, "status": 200, "headers": {}, "value": {"world_id": world_id, "has_more": false, "events": [{"stream_id": "world_%s" % world_id, "sequence": after + 2}]}}
		return {"ok": true, "status": 200, "headers": {}, "value": {"world_id": world_id, "has_more": false, "events": [{"stream_id": "world:%s" % world_id, "sequence": after + 1}]}}
	func get_world_snapshot(_context: Dictionary, world_id: String) -> Dictionary:
		await Engine.get_main_loop().process_frame
		return {"ok": true, "status": 200, "headers": {}, "value": {"world_id": world_id, "revision": 2, "last_event_sequence": 9, "state_schema_version": "1.0.0", "state_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "world_rules_version": "rules", "state": {}}}

func _initialize() -> void:
	var gateway := FakeGateway.new()
	var coordinator := Coordinator.new(gateway)
	var events: Dictionary = await coordinator.recover({}, "world_demo_0001", 3)
	if events.value.mode != "events" or events.value.events.size() != 1:
		push_error("Gap-free HTTP events must remain an event recovery segment.")
		quit(1)
		return
	gateway.corrupt = true
	var snapshot: Dictionary = await coordinator.recover({}, "world_demo_0001", 4)
	if snapshot.value.mode != "snapshot" or int(snapshot.value.snapshot.last_event_sequence) != 9:
		push_error("A broken HTTP sequence must atomically fall back to a Snapshot.")
		quit(1)
		return
	print("WORLD_RECOVERY_COORDINATOR_TEST_PASS")
	quit(0)
