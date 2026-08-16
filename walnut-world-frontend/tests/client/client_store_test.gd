extends SceneTree

const ClientStoreScript := preload("res://autoload/client_store.gd")


func _initialize() -> void:
	var store := ClientStoreScript.new()
	store.mark_draft_dirty("new local source")
	store.record_draft_conflict({"source": "server source", "revision": 2})
	if store.local_source != "new local source" or store.draft_state != store.DraftState.CONFLICT:
		push_error("CAS conflict must preserve unsaved local source.")
		quit(1)
		return
	store.replace_world(_snapshot())
	if store.last_applied_sequence != 5 or not store.record_applied_event({"event_id": "evt_demo_0006", "sequence": 6}):
		push_error("Authoritative Snapshot must establish the next contiguous event cursor.")
		quit(1)
		return
	if store.record_applied_event({"event_id": "evt_demo_0008", "sequence": 8}):
		push_error("WorldEvent sequence gap must be rejected.")
		quit(1)
		return
	store.free()

	var path := "user://client_store_int1_test.json"
	var absolute := ProjectSettings.globalize_path(path)
	if FileAccess.file_exists(path):
		DirAccess.remove_absolute(absolute)
	var first := ClientStoreScript.new()
	first.name = "PersistenceStoreOne"
	first.configure_persistence(path, true, false)
	root.add_child(first)
	await process_frame
	var active := {
		"activation_id": "activation_demo_0001",
		"skill_id": "skill_demo_0001",
		"skill_version_id": "skillver_demo_0001",
		"artifact_sha256": "a".repeat(64),
		"certification_id": "cert_demo_0001",
		"registry_revision": 3,
		"activated_at": "2026-08-12T00:00:00Z",
	}
	var content_ref := {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": "b".repeat(64)}
	var bootstrap := {
		"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]},
		"content": content_ref,
		"activation": {
			"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"},
			"registry_revision": 3,
			"active": active,
		},
		"session": {
			"current_session_id": "session_demo_0001",
		},
		"world": {
			"world_id": "world_demo_0001",
			"revision": 1,
			"last_event_sequence": 5,
			"state_hash": "c".repeat(64),
		},
	}
	if not first.bind_authority("https://API.YAYA.EXAMPLE:443/", bootstrap).get("ok", false):
		push_error("ClientStore must accept a closed normalized origin/actor/content authority binding.")
		quit(1)
		return
	first.set_authoritative_bootstrap(bootstrap)
	first.set_authoritative_session({"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": content_ref})
	first.replace_world(_snapshot())
	first.set_interaction_cursor(7)
	var request := {
		"turn_id": "turn_demo_0001",
		"expected_world_revision": 1,
		"input": {"type": "ASSIGNED_TASK", "task_id": "task_demo_0001"},
		"skill_bindings": [{
			"skill_id": active.skill_id,
			"skill_version_id": active.skill_version_id,
			"artifact_sha256": active.artifact_sha256,
			"certification_id": active.certification_id,
		}],
		"client_state": {"last_event_sequence": 5, "client_turn_sequence": 1},
	}
	var identity := JSON.stringify({
		"session_id": "session_demo_0001",
		"world_revision": 1,
		"last_event_sequence": 5,
		"client_turn_sequence": 1,
		"input": request.input,
		"skill_bindings": request.skill_bindings,
	}).sha256_text()
	var pending_result := first.ensure_pending_operation("agent_turn", identity, {
		"session_id": "session_demo_0001",
		"turn_id": "turn_demo_0001",
		"idempotency_key": RequestContextFactory.idempotency_key_for("createAgentTurn", "session_demo_0001:turn_demo_0001"),
		"request": request,
		"pre_world": _snapshot(),
		"interaction_cursor_before": 7,
	})
	if not pending_result.get("ok", false):
		push_error("Semantically bound pending Turn must persist: %s" % str(pending_result))
		quit(1)
		return
	first.queue_free()
	await process_frame

	var restored := ClientStoreScript.new()
	restored.name = "PersistenceStoreTwo"
	restored.configure_persistence(path, true, true)
	root.add_child(restored)
	await process_frame
	if (
		restored.active_skill_tuple != active
		or str(restored.authoritative_session.get("session_id", "")) != "session_demo_0001"
		or str(restored.get_pending_operation("agent_turn").get("turn_id", "")) != "turn_demo_0001"
		or restored.last_interaction_sequence != 7
		or str(restored.authority_binding.get("api_base_url", "")) != "https://api.yaya.example"
	):
		push_error("ClientStore must restore authority/envelope: active=%s session=%s pending=%s" % [str(restored.active_skill_tuple), str(restored.authoritative_session), str(restored.pending_operations)])
		quit(1)
		return
	restored.set_authoritative_session({"session_id": "session_demo_0001"})
	if restored.last_interaction_sequence != 7 or restored.get_pending_operation("agent_turn").is_empty():
		push_error("Reconfirming the same Session must preserve its cursor and pending envelopes.")
		quit(1)
		return
	restored.set_authoritative_session({"session_id": "session_demo_0002"})
	if restored.last_interaction_sequence != 0 or not restored.pending_operations.is_empty():
		push_error("Changing the authoritative Session must reset Session-scoped cursors and envelopes.")
		quit(1)
		return
	restored.bind_authority("https://api.yaya.example", bootstrap)
	restored.set_authoritative_bootstrap(bootstrap)
	restored.set_authoritative_session({"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": content_ref})
	restored.replace_world(_snapshot())
	restored.begin_authority_revalidation()
	if not restored.world_snapshot.is_empty():
		push_error("Persisted World authority must be quarantined before a new Bootstrap is verified.")
		quit(1)
		return
	var same_binding := restored.bind_authority("https://API.YAYA.EXAMPLE:443///", bootstrap)
	if same_binding.get("changed", true) or not restored.world_snapshot.is_empty():
		push_error("Origin/actor/content binding alone must not restore a quarantined World before exact Session/world verification.")
		quit(1)
		return
	restored.set_authoritative_bootstrap(bootstrap)
	restored.set_authoritative_session({"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": content_ref})
	var completed_revalidation := restored.complete_authority_revalidation(bootstrap, restored.authoritative_session)
	if not completed_revalidation.get("restored", false) or restored.world_snapshot != _snapshot():
		push_error("Exact Bootstrap world and canonical Session must restore the quarantined World only after full verification.")
		quit(1)
		return
	restored.begin_authority_revalidation()
	var drifted_bootstrap := bootstrap.duplicate(true)
	drifted_bootstrap.world.revision = int(bootstrap.world.revision) + 1
	drifted_bootstrap.world.last_event_sequence = int(bootstrap.world.last_event_sequence) + 1
	drifted_bootstrap.world.state_hash = "d".repeat(64)
	restored.bind_authority("https://api.yaya.example", drifted_bootstrap)
	restored.set_authoritative_bootstrap(drifted_bootstrap)
	restored.set_authoritative_session({"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": content_ref})
	var drifted_revalidation := restored.complete_authority_revalidation(drifted_bootstrap, restored.authoritative_session)
	if drifted_revalidation.get("restored", true) or not restored.world_snapshot.is_empty():
		push_error("A changed Bootstrap World must discard, never signal, the prior quarantined World.")
		quit(1)
		return
	var changed_binding := restored.bind_authority("https://other.yaya.example", bootstrap)
	if (
		not changed_binding.get("changed", false)
		or not restored.world_snapshot.is_empty()
		or not restored.authoritative_session.is_empty()
		or not restored.active_skill_tuple.is_empty()
	):
		push_error("Changing API origin must clear old World, Session and active Skill authority.")
		quit(1)
		return
	restored.queue_free()
	await process_frame
	DirAccess.remove_absolute(absolute)
	print("CLIENT_STORE_TEST_PASS")
	quit(0)


func _snapshot() -> Dictionary:
	return {
		"world_id": "world_demo_0001",
		"revision": 1,
		"last_event_sequence": 5,
		"state_schema_version": "1.0.0",
		"state_hash": "c".repeat(64),
		"world_rules_version": "farm-rules-1",
		"state": {},
	}
