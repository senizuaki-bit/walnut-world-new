extends ConfirmationDialog


func _ready() -> void:
	theme = preload("res://resources/ui/art_v2/theme.tres").duplicate()
	var paper := preload("res://resources/ui/art_v2/ui-panel.tres")
	theme.set_stylebox("panel", "AcceptDialog", paper)
	var border := StyleBoxFlat.new()
	border.bg_color = Color(0.98, 0.92, 0.73)
	border.set_content_margin_all(12)
	border.content_margin_top = 36
	theme.set_stylebox("embedded_border", "Window", border)
	theme.set_stylebox("embedded_unfocused_border", "Window", border)
	theme.set_color("title_color", "Window", Color(0.075, 0.145, 0.11))
	theme.set_color("title_unfocused_color", "Window", Color(0.075, 0.145, 0.11))
	theme.set_font_size("title_font_size", "Window", 20)
	for state in ["normal", "hover", "pressed", "disabled"]:
		var skin := theme.get_stylebox(state, "Button").duplicate() as StyleBox
		skin.content_margin_left = 20
		skin.content_margin_right = 20
		skin.content_margin_top = 12
		skin.content_margin_bottom = 12
		theme.set_stylebox(state, "Button", skin)
	# Native focus is painted over the button; keep its center transparent.
	theme.set_stylebox("focus", "Button", preload("res://resources/ui/art_v2/input_focus.tres"))
