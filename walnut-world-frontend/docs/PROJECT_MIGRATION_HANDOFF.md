# 核桃代码世界：Godot 前端迁移交接

> 更新：2026-08-15（INT1 历史快照与 INT2 当前状态分层）

## 当前项目与权威

- 当前仓库是唯一 Godot 前端工作目录；旧 `CodeWorld` 仅是历史来源，不参与运行或开发。
- sibling `../agent/contracts/` 是 Wire 合同源；v0.4.0 继续按138 files、26,127-byte manifest 与 SHA-256 `b62a6152f1f2fd87d1941beecd3a1d47089811e91b067a8b225ff0d7a5ce72b9` 逐字节锁定。当前 additive v0.6 candidate 为147 entries、27,848 bytes、SHA-256 `11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05`，但 v0.6 Git tag 尚未创建，严格为 `NOT_PROVEN`。
- sibling `../walnut-world-backend` 是唯一 HTTP Gateway、PostgreSQL/Alembic 写权和 combined durable worker owner。前端不访问 Agent HTTP、数据库、Docker、Provider 或本地编译器。

## 已完成

- 正式 composition root 为 `scenes/app/app_root.tscn`；只读取 base URL 与短期 token，不接收人工 Session ID。
- Student Bootstrap → server-created/recovered Session → Content/Workspace/starter Draft → Snapshot/Interaction 恢复。
- Draft revision/hash CAS、stable response-loss envelope、Build/Certification/Activation、original Session exact-version Turn。
- terminal Command/Run、World receipt、Evidence、连续 HTTP Events、Snapshot、Learner/Product Interaction 与 Workspace high-watermark 闭包。
- ClientStore 持久化公开 authority、exact active tuple 与 pending operation；CommandPoller 使用真实 deadline、指数退避、抖动和 `Retry-After`。
- Snapshot/Event 只投影到预置 TerrainManager/Player/UI；不从模型文本或 Sandbox intent 猜世界事实。
- v0.4 contract/client drift gate 与 offline headless suite；INT1 收敛时的 46/46 记录继续作为历史证据。当前 Frontend offline 为 60/60 PASS，另有两条 real opt-in 精确 `EXCLUDED_NOT_RUN`，0 skip/fail；stdout SHA-256 为 `269E5D6BA4FDCEFBBDCF82E33FDA204C820AD942EAECA2312DDED37753D8C2E4`。三仓当前自动化同时为 Agent 601（599 PASS + 2 exact excluded）与 Backend 468/468 PASS。远端 Agent annotated tag `refs/tags/agent-contracts-v0.4.0` 已发布并指向 release 提交 `0494c0f8ef6eb505e43db84c0249b046be35c589`；v0.6 tag 仍为 `NOT_PROVEN`。
- 默认关闭的真实 Gateway runner：`scripts/run-real-gateway-e2e.ps1`。

## 当前验收状态

Backend 已装配 Student Bootstrap、Content/Workspace/Draft、Build/Certification、Activation、exact-version Turn 和 terminal Run/World/Event/Evidence/Learner/Interaction/Workspace；这些不再是前端外部缺口。

27.6 秒 direct-POST、169.836 秒 recoverable-relay 与 194.12 秒真实 Provider 运行均只作 INT1 历史证据。当前 INT2 deterministic actual10 和受控真实 Provider M2 均已 PASS。真实 Provider `run868a` 用时 301.012 秒，DeepSeek `deepseek-v4-flash` 为 `source=provider`、`degraded=false`；18 unique dispatch / 18 generation、单 dispatch 最大 1，Provider relay response-loss 恢复同一 dispatch 且 generation 仍为 1。正式学生链闭合 4 次客观失败 → Request Patch → Dialog 显式 `ACCEPT` → 手动 Build/Activate/Run，Patch 状态为 `PUBLIC_UI_CHAIN_CLOSED`；全链为 6 Turn、5 Run、6 Interaction、11 个 terminal Command（7 `APPLIED` + 4 `REJECTED`）、12 POST/1 PUT、1 个 World commit 与 8 个 presentation event。任务自有 PostgreSQL 断库/原容器恢复并重启 Gateway/workflow/learner/Godot 后，phase 2 为 exact 17 GET/0 mutation；DB SHA-256 `b8bb2b568ac6978d938a98d041f9c5b74ef108167f53790c4bbeecbb6c051e30`，脱敏 stdout SHA-256 `2A7D2C057EF66D54F4DBFA828166DAC0A688704471618E6FA2940CE4F95B2425`，并精确恢复原有 3 个 Docker 容器。该 Windows 结果不是 production private DinD；production private DinD 与公开 Gateway pending write response-loss（不同于已验证的 Provider relay response-loss）仍为 `NOT_PROVEN`。具体运行见 [真实 Gateway E2E](testing/real-gateway-e2e.md)。

## 关键实现

- `project.godot`：主场景与 Autoload；
- `scenes/app/app_root.tscn` / `.gd`：唯一启动装配；
- `autoload/client_store.gd`：客户端状态和持久 envelope；
- `autoload/session_controller.gd`：Draft/Build/Activation/Turn/恢复编排；
- `addons/yaya_contract_client/`：Student/Game/Product 严格合同 Gateway/Transport/Validator；
- `scripts/client/command_poller.gd`：deadline/backoff/Retry-After；
- `scripts/client/world_recovery_coordinator.gd`：HTTP Events/Snapshot gap recovery；
- `scripts/client/product_interaction_gateway.gd`：Product resources；
- `docs/testing/godot-student-client-contract-gates.md`：离线门禁；
- `docs/testing/real-gateway-e2e.md`：真实跨进程门禁。

## 排除项与约束

- WSS 与 Client Event Batch 保持未完成且排除；Backend production 默认不挂载，v0.4 `StudentBootstrapV2` 也不包含 `world_event_stream`、`client_event_batch` 或 `stream_url` 响应字段；INT1 只使用 HTTP Events/Snapshot。
- Skill Patch/PatchDecision 在INT1范围内曾明确排除；当前INT2已实现学生可见预览、显式ACCEPT/REJECT与手动Build/Activate/Run，但Backend route与Frontend入口仍由默认false的capability/feature flags收紧，默认配置不自动启用。
- Feishu、Client Event Batch 新纵切、自动或多文件Patch、自动Build/Activation、空账号/内容创作和 UI 美术重做排除。
- 前端禁止旧 `/api/*`、猜字段/default revision/latest Session、直接 HTTP 绕过 Gateway、直接执行 C++ 或自动应用 AI Patch。
- 世界事实只来自已提交 Run/receipt/Event/Snapshot；只有闭合的非降级 Product Interaction 可作为教学展示。
