extends CheckButton


func _ready() -> void:
	set_pressed_no_signal(bool(Engine.get_meta("art_reduced_motion", false)))
	toggled.connect(func(value: bool) -> void: Engine.set_meta("art_reduced_motion", value))
