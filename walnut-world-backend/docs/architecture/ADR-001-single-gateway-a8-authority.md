# ADR-001: INT1 单 Gateway 与 A8 生产权威

- 状态：Accepted
- 日期：2026-08-11
- 范围：`agent`、`walnut-world-backend`、`walnut-world-frontend`

## 决策

`walnut-world-backend` 是 Godot 唯一可访问的 HTTP Gateway，也是唯一生产业务写权、事务边界与 PostgreSQL/Alembic 迁移链。本ADR形成时的线性head历史快照是 `017_durable_learner_worker`（父修订 `016_recoverable_llm_relay`）；当前INT2工作树已追加到 `019_int2_skill_patch_authority`（父修订 `018_world_presentation_events`）。生产拓扑只暴露一个监听器；私有 `llm-relay` 独占上游 Provider key，`workflow-worker` 装配 Control、Build/Certification、Activation、Turn、Run/World/Event/Evidence 并耐久提交 terminal hand-off，独立 `learner-worker` 再闭合 Learner、Product AgentInteraction 与 Workspace。三者都不开放产品 HTTP 或 Agent HTTP 端口。

Agent 仓只提供不可变 Wire 合同、公共 Ports、provider-neutral Runtime、Digest-pinned Docker Build/Sandbox 和教学策略等库能力。后端在自身表和 Unit of Work 上实现这些 Ports。生产进程不启动 `yaya_agent_backend` HTTP 服务，不运行 Agent 的 `yaya_schema_migrations`，也不直接或间接把 `yaya_*` 私表作为产品数据源。

现有 `agent-contracts-v0.3.0` 边界由 release lock 保持逐字节不变。INT1 所需但 v0.3 未表达的学生运行权威，发布为追加式 `0.4.0` 合同资源和新 GET 操作；其锁定manifest为26,127 bytes、138 files、SHA-256 `b62a6152f1f2fd87d1941beecd3a1d47089811e91b067a8b225ff0d7a5ce72b9`。当前INT2 additive v0.6 candidate为147 entries、27,848 bytes、SHA-256 `11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05`，但v0.6 Git tag尚未创建，严格为`NOT_PROVEN`。INT1新资源公开：

- 服务端可恢复的精确 Session 身份及创建所需 authority；
- Build policy、compiler/test suite 与允许 capability；
- 完整 Activation scope 和当前 registry CAS revision；
- 当前活跃的 `skill_id + skill_version_id + artifact_sha256 + certification_id + registry_revision` tuple，或明确的 `null`；
- World revision、HTTP event cursor、events URL 与 snapshot URL。

Run 继续使用 v0.3 已冻结的 `world_application.receipt`。Godot 必须校验 receipt，再按 receipt 的 sequence 范围读取 HTTP events；出现 gap、hash/revision 漂移或无法桥接时，原子替换为服务端 Snapshot。INT1 不宣称 WSS 完成：production 默认不挂载 WSS 或 Client Event Batch，Bootstrap capability=false；冻结 Schema 中的 `stream_url` 只是 inert 结构值。

## 后端持久化边界

Student Bootstrap 从 Backend-owned LaunchAuthority 读取唯一闭包。`workflow-worker` 的 Control job 成功创建 Session 时，在同一终态事务创建 revision-1 starter Draft 和 revision-1 Workspace；Draft CAS 与 Turn 接受刷新对应业务引用，独立 `learner-worker` 的 terminal projection 再推进 Workspace 的 World checkpoint 和 Interaction high-watermark。客户端不预置 Session/Draft，也不从默认值猜 authority。

后端 Alembic 线性 head 扩展以下权威记录，并用外键、唯一约束、不可变字段和 CAS 约束闭合关系：

- Build job/phase、Source identity、Artifact CAS、PUBLIC/HIDDEN test result、Certification；
- full-scope Registry head、Activation 与 exact active tuple；
- Session authority/binding、Turn exact tuple、Run/World receipt、Evidence、Interaction；
- Control/Build/Turn/Learner job lease、fencing token、attempt、checkpoint 与 terminal result；
- provider、Docker、Sandbox 等外部副作用的 deterministic request hash 和 durable receipt；
- Learner projection cursor/profile。

命令接收只提交 durable intent。`workflow-worker` 与 `learner-worker` 分别通过 `FOR UPDATE SKIP LOCKED`/CAS 获取有时限 lease；每次终态写入校验 fencing token。外部调用前后都有可恢复 checkpoint；相同 deterministic identity 只能观察或复用同一 receipt，接管者不得重复副作用。Provider 使用 capability-verified recoverable relay 的稳定 GET/PUT dispatch，并联合校验 `Retry-After`、fence、数据库 receipt、completion 与 raw bytes hash；Build/Sandbox 在控制面 response loss 后 reconcile 同一稳定容器。每个 job 的 Command、Job、Resource 或 terminal hand-off 必须在其 Backend-owned 终态事务中一致收敛。

## 进程与依赖

生产 Compose 包含：一个 PostgreSQL、一次性 Alembic migrate、一个 Gateway、私有 `llm-relay`、一个 `workflow-worker`、一个独立 `learner-worker`、私有 digest-pinned Docker daemon，以及持久化 Artifact/Workspace/Sandbox result 根。只有 Gateway 发布端口；`llm-relay` 是上游 Provider key owner，`workflow-worker` 只持有私有 relay bearer/endpoint 与 Docker socket，`learner-worker` 不持有 Provider、relay 或 Docker 凭据。Build 与 Sandbox 只允许精确 digest 镜像；密钥绝不写入镜像、Compose、日志或数据库。真实 Provider 成功记录必须是 `source=provider, degraded=false`。Provider 失败保留已提交的 Run/World/Evidence 客观事实，但不发布 `provider_fallback` Interaction、不推进 Learner。普通 direct chat endpoint 不能替代 recoverable relay。

## 被拒绝的方案

1. Gateway 反向代理第二个 `yaya_agent_backend` HTTP 服务：形成第二公共后端，明确禁止。
2. 在同一进程中直接复用 `StudentSkillChainApplication`/`AgentTurnApplication` 及 `yaya_*` 表：虽只有一个监听器，仍保留第二业务写权、第二迁移事实和私表耦合，拒绝。
3. 将 Agent SQL/实现复制到后端，或用同步脚本保持两份表/代码一致：会隐藏权威冲突和漂移，拒绝。
4. 客户端从 UI revision、默认 `0`、latest Session 或环境 `YAYA_SESSION_ID` 猜 authority：拒绝；所有值由追加式公开资源返回。

## 交付与非目标

本ADR的单Gateway/A8架构决策已落实。ADR形成阶段的历史验收快照为：2026-08-13唯一INT1 live在194.12秒取得`REAL_PROVIDER_PRIVATE_DURABLE_RELAY` PASS，DeepSeek V4 Flash为`source=provider`、`degraded=false`，13 unique dispatch / 13 generation且单dispatch最大1；当时分仓门禁为Agent non-real-Provider 574/574、Backend fresh full 299/299（0 failure/error/skip、142.238s）与Frontend offline 46/46。上述计数和live都明确属于INT1历史证据，不描述当前INT2工作树。更早的Agent 573/573与Backend 252/252同样只保留为historical。Provider/Sandbox/Build response-loss与接管虽有focused/fresh-PostgreSQL或library-level Docker证据，但不能据此声称真实Docker control-plane fault已在host Docker或private DinD live注入。

27.6 秒 direct-POST、79.764 秒、106.867 秒和 169.836 秒 recoverable-relay 诊断均只保留为历史证据。169.836 秒记录在当时完成四个 Turn（3 失败 + 1 成功）、同一 disposable PostgreSQL stop/start、Gateway/workflow/learner 数据库重连、三服务新 PID recovery-only phase2，并以历史 `side_effect_sha256` `9d9e770a6bf8f9f03fc351c50a3fba2dd3d57971df91237d46f9e49c3335ab05` 取得 **`DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER` PASS**。

同一历史INT1 194.12秒live还证明了4个Turn/Run/Interaction/Learner projection、2个Build/Certification/Activation、9个terminal Command、11个Evidence、8个Sandbox receipt、2个Artifact文件，以及Provider response-loss同dispatch恢复和三服务新PID后的8 GET / 0 mutation；它不是production Compose private DinD live，也不能替代INT2 live。

当前INT2同时具有deterministic actual10与受控real-Provider M2证据：正式学生UI完成Patch request、逐字段预览、显式ACCEPT，再分别手动Build、Activate、Run；总计6 Turn、5 Run、11个terminal Command（7 `APPLIED` + 4 `REJECTED`）、1个World commit和8个presentation event。两项门禁在任务自有PostgreSQL断库/恢复并重启Gateway/workflow/learner/Godot后，phase2均为exact 17 GET / 0 mutation；deterministic分类为`DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER`，real-Provider run `868a`在301.012秒取得PASS，具有18 unique dispatch / 18 generation、单dispatch最大generation 1与response-loss同dispatch generation-one恢复，学生可见链为`PUBLIC_UI_CHAIN_CLOSED`。当前三仓自动化为Agent 601项（599 PASS + 2个显式排除的真实E2E）、Backend fresh full 468/468（0 failure/error/skip）与Frontend offline 60/60 PASS + 2个显式排除的真实E2E。production private DinD live与公开Gateway pending write response-loss均仍为`NOT_PROVEN`；受控live harness的private relay/proxy PASS不替代后者。PatchDecision与presentation routes已实现但默认关闭并按feature flag条件挂载；WSS与Client Event Batch默认不挂载且capability=false，Feishu、自动/多文件Patch、自动Build/Activation和UI美术重做继续排除。远端Agent annotated tag `agent-contracts-v0.4.0`已发布并指向release提交`0494c0f8ef6eb505e43db84c0249b046be35c589`；v0.6 tag仍不存在，release identity继续为`NOT_PROVEN`。
