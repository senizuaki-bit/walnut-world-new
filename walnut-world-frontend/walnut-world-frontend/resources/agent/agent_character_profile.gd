class_name AgentCharacterProfile
extends Resource

@export var role_id: StringName = &""
@export var display_name: String = ""
@export var portrait: Texture2D
@export var world_presentation_key: StringName = &""


func is_valid() -> bool:
	return (
		not role_id.is_empty()
		and not display_name.strip_edges().is_empty()
		and portrait != null
		and not world_presentation_key.is_empty()
	)
