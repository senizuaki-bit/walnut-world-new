extends LineEdit

signal validation_changed

@export var expected_value := ""
@export var error_message := "请检查输入。"
var has_error := false
var _hovered := false


func _ready() -> void:
	text_changed.connect(func(_value: String) -> void:
		has_error = false
		_refresh_skin()
		validation_changed.emit()
	)
	mouse_entered.connect(func() -> void:
		_hovered = true
		_refresh_skin()
	)
	mouse_exited.connect(func() -> void:
		_hovered = false
		_refresh_skin()
	)
	_refresh_skin()


func validate() -> bool:
	has_error = text.strip_edges() != expected_value
	_refresh_skin()
	return not has_error


func clear_validation() -> void:
	has_error = false
	_refresh_skin()


func _refresh_skin() -> void:
	set_meta("invalid", has_error)
	var state := "error" if has_error else ("filled" if not text.is_empty() else ("hover" if _hovered else "normal"))
	add_theme_stylebox_override("normal", load("res://resources/ui/art_v2/input_%s.tres" % state))
	tooltip_text = error_message if has_error else ""
