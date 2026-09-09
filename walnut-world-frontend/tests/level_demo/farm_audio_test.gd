extends SceneTree

var _failures: Array[String] = []
var _cues: Array[StringName] = []


func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	var level := preload("res://scenes/level_demo/crop_adaptive_watering_demo.tscn").instantiate() as CropAdaptiveWateringDemo
	root.add_child(level)
	await process_frame
	level.story_dialogue.skip_sequence()
	await process_frame
	var audio := level.sfx
	audio.cue_played.connect(func(cue: StringName) -> void: _cues.append(cue))
	for node: Node in audio.get_children():
		var player := node as AudioStreamPlayer
		_check(player != null and player.stream != null and player.stream.get_length() > 0.0, "每个预置播放器必须能解码音频")
		_check(AudioServer.get_bus_index(player.bus) >= 0, "音频总线必须存在")
	_check((audio.get_node("Birds").stream as AudioStreamOggVorbis).loop, "鸟鸣必须启用资源循环")
	_check(audio.get_node("Birds").playing, "农场可见且无阅读面板时应有环境声")
	level._set_phase(CropAdaptiveWateringDemo.Phase.CODE)
	level._show_code_drawer()
	_check(not audio.get_node("Birds").playing, "写代码时环境声应静音")
	_cues.clear()
	level.code_editor.text += "\n// 编辑草稿"
	_check(_cues.is_empty(), "输入代码不能触发音效")
	level._hide_code_drawer()
	_check(_cues.has(&"PanelClose"), "关闭代码面板应播放关闭提示")
	_check(audio.get_node("Birds").playing, "关闭代码面板应恢复环境声")
	_cues.clear()
	level.present_stage_audio({"ok": false, "stage": "BUILD"})
	_check(_cues.is_empty(), "失败的构建不能播放确认音")
	level.present_stage_audio({"ok": true, "stage": "BUILD"})
	level.present_stage_audio({"ok": true, "stage": "ACTIVATION"})
	_check(_cues == [&"Confirm", &"Activate"], "构建和激活应使用不同提示")
	level.configure_agent_mode(true)
	level.configure_candidate_compatibility_available(true)
	_cues.clear()
	await level.present_candidate_evaluation({
		"ok": true, "source": "SANDBOX_ACTION_INTENT_CANDIDATE",
		"actions": [], "plot_results": [], "objective_succeeded": true,
	})
	_check(not _cues.has(&"Complete"), "候选成功不得播放正式通关音")
	_check(not _cues.has(&"Watering"), "没有 WATER 动作不得播放水声")
	level.complete_agent_submission("正式闭环")
	level.complete_agent_submission("重复投影")
	_check(_cues.count(&"Complete") == 1, "正式完成应只播放一次，重复状态不重放")
	audio.start_watering()
	_check(audio.get_node("Watering").playing, "水声必须能启动")
	level._candidate_playing = true
	level._on_skip_playback_pressed()
	_check(not audio.get_node("Watering").playing, "跳过演出应立即停止水声")
	level._candidate_playing = false
	await create_timer(0.11).timeout
	audio.start_watering()
	level.hide()
	for player: Node in audio.get_children():
		_check(not player.playing, "离开农场必须停止所有声音")
	_cues.clear()
	audio.play_cue(&"Confirm")
	_check(_cues.is_empty(), "隐藏关卡不得继续播放提示")
	level.queue_free()
	await process_frame
	if _failures.is_empty():
		print("FARM_AUDIO_TEST_PASS")
		quit(0)
	else:
		for failure: String in _failures:
			push_error(failure)
		quit(1)


func _check(condition: bool, message: String) -> void:
	if not condition:
		_failures.append(message)
