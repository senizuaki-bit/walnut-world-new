# 核桃代码世界：Godot 前端接口文档

> 文档版本：1.3（2026-08-17 WATER frontend candidate draft）
> 适用目录：`walnut-world-frontend`（Godot 4.5.2）
> Wire Contract：已发布 v0.4 字节保持不变；当前三仓工作树消费 additive v0.6 candidate（v0.6 tag `NOT_PROVEN`）

## 1. 权威顺序与交付状态

接口字段、路径、枚举、错误和实时帧以如下顺序裁决：

1. sibling `../agent/contracts/manifest.json` 与其锁定的 OpenAPI、AsyncAPI、JSON Schema、错误目录；
2. Agent 的 Python Port surface 与合同规则；
3. `05_核桃代码世界_接口对齐与联调规范.md`；
4. `01`—`04` 的产品与实现文档；
5. 本文只描述 Godot 如何使用上述合同，不创造新字段或近似接口。

Godot 当前合同候选为 additive v0.6：147 entries、27,848 bytes、SHA-256 `11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05`；v0.4/v0.5 历史 Wire 继续逐字节锁定，v0.6 tag 尚不存在。本仓 offline headless 当前为 60/60 PASS，另有两条 real opt-in 精确 `EXCLUDED_NOT_RUN`，0 skip/fail；stdout SHA-256 为 `269E5D6BA4FDCEFBBDCF82E33FDA204C820AD942EAECA2312DDED37753D8C2E4`。正式 deterministic actual10 与受控真实 Provider M2 均已 PASS。真实 Provider `run868a` 用时 301.012 秒，DeepSeek `deepseek-v4-flash` 为 `source=provider`、`degraded=false`，18 unique dispatch / 18 generation、单 dispatch 最大 1；Provider relay response-loss 恢复同一 dispatch 且 generation 仍为 1。6 Turn、5 Run、6 Interaction、11 terminal Command（7 `APPLIED` + 4 `REJECTED`）、1 条 World commit 与 8 条 presentation event 分别闭合，Patch 达到 `PUBLIC_UI_CHAIN_CLOSED`，断库恢复后的第二 Godot 进程只执行 17 GET/0 mutation。Skill Patch/PatchDecision 默认关闭且按 capability/flag 收紧；WSS、Client Event Batch、Feishu、自动/多文件 Patch 继续排除，production private DinD 与公开 Gateway pending write response-loss 仍为 `NOT_PROVEN`。

## 2. Godot 边界与组成

Godot 是学生端：展示任务、编辑 C++ 文本、保存 Draft、发送命令、呈现后端确认的 Build/Run/Evidence/反馈，并将权威 World Snapshot/Event 投影到**预置**场景节点。它不编译或执行 C++，不直接访问 Sandbox、数据库、LLM 或飞书，也不持有世界或 Learner 的权威状态。

```text
预置 TaskWorkspace / WorldViewport / 各 Panel
             ↓
SessionController + ClientStore
             ↓
YayaAgentApiGateway / ProductInteractionGateway / RealtimeClient
             ↓
Python Product REST、Game REST、/v1/realtime WSS
             ↓
Agent Runtime、Sandbox、World UoW、Run/Evidence、Learner Projection
```

场景不得直接发 HTTP 或解析私有 JSON。Transport 的结果统一为 `{ok,status,headers,value}` 或 `{ok:false,status,headers,error}`；Gateway/Validator 严格拒绝未知字段、身份错链、非法状态和 HTTP/错误码不匹配。仅 `APPLIED + WORLD_COMMIT` 或可核验的 World receipt 后，世界才允许表现永久变化。

## 3. 通用调用规则

### 3.1 身份、版本与尝试 Header

所有 Game/Product 请求使用 Bearer 身份；后端从 token 派生 tenant、actor 和 role，body 不得覆盖。每个 HTTP 尝试均发送：

```http
Authorization: Bearer <token>
X-Request-Id: req_<本次尝试唯一值>
X-Trace-Id: trace_<本次尝试唯一值>
X-Correlation-Id: corr_<本次尝试唯一值>
X-Schema-Version: 1.0.0
```

所有写操作增加稳定的 `Idempotency-Key`。重试复用**相同请求字节和幂等键**，但 request/trace/correlation 是新的本次尝试值；资源 body 的 `request_context` 是首次创建时的 origin，不能和轮询请求的 Header 要求相等。

### 3.2 CAS、202 与不确定结果

- Draft 与 Patch 使用 revision/hash CAS；World/Registry/Learner 使用各自 revision/sequence CAS；幂等不能替代 CAS。
- Game 写操作的 `202 AcceptedGameJob` 只表示已耐久接收。保留 `command_id`、`Location`、原 body 和 key，随后只查询该 Command。
- `RESOURCE_CREATED` 后按 `result.resource_url` 获取 Build/Activation/Session；Turn 到达 Sandbox/World 阶段后，按 `command.links.run → GET /v1/runs/{run_id}`。
- 若响应为 `503 UNKNOWN_COMMIT_STATE`，只轮询响应给出的 `Location`，禁止新建 key 再次提交。
- Product Draft PUT 与 PatchDecision 不是 Game Command；若收到 `503` 的 Product `RECONCILE` 响应，必须先严格核对 `Location`、resource type/session/id 与 `resource_url`，再 GET 该 canonical Draft 或 Interaction。GET 资源必须精确证明原写入已落地（Draft 的 source bundle；PatchDecision 的 decision/interaction revision/基线与结果 hash）；在此之前不得重放，更不得换 key。

错误必须使用合同 `ErrorResponse` 和 `error-catalog.json`。编译/测试/学生运行失败是资源业务终态，不能当作通用 HTTP 500；`retryable`、`Retry-After`、`Location` 以合同为准。

## 4. REST 接口清单

### 4.1 Product Experience：页面和可变草稿

| operationId | 方法与路径 | Godot 用途 | 当前可联调状态 |
|---|---|---|---|
| `getProductContentUnit` | `GET /product-experience/v1/content-units/{unit_id}/versions/{content_version}?content_hash={content_hash}` | 读取版本固定任务内容 | 唯一 Backend 已装配 |
| `getProductSessionWorkspace` | `GET /product-experience/v1/sessions/{session_id}/workspace` | 恢复页面资源引用、进度和游标 | 唯一 Backend 已装配；Session/Turn/terminal projection 维护 |
| `getProductSkillDraft` | `GET /product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}` | 恢复云端源码 | 唯一 Backend 已装配 |
| `upsertProductSkillDraft` | `PUT /product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}` | 自动保存完整 source bundle | 唯一 Backend 已装配，revision/hash CAS |
| `listProductAgentInteractions` | `GET /product-experience/v1/sessions/{session_id}/agent-interactions?after_sequence={after_sequence}&limit={limit}` | 恢复连续教学交互 | 已装配读取 |
| `getProductAgentInteraction` | `GET /product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}` | 取得反馈与 Patch | 已装配读取 |
| `recordProductPatchDecision` | `POST /product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}/patches/{patch_id}/decision` | 原子 ACCEPT/REJECT | Backend 在 Skill Patch flag 为 true 时条件挂载，默认关闭；deterministic actual10 与受控真实 Provider M2 已闭合，不等于 production 默认启用 |

Product 是页面投影，不能复制 World 成为第二权威。Draft 是可变工作区，Build 是不可变产物，二者 revision 不可混用。Patch 必须是 `SkillPatch.operations`，同时核验 interaction/turn/draft/skill、base revision/hash 和 result hash；已决定的 Patch 不可重复决定，REJECT 绝不改 Draft。

### 4.2 Game Command：客观执行与世界事实

| operationId | 方法与路径 | Godot 行为 |
|---|---|---|
| `getStudentBootstrap` | `GET /v1/student-bootstrap` | 返回固定 actor/content、Session 创建或精确恢复权威、Build policy、active Skill 精确元组及 HTTP world 恢复游标。 |
| `createSkillBuild` / `getSkillBuild` | `POST /v1/skill-builds` / `GET /v1/skill-builds/{build_id}` | 提交完整源码包；展示编译、课程测试、认证与不可变 Evidence。 |
| `activateSkillVersion` / `getSkillActivation` | `POST /v1/skill-versions/{skill_version_id}/activations` / `GET /v1/skill-activations/{activation_id}` | 仅激活已认证版本；提交时使用合同给出的 registry CAS 上下文。 |
| `createAgentSession` / `getAgentSession` | `POST /v1/agent-sessions` / `GET /v1/agent-sessions/{session_id}` | 创建或恢复版本固定的 Agent Session。 |
| `createAgentTurn` / `getCommand` / `getRun` | `POST /v1/agent-sessions/{session_id}/turns` / `GET /v1/commands/{command_id}` / `GET /v1/runs/{run_id}` | Turn 唯一编排运行/提示；没有 `POST /run`。只通过 `links.run` 发现 Run。 |
| `getWorldSnapshot` / `listWorldEvents` | `GET /v1/worlds/{world_id}/snapshot` / `GET /v1/worlds/{world_id}/events?after_sequence={after_sequence}&limit={limit}` | 快照投影、断线回补和 gap 恢复。 |
| `ingestClientEventBatch` | `POST /v1/client-events:batch` | 上报 `CODE_EDITED`、`HINT_VIEWED` 等已发生遥测；不等于保存或请求提示。 |
| `getEvidence` | `GET /v1/evidence/{evidence_id}` | 获取不可变证据并保存 ETag。 |

启动时先从 StudentBootstrap 精确恢复或创建 Agent Session，并恢复 Workspace；不会自动 Build 或 Activation。Build 与 Activation 是显式操作，学生 Run 只消费 StudentBootstrap 或显式 Activation 返回的 active 精确 Skill 元组。Turn 带认证 Skill 绑定、`expected_world_revision`、最后事件序号和连续 turn 序号。Sandbox 只产生 `ActionIntent[]`；世界事实只能来自 `WorldUnitOfWorkPort.commit` 的 receipt、Snapshot 和 Event，不可接受 `final_world_state` 直接覆盖。

## 5. 端到端客户端流程

### 5.1 进入或恢复任务

`StudentBootstrap → ContentUnit → exact AgentSession → SessionWorkspace → SkillDraft → WorldSnapshot → AgentInteraction page`。`current_session_id` 非空时只精确 GET；为空时原样 POST `session.create_request`，轮询 Command 后只 GET `command.result.resource_id`，没有 latest/default 回退。每一份资源都保留其固定内容/世界/会话身份，Snapshot 直接投影到 TerrainManager 和 Player 等预置节点。页面恢复不由本地缓存猜测世界结果；ContentUnit 的名称、目标与 Build policy/capability 权威不由场景猜测。

### 5.2 保存、Build、Activation

编辑器以预置 `AutoSaveTimer`（0.8 秒 debounce）保存 Product Draft，提交完整 bundle、`base_revision`、`base_hash` 和幂等键；CAS 冲突时读取权威 Draft 并让用户处理。保存中出现的新编辑必须保留为 DIRTY，使用刚收到 canonical Draft 的新 revision/hash 进行下一次保存，不能被旧回执覆盖。Draft、Turn 与 PatchDecision 的逻辑操作 envelope 在响应丢失重试间保持同一 ID、幂等键、时间与 body。Build 成功后仍须等待 Certification；只有已认证 SkillVersion 可 Activation。Build policy、Activation scope/registry revision 与 active 七字段精确元组均来自 StudentBootstrap，不能用 UI 字段拼凑；Build 与 Activation 均不属于“提交并运行”的自动步骤。

### 5.3 Turn、Run 与教学反馈

学生 Run 的闭包为 `createAgentTurn(202) → terminal Command → terminal Run → Evidence → HTTP Events → Snapshot → Product Interaction`。前端交叉检查 session、turn、command、active Skill 绑定、`world_application.receipt`、Evidence、连续 event cursor 与 world revision/sequence/state hash；只有 Snapshot 与 receipt 精确一致才原子替换本地世界，只有匹配的非降级 provider Interaction 可使流程进入 `COMPLETED`。提示 Turn 可合法没有 Run，但仍必须恢复匹配 Interaction。Agent 的内部 `agent.turn.feedback_ready` 不上客户端 WSS，展示反馈必须从 canonical Product Interaction 读取；INT1 不接受 fallback/degraded 假成功。

预置 `DialoguePanel` 只消费已验证 Interaction 的 `role`、`feedback.message`、`response_type`、`hint_level` 与可空 `question`：角色名称和提示级别以合同枚举映射展示，`question` 为空时不显示追问。前端不从文本推断角色或提示级别，也不生成未返回的问题。

提示按钮提交 `input.type=MESSAGE` 的 Agent Turn，使用 `zh-CN` locale 且允许空 `skill_bindings`；它不要求 Activation，也不把无 `links.run` 当作失败，而是恢复 Product Interaction 的教学反馈。学生代码运行仍只接受认证且已激活的不可变 Skill binding。

### 5.4 世界实时、回补与恢复

INT1 只验收 StudentBootstrap 给出的 HTTP `events_url` 与 `snapshot_url` 恢复闭包。Backend production 默认不挂载 WSS 或 Client Event Batch；v0.4 `StudentBootstrapV2` 不包含 `world_event_stream`、`client_event_batch` 或 `stream_url` 响应字段，客户端不得从本地未接入代码猜测可用。现有 WSS 客户端代码保留为未完成能力，本轮不扩展、不接入 AppRoot，也不把它计入 INT1 完成条件；HTTP Command/Run、Evidence、Events 与 Snapshot 始终是执行事实和恢复权威。

## 6. 前端实现映射

| 职责 | 实现 |
|---|---|
| 启动装配与 Bootstrap/恢复 | `scenes/app/app_root.tscn`、`scenes/app/app_root.gd` |
| 状态、保存、Build/Activation/Turn 编排 | `autoload/client_store.gd`、`autoload/session_controller.gd` |
| Game 合同 Gateway/Transport/Validator | `addons/yaya_contract_client/` |
| Product、Interaction、Patch | `scripts/client/product_interaction_gateway.gd` |
| Command 轮询、实时协议、恢复 | `scripts/client/command_poller.gd`、`world_realtime_client.gd`、`world_recovery_coordinator.gd` |
| Snapshot/事件表现 | `world_snapshot_projector.gd`、`world_event_player.gd`、`scenes/task/world_viewport.gd` |
| 预置学生端 UI | `scenes/task/task_workspace.tscn`、`code_editor_panel.tscn`、`run_control_panel.tscn`、`result_panel.tscn`、`dialogue_panel.tscn` |

UI 由预置 `Control` 与 Container 布局组成；运行时只更新既有节点内容和已确认的表现命令。`ClientStore` 状态为：`BOOTSTRAPPING`、`READY`、`BUILDING`、`BUILD_FAILED`、`CERTIFIED`、`ACTIVATING`、`ACTIVE`、`TURN_RUNNING`、`PLAYING`、`COMPLETED`、`ERROR`。

### 6.1 作物关卡 WATER 候选兼容（前端草案）

作物关卡新增显式、默认关闭的 `water_candidate_compatibility_enabled`。它只处理固定 ContentRef 下满足以下条件的 Run：

`REJECTED + terminal + Sandbox SUCCEEDED + WATER-only intents + WorldApplication REJECTED + receipt=null + failure.code=WORLD_RULE_REJECTED + failure.stage=WORLD_VALIDATE + failure.details.reason=TASK_INCOMPLETE`。

前端将 intents 应用到 Run 前 Snapshot 的本地副本，生成 `SANDBOX_ACTION_INTENT_CANDIDATE`，但不修改 `ClientStore.world_snapshot`、`objective_result`、revision、event sequence、state hash 或 Receipt。UI 必须显示“本地候选结果 / 世界未提交”，`LOCAL_COMPLETED` 不等于后端成功。

当前缺少可上线的固定 ContentRef、8 个 plot_id/目标 hydration 区间、Backend WATER Build policy/评分/提交以及正式 `world.action.watered` schema，均不得用占位值开启生产路径。完整接口、Owner 和迁移方案见 [WATER 前端候选兼容与三端联调方案](godot-prompter/plans/2026-08-17-water-candidate-frontend-interface.md)。

`AppRoot` 是唯一运行时 composition root：它只读取 `YAYA_API_BASE_URL` 与运行时注入的 `YAYA_AUTH_TOKEN`（token 永不写入场景），没有 Session ID 配置或 export。它先以 WireAttempt 调用 `GET /v1/student-bootstrap`，再以已验证的 actor/content 创建每次请求的新 RequestContext。`current_session_id` 非空时只精确 GET 该 Session；为空时原样 POST `session.create_request`，按总 deadline、指数退避与 `Retry-After` 轮询 Command，再精确 GET `command.result.resource_id`。随后恢复 Workspace/Draft/Snapshot/Interaction。Build、Activation 与 active Skill 精确元组均来自公开权威，不由 UI 推断。

## 7. 验收门禁

- manifest/Schema 与 Gateway、Validator、fixture 完全一致；未知字段/枚举、身份错链和非法状态必须失败。
- 覆盖 `POST A → GET B`、幂等重放、`UNKNOWN_COMMIT_STATE`、Product `RECONCILE` 的 canonical GET、Draft/Patch CAS、Command 非法状态边与 revision 跳变。
- 覆盖 Build 编译失败/课程测试失败、Activation 拒绝、Turn 的 `NO_EFFECT` 和 fallback；禁止显示假成功。
- 覆盖 Snapshot 初始投影、重复事件、sequence gap、HTTP 回补、快照替换与 WSS close code（尤其 `4406`）。
- 正式 E2E 必须证明 Draft PUT/CAS、Patch request/decision、Workspace exact Draft ref、Command、Run、World receipt/presentation、Evidence、Learner、Product Interaction、Snapshot 与正式 UI display 的 identity/revision/sequence/hash 闭合。当前 deterministic actual10/outage/restart 与受控真实 Provider M2 `run868a` 均为 PASS；194.12 秒 live 与 169.836 秒 fixture 只作 historical INT1 evidence，production private DinD 和公开 Gateway pending write response-loss 仍为 `NOT_PROVEN`。
