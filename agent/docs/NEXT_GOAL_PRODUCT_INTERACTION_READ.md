# 下一轮 Goal：Product AgentInteraction 只读生产纵切

> 历史归档：此 Goal 已被 INT1 单 Gateway 收敛取代。正文中的 `yaya_agent_backend serve` 只描述当时 Agent 单仓目标，不得作为当前部署说明；当前 production owner 是 sibling `walnut-world-backend`。

下面正文可直接作为下一轮 `/goal` 提示词使用。

---

要使用至少两个子代理进行相互独立的只读审查：一个审查 Product 合同、身份和错误语义，一个审查 PostgreSQL 并发、损坏与恢复测试矩阵；子代理不编辑文件。主代理负责实现、运行自动化测试，并亲自复核全部审查结论、代码差异和测试结果。代码必须保持模块化，并用真实 Agent Turn 产生的持久化 Interaction 跑通最终链路。

在本仓库中完成“Product AgentInteraction 只读生产纵切”。

本轮只实现以下两个冻结 operation：

- `listProductAgentInteractions`
- `getProductAgentInteraction`

本轮不包含：

- Product ContentUnit、SessionWorkspace 或 SkillDraft GET/PUT；
- Skill Patch 生成、Product SkillPatch 映射或 PatchDecision；
- Game Bootstrap、Build、Certification、Activation 或 Session 新入口；
- Godot 页面、Product Gateway 或确认 UI；
- World Realtime WSS；
- Bug/书书新能力；
- 飞书助手；
- LangGraph、MCP、Central Lane 或新的 Memory 系统；
- 修改任何已冻结 Wire 合同。

开始前完整阅读：

- `01_核桃代码世界_系统架构与技术实现方案.md`
- `03_核桃代码世界_后端开发文档.md`
- `04_核桃代码世界_Agent开发文档.md`
- `05_核桃代码世界_接口对齐与联调规范.md`
- `README.md`
- `docs/CONTRACT_RULES.md`
- `docs/INTERFACE_INTEGRATION_GUIDE.md`
- `contracts/manifest.json`
- `contracts/openapi/product-experience.openapi.json`
- `contracts/schemas/product-experience/agent-interaction.schema.json`
- `contracts/schemas/product-experience/agent-interaction-page.schema.json`
- `contracts/schemas/product-experience/product-error-responses-by-status.schema.json`
- `contracts/error-catalog.json`
- `contracts/port-surface.json`
- `python/yaya_agent_contracts/ports.py`
- `python/yaya_agent_backend/http_api.py`
- `python/yaya_agent_backend/application.py`
- `python/yaya_agent_backend/repositories.py`
- `python/yaya_agent_backend/migrations/0001_agent_turn.sql`
- 现有 Agent Turn、HTTP、Repository、live E2E 和 Product contract 测试。

裁决顺序必须是：

`contracts/manifest.json` 与具体 OpenAPI/JSON Schema
> Python Ports 与 `port-surface.json`
> `docs/CONTRACT_RULES.md`
> `05_核桃代码世界_接口对齐与联调规范.md`
> 01、03、04 中的目标说明和示例。

当前事实：

- AgentTurnCommit 已在同一事务中写入合同化 `AgentInteraction`、持久化表和 Product projection outbox；
- `yaya_agent_interactions` 已有 tenant、session、interaction 和连续 sequence 权威；
- 当前生产 HTTP 只暴露 Game Agent Turn 与部分 Game GET；
- live E2E 仍直接查询 PostgreSQL 验证 Interaction，客户端没有正式 Product 读取入口；
- 当前所有 Patch eligibility 和 Role Config 必须继续保持关闭。

目标链路：

```text
真实 Agent Turn
→ AgentTurnCommit 原子写入 yaya_agent_interactions
→ Product read Application
→ Product HTTP Adapter
→ listProductAgentInteractions / getProductAgentInteraction
→ 出站 Schema + 语义 + canonical row 身份校验
→ 客户端获得可恢复、可对账的同一份 Product AgentInteraction
```

必须完成：

1. 建立独立 Product 读取边界。

- Product Application、Repository 和 HTTP inbound adapter 必须与 Game Agent Turn 逻辑分层；
- 可以与 Game API 部署在同一进程，但不得把 Product path、分页或错误语义硬塞进现有 Game adapter；
- Product 路由必须装入正式 production composition，并能通过 `python -m yaya_agent_backend serve` 到达；测试专用 Adapter 或独立测试服务器不算完成；
- Handler 只负责认证、合同解析、调用应用层和合同化响应；
- 不允许 ORM row 或任意字典直接作为 Wire DTO 返回。

2. 实现 `listProductAgentInteractions`。

严格执行冻结合同中的：

- `session_id` path 绑定；
- `after_sequence` 与默认/显式 `limit`；
- session 内严格递增、无 gap 的 interaction sequence；
- 单次查询使用稳定 high-watermark；
- `requested_after_sequence`、`requested_limit` 回显；
- `next_after_sequence`、`has_more` 与空页不推进；
- `X-Interaction-High-Watermark` 与 body 完全一致；
- page `request_context` 保留 canonical Session 的创建来源上下文，不得用本次 GET 的 request/trace/correlation Header 覆盖；本次 attempt identity 只在响应 Header 回显；
- ahead-of-tip 明确返回合同规定的 `400 INVALID_REQUEST`；
- 不得使用会在并发写入下漂移的普通 offset pagination。

写端当前使用无锁 `MAX(sequence)+1` 会破坏上述连续性。本轮必须在分配 Interaction sequence 之前取得 `(tenant_id, session_id)` 级数据库锁，并把锁、sequence 分配、Interaction、receipt/event/outbox 写入保留在同一事务中；用两个并发真实 Agent Turn 证明 sequence 唯一、连续且无失败重试遗留。这是只读接口成立所必需的最小写端完整性修复，不得扩展成 Product 写 API。

3. 实现 `getProductAgentInteraction`。

- path `session_id`、`interaction_id` 必须与记录、body 和 `links.self` 一致；
- 返回合同声明的强 quoted `ETag`，且与闭合 projection 内容一致；projection 任一受保护字段变化时 ETag 必须变化；
- 返回 `X-Interaction-Revision`，并严格等于 body `interaction_revision`；list 只返回 `X-Interaction-High-Watermark`，不得混用这两个资源级 Header；
- body `request_context` 保留 Interaction 首次创建时的 origin context，本次 GET attempt identity 只能在响应 Header 中回显；
- Interaction 内嵌 feedback、SkillPatch 和 PatchDecision 的身份必须按 Schema 和语义门禁闭合；本轮正常生产数据中 Patch/Decision 仍为 `null`；
- 不存在资源返回合同化 404；存在但不属于当前 actor/session 的资源不得泄露。

4. 闭合认证与资源权威。

- 只信任认证后的 tenant、actor_id、actor_type 和 roles；
- 请求 body/query/path 不能覆盖认证身份；
- 以彼此独立的 canonical PostgreSQL 事实为锚闭合 tenant、actor、content、session、interaction、turn、command、run 和 Evidence identity；不能只比较同一 JSON 内部的两份值；
- 校验 canonical `agent.turn.feedback_ready` 完整 envelope 与 payload byte-equivalence、`feedback_event.feedback_sha256`、source Agent Turn 的 `event_sha256`、`projection_source.source_sha256`、CommittedAgentTurn 的 validated decision/source receipt、Interaction row、Session、Command、可空 Run、Evidence、Task hint cap 和时间关系；不得把 source GameEvent hash 冒充 feedback-ready event 的独立 hash；
- 合同允许 `feedback.run_id=null`。读取必须支持合法无 Run Interaction，不能为了统一路径伪造 Run 或 Evidence；
- 投影或 receipt 相对任一独立 canonical anchor 漂移时必须失败；不声称能够检测所有权威表被同时重写后的共谋篡改；
- 跨 tenant、actor、content 或 session 的读取必须 fail closed。

5. 严格实现 HTTP 与错误合同。

- Bearer、`X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id`、`X-Schema-Version` 按 Product OpenAPI 校验；三个 attempt identity Header（request/trace/correlation）任一缺失、重复或非法时不存在可诚实回显的身份，沿用现有 HTTP 边界返回 bare `400`（空 body、`Content-Length: 0`、`Cache-Control: no-store`、`Connection: close`），不得伪造 Header 或闭合 ErrorResponse；`X-Schema-Version` 缺失或值不等于 `1.0.0` 时，在 attempt identity 有效的前提下返回 `409 SCHEMA_VERSION_UNSUPPORTED`，重复 singleton Header 属于 `400 INVALID_REQUEST` framing failure；
- GET 不接受 body；未知 query、重复 query、越界数字、未知 path 和不支持 method 必须显式失败；
- 有效 attempt identity 建立后，后端按 request parse → authentication/authorization → canonical read → outbound Schema/语义校验 → encode 的顺序执行；响应 Header 回显本次 attempt，body `request_context` 保留资源 origin；
- 有效 attempt identity 下使用闭合 `ErrorResponse` 和 `error-catalog.json`，不返回 traceback、任意 SDK 错误或 `200 {"success": false}`；错误 schema version 固定为 `409 SCHEMA_VERSION_UNSUPPORTED`；projection sequence gap、hash 或 identity 损坏固定为 `500 INVARIANT_VIOLATION`，不得返回 Product 409 闭集不允许的 `EVENT_SEQUENCE_GAP`；
- 401 用于缺失/非法 Bearer；403 只用于已认证主体被现有资源授权策略拒绝；404 用于认证作用域内不存在的 Session/Interaction，且 tenant-scoped 查询不得泄露跨租户存在性；429 必须带合同要求的 `Retry-After`；
- 读取依赖不可用时使用合同声明的 503，不构造缓存或伪造成功。

6. 保证读取无副作用。

两个 GET 在成功、失败、重试和重放时都不得：

- 调用 LLM；
- 调用 Sandbox 或 World；
- 写任何数据库记录，包括 Message、Interaction、Event、Outbox、receipt 或 audit；这两个 GET 没有 `x-audit-access: true`，不得自行增加审计写入；
- 推进 Command、World 或 Learner revision；
- 修复、覆盖或跳过损坏的持久化投影。

损坏的 Interaction、sequence gap、hash/identity 漂移必须 fail loud，不能在读路径静默自愈。

7. 保持合同与发布边界。

- 现有 Product OpenAPI 和 JSON Schema 已足以表达目标，默认不得修改 `contracts/**`、Manifest、Port surface、package version 或现有合同 tag；
- 如果实现时发现冻结合同确实不可执行，立即停止，给出最小版本化变更提案，不得偷偷增加字段、Header、错误码或放宽 `additionalProperties`；
- 不得因为实现了两个 GET 就宣称 Product REST、Godot UI、WSS 或 Skill Patch 已完整交付。

测试要求：

一、单元与合同投影测试

必须覆盖：

- list/get path、query、Header 和 method；
- 默认 limit、最小/最大 limit、空页和 ahead-of-tip；
- `has_more`、`next_after_sequence` 与 high-watermark；
- list 的 `X-Interaction-High-Watermark`，以及 get 的强 ETag、`X-Interaction-Revision == body.interaction_revision`；
- unknown field、缺字段、错枚举和坏 JSON；
- Product ErrorResponse 与 error catalog；
- 出站 Schema 和跨字段语义校验。

二、真实 PostgreSQL 集成测试

必须覆盖：

- 一个 session 的 sequence 1..N 连续分页；
- 并发插入时单页 high-watermark 稳定；
- interaction_id 与 session 错链；
- 跨 tenant、actor、content、session 读取；
- canonical event、source receipt、CommittedAgentTurn、Run/Evidence、Feedback、Task hint cap、timestamps 和 links 的独立锚漂移；
- 合法 `run_id=null` Interaction 的 list/get，不得伪造 Run/Evidence；
- 数据库约束拒绝重复 sequence；read path 对 sequence gap 和损坏但可解码 projection fail loud；
- 两个并发真实 Agent Turn 在同一 tenant/session 下分配唯一连续 sequence；
- 事务重启后返回完全相同的 canonical resource；
- 查询失败不产生任何业务写入。

所有数据库测试必须使用真实 PostgreSQL，不得用 SQLite 或内存实现替代。

三、真实 localhost HTTP 测试

必须覆盖：

- 两个合法 GET；
- 缺失/非法认证；
- 缺失、重复和非法 attempt identity 的 bare 400 transport rejection；有效 attempt identity 下的错误必须返回闭合 JSON 与三个回显 Header；
- 非法 query、GET body、错误 method、错误 status/header；
- 两个 operation 声明的完整 400、401、403、404、409、429、500、503 错误矩阵；至少明确验证 schema version → 409、投影损坏 → 500、依赖不可用 → 503、rate limit → 429 + `Retry-After`；当前路径不可达的状态也必须由共享边界单元测试证明，不得静默遗漏；
- 响应出站校验失败时不得把不可信 body 发送给调用方。

错误 status/Header/body 组合和出站畸形响应通过 Adapter 单元故障注入验证；不得要求真实 localhost 服务依靠非法业务请求伪造一个不可能的下游坏响应，也不把本轮排除的 Product Gateway 客户端校验混入后端验收。

四、最终真实 E2E

禁止 Mock Server、scripted provider、fake success 或预置 Interaction。

扩展现有真实 Agent/Learner live E2E：

```text
真实 OpenAI-compatible Provider
+ PostgreSQL
+ Agent Worker
+ Docker C++ Sandbox
+ World CAS
+ AgentTurnCommit
→ 产生真实 Product AgentInteraction
→ Product list GET 从 after_sequence=0 发现该 Interaction
→ Product single GET 读取同一 canonical Interaction
→ 重启 HTTP/Application 后读取结果不变
→ 重放原 Agent Turn 后 Interaction ID/sequence 集合及数量完全不变，不新增重复 Interaction
→ 另一个真实无 Run Agent Turn 产生 run_id=null Interaction，list/get 不伪造 Run 或 Evidence
```

允许为本轮范围外的 Task、Session、World 和已认证 Skill 建立测试 authority fixture，但不得直接插入、伪造或预计算 `yaya_agent_interactions`。

E2E 必须证明 list/get 不新增：

- Provider 调用；
- Sandbox 执行；
- World CAS；
- Message、Interaction、Event 或 Outbox；
- Learner receipt 或 revision。

所有依赖缺失必须失败，禁止 skip。

子代理要求：

1. 一个只读审查 Product OpenAPI、分页、高水位、Header、ETag、错误和身份闭包。
2. 一个独立只读审查 PostgreSQL writer lock/并发、跨租户、损坏投影、重启和 read-side-effect 测试矩阵。

主代理必须亲自读取合同、复核每个发现、检查实现差异并重新运行全部测试，不能直接接受子代理结论。

文档要求：

- 更新 README 的当前交付能力与明确排除项；
- 同步根目录 05 与 `docs/INTERFACE_INTEGRATION_GUIDE.md` 的交付快照；
- 记录 Product Interaction 分页、high-watermark、ETag、恢复和故障语义；
- 如实列出仍未交付的 Bug/书书真实验收、Draft/Build/Certification/Activation/Session、A8 Patch、Godot、WSS 和飞书能力；不得在本轮实现或提前开放它们。

完成标准：

- 两个 Product GET 已装入生产 composition，可通过 `python -m yaya_agent_backend serve` 启动并访问；
- 已持久化 Interaction 可经正式 HTTP 被发现、读取和跨重启恢复；
- 所有认证、身份、分页、Header、Schema、损坏投影和故障负例通过；
- 保留现有 SQL 原子性、event/outbox/receipt 身份闭包断言，同时新增 Product HTTP 作为客户端验收；SQL 不再替代 HTTP 读取断言；
- 无未说明 Mock、TODO、静默降级和测试跳过；
- `npm run verify` 全部通过；
- 准确报告 Node、Python、Godot、TypeScript、Pyright、Ruff、PostgreSQL、真实 Provider E2E 结果；
- 不能把本轮结果表述为完整 Product REST、UI、WSS 或 A8 Skill Patch。

Git 安全边界：

- 只在当前 workspace 实现和验证；
- 未经用户在当轮明确授权并给出精确 repository、remote 与 branch，不得 commit、push、创建 PR 或移动/重建合同 tag；
- 若用户另行授权发布，先报告 Git root、remote、目标分支、基线 SHA 和工作树差异，再执行相应的非强制推送流程。
