extends SceneTree

const LEVEL_PATH := "res://scenes/level_demo/crop_adaptive_watering_demo.tscn"
const BridgeScript := preload("res://scenes/app/crop_agent_bridge.gd")

class FakeBookSpeech:
	extends Node
	signal release
	var calls := 0
	var reject := true
	func prepare(_interaction: Dictionary) -> Dictionary:
		calls += 1
		await release
		if reject:
			return {"ok": false, "code": "BOOK_SPEECH_RESOURCE_NOT_GRANTED"}
		var stream := AudioStreamWAV.new()
		stream.format = AudioStreamWAV.FORMAT_16_BITS
		stream.mix_rate = 24000
		var pcm := PackedByteArray()
		pcm.resize(48000)
		stream.data = pcm
		return {"ok": true, "stream": stream}


class FakeStore:
	extends Node
	signal draft_changed(source: String, state: int)
	signal flow_changed(state: int)
	signal error_reported(error: Dictionary)

	var local_source := ""
	var draft_state := 0
	var content: Dictionary = {
		"unit_id": "TASK_CROP_ADAPTIVE_WATERING",
		"task": {"name": "Water every thirsty plot"},
	}
	var world_snapshot: Dictionary = {
		"world_id": "world_demo", "revision": 1, "state_hash": "state_hash_1",
	}
	var flow_state := 1
	var active_skill_tuple: Dictionary = {}
	var objective_result: Dictionary = {}
	var last_interaction_sequence := 1

	func mark_draft_dirty(source: String) -> void:
		local_source = source
		draft_state = 1
		draft_changed.emit(source, draft_state)

	func set_flow(value: int) -> void:
		flow_state = value
		flow_changed.emit(value)


class FakeSession:
	extends Node
	signal capability_unavailable(capability: String, message: String)
	signal interactions_recovered(interactions: Array[Dictionary])
	signal run_resolved(run: Dictionary)
	signal objective_available(run: Dictionary)

	var store: FakeStore
	var stages: Array[String] = []
	var fail_next_turn := false
	var fail_next_hint := false
	var fail_next_build := false
	var next_turn_error: Dictionary = {}
	var fail_next_summary := false
	var include_book := false

	func _init(value: FakeStore) -> void:
		store = value

	func request_build() -> void:
		stages.append("build")
		if fail_next_build:
			fail_next_build = false
			store.set_flow(WalnutClientStore.FlowState.BUILD_FAILED)
			store.error_reported.emit({"code": "SANDBOX_COMPILE_ERROR", "message": "Compiler rejected the source."})
			await get_tree().process_frame
			store.error_reported.emit({"code": "INTERNAL_ERROR", "message": "Teaching feedback unavailable."})
			return
		store.set_flow(4)

	func request_activation() -> void:
		stages.append("activate")
		store.active_skill_tuple = {"skill_id": "skill_demo", "skill_version_id": "skillver_demo"}
		store.set_flow(6)

	func request_submit_and_run() -> Dictionary:
		stages.append("turn")
		if fail_next_summary:
			fail_next_summary = false
			objective_available.emit({"run_id": "run_committed", "status": "SUCCEEDED"})
			store.error_reported.emit({"code": "INTERNAL_ERROR", "message": "Summary unavailable."})
			await get_tree().process_frame
			store.set_flow(WalnutClientStore.FlowState.ERROR)
			return {"ok": false, "stage": "RUN", "message": "Summary unavailable."}
		if not next_turn_error.is_empty():
			store.set_flow(WalnutClientStore.FlowState.ERROR)
			store.error_reported.emit(next_turn_error.duplicate(true))
			next_turn_error.clear()
			return {"ok": false, "stage": "RUN", "message": "Run closure did not complete."}
		if fail_next_turn:
			fail_next_turn = false
			store.set_flow(WalnutClientStore.FlowState.ERROR)
			store.error_reported.emit({
				"scope": "CLIENT_LOCAL",
				"code": "RESOURCE_RECONCILIATION_TIMEOUT",
				"message": "The resource did not reach a terminal state before the total polling deadline.",
				"retryable": true,
			})
			return {"ok": false, "stage": "RUN", "message": "Run closure did not complete."}
		var interactions: Array[Dictionary] = [{
			"interaction_id": "interaction_demo",
			"role": "xiaohutao",
			"response_type": "message",
			"hint_level": null,
			"question": null,
			"feedback": {"message": "正式 Agent 已验证这次提交。"},
		}]
		if include_book:
			interactions.append({"interaction_id":"interaction_book_speech", "role":"book_agent", "response_type":"growth_summary", "feedback":{"message":"循环逐一配对目标湿度与当前湿度。"}})
		interactions_recovered.emit(interactions)
		store.world_snapshot = {"world_id": "world_demo", "revision": 2, "state_hash": "state_hash_2"}
		store.objective_result = {"objective_succeeded": true, "summary": "权威 Run 已闭环。"}
		store.set_flow(9)
		return {"ok": true, "stage": "RUN", "message": "closed"}

	func request_hint(_message: String) -> void:
		stages.append("hint")
		await get_tree().process_frame
		if fail_next_hint:
			fail_next_hint = false
			store.error_reported.emit({"code": "PROVIDER_UNAVAILABLE", "message": "提示服务暂时不可用，请重试。"})
			return
		var interactions: Array[Dictionary] = [{
			"interaction_id": "interaction_hint",
			"role": "teaching_agent",
			"response_type": "message",
			"hint_level": 1,
			"question": null,
			"feedback": {"message": "请比较同一下标的目标值与当前值。"},
		}]
		interactions_recovered.emit(interactions)


func _initialize() -> void:
	var failures: Array[String] = []
	var level := (load(LEVEL_PATH) as PackedScene).instantiate() as CropAdaptiveWateringDemo
	level.timing_scale = 0.05
	root.add_child(level)
	var store := FakeStore.new()
	root.add_child(store)
	var session := FakeSession.new(store)
	root.add_child(session)
	var bridge := BridgeScript.new()
	root.add_child(bridge)
	await process_frame
	var story_overlay := level.get_node("StoryDialogueOverlay") as StoryDialogueOverlay
	story_overlay.skip_sequence()
	await process_frame
	store.local_source = CropAdaptiveWateringDemo.STARTER_CODE
	var audio_cues: Array[StringName] = []
	level.sfx.cue_played.connect(func(cue: StringName) -> void: audio_cues.append(cue))
	bridge.configure(store, session, level)
	bridge.configure(store, session, level)
	var historical_interactions: Array[Dictionary] = [{
		"interaction_id": "interaction_historical_0001",
		"sequence": 1,
		"role": "teaching_agent",
		"response_type": "hint",
		"hint_level": 1,
		"question": null,
		"feedback": {"message": "这是已经展示过的恢复反馈。"},
	}]
	session.interactions_recovered.emit(historical_interactions)
	var activation: Dictionary = bridge.activate_initial_projection()
	if not activation.get("ok", false):
		failures.append("权威恢复完成后必须打开作物适配关卡的首次投影门禁。")
	var presenter := level.get_node("AgentInteractionPresenter") as AgentInteractionPresenter
	var bug_legion := level.get_node("BugLegion2D") as BugLegion2D
	var recovered_projection: Dictionary = level.formal_projection_state()
	if (
		presenter.is_presenting()
		or presenter.pending_count() != 0
		or bug_legion.is_legion_visible()
		or recovered_projection.get("interaction") != historical_interactions.back()
	):
		failures.append("首次恢复必须静态投影已展示 Interaction，不得重播对话或角色军团。")
	level.call("_enter_code_phase")
	if not audio_cues.all(func(cue: StringName) -> bool: return cue == &"PanelOpen"):
		failures.append("恢复历史结果不得重播业务成功音。")
	var source := CropAdaptiveWateringDemo.CORRECT_CODE
	(level.get_node("CodeDrawer/Surface/Margin/Content/CodeEditor") as CodeEdit).text = source
	var action_stages: Array[String] = []
	var action_results: Array[Dictionary] = []
	bridge.build_action_finished.connect(func(result: Dictionary) -> void:
		action_stages.append("BUILD")
		action_results.append(result.duplicate(true))
	)
	bridge.activation_action_finished.connect(func(result: Dictionary) -> void:
		action_stages.append("ACTIVATION")
		action_results.append(result.duplicate(true))
	)
	bridge.submit_action_finished.connect(func(result: Dictionary) -> void:
		action_stages.append("SUBMIT")
		action_results.append(result.duplicate(true))
	)
	var run_button := level.get_node("CodeDrawer/Surface/Margin/Content/Actions/RunButton") as Button
	run_button.pressed.emit()
	for _frame in range(10):
		await process_frame
	if session.stages != ["build", "activate", "turn"]:
		failures.append("一次直接运行必须在后台严格串行完成 Draft→Build→Activation→Agent Turn。")
	if audio_cues.count(&"Confirm") != 1 or audio_cues.count(&"Activate") != 1 or audio_cues.count(&"Complete") != 1 or audio_cues.has(&"Watering"):
		failures.append("正式构建、激活、完成应各播放一次对应音效；没有 WATER 演出时不能有水声。")
	if store.local_source != source:
		failures.append("正式链路必须提交代码界面的当前草稿。")
	if (level.get_node("Hud/WateringCan") as AnimatedSprite2D).visible:
		failures.append("WATER 权威协议未发布前不得播放本地浇水动画冒充 Agent 结果。")
	var evidence := level.get_node("Hud/EvidencePanel/Margin/Content/EvidenceBody") as RichTextLabel
	if not evidence.text.contains("游戏服务确认"):
		failures.append("完成文案必须说明结果已经服务确认。")
	if not level.evidence_title.tooltip_text.contains("revision 2") or not level.evidence_title.tooltip_text.contains("state_hash_2") or evidence.text.contains("state_hash_2"):
		failures.append("精确运行记录应保留在诊断提示中，不占用学生正文。")
	var projection: Dictionary = level.formal_projection_state()
	if (
		projection.get("content") != store.content
		or projection.get("snapshot") != store.world_snapshot
		or str(projection.get("source", "")) != source
		or (level.get_node("Hud/TaskCard/Margin/Content/TaskTitle") as Label).text != "作物适配浇水器"
	):
		failures.append("关卡必须保留原始 Content、Draft 与 Snapshot，并将旧版英文任务名显示为中文。")
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	run_button.pressed.emit()
	for _frame in range(10):
		await process_frame
	if session.stages != ["build", "activate", "turn", "turn"] or action_stages != ["BUILD", "ACTIVATION", "SUBMIT", "SUBMIT"]:
		failures.append("同源重试必须复用已认证/已激活 tuple，只再产生 Submit。")
	session.fail_next_turn = true
	# The real lesson disables PrimaryButton during manual comparison.
	# A terminal submission failure must not inherit that old UI lock.
	level.call("_begin_manual_compare")
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	run_button.pressed.emit()
	for _frame in range(10):
		await process_frame
	var failed_action: Dictionary = action_results.back()
	if (
		bool(failed_action.get("ok", true))
		or str(failed_action.get("code", "")) != "RESOURCE_RECONCILIATION_TIMEOUT"
		or not str(failed_action.get("message", "")).contains("did not reach a terminal state")
	):
		failures.append("资源轮询超时必须保留净化后的 code/message，不得折叠成无结构结果。")
	story_overlay.skip_sequence()
	await process_frame
	var before_retry: Dictionary = level.formal_projection_state()
	if level.primary_button.disabled or not level.primary_button.visible:
		failures.append("提交失败后必须恢复我自己修改按钮，不能继承手动比较阶段的禁用状态。")
	else:
		level.primary_button.pressed.emit()
		await process_frame
		if not level.code_drawer.visible or level.code_editor.text != source or level.formal_projection_state().snapshot != before_retry.snapshot:
			failures.append("我自己修改必须打开现有代码，不能修改草稿或世界。")
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	if not level.hint_button.visible or level.drawer_hint_button.disabled:
		failures.append("代码阶段必须提供农场和编辑器文字提示入口。")
	var stages_before_hint := session.stages.size()
	level.drawer_hint_button.pressed.emit()
	if not level.hint_button.disabled or not run_button.disabled:
		failures.append("请求提示时必须显示等待状态并阻止并行运行。")
	level.hint_button.pressed.emit()
	level.agent_submit_requested.emit(source)
	if session.stages.size() != stages_before_hint + 1:
		failures.append("提示等待期间不得重复请求或并发运行。")
	for _frame in range(5):
		await process_frame
	if session.stages.back() != "hint" or not evidence.text.contains("同一下标") or level.hint_button.disabled:
		failures.append("问叮当必须通过正式 Hint Turn 展示 AgentInteraction。")
	story_overlay.skip_sequence()
	session.fail_next_hint = true
	level.hint_button.pressed.emit()
	for _frame in range(5): await process_frame
	if level.hint_button.disabled or run_button.disabled or not evidence.text.contains("重试"):
		failures.append("提示失败必须恢复按钮并显示可重试反馈。")
	session.next_turn_error = {"code": "INTERNAL_ERROR", "category": "INTERNAL", "message": "RAW_PROVIDER_ERROR", "details": {"exception_type": "WorkflowInvariantError"}}
	story_overlay.skip_sequence()
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	run_button.pressed.emit()
	for _frame in range(10): await process_frame
	if level.evidence_title.text != "服务执行失败" or not evidence.text.contains("代码检查已通过") or evidence.text.contains("RAW_PROVIDER_ERROR") or level.primary_button.disabled:
		failures.append("已认证代码遇到后端内部错误，应显示服务失败、保留修改入口，不能让学生继续改正确答案。")
	if action_results.back().code != "INTERNAL_ERROR" or not level.evidence_title.tooltip_text.contains("INTERNAL_ERROR"):
		failures.append("后端错误编号必须传递到结果和可查看的标题提示。")
	story_overlay.skip_sequence()
	session.fail_next_summary = true
	var snapshot_before_summary := store.world_snapshot.duplicate(true)
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	run_button.pressed.emit()
	if level.evidence_title.text != "运行已成功，反馈暂未完成":
		failures.append("收到已提交的成功 Run 后，反馈错误不能立即误报世界没有变化。")
	for _frame in range(10): await process_frame
	if level.evidence_title.text != "运行已成功，反馈暂未完成" or not evidence.text.contains("结果已保存"):
		failures.append("成功 Run 后的总结故障必须与程序执行失败区分。")
	if store.world_snapshot != snapshot_before_summary or bool(action_results.back().ok):
		failures.append("提前获知运行成功不能伪造完整闭环或修改世界快照。")
	session.next_turn_error = {"code": "INTERNAL_ERROR", "message": "Next run failed."}
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	run_button.pressed.emit()
	for _frame in range(10): await process_frame
	if level.evidence_title.text != "服务执行失败":
		failures.append("上次成功通知不能掩盖下一次运行的失败。")
	story_overlay.skip_sequence()
	session.fail_next_build = true
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	level.agent_submit_requested.emit(source + "\n// compile rejection check")
	if level.evidence_title.text != "代码检查未通过":
		failures.append("编译拒绝必须立即显示代码检查未通过，不能等待教学反馈时误报没有连上。")
	for _frame in range(10): await process_frame
	if level.evidence_title.text != "代码检查未通过" or not evidence.text.contains("编译"):
		failures.append("教学反馈失败不能覆盖已经确认的编译错误。")
	level.complete_agent_submission("run_book_summary")
	var book_message := "你用同一下标配对当前湿度和目标湿度，条件判断让足够湿润的地块跳过了浇水。换一组湿度后，你会先检查哪个条件？"
	level.restore_agent_interaction({"role": "book_agent", "response_type": "growth_summary", "feedback": {"message": book_message}})
	var book_summary := level.get_node_or_null("CompletionCard/Margin/Content/BookSummary") as RichTextLabel
	if book_summary == null or not book_summary.visible or not book_summary.text.contains(book_message):
		failures.append("通关卡必须展示书书的真实总结，不能被固定奖励文案遮挡。")
	level.begin_agent_submission("new submission")
	level.complete_agent_submission("run_without_book")
	# A new completion must not reuse the last run's summary before feedback arrives.
	if book_summary != null and book_summary.visible:
		failures.append("新一轮运行不能复用上一轮书书的总结。")
	var speech := FakeBookSpeech.new()
	root.add_child(speech)
	bridge._book_speech = speech
	session.include_book = true
	story_overlay.skip_sequence()
	level.call("_set_phase", CropAdaptiveWateringDemo.Phase.CODE)
	level.agent_submit_requested.emit(source)
	for _frame in range(3): await process_frame
	if speech.calls != 1 or level.completion_card.visible or evidence.text.contains("循环逐一配对"):
		failures.append("完整语音准备好之前不得展示书书的文字或完成卡。")
	var stages_before_speech_retry := session.stages.size()
	speech.release.emit()
	await process_frame
	if level.primary_button.text != "重试总结语音" or level.completion_card.visible:
		failures.append("语音失败必须提供单独重试，且不能提前展示总结。")
	speech.reject = false
	level.primary_button.pressed.emit()
	await process_frame
	speech.release.emit()
	await process_frame
	if speech.calls != 2 or session.stages.size() != stages_before_speech_retry:
		failures.append("重试语音不得重新构建、激活或执行代码。")
	if not level.completion_card.visible or not level.book_summary_body.text.contains("循环逐一配对") or not is_instance_valid(level.book_speaker) or not level.book_speaker.playing:
		failures.append("文字与完整音频就绪后必须同时展示和播放。")
	level.completion_card.hide()
	await process_frame
	if level.book_speaker.playing:
		failures.append("离开完成卡必须停止书书的语音。")
	# The new entry flow gates the main write and survives a legacy Book failure.
	var practice_cases = load("res://tests/client/bug_practice_flow_test.gd")
	var practice_api = practice_cases.FakeGateway.new()
	level.practice.add_child(practice_api)
	practice_api.challenge.run_id = "run_committed"
	level.practice.api = practice_api
	level.practice.enabled = true
	practice_api.fail_action = "start"
	var prior_stages := session.stages.size()
	await bridge._on_submit_requested(source)
	if session.stages.size() != prior_stages:
		failures.append("本局 start 未确认前不得提交主关。")
	session.fail_next_summary = true
	var speech_calls: int = speech.calls
	await bridge._on_submit_requested(source)
	while level.practice.busy: await process_frame
	if level.practice.state.get("phase") != "CHALLENGE_READY" or level.completion_card.visible:
		failures.append("主关成功后即进入挑战，旧反馈失败不能替代挑战或提前完成。")
	if speech.calls != speech_calls:
		failures.append("新挑战流程不得调用旧 Book 音频接口。")
	var legacy: Array[Dictionary] = [
		{"role": "bug_agent"}, {"role": "book_agent"}, {"role": "teaching_agent"},
	]
	if bridge._practice_visible_interactions(legacy) != [{"role": "teaching_agent"}]:
		failures.append("新流程只过滤旧 Bug/Book 展示，保留叮当教学反馈。")
	speech.queue_free()
	bridge.queue_free()
	session.queue_free()
	store.queue_free()
	level.queue_free()
	await process_frame
	if failures.is_empty():
		print("CROP_AGENT_BRIDGE_TEST_PASS: 合并交付与正式提示链路通过")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
