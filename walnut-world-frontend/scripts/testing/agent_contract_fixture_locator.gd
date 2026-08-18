class_name AgentContractFixtureLocator
extends RefCounted

## Keeps contract-backed tests portable across the historical sibling layout
## and the combined workspace layout used by the end-to-end harness.

const AGENT_ROOT_ENVIRONMENT_VARIABLE := "YAYA_AGENT_REPOSITORY_ROOT"


static func examples_root() -> String:
	var frontend_root := ProjectSettings.globalize_path("res://").simplify_path()
	var configured := OS.get_environment(AGENT_ROOT_ENVIRONMENT_VARIABLE).strip_edges()
	var agent_root := configured if not configured.is_empty() else "../agent"
	if not agent_root.is_absolute_path():
		agent_root = frontend_root.path_join(agent_root)
	return agent_root.simplify_path().path_join("contracts/examples")
