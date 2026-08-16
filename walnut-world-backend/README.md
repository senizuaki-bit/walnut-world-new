# walnut-world-backend

核桃代码世界唯一生产 HTTP Gateway、PostgreSQL 写入与 Alembic 迁移权威。INT1 生产拓扑只由本仓库的 `backend` 服务公开端口；`postgres`、一次性 `migrate`、私有 `llm-relay`、digest-pinned DinD、镜像预载任务、`workflow-worker` 与独立 `learner-worker` 都不暴露产品 HTTP。`workflow-worker` 闭合 Control、Build/Certification、Activation、Turn 与 Run/World/Event/Evidence，并耐久写入 terminal hand-off；`learner-worker` 独立闭合 Learner、Product AgentInteraction 与 Workspace。DinD 与 `workflow-worker` 共享固定 Linux runtime volume/socket，避免 Windows host path 被误作嵌套容器 bind source；Worker 不挂载 Docker Desktop host socket。Gateway 不代理 sibling Agent 的历史 `yaya_agent_backend` 服务，也不读取或迁移其 `yaya_*` 私表。

当前工作树的 Alembic head 是 `019_int2_skill_patch_authority`，父修订为 `018_world_presentation_events`。Backend 消费 Agent additive v0.6 candidate（147 entries、27,848-byte manifest、SHA-256 `11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05`）以及 provider-neutral Runtime、Build 和 Sandbox 库；v0.4/v0.5 历史 Wire 继续逐字节锁定，v0.6 tag 尚不存在，release identity 为 `NOT_PROVEN`。Backend 在自己的表/UoW 中实现：

- Student Bootstrap 的 Session、Build policy、Activation registry 和 HTTP World authority；
- server-created Session 及同事务 starter Draft/Workspace；
- Draft revision/hash CAS、pinned-Docker Build/PUBLIC/HIDDEN tests/Certification；
- full-scope Activation registry CAS 与 exact-version Turn；
- workflow terminal Run/World/Event/Snapshot/Evidence 与 durable learner hand-off；
- 独立 learner worker 的 Learner/Interaction/Workspace projection closure；
- durable jobs、DB-clock leases、fencing 和不可变 side-effect receipts；
- capability-verified recoverable Provider relay，以及 Provider/Sandbox/Build response-loss 对账。

私有 `llm-relay` 是唯一上游 Provider key owner；`workflow-worker` 只读取私有 relay endpoint/bearer 与 Provider/model 标识，独立 `learner-worker` 不接收 Provider、relay 或 Docker 凭据。`workflow-worker` 启动时 capability fail-fast；每次 dispatch 使用稳定 ID、GET-first、权威 `ABSENT` 后同 ID PUT、`Retry-After` 和 fence/receipt/relay bytes 联合校验。普通 chat-completions direct endpoint 无法恢复 response loss，因此不会回退使用。

INT2 的只读 Product capability GET 始终挂载；World presentation GET 只在 `WALNUT_ENABLE_WORLD_PRESENTATION=true` 时挂载，PatchDecision 只在 `WALNUT_ENABLE_SKILL_PATCH=true` 时挂载，且 Patch flag 不能绕过 World flag。两项默认均关闭。M1 formal deterministic Gateway/Godot 与 2026-08-15 的正式 deterministic M2 均已 PASS；同日受控 real-Provider M2 也以 run `868a` 在 301.012 秒取得 PASS：18 unique Provider dispatch / 18 generation、单 dispatch 最大 generation 1，response-loss 恢复复用同一 dispatch 且 generation 仍为 1，学生可见 Patch 链为 `PUBLIC_UI_CHAIN_CLOSED`。两种 M2 均闭合 6 Turn、5 Run、11 个 terminal Command（7 `APPLIED` + 4 `REJECTED`）、1 个 World commit、8 个 presentation event，以及重启后的 17 GET / 0 mutation。该受控 live 不改变默认 flag，也不证明 production private DinD 或公开 Gateway pending write response-loss；两者仍为 `NOT_PROVEN`。WSS、Client Event Batch、Feishu、自动/多文件 Patch仍排除。

运行与authority seed见[运行手册](docs/operations/runbook.md)。当前 Backend fresh full 为 468/468、0 failure/error/skip；Agent 为 601 项（599 PASS + 2 个显式排除的真实 E2E），Frontend offline 为 60/60 PASS + 2 个显式排除的真实 E2E。2026-08-13 的194.12秒DeepSeek运行仅为 **historical INT1 real-Provider / host-Docker evidence**，当前 INT2 live 以 2026-08-15 run `868a` 为准；production private DinD 与公开Gateway pending write response-loss仍为`NOT_PROVEN`。当前INT2状态以sibling Agent的`docs/INT2_CROSS_REPO_VALIDATION_REPORT.md`为证据账本。
