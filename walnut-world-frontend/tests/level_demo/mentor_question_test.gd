extends SceneTree
const LEVEL := preload("res://scenes/level_demo/crop_adaptive_watering_demo.tscn")
const Pcm := preload("res://scripts/client/voice_pcm.gd")
var failures: Array[String] = []

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
	for _frame in range(3): await process_frame
	check(question.state == MentorQuestion.State.LISTENING, "等待真实 ready 事件后进入聆听")
	check(socket.sent.size() == 1 and socket.sent[0].type == "start", "每个连接只发送一次 start")
	check(not socket.url.contains("test-only-token") and socket.url.ends_with("/dingdang-voice"), "token 只在首帧，不在 URL")
	check(socket.audio.is_empty(), "没有采集音频时不能伪造上传")
	socket.incoming.append({"type": "conversation.item.input_audio_transcription.completed", "text": "哪里错了？"})
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reply_1", "text": "看看"})
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reply_1", "text": "下标。"})
	socket.incoming.append({"type": "response.output_text.done", "response_id": "reply_1", "text": ""})
	await process_frame
	check(question.reply_text.text.contains("哪里错了？") and question.reply_text.text.ends_with("看看下标。"), "展示后端转写及增量文本；空 done 不清空回答")
	question.interrupt_button.pressed.emit()
	check(socket.sent.back().type == "interrupt", "主动打断发送公开协议")
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reply_1", "text": "过期内容"})
	await process_frame
	check(not question.reply_text.text.contains("过期内容"), "打断后丢弃在途旧回复")
	socket.incoming.append({"type": "response.output_text.delta", "response_id": "reply_2", "text": "新的解释。"})
	await process_frame
	check(question.reply_text.text.ends_with("新的解释。"), "同连接允许新一轮回答")
	socket.incoming.append({"type": "response.canceled", "response_id": "reply_1"})
	await process_frame
	check(question.state == MentorQuestion.State.ANSWERING, "旧回答迟到的取消事件不能打断新回答")
	socket.incoming.append({"type": "response.done", "response_id": "reply_2"})
	await process_frame
	check(question.state == MentorQuestion.State.LISTENING and question.reply_text.text.ends_with("新的解释。"), "完成事件恢复聆听并保留增量回答")
	question.ask_button.pressed.emit()
	check(socket.state == WebSocketPeer.STATE_CLOSED and socket.sent.back().type == "close", "第二次点击停止并关闭连接")
	check(not question.voice.microphone.playing and not question.voice.speaker.playing, "关闭清理录音和播放")
	question.ask_button.pressed.emit()
	question.ask_button.pressed.emit()
	check(question.voice.state == "IDLE", "连接中可以取消")
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
	# Preserve resampling phase across uneven capture blocks (48k -> 16k).
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
