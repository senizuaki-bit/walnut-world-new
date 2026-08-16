# INT2 正式 Gateway / Godot M2 E2E 设计基线

状态：**DESIGN / NOT PASS**
日期：2026-08-14
范围：只读代码审计后形成的最小实施设计；本文不是当前实现、测试结果或验收证据。
禁止解读：本文不得被引用为 Backend full gate、三仓 deterministic E2E、正式 Gateway/Godot 跨进程 E2E 或真实 Provider E2E 已通过。

## 1. 要证明的唯一正式学生链

主链必须在一个新 Session 中严格按下列顺序发生：

1. Backend 创建 starter Draft revision 1 / Workspace revision 1。
2. 学生在正式编辑器中做一次确定性错误修改并保存；唯一 Draft `PUT` 创建 student revision 2。
3. 学生通过正式 Build 按钮构建 revision 2，再通过正式 Activate 按钮激活失败版本。
4. 学生通过正式 Submit/Run 按钮连续运行四次同一失败。四次 Run 都在 Sandbox 成功地产生七个 `HARVEST` intent，但被权威 World 以 `TASK_INCOMPLETE` 拒绝；World 无 commit、无展示事件、Snapshot 不变。
5. 第四次失败后，且仅在前后端双 flag/capability 闭合时，正式 `RequestAiPatchButton` 可见。学生按下它，请求一个 teaching-agent、hint-level-4、Evidence-bound、无 Run 的 Patch proposal。
6. 正式 `CodePatchDialog` 展示完整 BEFORE/AFTER 代码、一次 `UPSERT_FILE main.cpp`、Draft CAS、Evidence 与所有内容 hash。此时 revision 2 的 Draft、Workspace Draft ref、Build、Activation、Run、World 和 Learner 均不得因预览而改变。
7. 学生按正式对话框“接受修改”。ACCEPT 只创建 revision 3，更新 Workspace Draft ref，并使旧 Activation 失效；不得 Build、Activate 或 Run。
8. 学生分别再次按正式 Build、Activate、Submit/Run 按钮。成功 Run 必须产生八个按序 `HARVEST` 权威展示事件，经 `TURN_RUNNING -> PLAYING -> COMPLETED` 播放。
9. Learner 对第五个 Run 投影 `used_skill_patch=true` / `assistance_authority=SKILL_PATCH`，不得把成功晋级为独立掌握。
10. 停止 Gateway、workflow worker、Learner worker 和 Godot；以新进程 identity 重启后，Godot 只允许 GET 恢复，所有权威与副作用指纹逐字节不变。

REJECT 不能与主链共用 proposal：`product_skill_patch_decisions.patch_id` 是唯一终态（Backend `src/walnut_backend/adapters/postgres/models.py:693-740`）。REJECT 零业务副作用应由 fresh PostgreSQL focused test 或独立 deterministic Session 证明；真实 Provider 主链只运行一次 ACCEPT，不为 REJECT 再产生计费 proposal。

## 2. 当前正式路径与最小代码变更

### 2.1 Frontend runner：只扩展开关和精确写审计

现有 runner 只有 `-EnableWorldPresentation`（Frontend `scripts/run-real-gateway-e2e.ps1:8`），仅保存/设置 `YAYA_REAL_GATEWAY_E2E_ENABLE_WORLD_PRESENTATION`（同文件 `:75-103`），phase-1 仍固定 INT1 的 9 POST / 2 PUT（同文件 `:245-271`）。最小变更：

- 增加 `-EnableSkillPatch`；若开启 Patch 却未开启 World，立即 fail closed。
- 保存、设置并在 `finally` 恢复 `YAYA_REAL_GATEWAY_E2E_ENABLE_SKILL_PATCH`，与 World 环境变量采取相同生命周期。
- phase 1 精确 mutation audit 改为：
  - HTTP methods：`POST=12, PUT=1, PATCH=0, DELETE=0`；
  - `create_agent_session=1`；
  - `upsert_product_skill_draft=1`；
  - `submit_skill_build=2`；
  - `activate_skill_version=2`；
  - `submit_agent_turn=6`；
  - `record_product_patch_decision=1`。
- recovery-only 继续逐项要求 `GET == total_started == total_completed` 且 POST/PUT/PATCH/DELETE 全为 0；现有断言位于同文件 `:165-181`，不能降级为“只看最终 Snapshot”。

计数闭合：POST = Session 1 + Build 2 + Activate 2 + Turn 6 + PatchDecision 1 = 12；唯一 PUT 是学生保存 failure Draft。Patch ACCEPT 是 POST，不是第二次 Draft PUT。

### 2.2 正式 Godot 测试：所有 mutation 都从 TaskWorkspace 控件发出

当前正式测试已通过实际 `BuildButton`、`ActivationButton`、`SubmitButton` 的 `pressed.emit()` 触发动作（Frontend `tests/client/real_gateway_chain_e2e_test.gd:715-747`）；这条边界必须保留。当前错误代码保存位于 `:139-155`，失败 Build/Activate 位于 `:176-205`，三失败循环位于 `:208-247`；当前 `:249-285` 直接手工写 corrected Draft，必须整体替换为正式 Patch UI 路径。

最小可等待 UI seam 只加两个公开 signal 到 `scenes/task/task_workspace.gd`，不得加测试专用 mutation 方法：

```gdscript
signal patch_request_action_finished(result: Dictionary)
signal patch_decision_action_finished(decision: String, result: Dictionary)
```

- `_on_ai_patch_requested()`（当前 `:505-516`）在每个同步拒绝、已有 proposal 重开和异步 request 终态都发 `patch_request_action_finished`。
- `_accept_patch()`（当前 `:624-641`）发 `patch_decision_action_finished("ACCEPT", result)`。
- `_reject_patch()`（当前 `:644-661`）发 `patch_decision_action_finished("REJECT", result)`。
- 测试连接这些 signal 后，只允许触发：
  - `TaskWorkspace/Hud/SafeArea/EdgeLayer/ToolRail/RequestAiPatchButton.pressed.emit()`；
  - `CodePatchDialog.get_ok_button().pressed.emit()`；
  - REJECT 独立用例触发 `reject_patch_button.pressed.emit()` 或等价正式 `custom_action` 控件路径；
  - Build/Activate/Submit 继续使用现有 `_press_task_workspace_action`。
- 正式 E2E 禁止调用 `SessionController.request_ai_patch()`、`decide_patch()`、`TaskWorkspace._accept_patch()`、`_reject_patch()`，禁止通过 Product Gateway 直接发 mutation。Gateway GET 可用于独立核验权威。

TaskWorkspace 已把实际 Patch 按钮接到 handler（`scenes/task/task_workspace.gd:90`），正式 dialog 及明确 Accept/Reject wiring 位于 `:116-129`，预览格式位于 `:664-694`。新增 signal 只使现有学生动作可等待，不改变生产所有权。

### 2.3 双 flag/capability

AppRoot 已有本地 `world_presentation_enabled` 与 `skill_patch_enabled`（Frontend `scenes/app/app_root.gd:17-18`）；只有两个本地 flag 均 true 且 Backend capability 同时返回 true，UI 才被启用（同文件 `:342-369`）。Backend capability 的闭合约束为 explicit UI、selected failed Interaction、teaching agent、rectification、hint 4、一次 current-entrypoint UPSERT、Evidence/CAS/confirmation、无自动 Build/Activate/Run（Backend `src/walnut_backend/api/routes/product_capabilities.py:16-54`）。

因此 phase 1 与 recovery test 都必须在实例化 AppRoot 前设置两个属性。当前 phase 1 只设置 World（Frontend `tests/client/real_gateway_chain_e2e_test.gd:78-80`），recovery 也只设置 World（`tests/client/real_gateway_chain_recovery_e2e_test.gd:134-136`）。runner 必须同时传两个环境 flag。任何一侧 false 时，断言 Patch 按钮不可见、disabled、无 PatchDecision route 可用；不能偷偷打开 controller legacy 开关。

## 3. 主链精确断言

### 3.1 四次同失败

把失败角色序列从当前三次的 `teaching_agent, teaching_agent, bug_agent`（Frontend `tests/client/real_gateway_chain_e2e_test.gd:32`）改为：

```text
teaching_agent, teaching_agent, bug_agent, bug_agent
```

Agent 的冻结 router 在 failure_count >= 3 时路由 bug agent（Agent `python/yaya_agent_runtime/router.py:26-32`），而 Patch request 永远路由 teaching agent（同文件 `:40-45`）。四次失败逐次断言：

- failure counts 精确 `[1,2,3,4]`，failure key、Build、Draft revision/hash、entrypoint、active tuple 全相同；
- 四个 Sandbox result 均 SUCCEEDED 且各恰好七个 intent；
- 四个 Run/Command 均 REJECTED，World failure 精确 `WORLD_RULE_REJECTED/TASK_INCOMPLETE`；
- 每个失败 Run 恰好一个 `SKILL_RUN` Evidence，且 feedback 与 Run 引用同一 Evidence；
- 每次 World revision、last sequence、state hash、Snapshot 字节都保持初值，Events GET 空；
- 第四次失败前 Patch button 不可用，第四次终态且 interaction 已显示后才可用。

现有失败 Run 的权威核验已经覆盖七 intent、World 无 receipt、相同 Evidence 与第二次 GET 不漂移（Frontend `tests/client/real_gateway_chain_e2e_test.gd:1286-1437`）；扩展为四次并保持这些断言，不得仅增循环次数。

### 3.2 Proposal 与精确预览

按 Request 按钮前保存以下 before fingerprint：Draft resource/source bytes、Workspace、Build/Artifact/Certification、active tuple/registry、Run/Evidence、World Snapshot/events/presentation cursor、Learner profile/jobs、Sandbox/Artifact 文件指纹，以及 provider/sandbox/world/learner 计数。

请求终态必须断言：

- 新 Interaction sequence=5、revision=1、role=`teaching_agent`、response_type=`skill_patch`、hint_level=4；
- feedback `source=provider`, `degraded=false`, `fallback_reason=null`, `run_id=null`；Backend 明确投影 `run_id=None`（`src/walnut_backend/workers/turn_projection.py:1361-1373`）；
- Command terminal APPLIED，但 `result_type=NO_EFFECT`, `reason_code=SKILL_PATCH_PROPOSED`（同文件 `:1544-1555`）；
- proposal 的 base Draft 是 revision 2 / exact hash；one operation exactly `UPSERT_FILE main.cpp`；previous hash 等于失败 source；content hash 等于 AFTER bytes；result Draft hash 可由相同 canonical Draft 算法重建；
- proposal 引用第四次失败的 interaction revision/sequence、Build、Run、failure_count=4、failure key、一个 Evidence id/type/hash/uri；
- dialog 文本精确包含 `AI CODE PATCH (NOT APPLIED)`、rationale、base revision/hash、result hash、operation/path、BEFORE/AFTER、两个内容 hash 和 Evidence（Frontend `scenes/task/task_workspace.gd:664-694`）；
- proposal/preview 后 Draft 仍 revision 2 且 bytes/hash 不变；无新的 Build、Artifact、Certification、Activation、Run、World commit/event、Learner job/update 或 Sandbox receipt。允许且必须存在的 proposal 副作用只有第 5 个 Turn/Command/Interaction、Patch request/proposal/evidence linkage、Provider receipt 和 Workspace interaction high-watermark；Backend proposal 将 Command 收为 NO_EFFECT 并刷新 Workspace（`turn_projection.py:1544-1575`），所以不能错误要求 Workspace 整体字节不变，但必须要求 Workspace Draft ref、World checkpoint、Session authority不变。

### 3.3 ACCEPT 只生成 revision 3

学生按 dialog Accept 后断言：

- PatchDecision stable idempotency key/decision id 唯一；相同 key 同 payload replay 返回完全相同 receipt，不增加任何 row；同 key 不同 payload fail closed；
- receipt `decision=ACCEPT`, `draft_updated=true`, base revision 2, accepted revision 3，accepted Draft hash 等于 proposal result hash；
- Draft head revision 3、`last_applied_patch_id == proposal.patch_id`、entrypoint source 等于预览 AFTER、source hash/content hash 全闭合；Draft projection字段见 Backend `src/walnut_backend/adapters/postgres/product_drafts.py:361-393`；
- append-only revisions 精确 3 行：rev1 STUDENT、rev2 STUDENT、rev3 SKILL_PATCH；rev3 parent 指向 rev2，patch_id 精确；约束位于 `models.py:456-520`；
- `product_draft_revision_assistance` 精确一行，`inherited=false`、origin accepted row=rev3、patch/decision ids 精确；ACCEPT 写入与 Workspace 同事务（Backend `product_interactions.py:433-514`）；
- Workspace Draft ref 改为 revision 3 / exact hash；其它业务 authority 只允许 interaction revision 从 1 变 2 并出现 PatchDecision；
- ClientStore editor 显示 revision 3 的 canonical source，draft CLEAN，旧 active tuple 被清空且 accepted-Draft invalidation 持久化；现有实现位于 Frontend `autoload/client_store.gd:335-366`；
- 没有第三个 Build、Certification、Activation 或第六个 Run，且 Provider、Sandbox、World、Learner 计数不增加。flow 必须回 READY，Build/Activate/Submit 仍需学生后续点击。

### 3.4 手动 Build / Activate / Run 与 World 演出

ACCEPT 后依次触发正式 BuildButton、ActivationButton、SubmitButton。断言：

- 第二个 Build 绑定 revision 3、Draft hash、patch id、decision id、`assistance_authority=SKILL_PATCH`；第一个 Build 绑定 revision 2 且 NONE。
- 第二个 Artifact/Certification/Activation 通过稳定外键/hash闭合到这个 Build；registry revision 精确 2。
- 第五个 Run 绑定第二个 Activation 与 revision 3 Build provenance，Sandbox 恰好八个 `HARVEST`，World commit 恰好一次。
- Interaction sequence=6、role=`book_agent`、feedback provider/non-degraded；六个角色完整序列为 `teaching_agent,teaching_agent,bug_agent,bug_agent,teaching_agent,book_agent`。
- Backend presentation stream=1、events=8、commit count=1、last sequence=8、gap count=0；Godot 正式 player playback_started=1、finished=1、observed PLAYING=true，started/finished event ids 完全相同且等于 DB sequence order。现有八事件正式断言位于 Frontend `real_gateway_chain_e2e_test.gd:369-373`，需保留。
- 最终 Snapshot revision/last sequence/state hash 与 Run receipt、Events terminal relation 和 Godot renderer 完全相同。

## 4. deterministic relay 的可编译 Patch candidate

### 4.1 当前缺口

现有 INT1 relay `_closed_output` 只按 schema 递归填充（Backend `scripts/int1_recoverable_relay.py:400-444`）；未知 string 默认成 `"fixture"`（同文件 `:560-588`）。Patch schema要求 `replacement_content`（Agent `python/yaya_agent_runtime/model_output.py:192-207`），因此当前 fixture 会产出 schema-valid 但不可编译、无法完成八 HARVEST 的 `"fixture"`。这不是 M2 deterministic PASS。

### 4.2 最小安全设计

扩展 shared relay 时不得识别 dispatch ordinal、prompt prose 或固定模型正文；应从已闭合 completion 中解析 provider-safe `turn_context`：

1. 遍历 user JSON message，唯一识别同时满足：
   - `turn_context.event.event_type == "skill_patch_requested"`；
   - `turn_context.event.failure_count == 4`；
   - `turn_context.hint_level == 4`；
   - `turn_context.teaching_directive.patch_eligible == true`；
   - allowed response types 只允许 `skill_patch`；
   - `turn_context.skill_patch_request` 精确为 explicit request / UPSERT / CURRENT_ENTRYPOINT / confirmation true / three auto flags false；
   - `turn_context.skill.entrypoint == "main.cpp"`，并存在唯一 `source_code`。
2. 对 current failure source 做闭合变换：
   - 必须恰好包含一次 `#include <cstdlib>\n`；
   - 必须恰好包含一次由现有 Frontend helper 插入的
     `if (std::getenv("YAYA_DETERMINISTIC_SEED") != nullptr && length > 0) { --length; }` block；该 helper 位于 Frontend `real_gateway_chain_e2e_test.gd:690-706`；
   - 必须恰好包含一次 `// INT1_REAL_GATEWAY_FAILURE_DRAFT_V1`；
   - 删除这三个测试错误片段，保留其余 source 字节，再追加一个新的 deterministic M2 corrected marker；
   - 变换后的 source 必须包含一次 `for (int index = 1; index <= length; ++index)`，不得再包含 seed/decrement/failure marker；
   - 将其与 seeder canonical starter（Backend `src/walnut_backend/int1_e2e_authority.py:241-295`）按规范化换行核对：除明确 corrected marker 外必须字节相同。任何 shape/hash/anchor 漂移均让 relay 返回明确 fixture error，而不是猜代码。
3. 将变换结果写进 schema-generated decision 的 `skill_patch.replacement_content`，rationale 使用固定短文本；其余 authority 仍由 Runtime 注入。Agent prompt本来只让 Provider看到 source和opaque Evidence alias，不暴露 IDs/hash/CAS（Agent `prompting.py:41-48,270-281`）。
4. 先 `validate_instance(output, schema)`，再由 focused test实际用 pinned C++20 compiler/build library编译 candidate，并以参数 `8` 运行，解析 stdout，断言八个严格顺序的 HARVEST intent；以 deterministic seed 环境运行也必须仍为八个。
5. 测试反例：missing/duplicate anchor、非 main.cpp、failure_count 3、patch_eligible false、auto flag true、source已 corrected、第二文件、内容超限，全部 fail closed；generic non-Patch relay outputs保持原样。

证据与 statistics 只保存 dispatch_id、request/context/completion SHA-256、generation_count 与计数；现有 sanitized statistics 位于 relay `:154-181`。不得把 prompt、failure source、replacement source、Provider request/response body写入跨仓验证报告。deterministic classification 必须继续是 `DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER`，不能冒充真实 Provider。

### 4.3 deterministic dispatch 精确计数

正常 fixture 每个普通 Run Turn 恰好：root 第一次请求 tool_calls、root post-tool 决策、final 决策 = 3；五个 Run Turn=15。Patch Turn零工具且第一次即返回有效 proposal=1，总计精确 16 dispatch / 16 generations，且每 dispatch generation_count=1。focused relay test必须先锁住 Patch=1，再由正式 deterministic harness锁总数16；一旦产生 repair，deterministic gate应失败，不能把17当容许成功。

## 5. 真实 Provider 的事前 generation budget

真实 gate 使用 **硬上限 32**，而不是声称预期会花32次。推导必须写进 wrapper且在任何 Provider/容器进程启动前设置：

- 每个普通 Run Turn有 root 与 final 两个 runtime（Backend `src/walnut_backend/workers/turn_worker.py:221-347`）。
- Shared runtime最多一个 tool round和一个 invalid-output repair（Agent `python/yaya_agent_runtime/runtime.py:57-58`），单 runtime 串行请求最多3次：initial、post-tool、一次 repair（同文件 `:83-91,200-293,439-506`）。
- 五个普通 Run Turn的绝对上限是 `5 * 2 * 3 = 30`。
- Patch runtime构造为空 ToolRegistry（Backend `turn_worker.py:349-401,435-452`）；Patch eligible又强制 tool definitions/limit为0（Agent `runtime.py:155-176`），故最多 initial + one repair = 2。
- 总上限 `30 + 2 = 32`。正常无修复下预期16；17..32只能表示合规的一次-per-runtime repair，而不是自动重跑整个 E2E。

不能安全收紧到16，因为真实 Provider一次无效结构允许且设计上只允许一次修复；也不能沿用 INT1 的24，因为M2增加一个普通 Run Turn和一个 Patch runtime。若产品决定真实 gate禁止任何 repair，可另立严格16预算，但必须同时把 runtime/acceptance语义改为“第一次模型输出必须有效”；仅改 wrapper为16会把合法修复误报成预算耗尽。

实现要求：新 `run-int2-real-provider-e2e.ps1`，保留历史 INT1 wrapper；要求显式 opt-in、digest-pinned images、唯一 key source，设置 `WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS=32` 后才调用 INT2 harness，结束恢复原环境，不自动重跑。真实结果须断言 `16 <= total_generations == unique_dispatches <= 32`、每个 generation_count=1、source=provider、degraded=false。

预算 fail-closed 已有 PostgreSQL边界：新的唯一 reservation超过上限会回滚，未进入 Provider POST（Backend `src/walnut_backend/llm_relay/store.py:87-128`）；claim边界在把 generation fence置1和网络I/O之前再次拒绝（同文件 `:147-194`）。现有集成测试以12证明“第13个 reservation/claim 在 Provider POST前拒绝且 generation_count仍0”（Backend `tests/integration/test_private_recoverable_llm_relay_postgres.py:37-84`）。INT2需参数化/新增32→第33个同类测试，并在 wrapper contract test中证明设置发生在启动 harness之前。

## 6. Backend / 文件系统精确副作用计数

以下是 fresh seeded main ACCEPT Session 的预期，不是当前 PASS：

| Authority | 精确期望 |
|---|---:|
| Agent Session / Workspace / Draft head | 1 / 1 / 1 |
| immutable Draft revisions | 3（rev1 STUDENT, rev2 STUDENT, rev3 SKILL_PATCH） |
| Patch request / proposal / Evidence link / decision / decision receipt | 1 / 1 / 1 / 1 / 1 |
| Draft assistance | 1（rev3, inherited=false） |
| Build / Artifact / Certification / Activation / registry revision | 2 / 2 / 2 / 2 / 2 |
| Build / Certification / Activation provenance | 2 / 2 / 2 |
| Turn / Interaction | 6 / 6 |
| Run / Run provenance / Sandbox dispatch+result receipts | 5 / 5 / 5+5 |
| World commits / presentation streams / presentation events / gap | 1 / 1 / 8 / 0 |
| Learner jobs succeeded+closed / learner revision | 5 / 5 |
| Commands total / APPLIED / REJECTED | 11 / 7 / 4 |
| Command type: Build / Activate / Turn | 2 / 2 / 6 |
| deterministic Provider dispatch/result | 16 / 16 |
| persistent Sandbox files / Artifact files | 10（5 launch/result pairs）/ 2 |
| Evidence rows | 13 |

Command总数11沿用现有INT1的组成：create Session command 1 + Build 2 + Activate 2 + Turn 6；四个失败 Turn REJECTED，其余7个 APPLIED（Patch proposal Turn是 `NO_EFFECT` 但仍 APPLIED）。Evidence 13 = Build TEST_REPORT 2 + 四失败 Run各SKILL_RUN 4 + 成功 Run的 SKILL_RUN/WORLD_COMMIT 2 + 五个 LEARNER_UPDATE 5。Patch Evidence linkage复用第四次失败 Evidence，不新建 `game_evidence`。

数据库 fingerprint 必须包含完整 row material/hash，而非只 count：Patch五张表、Draft revision/assistance、四层 provenance、commands/jobs/receipts、interactions、Runs/Evidence、World/presentation、Learner profile/job/`LEARNER_PROJECTION_COMMITTED` receipts、recoverable Provider dispatch。现有 INT1 SQL fingerprint从 Backend `scripts/run-int1-local-diagnostic.ps1:1381` 开始，INT2应新建 harness或明确分支，不能覆盖历史 INT1 证据。

## 7. Learner assisted 非独立断言

第五个成功 Run 的 `skill_run_provenance.assistance_authority` 必须为 `SKILL_PATCH`。Backend handoff由 Run->Build权威图计算 `used_skill_patch` 并携带 patch/decision IDs（Backend `src/walnut_backend/workers/turn_projection.py:1720-1771`），投影时强制 assistance level 4（同文件 `:2691-2728`）。

精确断言：

- 第五个 learner job frozen assistance 与当前 DB provenance逐字段相同；`used_skill_patch=true`、authority=SKILL_PATCH、patch/decision ids、origin accepted revision row、Draft/Build/Activation/Certification/Artifact hashes全闭合。
- 最终 learner profile revision=5，目标 competency assistance_level=4。
- 最终 `LEARNER_PROJECTION_COMMITTED` receipt 的 `reason_codes` 包含 `SKILL_PATCH_BLOCKED_PROMOTION`；Backend将 reason codes写入 receipt（`turn_projection.py:2864-2875`）。
- success前后 evidence_stage不得因本次Patch成功晋级；Agent policy明确 `used_skill_patch` 阻断独立成功（Agent `python/yaya_agent_runtime/learner_projection_policy.py:338-363`），并安排近复习而非独立掌握间隔（同文件 `:400-424`）。
- Patch proposal Turn不创建 Learner job，因此 Learner jobs与Runs均为5而不是6。

这些字段当前不全部由Frontend public API展示；正式 harness应以 PostgreSQL权威 fingerprint断言，Godot只断言学生可见interaction/flow，不在客户端推测掌握度。

## 8. REJECT 零业务副作用用例

fresh PostgreSQL focused/独立 deterministic Session中生成一个有效proposal，保存 before fingerprint后通过正式/HTTP PatchDecision `REJECT`：

- 允许新增/改变：恰好一行 terminal decision、恰好一行 idempotency receipt、proposal Interaction revision+1且 `patch_decision=REJECT`、Workspace仅因 interaction revision/high-watermark authority合法更新（若实现会刷新）。
- 必须逐字节不变：Draft head及revision rows、Draft assistance、Workspace Draft ref、Build/Artifact/Certification/Activation/registry、Turn/Run/Evidence、World/Snapshot/events/presentation、Learner jobs/profile、Sandbox/Artifact文件、Provider generation count。
- 相同 key同payload replay完全相同且不增row；相同 key不同payload失败；第二个不同key或 ACCEPT 同一patch失败，因为一个patch只能有一个终态。

Backend decision实现只有 ACCEPT进入 Draft apply/append/assistance/Workspace Draft同步（`src/walnut_backend/adapters/postgres/product_interactions.py:430-514`）；REJECT仍应记录明确决策，而“零副作用”按上面的业务集合定义，不能错误要求完全零数据库写。

## 9. 重启与 recovery-only 指纹

当前 recovery test已先GET Bootstrap绑定exact persisted Session/active tuple（Frontend `tests/client/real_gateway_chain_recovery_e2e_test.gd:92-121`），要求 pending operations为空（`:266-289`），重建Workspace/Draft refs（`:319-343`），并逐字段匹配phase1 authority（`:366-375`）。最小扩展：

- phase1/recovery authority fingerprint加入：Patch proposal id/hash、decision id/receipt hash、accepted revision row identity、Draft `last_applied_patch_id`、完整 source hash、second Build/Activation provenance authority hashes、final Run assistance authority、interaction sequence 6/revision 1/book role；
- recovery设置两个本地 flag，GET capability后UI capability仍true；最终 Interaction必须是第6个 book success，而不是恢复到第5个Patch proposal；
- recovery AppRoot先调用 pending PatchDecision/Patch request recovery（Frontend `scenes/app/app_root.gd:255-280`），但成功phase1必须无pending envelope，所以不得产生 replay mutation；
- Frontend production transport audit继续要求每次请求已完成且method只GET（recovery test `:538-560`）；Backend Gateway日志独立拒绝任何非 GET/HEAD/OPTIONS（Backend `run-int1-local-diagnostic.ps1:804-852`）。
- 停止并以新PID/worker identity重启 Gateway、workflow worker、Learner worker；现有边界位于 Backend harness `:1836-1862`。recovery后DB、Provider relay、Sandbox、Artifact指纹必须逐字节不变；现有比较位于 `:1933-1981`。
- Godot最终 world revision/sequence/hash/Snapshot、presentation cursor=8、Draft revision3/hash/source、active tuple registry2、Workspace、final Interaction、Patch/provenance指纹都等于phase1；recovery显示Snapshot是校正，不重播业务动画、不产生业务副作用。

## 10. 实施和验证顺序

1. focused红测：TaskWorkspace signals/正式控件、四失败eligibility、relay Patch candidate+编译运行、transport 12/1计数、Learner assisted断言、32预算第33拒绝、recovery GET-only Patch fingerprint。
2. 最小实现后focused绿测。
3. Agent full non-live、Backend fresh PostgreSQL/Alembic/full pytest/Ruff/Pyright/compileall/contracts、Frontend full offline 全绿。
4. deterministic三仓 E2E：精确16 dispatch，classification deterministic，主链与restart fingerprint全闭合。
5. 正式 Gateway+Godot跨进程恢复E2E：host Docker分类，不能写成production private DinD。
6. 所有非计费门禁绿后，人工显式启动一次真实 Provider wrapper；上限32，禁止自动重跑。

任何一步失败都保存最小可复现RED诊断并保持 feature默认关闭。Backend当前full gate在本设计形成时仍未被本文件证明稳定；因此本文件只能用于实现，不得改变任何PASS状态。
