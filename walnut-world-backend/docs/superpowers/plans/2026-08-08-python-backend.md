# 核桃代码世界 Python 后端实施计划

> 历史计划（2026-08-08）：合同基线、缺口和实施顺序保留作审计，不代表当前状态。当前实现固定 v0.4.0/138-file release，唯一 Backend Gateway、Alembic head `017_durable_learner_worker`、durable workers 及 Student Bootstrap/Workspace/Build/Activation/Turn terminal chain 已装配；真实 Provider cross-process E2E 已于 2026-08-13 在 194.12 秒取得 `REAL_PROVIDER_PRIVATE_DURABLE_RELAY` PASS。Backend 当前 fresh full 为 299/299、0 failure/error/skip、142.238s。

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个 Python 3.12 模块化单体后端，按固定 Wire Contract 为 Godot、Agent Runtime、Sandbox 与飞书适配器提供可对账的 HTTP/WSS 服务。

**Architecture:** `walnut-world-backend` 只包含入站 Transport、应用用例、领域规则和 Port Adapter；领域/应用层只依赖已固定版本的 `yaya_agent_contracts`。所有异步写请求先由 `CommandStorePort.accept_once` 持久化，再由 worker 推进命令状态；世界状态、领域事件和 Outbox 在 `WorldUnitOfWorkPort.commit` 的同一 PostgreSQL 事务中提交。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL 16、pytest、httpx、WebSocket、Docker；C++20 Sandbox 和 LLM/飞书为 Port Adapter。

## Global Constraints

- Wire 权威顺序：固定的 `contracts/manifest.json`、OpenAPI/AsyncAPI/JSON Schema、`ports.py`/`port-surface.json`、`CONTRACT_RULES.md`、再到五份 Markdown 文档。
- 只消费不可变合同 release（tag + Manifest 逐文件 SHA-256）；不得依赖本机 `agent/main`，不得手写或放宽 v1 DTO。Sibling `agent` workspace 提供合同、Agent Runtime 与参考实现；整个产品的 HTTP/WSS 后端归属 `walnut-world-backend`，不得部署第二套竞争性业务后端。
- 所有 Port 为异步方法，并接收 `OperationContext`；Agent、Sandbox、World、LLM、ORM/SDK 相互隔离。
- 认证主体派生 tenant/actor/roles；body 不能覆盖它。写操作实施合同规定的幂等作用域；Draft/Patch 决策额外使用 revision/hash CAS。
- `202 Accepted` 仅表示命令已经持久化；用 `command_id + Location` 对账。`503 UNKNOWN_COMMIT_STATE` 必须返回原 command 和精确 Location。
- Sandbox 只生成 ActionIntent；唯一世界写路径是 `WorldUnitOfWorkPort.commit`。LLM 失败必须以 `degraded/source/fallback_reason` 显式降级。
- 所有出站响应依次验证 status/header、JSON Schema、跨字段不变量、path/body/header 身份、canonical 存储资源；不返回 ORM Model 或 `200 {"success": false}`。
- 每项功能独立提交；提交信息使用中文并遵循 `feat: <中文功能说明>`。

## 已确认的合同状态（2026-08-09 复核）

当前固定基线为 `agent-contracts-v0.3.0`：保留 Product Experience 合同，并新增 `learner.inference.recorded`、EventEnvelope V2 与推断 hash 合同。Python 后端必须按该 release 规划 Product Workspace、Draft、Interaction、Patch Decision 与 Learner 投影；仍不得从 Markdown 猜字段。

接入前的发布门禁不变：后端要锁定 `refs/tags/agent-contracts-v0.3.0` 和 Manifest SHA-256，并在 Python 3.12、Node 依赖已安装、`contracts/**` 以 LF 签出的干净工作树运行合同验证。验证必须使用 `git archive` 的 release 字节，不能用可能被 `core.autocrlf` 改写的工作树字节。

## 目标目录

```text
walnut-world-backend/
├── src/walnut_backend/
│   ├── api/                 # FastAPI routers、认证、合同解析与响应校验
│   ├── application/         # 每个 operationId 一个 use case/command worker
│   ├── domain/              # 世界规则、命令状态转换、评分与补偿规则
│   ├── ports/               # 对 yaya_agent_contracts Protocol 的依赖声明
│   ├── adapters/            # PostgreSQL、Sandbox、LLM、飞书、WSS 实现
│   ├── workers/             # Command 与 Outbox worker
│   └── bootstrap.py         # Settings、DI 与 lifespan
├── migrations/versions/
├── tests/{contract,integration,unit,e2e}/
├── contract-release.json    # 锁定 tag、Manifest SHA 和文件清单
├── pyproject.toml
├── alembic.ini
└── docker-compose.yml
```

---

### Task 1: 锁定并验收 Wire Contract release（阻塞 Product 实现）

**Files:**
- Create: `contract-release.json`, `scripts/verify_contract_release.py`, `tests/contract/test_contract_release.py`
- Dependency owner: sibling `agent/contracts/` 发布包含 Product Experience OpenAPI、全部 Product Schema、示例、不变量和 Manifest 的新不可变 tag。

**Interfaces:**
- Consumes: Agent release tag、`contracts/manifest.json`、`ports.py`、`port-surface.json`。
- Produces: 后端构建和 CI 可验证的固定合同输入；Product 端点的精确 schema 文件名。

- [ ] 记录 release tag、package version、Manifest SHA-256 和每个 Wire 文件的 SHA-256 到 `contract-release.json`，并把合同 Python 包以精确版本安装。
- [ ] 编写 `verify_contract_release.py`：读取 JSON，验证 tag 非 `main/latest/unreleased`、所有 manifest 路径存在、字节数和 SHA-256 匹配；缺失文件必须以非零状态失败。
- [ ] 编写失败测试：删除 Product schema、替换任一 hash、将 tag 改为 `main`，均断言验证失败；以完整 release 断言成功。
- [ ] 运行 `python scripts/verify_contract_release.py` 与 `pytest tests/contract/test_contract_release.py -v`，两者必须通过。
- [ ] 提交：`git commit -m "feat: 锁定后端 Wire Contract 发布版本"`。

### Task 2: 建立可测试的 FastAPI 分层骨架与公共 Transport

**Files:**
- Create: `pyproject.toml`, `src/walnut_backend/bootstrap.py`, `src/walnut_backend/api/app.py`, `src/walnut_backend/api/dependencies.py`, `src/walnut_backend/api/middleware.py`, `src/walnut_backend/api/errors.py`, `src/walnut_backend/api/response_validation.py`, `tests/integration/test_transport_contract.py`

**Interfaces:**
- Consumes: `ErrorResponse`、error catalog、`OperationContext`、认证后的 `ActorRef`。
- Produces: 任何 HTTP/WSS handler 都能取得可信 context、标准错误和经二次校验的合同响应。

- [ ] 写失败测试，覆盖缺失/无效 Bearer、缺失 request/trace/correlation/schema header、未知 schema version、错误 response header、路径 ID 与 body ID 错链。
- [ ] 实现 Settings（数据库、合同路径、开发认证映射、Sandbox/LLM/飞书 URL、超时）与 lifespan 依赖注入；开发 token 的精确映射由固定 release/profile 定义。
- [ ] 实现 middleware：生成/回显本次尝试的 request/trace/correlation；从 Bearer 派生 actor；把 `OperationContext` 注入依赖；只映射 error catalog 的 HTTP 错误。
- [ ] 实现 response gateway：按“header→schema→不变量→身份→canonical resource”顺序校验，不合格响应转换为合同化 `INVARIANT_VIOLATION`，绝不发送半合法 JSON。
- [ ] 运行 `pytest tests/integration/test_transport_contract.py -v`、`ruff check src tests`、`pyright src`。
- [ ] 提交：`git commit -m "feat: 建立后端 Transport 与合同响应校验"`。

### Task 3: PostgreSQL 基础设施、审计、幂等命令与 Outbox

**Files:**
- Create: `src/walnut_backend/adapters/postgres/{session,models,command_store,audit,outbox}.py`, `migrations/versions/001_core_infrastructure.py`, `src/walnut_backend/workers/command_worker.py`, `src/walnut_backend/workers/outbox_worker.py`, `tests/integration/test_command_idempotency.py`, `tests/integration/test_outbox_replay.py`

**Interfaces:**
- Consumes: `CommandStorePort`、`AuditPort`、`OutboxPort`。
- Produces: 原子 `accept_once`、revision/status CAS、审计追加与可重放 Outbox。

- [ ] 为 commands、idempotency receipts、audit records、outbox messages 建表；唯一索引严格按合同的 tenant/actor/operation/key scope 建立，不把 trace ID 纳入 scope。
- [ ] 写失败测试：同 key 同字节 body 回放原 command；同 key 不同 body 返回 `IDEMPOTENCY_KEY_REUSED`；跨 actor 不共享记录；旧 revision transition 失败；worker lease 过期可重领。
- [ ] 实现 `accept_once`、`transition`、`find_non_terminal_before` 和 Outbox 五个方法；每次状态转换和外部访问均写不可变、脱敏 audit。
- [ ] 实现 command worker：仅 `created=true` 的 command 可执行；崩溃重启后从非终态命令恢复；未知状态或 event type 明确失败。
- [ ] 运行迁移、PostgreSQL 集成测试与 worker 重放测试。
- [ ] 提交：`git commit -m "feat: 实现命令幂等事务与可靠 Outbox"`。

### Task 4: 实现确定性 World、事件流与唯一提交事务

**Files:**
- Create: `src/walnut_backend/domain/world/{state,engine,rules,scoring}.py`, `src/walnut_backend/adapters/postgres/{world,event_store}.py`, `migrations/versions/002_world_event_store.py`, `tests/unit/test_world_engine.py`, `tests/integration/test_world_atomic_commit.py`

**Interfaces:**
- Consumes: `ActionIntent`、`WorldPort`、`WorldUnitOfWorkPort`、`EventStorePort`。
- Produces: `WorldSnapshot`、连续 `DomainEvent`、`WorldAtomicCommitReceipt`。

- [ ] 写单元测试：相同输入得到相同状态；移动越界、非法浇水、重复动作、动作上限、目标完成；以及任务内容版本决定的评分/成功条件。
- [ ] 实现纯领域 WorldEngine，只将有效 ActionIntent 转为新状态和未提交事件，不导入 SQLAlchemy 或 Sandbox；首个 watering 规则必须与 `farm-rules-1`、`world_rules_version`、WaterIntent 和快照 state hash 的上游可执行语义一致。
- [ ] 实现 `WorldUnitOfWorkPort.commit`：以 `stream_id + expected_sequence` CAS，在一个事务保存 snapshot、events 和待投递 Outbox；返回的 revision、first/last sequence 和 state hash 必须逐项一致。
- [ ] 写集成测试：并发 commit 只有一个成功；receipt 整组篡改、sequence 断档、event/world/hash 错链均失败。
- [ ] 提交：`git commit -m "feat: 实现世界规则事件流与原子提交"`。

### Task 5: 交付 Game 读模型与查询 operationId

**Files:**
- Create: `src/walnut_backend/api/routes/{bootstrap,commands,worlds,evidence}.py`, `src/walnut_backend/application/game/{bootstrap,get_command,get_run,get_world_snapshot,list_world_events,get_evidence}.py`, `tests/contract/test_game_read_operations.py`

**Interfaces:**
- Produces: `getGameBootstrap`、`getCommand`、`getRun`、`getWorldSnapshot`、`listWorldEvents`、`getEvidence`。

- [ ] 以 OpenAPI operationId 建路由，不创建旧文档的 `/api/*` 或独立 `POST /run`。
- [ ] 实现资源授权和 canonical identity 验证：path world/run/evidence ID、tenant/actor、content ref、ETag 与 `X-World-Revision` 必须与存储资源相符。
- [ ] 写合同测试，分别用全部示例、未知字段、错误 enum、错误 Location、`first_event_sequence > last_event_sequence` 和跨用户读取断言正确行为。
- [ ] 运行 `pytest tests/contract/test_game_read_operations.py -v`。
- [ ] 提交：`git commit -m "feat: 实现 Game 查询接口与世界对账"`。

### Task 6: 实现 Build、认证、Activation 的命令编排

**Files:**
- Create: `src/walnut_backend/api/routes/{skill_builds,activations}.py`, `src/walnut_backend/application/game/{create_skill_build,get_skill_build,activate_skill,get_skill_activation}.py`, `src/walnut_backend/adapters/sandbox/client.py`, `src/walnut_backend/adapters/postgres/registry.py`, `tests/integration/test_skill_lifecycle.py`

**Interfaces:**
- Consumes: `SandboxPort.compile_and_test`、`RegistryPort`、`CommandStorePort`。
- Produces: `createSkillBuild`、`getSkillBuild`、`activateSkillVersion`、`getSkillActivation`。

- [ ] 写失败测试：source bundle 超 32 文件/1 MiB、source hash 不符、未认证版本激活、幂等 key 复用不同 body、命令被接受但响应丢失后的 `UNKNOWN_COMMIT_STATE` 对账。
- [ ] 实现 `POST /v1/skill-builds` 为 “accept then worker”：先持久化 `CREATE_SKILL_BUILD`，返回合同化 202 和 command Location；worker 才调用隔离 Sandbox，随后认证或拒绝认证。
- [ ] 实现 Activation：只接受精确认证版本和 registry revision；命令完成后返回可查询的 canonical activation 资源。
- [ ] 将编译或测试失败保存为资源终态和证据，不误映射成 HTTP 500；Sandbox Adapter 只回传意图/证据，不写世界。
- [ ] 运行 `pytest tests/integration/test_skill_lifecycle.py -v`。
- [ ] 提交：`git commit -m "feat: 实现 Skill 构建认证与激活命令"`。

### Task 7: Agent Session/Turn、Sandbox Run、Evidence 与 Learner 投影

**Files:**
- Create: `src/walnut_backend/api/routes/agent_sessions.py`, `src/walnut_backend/application/game/{create_agent_session,execute_agent_turn}.py`, `src/walnut_backend/application/learner/project.py`, `src/walnut_backend/adapters/{llm/provider,postgres/learner}.py`, `tests/e2e/test_agent_turn_to_world_commit.py`

**Interfaces:**
- Consumes: `LlmPort`、`SandboxPort.run`、`LearnerPort`、`WorldUnitOfWorkPort`。
- Produces: `createAgentSession`、`getAgentSession`、`createAgentTurn`、Run、Evidence、`agent.turn.feedback_ready` 内部事件。

- [ ] 写 E2E 失败测试：skill 未激活、world revision 旧值、LLM 超时/非法输出、Sandbox 超时、World CAS 冲突、Evidence hash 不符；每种情况都保留已发生的客观阶段。
- [ ] 创建版本固定的 session；turn command 依序完成权限/registry、LLM 结构输出校验、Sandbox 执行、World UoW commit、Evidence 保存、`learner.inference.recorded`（V2 envelope + canonical inference hash）与 Learner projection。
- [ ] LLM 不可用时使用确定性 fallback，持久化 `degraded=true`、source 和 reason；不得让 Agent 直接 import ORM、直接写世界或激活 Skill。
- [ ] 只有 Run 进入合同规定的 Sandbox/World stage 时写 `command.links.run`；`NO_EFFECT` 且 run_id 为 null 时不伪造 Run。
- [ ] 运行 `pytest tests/e2e/test_agent_turn_to_world_commit.py -v`。
- [ ] 提交：`git commit -m "feat: 实现 Agent 回合运行证据与学习投影"`。

### Task 8: Product Experience API（仅在 Task 1 发布 Product Contract 后）

**Files:**
- Create: `src/walnut_backend/api/routes/product.py`, `src/walnut_backend/application/product/{content,workspace,drafts,interactions,patch_decisions}.py`, `src/walnut_backend/adapters/postgres/product.py`, `migrations/versions/003_product_workspace.py`, `tests/integration/test_product_reconciliation.py`

**Interfaces:**
- Produces: 05 文档列出的全部 Product operationId，尤其 `upsertProductSkillDraft` 与 `recordProductPatchDecision`。

- [ ] 以发布后的 Product schemas 生成/校验 DTO 与闭合响应；没有发布文件时让 CI 和启动检查明确失败，不临时从 Markdown 定义字段。
- [ ] 写测试：草稿 PUT 的 idempotency + revision/hash CAS；同一 patch 的 ACCEPT/REJECT 只能决定一次；REJECT 不改草稿；interaction/turn/command/run/evidence 任一错链均 409/合同错误。
- [ ] 实现 Workspace 只保存 Game 资源引用，不复制 World Snapshot；Patch 只接受结构化 operations，并在服务端验证 base revision/hash 后原子写入 Draft。
- [ ] 响应不确定时按 OpenAPI canonical Location 对账后再允许重放。
- [ ] 提交：`git commit -m "feat: 实现 Product 工作区草稿与 Patch 决策"`。

### Task 9: World Realtime WSS 与 HTTP 恢复链路

**Files:**
- Create: `src/walnut_backend/api/realtime.py`, `src/walnut_backend/application/realtime/{subscription,resume}.py`, `src/walnut_backend/adapters/realtime/hub.py`, `tests/integration/test_realtime_protocol.py`

**Interfaces:**
- Consumes: AsyncAPI frame schemas、EventStorePort。
- Produces: `/v1/realtime` 的 subscribe/resume/ack/heartbeat_ack 和 WorldEvent/subscribed/heartbeat/error。

- [ ] 写协议测试：Upgrade Header/子协议错误、订阅前 event、未知 frame/event version、重复 event、乱序、gap、ACK 非连续 sequence 及各致命 close code。
- [ ] 实现只推送已提交 WorldEvent；内部 `runtime.events.{stream_id}` 与客户端 WSS 必须为不同 channel，`agent.turn.feedback_ready` 不得直接下行。
- [ ] 实现 at-least-once：以 event_id 去重、只 ACK 已连续且已持久应用的最高 sequence；gap 用 HTTP events 回补，无法闭合再用 snapshot 恢复。
- [ ] 提交：`git commit -m "feat: 实现世界实时订阅与断线恢复"`。

### Task 10: 飞书 Integration API、Outbox 适配器与报告草稿

**Files:**
- Create: `src/walnut_backend/api/routes/feishu.py`, `src/walnut_backend/application/feishu/{webhook,releases,queries,reports}.py`, `src/walnut_backend/adapters/feishu/client.py`, `tests/integration/test_feishu_integration.py`

**Interfaces:**
- Produces: `receiveFeishuWebhook`、content release、approval decision、learner/class query、report job 与脱敏 Evidence 的全部已发布 operationId。

- [ ] 写测试：Webhook 签名、timestamp/nonce 重放、各 OpenAPI 指定的幂等 scope、Service Bearer、角色授权、Evidence 脱敏与 audit、错误 receipt/dedupe/report/attempt。
- [ ] 实现飞书写操作仅经 `OutboxPort -> DeliveryPort`；`APPROVE` 只能推进候选验证，报告只能是 `DRAFT_ONLY`，均不写 World/不触发真实发送。
- [ ] 对外 SDK 异常映射 error catalog；worker 重试遵循 Retry-After、lease 与 DEAD_LETTER 状态。
- [ ] 提交：`git commit -m "feat: 实现飞书集成与报告草稿投递"`。

### Task 11: 完整质量门禁、部署与联调验收

**Files:**
- Create: `docker-compose.yml`, `Dockerfile`, `.github/workflows/verify.yml`, `scripts/verify_all.ps1`, `tests/e2e/test_learning_closed_loop.py`, `docs/operations/runbook.md`

**Interfaces:**
- Consumes: 所有已固定合同和全链路服务。
- Produces: 可复现本地/CI 验收与故障恢复证据。

- [ ] 写端到端测试：Content → Workspace → Draft → Build → Certification → Activation → Session/Turn → Run → World receipt/events/snapshot → Evidence → Interaction → Patch decision → next Build，并在每一步断言身份、revision、sequence、hash。
- [ ] 增加合同正反例、Python Schema 差分、真实 localhost HTTP、PostgreSQL 中断、worker 重启、Outbox 重放、Sandbox/LLM/飞书超时取消、WSS resume/gap recovery 测试；在锁定 tag 上运行上游 `npm run verify` 与 `npm run port-surface:check`。
- [ ] `verify_all.ps1` 必须顺序运行合同 release 检查、Alembic upgrade、Ruff、Pyright、pytest（unit/contract/integration/e2e）；缺依赖或缺 Product Contract 必须失败，不能 skip。
- [ ] 在 Docker Compose 启动 PostgreSQL、backend、worker、Sandbox；将生产 secrets 只经环境/secret manager 注入，日志不输出 token、源码或敏感 Evidence。
- [ ] 提交：`git commit -m "feat: 建立后端全链路验收与部署门禁"`。

## 实施完成定义

- 每个公开 operationId 都只有一个 handler/use case，并以当前固定合同的 schema、错误目录和示例验收。
- 05 文档要求的幂等、CAS、异步 Command 对账、唯一世界写入、WSS 恢复、Product Patch 身份闭合和飞书状态边界均有自动化负例。
- `python scripts/verify_contract_release.py`、`ruff check src tests`、`pyright src`、`pytest` 与全链路 `scripts/verify_all.ps1` 在干净环境成功。

## 计划自检

- 01—04 的核心闭环（C++ Skill 改变确定性世界、Agent 教学、Learner、单一飞书助手）由 Task 4、6、7、10 覆盖。
- 第 05 份的接口/契约优先级、身份、幂等/CAS、Command、WSS、Product、飞书、Port 和测试门禁分别由 Task 1—11 覆盖。
- 当前合同实际缺失 Product Experience 文件已作为显式阻塞项；计划不会以旧 `/api/*`、同步 Run 或任意 Patch 文本绕过它。
