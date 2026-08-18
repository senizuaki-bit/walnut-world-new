# 核桃代码世界 Python 后端接口文档

> 文档版本：v1.4（2026-08-15 INT2 current evidence）
> 后端仓库：当前仓库根目录
> Agent 合同与参考实现：sibling `../agent` workspace
> 本文性质：Python 后端实现说明与交付状态，不是 Wire Contract。

## 0. 权威、版本与交付状态

接口语义按下列顺序裁决：

1. Agent 工作树中的 `contracts/manifest.json` 及其 OpenAPI、AsyncAPI、JSON Schema；
2. `python/yaya_agent_contracts/ports.py` 与 `contracts/port-surface.json`；
3. Agent 仓库的 `docs/CONTRACT_RULES.md`；
4. 本文和其他产品开发文档。

Python 后端是整个产品的唯一 HTTP Gateway、PostgreSQL 写入端和 Alembic 迁移权威；当前工作树 head 是 `019_int2_skill_patch_authority`，父修订为 `018_world_presentation_events`。Sibling `agent` workspace 提供 additive v0.6 candidate合同、Ports、Runtime、Build/Sandbox 库，**不是第二套产品后端**。只读INT2 capability GET始终挂载；presentation/Patch routes按flag条件挂载且默认关闭。WSS、Client Event Batch与Feishu不挂载；Skill Patch 已实现并通过当前门禁，默认关闭不等于未实现或生产启用。

合同跟随当前 Agent 工作树：每次构建、测试和部署前运行：

```powershell
$agentRepo = if (Test-Path '.\agent\contracts\manifest.json') { '.\agent' } else { '..\agent' }
$env:WALNUT_CONTRACT_PATH = (Resolve-Path $agentRepo).Path
py -3.12 scripts/verify_contract_release.py --agent-repo $env:WALNUT_CONTRACT_PATH
```

该校验会核对 manifest 中的每个 Wire 文件。不得手写、放宽或从 Markdown 猜测 v1 DTO；合同发生破坏性变化时，由 Agent 仓库发布新版本并同步消费者，不以旧 tag 或历史 hash 锁死当前开发。

### 0.1 当前后端交付快照

| 状态 | 范围 | 证据/边界 |
|---|---|---|
| Current PASS | Backend fresh full；M1 committed presentation；正式 deterministic M2 与受控 real-Provider M2 Proposal→Decision→Run→Learner/Godot | Backend 468/468、0 failure/error/skip，JUnit `artifacts/backend-full-int2-live-final-20260815T195320Z-671651fc.xml`，SHA-256 `852068818ADB98BEB12B830CEF27BBAE6928515C6CE3A1DF63FE6B43F3150DF6`；deterministic actual10 PASS；real-Provider run `868a` 301.012秒 PASS，outer stdout SHA-256 `2A7D2C057EF66D54F4DBFA828166DAC0A688704471618E6FA2940CE4F95B2425`，DB SHA-256 `b8bb2b568ac6978d938a98d041f9c5b74ef108167f53790c4bbeecbb6c051e30` |
| Current NOT PROVEN | production private DinD；公开 Gateway pending write response-loss | 受控 live harness 的 private relay/proxy response-loss PASS 不替代这两项部署证据 |
| 已装配 Product | ContentUnit、SessionWorkspace、SkillDraft GET/PUT、AgentInteraction list/get、只读INT2 capability | PatchDecision默认关闭并按flag条件挂载；默认关闭不等于未实现 |
| Historical evidence | 旧299/299与194.12秒INT1 live | 只作historical；不替代当前468/468、deterministic M2或INT2 live |
| 明确排除 | WSS、Feishu、Client Event Batch、自动/多文件Patch | HTTP aggregate+presentation GET是INT2 World权威；Patch capability默认false |

当前 Compose 启动私有 PostgreSQL、一次性 `alembic upgrade head`、唯一公开 `backend:8000`，以及均不公开端口的私有 `llm-relay`、digest-pinned DinD、`workflow-worker` 和独立 `learner-worker`。`workflow-worker` 持有私有 relay bearer 与 Docker socket，并提交 terminal hand-off；只有 `llm-relay` 持有上游 Provider key，`learner-worker` 不接收 Provider、relay 或 Docker 凭据。任何依赖失败都必须显式失败，不能用伪造的 `ACTIVE`、Run、Evidence、Interaction 或成功回执替代。

## 1. 架构与写入边界

```text
Godot / Product Web / Feishu Adapter
                -> FastAPI HTTP + default-off optional transport routers
                -> Application Use Case
                -> yaya_agent_contracts Ports
                -> PostgreSQL / Sandbox / Agent(LLM) / Outbox Adapter
```

- HTTP Adapter 负责认证、当前请求 attempt、合同校验、标准错误和响应二次校验；WSS/Event Batch 只在后续显式启用时装配；
- Application 层负责幂等、状态机、授权、CAS 与编排；
- `WorldUnitOfWorkPort.commit` 是唯一世界写入口，在同一事务提交快照、已提交事件和 Outbox；
- Sandbox 只能给出受限运行事实或 `ActionIntent`，不能返回并覆盖最终世界；
- Agent 只能产生受 Schema、权限和 Evidence 约束的结果或候选，不能直接写 World、SkillDraft、Skill Activation 或最终 Learner Projection；
- 所有外部投递经 Outbox；飞书不参与实时世界写链路。

## 2. 认证、当前请求与幂等

Game 与 Product 的每次 HTTP 尝试必须携带：

```http
Authorization: Bearer <token>
X-Request-Id: req_<unique-attempt-id>
X-Trace-Id: trace_<unique-attempt-id>
X-Correlation-Id: corr_<unique-attempt-id>
X-Schema-Version: 1.0.0
```

公开写操作另需：

```http
Idempotency-Key: <stable-business-action-key>
```

服务端从验证后的 Bearer 主体派生 `tenant_id`、`actor_id`、`actor_type` 与 `roles`。body 内的身份字段只能是业务关联，不能覆盖认证主体；任意 path/resource 读取或写入均需验证 tenant、actor 与资源绑定。

Game actor 隔离写操作的幂等 scope 是 `tenant_id + actor_id + operation + Idempotency-Key`。同一 key 仅在 body 字节完全相同时重放原回执；同 key 不同 body 返回合同 `409`。Product 的 scope 还包含 `operationId + canonical_path`；Feishu 则严格遵从各 operation 在 OpenAPI 声明的 scope。

响应 Header 描述本次 HTTP attempt；持久化资源里的 `request_context` 是首次创建的不可变 origin。轮询或重放使用新的 request/trace/correlation，不得错误要求其等于 origin context。

## 3. 异步 Command 与对账

Game 写操作返回 `202 Accepted` 只说明 Command 已耐久接收：

```text
POST + Idempotency-Key
-> 202 AcceptedGameJob + Location
-> GET /v1/commands/{command_id}
-> result.resource_url 查询 Build / Activation / Session
-> Agent Turn 进入 Sandbox/World 阶段后，通过 command.links.run 查询 Run
```

当发送 `202` 的结果不确定时，服务端必须返回含原 `command_id` 和 `Location` 的 `503 UNKNOWN_COMMIT_STATE`；客户端只按该 Command 对账，不能换 key 新建命令。`NO_EFFECT` 且 `run_id=null` 时不存在 Run，不能虚构轮询 URL。

只有 `APPLIED + WORLD_COMMIT` 的 Command 与可核对的 World receipt 才证明世界已改变。Run、Command、Snapshot、WorldEvent 和 Evidence 的 world revision、sequence、state hash 必须逐项闭合；Sandbox 成功本身不是世界成功。

## 4. Game Command REST

权威文件：`contracts/openapi/game-api.openapi.json` 与 `contracts/schemas/game/`。

| operationId | Path | 当前后端状态 |
|---|---|---|
| `getStudentBootstrap` | `GET /v1/student-bootstrap` | v0.4 已实现；返回 Session create/restore、Build policy、Registry/Activation exact tuple 与 HTTP World authority |
| `getGameBootstrap` | `GET /v1/bootstrap` | 冻结兼容读取；INT1 AppRoot 以 Student Bootstrap 为启动权威 |
| `createSkillBuild` / `getSkillBuild` | `POST /v1/skill-builds`、`GET /v1/skill-builds/{build_id}` | `workflow-worker` 使用完整 source bundle、pinned Docker、PUBLIC/HIDDEN tests、Artifact CAS 和 Build-terminal Certification |
| `activateSkillVersion` / `getSkillActivation` | `POST /v1/skill-versions/{skill_version_id}/activations`、`GET /v1/skill-activations/{activation_id}` | full-scope Registry CAS 与 immutable Activation 已实现 |
| `createAgentSession` / `getAgentSession` | `/v1/agent-sessions` | Control worker 终态原子创建 Session、starter Draft 与 Workspace |
| `createAgentTurn` | `POST /v1/agent-sessions/{session_id}/turns` | 接受时绑定 exact Skill tuple；`workflow-worker` 执行 Runtime/Sandbox/World 并耐久提交 terminal hand-off，独立 `learner-worker` 执行 Learner/Product/Workspace projection |
| `getCommand` | `GET /v1/commands/{command_id}` | 已实现 |
| `getRun` / `getEvidence` | `/v1/runs/{run_id}`、`/v1/evidence/{evidence_id}` | 由 terminal chain 写入并按 actor/opaque ID 授权读取，资源 origin 自闭合 |
| `getWorldSnapshot` / `listWorldEvents` | `/v1/worlds/{world_id}/snapshot`、`/v1/worlds/{world_id}/events` | 已实现；事件页仅包含已提交事实 |
| `ingestClientEventBatch` | `POST /v1/client-events:batch` | 已实现，批量事务成功时 Command 进入 `APPLIED`；序号冲突会整体回滚 |

不存在独立的 `POST /run`。`CODE_EDITED` 和 `HINT_VIEWED` 都是已发生遥测，不替代草稿保存或提示请求。

### 4.1 Activation 的公开 authority

当前 `skill-activation-request.schema.json` 强制要求：

```json
{
  "expected_registry_revision": 17,
  "activation_scope": { "world_id": "...", "agent_profile_id": "..." }
}
```

追加式 Student Bootstrap v0.4 已返回 `world_id`、`agent_profile_id`、`expected_registry_revision` 和当前 exact active tuple。前端原样消费这些值；不得用默认 `0`、UI state 或私有数据库猜测。Activation worker 重新读取 Build/SkillVersion/Certification/Artifact，执行 full-scope revision CAS，并原子提交 Activation、Registry head/history、Command 和 job receipt。

## 5. World Realtime WSS（INT1 排除）

WSS 在 INT1 中继续标记为未完成且不参与 AppRoot composition；`WALNUT_ENABLE_REALTIME_WSS` 与 `WALNUT_ENABLE_CLIENT_EVENT_BATCH` 默认均为 false，production app 因而不挂载对应 router，Student Bootstrap 的 `world_event_stream` / `client_event_batch` capability 也为 false。本轮只使用 HTTP Events/Snapshot 闭合恢复。以下协议说明保留给后续阶段，不得从 dormant 实现或 focused test 外推为本目标已交付。

权威文件：`contracts/asyncapi/runtime-events.asyncapi.json`。`runtime.events.{stream_id}` 是内部事件总线；`/v1/realtime` 才是 Godot WSS，两者不能混用。

冻结 Bootstrap Schema 即使 capability=false 仍要求 `stream_url`；默认 `wss://localhost/v1/realtime` 只是无 credential、无 query、无 fragment 的 inert 结构值。客户端必须先以 `world_event_stream` capability 裁决，false 时不得连接。未来 capability=true 后 Upgrade 才必须带：

```http
Authorization: Bearer <token>
X-Request-Id: req_<id>
X-Trace-Id: trace_<id>
X-Correlation-Id: corr_<id>
X-Schema-Version: 1.0.0
X-Stream-Protocol-Version: 1.0.0
Sec-WebSocket-Protocol: yaya.runtime.v1
```

公开 WSS 闭合帧为：客户端 `subscribe`、`resume`、`ack`、`heartbeat_ack`；服务端 `WorldEvent`、`subscribed`、`heartbeat`、`error`。`agent.turn.feedback_ready`、Build、Sandbox、Command 和 Learner 内部事件不得直接下行到客户端。

Dormant router 与 focused tests 覆盖 subscribe/resume/ACK/heartbeat 和 HTTP events/snapshot 回补语义：at-least-once 投递，客户端按 `event_id` 去重，只 ACK 已连续且持久应用的最大 sequence；gap 时先读 events，无法闭合再原子恢复 snapshot。这不是 production route 已发布的证据。

尚未完成的联调项：Godot 必须实现能携带上述 Upgrade headers 的真实 Transport，并与 localhost/部署环境的显式启用 WSS 做端到端握手、恢复和事件播放测试；完成前 capability 不能置 true。

## 6. Agent、Sandbox、Run 与 Learner

Turn terminal chain 由两个独立 worker 通过 durable hand-off 串联：

```text
已接受 Agent Turn
-> Session / scope / 激活版本校验
-> 受控 Agent Runtime + recoverable Provider relay（INT1 fail loud）
-> SandboxPort.run
-> WorldUnitOfWorkPort.commit
-> Run + Evidence
-> workflow-worker 原子提交 terminal hand-off
-> learner-worker 获取独立 lease/fence
-> Learner projection
-> Agent Interaction / Outbox / Workspace high-watermark
```

LLM 失败保留已经提交的客观 Run/World/Evidence，并让 job/Command fail loud；不得发布 `provider_fallback` Product Interaction，也不得推进 Learner。Production `workflow-worker` 只接受 `WALNUT_LLM_RELAY_ENDPOINT` 所指向的 `YAYA_RECOVERABLE_LLM_V1` 私有 relay：启动 capability fail-fast，dispatch GET-first，仅线性一致 `ABSENT` 后同 ID PUT，`PENDING` 遵守 `Retry-After`；fence、PostgreSQL dispatch/result receipt、completion 和 raw Provider bytes hash 联合校验。普通 direct chat adapter 是 best-effort，不能进入该 composition。Agent 内部 `AgentDecision` 不能直接当公共 DTO 返回；Godot 的展示内容只能来自合同化的 Command、Run、Evidence 或 Product `AgentInteraction`。

Backend 从 `yaya_agent_build` 使用唯一 Build/CAS 实现，从 `yaya_agent_sandbox` 使用 digest-pinned runtime Sandbox；数据库事务、lease/fencing、durable step receipt、Certification、Registry 与 Run/World/Evidence 由 Backend-owned `workflow-worker` 实现，Learner/Product/Workspace terminal projection 由独立 Backend-owned `learner-worker` 实现。Build 在 start-attach response loss 后 inspect 同一稳定容器并从 bounded logs 恢复；Sandbox 在 `SANDBOX_DISPATCHED` 后只 reconcile 同一 run/container/receipt，Docker preflight/create 暂不可用保持 retryable，不能落成伪学生终态。生产无 host compiler/native fallback。Focused/library tests 不等于真实 host Docker 或 private DinD control-plane fault 已被 live 注入。

## 7. Product Experience REST

已实现并按 Product 合同校验：

- 精确 content hash 的 `getProductContentUnit`；
- `getProductSessionWorkspace`，只保存资源引用，不复制 World Snapshot；
- `getProductSkillDraft` 与 `upsertProductSkillDraft` 的 revision/hash CAS；
- `listProductAgentInteractions` 与 `getProductAgentInteraction` 的连续 sequence 读取；
- `getInt2Capabilities`始终挂载；`recordProductPatchDecision`只在Backend Skill Patch flag为true时条件挂载并默认关闭。正式 deterministic M2 与受控 real-Provider M2 均已 PASS，但默认 flag 未改变，也不等于生产启用。

Product 是页面投影层，不得成为第二个 Game 事实源。Interaction 内的 feedback 必须与同一 session、turn、command、run 和 Evidence 闭合；terminal projection 与 Learner/Workspace high-watermark 在同一 Backend 事务收敛。

## 8. Feishu Integration REST

Feishu 的目标 operation 仍以 `contracts/openapi/feishu-integration.openapi.json` 为准，但 Feishu 明确排除在 INT1 之外；本文不把合同存在写成当前交付。

实现前还缺三项外部权威输入：Webhook 签名算法/签名原文与密钥轮换规则、飞书主体到产品 tenant/role 的授权关系、Content Release 的创建/验证/审批/发布状态来源。Webhook 不使用 Service Bearer；其他 Feishu operation 使用 Service Bearer，并且所有写入严格经 Outbox。审批不等于 Activation，报告草稿不等于真实发送。

## 9. 验收与运行

完整验收链路必须是：

```text
Student Bootstrap -> server-created Session + starter Workspace/Draft
-> Build -> Certification -> Activation
-> Agent Session / Turn -> Run
-> World receipt / Events / Snapshot -> Evidence
-> Learner -> Agent Interaction -> Workspace recovery -> Godot display
```

每一步断言身份、revision、sequence 与 hash，不以 HTTP 2xx 代替业务成功。27.6 秒本地 direct-POST，以及后续 79.764 秒、106.867 秒和 169.836 秒诊断都只保留为历史证据。169.836 秒 fixture-recoverable-relay 记录曾闭合 Draft/Workspace revision、Build/Activation、四个 Turn/Run、11 个 Evidence、4 个 Learner projection/Interaction、三个正式 UI panel、同一 disposable PostgreSQL stop/start 和三服务新 PID recovery-only phase2；其历史 authority tuple `side_effect_sha256` 为 `9d9e770a6bf8f9f03fc351c50a3fba2dd3d57971df91237d46f9e49c3335ab05`，当时状态为 **`DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER` PASS**。该 fixture 记录仍只是历史证据。

历史 INT1 唯一真实 Provider 运行在 194.12 秒取得 `REAL_PROVIDER_PRIVATE_DURABLE_RELAY` PASS；它属于 host-Docker historical evidence，不等同于 production private DinD 或当前 INT2 live。当前 INT2 real-Provider run `868a` 在 301.012 秒取得 PASS：18 unique dispatch / 18 generation、单 dispatch 最大 generation 1，注入的 response-loss 复用同一 dispatch 且 generation 仍为 1；`PUBLIC_UI_CHAIN_CLOSED`、1 个 World commit、8 个 presentation event、11 个 terminal Command（7 `APPLIED` + 4 `REJECTED`）以及 phase2 17 GET / 0 mutation 全部闭合。旧 Backend 299/299 只作历史记录；当前 Backend full 的独立证据为 468/468、0 failure/error/skip。

本地基础校验：

```powershell
.\scripts\verify_all.ps1 -DatabaseUrl '<disposable PostgreSQL URL>'
```

该脚本依次验证当前 Agent 合同清单、Alembic、Ruff、Pyright 和测试。生产部署必须关闭开发鉴权，从密钥系统注入数据库和 JWT 参数；日志与审计不得输出 Bearer、源代码或敏感 Evidence。

## 10. 当前状态与剩余边界

1. Backend current full gate 已为 468/468、0 failure/error/skip；旧 299/299 与 252/252 均为 historical，不得作为当前计数；
2. INT1 194.12秒real-Provider行是historical；当前 INT2 live authority 是 2026-08-15 run `868a` 的 301.012 秒 PASS；
3. production private DinD live 与真实 Docker control-plane fault 仍是独立、尚未完成的部署证据，不能从 host-Docker live 或 focused tests 外推；
4. 公开 Gateway pending write response-loss 仍为 `NOT_PROVEN`；本次证明的是私有 Provider relay 的 PUT/GET response-loss 恢复。

任何阶段都不得为了推进前端页面而放宽 v1 schema、跳过合同验证、伪造终态或绕过唯一世界写路径。
