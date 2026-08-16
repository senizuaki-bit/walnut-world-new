# INT2 Agent M2 focused red diagnostic

Date: 2026-08-14 (Asia/Shanghai)

Evidence class: deterministic local pre-implementation red test. This is not
real Provider evidence.

Command:

```powershell
Set-Location C:\Users\HP\Desktop\核桃编程\agent
.\.venv\Scripts\python.exe -m unittest tests.test_agent_runtime_skill_patch_int2 -v
```

Observed result: exit 1; the focused suite could not import
`DraftAuthority` from `yaya_agent_runtime`.

Minimal diagnosis: the current Agent runtime has no typed current-Draft
authority/read port and therefore cannot close an explicit Patch proposal to
the exact immutable Draft revision/hash/entrypoint used by the failed
Build/Run. Existing model output still treats Patch authority fields as model
output and the deterministic policy hard-disables Patch. The red test is
`tests/test_agent_runtime_skill_patch_int2.py`.

## Post-M1 independent hardening red

After Milestone 1 formal PASS, the approved M2 hardening tests were run with:

```powershell
.\.venv\Scripts\python.exe -m unittest `
  tests.test_agent_runtime_skill_patch_int2 `
  tests.test_agent_non_live_runner -v
```

Observed result: exit 1, 16 tests run, 7 failures and 1 error. The focused
failures proved that the default registry still exposed the legacy partial
`propose_skill_patch` tool; three source paths rejected by the frozen public
Draft contract were accepted; a matching-ID teacher actor was accepted; the
proposal authority/hash omitted tenant/actor/turn/command; the live generation
budget was only checked after dispatch; and the package Python entry point
still used raw discovery. This run did not start Docker or a Provider.
## 2026-08-14 request/failure authority split red

- Command: `.venv\\Scripts\\python.exe -m unittest tests.test_agent_runtime_skill_patch_int2.SkillPatchContextAndRuntimeTests.test_request_turn_is_distinct_from_selected_failed_run`
- Result: **RED**, 1 test / 1 error.
- Minimal diagnosis: `ContextBuilder._validate_run_identity` required the selected failed Run's `turn_id` and `command_id` to equal the new `skill_patch_requested` UI-action Turn/Command. That collapses two distinct authorities and rejects the valid explicit-request flow before proposal generation.
- Required invariant: the request Turn/Command identifies the new Provider dispatch; `requested_interaction_id` resolves through a read-only interaction boundary to the selected current failed Run, whose prior Turn/Command/Build/Run/Evidence remain distinct and immutable.

### Green closure

- Added read-only `InteractionReadPort.get_current_failed_interaction` and a typed `FailedInteractionSnapshot` retaining the validated Product Interaction revision, feedback event identity and projection receipt identity.
- Split the internal authority into `SkillPatchRequestAuthority` (new authenticated student UI-action Turn/Command and selection) and `SkillPatchFailureAuthority` (selected current failed interaction's prior Turn/Command/Build/Run/Evidence/task/skill/world/failure closure).
- The stable proposal digest binds both scopes plus exact Draft and one full-entrypoint UPSERT. No model-controlled ID/path/hash enters either authority.
- Focused runtime/context/regression/non-live-budget set: **80/80 PASS**; it now also proves pre-Provider ineligible rejection, degraded reply zero-publication, and latest same-failure Interaction selection. Recoverable relay identity/capability/corruption focused set: **8/8 PASS**, including `generation_count <= 1` closure.
- Node contract/full offline with `YAYA_PYTHON_EXE=.venv\\Scripts\\python.exe`: **175/175 PASS**. The preceding 173/175 run was environment-only (`ModuleNotFoundError: jsonschema` from system Python), not a contract failure.
- Ruff + format, Pyright (explicit `.venv` interpreter), TypeScript, `compileall`, and `git diff --check`: PASS.
- Frozen v0.6 manifest remained exactly 147 files / 27,848 bytes / SHA-256 `11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05`; no public wire bytes changed.
