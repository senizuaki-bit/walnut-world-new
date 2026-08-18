class_name AgentCharacterCatalog
extends Resource

const REQUIRED_ROLE_IDS: Array[StringName] = [
	&"world_agent",
	&"xiaohutao",
	&"teaching_agent",
	&"bug_agent",
	&"book_agent",
]

@export var profiles: Array[AgentCharacterProfile] = []


func profile_for(role_id: StringName) -> AgentCharacterProfile:
	for profile in profiles:
		if profile != null and profile.role_id == role_id:
			return profile
	return null


func validate() -> Dictionary:
	var seen: Dictionary = {}
	for profile in profiles:
		if profile == null or not profile.is_valid():
			return {"ok": false, "message": "Agent character catalog contains an invalid profile."}
		if seen.has(profile.role_id):
			return {"ok": false, "message": "Agent character catalog contains a duplicate role_id: %s" % profile.role_id}
		seen[profile.role_id] = true
	for role_id in REQUIRED_ROLE_IDS:
		if not seen.has(role_id):
			return {"ok": false, "message": "Agent character catalog is missing required role_id: %s" % role_id}
	if seen.size() != REQUIRED_ROLE_IDS.size():
		return {"ok": false, "message": "Agent character catalog contains an unsupported role_id."}
	return {"ok": true, "message": ""}
