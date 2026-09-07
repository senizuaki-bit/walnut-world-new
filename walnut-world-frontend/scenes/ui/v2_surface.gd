extends NinePatchRect
## CSS border-image equivalent: source slice and displayed border are independent.
## Authored child node, never creates UI nodes or changes gameplay state.
@export var asset_id := "ui-panel"
@export var source_slice := 64.0
@export var border_width := 22.954
const BASE := "res://assets/art/redesign/crop_adaptive/v2/components/"
var _host: Control
var _last_asset := ""

func _ready() -> void:
	_host = get_parent() as Control
	_host.resized.connect(_fit)
	_fit()
	_update_texture()

func _fit() -> void:
	var fitted_border := minf(border_width, minf(_host.size.x, _host.size.y) * 0.5)
	scale = Vector2.ONE * maxf(fitted_border, 0.01) / source_slice
	size = _host.size / scale
	position = Vector2.ZERO

func _process(_delta: float) -> void:
	if is_visible_in_tree():
		_update_texture()

func _update_texture() -> void:
	var feedback := _host.get_node_or_null("Feedback") as ArtMotionTexture
	if feedback != null and feedback.visible and not (_host as Button).disabled:
		texture = feedback.texture
		_last_asset = ""
		return
	var next := asset_id
	if _host is Button:
		var button := _host as Button
		if button.disabled:
			next = "button-disabled"
		elif button.is_pressed():
			next = "button-pressed"
		elif button.has_meta("art_feedback"):
			next = "button-" + str(button.get_meta("art_feedback"))
		elif button.has_focus():
			next = "button-focus"
		elif button.is_hovered():
			next = "button-hover"
	elif _host is LineEdit:
		var field := _host as LineEdit
		if not field.editable:
			next = "input-disabled"
		elif bool(field.get_meta("invalid", false)):
			next = "input-error"
		elif field.has_focus():
			next = "input-focus"
		elif field.get_global_rect().has_point(field.get_global_mouse_position()):
			next = "input-hover"
		elif not field.text.is_empty():
			next = "input-filled"
	if next == _last_asset:
		return
	_last_asset = next
	texture = load(BASE + next + ".png")
