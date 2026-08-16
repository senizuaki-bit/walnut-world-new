extends PanelContainer

@onready var store: WalnutClientStore = get_node_or_null("/root/ClientStore") as WalnutClientStore

func _ready() -> void:
	if store != null:
		store.flow_changed.connect(_on_flow_changed)
		store.draft_changed.connect(_on_draft_changed)
		_refresh()

func _on_flow_changed(flow: int) -> void:
	_refresh()


func _on_draft_changed(_source: String, _state: int) -> void:
	_refresh()


func _refresh() -> void:
	if store == null:
		return
	var busy := store.flow_state in [WalnutClientStore.FlowState.BUILDING, WalnutClientStore.FlowState.ACTIVATING, WalnutClientStore.FlowState.TURN_RUNNING, WalnutClientStore.FlowState.PLAYING]
	var authority_ready := store.flow_state in [
		WalnutClientStore.FlowState.READY,
		WalnutClientStore.FlowState.BUILD_FAILED,
		WalnutClientStore.FlowState.CERTIFIED,
		WalnutClientStore.FlowState.ACTIVE,
		WalnutClientStore.FlowState.COMPLETED,
	]
	%ResetButton.disabled = busy
	%BuildButton.disabled = busy or not authority_ready
	%ActivationButton.disabled = busy or store.flow_state != WalnutClientStore.FlowState.CERTIFIED
	%SubmitButton.disabled = busy or not authority_ready or store.active_skill_tuple.is_empty()
