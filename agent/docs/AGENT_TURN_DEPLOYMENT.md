# Agent Turn 生产部署

> INT1 说明（2026-08-13）：本文保留 Agent 单仓 A6/A8 composition 的历史部署方式，不是当前产品运行手册。生产只启动 sibling `walnut-world-backend` 的唯一 Gateway、Alembic head `017_durable_learner_worker`（parent `016_recoverable_llm_relay`）与 combined worker；不得运行本文的 `yaya_agent_backend` HTTP/migration 作为第二权威。当前运行方式见 Backend `docs/operations/runbook.md`。

## 1. 进程与信任边界

生产至少运行 PostgreSQL、HTTP 和 Worker 三个独立故障域。HTTP 只负责 JWT 认证、严格 Wire 校验、原子受理与查询；Worker 从 `yaya_command_jobs` 领取持久化任务；C++ 只在固定摘要 Docker 容器中运行。反向代理负责公网 TLS，Python HTTP 进程绑定明确的 loopback 或受控容器地址。

生产 composition 不接受 Sandbox 注入，也不会从 Docker 降级到宿主进程。容器固定使用：无网络、只读根文件系统、全部 capability 删除、`no-new-privileges`、非 root 用户、PID/CPU/内存限制、临时工作目录和只读 artifact mount。

## 2. Artifact

certified Linux ELF 以 SHA-256 为身份，放在以下任一只读路径：

```text
<YAYA_ARTIFACT_ROOT>/<sha256>
<YAYA_ARTIFACT_ROOT>/<sha256 前两位>/<sha256>
```

文件必须是普通文件，内容摘要必须等于 Skill 的 `artifact_sha256`。Sandbox 会在容器启动前再次流式校验。生产发布流程应同时固定编译器 profile、compiler version、Sandbox image digest 和 test suite version。

## 3. 必需配置

| 变量 | 说明 |
|---|---|
| `YAYA_DATABASE_DSN` | 绝对 PostgreSQL DSN；账号需要 migration 和运行时表权限 |
| `YAYA_ARTIFACT_ROOT` | 绝对 artifact 根目录 |
| `YAYA_CONTRACTS_ROOT` | 含冻结 `manifest.json` 的绝对 `contracts` 目录 |
| `YAYA_AUTH_HMAC_SECRET` | 32—4096 字符，密钥系统注入 |
| `YAYA_AUTH_ISSUER` | JWT issuer |
| `YAYA_AUTH_AUDIENCE` | JWT audience |
| `YAYA_LLM_MODE` | `provider` 或显式 `fallback` |
| `YAYA_LLM_ENDPOINT` | provider 模式必需的 HTTPS chat-completions URL |
| `YAYA_LLM_API_KEY` / `YAYA_LLM_API_KEY_FILE` | 二选一；禁止提交密钥 |
| `YAYA_LLM_MODEL` | 模型身份，写入 VersionSet |
| `YAYA_LLM_PROVIDER` | provider 身份 |
| `YAYA_SANDBOX_IMAGE` | 必须为 `name@sha256:<64 hex>` |

可选变量与默认值：

| 变量 | 默认值 |
|---|---:|
| `YAYA_LLM_RESPONSE_FORMAT` | `json_object` |
| `YAYA_LLM_THINKING_MODE` | 未设置；支持该扩展的 provider 可显式设为 `enabled` 或 `disabled`。DeepSeek V4 严格 JSON 调用设为 `disabled` |
| `YAYA_LLM_MAX_RESPONSE_BYTES` | `2097152` |
| `YAYA_LLM_ALLOW_INSECURE_LOCALHOST` | `false` |
| `YAYA_HTTP_HOST` / `YAYA_HTTP_PORT` | `127.0.0.1` / `8080` |
| `YAYA_WORKER_ID` | `worker_agent_0001`；每个实例必须唯一 |
| `YAYA_WORKER_LEASE_SECONDS` | `30`，composition 会按 Runtime 最坏预算提升有效 lease |
| `YAYA_WORKER_POLL_MS` | `100` |
| `YAYA_SANDBOX_WALL_MS` / `YAYA_SANDBOX_CPU_MS` | `2000` / `1000` |
| `YAYA_SANDBOX_MEMORY_BYTES` | `67108864` |
| `YAYA_SANDBOX_MAX_INTENTS` | `64` |
| `YAYA_SANDBOX_MAX_OUTPUT_BYTES` | `65536` |
| `YAYA_SANDBOX_MAX_PROCESSES` | `1` |
| `YAYA_DOCKER_EXE` | `docker` |

未设置 `YAYA_LLM_THINKING_MODE` 时，adapter 不发送 provider-specific `thinking` 字段；这保持通用 OpenAI-compatible provider 的默认兼容性。设置后只允许精确的 `enabled` 或 `disabled`，非法值会在启动时失败。

`ProductionSettings` 对 DSN 和密钥关闭 repr，启动日志不得打印 settings 的原始 secret。

## 4. 发布顺序

1. 安装锁文件依赖，挂载冻结合同和只读 artifact。
2. 确认 PostgreSQL 可用，Docker daemon 可用，固定摘要镜像已拉取。
3. 运行一次 migration：

   ```powershell
   python -m yaya_agent_backend migrate
   ```

4. 启动一个或多个唯一 `YAYA_WORKER_ID` 的 Worker：

   ```powershell
   python -m yaya_agent_backend worker
   ```

5. 启动 HTTP：

   ```powershell
   python -m yaya_agent_backend serve
   ```

6. 用有效 JWT 和六个必需 attempt header 执行只读查询，再提交一条 canary Agent Turn。核对 Command、Run、World、Evidence、World Events 和 `Idempotency-Replayed`。

启动会 fail closed 校验合同 Manifest、migration checksum、JWT 配置、provider URL、Docker image digest 和 artifact 根目录。缺依赖不会切换到内存、SQLite、Mock server 或宿主 Sandbox。

## 5. Migration

`0001_agent_turn.sql` 建立 Task/World/Session/Skill/Run/Evidence/Invocation/Message/Trace/Command/Job/AgentTurn/Interaction/Event/Outbox 等生产表，并用复合外键闭合 tenant、actor、content、session、turn、command、world 与 run 身份。数据库约束还负责 Command 状态、lease 字段互斥、accepted turn 唯一预约、World revision CAS、Outbox 状态与 receipt 互斥。

`PostgresDatabase.migrate()` 使用 advisory lock 串行化并记录 migration SHA-256；已应用 migration 的字节漂移会拒绝启动。已发布 migration 不得原地修改，后续变更必须新增递增文件。

## 6. 停机与升级

先从负载均衡摘除 HTTP，再停止 Worker。HTTP CLI 在取消或 Ctrl-C 时调用 `shutdown()`、`server_close()` 并等待线程退出；Worker cancellation 会停止领取新租约，正在执行的副作用由 receipt、lease fencing 和重启对账收敛。升级后先 migrate，再启动 Worker，最后恢复 HTTP 流量。
