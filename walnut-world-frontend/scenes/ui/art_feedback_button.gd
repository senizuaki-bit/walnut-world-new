extends Button
## Render the motion atlas as the native button skin, below its text and focus ring.

@onready var feedback: ArtMotionTexture = $Feedback
var _skin: StyleBoxTexture


func _ready() -> void:
	add_theme_stylebox_override("focus", preload("res://resources/ui/art_v2/input_focus.tres"))
	visibility_changed.connect(func() -> void:
		if not is_visible_in_tree():
			clear_feedback()
	)
	set_process(false)


func show_feedback(success: bool) -> void:
	_skin = preload("res://resources/ui/art_v2/button_normal.tres").duplicate()
	# Union of the alpha bounds across each clip: exclude transparent canvas padding.
	_skin.region_rect = Rect2(35, 26, 570, 172) if success else Rect2(34, 28, 573, 168)
	feedback.show()
	feedback.play_clip("button-success-motion" if success else "button-error-motion", true)
	_skin.texture = feedback.texture
	add_theme_stylebox_override("normal", _skin)
	add_theme_stylebox_override("hover", _skin)
	for color in ["font_color", "font_hover_color", "font_focus_color"]:
		add_theme_color_override(color, Color(1.0, 0.98, 0.88))
	set_process(true)


func clear_feedback() -> void:
	feedback.hide()
	remove_theme_stylebox_override("normal")
	remove_theme_stylebox_override("hover")
	for color in ["font_color", "font_hover_color", "font_focus_color"]:
		remove_theme_color_override(color)
	_skin = null
	set_process(false)


func _process(_delta: float) -> void:
	if not feedback.visible:
		clear_feedback()
		return
	# Loading replaces the poster with an AtlasTexture; subsequent frames update it in place.
	if _skin.texture != feedback.texture:
		_skin.texture = feedback.texture
