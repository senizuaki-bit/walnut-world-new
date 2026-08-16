extends SceneTree

const RealtimeClient := preload("res://scripts/client/world_realtime_client.gd")

func _initialize() -> void:
	var client := RealtimeClient.new()
	root.add_child(client)
	var outbound: Array[Dictionary] = []
	var received: Array[Dictionary] = []
	var recovery := {"after": -1}
	client.frame_outbound.connect(func(frame: Dictionary) -> void: outbound.append(frame))
	client.event_available.connect(func(event: Dictionary) -> void: received.append(event))
	client.recovery_required.connect(func(sequence: int) -> void: recovery["after"] = sequence)
	client._stream_id = "world_world_0001"
	client._request_context = _context()
	client._durable_sequence = 3
	client._send_open_frame()
	client.accept_server_frame({"frame_type": "subscribed", "protocol_version": "1.0.0", "request_id": _context().request_id, "subscription_id": "sub_world_0001", "stream_id": "world_world_0001", "accepted_after_sequence": 3, "high_watermark_sequence": 5, "heartbeat_interval_ms": 1000, "max_unacked_events": 10})
	client.accept_server_frame(_event("evt_world_0004", 4))
	if received.size() != 1 or outbound.size() != 1 or not client.mark_event_durably_applied(received[0]):
		push_error("Realtime event must not ACK before durable application: received=%s outbound=%s sequence=%s" % [received.size(), outbound.size(), client._durable_sequence])
		quit(1)
		return
	client.accept_server_frame({"frame_type": "heartbeat", "protocol_version": "1.0.0", "subscription_id": "sub_world_0001", "stream_id": "world_world_0001", "nonce": "hb_world_0001", "server_time": "2026-08-09T00:00:00Z", "high_watermark_sequence": 4})
	client.accept_server_frame(_event("evt_world_0006", 6))
	if str(outbound[1].frame_type) != "ack" or str(outbound[2].frame_type) != "heartbeat_ack" or recovery.after != 4:
		push_error("Realtime protocol must ACK contiguous events, answer heartbeat, and pause on gaps: outbound=%s recovery=%s" % [str(outbound), recovery.after])
		quit(1)
		return
	print("WORLD_REALTIME_CLIENT_TEST_PASS")
	quit(0)

func _context() -> Dictionary:
	return {"schema_version": "1.0.0", "request_id": "req_world_0001", "correlation_id": "corr_world_0001", "trace_id": "trace_world_0001", "requested_at": "2026-08-09T00:00:00Z", "actor": {"tenant_id": "tenant_demo", "actor_id": "student_demo", "actor_type": "student", "roles": ["game:player"]}, "content_ref": {"unit_id": "YAYA_FARM_001", "version": "1.0.0", "content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}

func _event(event_id: String, sequence: int) -> Dictionary:
	return {"event_id": event_id, "event_type": "world.committed", "event_version": 1, "schema_version": "1.0.0", "stream_id": "world_world_0001", "sequence": sequence, "occurred_at": "2026-08-09T00:00:00Z", "producer": "world", "trace_id": "trace_world_0001", "command_id": "cmd_world_0001", "correlation_id": "corr_world_0001", "causation_id": null, "content_ref": _context().content_ref, "payload": {}}
