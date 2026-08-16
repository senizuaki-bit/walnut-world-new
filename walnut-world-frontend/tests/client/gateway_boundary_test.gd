extends SceneTree

const Gateway := preload("res://addons/yaya_contract_client/agent_api_gateway.gd")

func _initialize() -> void:
	var gateway := Gateway.new()
	var result: Dictionary = await gateway.get_bootstrap({
		"schema_version": "1.0.0",
		"request_id": "req_frontend_000001",
		"correlation_id": "corr_frontend_000001",
		"trace_id": "trace_frontend_000001",
		"requested_at": "2026-08-09T00:00:00Z",
		"actor": {
			"tenant_id": "tenant_demo",
			"actor_id": "student_demo",
			"actor_type": "student",
			"roles": ["game:player"],
		},
		"content_ref": {
			"unit_id": "YAYA_FARM_001",
			"version": "1.0.0",
			"content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		},
	})
	if result.get("ok") != false or result.get("status") != 0 or result.get("error", {}).get("scope") != "CLIENT_LOCAL":
		push_error("未配置 Transport 时，Gateway 必须返回显式本地错误，不能向 UI 伪造默认数据。")
		quit(1)
		return
	print("GATEWAY_BOUNDARY_TEST_PASS")
	quit(0)
