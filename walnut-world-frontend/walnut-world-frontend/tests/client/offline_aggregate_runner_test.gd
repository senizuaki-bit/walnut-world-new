extends SceneTree


func _initialize() -> void:
	var source := FileAccess.get_file_as_string("res://scripts/run-offline-tests.ps1")
	var required := [
		"$realOptInTests = @(",
		"'tests/client/real_gateway_chain_e2e_test.gd'",
		"'tests/client/real_gateway_chain_recovery_e2e_test.gd'",
		"status = 'EXCLUDED_NOT_RUN'",
		"runner = 'scripts/run-real-gateway-e2e.ps1'",
		"skipped = 0",
		"if ($realOptInTests -contains $relative)",
		"REAL_OPT_IN_TEST_REPORT ",
		"OFFLINE_TEST_SUMMARY ",
	]
	for fragment: String in required:
		if not source.contains(fragment):
			push_error("Offline aggregate runner is missing exclusion/reporting guard: %s" % fragment)
			quit(1)
			return
	if source.contains("REAL_GATEWAY_CHAIN_E2E_SKIP"):
		push_error("Offline aggregate runner must exclude the real test, not count its internal skip.")
		quit(1)
		return
	print("OFFLINE_AGGREGATE_RUNNER_TEST_PASS")
	quit(0)
