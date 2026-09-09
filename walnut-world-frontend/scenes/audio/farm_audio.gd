class_name FarmAudio
extends Node
## Fixed, editor-visible players. Audio never advances the lesson or world state.

signal cue_played(cue: StringName)

var _screen: Control
var _last_played: Dictionary = {}
var _ambience_enabled := false


func _ready() -> void:
	_screen = get_parent() as Control
	_screen.visibility_changed.connect(_on_screen_visibility_changed)
	call_deferred("_bind_buttons")


func _bind_buttons() -> void:
	for node: Node in _screen.find_children("*", "BaseButton", true, false):
		var button := node as BaseButton
		var callback := _on_button_pressed.bind(button)
		if not button.pressed.is_connected(callback):
			button.pressed.connect(callback)


func _on_button_pressed(_button: BaseButton) -> void:
	# The native button may hide itself in its preceding pressed handler.
	# Screen visibility is checked by play_cue; disabled buttons do not emit pressed.
	play_cue(&"Click")


func play_cue(cue: StringName) -> void:
	if not is_instance_valid(_screen) or not _screen.is_visible_in_tree():
		return
	var player := get_node_or_null(NodePath(cue)) as AudioStreamPlayer
	if player == null:
		return
	var now := Time.get_ticks_msec()
	if now - int(_last_played.get(cue, -1000)) < 100:
		return
	_last_played[cue] = now
	player.play()
	cue_played.emit(cue)


func start_watering() -> void:
	play_cue(&"Watering")


func stop_watering() -> void:
	$Watering.stop()


func set_ambience_enabled(enabled: bool) -> void:
	_ambience_enabled = enabled
	_refresh_ambience()


func _refresh_ambience() -> void:
	if _ambience_enabled and is_instance_valid(_screen) and _screen.is_visible_in_tree():
		if not $Birds.playing:
			$Birds.play()
	else:
		$Birds.stop()


func _on_screen_visibility_changed() -> void:
	if not _screen.is_visible_in_tree():
		stop_all()
	else:
		_refresh_ambience()


func stop_all() -> void:
	for child: Node in get_children():
		if child is AudioStreamPlayer:
			child.stop()
	_last_played.clear()
