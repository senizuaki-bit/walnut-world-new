extends SceneTree
const LEVEL := preload("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")
const Pcm := preload("res://scripts/client/voice_pcm.gd")
var failures: Array[String] = []

class DelayedCapture:
	extends RefCounted
	var error := ""
	var stopped := false
	var packets: Array[PackedByteArray] = []
	func start() -> bool: return true
	func read_packets() -> Array[PackedByteArray]:
		var result := packets.duplicate()
		packets.clear()
		return result
	func stop() -> void: stopped = true

class Socket:
	extends RefCounted
	var state := WebSocketPeer.STATE_CLOSED
	var url := ""
	var sent: Array[Dictionary] = []
	var incoming: Array[Dictionary] = []
	var audio: Array[PackedByteArray] = []
	func connect_to_url(value: String) -> int:
		url = value
		state = WebSocketPeer.STATE_OPEN
		return OK
	func get_ready_state() -> int: return state
	func poll() -> void: pass
	func get_available_packet_count() -> int: return incoming.size()
	func get_packet() -> PackedByteArray: return JSON.stringify(incoming.pop_front()).to_utf8_buffer()
	func was_string_packet() -> bool: return true
	func get_current_outbound_buffered_amount() -> int: return 0
	func send_text(value: String) -> int:
		var message: Dictionary = JSON.parse_string(value)
		sent.append(message)
		if message.type == "start": incoming.append({"type": "voice.ready", "input_rate": 16000, "output_rate": 24000})
		return OK
	func put_packet(value: PackedByteArray) -> int:
		audio.append(value)
		return OK
	func close() -> void: state = WebSocketPeer.STATE_CLOSED

func _initialize() -> void:
	Engine.set_meta("art_reduced_motion", true)
	root.size = Vector2i(1280, 720)
	root.gui_embed_subwindows = true
	var level := LEVEL.instantiate() as CropAdaptiveWateringDemo
	root.add_child(level)
	await process_frame
	level.story_dialogue.skip_sequence()
	level.call("_begin_workshop_experiments")
	var question := level.mentor_question
	check(not question.visible, "固定台词期间不显示提问入口")
	level.story_dialogue.skip_sequence()
	await process_frame
	check(question.visible and not level.hint_button.visible, "保留唯一提问入口")
	check(question.ask_button.get_global_rect().position.x >= (level.workshop_overlay.get_node("Card") as Control).get_global_rect().end.x, "提问按钮不遮挡实验题板")
	var socket := Socket.new()
	question.voice.capture_enabled = false
	question.voice.socket_factory = func(): return socket
	question.configure_voice("http://127.0.0.1:8790", "test-only-token", "session_voice_demo", func(): return {"code": "int main() {}", "observation": "正在编辑"})
	question.ask_button.pressed.emit()
	check(question.state == MentorQuestion.State.CONNECTING, "第一次点击连接语音")
	check(question.reply_panel.visible and question.reply_text.get_parsed_text().contains("正在连接"), "点击后立刻显示对话框，连接中不再空白等待")
	for _frame in range(3): await process_frame
	check(question.state == MentorQuestion.State.LISTENING, "等待真实 ready 事件后进入聆听")
	check(question.reply_panel.visible and question.reply_text.get_parsed_text().contains("麦克风"), "尚无转写也显示聆听说明")
	check(question.interrupt_button.disabled and question.interrupt_button.size.y >= 42, "尚未回答时打断不可用，按钮高度完整")
	check(socket.sent.size() == 1 and socket.sent[0].type == "start", "每个连接只发送一次 start")
	check(not socket.url.contains("test-only-token") and socket.url.ends_with("/dingdang-voice"), "token 只在首帧，不在 URL")
	check(socket.audio.is_empty(), "没有采集音频时不能伪造上传")
	socket.incoming.append({"type": "conversation.item.input_audio_transcription.completed", "text": "哪里错了？"})
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reply_1", "text": "看看"})
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reply_1", "text": "下标。"})
	socket.incoming.append({"type": "response.output_text.done", "response_id": "reply_1", "text": ""})
	await process_frame
	check(question.reply_text.get_parsed_text().contains("哪里错了？") and question.reply_text.get_parsed_text().ends_with("看看下标。"), "展示后端转写及增量文本；空 done 不清空回答")
	check(not question.interrupt_button.disabled and question.reply_text.get_parsed_text().contains("叮当师傅"), "回答中可打断，文字明确区分说话人")
	question.interrupt_button.pressed.emit()
	check(socket.sent.back().type == "interrupt", "主动打断发送公开协议")
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reply_1", "text": "过期内容"})
	await process_frame
	check(not question.reply_text.get_parsed_text().contains("过期内容"), "打断后丢弃在途旧回复")
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reply_2", "text": "新的解释。"})
	await process_frame
	check(question.reply_text.get_parsed_text().ends_with("新的解释。"), "同连接允许新一轮回答")
	socket.incoming.append({"type": "response.canceled", "response_id": "reply_1"})
	await process_frame
	check(question.state == MentorQuestion.State.ANSWERING, "旧回答迟到的取消事件不能打断新回答")
	var completions := {"count": 0}
	question.voice.response_finished.connect(func(): completions.count += 1)
	# The real Doubao service ends with audio.done without a response.done.
	socket.incoming.append({"type": "response.output_text.done", "response_id": "reply_2", "text": ""})
	socket.incoming.append({"type": "response.output_audio.done", "response_id": "reply_2"})
	await process_frame
	check(question.state == MentorQuestion.State.LISTENING and question.reply_text.get_parsed_text().ends_with("新的解释。") and completions.count == 1, "真实服务仅发送 audio.done 时也恢复聆听并保留增量回答")
	socket.incoming.append({"type": "response.done", "response_id": "reply_2"})
	await process_frame
	check(completions.count == 1, "后续 response.done 不重复发出回答完成信号")
	socket.incoming.append({"type": "response.output_text.done", "response_id": "reply_2", "text": ""})
	await process_frame
	check(question.reply_text.get_parsed_text().ends_with("新的解释。") and completions.count == 1 and not question.voice.can_interrupt(), "音频结束后的空文字结束不清空回答或重新开始播放")
	socket.incoming.append({"type": "response.output_text.done", "response_id": "reply_2", "text": "新的解释，完整句。"})
	await process_frame
	check(question.reply_text.get_parsed_text().ends_with("新的解释，完整句。"), "音频先结束仍接收同一回答的完整文字")
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reply_3", "text": "第三轮回答。"})
	socket.incoming.append({"type": "response.canceled", "response_id": ""})
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reply_3", "text": "迟到的旧字幕"})
	socket.incoming.append({"type": "response.output_audio.delta", "response_id": "reply_3", "audio": "AQABAA=="})
	await process_frame
	check(question.state == MentorQuestion.State.LISTENING and question.reply_text.get_parsed_text().ends_with("第三轮回答。"), "空 response_id 的取消事件仍应取消当前已知回答")
	check(question.voice.get("_audio").is_empty() and not question.voice.speaker.playing, "匿名取消后不能继续播放上一段已知回答的迟到音频")
	socket.incoming.append({"type": "response.output_audio.done", "response_id": ""})
	await process_frame
	check(completions.count == 1, "取消后迟到的匿名 audio.done 不作为新回答完成")
	question.ask_button.pressed.emit()
	check(socket.state == WebSocketPeer.STATE_CLOSED and socket.sent.back().type == "close", "第二次点击停止并关闭连接")
	check(not question.voice.microphone.playing and not question.voice.speaker.playing, "关闭清理录音和播放")
	check(question.reply_panel.visible and question.reply_text.get_parsed_text().contains("第三轮回答。"), "结束对话保留收到的字幕")
	question.get_window().focus_exited.emit()
	check(question.reply_panel.visible and question.reply_text.get_parsed_text().contains("第三轮回答。"), "结束后切换窗口仍保留可阅读文字")
	question.ask_button.pressed.emit()
	question.ask_button.pressed.emit()
	check(question.voice.state == "IDLE", "连接中可以取消")
	check(question.reply_panel.visible and question.reply_text.get_parsed_text().contains("本次未收到"), "提前结束且没有回复时保留明确说明")
	question.ask_button.pressed.emit()
	for _frame in range(2): await process_frame
	question.get_window().focus_exited.emit()
	check(question.voice.state == "IDLE", "失焦停止录音")
	question.ask_button.pressed.emit()
	for _frame in range(2): await process_frame
	level.story_dialogue.play_sequence("叮当", null, ["固定台词。"])
	check(not question.visible and question.voice.state == "IDLE", "隐藏入口时清理连接")
	level.story_dialogue.skip_sequence()
	level.workshop_overlay.hide()
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	await process_frame
	check(level.farm_mentor.visible and question.visible, "农田阶段仍在原位置显示入口")
	question.ask_button.pressed.emit()
	for _frame in range(2): await process_frame
	level.call("_show_code_drawer")
	check(question.visible and question.voice.state == "READY", "编辑代码时保持语音连接")
	check(question.ask_button.get_global_rect().end.x < level.code_drawer.get_node("Surface").get_global_rect().position.x, "编辑器打开后语音入口位于左侧，不遮挡编辑和运行")
	level.code_drawer.hide()
	await process_frame
	check(question.visible, "关闭卷轴恢复入口")
	socket.incoming.append({"type": "voice.error", "code": "VOICE_CONFIGURATION_INVALID"})
	await process_frame
	check(question.voice.state == "IDLE" and question.voice.get("_socket") == null, "服务配置错误后释放语音连接")
	check(not question.voice.microphone.playing and not question.voice.speaker.playing, "服务错误后停止录音和播放")
	check(question.ask_label.text == "问叮当" and not question.ask_button.disabled and not level.code_button.disabled, "语音错误不能锁住提问按钮或代码入口")
	check(question.reply_panel.visible and question.reply_text.get_parsed_text().contains("语音服务尚未准备好"), "服务失败显示在对话框中，不仅是按钮小字")
	# Reading older text must not jump back down when the next chunk arrives.
	question.ask_button.pressed.emit()
	for _frame in range(3): await process_frame
	var long_reply := "[b]原样保留的代码标记[/b]\n" + "先比较目标湿度，再检查当前湿度，最后决定需要补多少水。\n".repeat(30)
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reading", "text": long_reply})
	for _frame in range(5): await process_frame
	check(question.reply_text.get_parsed_text().contains("[b]原样保留的代码标记[/b]"), "模型文本按原文显示，不作为富文本指令执行")
	var wheel := InputEventMouseButton.new()
	wheel.button_index = MOUSE_BUTTON_WHEEL_UP
	wheel.pressed = true
	question.reply_scroll.gui_input.emit(wheel)
	question.reply_scroll.scroll_vertical = 32
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reading", "text": "新的补充说明。"})
	for _frame in range(4): await process_frame
	check(not question.latest_button.disabled and question.reply_scroll.scroll_vertical == 32, "手动阅读旧内容时新字幕不抢滚动位置")
	question.latest_button.pressed.emit()
	await create_timer(0.06).timeout
	check(question.latest_button.disabled and question.reply_scroll.scroll_vertical > 32, "点击最新恢复跟随")
	# Generation completion must not disable interruption while PCM is queued.
	var queued := PackedVector2Array()
	queued.resize(24000)
	question.voice.set("_audio", queued)
	socket.incoming.append({"type": "response.output_audio.done", "response_id": "reading"})
	await process_frame
	check(not question.interrupt_button.disabled, "文字生成结束但音频尚未播完时仍可打断")
	question.interrupt_button.pressed.emit()
	await process_frame
	check(question.interrupt_button.disabled and socket.state == WebSocketPeer.STATE_OPEN and question.reply_panel.visible, "打断立即清空播放并保留字幕和语音连接")
	check(question.interrupt_button.get_global_rect().position.y >= question.reply_scroll.get_global_rect().end.y, "打断按钮固定在文字滚动区之外")
	question.ask_button.pressed.emit()
	# Preserve resampling phase across uneven capture blocks (48k -> 16k).
	var closed_socket := Socket.new()
	question.voice.socket_factory = func(): return closed_socket
	question.ask_button.pressed.emit()
	closed_socket.incoming = [{"type": "voice.error", "code": "VOICE_DISABLED"}]
	closed_socket.state = WebSocketPeer.STATE_CLOSED
	await process_frame
	check(question.reply_text.get_parsed_text().contains("尚未开启语音"), "服务错误与关闭同帧到达时必须保留具体原因，不能覆盖成连接断开")
	await check_microphone_startup(question)
	var codec := Pcm.new()
	var samples := PackedVector2Array()
	samples.resize(9601)
	samples.fill(Vector2(0.5, 0.5))
	var packets: Array[PackedByteArray] = []
	packets.append_array(codec.encode(samples.slice(0, 777), 48000))
	packets.append_array(codec.encode(samples.slice(777), 48000))
	check(packets.size() == 10, "200ms 输入产生 10 帧 20ms 音频")
	for packet in packets:
		check(packet.size() == 640, "每帧必须为 640 字节 PCM16")
		check(absf(Pcm.decode(packet)[0].x - 0.5) < 0.001, "PCM16 编解码数值正确")
	codec.clear()
	check(codec.encode(PackedVector2Array(), 48000).is_empty(), "重连清理残留采样")
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("MENTOR_QUESTION_PASS: voice protocol, UI lifecycle, PCM framing")
		quit(0)
	else:
		for failure in failures: push_error(failure)
		quit(1)

func check(condition: bool, message: String) -> void:
	if not condition: failures.append(message)

func check_microphone_startup(question: MentorQuestion) -> void:
	question.voice.capture_enabled = true
	for scenario in ["ready", "cancel", "timeout"]:
		var socket := Socket.new()
		var capture := DelayedCapture.new()
		question.voice.socket_factory = func(): return socket
		question.ask_button.pressed.emit()
		# Replace only hardware I/O; exercise the production startup state machine.
		question.voice.set("_capture_pipe", capture)
		for _frame in range(3): await process_frame
		check(question.voice.state == "PREPARING" and question.state == MentorQuestion.State.CONNECTING, "服务器 ready 后须等待麦克风首包，不能提前聆听")
		check(socket.audio.is_empty() and question.reply_text.get_parsed_text().contains("正在准备麦克风") and question.interrupt_button.disabled, "无采集数据时显示准备状态，不伪造录音")
		if scenario == "ready":
			var first := PackedByteArray()
			first.resize(640) # Silence is valid input; readiness must not require loud speech.
			capture.packets.append(first)
			await process_frame
			check(question.voice.state == "READY" and socket.audio.size() == 1 and socket.audio[0] == first, "首个完整录音包必须上传并进入就绪，静音也能就绪")
			check(question.status_label.text.contains("可以开始说话"), "采集就绪后再明确提示开口")
			question.ask_button.pressed.emit()
		elif scenario == "cancel":
			question.ask_button.pressed.emit()
		else:
			question.voice.set("_capture_started_at", Time.get_ticks_msec() - 5001)
			await process_frame
			check(question.reply_text.get_parsed_text().contains("没有收到麦克风数据"), "采集无数据超时后提供可见错误")
		check(question.voice.state == "IDLE" and capture.stopped and question.voice.get("_capture_pipe") == null and socket.state == WebSocketPeer.STATE_CLOSED, "准备中取消、超时或正常结束都必须释放设备和连接")
