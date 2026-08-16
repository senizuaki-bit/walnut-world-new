# 核桃代码世界 Agent Teaching 与 Learner Model

本仓库是 INT1/INT2 的 Agent 能力与合同仓：发布不可变 Wire 合同、Ports、provider-neutral Runtime、确定性教学策略、Build/CAS 和 Sandbox 库。`TeachingDirective` 由可验证的 Task、Run、Evidence、连续同类失败计数和 Learner revision 决定；模型只能在该边界内生成反馈与 inference 候选。

生产产品面由 sibling `walnut-world-backend` 独占：它是唯一公开 HTTP Gateway、唯一 PostgreSQL/Alembic 写入与迁移权威；当前工作树的线性 migration head 为 `019_int2_skill_patch_authority`，父修订为 `018_world_presentation_events`。Gateway 不代理本仓库的历史 `yaya_agent_backend` HTTP 服务，也不读取其 `yaya_*` 私表。所有权决策见 [INT1 单一 Gateway ADR](docs/ADR-INT1-SINGLE-GATEWAY-OWNERSHIP.md) 与 [INT2 权威动作/显式 Patch ADR](docs/ADR-INT2-AUTHORITATIVE-WORLD-AND-EXPLICIT-SKILL-PATCH.md)。

历史 INT1 正式链为 `Student Bootstrap → server-created Session + starter Draft/Workspace → Draft CAS → pinned-Docker Build/Certification → full-scope Activation CAS → original Session exact-version Turn → Run/World/Event/Snapshot/Evidence → Learner/Interaction/Workspace → Godot recovery/display`。2026-08-13 的 194.12 秒 DeepSeek V4 Flash 运行是 **historical INT1 real-Provider / host-Docker evidence**，不是 INT2 当前树或 production private DinD 的通过证据；公开 Gateway pending write response-loss 也仍为 `NOT_PROVEN`。见 [INT1 三仓验证报告](docs/INT1_CROSS_REPOSITORY_VALIDATION_REPORT.md)。

INT2 当前事实：v0.4 继续按已发布基线逐字节锁定；当前三仓工作树消费 additive v0.6 candidate（147 entries、27,848-byte manifest、SHA-256 `11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05`），但 `refs/tags/agent-contracts-v0.6.0` 尚不存在，发布身份为 `NOT_PROVEN`。当前 Agent full discovery 为 601：2 条真实 Provider opt-in 精确 `EXCLUDED_NOT_RUN`，其余 599/599 non-live 全部通过且 0 skip；Backend current-tree full 为 468/468、0 failure/error/skip；Frontend offline 为 60/60、另有 2 条真实 E2E opt-in 精确排除。正式 deterministic Gateway/Godot M2 actual10 已 PASS。受控真实 Provider M2 也已由 2026-08-15 run `868a` 在 301.012 秒取得 PASS：`source=provider`、`degraded=false`，18 unique dispatch / 18 generation、单 dispatch 最大 generation 1，Provider relay response-loss 复用同一 dispatch 且 generation 仍为 1；学生可见、可确认 Patch 为 `PUBLIC_UI_CHAIN_CLOSED`，World commit 1、8 条 presentation event 与第二 Godot 进程 17 GET / 0 mutation 均闭合。该 live 不改变默认 flag，也不证明 production private DinD 或公开 Gateway pending write response-loss；两者仍为 `NOT_PROVEN`。Patch capability 继续默认关闭并由 Backend capability 与 Frontend 本地 flag 双重收紧；WSS、Client Event Batch、Feishu、自动接受/应用/Build/Activate/Run、多文件 Patch 与通用动画平台仍明确排除。

运行链路为：

```text
POST /v1/agent-sessions/{session_id}/turns
  -> 202 AcceptedGameJob
  -> walnut-world-backend combined durable workflow worker
  -> 根执行阶段取得或创建幂等 invocation receipt
  -> pinned Docker 隔离的真实 C++ Sandbox
  -> Run + Evidence；成功时 World CAS + Event + Outbox
  -> 从 canonical Run 权威派生 run_failed / task_completed
  -> 精确计算同 Session、同 Skill、同 failure_key 的连续失败次数
  -> 前两次失败 teaching_agent；第三次 bug_agent；成功 book_agent
  -> ContextBuilder + TeachingDirective-bound SharedAgentRuntime
  -> 可恢复 Relay 后的真实 Provider（source=provider, degraded=false）
  -> 唯一公开 AgentTurn / Message / Product AgentInteraction
  -> learner.inference.recorded + projection job/outbox + committed fence
  -> Product list/get HTTP + canonical closure + restart recovery
  -> Backend-owned Learner projection in the terminal workflow
  -> Learner snapshot revision +1 + source receipt
  -> learner.model.updated + Outbox
  -> next ContextBuilder consumes the new task-scoped profile
```

同一个请求字节和 `Idempotency-Key` 会重放首次 202 receipt；同一 `invocation_id` 会先读取持久化 receipt。生产 Provider 只接受启动时 capability fail-fast 的 recoverable relay：以稳定 `dispatch_id` GET-first，只有线性一致的 `ABSENT` 才同 ID PUT；`PENDING` 遵守 `Retry-After`，接管时联合校验 fence、PostgreSQL receipt、relay completion/raw bytes hash，禁止退回普通 chat-completions POST。Build/Sandbox 也以稳定容器身份、inspect/logs 和持久结果恢复 response loss。历史INT1 live与当前focused合同共同覆盖相应边界，但不替代INT2 full/formal。Runtime仅依赖合同Port，不依赖ORM、数据库驱动、模型SDK或具体World实现。

## 历史 Agent 单仓已验证的 Product 交付边界

历史 Agent 单仓完整门禁已验证冻结 Product 合同中的两个只读 operation：

- `listProductAgentInteractions`
- `getProductAgentInteraction`

`listProductAgentInteractions` 使用必填 `after_sequence`、默认 `limit=50`、session 内连续 sequence 和单次稳定 high-watermark；响应只返回 `X-Interaction-High-Watermark`。`getProductAgentInteraction` 返回与完整 projection 内容绑定的强 quoted `ETag` 和严格等于 body revision 的 `X-Interaction-Revision`。两者都从认证主体与独立 PostgreSQL authority 闭合 Session、Interaction、source Agent Turn、Command、可空 Run、Evidence、feedback event 和 projection outbox；body 保留资源 origin context，本次 request/trace/correlation 只在响应 Header 回显。

读取成功、失败、重试和重放均不调用 Provider、Sandbox 或 World，也不写 Message、Interaction、Event、Outbox、receipt、audit、Command、World 或 Learner revision。三个 attempt identity Header 无法诚实建立时返回 bare 400；attempt 有效后，Product 闭合错误矩阵为请求/ahead-of-tip `400 INVALID_REQUEST`、认证 `401`、授权策略 `403`、作用域内不存在 `404`、Schema 版本 `409 SCHEMA_VERSION_UNSUPPORTED`、限流 `429 RATE_LIMITED`（必带 `Retry-After`）、投影 gap/hash/identity 损坏 `500 INVARIANT_VIOLATION`、依赖不可用 `503 DEPENDENCY_UNAVAILABLE`。当前仍保持 `allow_skill_patch=false`。

## A6 Bug／书书生产链历史验收边界

历史 Agent 单仓 A6 曾使用 composite HTTP、durable Command/Job、Worker claim/lease/fencing、真实 Provider、pinned Docker C++ Sandbox、PostgreSQL、Learner Worker 和 Product list/get 完成验收。同一身份闭包下的三个不同真实失败 Run 具有相同 canonical `failure_key`，失败不推进 World revision；前两次确定性选择 `teaching_agent`，第三次选择 `bug_agent` 并生成 `RECTIFICATION`。真实成功由 World 规则客观计算，World revision 精确 `+1` 并生成 hash 闭合的 `WORLD_COMMIT` Evidence，随后选择 `book_agent` 并生成 `SUMMARIZATION/growth_summary`。两类被接受的 Provider 结果均要求 `source=provider`、`degraded=false`，且 `patch_eligible=false`、`full_solution_eligible=false`。该旧 composition 不包含当前唯一 Backend Gateway 与 recoverable relay，不能作为 INT1 三仓 live PASS。

E2E 设置阶段只预置完成运行所需的 Task、初始 World、Session，以及同 Session、同一 Skill 的两版 canonical Certified Skill 权威，并绑定当前执行版本；它不预置 Run、Evidence、AgentDecision、Message、Interaction、feedback Event、LearnerInference 或 Learner revision。因此该验收是“初始权威已设置后的角色生产链”，不是“空账号到运行”的完整学生前门旅程。

INT1交付能力已装配Student Bootstrap、ContentUnit、SessionWorkspace、SkillDraft、Build/Certification、Activation、exact-version Turn与terminal projections；当前INT2在此基础上增加presentation与默认关闭的Patch focused能力。Certification仍是Build终态，不存在独立公共Certification operation。

## A8 前置公共链（历史 Agent composition）

```text
published Task/Content + initial World/Learner/Profile authority
→ GET Bootstrap
→ POST/GET AgentSession
→ Product SkillDraft GET/PUT revision+hash CAS
→ Game SkillBuild with complete source_bundle
→ pinned Docker C++20 + versioned public/hidden tests
→ successful Build terminal creates Artifact + SkillVersion + immutable Certification + Evidence
→ full-scope Activation registry CAS
→ exact-version Agent Turn in the original Session
→ existing Run / World / teaching / Bug / 书书 / Learner / Product-read chain
```

Game Build 使用请求中的完整 source bundle，服务端自行计算 hash 并记录 `client_draft_revision`；它不得读取“最新 Draft”或隐藏依赖 Product repository。失败 Build 只保留可查询的 phases/diagnostics 终态，不生成 Artifact、SkillVersion、Certification、Activation、Registry 或伪造的 Certification Evidence。

## 权威顺序

实现和测试严格按以下顺序裁决：

1. `contracts/manifest.json` 与 OpenAPI / AsyncAPI / JSON Schema
2. Python Ports 与 `contracts/port-surface.json`
3. `docs/CONTRACT_RULES.md`
4. `05_核桃代码世界_接口对齐与联调规范.md`
5. `01`—`04` 的历史示例

当前工作树的追加式合同 descriptor 为 `@yaya/agent-contracts` v0.6.0 candidate：manifest 27,848 bytes、147 files、SHA-256 `11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05`。v0.3/v0.4/v0.5 release locks 分别校验已冻结历史字节；v0.5 只追加 World presentation，v0.6 只追加 INT2 capability。已发布的 v0.4 annotated tag 保留为历史兼容证据；v0.6 tag 尚不存在，不能把 candidate 写成正式 release PASS。

## 目录

```text
contracts/                    冻结 Wire 合同、错误目录与 Manifest
python/yaya_agent_contracts/  供应商无关的领域模型和 Port
python/yaya_agent_build/      供应商无关的 pinned Docker Build 与 Artifact CAS 库
python/yaya_agent_sandbox/    供应商无关的 pinned Docker/native Sandbox Adapter
python/yaya_agent_runtime/    Router、Context、Tool、Runtime、Hub
python/yaya_agent_backend/    历史 A8 兼容/回归 composition；不是 INT1 生产 Gateway
python/yaya_agent_backend/migrations/  历史 Agent 私有 migration；生产不运行
tests/                        单元、真实 PostgreSQL、Docker C++、HTTP 与 live E2E
docs/                         合同、部署和故障恢复规则
```

## 生产依赖

- Node.js 20+
- Python 3.12
- PostgreSQL 15+
- Docker；生产 Sandbox 镜像必须使用 `name@sha256:<64 hex>`
- Godot 4.5.2（统一合同门禁）
- Windows 开发门禁还需要 Visual Studio 2022 C++ Build Tools；原生 Sandbox 只用于证明其不具备生产隔离能力，composition 永远只装配 Docker Sandbox

安装锁定依赖：

```powershell
npm ci --ignore-scripts
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
$env:YAYA_PYTHON_EXE = (Resolve-Path .\.venv\Scripts\python.exe)
```

## 生产启动

不要从本仓库启动第二个产品 HTTP 或 migration 进程。生产启动、唯一 Gateway、当前 Alembic head `019_int2_skill_patch_authority`、durable workers、recoverable relay 与 pinned Docker 配置均在 sibling `walnut-world-backend` 的运行文档中。生产只读取受控 relay endpoint/credential 与 Provider/model 标识；普通 direct endpoint 配置不足以启动 worker。Relay credential 只从受控进程环境或 secret 文件注入；不得进入仓库、fixture、日志或报告。

本仓库只为 Backend 提供可安装/导入的 contracts、Runtime、Build 与 Sandbox 能力，并保留历史 Agent composition 的回归测试。历史启动说明只用于重放旧 A6/A8 证据，不属于 INT1 部署拓扑。

## 验证

Agent 分仓门禁：

```powershell
npm run verify
```

历史 INT1 non-real-Provider full 为 discovered 576、排除 2 个 live opt-in、574/574 passed；随后 595-test、31-error + 5-failure 的 INT2 运行保留在[最小红诊断](docs/INT2_AGENT_FULL_NON_LIVE_RED_DIAGNOSTIC.md)中，作为 verifier 修复前的历史 RED，不回写或篡改。修复后的当前 full 已重新发现 601 条用例：2 条 billable live opt-in 为 `EXCLUDED_NOT_RUN`，599/599 non-live PASS，0 skip；脱敏 stdout SHA-256 为 `346666cb194561079fc280059b070da3e9a13e6c34666ae06cf869deca2740de`。这证明 Agent 当前 full non-live 门禁；独立计费门禁的当前证据是 INT2 real-Provider run `868a` PASS，详见[跨仓验证报告](docs/INT2_CROSS_REPO_VALIDATION_REPORT.md)。

重点故障门禁覆盖 7/8 未完成、8/8 仅一次 CAS、连续失败计数或 `failure_key` 漂移、跨 Session/Skill/actor/content/world 历史、重复 Run/Turn/Command、错误成功/失败声明、伪造 Evidence、Provider role/phase/response/永久掌握越界、无限循环清理、数据库中断、租约接管、旧 fencing token、响应丢失、`UNKNOWN_COMMIT_STATE`、Outbox takeover 和 Worker 重启。角色输入或输出不可信时在 Provider 或公开写入前 fail closed，不能用 degraded/fallback 冒充 A6 通过。

## 运维文档

- [INT1 单一 Gateway 与组件所有权 ADR](docs/ADR-INT1-SINGLE-GATEWAY-OWNERSHIP.md)
- [Agent Runtime](docs/AGENT_RUNTIME.md)
- [合同规则](docs/CONTRACT_RULES.md)
- [部署说明](docs/AGENT_TURN_DEPLOYMENT.md)
- [故障恢复](docs/AGENT_TURN_FAILURE_RECOVERY.md)
- [Learner Projection 部署](docs/LEARNER_PROJECTION_DEPLOYMENT.md)
- [Learner Projection 故障恢复与重建](docs/LEARNER_PROJECTION_FAILURE_RECOVERY.md)
- [A8 前置学生源码公共生产链权威与证据矩阵](docs/A8_PUBLIC_STUDENT_SKILL_CHAIN.md)
- [A8 Agent 单仓历史验证报告](docs/A8_VALIDATION_REPORT.md)
- [INT1 三仓历史验证报告](docs/INT1_CROSS_REPOSITORY_VALIDATION_REPORT.md)
- [INT2 当前跨仓验证报告](docs/INT2_CROSS_REPO_VALIDATION_REPORT.md)
- [INT2 目标—实现—验收矩阵](docs/INT2_TARGET_IMPLEMENTATION_GAP_ACCEPTANCE_MATRIX.md)
- [本轮原始 Goal 说明（历史）：Product AgentInteraction 只读生产纵切](docs/NEXT_GOAL_PRODUCT_INTERACTION_READ.md)

本仓库为私有专有工程，包元数据为 `UNLICENSED`。
