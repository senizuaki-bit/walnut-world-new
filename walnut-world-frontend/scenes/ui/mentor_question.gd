class_name MentorQuestion
extends Control
## Click to start/stop the public Dingdang voice session. No canned answers.
signal answering_changed(answering: bool)
enum State { IDLE, CONNECTING, LISTENING, ANSWERING, COMPLETE }
@onready var ask_button: Button = %AskButton
@onready var ask_label: Label = %AskLabel
@onready var reply_panel: PanelContainer = %ReplyPanel
@onready var reply_text: Label = %ReplyText
@onready var reply_scroll: ScrollContainer = %ReplyScroll
@onready var understood_button: Button = %UnderstoodButton
@onready var status_label: Label = %QuestionStatus
@onready var interrupt_button: Button = %InterruptButton
@onready var follow_timer: Timer = $FollowTimer
@onready var voice: Node = $VoiceClient
var state: State = State.IDLE
var context_provider: Callable
var _context := ""
var _follow_text := true
var _answer := ""
var _transcript := ""

func _ready() -> void:
	ask_button.pressed.connect(toggle_voice)
	understood_button.pressed.connect(dismiss_reply)
	interrupt_button.pressed.connect(voice.interrupt)
	follow_timer.timeout.connect(_follow_reply_end)
	reply_scroll.gui_input.connect(_on_scroll_input)
	reply_scroll.get_v_scroll_bar().gui_input.connect(_on_scroll_input)
	reply_scroll.get_v_scroll_bar().changed.connect(_schedule_follow)
	voice.state_changed.connect(_on_voice_state)
	voice.text_received.connect(_on_text_received)
	voice.transcript_received.connect(_on_transcript_received)
	voice.response_started.connect(_on_response_started)
	voice.response_finished.connect(_on_response_finished)
	voice.response_cancelled.connect(_on_response_cancelled)
	voice.failed.connect(_on_voice_error)
	get_window().focus_exited.connect(reset)
	visibility_changed.connect(_on_visibility_changed)
	reset()

func configure_voice(base_url: String, token: String, session_id: String, provider: Callable) -> void:
	context_provider = provider
	voice.configure(base_url, token, session_id)

func configure_context(context: String) -> void:
	_context = context

func set_available(available: bool) -> void:
	visible = available

func set_editor_open(open: bool) -> void:
	var stack := $Stack as Control
	stack.anchor_left = 0.0 if open else 1.0
	stack.anchor_right = stack.anchor_left
	stack.offset_left = 18.0 if open else -216.0
	stack.offset_right = 216.0 if open else -18.0

func toggle_voice() -> void:
	if not is_visible_in_tree():
		return
	if voice.state != "IDLE":
		voice.close()
		return
	_answer = ""
	_transcript = ""
	reply_text.text = ""
	reply_panel.hide()
	_follow_text = true
	voice.start(_current_context())

func _process(_delta: float) -> void:
	if voice != null and voice.state != "IDLE":
		voice.update_context(_current_context())

func _current_context() -> Dictionary:
	if context_provider.is_valid():
		var value: Variant = context_provider.call()
		if value is Dictionary:
			return value
	return {"code": "", "observation": _context}

func dismiss_reply() -> void:
	reply_panel.hide()
	ask_button.grab_focus()

func reset() -> void:
	if not is_node_ready():
		return
	voice.close()
	follow_timer.stop()
	state = State.IDLE
	_answer = ""
	_transcript = ""
	reply_text.text = ""
	reply_panel.hide()
	understood_button.hide()
	interrupt_button.hide()
	ask_button.disabled = false
	ask_label.text = "问叮当"
	status_label.text = "点击开始语音对话"
	answering_changed.emit(false)

func _on_voice_state(next: String) -> void:
	match next:
		"CONNECTING":
			state = State.CONNECTING
			ask_label.text = "取消连接"
			status_label.text = "正在连接叮当……"
		"READY":
			state = State.LISTENING
			ask_label.text = "结束对话"
			status_label.text = "正在聆听 · 再次点击结束"
			interrupt_button.show()
		"IDLE":
			state = State.COMPLETE if not _answer.is_empty() else State.IDLE
			ask_label.text = "问叮当"
			status_label.text = "点击开始语音对话"
			interrupt_button.hide()
			understood_button.visible = not _answer.is_empty()
			answering_changed.emit(false)

func _on_response_started() -> void:
	_answer = ""
	state = State.ANSWERING
	reply_panel.show()
	understood_button.hide()
	status_label.text = "叮当正在回答 · 可以直接插话"
	answering_changed.emit(true)

func _on_text_received(text: String, complete: bool) -> void:
	_answer = text if complete else _answer + text
	_render_text()
	if complete:
		understood_button.show()
		state = State.LISTENING
		answering_changed.emit(false)

func _on_transcript_received(text: String, complete: bool) -> void:
	_transcript = text if complete else _transcript + text
	_render_text()

func _on_response_cancelled() -> void:
	state = State.LISTENING
	status_label.text = "正在聆听，你可以继续说"
	answering_changed.emit(false)

func _on_response_finished() -> void:
	state = State.LISTENING
	status_label.text = "正在聆听，你可以继续说"
	understood_button.visible = not _answer.is_empty()
	answering_changed.emit(false)

func _on_voice_error(message: String) -> void:
	status_label.text = message
	answering_changed.emit(false)

func _render_text() -> void:
	reply_text.text = (("你：" + _transcript + "\n\n") if not _transcript.is_empty() else "") + _answer
	reply_text.visible_characters = -1
	reply_panel.visible = not reply_text.text.is_empty()
	if _follow_text:
		_schedule_follow()

func _follow_reply_end() -> void:
	if _follow_text:
		reply_scroll.scroll_vertical = int(reply_scroll.get_v_scroll_bar().max_value)

func _schedule_follow() -> void:
	if _follow_text and follow_timer.is_stopped():
		follow_timer.start()

func _on_scroll_input(event: InputEvent) -> void:
	if (event is InputEventMouseButton and event.pressed) or event is InputEventScreenDrag or event is InputEventPanGesture:
		_follow_text = false

func _on_visibility_changed() -> void:
	if not is_visible_in_tree():
		reset()
