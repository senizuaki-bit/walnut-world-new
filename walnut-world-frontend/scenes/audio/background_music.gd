extends AudioStreamPlayer
## One persistent player above the start screen and lesson pages.

@export_range(-40.0, 0.0, 1.0) var music_volume_db := -20.0
@export_range(0.1, 4.0, 0.1) var fade_seconds := 1.2

var _fade: Tween


func _ready() -> void:
	fade_in()


func fade_in() -> void:
	if _fade != null and _fade.is_valid():
		_fade.kill()
	if not playing:
		volume_db = -60.0
		play()
	_fade = create_tween()
	_fade.tween_property(self, "volume_db", music_volume_db, fade_seconds)


func fade_out() -> void:
	if _fade != null and _fade.is_valid():
		_fade.kill()
	if not playing:
		return
	_fade = create_tween()
	_fade.tween_property(self, "volume_db", -60.0, fade_seconds)
	await _fade.finished
	stop()
