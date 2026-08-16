# Agent Runtime 实现说明

> INT1 ownership（2026-08-12）：本 Runtime 是 provider-neutral library，不是公开后端。唯一 Gateway、PostgreSQL/Alembic、durable LLM/skill receipts、combined Control/Build/Turn worker 和 terminal projection 均由 sibling `walnut-world-backend` 装配。Backend 的 strict Turn 路径只从 durable Provider/Sandbox receipts 恢复；production Provider 必须是 capability-verified recoverable relay，不接受普通 direct chat adapter。Provider 终局缺失时保留已提交 Run/World 客观事实并 fail loud，不发布 `provider_fallback` Interaction、不推进 Learner。真实 Provider 三仓 E2E 当前仍 NOT PASS。

## 交付范围

`python/yaya_agent_runtime` 是建立在冻结 Wire Contract 和 `yaya_agent_contracts` Port 之上的内部应用层。内部 `GameEvent`、`TurnContext`、`AgentDecision` 不是公开 DTO；入口和出站适配器必须继续使用 `contracts/manifest.json`、OpenAPI、AsyncAPI 和 JSON Schema 定义的对象。

Runtime 不导入 ORM、模型厂商 SDK 或具体 Sandbox/World 实现。所有外部能力都通过异步 Port 注入；模型不能直接写世界、激活 Skill、更新 Learner 或应用 Patch。

A8 前置门禁在 Runtime 之前增加学生源码公共生产链，但不把 Draft/Build/Activation 职责塞进本 package：

```text
published Task/Content + initial World/Learner/Profile authority
→ Bootstrap
→ canonical AgentSession identity
→ Product SkillDraft revision/hash CAS
→ Game Build with the request's complete source_bundle
→ pinned Docker C++20 + versioned tests
→ successful Build terminal: Artifact + SkillVersion + immutable Certification + Evidence
→ full-scope Activation registry CAS
→ exact SkillVersion Agent Turn in the original Session
→ Agent Runtime / Run / World / role / Learner / Product-read chain
```

Product SkillDraft URL 需要 `session_id`，因此首次流程必须先创建 Session 身份。Session 本身不需要 Active Skill。Build 请求不带 `session_id/draft_id/draft_sha256`，所以 Build Worker 只信任请求中的完整 source bundle，不读“最新 Draft”。Certification 是成功 Build 终态，不存在独立公共 Certification operation。该纵切的准确范围是“已发布内容与初始世界权威后的学生源码公共生产链”，不是空账号旅程。完整权威和 Evidence 矩阵见 [A8 学生源码公共生产链](A8_PUBLIC_STUDENT_SKILL_CHAIN.md)。

```text
accepted root GameEvent
       |
       v
durable Worker claim / lease / fencing
       |
       v
internal execution receipt -> pinned Docker C++ -> canonical Run / Evidence / World CAS
       |
       v
derive run_failed / task_completed + authoritative consecutive failure count
       |
       v
RoleRouter ----> teaching / bug / book / explicit NO_AGENT_ACTION
       |
       v
ContextBuilder -- exact IDs / pinned content / minimal role data
       |
       v
SharedAgentRuntime -- closed model schema -- real Provider
       |
       v
validated AgentDecision
       |
       v
AgentTurnCommitPort (one public interaction/message/event/outbox transaction)
```

## 确定性路由

| 事件 | 结果 |
|---|---|
| `task_started` | `world_agent` |
| `run_skill_requested` | `xiaohutao` |
| `compile_failed` | `teaching_agent` |
| `run_failed`, `hint_requested`，同类连续失败少于 3 次 | `teaching_agent` |
| `run_failed`, `hint_requested`，同类连续失败至少 3 次 | `bug_agent` |
| `task_completed` | `book_agent` |
| `compile_succeeded`, `run_succeeded`, `skill_patch_confirmed` | 显式 `NO_AGENT_ACTION` |

未知事件不会默认变成教学消息。事件构造器、Router 与 Role 配置三层都拒绝未知值。

`compile_failed` 路由只适用于已具有 exact build/session 和可闭合 Evidence 的内部事件。A8 前置公共 Build 请求没有 Session 身份，且冻结 `BUILD_CERTIFICATION` Evidence 需要 artifact/version；因此失败 Build 只产生 canonical Build phases/diagnostics，不为了触发该路由伪造 Evidence、AgentInteraction 或 Learner 更新。

### A6 正式生产派生

正式 Game HTTP 仍只接受冻结的 Agent Turn operation。Worker 在同一 durable Command/Job、同一 command/turn 身份下先取得幂等 Skill invocation receipt；receipt 不存在时才运行 pinned Docker C++ Sandbox，并由 World 规则生成 canonical Run/Evidence，成功路径才执行一次 World CAS。Worker 不接受程序或模型自报 `task_success`、`failure_key` 或失败次数。

Run 成为权威后，Worker 在内部派生最终角色事件：相同 tenant/actor/content/session/task/world/Skill 下，同一 `failure_key` 的精确连续失败次数为 1、2 时路由到 `teaching_agent`，第 3 次路由到 `bug_agent`；成功 Run 派生 `task_completed` 并路由到 `book_agent`。每个 command/turn 先持久化一个只承载 invocation/Run 回执、不发布任何公开投影的内部 `xiaohutao` Turn，再提交一个最终角色 Turn；只有最终 Turn 产生一份公开 AgentTurn、Message 和 Product AgentInteraction。

通过历史 A6 验收的 Bug／书书结果必须来自真实 OpenAI-compatible Provider，且 `source=provider`、`degraded=false`。当前 INT1 production 还要求 recoverable relay 以稳定 `dispatch_id` 提供 capability GET、原子同 ID PUT 和线性一致 GET；数据库 result receipt 与 relay completion/raw bytes hash 联合验证，`PENDING` 遵守 `Retry-After`。重放、receipt 响应丢失、Worker 接管或全进程重启都先读取原 durable receipt/turn，不重复调用 Provider、运行 Sandbox、推进 World 或写第二份 Interaction。

## 身份与事实约束

- `OperationContext.command_id` 和认证 actor 必须与事件一致。
- Context 读取使用精确 `session_id`、`turn_id`、`command_id`、`run_id`、`build_id`、Skill 版本和 Evidence；没有 “latest” 查询。
- World snapshot 必须匹配 session world、`expected_world_revision`、actor 与固定 content version。
- Run 必须闭合 session/turn/command/world/Skill，并包含事件引用的不可变 Evidence。
- Task、Session、Skill、Compile、Run、Learner、Message、反例和历史快照都携带 actor/content provenance；Skill 历史还必须闭合到当前 session。
- 对公共链创建的 Session，Turn 只能消费当前 tenant/actor/content/world/profile 全 scope registry 中的 exact `skill_version_id + artifact_sha256 + certification_id`；新 Session 不得退回 actor+skill 的 legacy 激活解析。
- 成功 Run 必须携带精确 `previous_revision + 1` 的 typed `WorldCommitReceipt`，并让 WORLD_COMMIT Evidence 的 SHA-256 等于规范 payload 的 `YAYA_CANONICAL_JSON_V1` 哈希。
- Bug Agent 只接受至少三条相同 `failure_key` 的真实连续失败 Run；历史必须同 tenant/actor/content/session/world/Skill，Run/Turn/Command 不得重复，声明的 `failure_count` 必须等于数据库权威连续后缀。
- Book Agent 只接受带 canonical World commit 的成功 Run，并读取同 Session 的真实失败/成功 Run、canonical Skill 版本历史和当前 Learner Profile。
- xiaohutao 只有在 `invoke_skill` 实际返回身份闭合的 Run/Evidence 后，才能生成成功反馈。

## 模型与工具协议

冻结的 `LlmRequest` 没有厂商专属 `tools` 字段，因此 Runtime 把工具定义放入结构化提示，并要求模型输出闭合的 `decision | tool_calls` 判别联合。工具输入对象必须显式 `additionalProperties: false`。

每个 turn 最多一个工具轮次；角色配置限制总调用数，`invoke_skill` 最多一次。Registry 会先对整批调用做角色白名单和闭合输入 Schema 预检，再先执行只读工具、最后执行唯一副作用工具。副作用幂等身份由 command/turn/tool 稳定派生，不依赖模型输出顺序。实际工具记录由 Runtime 创建，模型不能自报或伪造执行日志。

所有对外事实文案由 Runtime 根据可信 Task、Compile、Run、World receipt 和 session 历史重新渲染；模型文本不能覆盖任务状态、运行成败、失败次数或永久掌握结论。Trace 写入失败会成为 `runtime_warnings`，不能阻断工具或掩盖已经提交的世界事实。Run 摘要、工具输出和 Evidence 集合都有构造期字节/条数上限，副作用前会预留完整 Evidence 容量。

模型提示只使用本轮生成的 Evidence/Skill 别名，不发送 canonical student、tenant、session、turn、command 或 Skill ID。Learner 推断必须显式引用已验证的 Evidence 别名，Runtime 再解析回真实 Evidence ID。

通用 Runtime 对可修复的结构错误只允许一次带具体错误码的修复请求；合同仍能显式表达：

- `source = provider_fallback`
- `degraded = true`
- `fallback_reason = <机器错误码>`

降级不会回滚此前已经完成的 Run、World commit 或 Evidence。A6 Bug／书书链更严格：Provider timeout/unavailable、返回 degraded，或一次修复后仍有错误 role、phase smuggling、错误 response type、伪造 Evidence、Patch/full-solution 越权或永久掌握断言时，只按 canonical Run 事实终结 Command，在公开 commit 前 fail closed；不发布 provider_fallback Interaction，也不新增 Message、feedback Event、projection Outbox 或 Learner revision。

## 幂等、租约与未知提交状态

`AgentHub` 在模型或工具前按 tenant + immutable event 原子取得有期限的 claim。进入 xiaohutao Runtime 前，Hub 以覆盖最坏模型/工具超时的预算续租；claim token 是 fencing token，过期接管后旧 worker 不能提交或释放新 claim。已知发生在 Runtime 前的 Context 失败会 CAS abandon；Runtime 或 commit 的不确定失败保留到租约过期。

`SkillInvocationPort` 必须把 World、Run、Evidence 与 `SkillInvocationResult` 按稳定 invocation ID 原子发布，并提供 `get_result` 对账。Hub 在执行前、降级提交前以及 worker 接管后查询该 receipt：响应丢失或世界已经推进时，不再读取旧 revision 的 World，也不再次调用模型/副作用，而是生成 `SIDE_EFFECT_RECEIPT_RECOVERED` 的确定性反馈。invoke 已发出但 receipt 暂不可见时会做短时有界轮询；仍不可见则抛 `UNKNOWN_COMMIT_STATE`，绝不把临时 fallback 持久化为终局。

## 自动化验证

统一门禁：

```powershell
cd agent
npm ci --ignore-scripts
python -m pip install -r requirements-test.txt
$env:YAYA_PYTHON_EXE = (Resolve-Path .\.venv\Scripts\python.exe)
npm run verify
```

Runtime 专项测试：

```powershell
python -m unittest discover -s tests -p "test_agent_runtime*.py" -v
```

专项用例覆盖穷尽路由、严格 Role 配置、Context 最小取数和错链拒绝、Tool 授权与异常转换、一次修复、二次失败降级、A6 角色越界零公开写入、禁止第二工具轮次、Hub 幂等重放、租约/fencing、响应丢失与延迟 receipt 对账、假成功拒绝、副作用前全批预检、World receipt/Evidence 完整性，以及内存、宿主攻击证明和生产 Docker C++ 浇水任务。PostgreSQL/Worker 矩阵另验证 count/key、跨 scope 历史、重复身份、成功/失败错配和 Evidence 归属在 Provider 前 fail closed。

内存任务不是预置成功结果：测试 adapter 在受限执行环境中运行绑定的 Skill 源码，按 `length=8` 循环修改其 canonical World，计算 8/8 后生成 Run/Evidence；测试随后重新读取 World，并核对 revision、sequence、hash、Evidence、非降级决策及 Hub 无二次模型/工具调用的原样重放。

原生门禁通过 MSVC Build Tools 现场以 C++20、`/W4 /WX` 编译浇水程序，并真实执行 7/8 失败边界、无限循环超时和 8/8 成功路径。这个原生 adapter 不是生产 Build fallback；A8 Build 只能使用 digest-pinned、networkless Docker compiler image 和版本化 public/hidden tests。学生程序只能输出闭合的逐动作 intent；测试 World 独立校验动作、应用到临时状态并重算完成度，不接受 `task_success` 自报。A8 前置 live E2E 必须先通过公共 HTTP 创建 Session/Draft/两版 Build/Certification/Activation，再在 production composition 中执行三次同类失败和一次成功，核对 teaching/teaching/bug、book、World revision、sequence、状态 hash、规范 Evidence payload、真实 Provider 非降级结果以及 Product HTTP 恢复。stdout/stderr 有固定上限，重复 JSON key、非法动作、认证 artifact 哈希不匹配和构造后替换都会失败。找不到编译器、Docker、PostgreSQL 或 Provider 配置时门禁失败，不会 skip。

宿主原生 adapter 只用于测试资源上限、进程树清理以及证明其文件系统/网络隔离不足，不是生产安全边界。production composition 只实例化固定摘要的 Docker Sandbox：从只读 content-addressed artifact store 解析执行文件，并强制无网络、只读根文件系统、非 root、cap-drop、no-new-privileges、PID/CPU/内存/时间/输出限制和容器清理。

## 历史 Agent composition 与当前生产宿主

下列 `yaya_agent_backend` 项目是 Agent 单仓 A6/A8 的历史兼容/回归适配。INT1 生产宿主是 `walnut-world-backend`；不得启动前者作为第二 Gateway、运行其私有 migration，或让后者读取 `yaya_*` 表。当前 Backend 通过稳定 contracts/Runtime/Build/Sandbox 边界复用能力，并在自己的表/UoW 中实现 receipt、fencing 与投影。

- PostgreSQL `Task`、`Session`、`Skill`、`Run`、`Learner`、`Message`、`Counterexample`、`World`、Trace 与 AgentTurn 窄适配器。
- `SkillInvocationPort` 的真实 Docker Sandbox、World UoW、Event、Outbox、Run、Evidence 和幂等 receipt 原子应用服务。
- `AgentTurnCommitPort` 的 committed turn、feedback Event、Message、Product AgentInteraction 与 projection Outbox 原子事务。
- 冻结 OpenAPI 的 202/查询 HTTP、JWT 身份闭合、持久化 Worker、claim/renew/fencing、重启恢复和 `UNKNOWN_COMMIT_STATE` Location 对账。
- Worker 内部的 invocation receipt → canonical Run 派生 → teaching/bug/book 最终角色提交；不增加公开 route 或每 turn 的第二份 Interaction。
- OpenAI-compatible provider 配置、边界超时/响应上限、严格结构输出以及显式 fallback。

## Runtime 边界与本阶段明确不包含

- Product Draft CAS、Build compiler/publisher、Certification 和 Activation 属于 `yaya_agent_backend` 公共生产链，不属于 `yaya_agent_runtime` 的模型/工具职责。本文仅要求 Runtime 在 Turn 时消费已经闭合的 exact active version。该 A8 前置纵切在全部门禁和真实 Provider public-chain live E2E 通过前不得声称完成。
- 初始 Task/Content、World、Learner/actor、Agent Profile 和 Build policy 仍由集中平台权威设置；Session、Draft、Build、Artifact、SkillVersion、Certification、Activation、Registry 不得再作为新 public-chain E2E 的 SQL 预置。
- 结构化 Skill Patch/PatchDecision 的 CAS 接受/拒绝与 Godot confirmation；Teaching 配置保持 `allow_skill_patch=false`、`patch_eligible=false`、`full_solution_eligible=false`。
- 飞书助手与飞书消息投递。
- Godot/前端 UI 与 World WSS。
