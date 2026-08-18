extends SceneTree

## Regression: a Turn the gateway REFUSED must not strand its envelope.
##
## Every student action reconciles pending Turn envelopes before doing anything
## else, and returns early while one is still open. That cross-slot reconcile is
## deliberate and must stay: a pending Hint carries no Run, but the gateway did
## advance last_turn_sequence when it accepted it, so a later Turn would compute
## the wrong client_turn_sequence if it skipped ahead.
##
## The bug was the other half: when the gateway refuses the submission outright
## (422 SKILL_NOT_CERTIFIED was the real "问叮当" case), the envelope was kept for
## reconciliation that can never succeed -- the replay reuses the same body and
## gets the same 422 forever. One refused hint therefore blocked "直接运行" and
## every later action in the session, permanently.
##
## A refused request never became a Turn: the envelope always replays under its
## original Idempotency-Key, so an accepted Turn would come back 202 instead.
## There is no server state to reconcile, so the envelope is dropped and the
## student can act again. 401 is excluded on purpose and pinned below, because a
## fresh token still recovers that envelope.

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")


class Game:
	extends RefCounted
	var status := 422
	var code := "SKILL_NOT_CERTIFIED"
	var submissions := 0

	func submit_agent_turn(_a: Dictionary, _s: String, _k: String, _v: Dictionary) -> Dictionary:
		submissions += 1
		return {
			"ok": false,
			"status": status,
			"headers": {},
			"error": {
				"scope": "HTTP",
				"code": code,
				"message": "gateway refused the turn",
				"retryable": false,
			},
		}

	func get_command(_a: Dictionary, command_id: String) -> Dictionary:
		return {"ok": true, "value": {"command_id": command_id, "terminal": true, "status": "APPLIED", "links": {}}}

	func get_run(_a: Dictionary, _run_id: String) -> Dictionary:
		return {"ok": false}


class Product:
	extends RefCounted
	func list_interactions(_a: Dictionary, _s: String, after_sequence: int, _l: int) -> Dictionary:
		return {
			"ok": true,
			"value": {
				"interactions": [],
				"next_after_sequence": after_sequence,
				"high_watermark_sequence": after_sequence,
				"has_more": false,
			},
		}


func _initialize() -> void:
	var failures: Array[String] = []

	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	if store == null:
		store = StoreScript.new()
		store.name = "ClientStore"
		root.add_child(store)
	var controller := root.get_node_or_null("SessionController") as Node
	if controller == null:
		controller = ControllerScript.new()
		controller.name = "SessionController"
		root.add_child(controller)
	await process_frame
	store.persistence_enabled = false

	var bootstrap := {
		"actor": {"tenant_id": "tenant_demo", "actor_id": "learner_demo_0001", "actor_type": "student", "roles": ["student"]},
		"content": {"unit_id": "TASK_DEMO_001", "version": "1.0.0", "content_hash": "d".repeat(64)},
		"session": {"current_session_id": "session_demo_0001"},
		"activation": {"scope": {"world_id": "world_demo_0001", "agent_profile_id": "profile_demo_0001"}, "registry_revision": 0, "active": null},
	}
	var session := {"session_id": "session_demo_0001", "world_id": "world_demo_0001", "content": bootstrap.content}
	store.set_authoritative_bootstrap(bootstrap)
	store.set_authoritative_session(session)
	store.set_workspace({"session": {"session_id": "session_demo_0001", "status": "ACTIVE", "last_turn_sequence": 3}, "current_task": {"task_id": "task_demo_0001"}})
	store.replace_world({"world_id": "world_demo_0001", "revision": 4, "last_event_sequence": 7, "state_schema_version": "1.0.0", "state_hash": "a".repeat(64), "world_rules_version": "rules", "state": {}})

	var game := Game.new()
	controller.configure(game, Product.new())
	controller.configure_authority(bootstrap, session)
	controller.configure_polling({"initial_delay_seconds": 0.0, "base_delay_seconds": 0.0, "max_delay_seconds": 0.0, "jitter_ratio": 0.0, "interaction_delay_seconds": 0.0, "interaction_deadline_seconds": 0.2})
	controller.configure_draft_context({"attempt": {}})
	store.set_flow(WalnutClientStore.FlowState.ACTIVE)

	# 问叮当 refused by the gateway, exactly as the un-fixed backend did.
	await controller.request_hint()
	if game.submissions != 1:
		failures.append("失败前提不成立：问叮当必须真的提交过一次 Turn。")
	if not store.get_pending_operation("agent_hint").is_empty():
		failures.append("被网关拒绝的问叮当不得留下无法和解的 envelope，否则会永久挡住后续操作。")

	# The next student action must reach the gateway instead of being consumed by
	# reconciliation of the refused hint.
	store.set_flow(WalnutClientStore.FlowState.ACTIVE)
	await controller.request_hint()
	if game.submissions != 2:
		failures.append("被拒的问叮当之后，下一次学生操作必须真正发出请求，而不是被残留 envelope 吞掉。")

	# 401 is a different story: a fresh token still recovers that envelope, so it
	# must survive. This is the boundary of the fix above.
	game.status = 401
	game.code = "UNAUTHORIZED"
	store.set_flow(WalnutClientStore.FlowState.ACTIVE)
	await controller.request_hint()
	if store.get_pending_operation("agent_hint").is_empty():
		failures.append("401 之下的 envelope 必须保留：换一个 token 就能继续和解。")

	if failures.is_empty():
		print("REFUSED_TURN_DOES_NOT_BLOCK_LATER_ACTIONS_TEST_PASS")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
