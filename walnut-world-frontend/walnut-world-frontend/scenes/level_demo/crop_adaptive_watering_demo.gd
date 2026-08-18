class_name CropAdaptiveWateringDemo
extends Control

signal replay_requested
signal next_level_requested
signal return_home_requested
signal agent_submit_requested(source: String)
signal agent_build_requested(source: String)
signal agent_activation_requested
signal agent_hint_requested(message: String)
signal agent_draft_changed(source: String)

enum Phase {
	INTRO,
	OLD_TOOL,
	MANUAL_COMPARE,
	SKILL_TREE,
	WORKSHOP,
	CODE,
	CERTIFIED,
	ACTIVE,
	RUNNING,
	FAILED,
	OBJECTIVE_COMPLETE,
	GROWTH_SUMMARY,
	SKILL_UNLOCKED,
	FREE_PLAY,
	COMPLETED,
	CANDIDATE_VALIDATING,
	CANDIDATE_PRESENTING,
	LOCAL_FAILED,
	LOCAL_COMPLETED,
	CHAIN_ERROR,
	BUILDING,
	ACTIVATING,
}

const MOISTURE: Array[int] = [20, 65, 45, 90, 60, 35, 55, 50]
const TARGET: Array[int] = [60, 70, 50, 65, 60, 70, 50, 65]
const CROPS: Array[String] = ["胡萝卜", "番茄", "土豆", "玉米", "胡萝卜", "番茄", "土豆", "玉米"]
const EXPECTED_UNITS: Array[int] = [2, 1, 1, 0, 0, 2, 0, 1]
const MANUAL_ORDER: Array[int] = [1, 6, 5]
const INITIAL_PRACTICE_CODE: String = "#include <iostream>\nusing namespace std;\n\nint main() {\n    int moisture[8] = {20, 65, 45, 90, 60, 35, 55, 50};\n    int target[8]   = {60, 70, 50, 65, 60, 70, 50, 65};\n\n    for (int i = 0; i < 8; i++) {\n        // 第一步：将两个标记替换为目标数组名和当前数组名\n        int gap = /*目标*/[i] - /*当前*/[i];\n\n        // 第二步：将边界和份数标记替换为正确数字\n        if (gap >= /*边界*/) {\n            cout << \"WATER \" << i << \" /*份数*/\\n\";\n        } else if (gap > /*边界*/) {\n            cout << \"WATER \" << i << \" /*份数*/\\n\";\n        }\n\n        // 提示：gap <= 0 时不输出 WATER，喷头保持关闭\n        // 真实运行出错后，可继续修改整个循环体\n    }\n    return 0;\n}\n"
const STARTER_CODE: String = "#include <iostream>\nusing namespace std;\n\nint main() {\n    int moisture[8] = {20, 65, 45, 90, 60, 35, 55, 50};\n    int target[8]   = {60, 70, 50, 65, 60, 70, 50, 65};\n\n    for (int i = 0; i < 8; i++) {\n        int gap = 60 - moisture[i];\n\n        if (gap >= 30) {\n            cout << \"WATER \" << i << \" 2\\n\";\n        } else if (gap > 0) {\n            cout << \"WATER \" << i << \" 1\\n\";\n        }\n    }\n    return 0;\n}\n"
const CORRECT_CODE: String = "#include <iostream>\nusing namespace std;\n\nint main() {\n    int moisture[8] = {20, 65, 45, 90, 60, 35, 55, 50};\n    int target[8]   = {60, 70, 50, 65, 60, 70, 50, 65};\n\n    for (int i = 0; i < 8; i++) {\n        int gap = target[i] - moisture[i];\n\n        if (gap >= 30) {\n            cout << \"WATER \" << i << \" 2\\n\";\n        } else if (gap > 0) {\n            cout << \"WATER \" << i << \" 1\\n\";\n        }\n    }\n    return 0;\n}\n"
const INTRO_LINES: Array[String] = [
	"刚才的阵雨没有落在每一块土地上。",
	"相同的湿度，面对不同作物，也许需要完全不同的动作。",
	"先看看旧工具为什么照顾不好这片混合试验田吧。",
]
const WORKSHOP_EXPERIMENTS: Array[Dictionary] = [
	{
		"badge": "第一步 / 2 · 数据配对",
		"title": "用同一个 i 补全湿度缺口",
		"body": "1 号番茄的 target[1] = 70，moisture[1] = 65。\n请把两个数组名填进空白，得到“目标湿度 - 当前湿度”。",
		"action": "检查第一步  →",
	},
	{
		"badge": "第二步 / 2 · 分级动作",
		"title": "根据 gap 补全 0 / 1 / 2 份规则",
		"body": "缺口 ≥ 30：浇 2 份　0 < 缺口 < 30：浇 1 份。缺口 ≤ 0：不输出 WATER。\n请补全两个边界和两个输出份数。",
		"action": "检查第二步  →",
	},
	{
		"badge": "理解汇总 · 准备完整练习",
		"title": "把刚才学到的两条理解结合起来",
		"body": "先用同一个 i 配对 target[i] 与 moisture[i]，再让计算出的 gap 进入 0 / 1 / 2 份分级。\n下一页将严格按关卡文档打开完整 C++ 练习，你要把这两条理解自己写进卷轴。",
		"action": "把两条规则结合起来，开始完整练习  →",
	},
]
const PROFILE_CATALOG := preload("res://resources/agent/agent_character_catalog.tres")
const CROP_TEXTURES := [
	preload("res://assets/art/generated/crops/carrot.png"),
	preload("res://assets/art/generated/crops/tomato.png"),
	preload("res://assets/art/generated/crops/potato.png"),
	preload("res://assets/art/generated/crops/corn.png"),
]

@export_range(0.05, 2.0, 0.05) var timing_scale: float = 1.0

@onready var plot_grid: GridContainer = %PlotGrid
@onready var task_title: Label = %TaskTitle
@onready var task_progress: Label = %TaskProgress
@onready var phase_strip: Label = %PhaseStrip
@onready var evidence_panel: Control = %EvidencePanel
@onready var evidence_title: Label = %EvidenceTitle
@onready var evidence_body: RichTextLabel = %EvidenceBody
@onready var primary_button: Button = %PrimaryButton
@onready var hint_button: Button = %HintButton
@onready var request_patch_button: Button = %RequestPatchButton
@onready var code_button: Button = %CodeButton
@onready var water_choices: HBoxContainer = %WaterChoices
@onready var skip_water_button: Button = %SkipWaterButton
@onready var one_water_button: Button = %OneWaterButton
@onready var two_water_button: Button = %TwoWaterButton
@onready var code_drawer: Control = %CodeDrawer
@onready var code_editor: CodeEdit = %CodeEditor
@onready var dismiss_code_button: Button = %DismissCodeButton
@onready var close_code_button: Button = %CloseCodeButton
@onready var run_button: Button = %RunButton
@onready var reset_button: Button = %ResetButton
@onready var save_state: Label = %SaveState
@onready var story_dialogue: StoryDialogueOverlay = %StoryDialogueOverlay
@onready var watering_can: AnimatedSprite2D = %WateringCan
@onready var completion_card: Control = %CompletionCard
@onready var completion_title: Label = %CompletionTitle
@onready var completion_summary: Label = %CompletionSummary
@onready var replay_button: Button = %ReplayButton
@onready var next_button: Button = %NextButton
@onready var return_button: Button = %ReturnButton
@onready var patch_dialog: ConfirmationDialog = %PatchDialog
@onready var autosave_timer: Timer = %AutosaveTimer
@onready var playback_speed_button: Button = %PlaybackSpeedButton
@onready var skip_playback_button: Button = %SkipPlaybackButton
@onready var replay_playback_button: Button = %ReplayPlaybackButton
@onready var skill_tree_overlay: Control = %SkillTreeOverlay
@onready var skill_tree_badge: Label = %SkillTreeBadge
@onready var skill_tree_title: Label = %SkillTreeTitle
@onready var skill_tree_body: RichTextLabel = %SkillTreeBody
@onready var skill_tree_continue_button: Button = %SkillTreeContinueButton
@onready var workshop_overlay: Control = %WorkshopOverlay
@onready var workshop_badge: Label = %WorkshopBadge
@onready var workshop_title: Label = %WorkshopTitle
@onready var workshop_body: RichTextLabel = %WorkshopBody
@onready var workshop_gap_code: Control = %WorkshopGapCode
@onready var workshop_branch_code: Control = %WorkshopBranchCode
@onready var workshop_summary_code: Control = %WorkshopSummaryCode
@onready var gap_target_input: LineEdit = %GapTargetInput
@onready var gap_moisture_input: LineEdit = %GapMoistureInput
@onready var severe_boundary_input: LineEdit = %SevereBoundaryInput
@onready var severe_units_input: LineEdit = %SevereUnitsInput
@onready var light_boundary_input: LineEdit = %LightBoundaryInput
@onready var light_units_input: LineEdit = %LightUnitsInput
@onready var workshop_action_button: Button = %WorkshopActionButton
@onready var bug_challenge_overlay: Control = %BugChallengeOverlay
@onready var bug_challenge_body: RichTextLabel = %BugChallengeBody
@onready var bug_continue_button: Button = %BugContinueButton
@onready var growth_summary_overlay: Control = %GrowthSummaryOverlay
@onready var growth_summary_body: RichTextLabel = %GrowthSummaryBody
@onready var archive_button: Button = %ArchiveButton

var _phase: int = Phase.INTRO
var _manual_cursor: int = 0
var _selected_manual_plot: int = -1
var _build_result: Dictionary = {}
var _same_failure_key: String = ""
var _same_failure_count: int = 0
var _last_chain_error_detail: String = ""
var _agent_stage_message_visible := false
var _hint_level: int = -1
var reject_patch_button: Button
var _workshop_step := 0
var _skill_tree_showing_unlocked := false
var _bug_challenge_seen := false
var _patch_pending := false
var _patch_stale := false
var _patch_rejected_at_failure := -1
var _used_ai_patch := false
var _used_hint_levels: Dictionary = {}
var _draft_active := false
var _drawer_tween: Tween
var _evidence_tween: Tween
var _button_tweens: Dictionary = {}
var _agent_mode := false
var _synchronizing_agent_draft := false
var _agent_source := ""
var _clean_agent_source := ""
var _agent_task_title := ""
var _authoritative_content: Dictionary = {}
var _authoritative_snapshot: Dictionary = {}
var _last_agent_interaction: Dictionary = {}
var _candidate_compatibility_available := false
var _candidate_playback_speed := 1.0
var _candidate_skip_requested := false
var _candidate_playing := false
var _last_candidate_result: Dictionary = {}


func _ready() -> void:
	primary_button.pressed.connect(_on_primary_pressed)
	hint_button.pressed.connect(_on_hint_pressed)
	request_patch_button.pressed.connect(_on_patch_requested)
	code_button.pressed.connect(_show_code_drawer)
	dismiss_code_button.pressed.connect(_hide_code_drawer)
	close_code_button.pressed.connect(_hide_code_drawer)
	skip_water_button.pressed.connect(_choose_manual_water.bind(0))
	one_water_button.pressed.connect(_choose_manual_water.bind(1))
	two_water_button.pressed.connect(_choose_manual_water.bind(2))
	run_button.pressed.connect(_request_run)
	reset_button.pressed.connect(_reset_code)
	replay_button.pressed.connect(func() -> void: replay_requested.emit())
	next_button.pressed.connect(_on_completion_next_pressed)
	return_button.pressed.connect(func() -> void: return_home_requested.emit())
	patch_dialog.confirmed.connect(_accept_patch)
	patch_dialog.custom_action.connect(_on_patch_custom_action)
	reject_patch_button = patch_dialog.add_button("拒绝修改", true, "reject_patch")
	reject_patch_button.name = "RejectPatchButton"
	reject_patch_button.disabled = false
	reject_patch_button.tooltip_text = "拒绝当前提案，并保留自己的代码"
	skill_tree_continue_button.pressed.connect(_on_skill_tree_continue_pressed)
	workshop_action_button.pressed.connect(_on_workshop_action_pressed)
	bug_continue_button.pressed.connect(_on_bug_continue_pressed)
	archive_button.pressed.connect(_on_archive_pressed)
	code_editor.text_changed.connect(_on_code_changed)
	autosave_timer.timeout.connect(_flush_autosave)
	playback_speed_button.pressed.connect(_on_playback_speed_pressed)
	skip_playback_button.pressed.connect(_on_skip_playback_pressed)
	replay_playback_button.pressed.connect(_on_replay_playback_pressed)
	for index in range(plot_grid.get_child_count()):
		var card := plot_grid.get_child(index) as CropPlotCard
		card.configure(index, CROPS[index], MOISTURE[index], TARGET[index], CROP_TEXTURES[index % 4])
		card.plot_pressed.connect(_on_plot_pressed)
	code_editor.text = INITIAL_PRACTICE_CODE
	completion_card.visible = false
	code_drawer.visible = false
	_hide_lesson_overlays()
	water_choices.visible = false
	watering_can.visible = false
	_set_phase(Phase.INTRO)
	if is_visible_in_tree():
		call_deferred("_play_intro")


func restart_level() -> void:
	story_dialogue.skip_sequence()
	if patch_dialog.visible:
		patch_dialog.hide()
	_manual_cursor = 0
	_selected_manual_plot = -1
	_build_result.clear()
	_same_failure_key = ""
	_same_failure_count = 0
	_hint_level = -1
	_workshop_step = 0
	_skill_tree_showing_unlocked = false
	_bug_challenge_seen = false
	_patch_pending = false
	_patch_stale = false
	_patch_rejected_at_failure = -1
	_used_ai_patch = false
	_used_hint_levels.clear()
	_draft_active = false
	_candidate_skip_requested = false
	_candidate_playing = false
	_last_candidate_result.clear()
	if _agent_mode:
		_synchronizing_agent_draft = true
		code_editor.text = _agent_source if not _agent_source.is_empty() else INITIAL_PRACTICE_CODE
		_synchronizing_agent_draft = false
	else:
		code_editor.text = INITIAL_PRACTICE_CODE
	completion_card.visible = false
	code_drawer.visible = false
	_hide_lesson_overlays()
	evidence_panel.visible = true
	for child in plot_grid.get_children():
		var card := child as CropPlotCard
		card.reset_candidate_display()
		card.show_gap(false)
		card.set_attention(false)
	_set_phase(Phase.INTRO)
	_play_intro()


static func evaluate_source(source: String) -> Dictionary:
	var compact := source.replace(" ", "").replace("\t", "").replace("\r", "")
	if (
		source.strip_edges().is_empty()
		or "__" in source
		or "/*目标*/" in source
		or "/*当前*/" in source
		or "/*边界*/" in source
		or "/*份数*/" in source
	):
		return {"build_ok": false, "failure_key": "COMPILE_INCOMPLETE", "message": "代码中还有未完成的位置。", "actions": []}
	if not "for(inti=" in compact or not "cout<<\"WATER" in compact:
		return {"build_ok": false, "failure_key": "COMPILE_STRUCTURE", "message": "编译器没有找到完整的循环或 WATER 输出。", "actions": []}
	var start := 0
	var limit := 8
	var loop_regex := RegEx.new()
	loop_regex.compile("for\\s*\\(\\s*int\\s+i\\s*=\\s*(-?\\d+)\\s*;\\s*i\\s*<\\s*(-?\\d+)")
	var loop_match := loop_regex.search(source)
	if loop_match == null:
		return {"build_ok": false, "failure_key": "COMPILE_LOOP", "message": "循环起点或终点无法识别。", "actions": []}
	start = int(loop_match.get_string(1))
	limit = int(loop_match.get_string(2))
	var failure_key := ""
	var message := "世界规则全部满足。"
	if "60-moisture[i]" in compact:
		failure_key = "FIXED_TARGET_VALUE"
		message = "胡萝卜正确，但番茄与土豆出现漏浇、少浇和多浇。"
	elif "moisture[i]-target[i]" in compact:
		failure_key = "REVERSED_GAP"
		message = "真正缺水的土地被跳过，已经达标的土地反而触发了浇水。"
	elif "target[0]-moisture[i]" in compact:
		failure_key = "FIXED_TARGET_INDEX"
		message = "所有土地都套用了0号胡萝卜的目标值。"
	elif "target[i+1]-moisture[i]" in compact:
		failure_key = "MISALIGNED_INDEX"
		message = "当前土地读到了下一块土地的目标值。"
	elif start != 0 or limit != 8:
		failure_key = "LOOP_BOUNDARY"
		message = "扫描范围没有精确覆盖0到7号土地。"
	elif "if(gap>0)" in compact and compact.find("if(gap>0)") < compact.find("if(gap>=30)"):
		failure_key = "BRANCH_ORDER"
		message = "严重缺水先命中了轻度分支，因此只得到1份水。"
	elif not "target[i]-moisture[i]" in compact:
		failure_key = "GAP_EXPRESSION"
		message = "缺口没有使用同一块土地的目标值减去当前值。"
	elif not "if(gap>=30)" in compact or not "elseif(gap>0)" in compact:
		failure_key = "BRANCH_RULE"
		message = "分级边界或 else if 结构与任务规则不一致。"
	var actions: Array[Dictionary] = []
	for index in range(maxi(0, start), mini(8, limit)):
		var gap: int = int(60 if failure_key == "FIXED_TARGET_VALUE" else TARGET[index]) - int(MOISTURE[index])
		if failure_key == "REVERSED_GAP":
			gap = MOISTURE[index] - TARGET[index]
		elif failure_key == "FIXED_TARGET_INDEX":
			gap = TARGET[0] - MOISTURE[index]
		elif failure_key == "MISALIGNED_INDEX":
			gap = TARGET[mini(index + 1, 7)] - MOISTURE[index]
		var units: int = 2 if gap >= 30 else (1 if gap > 0 else 0)
		if failure_key == "BRANCH_ORDER" and gap >= 30:
			units = 1
		actions.append({"plot_index": index, "gap": gap, "units": units})
	return {
		"build_ok": true,
		"objective_succeeded": failure_key.is_empty(),
		"failure_key": failure_key,
		"message": message,
		"actions": actions,
	}


func _play_intro() -> void:
	var profile = PROFILE_CATALOG.profile_for(&"world_agent")
	if profile != null:
		story_dialogue.play_sequence(profile.display_name, profile.portrait, INTRO_LINES)
		await story_dialogue.sequence_finished
	if _phase == Phase.INTRO:
		primary_button.disabled = false


func _on_primary_pressed() -> void:
	_bounce(primary_button)
	match _phase:
		Phase.INTRO:
			_play_old_tool_demo()
		Phase.OLD_TOOL:
			_begin_manual_compare()
		Phase.WORKSHOP:
			_begin_workshop_experiments()
		Phase.FAILED:
			_enter_code_phase()


func _play_old_tool_demo() -> void:
	_set_phase(Phase.OLD_TOOL)
	primary_button.disabled = true
	for index in range(8):
		var card := plot_grid.get_child(index) as CropPlotCard
		await card.play_scan(_duration(0.23))
		var gap: int = 60 - int(MOISTURE[index])
		var units: int = 2 if gap >= 30 else (1 if gap > 0 else 0)
		card.set_result(units, true, units != EXPECTED_UNITS[index])
		evidence_title.text = "旧工具把 60 当作所有土地的目标湿度"
		evidence_body.text = "[color=#c45622]1号番茄漏浇[/color]　[color=#c45622]5号番茄水量不足[/color]　[color=#c45622]6号土豆被多浇[/color]"
	_reveal_evidence()
	primary_button.disabled = false
	primary_button.text = "亲自比较三块土地  →"


func _begin_manual_compare() -> void:
	_set_phase(Phase.MANUAL_COMPARE)
	for child in plot_grid.get_children():
		var card := child as CropPlotCard
		card.set_result(-1, false)
		card.show_gap(true)
		card.set_attention(false)
	_selected_manual_plot = -1
	_manual_cursor = 0
	water_choices.visible = false
	_set_manual_attention(MANUAL_ORDER[0])
	evidence_title.text = "先读当前湿度和目标湿度，再决定水量"
	evidence_body.text = "[b]缺口 ≥ 30：浇 2 份　0 < 缺口 < 30：浇 1 份。缺口 ≤ 0：不浇水。[/b]\n请先点击高亮的 1 号番茄。"
	primary_button.disabled = true


func _on_plot_pressed(index: int) -> void:
	if _phase != Phase.MANUAL_COMPARE:
		return
	var expected_index: int = MANUAL_ORDER[_manual_cursor]
	if index != expected_index:
		(plot_grid.get_child(expected_index) as CropPlotCard).pulse_attention()
		return
	_selected_manual_plot = index
	water_choices.visible = true
	evidence_title.text = "%d号%s：当前湿度 %d，目标湿度 %d" % [index, CROPS[index], MOISTURE[index], TARGET[index]]
	evidence_body.text = "湿度缺口 = 目标湿度 - 当前湿度 = [b]%+d[/b]。\n[b]缺口 ≥ 30 浇 2 份；0 < 缺口 < 30 浇 1 份；缺口 ≤ 0 不浇水。[/b]" % (TARGET[index] - MOISTURE[index])


func _choose_manual_water(units: int) -> void:
	if _phase != Phase.MANUAL_COMPARE or _selected_manual_plot < 0:
		return
	var index := _selected_manual_plot
	if units != EXPECTED_UNITS[index]:
		(plot_grid.get_child(index) as CropPlotCard).set_result(units, true, true)
		evidence_body.text = "再看看：当前湿度 %d，目标湿度 %d，缺口是 %+d。\n[b]缺口 ≥ 30 浇 2 份；0 < 缺口 < 30 浇 1 份；缺口 ≤ 0 不浇水。[/b]" % [MOISTURE[index], TARGET[index], TARGET[index] - MOISTURE[index]]
		return
	var selected_card := plot_grid.get_child(index) as CropPlotCard
	selected_card.set_attention(false)
	selected_card.set_result(units, true)
	_manual_cursor += 1
	_selected_manual_plot = -1
	water_choices.visible = false
	if _manual_cursor < MANUAL_ORDER.size():
		_set_manual_attention(MANUAL_ORDER[_manual_cursor])
		evidence_body.text = "判断正确。继续检查下一块高亮土地。"
		return
	evidence_title.text = "升级委托已经触发"
	evidence_body.text = "[b]同下标读取当前值与目标值 → 计算 gap → 选择 0 / 1 / 2 份水[/b]\n新的 4★ 技能节点已经可以学习。"
	_show_skill_tree(false)


func _set_manual_attention(plot_index: int) -> void:
	for child_index in range(plot_grid.get_child_count()):
		var card := plot_grid.get_child(child_index) as CropPlotCard
		card.set_attention(child_index == plot_index)
	if plot_index >= 0:
		(plot_grid.get_child(plot_index) as CropPlotCard).pulse_attention()


func _show_skill_tree(unlocked: bool) -> void:
	_hide_lesson_overlays()
	_skill_tree_showing_unlocked = unlocked
	_set_phase(Phase.SKILL_UNLOCKED if unlocked else Phase.SKILL_TREE)
	skill_tree_badge.text = "技能树 · 4★ 节点"
	skill_tree_title.text = "作物适配浇水器"
	if unlocked:
		skill_tree_body.text = (
			"[center][color=#2a8a4f][font_size=28]★★★★  已解锁[/font_size][/color][/center]\n"
			+ "[center]读取每块土地的当前湿度与作物目标湿度，计算缺口并决定 0 / 1 / 2 份水。[/center]\n\n"
			+ "[color=#738276]★★★★★  区域灌溉 · 未来能力（尚未解锁）[/color]"
		)
		skill_tree_continue_button.text = "进入完成后的自由状态  →"
	else:
		skill_tree_body.text = (
			"[center][color=#d28a18][font_size=28]★★★★  剧情可学习[/font_size][/color][/center]\n"
			+ "[center]能力槽：数据配对　缺口计算　分级动作[/center]\n\n"
			+ "[color=#738276]★★★★★  区域灌溉 · 功能预告（不能提前学习）[/color]"
		)
		skill_tree_continue_button.text = "进入清泉工坊  →"
	skill_tree_overlay.visible = true


func _on_skill_tree_continue_pressed() -> void:
	_bounce(skill_tree_continue_button)
	skill_tree_overlay.visible = false
	if _skill_tree_showing_unlocked:
		_enter_free_play()
	else:
		_begin_workshop_experiments()


func _begin_workshop_experiments() -> void:
	_hide_lesson_overlays()
	_set_phase(Phase.WORKSHOP)
	_workshop_step = 0
	workshop_overlay.visible = true
	_show_workshop_step()
	var profile = PROFILE_CATALOG.profile_for(&"teaching_agent")
	if profile != null:
		story_dialogue.play_agent_presentation(
			profile.display_name,
			profile.portrait,
			"我们不背答案。先做三个小实验，把数组、缺口和水量在世界里一一对应起来。",
			"两个数组为什么要使用同一个 i？",
			"教学实验",
		)


func _show_workshop_step() -> void:
	var experiment: Dictionary = WORKSHOP_EXPERIMENTS[_workshop_step]
	workshop_badge.text = str(experiment.badge)
	workshop_title.text = str(experiment.title)
	workshop_body.text = str(experiment.body)
	workshop_action_button.text = str(experiment.action)
	workshop_gap_code.visible = _workshop_step == 0
	workshop_branch_code.visible = _workshop_step == 1
	workshop_summary_code.visible = _workshop_step == 2
	match _workshop_step:
		0:
			gap_target_input.text = ""
			gap_moisture_input.text = ""
			(plot_grid.get_child(1) as CropPlotCard).pulse_attention()
			gap_target_input.grab_focus()
		1:
			severe_boundary_input.text = ""
			severe_units_input.text = ""
			light_boundary_input.text = ""
			light_units_input.text = ""
			evidence_title.text = "番茄的真实数据"
			evidence_body.text = "1号番茄：当前湿度 65　目标湿度 70　缺口 +5"
			severe_boundary_input.grab_focus()
		2:
			evidence_title.text = "水量单位已经对应"
			evidence_body.text = "1份 = 250 ml　2份 = 500 ml；达到目标的土地保持喷头关闭。"


func _on_workshop_action_pressed() -> void:
	_bounce(workshop_action_button)
	if _workshop_step == 0 and (
		gap_target_input.text.strip_edges() != "target"
		or gap_moisture_input.text.strip_edges() != "moisture"
	):
		workshop_body.text = str(WORKSHOP_EXPERIMENTS[0].body) + "\n⚠ 请在两个输入框中依次填入 target 和 moisture。"
		gap_target_input.grab_focus()
		return
	if _workshop_step == 1 and not (
		severe_boundary_input.text.strip_edges() == "30"
		and severe_units_input.text.strip_edges() == "2"
		and light_boundary_input.text.strip_edges() == "0"
		and light_units_input.text.strip_edges() == "1"
	):
		workshop_body.text = str(WORKSHOP_EXPERIMENTS[1].body) + "\n⚠ 请检查四个输入框：30、2、0、1。"
		severe_boundary_input.grab_focus()
		return
	_workshop_step += 1
	if _workshop_step >= WORKSHOP_EXPERIMENTS.size():
		workshop_overlay.visible = false
		_enter_code_phase()
		return
	_show_workshop_step()


func _hide_lesson_overlays() -> void:
	if is_instance_valid(skill_tree_overlay):
		skill_tree_overlay.visible = false
	if is_instance_valid(workshop_overlay):
		workshop_overlay.visible = false
	if is_instance_valid(bug_challenge_overlay):
		bug_challenge_overlay.visible = false
	if is_instance_valid(growth_summary_overlay):
		growth_summary_overlay.visible = false


func _enter_code_phase() -> void:
	_hide_lesson_overlays()
	_set_phase(Phase.CODE)
	_show_code_drawer()


func _show_code_drawer() -> void:
	if _phase < Phase.CODE or _phase in [Phase.RUNNING, Phase.CANDIDATE_VALIDATING, Phase.CANDIDATE_PRESENTING]:
		return
	if _drawer_tween != null and _drawer_tween.is_valid():
		_drawer_tween.kill()
	code_drawer.visible = true
	code_drawer.mouse_filter = Control.MOUSE_FILTER_STOP
	code_drawer.position.x = 500.0
	code_drawer.modulate.a = 0.0
	_drawer_tween = create_tween().set_parallel(true)
	_drawer_tween.set_trans(Tween.TRANS_QUINT).set_ease(Tween.EASE_OUT)
	_drawer_tween.tween_property(code_drawer, "position:x", 0.0, _duration(0.32))
	_drawer_tween.tween_property(code_drawer, "modulate:a", 1.0, _duration(0.24))
	code_editor.grab_focus()


func _hide_code_drawer() -> void:
	if not code_drawer.visible:
		return
	if _drawer_tween != null and _drawer_tween.is_valid():
		_drawer_tween.kill()
	code_drawer.mouse_filter = Control.MOUSE_FILTER_IGNORE
	code_drawer.visible = false


func _request_run() -> void:
	if _phase not in [Phase.CODE, Phase.CERTIFIED, Phase.ACTIVE, Phase.FAILED, Phase.LOCAL_FAILED, Phase.LOCAL_COMPLETED, Phase.CHAIN_ERROR]:
		return
	_bounce(run_button)
	if _agent_mode:
		agent_submit_requested.emit(code_editor.text)
		return
	_flush_autosave()
	_build_result = evaluate_source(code_editor.text)
	if not _build_result.get("build_ok", false):
		_set_phase(Phase.CODE)
		evidence_title.text = "构建失败 · 世界没有变化"
		evidence_body.text = str(_build_result.get("message", "编译失败。"))
		_reveal_evidence()
		return
	evidence_title.text = "正在直接运行"
	evidence_body.text = "当前代码已保存，正在进行本地教学验证。"
	_set_phase(Phase.RUNNING)
	_hide_code_drawer()
	for child in plot_grid.get_children():
		(child as CropPlotCard).set_result(-1, false)
	for action: Dictionary in _build_result.get("actions", []):
		var index := int(action.plot_index)
		var card := plot_grid.get_child(index) as CropPlotCard
		await card.play_scan(_duration(0.20))
		var units := int(action.units)
		var amount_ml := units * 250
		evidence_title.text = "小核桃正在执行第 %d / 8 次循环" % (index + 1)
		evidence_body.text = (
			"i=%d　当前%d　目标%d　缺口%+d　→　%s" % [
				index,
				MOISTURE[index],
				TARGET[index],
				int(action.gap),
				"喷头关闭" if units == 0 else "WATER %d %d（%d ml）" % [index, units, amount_ml],
			]
		)
		_reveal_evidence()
		if units > 0:
			watering_can.visible = true
			watering_can.position = card.global_position + Vector2(card.size.x * 0.50, 68.0)
			watering_can.scale = Vector2.ONE * 0.22
			watering_can.frame = 0
			watering_can.play(&"pour")
			await watering_can.animation_finished
			watering_can.visible = false
		card.set_result(units, true, units != EXPECTED_UNITS[index])
		await get_tree().create_timer(_duration(0.08)).timeout
	if bool(_build_result.get("objective_succeeded", false)):
		_complete_level()
	else:
		_fail_run()


func _fail_run() -> void:
	var key := str(_build_result.get("failure_key", "UNKNOWN"))
	_same_failure_count = _same_failure_count + 1 if key == _same_failure_key else 1
	_same_failure_key = key
	_hint_level = mini(_hint_level, 3)
	_set_phase(Phase.FAILED)
	evidence_title.text = "世界事实 · 本轮未通过"
	evidence_body.text = "%s\n失败类型：[code]%s[/code]　同类失败：%d 次\n先看事实，再决定自己修改或询问叮当师傅。" % [_build_result.message, key, _same_failure_count]
	_reveal_evidence()
	if key == "FIXED_TARGET_VALUE" and _same_failure_count == 3 and not _bug_challenge_seen:
		call_deferred("_show_bug_challenge")
	_refresh_patch_button()


func _show_bug_challenge() -> void:
	_bug_challenge_seen = true
	bug_challenge_body.text = (
		"[center][font_size=21][b]公开测试：相同当前湿度，不同目标[/b][/font_size][/center]\n\n"
		+ "[table=4][cell][b]作物[/b][/cell][cell][b]当前[/b][/cell][cell][b]目标[/b][/cell][cell][b]正确动作[/b][/cell]"
		+ "[cell]土豆[/cell][cell]55[/cell][cell]50[/cell][cell]不浇水[/cell]"
		+ "[cell]番茄[/cell][cell]55[/cell][cell]70[/cell][cell]1份 · 250 ml[/cell][/table]\n\n"
		+ "相同的当前湿度 55，却需要两个不同动作。固定的 60 缺少了哪一张表？"
	)
	bug_challenge_overlay.visible = true
	var profile = PROFILE_CATALOG.profile_for(&"bug_agent")
	if profile != null:
		story_dialogue.play_agent_presentation(
			profile.display_name,
			profile.portrait,
			"这是第三次相同类型的失败。我带来了实际运行过的双作物边界测试，不会替你编造答案。",
			"土豆和番茄都为 55 时，为什么动作不同？",
			"真实反例",
		)


func _on_bug_continue_pressed() -> void:
	_bounce(bug_continue_button)
	bug_challenge_overlay.visible = false
	evidence_title.text = "Bug 先生 · 真实反例已记录"
	evidence_body.text = "同为 55：土豆目标 50，应跳过；番茄目标 70，应浇 1 份（250 ml）。你可以自己修改，或继续请求下一层提示。"
	_reveal_evidence()


func _on_hint_pressed() -> void:
	_bounce(hint_button)
	if _phase not in [Phase.CODE, Phase.FAILED, Phase.CERTIFIED, Phase.ACTIVE, Phase.LOCAL_FAILED, Phase.LOCAL_COMPLETED, Phase.CHAIN_ERROR]:
		evidence_body.text = "先完成当前观察步骤，叮当师傅会根据真实结果继续帮助你。"
		_reveal_evidence()
		return
	if _agent_mode:
		agent_hint_requested.emit("请根据我当前的代码和最近一次权威验证结果，给出下一层教学提示。")
		return
	if _same_failure_key.is_empty():
		evidence_title.text = "叮当师傅 · 目标复述"
		evidence_body.text = "两个数组要用同一个 i 找到同一块土地。现在还没有失败 Run，我不会假装某一行已经出错。"
		_reveal_evidence()
		return
	_hint_level = mini(3, _hint_level + 1)
	_used_hint_levels[_hint_level] = true
	var hints := [
		"胡萝卜土地正确，但1号番茄漏浇、5号番茄水量不足，6号土豆反而多浇。它们有什么共同点？",
		"循环、分支和输出位置都在工作。请只检查 gap 这一行里的“目标值”从哪里来。",
		"两个数组用相同下标记录同一块土地；i 变化时，当前值和目标值要一起变化。",
		"局部支架：目标数组[同一下标] - 当前数组[同一下标]。",
	]
	evidence_title.text = "叮当师傅 · L%d" % _hint_level
	evidence_body.text = hints[_hint_level]
	_reveal_evidence()
	_refresh_patch_button()


func _on_patch_requested() -> void:
	if _agent_mode:
		return
	if not _can_request_patch():
		return
	_bounce(request_patch_button)
	_patch_pending = true
	_patch_stale = false
	patch_dialog.dialog_text = "AI 建议修改（尚未应用）\n\n修改前：int gap = 60 - moisture[i];\n修改后：int gap = target[i] - moisture[i];\n\n依据：1号漏浇、5号不足、6号多浇，以及 Bug 先生的同为55公开测试。\n影响范围：只修改缺口计算这一行。\n接受后只生成新草稿，仍需由你点击“直接运行”验证。"
	patch_dialog.popup_centered()


func _can_request_patch() -> bool:
	return (
		not _agent_mode
		and
		_phase == Phase.FAILED
		and _same_failure_count >= 4
		and _same_failure_key == "FIXED_TARGET_VALUE"
		and _hint_level >= 3
		and _patch_rejected_at_failure < _same_failure_count
	)


func _refresh_patch_button() -> void:
	request_patch_button.visible = _can_request_patch()


func _accept_patch() -> void:
	if not _patch_pending or _patch_stale:
		evidence_title.text = "提案已经过期"
		evidence_body.text = "草稿基线已经变化，请基于新的失败结果重新请求 AI 修改提案。"
		_reveal_evidence()
		return
	code_editor.text = code_editor.text.replace("int gap = 60 - moisture[i];", "int gap = target[i] - moisture[i];")
	_patch_pending = false
	_used_ai_patch = true
	_draft_active = false
	_build_result.clear()
	_set_phase(Phase.CODE)
	_flush_autosave()
	evidence_title.text = "已生成新的草稿"
	evidence_body.text = "AI 修改只进入草稿，没有自动运行。请检查后点击“直接运行”。"
	_show_code_drawer()


func _on_patch_custom_action(action: StringName) -> void:
	if action != &"reject_patch":
		return
	_patch_pending = false
	_patch_rejected_at_failure = _same_failure_count
	patch_dialog.hide()
	evidence_title.text = "已拒绝 AI 修改"
	evidence_body.text = "你的草稿没有变化。拒绝不会降低星级，可以继续自己修改；下一次产生新的同类失败后仍可重新请求提案。"
	_reveal_evidence()
	_refresh_patch_button()


func _complete_level() -> void:
	_set_phase(Phase.OBJECTIVE_COMPLETE)
	completion_card.visible = true
	completion_card.modulate.a = 0.0
	completion_card.scale = Vector2(0.82, 0.82)
	completion_card.pivot_offset = completion_card.size * 0.5
	var tween := create_tween().set_parallel(true)
	tween.set_trans(Tween.TRANS_ELASTIC).set_ease(Tween.EASE_OUT)
	tween.tween_property(completion_card, "modulate:a", 1.0, _duration(0.24))
	tween.tween_property(completion_card, "scale", Vector2.ONE, _duration(0.58))
	completion_title.text = "世界目标已经客观完成！"
	completion_summary.text = "8块土地全部检查 · 5块正确浇水 · 3块正确跳过\n最后一条本地教学动作已经结束，接下来由角色根据记录总结。"
	replay_button.visible = false
	return_button.visible = false
	next_button.visible = true
	next_button.disabled = false
	next_button.text = "听芽芽和书书总结  →"
	evidence_title.text = "世界目标已完成"
	evidence_body.text = "检查土地 8/8　正确浇水 5块　正确跳过 3块\n250 ml 动作 3次　500 ml 动作 2次　水量不足 0　水量过多 0"


func _on_completion_next_pressed() -> void:
	if _phase == Phase.OBJECTIVE_COMPLETE:
		_begin_growth_summary()
		return
	next_level_requested.emit()


func _begin_growth_summary() -> void:
	_set_phase(Phase.GROWTH_SUMMARY)
	completion_card.visible = false
	var yaya = PROFILE_CATALOG.profile_for(&"world_agent")
	if yaya != null:
		story_dialogue.play_agent_presentation(
			yaya.display_name,
			yaya.portrait,
			"每一种作物都得到了适合自己的照顾。世界目标已经完成，现在请书书根据真实学习路线归档。",
			"这次规则为什么能照顾不同作物？",
			"世界反馈",
		)
		await story_dialogue.sequence_finished
	var shushu = PROFILE_CATALOG.profile_for(&"book_agent")
	if shushu != null:
		story_dialogue.play_agent_presentation(
			shushu.display_name,
			shushu.portrait,
			"我只记录已经发生的事实：代码变化、使用过的提示，以及是否接受过 AI 修改。",
			"你想把哪一步带到下一次挑战？",
			"成长总结",
		)
		await story_dialogue.sequence_finished
	_show_growth_summary()


func _show_growth_summary() -> void:
	var hint_names: Array[String] = []
	for level in range(4):
		if _used_hint_levels.has(level):
			hint_names.append("L%d" % level)
	var route := "接受 AI 局部修改后，由学生再次直接运行并验证" if _used_ai_patch else "由学生完成最终代码修改与验证"
	var assistance := "、".join(hint_names) if not hint_names.is_empty() else "未使用分层提示"
	growth_summary_body.text = (
		"[b]完成方式[/b]：%s\n\n" % route
		+ "[b]代码变化[/b]：使用同一个 i 配对 moisture[i] 与 target[i]，计算目标减当前的缺口。\n\n"
		+ "[b]提示记录[/b]：%s\n\n" % assistance
		+ "[b]验证记录[/b]：8 次循环；3 次 250 ml；2 次 500 ml；3 次跳过。"
	)
	growth_summary_overlay.visible = true


func _on_archive_pressed() -> void:
	_bounce(archive_button)
	growth_summary_overlay.visible = false
	_show_skill_tree(true)


func _enter_free_play() -> void:
	_set_phase(Phase.FREE_PLAY)
	completion_card.visible = true
	completion_card.modulate.a = 1.0
	completion_card.scale = Vector2.ONE
	completion_title.text = "4★ 作物适配浇水器已解锁"
	completion_summary.text = "本次关卡记录已经完成前端归档。\n你可以重玩本关、查看下一关预告，或返回农场。"
	replay_button.visible = true
	return_button.visible = true
	next_button.visible = true
	next_button.disabled = false
	next_button.text = "下一关预告  →"
	evidence_title.text = "完成后的自由状态"
	evidence_body.text = "已解锁：读取同下标数据、计算缺口、按 0 / 1 / 2 份执行。"
	_reveal_evidence()


func show_next_level_preview() -> void:
	completion_title.text = "下一关正在准备中"
	completion_summary.text = "未来能力：让同一套规则管理更大范围的区域灌溉。\n当前可以返回农场或重玩本关。"
	next_button.disabled = true


func _reset_code() -> void:
	code_editor.text = _clean_agent_source if _agent_mode and not _clean_agent_source.is_empty() else INITIAL_PRACTICE_CODE
	_draft_active = false
	_build_result.clear()
	_set_phase(Phase.CODE)
	_flush_autosave()


func _on_code_changed() -> void:
	if _synchronizing_agent_draft:
		return
	_draft_active = false
	if _patch_pending:
		_patch_stale = true
	if _agent_mode:
		_agent_source = code_editor.text
		save_state.text = "• 待交给小核桃"
		agent_draft_changed.emit(code_editor.text)
		if _phase in [Phase.CERTIFIED, Phase.ACTIVE, Phase.FAILED, Phase.LOCAL_FAILED, Phase.LOCAL_COMPLETED, Phase.CHAIN_ERROR]:
			_build_result.clear()
			_set_phase(Phase.CODE)
		return
	save_state.text = "• 等待自动保存"
	autosave_timer.start()
	if _phase in [Phase.CERTIFIED, Phase.ACTIVE, Phase.FAILED, Phase.LOCAL_FAILED, Phase.LOCAL_COMPLETED, Phase.CHAIN_ERROR]:
		_build_result.clear()
		_set_phase(Phase.CODE)


func _flush_autosave() -> void:
	autosave_timer.stop()
	save_state.text = "• 待交给小核桃" if _agent_mode else "✓ 草稿已保存"


func configure_agent_mode(enabled: bool) -> void:
	_agent_mode = enabled
	request_patch_button.visible = false
	if enabled:
		save_state.text = "✓ 已连接正式草稿"
		_refresh_authority_strip()


func load_authoritative_projection(content: Dictionary, snapshot: Dictionary) -> Dictionary:
	var task: Variant = content.get("task")
	if not task is Dictionary or str(task.get("name", "")).strip_edges().is_empty():
		return {"ok": false, "message": "权威 Content 缺少可展示的 task.name。"}
	if (
		str(snapshot.get("world_id", "")).strip_edges().is_empty()
		or int(snapshot.get("revision", -1)) < 0
		or str(snapshot.get("state_hash", "")).strip_edges().is_empty()
	):
		return {"ok": false, "message": "权威 Snapshot 缺少 world_id/revision/state_hash。"}
	_authoritative_content = content.duplicate(true)
	_authoritative_snapshot = snapshot.duplicate(true)
	_agent_task_title = str(task.get("name", "")).strip_edges()
	task_title.text = _agent_task_title
	_refresh_authority_strip()
	return {"ok": true}


func formal_projection_state() -> Dictionary:
	return {
		"content": _authoritative_content.duplicate(true),
		"snapshot": _authoritative_snapshot.duplicate(true),
		"interaction": _last_agent_interaction.duplicate(true),
		"source": code_editor.text,
		"phase": _phase,
	}


func open_formal_run_workspace() -> Dictionary:
	if (
		not _agent_mode
		or _authoritative_content.is_empty()
		or _authoritative_snapshot.is_empty()
		or _agent_source.is_empty()
	):
		return {"ok": false, "message": "Formal Content, Draft and Snapshot must be projected before Run becomes actionable."}
	_enter_code_phase()
	return {"ok": not run_button.disabled and code_drawer.visible}


func _refresh_authority_strip() -> void:
	if not _agent_mode or _authoritative_snapshot.is_empty():
		return
	phase_strip.text = "正式世界 %s · revision %d · state %s" % [
		str(_authoritative_snapshot.get("world_id", "")),
		int(_authoritative_snapshot.get("revision", -1)),
		str(_authoritative_snapshot.get("state_hash", "")),
	]


func configure_candidate_compatibility_available(available: bool) -> void:
	_candidate_compatibility_available = available
	playback_speed_button.disabled = not available
	playback_speed_button.tooltip_text = (
		"切换 Sandbox 候选动作演出速度"
		if available
		else "WATER 候选兼容未配置"
	)
	skip_playback_button.disabled = true
	replay_playback_button.disabled = true


func present_candidate_evaluation(result: Dictionary, replay := false) -> Dictionary:
	if (
		not _candidate_compatibility_available
		or _candidate_playing
		or not bool(result.get("ok", false))
		or str(result.get("source", "")) != "SANDBOX_ACTION_INTENT_CANDIDATE"
		or not result.get("actions") is Array
		or not result.get("plot_results") is Array
	):
		return {"ok": false, "code": "CANDIDATE_PRESENTATION_INVALID"}
	_candidate_playing = true
	_candidate_skip_requested = false
	if not replay:
		_last_candidate_result = result.duplicate(true)
	_set_phase(Phase.CANDIDATE_VALIDATING)
	completion_card.visible = false
	watering_can.stop()
	watering_can.visible = false
	for child: Node in plot_grid.get_children():
		(child as CropPlotCard).reset_candidate_display()
	evidence_title.text = "正在校验 Sandbox 候选动作"
	evidence_body.text = "本轮世界未提交。前端只在权威 Snapshot 的本地副本上演算 WATER intents。"
	_reveal_evidence()
	_set_phase(Phase.CANDIDATE_PRESENTING)
	skip_playback_button.disabled = false
	replay_playback_button.disabled = true
	for action_value: Variant in result.actions:
		if not action_value is Dictionary:
			_candidate_playing = false
			return {"ok": false, "code": "CANDIDATE_PRESENTATION_ACTION_INVALID"}
		var action: Dictionary = action_value
		var ui_index := int(action.get("ui_index", -1))
		if ui_index < 0 or ui_index >= plot_grid.get_child_count():
			_candidate_playing = false
			return {"ok": false, "code": "CANDIDATE_PRESENTATION_PLOT_INVALID"}
		var card := plot_grid.get_child(ui_index) as CropPlotCard
		if not _candidate_skip_requested:
			await card.play_scan(_duration(0.20) / _candidate_playback_speed)
			if not is_instance_valid(self):
				return {"ok": false, "code": "CANDIDATE_PRESENTATION_CANCELLED"}
			watering_can.visible = true
			watering_can.position = card.global_position + Vector2(card.size.x * 0.50, 68.0)
			watering_can.scale = Vector2.ONE * 0.22
			watering_can.frame = 0
			watering_can.speed_scale = _candidate_playback_speed / maxf(timing_scale, 0.05)
			watering_can.play(&"pour")
			await watering_can.animation_finished
			if not is_instance_valid(self):
				return {"ok": false, "code": "CANDIDATE_PRESENTATION_CANCELLED"}
			watering_can.visible = false
		card.show_candidate_action(
			int(action.get("amount_ml", 0)),
			int(action.get("hydration_after", 0)),
			not _candidate_skip_requested,
		)
		if not _candidate_skip_requested:
			await get_tree().create_timer(_duration(0.08) / _candidate_playback_speed).timeout
			if not is_instance_valid(self):
				return {"ok": false, "code": "CANDIDATE_PRESENTATION_CANCELLED"}
	watering_can.stop()
	watering_can.visible = false
	for plot_value: Variant in result.plot_results:
		if plot_value is Dictionary:
			var plot_result: Dictionary = plot_value
			var ui_index := int(plot_result.get("ui_index", -1))
			if ui_index >= 0 and ui_index < plot_grid.get_child_count():
				(plot_grid.get_child(ui_index) as CropPlotCard).show_candidate_outcome(
					int(plot_result.get("hydration", 0)),
					str(plot_result.get("status", "UNKNOWN")),
				)
	_candidate_playing = false
	skip_playback_button.disabled = true
	replay_playback_button.disabled = false
	if bool(result.get("objective_succeeded", false)):
		_set_phase(Phase.LOCAL_COMPLETED)
		completion_card.visible = true
		completion_card.modulate.a = 1.0
		completion_card.scale = Vector2.ONE
		evidence_title.text = "本地候选结果 · 满足本关目标"
	else:
		_set_phase(Phase.LOCAL_FAILED)
		evidence_title.text = "本地候选结果 · 仍需修改"
	evidence_body.text = "%s\n[color=#c45622]权威后端仍为 TASK_INCOMPLETE，世界未提交；该结果不会写入 ClientStore。[/color]" % str(result.get("summary", "候选判题已完成。"))
	_reveal_evidence()
	return {"ok": true, "skipped": _candidate_skip_requested}


func present_candidate_chain_error(code: String) -> void:
	_agent_stage_message_visible = false
	_candidate_playing = false
	_candidate_skip_requested = false
	_set_phase(Phase.CHAIN_ERROR)
	evidence_title.text = "无法安全生成候选结果"
	evidence_body.text = "[%s] 本轮保持后端失败结论，世界未提交。" % code
	_reveal_evidence()


func _on_playback_speed_pressed() -> void:
	if not _candidate_compatibility_available:
		return
	_candidate_playback_speed = 2.0 if is_equal_approx(_candidate_playback_speed, 1.0) else 1.0
	playback_speed_button.text = "%dx" % int(_candidate_playback_speed)


func _on_skip_playback_pressed() -> void:
	if _candidate_playing:
		_candidate_skip_requested = true
		watering_can.speed_scale = 1000.0


func _on_replay_playback_pressed() -> void:
	if _candidate_playing or _last_candidate_result.is_empty():
		return
	present_candidate_evaluation(_last_candidate_result.duplicate(true), true)


func load_agent_draft(source: String) -> void:
	if not _agent_mode or source.is_empty():
		return
	_agent_source = source
	if code_editor.text == source:
		return
	_synchronizing_agent_draft = true
	code_editor.text = source
	_synchronizing_agent_draft = false


func update_agent_draft_state(state: int) -> void:
	if not _agent_mode:
		return
	if state == WalnutClientStore.DraftState.CLEAN:
		_clean_agent_source = _agent_source
	save_state.text = {
		WalnutClientStore.DraftState.CLEAN: "✓ 正式草稿已同步",
		WalnutClientStore.DraftState.DIRTY: "• 待交给小核桃",
		WalnutClientStore.DraftState.SAVING: "↻ 正在保存正式草稿",
		WalnutClientStore.DraftState.CONFLICT: "! 草稿冲突",
		WalnutClientStore.DraftState.SAVE_FAILED: "! 草稿保存失败",
	}.get(state, "• 等待正式草稿")


func begin_agent_submission(message: String) -> void:
	_agent_stage_message_visible = true
	_set_phase(Phase.RUNNING)
	_hide_code_drawer()
	watering_can.stop()
	watering_can.visible = false
	completion_card.visible = false
	evidence_title.text = "正在交给小核桃"
	evidence_body.text = message
	_reveal_evidence()


func begin_agent_build() -> void:
	_set_phase(Phase.BUILDING)
	evidence_title.text = "正在创建正式构建"
	evidence_body.text = "先保存当前草稿，再等待 Build、课程测试与 Certification。"
	_reveal_evidence()


func complete_agent_build() -> void:
	_draft_active = false
	_set_phase(Phase.CERTIFIED)
	evidence_title.text = "正式构建已认证"
	evidence_body.text = "已取得不可变 SkillVersion 与 Certification，可以继续激活。"
	_reveal_evidence()


func begin_agent_activation() -> void:
	_set_phase(Phase.ACTIVATING)
	evidence_title.text = "正在激活技能版本"
	evidence_body.text = "正在使用后端给出的 registry revision 与 activation scope。"
	_reveal_evidence()


func complete_agent_activation() -> void:
	_draft_active = true
	_set_phase(Phase.ACTIVE)
	evidence_title.text = "正式激活完成"
	evidence_body.text = "精确 Skill tuple 已发布，可以运行或继续编辑。"
	_reveal_evidence()


func update_agent_submission_stage(message: String, keep_running := true) -> void:
	_agent_stage_message_visible = true
	if keep_running and _phase != Phase.RUNNING:
		_set_phase(Phase.RUNNING)
	evidence_title.text = "正式 Agent 链路"
	evidence_body.text = message
	_reveal_evidence()


func present_agent_interactions(interactions: Array[Dictionary]) -> void:
	if interactions.is_empty():
		return
	var latest: Dictionary = interactions.back()
	var feedback: Variant = latest.get("feedback")
	if not feedback is Dictionary:
		return
	var message := str(feedback.get("message", ""))
	if message.is_empty():
		return
	_agent_stage_message_visible = false
	_last_agent_interaction = latest.duplicate(true)
	var role_id := StringName(str(latest.get("role", "")))
	var profile = PROFILE_CATALOG.profile_for(role_id)
	var display_name: String = str(profile.display_name) if profile != null else "系统"
	var response_type := str(latest.get("response_type", "message"))
	var hint_level_value: Variant = latest.get("hint_level")
	var hint_level := int(hint_level_value) if typeof(hint_level_value) == TYPE_INT else 0
	evidence_title.text = "%s · %s" % [display_name, "L%d" % hint_level if response_type == "hint" else "Agent 反馈"]
	evidence_body.text = message
	_reveal_evidence()
	if profile != null:
		var question_value: Variant = latest.get("question")
		story_dialogue.play_agent_presentation(
			profile.display_name,
			profile.portrait,
			message,
			"" if question_value == null else str(question_value),
			"教学提示" if response_type in ["hint", "question"] else "验证反馈",
		)


## Agent/Gateway failures must never show the student a raw backend message, and
## must never overwrite the level instructions that are currently on screen.
## Staying completely silent is not the same guarantee though: when the panel is
## mid-flight it is showing a transient "正在……" progress line, and swallowing the
## failure freezes that line forever while Hint/Run/Code stay hidden or disabled.
## So only a transient phase is replaced, and only with fixed child-facing copy.
func present_agent_error(message: String) -> void:
	_last_chain_error_detail = message
	var frozen_transient_phase: bool = _phase in [Phase.RUNNING, Phase.BUILDING, Phase.ACTIVATING]
	if not frozen_transient_phase and not _agent_stage_message_visible:
		return
	_candidate_playing = false
	_candidate_skip_requested = false
	_agent_stage_message_visible = false
	if frozen_transient_phase:
		_set_phase(Phase.CHAIN_ERROR)
	evidence_title.text = "先停一下 · 这次没有连上"
	evidence_body.text = "刚才那一步没有走通，世界没有变化，你的代码也还在。\n可以再点一次“直接运行”，或者先问问叮当师傅。"
	_reveal_evidence()


func fail_agent_submission(_stage: String, _message: String) -> void:
	_agent_stage_message_visible = false
	_set_phase(Phase.FAILED)
	evidence_title.text = "继续完善规则"
	evidence_body.text = "可以继续修改代码并重新验证。"
	_reveal_evidence()


func complete_agent_submission(summary: String) -> void:
	_agent_stage_message_visible = false
	_set_phase(Phase.COMPLETED)
	completion_card.visible = true
	completion_card.modulate.a = 1.0
	completion_card.scale = Vector2.ONE
	evidence_title.text = "正式 Agent 验证已闭环"
	var feedback: Variant = _last_agent_interaction.get("feedback")
	var feedback_message := str(feedback.get("message", "")) if feedback is Dictionary else ""
	evidence_body.text = "%s%s\n%s\n[color=#8b5a2b]结果来自正式 Run、Receipt 与最终权威 Snapshot。[/color]" % [
		summary,
		"\n%s" % feedback_message if not feedback_message.is_empty() and feedback_message != summary else "",
		_authoritative_snapshot_line(),
	]
	_reveal_evidence()


func _authoritative_snapshot_line() -> String:
	if _authoritative_snapshot.is_empty():
		return "Snapshot authority unavailable"
	return "World %s · revision %d · state_hash %s" % [
		str(_authoritative_snapshot.get("world_id", "")),
		int(_authoritative_snapshot.get("revision", -1)),
		str(_authoritative_snapshot.get("state_hash", "")),
	]


func _set_phase(value: Phase) -> void:
	_phase = value
	task_title.text = _agent_task_title if _agent_mode and not _agent_task_title.is_empty() else "作物适配浇水器"
	var names := {
		Phase.INTRO: "进入试验田",
		Phase.OLD_TOOL: "观察旧工具",
		Phase.MANUAL_COMPARE: "手动比较",
		Phase.SKILL_TREE: "技能树升级委托",
		Phase.WORKSHOP: "叮当实验",
		Phase.CODE: "编写规则",
		Phase.CERTIFIED: "准备交付",
		Phase.ACTIVE: "准备验证",
		Phase.RUNNING: "世界验证",
		Phase.FAILED: "分层教学",
		Phase.OBJECTIVE_COMPLETE: "世界目标完成",
		Phase.GROWTH_SUMMARY: "书书成长总结",
		Phase.SKILL_UNLOCKED: "4★技能解锁",
		Phase.FREE_PLAY: "完成后自由状态",
		Phase.COMPLETED: "完成归档",
		Phase.CANDIDATE_VALIDATING: "候选校验",
		Phase.CANDIDATE_PRESENTING: "候选演出",
		Phase.LOCAL_FAILED: "候选未通过",
		Phase.LOCAL_COMPLETED: "候选完成",
		Phase.CHAIN_ERROR: "链路异常",
		Phase.BUILDING: "正式构建",
		Phase.ACTIVATING: "正式激活",
	}
	task_progress.text = str(names.get(value, "进行中"))
	phase_strip.text = "观察旧工具  →  叮当实验  →  编写规则  →  世界验证  →  成长归档"
	_refresh_authority_strip()
	primary_button.visible = value in [Phase.INTRO, Phase.OLD_TOOL, Phase.FAILED]
	hint_button.visible = value in [Phase.CODE, Phase.CERTIFIED, Phase.ACTIVE, Phase.FAILED, Phase.LOCAL_FAILED, Phase.LOCAL_COMPLETED, Phase.CHAIN_ERROR]
	code_button.visible = value in [Phase.CODE, Phase.CERTIFIED, Phase.ACTIVE, Phase.FAILED, Phase.LOCAL_FAILED, Phase.LOCAL_COMPLETED, Phase.CHAIN_ERROR]
	_refresh_patch_button()
	run_button.disabled = value not in [Phase.CODE, Phase.CERTIFIED, Phase.ACTIVE, Phase.FAILED, Phase.LOCAL_FAILED, Phase.LOCAL_COMPLETED, Phase.CHAIN_ERROR]
	match value:
		Phase.INTRO:
			primary_button.text = "查看旧工具演示  →"
		Phase.FAILED:
			primary_button.text = "我自己修改  →"


func _reveal_evidence() -> void:
	if _evidence_tween != null and _evidence_tween.is_valid():
		_evidence_tween.kill()
	evidence_panel.visible = true
	evidence_panel.modulate.a = 0.0
	evidence_panel.position.y += 8.0
	_evidence_tween = create_tween().set_parallel(true)
	_evidence_tween.set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_OUT)
	_evidence_tween.tween_property(evidence_panel, "modulate:a", 1.0, _duration(0.18))
	_evidence_tween.tween_property(evidence_panel, "position:y", evidence_panel.position.y - 8.0, _duration(0.18))


func _bounce(control: Control) -> void:
	var id := control.get_instance_id()
	var active := _button_tweens.get(id) as Tween
	if active != null and active.is_valid():
		active.kill()
	control.pivot_offset = control.size * 0.5
	var tween := create_tween()
	_button_tweens[id] = tween
	tween.set_trans(Tween.TRANS_BACK).set_ease(Tween.EASE_OUT)
	tween.tween_property(control, "scale", Vector2(0.94, 0.94), _duration(0.06))
	tween.tween_property(control, "scale", Vector2.ONE, _duration(0.15))


func _duration(seconds: float) -> float:
	return maxf(0.01, seconds * timing_scale)
