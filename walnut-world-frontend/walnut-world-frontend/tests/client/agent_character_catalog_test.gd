extends SceneTree

const CATALOG: AgentCharacterCatalog = preload("res://resources/agent/agent_character_catalog.tres")

const EXPECTED := {
	&"world_agent": ["芽芽", "yaya_sprout.png", &"yaya"],
	&"xiaohutao": ["小核桃", "little_walnut.png", &"little_walnut"],
	&"teaching_agent": ["叮当师傅", "master_ding_dang.png", &"master_ding_dang"],
	&"bug_agent": ["Bug 先生", "pest_bug.png", &"bug_legion"],
	&"book_agent": ["书书", "shu_shu.png", &"shu_shu"],
}


func _initialize() -> void:
	var validation := CATALOG.validate()
	if not bool(validation.get("ok", false)):
		push_error(str(validation.get("message", "catalog validation failed")))
		quit(1)
		return
	for role_id: StringName in EXPECTED:
		var profile := CATALOG.profile_for(role_id)
		var expected: Array = EXPECTED[role_id]
		if (
			profile == null
			or profile.display_name != str(expected[0])
			or profile.portrait == null
			or not profile.portrait.resource_path.ends_with(str(expected[1]))
			or profile.world_presentation_key != expected[2]
		):
			push_error("Agent character profile mismatch for %s." % role_id)
			quit(1)
			return
	if CATALOG.profile_for(&"system") != null or CATALOG.profile_for(&"unknown_agent") != null:
		push_error("System and unknown roles must not resolve to a character profile.")
		quit(1)
		return
	print("AGENT_CHARACTER_CATALOG_TEST_PASS")
	quit(0)
