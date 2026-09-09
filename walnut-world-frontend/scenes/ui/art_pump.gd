extends ArtMotionTexture
## Visual transitions follow activity; their completion never advances gameplay.

var _running := false
var _fault := false


func set_activity(running: bool, fault := false) -> void:
	if _running == running and _fault == fault:
		return
	var was_running := _running
	_running = running
	_fault = fault
	if fault:
		play_clip("prop-pump-fault")
	elif bool(Engine.get_meta("art_reduced_motion", false)) or reduced_motion:
		play_clip("prop-pump-working" if running else "prop-pump-standby")
	elif running:
		play_clip("prop-pump-start", true)
	elif was_running:
		play_clip("prop-pump-stop", true)
	else:
		play_clip("prop-pump-standby")


func _process(delta: float) -> void:
	super._process(delta)
	if _fault or not is_visible_in_tree():
		return
	if _finished or reduced_motion or bool(Engine.get_meta("art_reduced_motion", false)):
		if motion_id in ["prop-pump-start", "prop-pump-stop"]:
			play_clip("prop-pump-working" if _running else "prop-pump-standby")
