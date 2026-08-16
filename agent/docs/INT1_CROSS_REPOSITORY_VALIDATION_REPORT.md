# INT1 三仓验证报告

> Historical evidence notice：本文件冻结记录 2026-08-13 的 INT1 工作树、v0.4 发布和真实 Provider host-Docker 验收；文中的“当前”“唯一”和旧 head/测试数字均以该报告日期为准，不代表 2026-08-14 的 INT2 工作树。INT2 当前状态、v0.6 candidate 与未闭合门禁以 [`INT2_CROSS_REPO_VALIDATION_REPORT.md`](INT2_CROSS_REPO_VALIDATION_REPORT.md) 为准。
>
> 报告日期：2026-08-13（Asia/Shanghai）<br>
> 范围：`agent`、`walnut-world-backend`、`walnut-world-frontend`<br>
> 当前结论：**唯一 Gateway + fresh disposable PostgreSQL + digest-pinned host Docker + durable workflow/learner workers + 正式 Godot AppRoot + DeepSeek V4 Flash 的三仓真实 Provider 验收已在 194.12 秒取得 `REAL_PROVIDER_PRIVATE_DURABLE_RELAY` PASS；四轮学生闭环、Provider response-loss 对账、三服务新 PID 恢复和五类副作用指纹不变均有结构化证据。**

本报告是 INT1 的跨仓证据账本，不替代 Agent 单仓历史 A8 报告，并严格区分三层证据：一是合同、单元、focused integration 与 offline UI 测试；二是 fixture relay 驱动的本地确定性三仓诊断；三是 recoverable relay 后真实 Provider 的 live acceptance。历史 A6/A8 与 2026-08-12 的 direct-POST/fixture 运行仍只属于历史或确定性证据；2026-08-13 的第 4.4 节是当前唯一真实 Provider PASS。该 live 使用 digest-pinned host Docker，不是 production Compose private DinD live 证据。

## 1. 发布与运行身份

| 项目 | 报告当时锁定值 |
|---|---|
| Wire package | `@yaya/agent-contracts` / `yaya-agent-contracts` `0.4.0` |
| release ref | 远端 annotated `refs/tags/agent-contracts-v0.4.0` 已发布并指向 `0494c0f8ef6eb505e43db84c0249b046be35c589`；发布门禁同时验证 ref、commit 与 manifest 一致 |
| manifest | 26,127 bytes；138 files；SHA-256 `b62a6152f1f2fd87d1941beecd3a1d47089811e91b067a8b225ff0d7a5ce72b9` |
| v0.3 compatibility | `contracts/releases/agent-contracts-v0.3.lock.json` 按字节锁定旧发布边界 |
| Backend migration head | Alembic `017_durable_learner_worker`，父修订 `016_recoverable_llm_relay` |
| compiler/runtime image | `gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c` |

## 2. 已实现的唯一权威拓扑

Gateway、数据库 Schema/迁移、Worker、Wire Contract 与 provider-neutral library 的正式 owner 决策见 [INT1 单一 Gateway 与组件所有权 ADR](ADR-INT1-SINGLE-GATEWAY-OWNERSHIP.md)。该 ADR 是对当前实现的现状追认，不改写本文各证据层的历史时间线。

```text
Godot app_root.tscn
        |
        v
walnut-world-backend :8000       <- 唯一公开 HTTP Gateway
        |
        +-- Backend-owned Alembic/PostgreSQL tables and transactions
        +-- combined workflow-worker (Control + Build + Turn + terminal projection)
        +-- Agent v0.4 contracts / Runtime / Build / Sandbox library imports
        +-- private digest-pinned Docker daemon + persistent runtime/result volume
        +-- capability-verified recoverable relay -> configured real Provider
```

- `agent` 不作为第二公开产品后端，不运行生产 `yaya_agent_backend serve`，也不向 Walnut Gateway 提供第二套数据库或 `yaya_*` 私表。
- `walnut-world-backend` 是唯一业务写入、迁移和公开资源权威。Compose 只有 `backend` 暴露端口；`postgres`、`migrate`、private DinD、image preload 与 `workflow-worker` 不公开产品 HTTP，Worker 不挂载 Docker Desktop host socket。
- Student Bootstrap 返回 server-owned Session 创建模板、Build policy、完整 Activation scope/registry revision、exact active tuple 和 HTTP World 恢复权威。
- Session Control 终态在同一 Backend 事务创建 canonical Session、starter Draft 与 Workspace；后续 Draft CAS、Turn 接受和 terminal projection 同步推进 Workspace。
- terminal Turn 闭合 Run、World receipt、连续 Event、Snapshot、Evidence、Learner projection、Product AgentInteraction 与 Workspace high-watermark。Provider 失败保留已提交客观事实，但不发布 `provider_fallback` Interaction，也不推进 Learner。
- Production Provider 只允许 `YAYA_RECOVERABLE_LLM_V1` relay。启动 capability fail-fast；稳定 `dispatch_id` GET-first，仅权威 `ABSENT` 后同 ID PUT；`PENDING` 遵守 `Retry-After`，并联合验证 fence、PostgreSQL receipt、completion 与 raw Provider bytes hash。普通 direct chat adapter 仅属 best-effort，不进入 production worker。
- PatchDecision dormant router 在 production app 中默认不注册；WSS 与 Client Event Batch route 默认不挂载，Student Bootstrap 对应 capability=false。冻结 Schema 要求的 `stream_url` 是 inert、无凭据结构值，是否可用只由 capability 裁决。

## 3. 分仓门禁证据

本节不沿用旧 Agent full-suite 数字。Agent non-real-Provider 与 Backend fresh full 均已取得本轮最终全绿；它们和下面的 focused/offline 行仍不能被解释为第 4.4 节的真实 Provider PASS。

| 证据层 | 当前结果 | 判定边界 |
|---|---|---|
| Agent final full（non-real-Provider） | **574/574 PASS**；discovered 576、selected/run 574 | 2 个 real-Provider test ID 因缺 live 环境精确 NOT RUN；单独调用时 fail loud，不计 skip/PASS；本行只证明本地 non-real-Provider full |
| Agent `npm test`（`YAYA_PYTHON_EXE=.venv\Scripts\python.exe`） | **162/162 PASS**；TAP fail/skipped/todo=0；runner 14.017s，shell wall 17.2s | 最终 Node/contract 快照；不替代 real Provider |
| Agent Pyright（5 packages，显式 `.venv` Python） | **0 errors / 0 warnings PASS** | production/library type gate |
| Agent Ruff check + format-check `python tests` | **PASS**；139 files already formatted | lint/format gate |
| Agent TS typecheck + manifest + port-surface + compileall | **PASS**；manifest 138 files / v0.4 | v0.3 frozen surface 未漂移 |
| Agent isolated wheel build/install/import | **`AGENT_PYTHON_PACKAGE_TEST_OK` PASS**；56.8s | 必须显式 `YAYA_PYTHON_EXE=.venv\Scripts\python.exe` |
| Agent canonical Godot client | **2 runners PASS** | canonical client gate，不是 Frontend real-Gateway live |
| Agent final static + canonical combo rerun | **PASS**；wall 27.3s | Ruff check/format、Pyright 5 packages、TS、manifest 138、port surface、compileall、canonical Godot 2 runners |
| Agent A8 public process-restart marker | **PASS**；28.107s | 本地 process-recovery marker，不是 real-Provider cross-process acceptance |
| Agent `git diff --check` | **exit 0** | 仅既有 LF/CRLF notices |
| Backend fresh migration | **PASS**；zero → `017_durable_learner_worker` | 父修订 `016_recoverable_llm_relay`；fresh disposable PostgreSQL |
| Backend fresh full `.venv\Scripts\python.exe -m pytest tests -q -ra` | **299/299 PASS**；0 failure/error/skip；142.238s | 当前 fresh PostgreSQL full gate；不替代 real Provider |
| Backend Ruff | **PASS**；2.622s | `src` / tests / migrations gate |
| Backend Pyright `src` | **0 errors / 0 warnings / 0 informations PASS**；12.423s | production source gate |
| Backend Agent release verifier | **138 files PASS**；0.468s | manifest bytes pin；tag 创建后由 Agent 发布门禁复核 ref/commit/manifest 一致 |
| Backend `docker compose config --quiet` | **PASS**；0.429s | 静态 topology，不是 DinD runtime PASS |
| Backend lightweight contract/diagnostic group | **18/18 PASS**；pytest 12.50s，wall 14.57s | 不启动 DinD/Gateway/Provider |
| Frontend headless offline Godot suite | **46/46 PASS** | 包含 nullable Interaction、跨 Session cursor、pending Draft/Turn 跨两个 `ClientStore` 重启、生产 HTTP Draft PUT/CAS、HTTP transport host 生命周期与正式 UI display |
| Frontend real-Gateway opt-in guard | 2 个 opt-in test 仍从普通 offline discovery 中精确排除为 `EXCLUDED_NOT_RUN` | 不计入 46 项 offline PASS；同一正式 runner 已由第 4.4 节的显式 billable wrapper 在真实 Provider 拓扑执行并 PASS |
| Recoverable Provider focused gates | PASS | capability fail-fast、稳定 PUT/GET、lost PUT/GET response、`Retry-After`、lease/fence takeover、commit-ACK 重读、receipt/relay co-tamper 与 `generation_count=1` |
| Sandbox recovery focused gates | PASS | stable run/container/result receipt、start/create response loss、暂时 Docker 不可用、restart reconcile、无第二次 `run` |
| Build recovery focused gates | PASS | stable labelled container、start-attach response loss、terminal inspect/logs、retryable infra 不伪造 student `REJECTED` |
| Agent manifest check | 138 files / 26,127 bytes / locked SHA-256 | bytes pin PASS；annotated tag 门禁另行证明 ref/commit/manifest 一致 |
| Default-off transport/patch boundary | PASS | production 无 PatchDecision route；WSS/Event Batch 默认无 route 且 Bootstrap capability=false |
| Repo-owned recoverable-relay full-restart three-repo harness | **PASS**；169.836s | `DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER`；phase 1 四轮后真实 stop/start 同一 PostgreSQL 容器，再以新 PID 重启 Gateway/workflow/learner；phase 2 recovery-only 只读恢复，四类业务指纹在两道恢复边界前后均不变；不是 real-Provider PASS |
| Real Provider cross-process gate | **PASS**；194.12s | `REAL_PROVIDER_PRIVATE_DURABLE_RELAY`；DeepSeek V4 Flash；`source=provider`、`degraded=false`；13 unique dispatch / 13 generation、单 dispatch 最大 1 |

Frontend 的 2 个真实 Provider测试不在 46 项离线计数中；普通 full discovery 仍将它们精确标记为 `EXCLUDED_NOT_RUN`，不计 skip/PASS，缺受控 live 环境时 fail loud。这不再表示 live acceptance 未执行：增强后的同一正式 runner 已由显式 billable wrapper 在第 4.4 节的真实 Provider 拓扑执行。门禁强制 fresh revision-1 Draft 执行真实 PUT/CAS、Workspace exact ref，再 Build/Activation/Turn，并把 API/Store closure 与 TaskWorkspace、DialoguePanel、WorldViewport 的 UI display 指纹分开断言。

Agent 门禁曾有两次错误环境调用：未绑定 `.venv` 的 Pyright 因找不到 `psycopg` 失败，未设置 `YAYA_PYTHON_EXE` 的 wheel gate 因找不到 `build` 失败。二者均保留为命令环境错误记录；随后按仓库要求显式绑定 `.venv` 重跑并取得对应 Pyright/wheel 行的 PASS，不计为源码回归，也未被删除或改写为 skip。

Agent 早期 one-process full 尝试曾出现 37 failures；诊断确认 fresh-DB setup 没有清理 class-shared Artifact root 下的 `.sandbox-results`，因而让跨测试的旧 Sandbox receipt 污染后续用例。修复仅发生在测试 setup：严格限定并清理对应 result root，不改变 production recovery 语义；随后相关 focused matrices 全绿。下一次历史 full 为 558/559、1 failure、0 error/skip、1473.534s：`BookOutcomeMatrix` permanent-mastery/learner-reason case 错误路由为 `teaching_agent`。该失败在相同 discovery 预导入顺序的 prefix + target 8/8、同环境 target 重复 5/5 与 Book matrix 15/15 中均未复现；测试只增强失败诊断，没有以猜测修改 production 语义。其后的 559/559 是已被当前 574/574 门禁取代的历史全绿基线，不再作为当前数字；两次红灯与诊断过程均保留在台账中，不能删除或折算为 skip。

Backend 可复跑命令为：

```powershell
.venv\Scripts\python.exe scripts\verify_contract_release.py --agent-repo <agent-repo>
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m pytest tests -q -ra
uvx --from ruff==0.12.11 ruff check src tests migrations
uvx --from pyright==1.1.408 pyright src
.venv\Scripts\python.exe -m alembic downgrade 016_recoverable_llm_relay
.venv\Scripts\python.exe -m alembic upgrade 017_durable_learner_worker
docker compose config --quiet
docker compose build --check backend
```

较早的 Backend final-gate preflight 曾记录 Docker daemon `29.4.3`、C: 可用 24.20 GB、物理可用内存 506.4 MB；当时 fresh gate 只启动一个 `postgres:16.9` scratch instance，结束后 container 与 volume 已移除，58404 端口已释放。其后记录的 `free_memory_bytes=2221518848`、`free_disk_bytes=47646638080` 也属于已被新运行替代的历史快照。当前 repo-owned full-restart harness 的唯一权威 preflight 见第 4.2 节，不得把这些旧资源值描述成“最新”。

## 4. 跨仓 E2E 证据与状态

目标链为：

```text
Student Bootstrap
-> server-created Session + starter Draft + Workspace
-> Draft GET/PUT CAS
-> digest-pinned Docker Build/PUBLIC/HIDDEN tests/Certification
-> full-scope Activation registry CAS
-> original Session exact-version Turn
-> Run/World receipt/HTTP Events/Snapshot/Evidence
-> Learner/Product AgentInteraction/Workspace recovery
-> Godot display
```

### 4.1 历史 direct-POST 本地 wiring：HISTORICAL PASS

2026-08-12 的一次性 27.6 秒诊断使用正式 `app_root.tscn`、唯一 Gateway、fresh disposable PostgreSQL、production HS256 authority seeder、combined workflow worker、固定 GCC digest 和普通确定性 OpenAI-compatible POST 进程。它从 fresh PostgreSQL 开始，通过 Gateway 创建 Session 及下游业务结果，没有 SQL 预置 Session、Draft、Build、Artifact、Certification、Activation、Run、Evidence 或 Interaction。

该运行发生在 production Provider 切换为 recoverable relay、real-Gateway test 强制 Draft PUT/CAS、Workspace exact ref 和正式 UI display 指纹之前；旧脚本还位于临时目录，不再是可复跑权威。因此它只能标记为 **HISTORICAL LOCAL WIRING PASS**，不能写作当前 local diagnostic PASS，也不能证明 Provider response-loss recovery 或增强后的 Godot 显示闭包。

| 关键结果 | 观测值 |
|---|---|
| Session | `session_7742a13d9646157f7faeb81b` |
| Activation | `activation_44a00e938618bc93a8e21a55` |
| Run | `run_10c3a1374500aa9a34ad3092` |
| Product AgentInteraction | `interaction_6872706fe0caf2a0f1b76d55` |
| Run response Evidence | `evidence_count=2` |
| World closure | revision `1`；event sequence `1` |
| Godot closure | 当时的 Bootstrap、Session、Draft、Build、Activation、Turn、Run、Events、Snapshot、Evidence、Learner、Interaction、Workspace 与 Store 闭合；没有执行当前强制 Draft PUT/CAS，也没有逐控件 UI display 指纹 |

Windows 首次诊断暴露长路径预算：Artifact receipt 的原子 hard-link 在深 `%TEMP%` 路径下超过 Windows 路径限制。诊断 runner 改用 `%TEMP%\wi1-<8-hex>` 短 runtime root 后通过；生产 Compose 的 `/var/lib/walnut` 路径不受此 Windows 主机限制。后续 Windows 重放必须保留短 runtime root 约束。

#### 历史 durable 副作用指纹

诊断完成后从 Backend-owned PostgreSQL 和 Docker 事实对账：

| 指纹 | 终态 / 数量 |
|---|---|
| Commands | 4，全部 `APPLIED` |
| Workflow jobs | 4，全部 `SUCCEEDED` |
| 唯一 durable receipts | 10 |
| Provider receipts | dispatch/result ordinal `1` 各一次，dispatch/result ordinal `2` 各一次 |
| 其他 receipts | Sandbox、Build、Activation、Session、Skill invocation、Turn 各一次 |
| Artifact / Certification / Activation / Run / Interaction | 各 1 |
| Evidence | 共 4，包含 Learner Evidence；其中 Run wire 返回 2 |
| Domain events | 3 |
| Learner projection | revision `1` |
| World | revision `1`，event sequence `1` |
| Agent 私表 | `yaya_*` tables = 0 |

这些指纹证明旧 wiring 运行当时没有重复 Provider、Docker/Sandbox 或业务终态副作用；它们不属于当前 recoverable relay harness 的结果，也不替代当前真实 Provider 或增强 Godot 链。

### 4.2 Repo-owned recoverable-relay 三仓诊断：DETERMINISTIC LOCAL PASS

当前可复跑权威是 Backend 仓的 `scripts/run-int1-local-diagnostic.ps1`；说明见 [INT1 deterministic local diagnostic](../../walnut-world-backend/docs/operations/int1-local-diagnostic.md)。它使用 `int1_recoverable_relay.py` fixture，标识为 `DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER`，并调用正式 Frontend real-Gateway runner。

```powershell
Set-Location ..\walnut-world-backend
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-int1-local-diagnostic.ps1
```

最新 harness preflight 观测到 `free_memory_bytes=2837123072`、`free_disk_bytes=53827231744`、运行中容器数为 0、所需 PostgreSQL/GCC 镜像均已存在，且 contract 固定的 loopback Gateway 端口 `127.0.0.1:8790` 可用。完整诊断在 **169.836s** 内输出 `INT1_LOCAL_DIAGNOSTIC_PASS`，分类严格保持 **`DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER`**。Windows 跨仓诊断使用 digest-pinned host Docker 和短 runtime root；生产 Compose 的 private DinD 是独立静态/部署拓扑门禁，不得把本次结果记为 private-DinD live 证据。

Phase 1 实际闭合 4 个 Turn/Run：Run 状态为 `REJECTED / REJECTED / REJECTED / SUCCEEDED`，对应 4 个 Turn Command 为 `REJECTED / REJECTED / REJECTED / APPLIED`，Interaction 角色为 `teaching_agent / teaching_agent / bug_agent / book_agent`。Run 与 Interaction 总数均为 4，且每个 Turn 各唯一一条；4 个 learner projection 均闭合，learner revision 到达 `4`；全链共有 9 个 Command，9 个均为 terminal。Harness 还闭合 Draft CAS、Workspace、两次 Build/Certification/Activation、HTTP Events、Snapshot、Evidence，以及 TaskWorkspace、DialoguePanel、WorldViewport 三个正式 UI panel；使用 canonical Workspace resource URL，并按 server-owned UI Content identity 对账。

受控 response-loss 指纹为：`put_ack_drops=1`、`forced_reconcile_unavailable=1`、`reconcile_gets=2`、`turn_job_attempt=2`、`turn_job_fencing_token=2`、`worker_reconcile_receipts=1`、`worker_failure_receipts=0`、`generation_count_max=1`。Relay 记录 `relay_unique_dispatches=12`、`relay_total_generations=12`，与 Backend 的 `provider_dispatch_count=12` 一致；Sandbox receipt 为 8，Artifact 文件为 2。完整 authority tuple 的当前 `side_effect_sha256` 为 `9d9e770a6bf8f9f03fc351c50a3fba2dd3d57971df91237d46f9e49c3335ab05`；PostgreSQL 端口不可用 4,784 ms 后三项服务均建立了新连接。

Phase 1 终态后，harness 先真实 stop/start 同一个 disposable PostgreSQL 容器并保留它的 run-unique volume。发布端口关闭 4,784 ms；期间 Gateway 保持监听，且对数据库依赖请求返回预期 fail-closed 错误。数据库恢复后，Gateway、workflow worker 和 learner worker 均建立了新 PostgreSQL 连接，DB/relay/Sandbox/Artifact 四类指纹在 outage 前后逐字节不变。

随后 harness 强停 Gateway、workflow worker、learner worker，证明中间无 8790 listener，再以新 PID 和新 worker identity 启动三项服务。Phase 2 的 recovery-only Godot 进程只产生 8 个 GET、0 个 mutation；deterministic relay capability probe 从 `1 → 2`，精确 `+1`，是新 worker 的预期只读启动探测。进程重启前后四类业务指纹再次不变：relay `dd565a3ad002a0c11db2a098dd0d074f7e5cd93996af5d3580bbbe926ac77661`、database `45f85c322f1bbb9cc95e97219af579a83176ce2d6deacbdb559b82879c28956a`、Sandbox `5b1692ff07f3256730e10545f1f195d490c17cff313645948dc165fcacfa9b19`、Artifact `8e534551a9d79904e983b1a2784f5e5804efc33fcaa72457d4dc3ecb8e6df43e`。

这条 PASS 证明 repo-owned fixture relay 下的确定性三仓 wiring、恢复和 UI 闭包；fixture 明确不调用真实 Provider，当前 live 结论只来自第 4.4 节的独立真实 Provider 运行。

### 4.3 Provider / Sandbox / Build 恢复合同：PASS

这些证据来自单元、focused integration、fresh PostgreSQL 或 pinned Docker，不是 fixture 三仓链，更不是 real-Provider live：

- Provider：worker 启动 capability fail-fast；PostgreSQL dispatch receipt 先落地；稳定 `dispatch_id` GET-first，仅线性一致 `ABSENT` 后同 ID PUT；lost PUT response 与首次 GET 后由新 lease/fence 恢复同一终态，`generation_count=1`；`PENDING` 的 `Retry-After` 用 DB clock 调度；dispatch/result commit-ACK 丢失后重读；completion、result、raw Provider bytes、receipt hash 与 relay authority 任一或共同篡改均 fail closed。
- Sandbox：稳定 invocation/request/context/image hash、持久 result root、不可变 result receipt 和全标签容器；`SANDBOX_DISPATCHED` 后 worker 只 `reconcile`，不再次 `run`；start/create 控制面 response loss、worker restart 和 Docker 暂不可用都对账同一容器/结果，未物化的临时依赖失败保持 retryable，不伪造 terminal Run。
- Build：稳定、全标签 phase container；`docker start --attach` 响应丢失或返回非零时先 inspect/wait 同一容器，terminal 输出以 bounded `docker logs` 为权威；暂时 control-plane 不可用返回 retryable infrastructure 状态，Backend 不把它伪装成学生代码 `REJECTED`。

这关闭了旧报告中“generic Provider/Sandbox 只会 fail loud、不能恢复成功结果”的实现风险；第 4.4 节已把真实 Provider response-loss 与恢复后的副作用指纹归档进 live 证据。

### 4.4 真实 Provider 验收：PASS

2026-08-13 的唯一成功 live 由 Backend `scripts/run-int1-real-provider-e2e.ps1` 显式 opt-in 启动，正式 Godot AppRoot 只访问唯一 Gateway；Gateway 后连接 fresh disposable PostgreSQL、durable workflow/learner workers、私有 recoverable relay 和 DeepSeek V4 Flash。完整运行在 **194.12s** 输出 `INT1_LOCAL_DIAGNOSTIC_PASS`，分类为 **`REAL_PROVIDER_PRIVATE_DURABLE_RELAY`**，最终 Product Interaction 为 `source=provider`、`degraded=false`。可复现入口为版本化wrapper；脱敏`stdout.log` SHA-256为`2ea58a686b641820855ba4424994d5fbecd783f969cadfbd8c5c6c7d815bdbda`，`stderr.log`为0 bytes。本机临时目录不再作为证据位置。证据不包含Provider credential。

结构化闭包如下：

- 4 个 Turn、4 个 Run、4 个 Product Interaction 和 4 个 Learner projection；Command/Run 终态为 `REJECTED/REJECTED/REJECTED/APPLIED` 与 `REJECTED/REJECTED/REJECTED/SUCCEEDED`，角色为 `teaching_agent/teaching_agent/bug_agent/book_agent`。
- 2 个 Build、2 个 Certification、2 个 Activation；全链 9 个 terminal Command、11 个 Evidence、8 个 Sandbox receipt、2 个 Artifact 文件。
- Provider authority 为 13 个 unique dispatch / 13 次 generation，`generation_count_max=1`。受控 PUT ACK response loss 发生 1 次，强制 reconcile unavailable 已尝试 1 次且实际送达 1 次；恢复沿用同一 dispatch，最终 generation count 仍为 1。
- Gateway、workflow worker、learner worker 在 phase 2 使用新 PID/worker identity；recovery-only 审计为 8 GET / 0 mutation。relay、database、Sandbox、Artifact 与 response-loss proxy 五类指纹在重启前后逐字节不变。
- 结束时 stderr 为 0 bytes；清理后运行中 Docker 容器为 0、`127.0.0.1:8790` 无监听。

边界不随 PASS 被扩大：这是 **digest-pinned host-Docker live**，不是 production Compose private DinD live，也没有证明 private DinD 的真实 Docker control-plane fault。Godot 指纹中的 `live_pending_response_loss.status` 仍为 **`NOT_PROVEN`**，因为公开 Gateway 没有验收安全的“业务写已提交但客户端未收到响应”故障注入；本次已证明的是私有 Provider relay PUT/GET response-loss 恢复。普通 offline discovery 中两条 opt-in 用例继续为 `EXCLUDED_NOT_RUN`，只是测试选择状态，不否定本次显式 live PASS。

## 5. 诊断驱动的缺陷闭合

历史 wiring、focused recovery 与 offline UI 门禁把 HTTP/serialization/recovery 边界问题转成了回归测试并修复：

- Run wire 保留 `ActionIntent` discriminator，并让 Run producer 与读取投影使用同一个 canonical timestamp；
- Command response projection 正确处理 optional `EvidenceRef.uri`，不再因缺省字段触发 `RESPONSE_SCHEMA`；
- Evidence GET 返回并验证 canonical `ETag`；
- Run、Evidence、World 与 receipt 使用一个不早于 durable Command/Job 的 PostgreSQL-authoritative 发布时间；正常、fallback 与 receipt-recovery 的 `AgentDecision.completed_at` 均不早于事件和所引用 Evidence，关闭 host/VM clock skew；
- Provider production 配置从不可恢复的 direct chat endpoint 收敛为 capability-verified relay；dispatch/result receipt 与 relay completion/raw bytes 联合对账，`Retry-After`、fence takeover、commit-ACK loss 和 co-tamper 均 fail closed；
- Sandbox 将稳定 run/container identity 与持久 result receipt 暴露给 Backend reconcile；Build 在 attach response loss 后使用 terminal inspect/logs 恢复，两者的暂时 Docker 不可用都保持 retryable，不伪造业务失败；
- Frontend 对 nullable `hint_level` / `question` 安全渲染，不因合法 `null` 阻断 Interaction UI；
- Frontend 切换 Session 时清除旧 Session 的 Interaction cursor 和 pending envelopes，同一 Session 恢复时仍保留它们；pending Draft/Turn 跨两个 `ClientStore` 重启复用原 body/key/identity，Draft 使用生产 HTTP transport 真实 PUT/CAS；
- real-Gateway gate 把 API/Store closure 与 TaskWorkspace、DialoguePanel、WorldViewport 的正式 UI display 分开断言，且只有 canonical Workspace URL 返回的 exact Draft ref 闭合后才 Build；UI Content identity 由 server-owned Bootstrap authority 提供，不再由 harness 猜测。

## 6. 恢复边界与排除项

分仓门禁已覆盖 byte-equivalent replay、同 key 不同 body fail closed、lease/fencing takeover、corruption、Provider/Sandbox/Build 成功后 response loss 和临时依赖不可用。Recoverable Provider、Sandbox 与 Build 已能从 durable relay/container/result authority 恢复成功结果，而不只是 fail loud/dead-letter；repo-owned fixture harness 把这些边界闭合为 deterministic full-chain PASS，第 4.4 节又以真实 Provider 证明私有 relay response-loss 与进程恢复不重复 generation 或业务副作用。

当前仍未关闭的发布/部署边界：

- 远端 annotated `refs/tags/agent-contracts-v0.4.0` 已发布并指向 `0494c0f8ef6eb505e43db84c0249b046be35c589`，并由 `--verify-git-ref` 复核 ref/commit/manifest 一致。
- production Compose private DinD 尚无独立 live 证据；host-Docker PASS 不外推为 private DinD 或真实 Docker control-plane fault PASS。
- 公开 Gateway 的 live pending write response-loss 仍为 `NOT_PROVEN`；offline 跨 ClientStore 恢复用例不把该项提升为 live。私有 Provider relay response-loss 已由第 4.4 节证明。

INT1 明确排除且保持关闭：

- Skill Patch/PatchDecision 主链；production 默认不注册 dormant PatchDecision router，`allow_skill_patch=false`、`patch_eligible=false`、`full_solution_eligible=false`；
- WSS 与 Client Event Batch；production 默认不挂载，Bootstrap capability=false，冻结 Schema 的 `stream_url` 为 inert 值；INT1 只以 HTTP Events/Snapshot 闭合世界恢复；
- Feishu；
- 空账号/内容创作、自动 Patch/Build/Activation 和 UI 美术重做。

第 4.4 节真实链已经取得可审计的非降级 Provider PASS，v0.4 annotated tag 也已发布；production private DinD live 与 Gateway pending response-loss 仍按上述边界单列，不得把它们写成已证明。
