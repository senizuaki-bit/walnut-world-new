extends Node
## Public main Gateway protocol. Tokens stay in memory; voice never submits Turns.
signal state_changed(state: String)
signal text_received(text: String, complete: bool)
signal transcript_received(text: String, complete: bool)
signal response_started
signal response_finished
signal response_cancelled
signal failed(message: String)

const Pcm = preload("res://scripts/client/voice_pcm.gd")
const CapturePipe = preload("res://scripts/client/voice_capture_pipe.gd")
const MAX_AUDIO_FRAMES := 24000 * 20
const READY_TIMEOUT_MS := 20000
const CAPTURE_TIMEOUT_MS := 5000
@onready var microphone: AudioStreamPlayer = $Microphone
@onready var speaker: AudioStreamPlayer = $Speaker
var capture_enabled := true # Injectable hardware seam; production always captures.
var socket_factory: Callable
var state := "IDLE"
var _base_url := ""
var _token := ""
var _session_id := ""
var _context: Dictionary = {}
var _socket: RefCounted
var _capture: AudioEffectCapture
var _codec := Pcm.new()
var _playback: AudioStreamGeneratorPlayback
var _playback_capacity := 0
var _audio := PackedVector2Array()
var _started_at := 0
var _capture_started_at := 0
var _context_due := 0
var _start_sent := false
var _response_id := ""
var _cancelled_ids: Dictionary = {}
var _finished_ids: Dictionary = {}
var _drop_anonymous := false
var _new_response := true
var _capture_pipe: RefCounted

func _ready() -> void:
	set_process(false)
	var bus := AudioServer.get_bus_index("VoiceCapture")
	if bus >= 0 and AudioServer.get_bus_effect_count(bus) > 0:
		_capture = AudioServer.get_bus_effect(bus, 0) as AudioEffectCapture

func configure(base_url: String, token: String, session_id: String) -> void:
	if _base_url != base_url or _token != token or _session_id != session_id:
		close()
	_base_url = base_url.trim_suffix("/")
	_token = token
	_session_id = session_id

func start(context: Dictionary) -> void:
	if state != "IDLE":
		return
	if _token.is_empty() or _session_id.is_empty() or not (_base_url.begins_with("https://") or _base_url in ["http://127.0.0.1:8790", "http://localhost:8790"]):
		_fail("请先连接游戏服务并进入当前关卡。")
		return
	var pipe := CapturePipe.new()
	_capture_pipe = pipe if capture_enabled and pipe.configured() else null
	if capture_enabled and _capture_pipe == null and (_capture == null or not ProjectSettings.get_setting("audio/driver/enable_input", false)):
		_fail("麦克风尚未启用，请检查录音设置。")
		return
	_context = _bounded_context(context)
	_socket = socket_factory.call() if socket_factory.is_valid() else WebSocketPeer.new()
	if _socket is WebSocketPeer:
		_socket.inbound_buffer_size = 4 * 1024 * 1024
		_socket.outbound_buffer_size = 128 * 1024
		_socket.max_queued_packets = 256
	var url := _base_url.replace("https://", "wss://").replace("http://", "ws://") + "/product-experience/v1/sessions/" + _session_id.uri_encode() + "/dingdang-voice"
	_started_at = Time.get_ticks_msec()
	_start_sent = false
	_cancelled_ids.clear()
	_finished_ids.clear()
	_drop_anonymous = false
	_new_response = true
	_response_id = ""
	_codec.clear()
	_set_state("CONNECTING")
	if _socket.connect_to_url(url) != OK:
		_fail("暂时无法连接叮当，请稍后重试。")
		return
	set_process(true)

func update_context(context: Dictionary) -> void:
	var next := _bounded_context(context)
	if next != _context:
		_context = next
		_context_due = Time.get_ticks_msec() + 400

func interrupt() -> void:
	if state != "READY":
		return
	_cancel_response(_response_id)
	_send({"type": "interrupt"})

func can_interrupt() -> bool:
	# Audio generation can finish before the queued speech has been heard.
	return state == "READY" and (not _new_response or not _audio.is_empty() or (_playback != null and _playback.get_frames_available() < _playback_capacity))

func close() -> void:
	if _socket != null:
		if _socket.get_ready_state() == WebSocketPeer.STATE_OPEN:
			_socket.send_text(JSON.stringify({"type": "close"}))
		_socket.close()
	_socket = null
	if is_instance_valid(microphone):
		microphone.stop()
	if _capture_pipe != null:
		_capture_pipe.stop()
		_capture_pipe = null
	_clear_audio()
	if _capture != null:
		_capture.clear_buffer()
	_codec.clear()
	_context_due = 0
	_start_sent = false
	set_process(false)
	_set_state("IDLE")

func _exit_tree() -> void:
	close()
	_token = ""

func _process(_delta: float) -> void:
	if _socket == null:
		return
	_socket.poll()
	if _socket.get_ready_state() == WebSocketPeer.STATE_CLOSED:
		_fail("语音连接已断开，点击可重新连接。")
		return
	if state == "CONNECTING" and Time.get_ticks_msec() - _started_at > READY_TIMEOUT_MS:
		_fail("叮当连接超时，请稍后重试。")
		return
	if _socket.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return
	if not _start_sent:
		_start_sent = true
		_send({"type": "start", "token": _token, "context": _context})
	while _socket != null and _socket.get_available_packet_count() > 0:
		var packet: PackedByteArray = _socket.get_packet()
		if not _socket.was_string_packet():
			_fail("叮当返回了无法识别的语音数据。")
			return
		var message: Variant = JSON.parse_string(packet.get_string_from_utf8())
		if not message is Dictionary:
			_fail("叮当返回了无法识别的消息。")
			return
		_receive(message)
	if state not in ["PREPARING", "READY"] or _socket == null:
		return
	if state == "PREPARING" and Time.get_ticks_msec() - _capture_started_at > CAPTURE_TIMEOUT_MS:
		_fail("没有收到麦克风数据，请检查录音权限和设备后重试。")
		return
	if _context_due > 0 and Time.get_ticks_msec() >= _context_due:
		_context_due = 0
		_send({"type": "context", "context": _context})
	if _socket == null:
		return
	if capture_enabled:
		var packets: Array[PackedByteArray] = []
		if _capture_pipe != null:
			packets = _capture_pipe.read_packets()
			if not _capture_pipe.error.is_empty():
				_fail(_capture_pipe.error)
				return
		else:
			var available := _capture.get_frames_available()
			if available > 0:
				packets = _codec.encode(_capture.get_buffer(available), AudioServer.get_mix_rate())
		for packet in packets:
			if _socket == null:
				return
			if _socket.get_current_outbound_buffered_amount() > 64000 or _socket.put_packet(packet) != OK:
				_fail("语音网络跟不上录音速度，请重新连接。")
				return
		# Server readiness does not mean the physical microphone is ready.
		# Send the first complete frame before inviting the player to speak.
		if state == "PREPARING" and not packets.is_empty():
			_set_state("READY")
	_fill_speaker()

func _receive(message: Dictionary) -> void:
	var kind := str(message.get("type", ""))
	if kind == "voice.ready":
		if state != "CONNECTING" or int(message.get("input_rate", 0)) != 16000 or int(message.get("output_rate", 0)) != 24000:
			_fail("语音格式与当前客户端不兼容。")
			return
		if capture_enabled:
			if _capture_pipe != null:
				if not _capture_pipe.start():
					_fail(_capture_pipe.error)
					return
			else:
				_capture.clear_buffer()
				microphone.play()
			_capture_started_at = Time.get_ticks_msec()
			_set_state("PREPARING")
		else:
			_set_state("READY")
	elif kind == "voice.error":
		var code := str(message.get("code", ""))
		_fail({"VOICE_DISABLED": "当前服务尚未开启语音。", "VOICE_CONFIGURATION_INVALID": "语音服务尚未准备好，你可以继续操作关卡。", "VOICE_AUTH_FAILED": "语音鉴权失败，请重新连接；持续失败请联系老师。", "VOICE_SESSION_UNAVAILABLE": "当前关卡会话不可用，请重新进入。"}.get(code, "语音服务暂时不可用，请稍后重试。"))
	elif kind == "session.closed":
		close()
	elif kind == "response.canceled":
		var cancelled_id := str(message.get("response_id", ""))
		_cancel_response(_response_id if cancelled_id.is_empty() else cancelled_id)
	elif kind.begins_with("conversation.item.input_audio_transcription."):
		if kind.ends_with("started"):
			transcript_received.emit("", true)
		elif kind.ends_with("delta") or kind.ends_with("completed"):
			var text := str(message.get("text", ""))
			if not text.is_empty():
				transcript_received.emit(text, kind.ends_with("completed"))
	elif kind in ["response.done", "response.output_audio.done"]:
		var completed_id := str(message.get("response_id", ""))
		# Doubao can finish its output with audio.done alone. This marks
		# generation complete; already queued PCM still drains normally.
		if _new_response or (completed_id.is_empty() and _drop_anonymous) or _cancelled_ids.has(completed_id) or (not completed_id.is_empty() and completed_id != _response_id):
			return
		_new_response = true
		if not _response_id.is_empty():
			_finished_ids[_response_id] = true
			if _finished_ids.size() > 128:
				_finished_ids.erase(_finished_ids.keys()[0])
		response_finished.emit()
	elif kind.begins_with("response.output_"):
		var response_id := str(message.get("response_id", ""))
		# Text and audio finish independently. A trailing text completion belongs
		# to the same answer; it must not clear it or reopen finished playback.
		if _finished_ids.has(response_id):
			if response_id == _response_id and not _cancelled_ids.has(response_id) and kind in ["response.output_text.delta", "response.output_text.done"]:
				var trailing_text := str(message.get("text", ""))
				if not trailing_text.is_empty():
					text_received.emit(trailing_text, kind.ends_with("done"))
			return
		if _new_response and kind.ends_with("done") and str(message.get("text", "")).is_empty():
			return
		if _cancelled_ids.has(response_id) or (response_id.is_empty() and _drop_anonymous):
			# Anonymous canceled chunks cannot be distinguished safely. A new
			# explicit started event opens the next response; late deltas do not.
			if response_id.is_empty() and kind.ends_with("started"):
				_drop_anonymous = false
			else:
				return
		if _new_response or (not response_id.is_empty() and response_id != _response_id):
			_response_id = response_id
			if not response_id.is_empty():
				_drop_anonymous = false
			_new_response = false
			response_started.emit()
		if kind in ["response.output_text.delta", "response.output_text.done"]:
			var text := str(message.get("text", ""))
			if not text.is_empty():
				text_received.emit(text, kind.ends_with("done"))
		elif kind == "response.output_audio.delta":
			var bytes := Marshalls.base64_to_raw(str(message.get("audio", "")))
			if bytes.is_empty() or bytes.size() % 2 != 0:
				_fail("叮当的音频数据不完整，请重新连接。")
				return
			_audio.append_array(Pcm.decode(bytes))
			if _audio.size() > MAX_AUDIO_FRAMES:
				_fail("语音播放积压，请重新连接。")

func _send(message: Dictionary) -> void:
	if _socket != null and _socket.send_text(JSON.stringify(message)) != OK:
		_fail("语音消息发送失败，请重新连接。")

func _cancel_response(response_id: String) -> void:
	if not response_id.is_empty():
		_cancelled_ids[response_id] = true
		if _cancelled_ids.size() > 128:
			_cancelled_ids.erase(_cancelled_ids.keys()[0])
		if not _response_id.is_empty() and response_id != _response_id:
			return # A late cancellation must not stop a newer answer.
	_drop_anonymous = true
	_clear_audio()
	_new_response = true
	response_cancelled.emit()

func _clear_audio() -> void:
	_audio.clear()
	if is_instance_valid(speaker):
		speaker.stop()
	_playback = null
	_playback_capacity = 0

func _fill_speaker() -> void:
	if _audio.is_empty():
		return
	if _playback == null:
		speaker.play()
		_playback = speaker.get_stream_playback() as AudioStreamGeneratorPlayback
		if _playback != null:
			_playback_capacity = _playback.get_frames_available()
	if _playback == null:
		return
	var count := mini(_audio.size(), _playback.get_frames_available())
	if count > 0 and _playback.push_buffer(_audio.slice(0, count)):
		_audio = _audio.slice(count)

func _bounded_context(context: Dictionary) -> Dictionary:
	return {"code": str(context.get("code", "")).left(1800), "observation": str(context.get("observation", "")).left(400)}

func _set_state(next: String) -> void:
	state = next
	state_changed.emit(state)

func _fail(message: String) -> void:
	close()
	failed.emit(message)
