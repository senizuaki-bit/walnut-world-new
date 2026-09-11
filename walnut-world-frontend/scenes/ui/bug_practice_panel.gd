extends Control
## Owns only the entry/challenge buffer. Never writes the main Draft or World.
signal entry_ready
signal challenge_entered
signal completed(summary: Dictionary)
signal restart_requested

const Gateway := preload("res://scripts/client/bug_practice_gateway.gd")
const BUG_PORTRAIT := preload("res://assets/art/redesign/crop_adaptive/v2/components/char-bug-idle.png")
const BOOK_PORTRAIT := preload("res://assets/art/redesign/crop_adaptive/v2/components/char-shushu-idle.png")
@onready var portrait: TextureRect = $Margin/Card/Padding/Content/Heading/Portrait
@onready var api: Node = $Gateway
@onready var title: Label = %PracticeTitle
@onready var details: RichTextLabel = %PracticeDetails
@onready var editor: CodeEdit = %PracticeEditor
@onready var feedback: RichTextLabel = %PracticeFeedback
@onready var action_button: Button = %PracticeAction
@onready var restart_button: Button = %PracticeRestart
@onready var replay_audio: Button = %PracticeReplayAudio
@onready var speaker: AudioStreamPlayer = $SummarySpeaker

var enabled := false
var state: Dictionary = {}
var busy := false
var _path := ""
var _entered := false
var _ready_for_main := false
var _next_action := "start"
var _summary: Dictionary = {}
var _generation := 0
var _invalid_saved_state := false

func _ready() -> void:
	hide()
	action_button.pressed.connect(_on_action)
	restart_button.pressed.connect(_restart)
	replay_audio.pressed.connect(func(): speaker.play())
	editor.text_changed.connect(func():
		state["source"] = editor.text
		_save()
	)
	visibility_changed.connect(func():
		if not is_visible_in_tree(): speaker.stop()
	)

func configure(url: String, token: String, session_id: String) -> void:
	enabled = true
	api.configure(url, token, session_id)
	_path = "user://bug-practice-" + (url.trim_suffix("/") + ":" + session_id).sha256_text() + ".json"
	state = {}
	_invalid_saved_state = false
	if FileAccess.file_exists(_path):
		var saved: Variant = JSON.parse_string(FileAccess.get_file_as_string(_path))
		if saved is Dictionary and _valid_entry_id(str(saved.get("entry_id", ""))):
			state = saved
		else:
			_invalid_saved_state = true
	_ready_for_main = false
	_entered = false

func enter_level() -> void:
	if not enabled or busy:
		return
	if _entered:
		state = {}
		_invalid_saved_state = false
	_entered = true
	_ready_for_main = false
	_summary.clear()
	speaker.stop()
	await ensure_entry()

func ensure_entry() -> bool:
	if not enabled:
		return true
	if _invalid_saved_state:
		_error({"code": "PRACTICE_LOCAL_STATE_INVALID"})
		return false
	if busy:
		return false
	if _ready_for_main:
		return str(state.get("phase", "")) == "WAITING_MAIN" and str(state.get("run_id", "")).is_empty()
	if state.is_empty():
		state = {"entry_id": Crypto.new().generate_random_bytes(16).hex_encode(), "started": false, "phase": "WAITING_MAIN", "source": "", "pending_answer": {}}
	var response := await _request("status" if state.get("started", false) else "start", {})
	if not response.get("ok", false):
		return false
	var value: Dictionary = response.value
	if value.get("entry_id") != state.entry_id or str(value.get("phase", "")) not in ["WAITING_MAIN", "CHALLENGE_PENDING", "CHALLENGE_READY", "SUMMARY_PENDING", "COMPLETED"]:
		_error({"code": "PRACTICE_RESPONSE_INVALID"})
		return false
	state["started"] = true
	state["phase"] = value.phase
	# Preserve a locally recorded successful Run if prepare lost its response.
	if value.get("run_id") != null:
		state["run_id"] = value.run_id
	if value.get("challenge") is Dictionary:
		if not _accept_challenge(value.challenge): return false
	_ready_for_main = true
	_save()
	if value.phase in ["SUMMARY_PENDING", "COMPLETED"]:
		state["pending_answer"] = {}
		await _load_summary()
	elif not state.get("pending_answer", {}).is_empty():
		_next_action = "answer"
		_show_challenge()
		feedback.text = "上次提交尚未确认。请重试原答案，确认后可以继续修改。"
	elif value.phase == "CHALLENGE_READY":
		_show_challenge()
	elif not str(state.get("run_id", "")).is_empty():
		await _prepare()
	else:
		hide()
		entry_ready.emit()
		return true
	return false

func complete_main(run: Dictionary) -> void:
	if not enabled or busy or run.get("status") != "SUCCEEDED" or str(run.get("run_id", "")).is_empty():
		return
	if not str(state.get("run_id", "")).is_empty():
		return
	state["run_id"] = run.run_id
	state["phase"] = "CHALLENGE_PENDING"
	await _prepare()

func _prepare() -> void:
	var response := await _request("prepare", {"run_id": state.get("run_id", "")})
	if not response.get("ok", false): return
	if not _accept_challenge(response.value): return
	state["phase"] = "CHALLENGE_READY"
	_show_challenge()
	if not state.get("seen", false):
		state["seen"] = true
		_save()
		challenge_entered.emit()

func _accept_challenge(value: Dictionary) -> bool:
	var skill: Variant = value.get("starter_skill")
	if str(value.get("challenge_id", "")).is_empty() or value.get("run_id") != state.get("run_id") or not skill is Dictionary:
		_error({"code": "PRACTICE_RESPONSE_INVALID"})
		return false
	var bundle: Variant = skill.get("source_bundle")
	if not bundle is Dictionary:
		_error({"code": "PRACTICE_RESPONSE_INVALID"})
		return false
	var files: Variant = bundle.get("files", [])
	if not files is Array or files.size() != 1 or not files[0] is Dictionary or typeof(files[0].get("content")) != TYPE_STRING or files[0].get("content_sha256") != str(files[0].content).sha256_text():
		_error({"code": "PRACTICE_RESPONSE_INVALID"})
		return false
	if state.get("challenge", {}).get("challenge_id") != value.challenge_id:
		state["source"] = files[0].content
	state["challenge"] = value.duplicate(true)
	_save()
	return true

func _show_challenge() -> void:
	show()
	portrait.texture = BUG_PORTRAIT
	var challenge: Dictionary = state.get("challenge", {})
	title.text = "Bug 军团 · " + str(challenge.get("title", "变式挑战"))
	details.text = "%s\n关注：%s\n当前湿度：%s\n目标湿度：%s\n规则：目标 − 当前 ≥ 30 浇 2 份；0 < 差值 < 30 浇 1 份；差值 ≤ 0 不输出。\n按下标递增输出 WATER i units，每行换行。请保留题目提供的输入读取代码。" % [challenge.get("brief", ""), challenge.get("focus", ""), _array_line(challenge.get("moisture", [])), _array_line(challenge.get("target", []))]
	editor.text = str(state.get("source", ""))
	editor.show()
	editor.editable = state.get("pending_answer", {}).is_empty()
	_next_action = "answer"
	action_button.text = "提交挑战" if editor.editable else "重试原答案"
	action_button.disabled = false
	restart_button.hide()
	replay_audio.hide()
	feedback.text = "主关已经通过。这道练习的代码单独保存，可以多次修改后提交。"

func _answer() -> void:
	if state.get("phase") != "CHALLENGE_READY": return
	if state.get("pending_answer", {}).is_empty():
		state["source"] = editor.text
		if editor.text.to_utf8_buffer().size() > 32000:
			_error({"code": "PRACTICE_REQUEST_TOO_LARGE"})
			return
		state["pending_answer"] = Gateway.answer_body(state.challenge.challenge_id, editor.text)
	var response := await _request("answer", state.pending_answer)
	if not response.get("ok", false): return
	var value: Dictionary = response.value
	if value.get("challenge_id") != state.challenge.challenge_id or typeof(value.get("correct")) != TYPE_BOOL or value.get("status") != ("SUCCEEDED" if value.correct else "REJECTED"):
		_error({"code": "PRACTICE_RESPONSE_INVALID"})
		return
	state["pending_answer"] = {}
	_save()
	if value.correct:
		state["phase"] = "SUMMARY_PENDING"
		await _load_summary()
	else:
		_show_challenge()
		feedback.text = "第 %d 次尝试 · %s\n%s" % [int(value.get("attempts", 0)), value.get("stage", ""), value.get("message", "请根据反馈修改后再试。")]
		for diagnostic: Dictionary in value.get("diagnostics", []):
			feedback.text += "\n" + str(diagnostic.get("message", ""))

func _load_summary() -> void:
	editor.hide()
	var response := await _request("summary", {"challenge_id": state.get("challenge", {}).get("challenge_id", "")})
	if not response.get("ok", false): return
	var value: Dictionary = response.value
	var audio := Gateway.summary_audio(value, str(state.challenge.challenge_id))
	if not audio.ok:
		_error(audio)
		return
	state["phase"] = "COMPLETED"
	if not _save(): return
	_summary = value
	portrait.texture = BOOK_PORTRAIT
	title.text = "书书 · 本局成长记录"
	details.text = str(value.message)
	feedback.text = "主关和 Bug 挑战均已通过。本次记录了你的练习过程。"
	editor.hide()
	action_button.text = "完成归档"
	action_button.disabled = false
	replay_audio.show()
	restart_button.hide()
	_next_action = "finish"
	# Full text and validated audio become available in the same frame.
	speaker.stop()
	speaker.stream = audio.stream
	if is_visible_in_tree(): speaker.play()

func _request(action: String, body: Dictionary) -> Dictionary:
	_next_action = action
	if not _save(): return {"ok": false}
	busy = true
	show()
	editor.editable = false
	action_button.disabled = true
	restart_button.hide()
	replay_audio.hide()
	feedback.text = {"start": "正在建立本局练习记录……", "status": "正在恢复本局进度……", "prepare": "主关已通过，Bug 军团正在准备一道新挑战……", "answer": "正在编译并验证你的代码，请稍候……", "summary": "挑战已通过，书书正在准备完整总结和语音……"}.get(action, "正在连接……")
	feedback.tooltip_text = ""
	action_button.text = "正在处理……"
	var generation := _generation
	var response: Dictionary = {}
	for attempt in range(3):
		response = await api.send(action, state.entry_id, body)
		if generation != _generation: return {"ok": false}
		if response.get("ok", false) or not response.get("retryable", false) or attempt == 2:
			break
		feedback.text = "服务暂时没有完成响应，正在自动重试（%d/2）……\n代码与已通过的结果已保留，无需重复点击。" % (attempt + 1)
		await get_tree().create_timer(0.5 * (attempt + 1)).timeout
		if generation != _generation: return {"ok": false}
	busy = false
	if not response.get("ok", false): _error(response)
	return response

func _error(response: Dictionary) -> void:
	show()
	var code := str(response.get("code", "PRACTICE_UNAVAILABLE"))
	feedback.text = "本步暂未完成，代码和已通过的结果已保留。请稍后重试；如果持续失败，请联系老师。"
	feedback.tooltip_text = code
	var speech_configuration := code in ["BOOK_SPEECH_CONFIGURATION_INVALID", "BOOK_SPEECH_DISABLED", "BOOK_SPEECH_AUTH_FAILED", "BOOK_SPEECH_RESOURCE_NOT_GRANTED"]
	var expired := code in ["PRACTICE_ENTRY_EXPIRED", "PRACTICE_LOCAL_STATE_INVALID"]
	if speech_configuration:
		feedback.text = "主关和挑战已通过，答案无需修改。书书配音尚未配置好，请联系老师修复配音配置；修复后再继续获取总结。"
	elif expired:
		feedback.text = "本局练习已过期或服务已重启。请重新开始本局并再次提交主关；你的主关代码会保留。"
		if code == "PRACTICE_LOCAL_STATE_INVALID":
			feedback.text = "无法读取本局练习记录。请重新开始本局；你的主关代码会保留。"
	elif code == "PRACTICE_REQUEST_TOO_LARGE" or code == "PRACTICE_SOURCE_INVALID":
		state["pending_answer"] = {}
		editor.editable = true
		feedback.text = "代码或请求超过限制，请精简后重新提交（源码最多 32000 UTF-8 字节）。"
	elif code == "PRACTICE_ALREADY_PASSED":
		state["pending_answer"] = {}
		_next_action = "status"
	elif code == "PRACTICE_LOCAL_SAVE_FAILED":
		feedback.text = "无法保存本局进度，请检查本机存储空间和目录权限后重试。"
	action_button.text = "配置修复后重试" if speech_configuration else "重试本步"
	action_button.disabled = expired
	restart_button.visible = expired

func _on_action() -> void:
	if busy: return
	match _next_action:
		"start", "status":
			_ready_for_main = false
			await ensure_entry()
		"prepare": await _prepare()
		"answer": await _answer()
		"summary": await _load_summary()
		"finish":
			hide()
			completed.emit(_summary)

func _restart() -> void:
	if busy: return
	_generation += 1
	state = {}
	_invalid_saved_state = false
	_ready_for_main = false
	hide()
	restart_requested.emit()

func _save() -> bool:
	if _path.is_empty(): return true # Isolated test scene.
	var file := FileAccess.open(_path + ".tmp", FileAccess.WRITE)
	if file == null:
		_error({"code": "PRACTICE_LOCAL_SAVE_FAILED"})
		return false
	file.store_string(JSON.stringify(state))
	file.flush()
	if file.get_error() != OK:
		_error({"code": "PRACTICE_LOCAL_SAVE_FAILED"})
		return false
	file.close()
	if DirAccess.rename_absolute(_path + ".tmp", _path) != OK:
		_error({"code": "PRACTICE_LOCAL_SAVE_FAILED"})
		return false
	return true

static func _valid_entry_id(value: String) -> bool:
	if value.length() != 32: return false
	for character in value:
		if not character in "0123456789abcdef": return false
	return true

static func _array_line(values: Array) -> String:
	var parts := PackedStringArray()
	for value in values: parts.append(str(int(value)))
	return "[" + ", ".join(parts) + "]"
