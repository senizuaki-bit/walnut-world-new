class_name MentorQuestion
extends Control
## Local interaction prototype only: no microphone, transcription or Agent request.
## Demo copy depends on the lesson, never on what the child actually says.

signal answering_changed(answering: bool)

enum State { IDLE, HOLDING, RECORDING, THINKING, ANSWERING, COMPLETE }

@onready var ask_button: Button = %AskButton
@onready var ask_label: Label = %AskLabel
@onready var reply_panel: PanelContainer = %ReplyPanel
@onready var reply_text: Label = %ReplyText
@onready var reply_scroll: ScrollContainer = %ReplyScroll
@onready var understood_button: Button = %UnderstoodButton
@onready var status_label: Label = %QuestionStatus
@onready var hold_timer: Timer = $HoldTimer
@onready var limit_timer: Timer = $LimitTimer
@onready var thinking_timer: Timer = $ThinkingTimer
@onready var typing_timer: Timer = $TypingTimer
@onready var follow_timer: Timer = $FollowTimer

var state: State = State.IDLE
var _context := ""
var _demo_reply := ""
var _state_before_hold: State = State.IDLE
var _touch_index := -1
var _follow_text := true
var _typed_characters := 0


func _ready() -> void:
	ask_button.button_down.connect(begin_hold)
	ask_button.button_up.connect(end_hold)
	ask_button.mouse_exited.connect(_on_mouse_exited)
	hold_timer.timeout.connect(_begin_recording)
	limit_timer.timeout.connect(end_hold)
	thinking_timer.timeout.connect(_begin_answer)
	typing_timer.timeout.connect(_type_next_character)
	understood_button.pressed.connect(dismiss_reply)
	follow_timer.timeout.connect(_follow_reply_end)
	reply_scroll.gui_input.connect(_on_scroll_input)
	reply_scroll.get_v_scroll_bar().gui_input.connect(_on_scroll_input)
	reply_scroll.get_v_scroll_bar().changed.connect(_schedule_follow)
	get_window().focus_exited.connect(_on_focus_exited)
	visibility_changed.connect(_on_visibility_changed)
	reset()


func configure_context(context: String, demo_reply: String) -> void:
	if context != _context:
		reset()
	_context = context
	_demo_reply = demo_reply


func set_available(available: bool) -> void:
	visible = available


func begin_hold() -> void:
	if not is_visible_in_tree() or state not in [State.IDLE, State.COMPLETE]:
		return
	_state_before_hold = state
	state = State.HOLDING
	status_label.text = "按住说话，移出取消"
	hold_timer.start()


func end_hold() -> void:
	if state == State.HOLDING:
		hold_timer.stop()
		state = _state_before_hold
		status_label.text = "请按住按钮说话"
	elif state == State.RECORDING:
		limit_timer.stop()
		state = State.THINKING
		reply_text.text = "让我想一想……"
		reply_text.visible_characters = -1
		reply_panel.show()
		ask_label.text = "师傅思考中…"
		ask_button.disabled = true
		status_label.text = "向叮当师傅请教"
		thinking_timer.start()


func cancel_hold() -> void:
	if state not in [State.HOLDING, State.RECORDING]:
		return
	var was_recording := state == State.RECORDING
	hold_timer.stop()
	limit_timer.stop()
	state = State.IDLE if was_recording else _state_before_hold
	ask_label.text = "长按提问"
	status_label.text = "已取消，可以重新提问"
	_touch_index = -1


func dismiss_reply() -> void:
	if state != State.COMPLETE:
		return
	reset()
	ask_button.grab_focus()


func reset() -> void:
	for timer: Timer in [hold_timer, limit_timer, thinking_timer, typing_timer, follow_timer]:
		timer.stop()
	state = State.IDLE
	_typed_characters = 0
	_touch_index = -1
	reply_panel.hide()
	_follow_text = true
	reply_text.text = ""
	reply_scroll.scroll_vertical = 0
	understood_button.hide()
	ask_button.disabled = false
	ask_label.text = "长按提问"
	status_label.text = "向叮当师傅请教"
	answering_changed.emit(false)


func _begin_recording() -> void:
	if state != State.HOLDING:
		return
	state = State.RECORDING
	reply_panel.hide()
	understood_button.hide()
	ask_label.text = "松开结束"
	status_label.text = "正在聆听 · 移出取消"
	limit_timer.start()


func _begin_answer() -> void:
	if state != State.THINKING:
		return
	state = State.ANSWERING
	_typed_characters = 0
	ask_label.text = "师傅回答中…"
	reply_text.text = ""
	reply_text.visible_characters = 0
	reply_scroll.scroll_vertical = 0
	_follow_text = true
	answering_changed.emit(true)
	typing_timer.start()


func _type_next_character() -> void:
	if state != State.ANSWERING:
		return
	# Stop auto-follow when the child has scrolled up to re-read earlier words.
	var bar := reply_scroll.get_v_scroll_bar()
	if not _follow_text and bar.value + bar.page >= bar.max_value - 1.0:
		_follow_text = true
	_typed_characters += 1
	reply_text.text = _demo_reply.substr(0, _typed_characters)
	reply_text.visible_characters = -1
	if _follow_text:
		_schedule_follow()
	if _typed_characters >= _demo_reply.length():
		typing_timer.stop()
		state = State.COMPLETE
		understood_button.show()
		ask_button.disabled = false
		ask_label.text = "长按提问"
		answering_changed.emit(false)


func _follow_reply_end() -> void:
	if _follow_text and state in [State.ANSWERING, State.COMPLETE]:
		reply_scroll.scroll_vertical = int(reply_scroll.get_v_scroll_bar().max_value)


func _schedule_follow() -> void:
	# Re-run after the scroll range includes wrapped lines and the confirm button.
	if _follow_text and state in [State.ANSWERING, State.COMPLETE] and follow_timer.is_stopped():
		follow_timer.start()


func _on_scroll_input(event: InputEvent) -> void:
	if (event is InputEventMouseButton and event.pressed) or event is InputEventScreenDrag or event is InputEventPanGesture or (event is InputEventKey and event.pressed):
		_follow_text = false


func _input(event: InputEvent) -> void:
	if not is_visible_in_tree():
		return
	if event is InputEventScreenTouch and event.pressed and _touch_index == -1 and ask_button.get_global_rect().has_point(event.position):
		_touch_index = event.index
		begin_hold()
		get_viewport().set_input_as_handled()
	elif event is InputEventScreenTouch and event.index == _touch_index and not event.pressed:
		if event.canceled or not ask_button.get_global_rect().has_point(event.position):
			cancel_hold()
		else:
			end_hold()
		_touch_index = -1
		get_viewport().set_input_as_handled()
	elif event is InputEventScreenDrag and event.index == _touch_index:
		if not ask_button.get_global_rect().has_point(event.position):
			cancel_hold()
	elif event.is_action_pressed("ui_cancel") and state in [State.HOLDING, State.RECORDING]:
		cancel_hold()
		get_viewport().set_input_as_handled()


func _on_mouse_exited() -> void:
	if _touch_index == -1:
		cancel_hold()


func _on_focus_exited() -> void:
	cancel_hold()


func _on_visibility_changed() -> void:
	if not is_visible_in_tree():
		reset()
