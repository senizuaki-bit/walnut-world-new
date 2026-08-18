extends SceneTree

const CommandPollerScript := preload("res://scripts/client/command_poller.gd")


class FakeClock:
	extends RefCounted
	var now_msec := 0
	var waits: Array[float] = []

	func now() -> int:
		return now_msec

	func wait(seconds: float) -> void:
		waits.append(seconds)
		now_msec += roundi(seconds * 1000.0)


class ContextSource:
	extends RefCounted
	var calls := 0

	func fresh() -> Dictionary:
		calls += 1
		return {"attempt": calls}


class FakeGateway:
	extends RefCounted
	var calls := 0
	var contexts: Array[Dictionary] = []

	func get_command(context: Dictionary, command_id: String) -> Dictionary:
		calls += 1
		contexts.append(context.duplicate(true))
		if calls == 1:
			return {
				"ok": false,
				"status": 503,
				"headers": {"retry-after": "2"},
				"error": {"retryable": true},
			}
		return {
			"ok": true,
			"status": 200,
			"headers": {},
			"value": {
				"command_id": command_id,
				"terminal": calls >= 3,
				"status": "APPLIED" if calls >= 3 else "VALIDATING",
			},
		}


class SlowGateway:
	extends RefCounted
	var clock: FakeClock

	func _init(source: FakeClock) -> void:
		clock = source

	func get_command(_context: Dictionary, command_id: String) -> Dictionary:
		clock.now_msec += 2500
		return {
			"ok": true,
			"status": 200,
			"headers": {},
			"value": {"command_id": command_id, "terminal": true, "status": "APPLIED"},
		}


func _initialize() -> void:
	var gateway := FakeGateway.new()
	var clock := FakeClock.new()
	var contexts := ContextSource.new()
	var poller := CommandPollerScript.new(
		gateway,
		Callable(contexts, "fresh"),
		{
			"deadline_seconds": 10.0,
			"initial_delay_seconds": 0.5,
			"base_delay_seconds": 0.5,
			"max_delay_seconds": 4.0,
			"jitter_ratio": 0.0,
		},
		Callable(clock, "wait"),
		Callable(clock, "now"),
		func() -> float: return 0.5,
	)
	var result: Dictionary = await poller.reconcile(
		{},
		{
			"ok": true,
			"headers": {"retry-after": "1"},
			"value": {"command_id": "cmd_demo_0001"},
		},
	)
	if (
		not result.get("ok", false)
		or gateway.calls != 3
		or contexts.calls != 3
		or gateway.contexts[0] == gateway.contexts[1]
		or clock.waits != [1.0, 2.0, 1.0]
	):
		push_error("Poller must use fresh contexts and honor accepted/error Retry-After plus exponential backoff: %s" % str(clock.waits))
		quit(1)
		return

	var timeout_clock := FakeClock.new()
	var timeout_gateway := FakeGateway.new()
	var timeout_poller := CommandPollerScript.new(
		timeout_gateway,
		Callable(contexts, "fresh"),
		{"deadline_seconds": 2.0, "jitter_ratio": 0.0},
		Callable(timeout_clock, "wait"),
		Callable(timeout_clock, "now"),
	)
	var timeout: Dictionary = await timeout_poller.reconcile(
		{},
		{"ok": true, "headers": {"retry-after": "5"}, "value": {"command_id": "cmd_demo_0002"}},
	)
	if timeout.get("ok", true) or timeout_gateway.calls != 0 or str(timeout.get("error", {}).get("code", "")) != "RESOURCE_RECONCILIATION_TIMEOUT":
		push_error("Total deadline must stop a Retry-After wait that cannot fit.")
		quit(1)
		return

	var slow_clock := FakeClock.new()
	var slow_poller := CommandPollerScript.new(
		SlowGateway.new(slow_clock),
		Callable(contexts, "fresh"),
		{"deadline_seconds": 2.0, "initial_delay_seconds": 0.0, "jitter_ratio": 0.0},
		Callable(slow_clock, "wait"),
		Callable(slow_clock, "now"),
	)
	var late: Dictionary = await slow_poller.reconcile(
		{}, {"ok": true, "headers": {}, "value": {"command_id": "cmd_demo_0003"}},
	)
	if late.get("ok", true) or str(late.get("error", {}).get("code", "")) != "RESOURCE_RECONCILIATION_TIMEOUT":
		push_error("A terminal response observed after the total deadline must not be accepted.")
		quit(1)
		return
	print("COMMAND_POLLER_TEST_PASS")
	quit(0)
