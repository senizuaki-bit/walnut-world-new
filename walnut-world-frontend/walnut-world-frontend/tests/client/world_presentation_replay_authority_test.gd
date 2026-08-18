extends SceneTree


class FakePlayer:
	extends Node
	var store: WalnutClientStore
	var expected_final: Dictionary
	var observed_authority_rollback := false
	var fail_replay := false

	func replay_current_result(_renderer: Object = null) -> Dictionary:
		observed_authority_rollback = store.world_snapshot != expected_final
		if fail_replay:
			return {
				"ok": false, "status": 0, "headers": {},
				"error": {
					"scope": "CLIENT_LOCAL", "code": "PRESENTATION_PLAYBACK_CANCELLED",
					"message": "cancelled", "retryable": false, "data": null,
				},
			}
		return {
			"ok": true, "status": 200, "headers": {},
			"value": {"cursor": 2, "rendered": 1}, "skipped": false,
		}


class FakeRenderer:
	extends Node
	var replay_snapshots: Array[Dictionary] = []

	func project_replay_snapshot(snapshot: Dictionary) -> bool:
		replay_snapshots.append(snapshot.duplicate(true))
		return true

	func can_project_authoritative_snapshot(_snapshot: Dictionary) -> bool:
		return true

	func last_authoritative_projection_succeeded(_snapshot: Dictionary) -> bool:
		return true


func _initialize() -> void:
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	var controller := root.get_node_or_null("SessionController")
	if store == null or controller == null:
		return _fail("Required production autoloads are unavailable.")
	store.persistence_enabled = false
	await process_frame
	var pre_snapshot := _snapshot(1, 1, "1")
	var final_snapshot := _snapshot(2, 2, "2")
	if not store.replace_world(final_snapshot):
		return _fail("Could not establish final authoritative Snapshot.")
	store.set_flow(WalnutClientStore.FlowState.COMPLETED)
	var player := FakePlayer.new()
	player.store = store
	player.expected_final = final_snapshot.duplicate(true)
	var renderer := FakeRenderer.new()
	root.add_child(player)
	root.add_child(renderer)
	controller.configure_world_presentation(null, player, renderer, true)
	controller._last_presentation_pre_snapshot = pre_snapshot.duplicate(true)
	controller._last_presentation_final_snapshot = final_snapshot.duplicate(true)
	if not controller.can_replay_world_result():
		return _fail("Replay fixture was not accepted: enabled=%s player=%s pre=%s final=%s flow=%s" % [
			controller.world_presentation_enabled,
			controller.world_event_player != null,
			controller._last_presentation_pre_snapshot,
			controller._last_presentation_final_snapshot,
			store.flow_state,
		])
	var result: Dictionary = await controller.replay_world_result()
	if not result.get("ok", false):
		return _fail("Verified replay unexpectedly failed: %s" % result)
	if player.observed_authority_rollback:
		return _fail("Replay temporarily rolled authoritative ClientStore back to the pre-Run Snapshot.")
	if store.world_snapshot != final_snapshot or store.last_applied_sequence != 2:
		return _fail("Replay changed the final ClientStore authority fingerprint.")
	if renderer.replay_snapshots != [pre_snapshot, final_snapshot]:
		return _fail("Replay did not reset and close only the renderer's temporary view.")

	renderer.replay_snapshots.clear()
	player.fail_replay = true
	store.set_flow(WalnutClientStore.FlowState.COMPLETED)
	result = await controller.replay_world_result()
	if result.get("ok", false) or str(result.get("error", {}).get("code", "")) != "PRESENTATION_PLAYBACK_CANCELLED":
		return _fail("Cancelled replay did not propagate its fail-closed outcome.")
	if store.world_snapshot != final_snapshot or store.last_applied_sequence != 2:
		return _fail("Cancelled replay changed ClientStore's final authority fingerprint.")
	if renderer.replay_snapshots != [pre_snapshot, final_snapshot]:
		return _fail("Cancelled replay did not restore the renderer to the final authoritative Snapshot.")
	print("WORLD_PRESENTATION_REPLAY_AUTHORITY_TEST_PASS")
	quit(0)


func _snapshot(revision: int, sequence: int, hash_digit: String) -> Dictionary:
	return {
		"world_id": "world_demo", "revision": revision,
		"last_event_sequence": sequence, "state_schema_version": "1.0.0",
		"state_hash": hash_digit.repeat(64), "world_rules_version": "rules_demo",
		"state": {},
	}


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
