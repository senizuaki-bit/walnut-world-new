extends SceneTree

const ResultPanelScript := preload("res://scenes/task/result_panel.gd")


func _initialize() -> void:
	var build_text: String = ResultPanelScript.format_build({
		"status": "CERTIFIED",
		"artifact": {"compiler_profile": "YAYA_CPP20_SAFE_V1", "test_suite_version": "farm-water-v3"},
		"phases": [
			{"name": "COMPILE", "status": "PASSED", "diagnostic_codes": []},
			{"name": "PUBLIC_TEST", "status": "PASSED", "diagnostic_codes": ["TEST_WATER_01"]},
		],
		"evidence_refs": [{"evidence_id": "evidence_build_001", "evidence_type": "TEST_REPORT"}],
	})
	var run_text: String = ResultPanelScript.format_run({
		"status": "SUCCEEDED",
		"sandbox": {"status": "SUCCEEDED", "usage": {"cpu_ms": 31, "wall_ms": 46, "peak_memory_bytes": 4194304}},
		"world_application": {"status": "COMMITTED", "receipt": {"previous_revision": 4, "world_revision": 5, "first_event_sequence": 7, "last_event_sequence": 8}},
		"agent_feedback": {"source": "provider_fallback", "message": "已完成", "degraded": true, "fallback_reason": "provider timeout"},
		"evidence_refs": [{"evidence_id": "evidence_world_001", "evidence_type": "WORLD_COMMIT"}],
	})
	if not _contains_all(build_text, ["构建结果 · CERTIFIED", "编译：PASSED", "课程测试：PASSED（TEST_WATER_01）", "Evidence："]):
		push_error("Build result formatter must show distinct build phases and evidence.")
		quit(1)
		return
	if not _contains_all(run_text, ["Sandbox：SUCCEEDED", "世界提交：COMMITTED", "世界版本：4 → 5", "已降级：provider timeout", "Evidence："]):
		push_error("Run result formatter must show runtime, world receipt, fallback, and evidence.")
		quit(1)
		return
	print("RESULT_PANEL_FORMAT_TEST_PASS")
	quit(0)


func _contains_all(text: String, expected: Array[String]) -> bool:
	for value: String in expected:
		if not text.contains(value):
			return false
	return true
