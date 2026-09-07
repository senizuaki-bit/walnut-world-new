extends LineEdit
## Editing a rejected value clears only that field's visual error.
var invalid := false:
	set(value):
		invalid = value
		set_meta("invalid", value)

func _ready() -> void:
	text_changed.connect(func(_value: String): invalid = false)
