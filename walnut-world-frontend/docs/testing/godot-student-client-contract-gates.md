# Godot 学生端合同自检

依据 sibling Agent 的 `02_核桃代码世界_前端开发文档.md`、`05_核桃代码世界_接口对齐与联调规范.md`；Wire 唯一权威为 `../agent/contracts/manifest.json`。

## v0.6 candidate 与历史合同漂移门禁

Frontend descriptor当前指向`@yaya/agent-contracts` v0.6 candidate：147 entries、27,848-byte manifest、SHA-256 `11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05`。v0.4/v0.5历史发布字节继续由release locks校验；v0.6 tag尚不存在，release identity为`NOT_PROVEN`。

`contract_release_drift_test.gd` 默认只读解析 frontend 的 sibling `../agent`。非 sibling 布局必须把 Agent 仓库根目录传给 `YAYA_AGENT_REPOSITORY_ROOT`；相对 override 以 frontend 根目录为基准：

```powershell
$env:YAYA_AGENT_REPOSITORY_ROOT = 'C:\path\to\agent'
& $env:GODOT_EXE --headless --path . --script res://tests/client/contract_release_drift_test.gd
```

门禁逐字节核对 manifest 及其 147 个 entries，核对 package/release/version，并验证 frontend Validator 与 Agent canonical 源一致、Gateway 只有资源路径迁移、HTTP transport 的 Game/Student/Product operation method/path/query exact-set 与 manifested OpenAPI 一致。它不调用 Git、不要求任一工作区干净、不访问网络，也不修改 Agent；路径缺失、hash/version/release/file/client 映射任一漂移都会非零失败。

## 已验证

- [x] 原 `CodeWorld` Godot 项目已迁入本仓库；场景、资源、脚本和既有测试均保留，未迁移可再生 `.godot` 缓存。
- [x] 预置 `TaskWorkspace` 包含世界视图、角色对话、CodeEdit、保存/编译/运行控制、结果面板、Patch 和成长总结入口。
- [x] 预置 `DialoguePanel` 显示已验证 AgentInteraction 的角色、反馈、response type、hint level 与可空追问；不从文本推断教学状态。
- [x] `ClientStore` 统一保存草稿、流程、快照、事件游标和错误；Draft CAS 冲突不会覆盖本地源码。
- [x] Product Draft GET/CAS PUT：携带稳定 Idempotency-Key 与 revision/hash 基线，成功后仅采用 canonical Draft。
- [x] Product Draft PUT 与 PatchDecision 客户端兼容路径的 `503 RECONCILE` 已严格校验 canonical Location/资源身份；客户端先读取并比对 canonical Draft 或 Interaction/PatchDecision 回执，确认已落地才结束动作，绝不以新 key 重发。
- [x] PatchDecision 客户端已接入严格路径、幂等键、请求身份与 receipt invariants；正式 Dialog 只提供显式 ACCEPT/REJECT。Backend route 默认关闭并按 flag 条件挂载；启用 Frontend World/Patch 双 flag 与 Backend capability 后，deterministic actual10 与受控真实 Provider M2 均已通过学生显式 ACCEPT 主链。
- [x] 新交付的 Product SessionWorkspace GET 已接入严格 Gateway，用于后续以版本固定的 Session、world checkpoint、Draft refs 和 Interaction 高水位恢复页面。
- [x] `SessionController.recover_workspace` 已按 Workspace -> canonical Draft -> authoritative Snapshot -> Interactions 编排；Draft 或世界 checkpoint 错链时整组拒绝。
- [x] 创建 Build 前一定先保存当前 Draft；随后以 `202 + Location/command_id` 对账 Command，继续轮询同一 Build 直到终态。UI 不在本地编译 C++。
- [x] 单文件 C++ Draft 会核对终态 Build `artifact.source_sha256` 与实际提交文件的 SHA-256；不一致即 fail closed。
- [x] Build 流程使用课程恢复时注入的 compiler profile、测试版本和 capability 集合，不从 UI 猜测配置。
- [x] `WorldEventPlayer` 只消费已提交且 sequence 连续、按 `event_id` 去重的事件；gap 会请求恢复而非推进本地世界事实。
- [x] Realtime 协议层已覆盖 subscribe/resume、延迟 ACK、heartbeat_ack 和 gap 暂停；只有调用方确认事件已持久应用后才允许 ACK。
- [x] HTTP 世界恢复协调器仅返回连续 Event 段；任一页身份或 sequence 不连续时丢弃该段并原子读取 Snapshot。
- [x] 已验证 Snapshot 投影到现有 TerrainManager 与 Player：只更新地块表现与玩家位置，不动态创建世界节点或推导世界结果。
- [x] 迁入的 Game Gateway 和 Product Gateway 对未知字段/身份不一致 fail closed。
- [x] 无头回归已通过：任务工作台、ClientStore、Gateway 边界、Draft 保存、Build Command 流程、Product Interaction、Command Poller、WorldEvent Player、旧农场 Smoke、美术资源和全部 Terrain 测试。
- [x] Backend/Frontend descriptor与Agent v0.6 candidate对齐；v0.4/v0.5历史锁继续验证。

## Historical INT1 boundary 与 INT2 当前叠加

- [x] 真实 `app_root.tscn` 使用生产 HTTP transport/Gateway 完成 StudentBootstrap、Session 新建或精确恢复、Content、Workspace、Draft、Snapshot 与 Interaction 的 loopback E2E；最终场景不注入 FakeGateway。
- [x] Run 协调器完成 terminal Command/Run、exact Skill tuple、receipt、Evidence、HTTP Events、Snapshot 与 Interaction 闭包；任一错链或 gap 都不得进入 `COMPLETED`。
- [x] ClientStore 持久保存公开 authority、exact active tuple 与响应丢失 envelope；CommandPoller 覆盖 fresh context、总 deadline、指数退避和 `Retry-After`。
- [x] AppRoot 在 Workspace 恢复后、READY 前先以 canonical Draft GET 对账持久化 `draft_save`：已提交则只清 envelope；未提交才使用 JSON 持久化的原 request body、`client_saved_at`、CAS base 与 Idempotency-Key 重 PUT，并以第二次 GET 验证。瞬态错误保留 envelope，权威终态清理并失败。`pending_draft_restart_recovery_http_test.gd` 跨两个 ClientStore 与生产 HTTP transport 捕获精确原始 body/key。
- [x] AppRoot 在 Workspace 恢复后、READY 前优先恢复持久化 `agent_turn`/`agent_hint`；只以原 request body、key、Turn 前 World/Interaction cursor 重 POST/对账，闭包或权威终态前禁止按新高水位创建 Turn identity。`pending_turn_restart_recovery_test.gd` 跨两个 ClientStore 实例证明 response loss 后无新 Turn ID/sequence/body。
- [x] 27.6 秒 direct-POST cross-repository diagnostic 曾闭合增强前的 identity/revision/sequence 与无重复副作用 wiring 指纹；它不含 recoverable relay、强制 Draft PUT/CAS 或正式 UI display，现只作为历史证据。
- [x] Historical INT1：同一正式 runner 曾在受控真实 Provider 拓扑取得 194.12 秒 `REAL_PROVIDER_PRIVATE_DURABLE_RELAY` PASS；DeepSeek V4 Flash 为 `source=provider`、`degraded=false`，13/13 generation 且单 dispatch 最大 1，并闭合 Provider response-loss 同 dispatch 恢复、三服务新 PID、8 GET/0 mutation 与五类指纹不变。它不是当前 INT2 Provider evidence，也不是 production private DinD live。
- [x] 已提供默认关闭的 `real_gateway_chain_e2e_test.gd` 与 `scripts/run-real-gateway-e2e.ps1`；以 `-EnableWorldPresentation -EnableSkillPatch` 启用时，deterministic actual10/outage/restart 与真实 Provider M2 `run868a` 均已通过该正式路径。Phase 1 闭合 4 失败 → Request Patch → Dialog 显式 ACCEPT → 手动 Build/Activate/Run，Phase 2 由新 Godot 进程执行 exact GET-only 恢复。
- [x] WSS 与 Client Event Batch production 默认不挂载且属于排除项；v0.4 `StudentBootstrapV2` 不包含 `world_event_stream`、`client_event_batch` 或 `stream_url` 响应字段。WSS Upgrade Adapter 保留为未完成能力，INT1 世界闭包只使用 HTTP Events/Snapshot。
- [x] PatchDecision为INT2默认关闭能力；Backend按flag条件挂载，Frontend双flag只能收紧。

当前 Frontend offline gate 为 60/60 PASS，另有两条 real opt-in 精确 `EXCLUDED_NOT_RUN`，0 skip/fail；stdout SHA-256 为 `269E5D6BA4FDCEFBBDCF82E33FDA204C820AD942EAECA2312DDED37753D8C2E4`。三仓当前自动化为 Agent 601（599 PASS + 2 exact excluded）、Backend 468/468 PASS、Frontend 60/60 PASS。M1、deterministic actual10/outage/restart 与真实 Provider M2 `run868a` 均已 PASS。该 live 用时 301.012 秒，DeepSeek `deepseek-v4-flash` 为 `source=provider`、`degraded=false`；18 unique dispatch / 18 generation、单 dispatch 最大 1，Provider relay response-loss 恢复同一 dispatch 且 generation 仍为 1。Historical INT1 live 不是当前 INT2 证据；production private DinD 与公开 Gateway pending write response-loss 仍为 `NOT_PROVEN`。
> 更新（2026-08-15）：当前deterministic M2 PASS的正式UI链为4次客观失败 → Request Patch → PatchDialog完整before/after、操作hash/Evidence预览 → 学生显式`ACCEPT` → 手动Build/Activate/Run；未确认前不写Draft，确认后也不自动Build、Activate或Run。
> 更新（2026-08-15，计数与恢复）：deterministic actual10 与真实 Provider `run868a` 的 Phase 1 均为 12 POST/1 PUT、6 Turn、5 Run、11 terminal Command（7 `APPLIED` + 4 `REJECTED`）、1 条 World commit 与 8 条 presentation；Patch 达到 `PUBLIC_UI_CHAIN_CLOSED`。PostgreSQL 断库/原容器恢复与三服务新进程重启后，Phase 2 为 17 GET/0 mutation 且 exact 恢复同一 public M2 与持久化权威指纹。Real Provider DB SHA-256 为 `b8bb2b568ac6978d938a98d041f9c5b74ef108167f53790c4bbeecbb6c051e30`，脱敏 stdout SHA-256 为 `2A7D2C057EF66D54F4DBFA828166DAC0A688704471618E6FA2940CE4F95B2425`，清理后精确恢复原有 3 个 Docker 容器。
> 历史更新（2026-08-09，Activation）：前端已具备 Build → Activation Command → canonical SkillActivation 对账编排。原“后端路由未交付”缺口已由 Student Bootstrap v0.4 authority 和 Backend full-scope Registry CAS 解决；真实 Provider 跨进程验收随后于 2026-08-13 通过。
> 历史更新（2026-08-09，Turn/Run）：前端已实现 ACTIVE 状态下的 Agent Turn Command 对账。原“Backend worker 不产生 Run”缺口已由 combined worker 与 terminal projection 解决；历史当时剩余的真实 Provider 跨进程验收随后于 2026-08-13 通过。
> 更新（2026-08-14，权威动作演出）：正式AppRoot已通过HTTP presentation逐步播放8条已提交HARVEST，并在gap/损坏时回权威Snapshot；M1不依赖WSS，WSS仍明确排除。
> 更新（2026-08-09，分阶段结果）：预置 ResultPanel 已按合同的 `SkillBuild.phases` 展示源码校验、编译、课程测试、隐藏测试与认证；按 `Run` 展示 Sandbox、World receipt、教学反馈、降级原因和 Evidence。`result_panel_format_test.gd` 覆盖该格式化边界，避免把“Sandbox 成功”误呈现为世界提交成功。
> 更新（2026-08-12，INT1 运行时装配）：启动场景使用真实 `AppRoot` 与 HTTP Gateway。它只接受 base URL 和运行时 `YAYA_AUTH_TOKEN`，不接受手工 Session ID。它先读取严格的 StudentBootstrapV2；若 `current_session_id` 非空则精确 GET，若为空则原样提交权威 `create_request`、轮询 Command 并精确 GET 新 Session，随后恢复 Workspace/Draft/Snapshot/Interaction。`app_root_local_http_e2e_test.gd` 通过 loopback HTTP 同时覆盖新建与恢复拓扑，不以 FakeGateway 替代最终场景。
> 更新（2026-08-09，自动保存并发）：TaskWorkspace 使用预置的一次性 `AutoSaveTimer`，0.8 秒 debounce 后调用 Draft CAS 保存；恢复 Draft 时以同步标志避免 CodeEdit 的程序赋值误触保存。保存期间继续输入时，Store 保留新文本为 DIRTY，同时接收旧保存回执的 canonical revision/hash，下一次保存不会丢文本也不会使用陈旧 CAS 基线。`task_workspace_autosave_test.gd` 和 `draft_save_concurrency_test.gd` 覆盖这两个边界。
> 历史更新（2026-08-09，固定内容）：Product Gateway 严格读取 ContentUnit。原“Backend 未装配 Content Adapter”缺口已解决；Student Bootstrap/Content 提供 server-owned compiler/test/capability authority，Session Control 由 starter 创建 Draft/Workspace，前端不猜测。
> 更新（2026-08-09，真实提示 Turn）：提示按钮不再使用本地编造文案，而是提交 `MESSAGE` Agent Turn（空 skill bindings、`zh-CN`）；无 Run 是提示的合法终态，随后恢复 Product Interaction。学生运行仍要求 Activation。`agent_hint_turn_flow_test.gd` 验证该边界；编译诊断精确行列仍依赖合同新增字段，已登记。
> 更新（2026-08-09，Product 写入对账）：`upsertProductSkillDraft` 和 `recordProductPatchDecision` 已实现 Product `503 RECONCILE` 分支：网关先拒绝未知字段、错误 `Location` 或错链资源；协调器只 GET 合同指定的 canonical Draft/Interaction，逐项核验写入内容或 PatchDecision 回执，确认后绝不重发写操作。`product_write_reconciliation_test.gd` 与 Gateway 负例覆盖此门禁。
> 更新（2026-08-09，Agent 对话展示）：`DialoguePanel` 新增预置 response type 与 question 节点。TaskWorkspace 将合同校验后的 `role`、`feedback.message`、`response_type`、`hint_level` 和 `question` 映射为角色名称、提示等级标签与可选追问，不从自然语言猜测教学状态。`dialogue_panel_test.gd` 与工作台冒烟测试覆盖该行为。
> 更新（2026-08-12，INT1 Run 闭包）：学生 Turn 在 terminal Command 后继续轮询 terminal Run；只有 exact Skill binding、非降级 provider feedback、`world_application.receipt`、全部 Evidence（含匹配的 WORLD_COMMIT）、连续 HTTP Events、精确 Snapshot 与匹配 AgentInteraction 全部闭合，才原子替换世界并进入 `COMPLETED`。`agent_turn_run_flow_test.gd` 覆盖成功链与 event gap 负例；WSS 保留为未完成能力且不计入 INT1。
> 更新（2026-08-12，重试与恢复）：`CommandPoller` 为每次 GET 生成新的 RequestContext，实施总 deadline、指数退避、抖动与 `Retry-After`，deadline 后到达的终态也拒绝。ClientStore 持久保存 bootstrap/session/activation 七字段精确元组和待协调 envelope；Draft、Turn、PatchDecision 响应丢失重试保持 ID、幂等键、时间和 body 不变。AppRoot 启动恢复 pending Turn 时沿用持久化 session/pre-world/Interaction cursor，成功闭包或权威终态后清除，瞬态失败则保留且不创建新 identity。`command_poller_test.gd`、`client_store_test.gd`、`operation_envelope_stability_test.gd` 与 `pending_turn_restart_recovery_test.gd` 覆盖这些门禁。
> 更新（2026-08-15）：上述回归及 INT2 presentation/capability/Patch focused seam 均纳入当前 offline 60/60；deterministic 与真实 Provider M2 PASS 另由三仓、跨进程、断库/恢复与 PostgreSQL 权威指纹闭合，不由 offline 计数单独推导。
