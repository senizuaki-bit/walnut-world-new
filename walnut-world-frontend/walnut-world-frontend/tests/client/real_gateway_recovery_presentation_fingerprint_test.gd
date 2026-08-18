extends SceneTree

const RecoveryScript := preload("res://tests/client/real_gateway_chain_recovery_e2e_test.gd")


func _initialize() -> void:
	var recovery := RecoveryScript.new()
	if not recovery.has_method("_bind_presentation_authority_fingerprint"):
		return _fail("Recovery verifier has no pre-comparison presentation fingerprint binder.")
	var base := {"world_revision": 4}
	var enabled: Dictionary = recovery.call(
		"_bind_presentation_authority_fingerprint", base, true, 8,
	)
	if (
		enabled.get("world_revision") != 4
		or int(enabled.get("presentation_high_watermark", -1)) != 8
		or base.has("presentation_high_watermark")
	):
		return _fail("Enabled recovery did not bind the verified WorldEventPlayer cursor before comparison.")
	var disabled: Dictionary = recovery.call(
		"_bind_presentation_authority_fingerprint", base, false, 99,
	)
	if int(disabled.get("presentation_high_watermark", -1)) != 0:
		return _fail("Disabled recovery must bind presentation_high_watermark=0 regardless of a supplied cursor.")
	var source := FileAccess.get_file_as_string("res://tests/client/real_gateway_chain_recovery_e2e_test.gd")
	if (
		not source.contains("presentation_enabled,\n\t\tpresentation_high_watermark,")
		or not source.contains("var presentation_high_watermark := 0")
		or not source.contains("presentation_high_watermark = int(presentation_player.call(\"get_cursor\"))")
		or not source.contains("var recovered_fingerprint := _bind_presentation_authority_fingerprint")
	):
		return _fail("Formal recovery does not pass the startup player cursor into authority verification.")
	recovery.free()
	print("REAL_GATEWAY_RECOVERY_PRESENTATION_FINGERPRINT_TEST_PASS")
	quit(0)


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
