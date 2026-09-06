extends AnimatedSprite2D
## Reduced motion changes rendering only; the existing action clock still finishes.

@onready var still: Sprite2D = $Still


func _ready() -> void:
	still.texture = sprite_frames.get_frame_texture(&"pour", 0)
	visibility_changed.connect(func() -> void: set_process(is_visible_in_tree()))
	set_process(is_visible_in_tree())


func _process(_delta: float) -> void:
	var reduce := bool(Engine.get_meta("art_reduced_motion", false))
	self_modulate.a = 0.0 if reduce else 1.0
	still.visible = reduce
