class_name MentorQuestion
extends Control
## Click to start/stop the public Dingdang voice session. No canned answers.
signal answering_changed(answering: bool)
enum State { IDLE, CONNECTING, LISTENING, ANSWERING, COMPLETE }
@onready var ask_button: Button = %AskButton
@onready var ask_label: Label = %AskLabel
@onready var reply_panel: PanelContainer = %ReplyPanel
@onready var reply_text: RichTextLabel = %ReplyText
@onready var reply_scroll: ScrollContainer = %ReplyScroll
@onready var understood_button: Button = %UnderstoodButton
@onready var status_label: Label = %QuestionStatus
@onready var interrupt_button: Button = %InterruptButton
@onready var latest_button: Button = %LatestButton
@onready var follow_timer: Timer = $FollowTimer
@onready var voice: Node = $VoiceClient
var state: State = State.IDLE
var context_provider: Callable
var _context := ""
var _follow_text := true
var _answer := ""
var _transcript := ""
var _notice_visible := false

func _ready() -> void:
	ask_button.pressed.connect(toggle_voice)
	understood_button.pressed.connect(dismiss_reply)
	interrupt_button.pressed.connect(voice.interrupt)
	latest_button.pressed.connect(_resume_follow)
	follow_timer.timeout.connect(_follow_reply_end)
	reply_scroll.gui_input.connect(_on_scroll_input)
	reply_text.gui_input.connect(_on_scroll_input)
	reply_scroll.get_v_scroll_bar().gui_input.connect(_on_scroll_input)
	reply_scroll.get_v_scroll_bar().changed.connect(_schedule_follow)
	voice.state_changed.connect(_on_voice_state)
	voice.text_received.connect(_on_text_received)
	voice.transcript_received.connect(_on_transcript_received)
	voice.response_started.connect(_on_response_started)
	voice.response_finished.connect(_on_response_finished)
	voice.response_cancelled.connect(_on_response_cancelled)
	voice.failed.connect(_on_voice_error)
	get_window().focus_exited.connect(_on_focus_exited)
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
	_refresh_actions()
	if voice.state == "READY":
		var answering: bool = voice.can_interrupt()
		var next := State.ANSWERING if answering else State.LISTENING
		if state != next:
			state = next
			status_label.text = "叮当正在回答 · 可以打断" if answering else "正在聆听，你可以继续说"
			answering_changed.emit(answering)

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

func _on_focus_exited() -> void:
	# Stop recording/playback when leaving the game, but keep readable answers.
	voice.close()
	follow_timer.stop()
	answering_changed.emit(false)

func _on_voice_state(next: String) -> void:
	match next:
		"CONNECTING":
			state = State.CONNECTING
			ask_label.text = "取消连接"
			status_label.text = "正在连接叮当……"
			_show_notice("正在连接叮当师傅，请稍候……")
		"PREPARING":
			state = State.CONNECTING
			ask_label.text = "取消连接"
			status_label.text = "正在准备麦克风……"
			_show_notice("正在准备麦克风，请等显示「可以开始说话」后再开口。")
		"READY":
			state = State.LISTENING
			ask_label.text = "结束对话"
			status_label.text = "可以开始说话 · 说完稍等自动回答"
			_show_notice("麦克风已就绪，可以开始说话。说完稍等，识别文字会显示在这里。")
			interrupt_button.show()
		"IDLE":
			state = State.COMPLETE if not _answer.is_empty() else State.IDLE
			ask_label.text = "问叮当"
			status_label.text = "点击开始语音对话"
			interrupt_button.disabled = true
			if _answer.is_empty() and _transcript.is_empty() and reply_panel.visible:
				_show_notice("对话已结束，本次未收到识别文字或回答。\n\n请检查麦克风；说完话后稍等，叮当会自动回答。")
			answering_changed.emit(false)

func _on_response_started() -> void:
	_answer = ""
	state = State.ANSWERING
	reply_panel.show()
	understood_button.show()
	status_label.text = "叮当正在回答 · 可以直接插话"
	answering_changed.emit(true)
	_refresh_actions()

func _on_text_received(text: String, complete: bool) -> void:
	_answer = text if complete else _answer + text
	_render_text()
	if complete:
		understood_button.show()

func _on_transcript_received(text: String, complete: bool) -> void:
	_transcript = text if complete else _transcript + text
	_render_text()

func _on_response_cancelled() -> void:
	state = State.LISTENING
	status_label.text = "正在聆听，你可以继续说"
	answering_changed.emit(false)
	_refresh_actions()

func _on_response_finished() -> void:
	var playing: bool = voice.can_interrupt()
	state = State.ANSWERING if playing else State.LISTENING
	status_label.text = "正在播放回答 · 可以打断" if playing else "正在聆听，你可以继续说"
	_refresh_actions()
	answering_changed.emit(playing)

func _on_voice_error(message: String) -> void:
	status_label.text = message
	_show_notice(message + "\n\n你可以继续操作关卡，稍后再试。")
	answering_changed.emit(false)

func _show_notice(message: String) -> void:
	_notice_visible = true
	follow_timer.stop()
	reply_text.text = message
	reply_text.visible_characters = -1
	reply_panel.show()
	reply_scroll.set_deferred("scroll_vertical", 0)
	_refresh_actions()

func _render_text() -> void:
	if not _follow_text and not reply_text.get_selected_text().is_empty():
		return # Keep a manual text selection stable while new chunks arrive.
	var previous_scroll := reply_scroll.scroll_vertical
	if _notice_visible:
		_notice_visible = false
		_follow_text = true
	# Format the two speakers without interpreting model/user content as markup.
	reply_text.clear()
	if not _transcript.is_empty():
		reply_text.push_font_size(13)
		reply_text.push_color(Color(0.40, 0.43, 0.33))
		reply_text.add_text("你说\n" + _transcript + "\n\n")
		reply_text.pop()
		reply_text.pop()
	if not _answer.is_empty():
		reply_text.push_font_size(13)
		reply_text.push_color(Color(0.20, 0.39, 0.28))
		reply_text.add_text("叮当师傅\n")
		reply_text.pop()
		reply_text.pop()
		reply_text.add_text(_answer)
	reply_text.visible_characters = -1
	reply_panel.visible = not reply_text.get_parsed_text().is_empty()
	if _follow_text:
		_schedule_follow()
	else:
		reply_scroll.set_deferred("scroll_vertical", previous_scroll)
	_refresh_actions()

func _refresh_actions() -> void:
	interrupt_button.show()
	interrupt_button.disabled = not voice.can_interrupt()
	understood_button.show()
	latest_button.disabled = _follow_text or _notice_visible

func _resume_follow() -> void:
	_follow_text = true
	reply_text.deselect()
	if not _notice_visible:
		_render_text()
	_schedule_follow()
	_refresh_actions()

func _follow_reply_end() -> void:
	if _follow_text:
		reply_scroll.scroll_vertical = int(reply_scroll.get_v_scroll_bar().max_value)

func _schedule_follow() -> void:
	if _follow_text and not _notice_visible and follow_timer.is_stopped():
		follow_timer.start()

func _on_scroll_input(event: InputEvent) -> void:
	if (event is InputEventMouseButton and event.pressed) or event is InputEventScreenDrag or event is InputEventPanGesture:
		_follow_text = false
		follow_timer.stop()
		_refresh_actions()

func _on_visibility_changed() -> void:
	if not is_visible_in_tree():
		reset()
