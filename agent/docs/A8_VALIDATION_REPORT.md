# A8 发布门禁验证报告（Agent 单仓历史证据）

> **INT1 状态说明（2026-08-12）：** 本文件记录 2026-08-10 至 2026-08-11 的 Agent 仓历史 A8 composition 结果。它不验证当前 `walnut-world-backend` 唯一 Gateway/数据库/迁移/combined worker，也不验证正式 Godot AppRoot 的三仓跨进程链，因此不能作为 INT1 PASS。INT1 当前 `WALNUT_LLM_RELAY_ENDPOINT`、`WALNUT_LLM_RELAY_API_KEY`、`WALNUT_LLM_PROVIDER`、`WALNUT_LLM_MODEL` 在 Process/User/Machine 均未配置，跨仓 live 状态仍为 **NOT PASS**；以 [`INT1_CROSS_REPOSITORY_VALIDATION_REPORT.md`](INT1_CROSS_REPOSITORY_VALIDATION_REPORT.md) 为当前证据账本。

> 执行时间：2026-08-10 至 2026-08-11（Asia/Shanghai）。历史 Agent 单仓结论：**PASS / COMPLETE**。
> 最终工作树的冻结边界、静态检查、531 个 Python 测试、真实 PostgreSQL、pinned Docker、跨 OS 进程恢复、真实 DeepSeek Provider、wheel 与 Godot 均已通过。两个 live E2E 没有 skip、mock Provider、degraded fallback 或 host compiler fallback。
>
> Provider key 仅通过临时进程环境变量注入；本报告和仓库均不记录、回显或提交 secret。

## 1. 发布身份

| 字段 | 实际值 |
|---|---|
| Git 基线 | `78411205613db5561c099c4419d118144fc60619` |
| 工作区 | dirty；tracked `git diff HEAD` SHA-1 `6c3c7a57d5a23ad280962833126514465d78fcbf`；26 个 modified、29 个 untracked 条目 |
| OS / architecture | Windows NT `10.0.26200.0` / x64 |
| Node / npm | Node `v24.16.0` / npm `11.13.0` |
| Python | CPython `3.12.13`，锁定解释器 `.venv/Scripts/python.exe` |
| PostgreSQL | server/client `15.18`；测试使用真实 disposable database |
| Docker | client/server `29.4.3`；Linux amd64 daemon |
| compiler image | `gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c` |
| Godot | `4.5.2.stable.official.6ce3de25a` |
| package | Node `@yaya/agent-contracts@0.3.0`；Python `yaya-agent-contracts@0.3.0` |
| Provider | `deepseek`；model `deepseek-v4-flash`；endpoint `https://api.deepseek.com/chat/completions`；thinking disabled；key 仅临时注入且未落盘 |

工作区包含本目标实现及既有用户文件 `docs/assets/`、中文演示 HTML；未替用户删除、覆盖、暂存或提交这些文件。

## 2. 冻结边界

| 门禁 | 数量 / 结果 | 证据 |
|---|---:|---|
| contract manifest | PASS | `files=135 refs=1469 operations=30 events=25 errors=26 examples=61` |
| port surface exact-set | PASS | `PORT_SURFACE_SIGNATURES_OK` |
| `contracts/**` | 0 diff | `git diff --quiet -- contracts` |
| `src/ports/**` | 0 diff | `git diff --quiet -- src/ports` |
| package/lock/project identity | 0 diff | `package.json`、`package-lock.json`、`pyproject.toml` |
| 公共 route/operation 集 | PASS | manifest 仍为 30 operations；没有新增 Certification operation |
| Skill Patch 三重门禁 | PASS | 5/5 role `allow_skill_patch=false`；`patch_eligible=false`；`full_solution_eligible=false` |

冻结命令 `git diff --quiet -- contracts src/ports package.json package-lock.json pyproject.toml` 返回 0。`git diff --check` 也返回 0；输出中只有现有 LF→CRLF 提示，没有 whitespace error。

## 3. 当前树门禁命令账本

以下均在最终工作树执行；耗时是各命令实测，不把 focused 重跑时间累加到全量耗时。

| 命令 / 门禁 | 数量与结果 | 耗时 | 状态 |
|---|---|---:|---|
| `npm run validate` | 135 files、1,469 refs、30 operations、25 events、26 errors、61 examples | 3.2s | PASS |
| `npm run port-surface:check` | exact signatures | 2.8s | PASS |
| `npm run typecheck:ts` | 0 errors | 3.3s | PASS |
| `npm test` | 160/160，0 fail/skip/todo | 16.0s（TAP 13.256s） | PASS |
| `python -m ruff check python tests` + format check | 127 files；127/127 formatted | 3.1s | PASS |
| strict production Pyright | 0 errors / 0 warnings / 0 info | 9.9s | PASS |
| `python -m compileall -q python tests` + diff/frozen check | 127 Python files；0 error | 2.9s | PASS |
| `npm run verify` | contract/port/ts/node/pyright/ruff、Python 531/531、compileall、wheel、Godot 全过；0 skip | 1,430.5s；其中 Python 1,343.052s | **PASS** |
| wheel gate（包含在当前树 `npm run verify`） | wheel build、clean venv install、`pip check`、import/resource checks | included above | PASS |
| `npm run test:godot` | contract + HTTP transport 2/2 | 9.0s | PASS |

最终 `npm run verify` 在同一次脚本执行中完成了 Python、wheel 与 Godot，并在当前 tracked 快照返回 0。Python discovery 精确执行 531 项，结果为 531 pass、0 failure/error/skip。wheel 明确包含：

- `0001_agent_turn.sql`
- `0002_learner_projection.sql`
- `0003_student_skill_chain.sql`
- `0003_student_skill_chain.down.sql`

## 4. A8 provider-independent 专项证据

| 专项命令组 | 数量 | 耗时 | 边界与结果 |
|---|---:|---:|---|
| database + PostgreSQL migration + roundtrip | 26/26 | 70.420s | 真实 PG；ledger/hash drift、完整列/约束/guard、committed UP→DOWN→UP、重复 migrate 幂等、非空 DOWN 拒绝，PASS |
| Draft failure + durable Build worker/skill-build failure matrices | 20/20 | 70.907s | 真实 PG；Draft HTTP/CAS/receipt/path 与 Build terminal/history/receipt 分类，PASS |
| Certification durable corruption matrix | 8/8 | 28.077s | 真实 PG + Artifact FS；强制篡改后 public GET/Activation fail closed，PASS |
| Session/Build acceptance 与 Session final COMMIT-ack loss | 3/3 | 8.507s | 真实 PG + production HTTP/application/worker；重建后 exactly one，PASS |
| Session/Activation failure + recovery matrix | 18/18 | 34.994s | 真实 PG；全 `yaya_*` 数据库指纹及 Artifact path/bytes/mode/mtime 指纹，PASS |
| public localhost 前门链 | 1/1 | 16.693s | 真实 HTTP + PG + 持续 worker + pinned Docker；Session→Draft→Build/Certification→Activation，PASS |
| fresh-process crash/replay | 1/1 | 35.022s | 两代真实 `serve`/`worker` OS PID、同 PG/root、pinned Docker；完整 replay 零新增，PASS |
| real Build lease takeover | 1/1 | 14.152s | 真实 PG + pinned Docker；新 executor/worker 复用 FS receipts/CAS，新增 Docker starts=0，PASS |
| Build primitive / container takeover hardening | 19/19 | 1.846s | 注入 `RecordingDockerRunner` 的 primitive 单测；31 个 inspect drift 子分支，PASS |
| corruption + public surfaces + runtime Certification closure | 18/18 | 44.461s | 8 + 6 + 4 个测试；真实 PG 分支与内存 seam 混合，PASS |
| 最终 scope/closure 定向回归 | 109/109 | 240.413s | 当前树真实 PG/Docker；migration/roundtrip、非 UTC public binding、Skill invocation、Learner Store/Projection、public surfaces 与 Certification corruption，PASS |

focused 命令互有重叠，也全部包含在 531-test discovery 中；上表用于说明故障边界，不能把各行相加当作唯一测试总数。

### 真实边界与故障注入的区别

- `test_agent_backend_build_pipeline.py` 的 19 个方法使用 recording runner，证明 deterministic identity、严格 Docker inspect 对账和 CAS primitive，不声称启动真实 Docker。
- `test_agent_backend_build_worker_failure_matrix.py` 的 8 个方法使用真实 PG、注入 builder 结果，证明 worker 的分类、终态与持久化闭包。
- `test_agent_backend_skill_build_recovery_cleanup.py` 的 3 个方法使用真实 PG、注入成功 result；`test_agent_backend_student_chain_worker_recovery.py` 的 2 个方法是内存/mock 恢复单测。
- 真实 Docker 证据来自 Build executor、public localhost chain、failure matrix 中的真实分支、real takeover，以及 fresh-process crash/replay。production 路径没有 host compiler fallback。

## 5. 失败副作用、迁移与恢复闭包

- 中央 `a8_state_fingerprint` 在 repeatable-read 中覆盖迁移定义的 46 张业务表，并动态纳入额外 public `yaya_*` 表；比较 row count 与无序行 hash，缺表显式失败。
- Draft public PUT 已覆盖 absolute `/src/main.cpp` 与 traversal `src/../main.cpp`，并用全业务指纹证明拒绝后无新增或修复。
- Session/Activation 矩阵仅排除预期终态化的 Command/control-job ledger；其余全部数据库表以及 Artifact path、内容、mode、mtime 必须保持精确相等。
- Certification 矩阵强制注入 Build request context、phase、diagnostic、receipt identity、Evidence ownership、VersionSet、legacy rejected 与 CompileResult mirror 漂移；public GET 和 accepted Activation 均 fail closed，不静默修复。
- `0003_student_skill_chain.down.sql` 是 fail-safe reverse migration：A8 表非空时拒绝；空库真实执行 committed UP→DOWN→UP，且 forward scanner 不误执行 `.down.sql`。
- response-loss 测试覆盖 Session accept、Session worker final 和 Build accept（含 accepted Build 插入）COMMIT acknowledgement 丢失；重建后按相同 key/Location/GET 收敛到唯一资源。
- fresh-process 测试的本次 PID 为 serve `67928→68356`、worker `76408→46484`。第一代进程在完整终态后 hard crash/reap，第二代使用相同 PG、Artifact root 与原请求；Session、Build、Artifact、SkillVersion、Certification、Activation 各 1，数据库/Artifact 指纹不变，无 replay Docker create、容器或 workspace。
- real takeover 测试在第一 worker 完成 compiler probe/compile/public/hidden 后制造 final transaction unknown；第二 worker attempt/fence=2 不新增 Docker start，并复用同一只读 CAS inode/size/mtime/hash 收敛到唯一认证闭包。

## 6. 真实 Provider live E2E

| Gate | 发现数量 | 实测耗时 | 结果 |
|---|---:|---:|---|
| A6 real Provider teaching/teaching/Bug→成功→书书 | 1/1 | 60.454s | PASS |
| A8 public v1/v2 Session→Draft→Build/Certification→Activation→Turn | 1/1 | 125.075s | PASS |

两个 discover 命令各精确发现 1 个测试，均使用真实 `deepseek-v4-flash`，退出码为 0；没有 skip、fake Provider 或 degraded fallback。认证预检通过 `/models` 确认 key 可用且目标 model 存在。key 未写入 `.env`、配置文件、测试日志或仓库。

### A6 证据

- durable counts：Commands 4、Events 13、Evidence 5、Interactions 4、Invocations 4、Jobs 4、Learner jobs/receipts 4、Messages 4、Model requests 13、Runs 4、Turns 8、World revision 6、World sequence 1；Learner failures 0；
- Provider 请求：`xiaohutao=8`、`teaching_agent=2`、`bug_agent=2`、`book_agent=1`；
- 三次真实失败后产生真实 Bug，v2 成功只推进一次 World，最终 Book 同时消费 v1/v2 lineage；
- 原请求 replay 与 composition restart 后，Provider trace、数据库与副作用指纹不增加。

### A8 证据

- 2 个 Draft revision/receipt、2 个 Build、6 条 Build history、10 条 Build receipt、2 个 Artifact/SkillVersion/Certification/Activation、1 个 public Session、2 个 session binding；
- Commands 9、control jobs 5、Turns/Interactions/Runs 4、Evidence 7、Learner revision 4、Registry revision 2、World revision 6；
- 本次 Provider 请求为 `xiaohutao=8`、`teaching_agent=3`、`bug_agent=2`、`book_agent=1`；额外的一次 teaching 请求是合法的 `get_current_run` 工具二轮，角色触发仍精确为 teaching/teaching/Bug/Book，replay 后请求数不增长；
- Session、Draft v1/v2、Build v1/v2、Activation v1/v2、四个 Turn 的原请求 replay，以及 composition 重建后 replay，均保持 Provider/数据库/Artifact 指纹不变。

真实 live 首次执行还暴露并修复了三个 provider-independent 测试此前未经过的公共运行时缺口：

1. public Registry 的 plain `ActiveSkill` 被旧 codec 路径当作 tagged 对象解码；现在公共读取从 immutable Certification + Registry revision/time 重建并逐字段/哈希对账，A6 legacy tagged 路径不变；
2. at-rest `x-yaya-certification` 元数据被误送入模型工具 JSON Schema；现在仅在模型/工具输入边界剥离该内部字段，认证闭包不变；
3. Learner Projection 只认可 A6 `Skill.session_id`；现在严格认可 A6 legacy binding 或 A8 immutable `session_skill_versions` 全字段+哈希闭包，二者互斥。

最终 `npm run verify` 重新执行了两条 live 测试并通过；上述 focused 结果用于保留可读的独立耗时和证据计数。

## 7. 未交付能力与残余风险

- 未交付：SkillPatch、PatchDecision、patch confirmation、Product SessionWorkspace/ContentUnit 创作权威、Godot 学生 UI、World WSS、Client Event Batch 新纵切、飞书和空账号注册；三项 patch eligibility 仍为 false。
- Certification 仍只属于成功 Build 终态，不存在、也未新增独立公共 Certification operation。
- 真实 Provider 是外部依赖；门禁按设计 fail-loud，部署仍需独立的 secret 管理、限流与可用性监控。
- Docker Desktop 重启后曾自动恢复两个与本目标无关的 Compose 栈；当 Windows 可用内存仅约 483 MB、两个 Supabase 容器处于 restart loop 时，focused A8 明确暴露一次 `COMPILE_TIMEOUT` 和一次 FAILED Run。经用户授权临时停止 `solo-ios-ai-app` 与 LibreChat 栈后，A8 1/1、A6 1/1 均通过；随后 12 个原容器已全部按原名恢复运行。该过程没有被记作产品 PASS，而是作为本机容量/隔离风险保留。
- Build takeover 依赖 worker 可访问同一 Docker daemon、持久 workspace 与 Artifact root；跨主机部署必须提供共享/等价持久化语义，不能把本地磁盘当作隐式分布式存储。
- 当前工作树仍是 dirty 且包含用户既有文件；本 Goal 未获授权执行 stage、commit、push 或删除。

## 8. 最终结论

| 项目 | 状态 |
|---|---|
| 冻结 contracts / ports / package identity | PASS |
| 静态、Node、TypeScript、Python 531/531、PG、Docker、process recovery | PASS |
| wheel（含 0003 UP/DOWN）与 Godot | PASS |
| A6/A8 real Provider live | PASS，2/2 |
| Goal | **COMPLETE** |

最终可审计结论：**531 run = 531 pass，0 failure，0 error，0 skip**；真实 A6/A8 live、后续 wheel/Godot 与整套 `npm run verify` 全部 PASS。临时 Provider 环境变量已在命令结束时清除。
