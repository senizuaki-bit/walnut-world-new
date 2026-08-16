# Python libraries

INT1 的生产复用边界是以下 provider-neutral Python 库：

```text
yaya_agent_contracts
        ↑
yaya_agent_runtime     yaya_agent_build     yaya_agent_sandbox
        ↑                       ↑                    ↑
             walnut-world-backend（唯一生产宿主）
```

- `yaya_agent_contracts`：冻结的领域值、`Result`、Ports 和 Wire DTO。
- `yaya_agent_runtime`：角色路由、上下文、教学策略、工具注册表、通用 Runtime，以及 best-effort 与 recoverable Provider adapter。
- `yaya_agent_build`：完整 source bundle 校验、digest-pinned Docker PUBLIC/HIDDEN tests 和 CAS artifact 发布。
- `yaya_agent_sandbox`：digest-pinned Docker Sandbox 与持久化结果恢复边界。

`yaya_agent_backend` 仅保留为历史 A6/A8 行为回归和兼容实现。它不是 INT1 产品 Gateway、数据库写入权威或迁移权威；生产、Compose 和跨仓验收都不得启动它的 `serve`、`worker`、`learner-worker` 或 `migrate` 命令。唯一公开 Gateway、PostgreSQL 写入权威、worker composition 和 Alembic 链位于 sibling `walnut-world-backend`。

Agent 仓的回归套件仍会执行历史实现，用来证明公共库提取没有削弱 A6/A8 语义；这不表示历史后端可作为第二套产品服务。当前运行边界和跨仓证据见 [INT1 跨仓验证报告](../docs/INT1_CROSS_REPOSITORY_VALIDATION_REPORT.md)。
