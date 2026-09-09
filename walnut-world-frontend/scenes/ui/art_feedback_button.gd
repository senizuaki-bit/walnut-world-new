extends Button
## Render the motion atlas as the native button skin, below its text and focus ring.

@onready var feedback: ArtMotionTexture = $Feedback
var _skin: StyleBoxTexture
var _rest_styles: Dictionary = {}


func _ready() -> void:
	for state in ["normal", "hover"]:
		_rest_styles[state] = get_theme_stylebox(state) if has_theme_stylebox_override(state) else null
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
	for state in ["normal", "hover"]:
		if _rest_styles.get(state) != null:
			add_theme_stylebox_override(state, _rest_styles[state])
		else:
			remove_theme_stylebox_override(state)
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
