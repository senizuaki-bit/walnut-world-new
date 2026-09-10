extends RefCounted
## Optional local Windows microphone adapter for multichannel WASAPI devices.
## No files receive audio. FFmpeg converts DirectShow input to 16k mono PCM.
const CONFIG_PATH := "user://voice-input.cfg"
var error := ""
var _process: Dictionary = {}
var _pending := PackedByteArray()
var _started_at := 0
var _received := false

func configured() -> bool:
	var config := ConfigFile.new()
	return OS.get_name() == "Windows" and config.load(CONFIG_PATH) == OK and config.get_value("capture", "mode", "native") == "ffmpeg"

func start() -> bool:
	stop()
	error = ""
	var config := ConfigFile.new()
	if config.load(CONFIG_PATH) != OK:
		error = "麦克风配置无法读取，请检查录音设置。"
		return false
	var executable := str(config.get_value("capture", "executable", ""))
	var device := str(config.get_value("capture", "device", ""))
	if not executable.is_absolute_path() or not FileAccess.file_exists(executable) or device.is_empty():
		error = "麦克风兼容组件不可用，请检查录音设置。"
		return false
	# The capture helper has no reason to inherit game or model credentials.
	var previous: Dictionary = {}
	for key in ["YAYA_AUTH_TOKEN", "YAYA_DOUBAO_VOICE_API_KEY", "YAYA_DOUBAO_VOICE_API_KEY_FILE", "WALNUT_LLM_UPSTREAM_API_KEY", "WALNUT_LLM_UPSTREAM_API_KEY_FILE"]:
		if OS.has_environment(key):
			previous[key] = OS.get_environment(key)
			OS.unset_environment(key)
	_process = OS.execute_with_pipe(executable, PackedStringArray([
		"-hide_banner", "-loglevel", "error", "-nostdin", "-f", "dshow",
		"-audio_buffer_size", "20", "-i", "audio=" + device,
		"-ac", "1", "-ar", "16000", "-f", "s16le", "-flush_packets", "1", "pipe:1"
	]), false)
	for key in previous:
		OS.set_environment(key, previous[key])
	_started_at = Time.get_ticks_msec()
	_received = false
	if _process.is_empty():
		error = "麦克风启动失败，请检查设备是否已连接。"
	return not _process.is_empty()

func read_packets() -> Array[PackedByteArray]:
	var packets: Array[PackedByteArray] = []
	if _process.is_empty():
		return packets
	# Bounded, nonblocking reads keep a stalled recorder off the UI thread.
	var chunk: PackedByteArray = _process.stdio.get_buffer(8192)
	_process.stderr.get_buffer(4096) # Drain diagnostics without retaining audio/device details.
	if not chunk.is_empty():
		_received = true
		_pending.append_array(chunk)
	if not OS.is_process_running(int(_process.pid)):
		error = "麦克风采集已停止，请检查设备权限后重新连接。"
	elif not _received and Time.get_ticks_msec() - _started_at > 5000:
		error = "没有收到麦克风数据，请检查录音权限和设备。"
	elif _pending.size() > 32000:
		error = "录音数据积压，请重新开始语音对话。"
	if not error.is_empty():
		stop()
		return packets
	while _pending.size() >= 640:
		packets.append(_pending.slice(0, 640))
		_pending = _pending.slice(640)
	return packets

func stop() -> void:
	if not _process.is_empty():
		_process.stdio.close()
		_process.stderr.close()
		if OS.is_process_running(int(_process.pid)):
			OS.kill(int(_process.pid))
	_process.clear()
	_pending.clear()
