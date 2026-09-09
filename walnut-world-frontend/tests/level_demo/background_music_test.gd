extends SceneTree

var failures: Array[String] = []


func _initialize() -> void:
	Engine.set_meta("art_reduced_motion", true)
	var previous_quit := auto_accept_quit
	var flow = load("res://scenes/app/game_flow.tscn").instantiate()
	root.add_child(flow)
	await process_frame
	var music = flow.get_node("BackgroundMusic")
	var stream := music.stream as AudioStreamMP3
	check(stream != null and stream.loop, "MP3 必须启用资源级循环")
	check(AudioServer.get_bus_index("Music") >= 0 and music.bus == &"Music", "背景音乐必须使用独立 Music 总线")
	check(music.playing and music.volume_db < music.music_volume_db, "启动应由低音量淡入")
	await create_timer(music.fade_seconds + 0.15).timeout
	check(is_equal_approx(music.volume_db, music.music_volume_db), "淡入必须到达设置的背景音量")
	var position_before: float = music.get_playback_position()
	await flow.call("_enter_farm")
	await flow.call("_replay_level")
	await flow.call("_return_home")
	check(flow.get_node("BackgroundMusic") == music and music.playing, "进入农田、重开和返回首页必须保留同一播放器")
	check(music.get_playback_position() > position_before, "切页不得重置音乐进度")
	check(flow.find_children("BackgroundMusic", "AudioStreamPlayer", true, false).size() == 1, "整个前端只应有一份背景音乐")
	check(not auto_accept_quit and root.close_requested.is_connected(flow._on_close_requested), "窗口关闭必须经过淡出处理")
	var duration := stream.get_length()
	check(duration > 1.0, "提供的音乐必须能解码且有有效时长")
	music.seek(duration - 0.15)
	await create_timer(0.5).timeout
	check(music.playing and music.get_playback_position() < 1.0, "播放末尾必须自动循环回开头")
	music.fade_out()
	await create_timer(music.fade_seconds * 0.5).timeout
	check(music.playing and music.volume_db < music.music_volume_db, "淡出过程中应逐渐降低音量")
	await create_timer(music.fade_seconds).timeout
	check(not music.playing, "淡出完成才停止音乐")
	flow.queue_free()
	await process_frame
	check(auto_accept_quit == previous_quit, "释放前端时应还原窗口退出设置")
	if failures.is_empty():
		print("BACKGROUND_MUSIC_PASS: loop, fade, page continuity; duration=%.2fs" % duration)
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)


func check(condition: bool, message: String) -> void:
	if not condition:
		failures.append(message)
