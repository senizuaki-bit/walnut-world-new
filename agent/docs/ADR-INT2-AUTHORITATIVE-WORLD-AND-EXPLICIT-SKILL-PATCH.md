# ADR：INT2 权威动作演出与学生确认的 Skill Patch

- 状态：Accepted；formal deterministic M2/full/recovery actual10 与受控 real-Provider M2 均已 PASS；默认 capability 仍关闭
- 日期：2026-08-14（Asia/Shanghai）
- 取代范围：不取代 INT1 所有权 ADR；只追加 INT2 的展示投影和 Patch 决策边界

## 背景

INT1 已建立唯一 Product Gateway、Backend-owned PostgreSQL/Alembic、durable workflow/learner worker、recoverable Provider relay、正式 Godot AppRoot 和 HTTP World 恢复。本 ADR 形成时，正式客户端仍在成功 Run 后直接投影最终 Snapshot；冻结 v0.4 World 流每个 world revision 只有一条 `world.committed` 聚合事件，并强制 `world_revision == event.sequence`，无法表达一次提交内的逐动作演出。该句是 historical pre-implementation background；当前 M1 formal deterministic 已完成独立 presentation 演出。

v0.4 已包含 `SkillPatch` 与 `PatchDecision` 的宽合同。本 ADR 形成时 production route 未挂载，旧 dormant ACCEPT 更新可变 Draft head且缺少稳定 provenance；这些是 historical pre-implementation facts。当前 Backend 已有 019 migration、不可变 Draft/provenance 与按 flag 条件挂载的实现；formal deterministic M2/full/recovery actual10 与受控 real-Provider M2 均已 PASS，但这不代表默认打开，Backend 与 Frontend 的相关默认 flag 仍为 `false`。

## 决策一：所有权保持 INT1 单一权威

| 边界 | 唯一 owner | INT2 决策 |
|---|---|---|
| 公开产品 HTTP、Feature capability、Command/Job/Receipt | `walnut-world-backend` | Godot 仍只访问唯一 Gateway；不新增 Agent 产品服务 |
| 产品 Draft/Patch/Build/World/Evidence/Learner 事务、Schema、迁移 | `walnut-world-backend` + PostgreSQL | 新表只沿当前 Alembic head 线性追加；PostgreSQL 是唯一持久权威 |
| Agent 角色、教学策略、结构化候选生成与只读校验 | `agent` library | Agent 只能提案；无 Draft/Build/Activation/World 写口 |
| Wire 合同及兼容锁 | `agent/contracts` | v0.4 全文件字节冻结；World presentation 在 v0.5 追加，Patch capability 在 v0.6 追加；每一已发布基线都继续字节锁定 |
| 正式表现、显式确认与重启恢复 | `walnut-world-frontend` | UI 只消费 Gateway 权威；本地预测只允许作为可撤销即时反馈 |
| Provider key、Sandbox/Artifact 生产装配 | Backend 私有 worker/relay | Frontend、Sandbox 与 Agent library 均不得持有产品数据库或 Provider key |

历史 `yaya_agent_backend`、其私有表和迁移继续仅作 Agent 单仓兼容/回归资产，不进入产品拓扑。

## 决策二：World 展示使用独立的已提交投影流

1. v0.4 `/v1/worlds/{world_id}/events`、`world.committed` 和 `revision == sequence` 不变。不得把多条动作塞进原流，也不得修改 byte-pinned v0.4 文件。
2. v0.5 新增只读 HTTP presentation endpoint及闭合 Event/Page Schema；不修改 v0.4 Bootstrap。HTTP Events 足以完成 INT2；不增加 WSS 或 Event Batch。
3. presentation row 只能在 Backend World reducer 已接受全部 typed intents 后，由 reducer 的逐步 before/after 结果生成；原始模型文本、Sandbox intent payload、客户端预测和源码正则均不是展示权威。
4. presentation rows 与聚合 `world.committed`、最终 Snapshot、Run/Evidence 和 Workspace 终态在同一 PostgreSQL 事务提交。任何投影序列化、哈希或写入失败必须回滚整个 World commit。
5. 第一版只实现正式学生旅程需要的闭合动作，不建立通用动画平台。初始集合为 `HARVEST`；增加其他动作必须有正式旅程测试、闭合参数和同样的 fail-closed 门禁。
6. 每条 presentation event 必须闭合：tenant/session/turn/command/run/world/commit；world revision before/after；独立连续 sequence；action index/count；稳定 event identity；projection/schema version；动作类型及 exact-key 参数；state hash before/after；最终 Snapshot revision/sequence/hash；payload hash。
7. 稳定 identity 由不可变 authority tuple 与 canonical payload hash 派生，不使用重试时重新生成的随机 UUID。Backend 读侧验证 exact keys/types、gap/乱序/重复身份、哈希链、聚合 commit 和最终 Snapshot；任一损坏不返回部分成功。
8. Frontend 正式 AppRoot 的状态机为 `TURN_RUNNING -> PLAYING -> COMPLETED`。播放器不得排序掩盖乱序；必须支持 1x/2x、skip、当前结果 replay、重复读取去重和单播放并发保护。
9. 动画播放、skip、replay 或播放中退出恢复均不得产生产品 mutation。恢复只允许 GET。任何未知类型、缺口、篡改或 renderer 失败都显式进入 recovery，原子回收权威 Snapshot，并记录演出失败；不得静默标为正常动画成功。
10. 最终 Snapshot 是收口权威，不是动作演出的替代品。所有路径最终必须闭合相同 revision、last sequence、state hash 和 canonical Snapshot bytes。

## 决策三：Skill Patch 是显式请求和显式决策

Patch capability 默认关闭，且只有里程碑一全部门禁通过后才允许在受控 INT2 环境启用。v0.6 的只读 Product Capability endpoint 使用 Backend 返回值作为产品权威；Frontend 本地 flag 只能进一步收紧，不能自行放宽。

eligibility 必须同时满足，缺一即 false：

- 学生通过正式 UI 发出一次稳定身份的显式 Patch request；
- role 必须是 `teaching_agent`，场景必须是 `RECTIFICATION`，hint level 必须等于 4；
- 当前 Build/Run 存在 Backend 可验证的失败 Evidence；
- 当前 immutable Draft id/revision/hash、entrypoint 和 source bytes 完整闭合；
- feature capability 为 true；Provider 结果为真实、非 degraded，且该 dispatch 的 generation count 不超过 1。

“可见失败 Interaction”只是学生显式选择的稳定资源，不代表已经存在一个 level-4 hint。冻结的 v0.4 `AgentInteraction` 规定普通 `hint` 只能为 level 0..3，level 4 只在最终 `response_type=skill_patch` Proposal 上合法。因此 Frontend 只能提交一个合同有效、同 Session、可见且带失败 Run/Evidence 的 Interaction identity；不得制造 `hint + level 4`，也不得自行推断失败阈值。Backend 在接受 `UI_ACTION(action_id=request_ai_patch, selection_id=<failed interaction_id>)` 后重新闭合当前失败次数、提示策略、Run/Build/Evidence、Draft 和 capability，随后才向 Agent 构造可信的 `skill_patch_requested` level-4 authority。

Patch request 使用现有 Turn HTTP，但 Backend 必须进入独立的只读纠错分支。该分支只允许创建 Command/Job/Provider receipt、Proposal/Evidence 投影和新的 AgentInteraction；不得调用 `xiaohutao` 根执行，不得启动 Sandbox，不得提交 World，也不得创建 Build、Certification、Activation 或 Learner mastery 副作用。Proposal 显式引用所选择的历史失败 Run/Build/Evidence，而不是把 Patch request command 冒充一次新的失败 Run。

模型输出只是不可信候选 content/rationale。Agent runtime 以只读 authority context 绑定 Draft/Evidence，并只可形成一次 `UPSERT_FILE`，路径必须等于当前规范 entrypoint。第一版禁止多文件、删除、重命名、`SET_ENTRYPOINT`、`SET_DISPLAY_NAME` 和自动重构。fallback/degraded 输出永远不得包含 Patch。

Agent 没有 Draft、Workspace、Build、Certification、Activation、Turn、World 或产品数据库写口。它不得复制 Backend workflow。

## 决策四：PatchDecision、CAS 与副作用边界

1. 只读 Product Capability GET 始终挂载；正式 PatchDecision mutation route 受 Backend flag/capability 控制并默认不挂载，且 Skill Patch flag 依赖 World presentation flag。ACCEPT/REJECT 使用稳定 idempotency key。为兼容冻结 v0.4 语义，receipt 绑定 HTTP 原始 request body 的 SHA-256；同 key 只允许 byte-equivalent body 重放，任何字节差异都返回 `IDEMPOTENCY_KEY_REUSED`，不得悄然改成 canonical-JSON 等价重放。
2. Proposal、Decision 和 immutable Draft revision 都有稳定主键、外键与 canonical hash。决策事务重新验证 proposal hash、exact-one UPSERT、entrypoint、content hash、result Draft hash、当前失败 Evidence 和 Draft CAS。
3. `REJECT` 只写一个终态 Decision/receipt/Interaction 状态；不得创建 Draft revision，不得写 Workspace、Build、Activation、Turn、World、Evidence 或 Learner。
4. `ACCEPT` 只在一个事务内创建恰好一个下一 immutable Draft revision，并把 Workspace 切到该 exact revision/hash。不得自动 Build、Certification、Activation、Turn 或 Run。
5. stale CAS、Evidence/entrypoint 漂移、损坏 Proposal、同 key 不同 payload、相反或重复终态决策均 fail closed。相同 key+相同 payload 在 COMMIT ACK unknown、响应丢失、并发、worker retry 和进程重启下只重读同一 receipt。
6. Frontend 在决定前不得写 Draft；dirty 本地编辑时不得提交决策。ACCEPT 后必须清空旧 Build/Activation/Run readiness，显示 canonical 新 Draft，并要求学生分别手动 Build、Activate、Run。关闭预览窗口不是 REJECT。

## 决策五：Provenance 与 Learner assistance

Backend 使用稳定 FK/哈希闭合：

```text
PatchRequest -> PatchProposal -> PatchDecision
             -> ImmutableDraftRevision -> Build -> Artifact
             -> Certification -> Activation -> Turn/Run/Evidence
```

Build 必须引用 exact Draft id/revision/hash；不得以“最近一次 Patch”推断。被 Patch 产生的 Draft 必须把 proposal/decision FK 带入 Build provenance；下游沿现有 Artifact、Certification、Registry/Activation 与 Run FK 闭合。任何 missing/wrong link 都拒绝，不降级为无辅助成功。

成功 Run 若沿此链来自 accepted Patch，Learner projection 必须记录 `used_skill_patch=true`（或同等 assistance authority）并使用非零 assistance level。该成功不得增加 independent mastery；它必须影响后续提示或掌握度投影。Learner 仍由独立 durable worker 以 lease、monotonic fencing、immutable receipt 和重启恢复推进。

## 决策六：版本、默认关闭与逐步启用

- v0.4 新增 release lock，Manifest 生成器验证所有 v0.4 既有文件字节未变。
- v0.5 只追加 World presentation 的 Schema、Example 与只读 OpenAPI；v0.4 endpoint、Bootstrap 和响应字节不变。
- v0.6 先锁定 v0.5 全部字节，再追加独立只读 Product Capability Schema/OpenAPI。Patch request/decision 继续复用 v0.4 已冻结的宽 wire，由 INT2 policy 收紧为单入口单 UPSERT；Build provenance 使用 Backend 内部稳定外键和哈希闭合，不原地修改 v0.4 Build wire。
- v0.4 客户端仍可使用 INT1；v0.5 客户端可播放权威 World 动作；只有理解 v0.6 capability 且本地门禁也允许的客户端才显示 Patch 入口。
- 部署顺序：迁移和只读 presentation 写入（UI capability off）-> World focused/full/E2E -> World capability on -> Patch 表/Agent policy（Patch capability off）-> Patch offline/deterministic/recovery gates -> 一次受控 real Provider E2E -> Patch capability on。
- 真实 Provider 只有在所有非计费门禁全绿后由人工显式执行一次；事前固定生成预算，脚本禁止自动重跑。fixture PASS 永远标为 deterministic，不得冒充 provider evidence。

## 被拒绝的方案

- 在 v0.4 `world.committed` 流中每个动作追加一条事件：破坏冻结的 revision/sequence 语义。
- 在聚合事件 payload 内塞一个开放数组并由客户端自行解释：无法形成独立 high-watermark、稳定逐事件 identity 和严格读侧腐败门禁。
- 让 Frontend 根据原始 intent 或源码猜动画：越过 Backend reducer authority。
- 自动接受、自动应用、自动 Build/Activate/Run：越过学生明确授权。
- 让 Agent 服务或第二数据库拥有 Patch workflow：破坏唯一 Gateway/PostgreSQL owner。

## 验证与证据规则

本 ADR 只规定边界，不构成 PASS。最终状态以 INT2 跨仓报告中可复跑命令、结构化无密钥证据和独立审计为准。报告必须分别标记 current、historical、deterministic、real Provider，以及 host Docker 与 production private DinD；未执行项必须明确写 `NOT_PROVEN`。当前验证快照为 Backend current-tree full `468/468`、0 failure/error/skip，Agent non-live `599/599`，Frontend offline `60/60`；formal deterministic M2/full/recovery actual10 与受控 real-Provider run `868a` 均已 PASS。后者在 301.012 秒闭合 18 unique dispatch / 18 generation、单 dispatch 最大 generation 1、`PUBLIC_UI_CHAIN_CLOSED`、World commit 1、presentation events 8 与 phase2 17 GET / 0 mutation。v0.6 tag、production private DinD live 与公开 Gateway pending write response-loss仍为 `NOT_PROVEN`，且相关默认 flag 仍为 `false`。
