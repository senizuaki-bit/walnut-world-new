# A8 前置门禁：学生源码公共生产链权威与证据矩阵（INT1 ownership 更新）

> INT1 当前生产 owner 是 sibling `walnut-world-backend`：唯一公开 Gateway、PostgreSQL、Alembic head `017_durable_learner_worker`（parent `016_recoverable_llm_relay`）、Control/Build/Turn/terminal projection 及所有 Product 资源都在 Backend。Agent 仓只提供 v0.4 contracts、Ports、Runtime、Build/Sandbox 与教学库；本文件中关于 Agent 历史 `yaya_agent_backend` composition 的描述仅作 A8 回归背景，不是第二套产品服务。真实 Provider 三仓 E2E 仍为 **NOT PASS**；见 [`INT1_CROSS_REPOSITORY_VALIDATION_REPORT.md`](INT1_CROSS_REPOSITORY_VALIDATION_REPORT.md)。

> 状态：本文记录实现必须遵守的权威、事务和验收边界，不是门禁通过报告。未配置真实 Provider，或任一必须门禁未运行、被 skip、出现 degraded/fallback 时，该纵切都不得标记为完成。<br>
> 公共 Wire 权威：`contracts/manifest.json` 及其引用的 OpenAPI、AsyncAPI 和 JSON Schema；本文不增加 operation、字段或枚举。<br>
> 准确范围：“已发布内容与初始世界权威后的学生源码公共生产链”，不是“空账号旅程”。

## 1. 唯一先后顺序

首次进入任务时，Product SkillDraft 的 URL 已经需要 canonical `session_id`，所以不存在 `Draft → Build → Activation → Session` 这条首次创建顺序。唯一合法顺序是：

```text
已发布 Task / Content + 初始 World + Learner / actor + Agent Profile 权威
→ GET Game Bootstrap
→ POST AgentSession（durable 202 Command / Job，服务端生成 session_id）
→ GET canonical AgentSession
→ 在该 Session 下 GET / PUT Product SkillDraft（revision/hash CAS）
→ POST SkillBuild（请求自带当前完整 source_bundle）
→ Build Worker 用 pinned Docker C++20 编译、public/hidden tests
→ 仅成功 Build 在终态内生成 Artifact + SkillVersion + immutable Certification + Evidence
→ POST exact SkillVersion Activation（registry revision CAS）
→ GET canonical Activation
→ 在原 AgentSession 提交绑定 exact SkillVersion / Artifact / Certification 的 Agent Turn
→ 现有 Run / Evidence / World CAS / teaching / Bug / 书书 / Learner / Product Interaction 链
```

Session 创建不要求 Active Skill，也不得隐式激活任何 Skill。Activation 之后，Agent Turn 才能把运行绑定到 exact certified version。恢复时如果已知 `session_id`，可以直接 GET Session 并恢复 Draft；这不改变首次创建顺序。

## 2. 允许的初始权威

测试和部署可以通过一个集中、可审计的 helper 设置冻结合同没有公共创建入口的平台权威：

- 已发布 Task / Content；
- 初始 World 和 canonical checkpoint；
- Learner 与 authenticated actor identity；
- Agent Profile；
- compiler profile、测试套件、版本政策和 digest-pinned Docker image；
- 空的 content-addressed Artifact root。

集中 helper 必须使用 canonical Schema、输出可复核的初始 fingerprint，且不得预置以下任何结果：

```text
AgentSession / SkillDraft / SkillBuild / Compile Result / Artifact
/ SkillVersion / Certification / Activation / Active Registry
/ Run / Evidence / AgentDecision / Message / Interaction
/ LearnerInference / Learner revision
```

## 3. 数据权威与事务边界

### 3.1 Bootstrap 与 Session

Session 请求没有 `task_id`。服务端必须用以下闭包解析唯一 published Task authority：

```text
tenant + authenticated actor + learner
+ content unit / content version / content hash
+ world_id + agent_profile_id
→ exactly one active published Task authority
```

查无结果、多个结果、actor/content/world/profile/learner 错链或 `expected_world_revision` 过时均 fail closed，不能任选 Task。

Session POST 的接受事务原子保存 Command、专用 control job、请求字节/hash 和首次 `202 AcceptedGameJob` receipt。Worker 只在有效 claim/lease/fencing 下创建服务端 Session ID，并在终态事务中闭合 Session resource、Command result 与 job。同 key+同字节请求重放原 receipt；同 key+不同字节为 `409 IDEMPOTENCY_KEY_REUSED`。

### 3.2 Product SkillDraft

Draft 是 Session 下的可变源码权威，但不是 Build 的隐式输入源。创建使用 `base_revision=0` 和 `base_draft_sha256=null`，得到 revision 1 / HTTP 201；更新必须同时命中 current revision 和 current hash，得到精确 `+1` / HTTP 200。

单个数据库事务必须同时完成：

```text
path/body/session/draft/actor/content/skill 闭包校验
+ source bundle 路径、UTF-8 bytes、file hash、entrypoint 和限额校验
+ revision/hash CAS
+ immutable revision history
+ new head
+ 首次 status/body/headers/Location/ETag 的 byte-exact durable receipt
```

GET 只读 canonical head/history，零业务写入。`ETag` 为 `draft:{revision}:{draft_sha256}`。响应不确定时先查 durable receipt 和 GET，不能用新 key 推进下一 revision。

### 3.3 Build、Artifact 与 Certification

`createSkillBuild` 请求携带完整 `source_bundle` 和 `client_draft_revision`，但没有 `session_id`、`draft_id` 或 `draft_sha256`。因此 Game Build 必须：

- 独立验证请求中的完整源码，并由服务端计算 source bundle hash；
- 记录 `client_draft_revision` 作为客户端声明的源码基线，不据此猜测 Draft 身份；
- 不读取“当前 Session”、“最新 Draft”或 Product repository；
- 在响应不确定时根据 Build job/step receipt 对账，不重复编译或 hidden tests。

Build 使用专用 durable job，不为它伪造 `session_id` 或 `turn_id`，也不复用 Agent Turn 的 client sequence 语义。编译必须在 digest-pinned Docker image 中以 C++20 和版本化 flags 运行，并强制 network disabled、read-only rootfs、non-root、cap-drop all、no-new-privileges、PID/CPU/内存/时间/磁盘/输出上限。生产 Build 不允许 mock、host compiler fallback，也不能调用 Runtime Sandbox 中明确未实现的 `compile_and_test`。

成功 Artifact 的发布使用同 filesystem 临时文件、完整写入、server-computed SHA-256、fsync、atomic no-replace rename 和只读权限。同 digest 已存在时必须逐字节/hash 对账，不能覆盖漂移产物；Runtime 执行前仍重新验证 Artifact hash。

Certification 不是独立公共 operation。只有成功编译且全部 public/hidden tests 通过后，Build Worker 才在终态事务中闭合 Artifact、SkillVersion、immutable Certification、`BUILD_CERTIFICATION` Evidence 和 Build resource。Certification 精确绑定 tenant/actor/content、skill/version、source/artifact hash、compiler profile/version/image、test suite/version、requested/approved capabilities、Build ID、Evidence 和 `certified_at`。

失败 Build 只保留可查询的 phases/diagnostics/终态；不生成永久 Artifact、SkillVersion、Certification、Active Registry 或伪造的 `BUILD_CERTIFICATION` Evidence。

### 3.4 Activation

Activation 的持久化 scope 必须闭合：

```text
tenant + actor + content + world + agent_profile
+ skill_id + exact skill_version_id
+ certification_id + artifact_sha256
```

Worker 必须重新读取 exact Build/SkillVersion/Certification/Artifact，排除失败、未认证、已拒绝/撤销或 bytes/hash 漂移。`expected_registry_revision` 是严格 CAS，成功只能是 `previous_revision + 1`。Activation resource、全 scope registry head/entry、Command result 和 job receipt 在同一终态事务中提交。过时 CAS 和同基线并发败者零写入。

### 3.5 Agent Turn 的 exact version 绑定

公共链创建的 Session 在 Activation 前不包含 active Skill。Turn 接受时必须用 Session 的 actor/content/world/profile scope 读取 registry，将请求中的 `skill_id + skill_version_id + artifact_sha256 + certification_id` 与当前 exact entry 闭合，并为该 Session 保留不可变 version history。新 Session 不得退回只有 actor+skill 维度的 legacy registry；legacy 解析只能显式服务已存在的 A6 历史 Session。

## 4. 公共资源与 Evidence 闭包矩阵

| 阶段 | 公共入口/恢复入口 | canonical 事实 | 允许的 Evidence | 失败时禁止新增 |
|---|---|---|---|---|
| Bootstrap | `GET /v1/bootstrap` | published authority + World checkpoint | 无新 Evidence | 任何业务写入 |
| Session | `POST /v1/agent-sessions` → Command → `GET /v1/agent-sessions/{id}` | authority snapshot + Session resource/hash | 无新 Evidence | Session、Draft、Registry、Run 等副作用 |
| Draft | Product `PUT/GET .../skill-drafts/{draft_id}` | immutable revision + head + byte-exact receipt | 无新 Evidence | Draft revision/head 和任何下游资源 |
| Build accepted/running | `POST /v1/skill-builds` → Command / Build GET | request source bundle/hash + step receipts | 尚无 Certification Evidence | 重复 compile/test/publish |
| Build failed | `GET /v1/skill-builds/{id}` | immutable failed phases/diagnostics | 不伪造 `BUILD_CERTIFICATION` Evidence | Artifact、Certification、SkillVersion、Activation、Registry、Evidence |
| Build certified | 同一 Build GET，无独立 Certification route | source + compiler/tests + Artifact + SkillVersion + Certification 相互闭合 | 一份归属该 Build 且具有 exact artifact/version 的 `BUILD_CERTIFICATION` Evidence | 重复 Artifact/Certification/SkillVersion/Evidence |
| Activation | `POST .../activations` → Command → Activation GET | full-scope registry CAS + immutable activation | 不为了“记录失败”伪造 Evidence | Activation/registry revision 和其他业务资源 |
| Turn / Run | 原 Session 下 `POST .../turns` → Command / Run GET | exact active version + invocation receipt + Run | 现有 Sandbox/Run Evidence；成功才有 canonical `WORLD_COMMIT` | 重复 Sandbox、World CAS、Run、Evidence |
| Role / Learner / Product | Product Interaction list/get | final role turn + source receipts + projection outbox | 现有 feedback/inference closure | 重复 Provider、Message、Interaction、Learner revision |

INT1 Backend 已装配 SessionWorkspace Adapter。Session Control 在 Session 成功终态原子创建 starter Draft/Workspace；Draft CAS、Turn 接受与 terminal projection 刷新 Workspace。Draft 返回的 Workspace link 必须解析到这一 canonical Backend resource，任何缺失或错链都 fail closed，不伪造 `200`。

## 5. 重放、响应丢失与接管指纹

每个故障用例都要在操作前后记录 counts + canonical hashes，并说明为什么允许变化的集合恰好等于预期。最少包含：

```text
Session / Draft head+history / Build job+steps / compiler execution count
/ hidden-test execution count / Artifact files+rows / SkillVersion / Certification
/ Activation / registry head+history / Command+job+receipt
/ Run / Evidence / World revision+events / Provider calls / Sandbox calls
/ Message / Interaction / Event / Outbox / Learner revision
```

| 故障点 | 必须使用的恢复权威 | 必须保持不变的副作用 |
|---|---|---|
| HTTP commit-response-loss | 原 idempotency receipt + Command/Location + canonical GET | 不用新 key 创建第二个资源 |
| Draft PUT response-loss | Product write receipt + Draft head/revision history | revision 不二次 `+1` |
| Build Worker 崩溃 | job fencing + deterministic step receipts + Artifact reconciliation | 不重复 compile、hidden tests、publish、certify |
| Artifact publish response-loss | content digest + on-disk bytes/hash + publish receipt | 不覆盖、不发布第二份 |
| Activation response-loss | Command/job + activation resource + full-scope registry revision | registry 不二次推进 |
| Agent Turn / Provider / Sandbox response-loss | 现有 invocation/turn/source receipts | 不重复 Provider、runtime Sandbox、World、Interaction、Learner |
| 全进程重启 | 只重建 composition，从 PostgreSQL 和 Artifact root 恢复 | body/hash/counts 与重启前一致 |

损坏 receipt、snapshot、phase、diagnostic、Certification、registry entry 或 Artifact bytes/hash 必须 fail closed，不能“自动修复”为可信终态。

## 6. Provider-independent 失败矩阵

下列分组都必须在真实 PostgreSQL/Worker 上验收，不依赖 Provider，不 skip：

- Session：unknown World，wrong actor/content，ambiguous Task，unknown Learner/Profile，stale world revision，same key/different body，corrupt receipt，restart/replay。
- Draft：stale revision/hash/组合，path/body mismatch，actor/content/skill 漂移，duplicate/ASCII-fold collision/逃逸 path，entrypoint 缺失或重复，file hash 错误，count/bytes 超限，unknown field，duplicate JSON key，same key/different body，concurrent CAS，response loss，receipt/snapshot 篡改。
- Build：compile error，warning-as-error，hidden-test failure，compile/test timeout，stdout/stderr 超限，Docker unavailable，image digest 错误，source drift，Artifact publish response-loss/bytes drift，Build snapshot/phase/diagnostic/Evidence 损坏，lease takeover，restart/replay。
- Certification：failed Build 伪造，Build/SkillVersion/Artifact mismatch，compiler/test version mismatch，Evidence 错属，duplicate/overwrite，rejected/revoked，corrupt record。
- Activation：uncertified/failed Build，unknown/altered Artifact，cross tenant/actor/content/world/profile，stale/concurrent registry CAS，wrong Certification ID，Artifact mismatch，same key/different body，response loss，restart/replay。

每个失败用例都必须用指纹证明没有不应发生的 Draft revision、Build execution、Artifact、Certification、SkillVersion、Activation、Registry revision、Session、Run、Evidence、World CAS、Interaction、Message、Event、Outbox 或 Learner revision。

## 7. 公共链 live E2E 和门禁报告

live E2E 必须从 localhost Game/Product HTTP 出发，只使用允许的集中初始权威；不得用 application/repository/私有 Worker 调用或 SQL INSERT 代替 Session、Draft、Build、Certification、Activation、Registry、Run 和投影的公共创建。v1 的三次真实失败必须依次产生 teaching/teaching/Bug，v2 真实成功必须产生精确 `+1` World commit 和书书，并且两类最终角色均为真实 OpenAI-compatible Provider 的 `source=provider` / `degraded=false`。

发布报告必须对每组命令记录测试数、耗时和结果，并至少包含：contract manifest、port surface、TypeScript/Node、Ruff check/format、Pyright、compileall、全部 Python、migration 幂等/正反向、Draft/Session/Build/Artifact/Certification/Activation 专项、PostgreSQL CAS、localhost transport、response-loss/restart/takeover、A6 Bug/书书、Product Interaction、Learner Projection、Godot、隔离 wheel、真实 Provider public-chain live E2E 和 `npm run verify`。

未填写、未运行、被 skip、缺 PostgreSQL/Docker/compiler image/Godot/Provider、使用 fake/scripted Provider 或 host compiler fallback，一律等于门禁未通过。报告不得记录 API key。

## 8. 明确未开放能力

- SkillPatch 和 PatchDecision；
- `allow_skill_patch=true`、`patch_eligible=true` 或 `full_solution_eligible=true`；
- hint level 4；
- World WSS 与 Client Event Batch 新纵切（INT1 只使用 HTTP Events/Snapshot）；
- 空账号注册与 Task/World/Profile 公共创作平台；
- 飞书、教师助手和通用部署平台。

Product ContentUnit/SessionWorkspace 与正式 Godot AppRoot 已在 INT1 装配；它们仍须随真实 Provider 跨进程链验收。下一阶段才是结构化 SkillPatch/PatchDecision、WSS、Feishu 或另一个经明确授权的目标。
