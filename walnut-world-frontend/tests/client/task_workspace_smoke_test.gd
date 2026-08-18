extends SceneTree

const TASK_WORKSPACE := preload("res://scenes/task/task_workspace.tscn")
const ClientStoreScript := preload("res://autoload/client_store.gd")
const SessionControllerScript := preload("res://autoload/session_controller.gd")


class SubmitSession:
	extends Node
	var store: WalnutClientStore
	var build_calls := 0
	var activation_calls := 0
	var run_calls := 0

	func _init(client_store: WalnutClientStore) -> void:
		store = client_store

	func request_build() -> void:
		build_calls += 1
		store.set_flow(WalnutClientStore.FlowState.CERTIFIED)

	func request_activation() -> void:
		activation_calls += 1
		store.active_skill_tuple = {
			"activation_id": "activation_demo_0001",
			"skill_id": "skill_demo_0001",
			"skill_version_id": "skillver_demo_0001",
			"artifact_sha256": "c".repeat(64),
			"certification_id": "cert_demo_0001",
			"registry_revision": 3,
			"activated_at": "2026-08-12T00:00:00Z",
		}
		store.set_flow(WalnutClientStore.FlowState.ACTIVE)

	func request_submit_and_run() -> Dictionary:
		run_calls += 1
		store.set_flow(WalnutClientStore.FlowState.COMPLETED)
		return {"ok": true, "stage": "RUN", "message": "direct run closed"}


func _initialize() -> void:
	var failures: Array[String] = []
	var store := root.get_node_or_null("ClientStore") as WalnutClientStore
	if store == null:
		store = ClientStoreScript.new()
		store.name = "ClientStore"
		root.add_child(store)
	var session := root.get_node_or_null("SessionController") as Node
	if session == null:
		session = SessionControllerScript.new()
		session.name = "SessionController"
		root.add_child(session)
	store.replace_world({"world_id": "world_demo_0001", "revision": 2, "last_event_sequence": 3, "state_schema_version": "1.0.0", "state_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "world_rules_version": "rules", "state": {"avatar": {"position": {"x": 1, "y": 1}}, "plots": [{"position": {"x": 1, "y": 1}, "soil_state": "TILLED", "hydration": 100}]}})
	var page := TASK_WORKSPACE.instantiate()
	root.add_child(page)
	await process_frame
	for path in [
		"WorldViewport",
		"AgentInteractionPresenter/StoryDialogueOverlay",
		"Hud/SafeArea/EdgeLayer/TaskTag/Artwork",
		"Hud/SafeArea/EdgeLayer/TaskTag/TaskText/TaskTitle",
		"Hud/SafeArea/EdgeLayer/TaskTag/TaskText/TaskGoal",
		"Hud/SafeArea/EdgeLayer/TaskTag/TaskText/TaskProgressPanel",
		"Hud/SafeArea/EdgeLayer/ToolRail/CodeDrawerButton",
		"Hud/SafeArea/EdgeLayer/ToolRail/HintButton",
		"Hud/SafeArea/EdgeLayer/AutoSavePill/AutoSaveState",
		"DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/RunControlPanel/Actions/ResetButton",
		"DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/RunControlPanel/Actions/BuildButton",
		"DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/RunControlPanel/Actions/ActivationButton",
		"DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/RunControlPanel/Actions/SubmitButton",
		"DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DialoguePanel/Margin/Content/ResponseType",
		"DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DialoguePanel/Margin/Content/Question",
		"DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeEditorPanel/Content/CodeEditor",
		"Hud/SafeArea/EdgeLayer/Toast/ResultText",
		"AutoSaveTimer",
		"ToastTimer",
		"GrowthSummaryPanel",
	]:
		if page.get_node_or_null(path) == null:
			failures.append("缺少预置节点：%s" % path)
	if page.get_node_or_null("Hud/ActionBar") != null:
		failures.append("主世界底部不得保留常驻 ActionBar。")
	if page.patch_decisions_enabled or page.get_node_or_null("CodePatchDialog") != null:
		failures.append("正式 TaskWorkspace 默认不得装配排除项 PatchDecision 对话框。")
	var hud_layer := page.get_node_or_null("Hud") as CanvasLayer
	var drawer_layer := page.get_node_or_null("DrawerLayer") as CanvasLayer
	if hud_layer == null or drawer_layer == null or drawer_layer.layer <= hud_layer.layer:
		failures.append("代码抽屉必须位于主 HUD 之上的独立 CanvasLayer。")
	var editor := page.get_node_or_null("DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/CodeEditorPanel/Content/CodeEditor") as CodeEdit
	if editor == null or editor.text.is_empty():
		failures.append("CodeEdit 未加载默认 C++ 草稿。")
	if editor == null:
		for failure in failures:
			push_error(failure)
		quit(1)
		return
	var production_session: Node = page.session
	var submit_session := SubmitSession.new(store)
	root.add_child(submit_session)
	page.session = submit_session
	store.active_skill_tuple = {}
	store.draft = {"draft_id": "draft_demo_0001"}
	store.set_flow(WalnutClientStore.FlowState.BOOTSTRAPPING)
	page._refresh_action_buttons()
	if not page.build_button.disabled or not page.activation_button.disabled or not page.submit_button.disabled:
		failures.append("Bootstrap authority 未闭合时三个学生动作必须全部禁用。")
	store.set_flow(WalnutClientStore.FlowState.ERROR)
	page._refresh_action_buttons()
	if not page.build_button.disabled or not page.activation_button.disabled or not page.submit_button.disabled:
		failures.append("Startup ERROR must keep Build, Activation, and Submit/Run disabled.")
	store.set_flow(WalnutClientStore.FlowState.READY)
	page._refresh_action_buttons()
	if page.build_button.disabled or not page.activation_button.disabled or not page.submit_button.disabled:
		failures.append("Fresh UI 必须只开放显式 Build，Activation 与 Submit/Run 在获得各自权威前保持禁用。")
	page.build_button.pressed.emit()
	await process_frame
	if submit_session.build_calls != 1 or submit_session.activation_calls != 0 or submit_session.run_calls != 0 or page.activation_button.disabled == true:
		failures.append("Build 按钮只能完成 Build/Certification，并在成功后开放独立 Activation 动作。")
	page.activation_button.pressed.emit()
	await process_frame
	if submit_session.activation_calls != 1 or submit_session.run_calls != 0 or page.submit_button.disabled:
		failures.append("Activation 按钮只能发布 exact tuple，并在成功后开放独立 Submit/Run 动作。")
	page.submit_button.pressed.emit()
	await process_frame
	if submit_session.build_calls != 1 or submit_session.activation_calls != 1 or submit_session.run_calls != 1:
		failures.append("正式 TaskWorkspace 必须由三个学生按钮逐阶段触发 Build、Activation 与 Submit/Run，不得单击自动串联。")
	page.session = production_session
	store.active_skill_tuple = {}
	submit_session.queue_free()
	store.set_workspace({"task_title": "Demo task", "task_goal": "Demo goal", "current_task": {"task_id": "task_demo_0001", "status": "COMPLETED"}})
	store.set_content({"task": {"name": "Contract title", "goal": "Contract goal", "starter_skill": {"source_bundle": {"entrypoint": "src/main.cpp", "files": [{"path": "src/main.cpp", "content": "int main() { return 0; }"}]}}}})
	# Workspace has no task_title/task_goal in the frozen contract.  A later
	# Workspace projection must never overwrite ContentUnit-owned UI fields.
	store.set_workspace({"task_title": "Untrusted workspace title", "task_goal": "Untrusted workspace goal", "current_task": {"task_id": "task_demo_0001", "status": "COMPLETED"}})
	var interactions: Array[Dictionary] = [{"role": "book_agent", "response_type": "growth_summary", "feedback": {"message": "Verified growth summary", "degraded": false, "evidence_refs": []}}]
	session.interactions_recovered.emit(interactions)
	await process_frame
	var progress := page.get_node_or_null("Hud/SafeArea/EdgeLayer/TaskTag/TaskText/TaskProgressPanel") as Label
	var summary := page.get_node_or_null("GrowthSummaryPanel") as AcceptDialog
	var world_status := page.get_node_or_null("WorldViewport/ViewportShell/WorldStatus") as Label
	var title := page.get_node_or_null("Hud/SafeArea/EdgeLayer/TaskTag/TaskText/TaskTitle") as Label
	var goal := page.get_node_or_null("Hud/SafeArea/EdgeLayer/TaskTag/TaskText/TaskGoal") as Label
	if progress == null or not progress.text.contains("已完成") or summary == null or not summary.dialog_text.contains("Verified growth summary") or title == null or title.text != "Contract title" or goal == null or goal.text != "Contract goal":
		failures.append("Workspace and growth-summary projections must update preauthored UI nodes.")
	if world_status == null or not world_status.text.contains("world_demo_0001"):
		failures.append("WorldViewport must project a Snapshot that was recovered before scene entry.")
	editor.text = "int main() { return 99; }"
	editor.text_changed.emit()
	page.reset_code_to_starter()
	if editor.text != "int main() { return 0; }" or store.local_source != "int main() { return 0; }" or store.draft_state != WalnutClientStore.DraftState.DIRTY:
		failures.append("代码重置必须恢复 ContentUnit 提供的 starter_skill，而不是猜测本地内容。")
	var hint_interactions: Array[Dictionary] = [{"role": "teaching_agent", "response_type": "hint", "question": "循环应该在什么时候停止？", "hint_level": 2, "feedback": {"message": "先观察循环条件。", "degraded": false, "evidence_refs": []}}]
	session.interactions_recovered.emit(hint_interactions)
	await process_frame
	var response_type := page.get_node_or_null("DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DialoguePanel/Margin/Content/ResponseType") as Label
	var question := page.get_node_or_null("DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DialoguePanel/Margin/Content/Question") as Label
	if response_type == null or response_type.text != "概念提示" or question == null or not question.visible or not question.text.contains("循环应该在什么时候停止"):
		failures.append("AgentInteraction 的提示等级和问题必须映射到预置 DialoguePanel。")
	var bug_interactions: Array[Dictionary] = [{"interaction_id": "interaction_bug_smoke_0001", "role": "bug_agent", "response_type": "message", "question": null, "hint_level": null, "feedback": {"message": "这是第三次相同失败，请检查边界条件。", "degraded": false, "evidence_refs": []}}]
	session.interactions_recovered.emit(bug_interactions)
	await process_frame
	var speaker := page.get_node_or_null("DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DialoguePanel/Margin/Content/Speaker") as Label
	var objective_failure_text := page.get_node_or_null("DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DialoguePanel/Margin/Content/DialogueText") as Label
	if speaker == null or speaker.text != "Bug 先生" or objective_failure_text == null or objective_failure_text.text != "这是第三次相同失败，请检查边界条件。":
		failures.append("第三次相同目标失败的 Bug AgentInteraction 必须显示在正式 DialoguePanel。")
	var agent_overlay := page.get_node_or_null("AgentInteractionPresenter/StoryDialogueOverlay")
	var formal_bug_legion := page.get_node_or_null("WorldViewport/ViewportShell/SubViewportContainer/SubViewport/FarmWorld/AgentPresentation/BugLegion")
	if agent_overlay == null or not agent_overlay.visible or agent_overlay.speaker_label.text != "Bug 先生" or formal_bug_legion == null or not formal_bug_legion.visible:
		failures.append("正式任务流必须同时展示 Bug 先生对话头像与世界中的 Bug 军团。")
	if agent_overlay != null:
		agent_overlay.skip_sequence()
	await create_timer(0.25).timeout
	if formal_bug_legion != null and formal_bug_legion.visible:
		failures.append("Bug 先生对话结束后，世界中的 Bug 军团必须退出。")
	var message_interactions: Array[Dictionary] = [{"role": "xiaohutao", "response_type": "message", "question": null, "hint_level": null, "feedback": {"message": "真实运行已经完成。", "degraded": false, "evidence_refs": []}}]
	session.interactions_recovered.emit(message_interactions)
	await process_frame
	var dialogue_text := page.get_node_or_null("DrawerLayer/CodeDrawer/DrawerSurface/DrawerMargin/Content/DialoguePanel/Margin/Content/DialogueText") as Label
	if response_type == null or response_type.text != "任务说明" or dialogue_text == null or dialogue_text.text != "真实运行已经完成。":
		failures.append("AgentInteraction 可空 hint_level 必须安全映射为普通消息，不得触发整数转换错误。")
	var detailed_result := page.get_node_or_null("Hud/SafeArea/EdgeLayer/Toast/ResultText") as RichTextLabel
	session.build_resolved.emit({
		"status": "CERTIFIED",
		"phases": [{"name": "COMPILE", "status": "PASSED", "diagnostic_codes": ["COMPILE_OK"]}],
		"evidence_refs": [{"evidence_id": "evidence_build_ui_0001", "evidence_type": "TEST_REPORT"}],
	})
	await process_frame
	if detailed_result == null or not detailed_result.text.contains("CERTIFIED") or not detailed_result.text.contains("COMPILE_OK") or not detailed_result.text.contains("evidence_build_ui_0001"):
		failures.append("Production TaskWorkspace must render detailed Build phases/evidence through ResultPanel.show_build.")
	session.run_resolved.emit({
		"status": "FAILED",
		"sandbox": {"status": "SUCCEEDED", "usage": {"cpu_ms": 12, "wall_ms": 18, "peak_memory_bytes": 4096}},
		"world_application": {"status": "REJECTED", "receipt": null},
		"agent_feedback": {"source": "provider", "message": "Objective incomplete", "degraded": false},
		"evidence_refs": [{"evidence_id": "evidence_run_ui_0001", "evidence_type": "SANDBOX_LOG"}],
	})
	await process_frame
	if detailed_result == null or not detailed_result.text.contains("FAILED") or not detailed_result.text.contains("REJECTED") or not detailed_result.text.contains("evidence_run_ui_0001"):
		failures.append("Production TaskWorkspace must render detailed Run/Sandbox/World/evidence through ResultPanel.show_run.")
	var excluded_patch_interactions: Array[Dictionary] = [{"role": "teaching_agent", "response_type": "patch", "question": null, "hint_level": null, "feedback": {"message": "Excluded patch", "degraded": false, "evidence_refs": []}, "skill_patch": {"patch_id": "patch_excluded_0001", "requires_student_confirmation": true}}]
	session.interactions_recovered.emit(excluded_patch_interactions)
	await process_frame
	if page.get_node_or_null("CodePatchDialog") != null or not page.pending_patch_interaction.is_empty():
		failures.append("Patch interaction 不得在默认正式组合中弹窗或建立待决状态。")
	page.pending_patch_interaction = excluded_patch_interactions[0].duplicate(true)
	page._accept_patch()
	if not page.pending_patch_interaction.is_empty():
		failures.append("默认关闭时 PatchDecision 调用入口必须 fail closed。")
	var task_tag := page.get_node_or_null("Hud/SafeArea/EdgeLayer/TaskTag") as Control
	var task_goal_label := page.get_node_or_null("Hud/SafeArea/EdgeLayer/TaskTag/TaskText/TaskGoal") as Label
	var autosave_pill := page.get_node_or_null("Hud/SafeArea/EdgeLayer/AutoSavePill") as Control
	store.draft_state = WalnutClientStore.DraftState.CLEAN
	store.draft_changed.emit(store.local_source, store.draft_state)
	await process_frame
	if task_tag == null or progress == null or not progress.visible or task_tag.size.y > 92.0:
		failures.append("任务徽章默认必须保持紧凑，并直接显示短进度。")
	if task_goal_label == null or task_goal_label.visible:
		failures.append("任务目标长文本不得常驻显示，应通过 tooltip 或代码抽屉按需披露。")
	if autosave_pill == null or autosave_pill.visible:
		failures.append("自动保存成功状态应保持静默，不得常驻占用右上角。")
	var compact_height := task_tag.size.y if task_tag != null else 0.0
	page.toggle_task_tag()
	await create_timer(0.3).timeout
	if task_tag != null and not is_equal_approx(task_tag.size.y, compact_height):
		failures.append("任务徽章不得再通过频繁展开/收起来展示信息。")
	page.show_code_drawer()
	await create_timer(0.5).timeout
	var drawer := page.get_node_or_null("DrawerLayer/CodeDrawer") as Control
	if drawer == null or not drawer.visible or drawer.position.x != 0.0 or drawer.mouse_filter == Control.MOUSE_FILTER_IGNORE:
		failures.append("代码抽屉必须能以可验证的最终位置展开。")
	if task_tag == null or task_tag.modulate.a > 0.6:
		failures.append("代码抽屉打开时，常驻任务徽章必须自动降权。")
	page.hide_code_drawer()
	await create_timer(0.3).timeout
	if drawer == null or drawer.visible or drawer.mouse_filter != Control.MOUSE_FILTER_IGNORE:
		failures.append("代码抽屉关闭后必须隐藏并停止拦截世界输入。")
	if task_tag == null or task_tag.modulate.a < 0.95:
		failures.append("代码抽屉关闭后，常驻任务徽章必须恢复正常权重。")
	if failures.is_empty():
		print("TASK_WORKSPACE_SMOKE_TEST_PASS")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)
