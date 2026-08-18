extends SceneTree

## Regression: a Turn that fails at the transport boundary (an expired JWT is the
## real-world case) must not strand flow_state in TURN_RUNNING.
##
## _execute_pending_turn_envelope sets TURN_RUNNING before submitting and every
## failure path returned early without restoring it. TURN_RUNNING is not in the
## readiness gate's ready_states, so the marker outlived the operation that set it.
##
## The invariant under test is that the transient marker never outlives its
## operation, which matters most on AppRoot's startup recovery path -- that caller
## reports startup failure without going through report_error, so nothing else
## would ever clear TURN_RUNNING.
##
## Note what is NOT claimed here. report_error moves the flow to ERROR, and ERROR
## does not disable later student actions: a failed Turn is settled knowledge, not
## lost authority, and the learner must be able to retry it or ask 问叮当 for help.
## Authority itself is gated separately and strictly (submit_and_run_flow_test).
## What must not happen is the reverse -- a failed Turn silently *restoring* a
## marker-carrying flow, which is what the last block below pins.

const StoreScript := preload("res://autoload/client_store.gd")
const ControllerScript := preload("res://autoload/session_controller.gd")


class Game:
	extends RefCounted
	var submit_attempts := 0

	func submit_agent_turn(_attempt: Dictionary, _session: String, _key: String, _value: Dictionary) -> Dictionary:
		submit_attempts += 1
		return {
			"ok": false,
			"status": 401,
			"headers": {},
			"error": {
				"scope": "HTTP",
				"code": "UNAUTHORIZED",
				"message": "JWT has expired",
				"retryable": false,
			},
		}

	func get_command(_attempt: Dictionary, command_id: String) -> Dictionary:
		return {"ok": true, "value": {"command_id": command_id, "terminal": true, "status": "APPLIED", "links": {}}}

	func get_run(_attempt: Dictionary, _run_id: String) -> Dictionary:
		return {"ok": false}


class Product:
	extends RefCounted
	func list_interactions(_attempt: Dictionary, _session_id: String, after_sequence: int, _limit: int) -> Dictionary:
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

	# A Hint whose Turn is rejected at the transport boundary.
	await controller.request_hint()
	if game.submit_attempts != 1:
		failures.append("失败前提不成立：Hint 必须真的提交过一次 Turn。")
	if store.flow_state == WalnutClientStore.FlowState.TURN_RUNNING:
		failures.append("Turn 在传输层失败后不得把 flow_state 滞留在 TURN_RUNNING。")

	# The envelope is deliberately retained for reconciliation. Replaying it through
	# the startup recovery entry point exercises the caller that does NOT funnel the
	# failure through report_error, so only the executor itself can clear the marker.
	store.set_flow(WalnutClientStore.FlowState.ACTIVE)
	var recovery: Dictionary = await controller.recover_pending_turn_operations()
	if bool(recovery.get("ok", false)):
		failures.append("失败前提不成立：401 之下的 pending Turn 回收必须失败。")
	if store.flow_state == WalnutClientStore.FlowState.TURN_RUNNING:
		failures.append("回收失败的 pending Turn 不得把 flow_state 滞留在 TURN_RUNNING。")
	if store.flow_state != WalnutClientStore.FlowState.ACTIVE:
		failures.append("回收失败后必须原样恢复进入前的 flow_state，不得擅自升级或降级。")

	# And the marker must never be rewritten by a failure: whatever report_error
	# recorded has to survive a failed Turn unchanged.
	store.report_error({"scope": "HTTP", "code": "UNAUTHORIZED", "message": "JWT has expired"})
	var flow_before_error_turn := store.flow_state
	var recovery_from_error: Dictionary = await controller.recover_pending_turn_operations()
	if bool(recovery_from_error.get("ok", false)):
		failures.append("失败前提不成立：401 之下的第二次回收同样必须失败。")
	if store.flow_state != flow_before_error_turn:
		failures.append("失败的 Turn 不得改写 report_error 记录下来的 flow_state。")

	# The learner's way out. One failed Run used to take away every button --
	# including 问叮当, the one thing a stuck student actually needs -- until the
	# game was restarted. Authority is untouched by a failure, so the next action
	# must still be allowed.
	if not controller._student_action_readiness("Hint").get("ok", false):
		failures.append("一次失败的 Turn 之后必须还能问叮当，不能把学生锁死到重启为止。")
	if not controller._student_action_readiness("Run").get("ok", false):
		failures.append("一次失败的 Turn 之后必须还能重新运行代码。")

	store.queue_free()
	await process_frame
	if failures.is_empty():
		print("TURN_FAILURE_FLOW_RECOVERY_TEST_PASS: TURN_RUNNING 标记不再越过它的操作存活")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
