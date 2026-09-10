# 核桃代码世界：前后端接口联调文档

更新：2026-09-10。适用当前单题 Demo、Walnut 主后端与复用的 Agent 库。本文是完整接入入口，包含正式主关、主动教学与语音，以及新增“主关正确→每局一次Bug挑战→书书总结和音频”。前端只连接主后端，不连接独立 Agent 服务。

## 文档入口

| 文档 | 用途 |
| --- | --- |
| 本文 | 八个功能模块的调用顺序、字段来源、完成条件与界面处理 |
| [HTTP 接口参考](HTTP接口参考.md) | 28 个已实现 HTTP 操作的参数、必填字段、枚举、响应与 JSON 示例 |
| [问叮当实时语音](实时语音接口.md) | 点击开启／关闭、音频格式、上下文更新、打断及全部公开消息 |
| [Bug军团与书书完整接口](Bug军团与书书接口.md) | 5个练习动作、旧Book音频、完整字段、错误码、恢复与界面验收 |
| [Bug军团接入顺序](../bug-practice-frontend-handoff.md) | start→prepare→answer→summary 的完整请求/响应示例 |
| [Bug军团实现逻辑](../architecture/bug-practice.md) | 状态机、错题读取、模型出题、真实C++判题、幂等与语音 |
| [补充接口](补充接口.md) | 教师学习查询、MCP、可选世界 WebSocket 和客户端事件 |
| [examples](examples) | 完整请求／响应 JSON；均为示例数据，使用前替换为当前资源值 |

字段参考由实际 FastAPI 路由和 `agent/contracts` 生成。表格展开常用嵌套字段，更深层定义及条件必填可直接打开各接口的 Schema 链接。JSON 示例通过 Schema 校验，但不代表这些示例 ID 已存在于本机数据库，也不是一次可直接顺序执行的会话。

`HTTP接口参考.md` 的28个操作是原有合同接口。新增练习与Book音频属于Demo扩展，在《Bug军团与书书完整接口》中维护；它们返回最终HTTP200结果，不使用主关202 Command协议。旧前端仍可运行原流程，新前端必须按下文消费新挑战和新总结。

## 0. 所有模块共用的约定

### 地址、身份与请求头

持久 Demo 启动脚本使用 `http://127.0.0.1:8790`。实际主机和端口由联调环境提供，前端统一配置 `API_BASE_URL`。不要连接私有模型 relay、Worker 或数据库。

普通 HTTP 接口均携带：

```http
Authorization: Bearer <游戏登录token>
X-Request-Id: req_<本次请求UUID>
X-Trace-Id: trace_<本次请求UUID>
X-Correlation-Id: corr_<本次请求UUID>
X-Schema-Version: 1.0.0
```

主关合同中的JSON写请求再携带（练习接口的答题ID约定见扩展文档）：

```http
Content-Type: application/json
Idempotency-Key: <本次业务操作固定的唯一键>
```

- 三个请求 ID 的前缀分别为 `req_`、`trace_`、`corr_`，后缀 8–96 个字母、数字、下划线或连字符。UUID 去掉连字符后可直接作为后缀。
- 同一次保存／提交因网络原因重试：保留幂等键、业务 ID 和原始 body 字节；重新生成请求头中的三个 attempt ID。body 改动后是新操作，不能复用旧幂等键。
- 普通成功响应直接是资源 JSON，没有统一的 `data` 外壳。错误响应有独立格式，见下文。
- 资源里的 `request_context` 是最初创建该资源的上下文，不一定等于本次 GET 的请求头。响应头用于排查当前请求。
- 游戏 token 由现有登录／Demo 启动流程提供。当前没有给学生前端提供 `POST /login` 或 `/register`；不要使用豆包或 DS 的密钥充当游戏 token。
- 仅显式开发鉴权模式接受 `tenant:actor` 测试 token；正常 Demo 使用已配置的 JWT。
- 语音 WebSocket 使用首帧 token，见语音文档。教师 MCP 的请求头有例外，见补充文档。
- 当前主要客户端是 Godot。若使用独立域名的浏览器网页，需由联调环境安排同源代理或跨域支持；当前应用没有统一配置 CORS 中间件。

### 异步操作与三个不同的“完成”

创建会话、提交编译、激活技能和创建 Turn 通常返回 `202`：

```json
{
  "job_id": "job_demo_00000001",
  "job_type": "EXECUTE_AGENT_TURN",
  "status": "ACCEPTED",
  "created_at": "2026-09-09T10:00:00Z",
  "updated_at": "2026-09-09T10:00:00Z",
  "command_id": "cmd_demo_00000001",
  "trace_id": "trace_demo_00000001",
  "error": null
}
```

响应带 `Location: /v1/commands/{command_id}`、`Retry-After: 1`、`Idempotency-Replayed`。接下来轮询 `Location`，不要猜测 `job_id` 的查询地址。

| 观察对象 | 表示什么 | 前端处理 |
| --- | --- | --- |
| POST 返回 202 | 操作已接收 | 显示处理中，保留 command_id，继续读取 |
| Run 已终态 | 技能运行结果已经产生 | 可以先显示成功／失败和世界结果 |
| AgentInteraction 已出现 | 教学反馈／提问回复／成长总结已发布 | 展示文字、提示等级或补丁建议 |

**Run 完成、Command 完成和成长总结完成不是同一时刻。** Command 已提供 `links.run` 时，可以读取该 Run。成功结果已经提交就先显示；`agent_feedback=null` 表示反馈还没有生成，不要因此把成功运行显示成失败。

`GET /v1/commands/{command_id}` 的字段见 [getCommand](HTTP接口参考.md#getcommand)：

| 字段 | 处理方式 |
| --- | --- |
| `terminal` | 判断该 Command 是否结束，不要靠轮询次数判断 |
| `status` | `ACCEPTED`、`VALIDATING`、`RUNNING_SANDBOX`、`APPLYING_WORLD` 为处理中状态；其他状态结合 `terminal` 和 `error` 处理 |
| `result.result_type` | 区分 `RESOURCE_CREATED`、`WORLD_COMMIT`、`NO_EFFECT`、`CLIENT_EVENTS_ACCEPTED` |
| `result.resource_url` | `RESOURCE_CREATED` 时读取新建的 Session／Build／Activation |
| `links.run` | 后端已经给出 Run 链接时读取，不根据 command_id 自行拼 run_id |
| `error` | 基础设施或执行失败原因；与学生代码编译／运行结果分开呈现 |

网络断开后保留 command_id，通过 GET 恢复。`UNKNOWN_COMMIT_STATE` 时按返回的 command_id／Location 查询已有操作，不重复提交新业务。文字提问可能只有 `NO_EFFECT` 及 Interaction，没有新 Run。

轮询策略建议：同一个 Command 只保留一个轮询任务，响应未到时不叠加请求；参考 `Retry-After`，断线后允许恢复。当前完整提交仍约几十秒，短暂没有反馈不能当作调用失败。

### 版本号、序号和哈希的来源

| 值 | 从哪里取得 | 用在哪里 |
| --- | --- | --- |
| `session_id` | Bootstrap 的 `session.current_session_id`，或创建会话后的资源 | 所有会话内接口 |
| `content_ref`／`content` | Bootstrap／Workspace | 读取关卡、保存草稿等；字段名按各接口原样填写 |
| `draft_id`、`skill_id` | Workspace 的 `skill_draft_refs`／Draft | 保存草稿、编译、补丁 |
| `base_revision`、`base_draft_sha256` | 最新 Draft 的 `revision`、`draft_sha256` | 保存草稿的版本比对 |
| `expected_registry_revision` | Bootstrap 的 `activation.registry_revision` | 激活新版本 |
| 技能四元组 | 当前激活结果或 Bootstrap 的 `activation.active` | `skill_id`、`skill_version_id`、`artifact_sha256`、`certification_id` 一起提交 |
| `expected_world_revision` | 最新 Snapshot 的 `revision` | 创建 Turn |
| `client_state.last_event_sequence` | 已同步世界的事件游标 | 创建 Turn；不能填聊天列表 sequence |
| `client_state.client_turn_sequence` | 最新 Session 的 `last_turn_sequence + 1` | 每次新 Turn，包括文字提问和补丁请求 |
| 聊天列表游标 | Interaction page 的 `next_after_sequence` | 增量读取 AgentInteraction |

这些游标不能互换。异步提交后立即提问时，先同步当前 Session／世界状态，避免继续使用提交前的 Turn 序号。

### 错误格式与处理

普通 HTTP 失败响应示例：

```json
{
  "request_id": "req_demo_00000001",
  "trace_id": "trace_demo_00000001",
  "status": "REJECTED",
  "data": null,
  "error": {
    "code": "INVALID_REQUEST",
    "category": "VALIDATION",
    "retryable": false,
    "user_message_key": "request.invalid",
    "stage": "VALIDATE"
  }
}
```

| 情况 | 前端处理 |
| --- | --- |
| 400 `INVALID_REQUEST` | 检查必填字段、路径与 body 的 ID、序号及请求头；不要无限原样重试 |
| 401 `AUTHENTICATION_REQUIRED` | 更新游戏登录凭据 |
| 403 `AUTHORIZATION_DENIED` | 检查当前身份是否有权访问该功能 |
| 404 `NOT_FOUND` | 检查资源 ID、归属和能力开关；不要把不存在的资源当作空成功 |
| 409 版本／游标冲突 | 重新读取对应 Session、Draft、Bootstrap 或 Snapshot，再决定是否发起新操作 |
| `IDEMPOTENCY_KEY_REUSED` | 同一个幂等键配了不同请求；恢复原请求或使用新业务键 |
| 422 技能／规则拒绝 | 按 `error.code` 和当前 Build／Run 结果显示，不笼统提示网络错误 |
| 429 或 503 | 结合 `error.retryable` 和 `Retry-After` 等待／恢复 |
| 500 `INVARIANT_VIOLATION`／`INTERNAL_ERROR` | 保留 command_id 与 request_id／trace_id 给后端定位；不要覆盖已确认的世界结果 |

HTTP 错误体一般不包含内部详细诊断文本；界面使用 `user_message_key` 映射提示。完整学生运行错误从 Run／Evidence 读取，不能期望所有错误都位于 HTTP `error.message`。

## 1. 学生与会话初始化

| 接口 | 目的 |
| --- | --- |
| [GET /v1/student-bootstrap](HTTP接口参考.md#getstudentbootstrap) | 获取当前学生、关卡、会话、编译配置、激活版本和世界定位 |
| [POST /v1/agent-sessions](HTTP接口参考.md#createagentsession) | 当前无会话时创建 |
| [GET /v1/agent-sessions/{session_id}](HTTP接口参考.md#getagentsession) | 获取会话状态与下一轮序号来源 |
| [GET /product-experience/v1/sessions/{session_id}/workspace](HTTP接口参考.md#getproductsessionworkspace) | 恢复任务、草稿引用、世界检查点和交互游标 |

调用顺序：

1. 读取 Student Bootstrap。
2. `session.current_session_id` 非空时直接恢复；为空时将 `session.create_request` 原样提交到创建接口。
3. 创建返回 202 后轮询 Command，通过 `result.resource_url` 取得 Session。
4. 读取 Workspace；根据其中的 `links.content_unit`、`skill_draft_refs`、`links.world_snapshot` 和 `links.agent_interactions` 加载页面。
5. 界面完成恢复后才允许保存和提交。

Workspace 的 `current_task.status` 为 `NOT_STARTED`／`IN_PROGRESS`／`COMPLETED`／`ABANDONED`。它属于工作区投影，异步处理中可能落后于最新成功 Run；运行结果显示按模块 4 处理。

完整示例：[Bootstrap](examples/game-student-bootstrap-v2.json)、[创建请求](examples/game-agent-session-create-request.json)、[Workspace](examples/product-experience-session-workspace.json)。

`GET /v1/bootstrap` 是兼容读取；新页面以 Student Bootstrap 为初始化入口。当前没有给学生前端提供关闭、删除或重新分配会话的写接口。

## 2. 关卡内容与代码草稿

| 接口 | 目的 |
| --- | --- |
| [GET /product-experience/v1/content-units/{unit_id}/versions/{content_version}?content_hash=...](HTTP接口参考.md#getproductcontentunit) | 获取指定版本的关卡任务 |
| [GET /product-experience/v1/sessions/{session_id}/skill-drafts/{draft_id}](HTTP接口参考.md#getproductskilldraft) | 读取草稿 |
| [PUT 同一草稿地址](HTTP接口参考.md#upsertproductskilldraft) | 保存草稿，200 更新或 201 新建 |

关卡响应的 `task` 对象内包含 `name`、`goal`、`instructions`、`knowledge_points`、`starter_skill`、`hint_policy`、`story`。`content_hash` 为必需查询参数，建议直接使用 Workspace 返回的完整链接。

保存请求包含：`session_id`、`draft_id`、`skill_id`、`content_ref`、`base_revision`、`base_draft_sha256`、`display_name`、`source_bundle`、`client_saved_at`。字段与示例见 [保存草稿](examples/product-experience-skill-draft-upsert-request.json)。

`source_bundle` 是完整源文件集合：`language="CPP20"`、`entrypoint`、`files[]`；每个文件有 `path`、`content`、`content_sha256`。文件哈希为实际发送的 content 的 UTF-8 字节 SHA-256，小写十六进制。保留换行，不要计算完哈希再修改内容。草稿整体 `draft_sha256` 由后端生成，前端保存并回传。

常规 Demo 已由会话创建流程生成初始草稿；后续保存读取最新 revision/hash。首次新建时才使用 `base_revision=0`、`base_draft_sha256=null`；请求还需符合会话、技能和关卡绑定。

保存成功后更新本地 `revision`、`draft_sha256`，再允许基于新版本继续保存。冲突时保留编辑器未保存文本，重新 GET 后处理，不要静默用旧版本覆盖。

后端保存草稿版本，但当前没有公开的“草稿历史列表／回滚版本”接口。不要调用不存在的 `/revisions`。单纯保存草稿也不会自动编译或运行。

## 3. 代码编译与技能激活

| 接口 | 目的 |
| --- | --- |
| [POST /v1/skill-builds](HTTP接口参考.md#createskillbuild) | 提交代码编译 |
| [GET /v1/skill-builds/{build_id}](HTTP接口参考.md#getskillbuild) | 读取编译结果 |
| [POST /v1/skill-versions/{skill_version_id}/activations](HTTP接口参考.md#activateskillversion) | 激活编译出的版本 |
| [GET /v1/skill-activations/{activation_id}](HTTP接口参考.md#getskillactivation) | 读取激活结果 |

编译请求：[完整 JSON](examples/game-skill-build-create-request.json)。

| 请求字段 | 来源 |
| --- | --- |
| `skill_id`、`display_name`、`source_bundle` | 刚保存成功的 Draft |
| `client_draft_revision` | 刚保存成功的 Draft.revision |
| `compiler_profile`、`test_suite_version` | Bootstrap.build |
| `requested_capabilities` | 当前任务需要且 Bootstrap.build.allowed_capabilities 允许的能力 |

流程：保存 Draft → 提交 Build → 轮询 Command → 读取 Build。`status=CERTIFIED` 且 `terminal=true` 才继续激活。`REJECTED` 是学生源代码／测试未通过，`FAILED` 是构建流程失败；具体看 `failure`、`phases[]` 和 `evidence_refs[]`。

Build 公开响应含 `phases[].diagnostic_codes`、`failure.details` 等现有字段。具体编译诊断文本已经在后端保存并提供给问叮当上下文；当前公共 Build／拒绝 Evidence **没有独立的完整 compiler stderr 文本字段**。前端不要假定存在 `diagnostic_messages`、行号列表或 `compiler_output`，也不要把内部回执字段直接当作公共 DTO。

编译拒绝时可根据 `evidence_refs` 读取 `getEvidence`；它返回 `payload.evidence_kind="BUILD_REJECTION"` 的专用结构。编译失败不一定产生 Run，因此不要继续等待 Run 或把这次编译失败绑定到上次 Run。

激活请求：[完整 JSON](examples/game-skill-activation-request.json)。必填：

```json
{
  "expected_registry_revision": 7,
  "activation_scope": {
    "world_id": "world_demo_001",
    "agent_profile_id": "profile_sprout_001"
  }
}
```

数字和 ID 取当前 Bootstrap 的 `activation`，路径 skill_version_id 取本次 Build。202 后轮询 Command、读取 Activation；更新完整技能四元组与 registry_revision。只有激活成功，才提交执行本版本的 Turn。

## 4. 技能执行与游戏世界

| 接口 | 目的 |
| --- | --- |
| [POST /v1/agent-sessions/{session_id}/turns](HTTP接口参考.md#createagentturn) | 提交正式运行 |
| [GET /v1/runs/{run_id}](HTTP接口参考.md#getrun) | 查询技能和世界应用结果 |
| [GET /v1/evidence/{evidence_id}](HTTP接口参考.md#getevidence) | 查询运行证据或编译拒绝证据 |
| [GET /v1/worlds/{world_id}/snapshot](HTTP接口参考.md#getworldsnapshot) | 恢复权威世界状态 |
| [GET /v1/worlds/{world_id}/events](HTTP接口参考.md#listworldevents) | 增量读取已提交世界事件 |
| [GET /v1/worlds/{world_id}/presentation-events](HTTP接口参考.md#listworldpresentationevents) | 获取动画事件，需开关启用 |

Turn 请求的公共必填字段为：`turn_id`、`expected_world_revision`、`input`、`skill_bindings`、`client_state`。完整运行请求见 [JSON](examples/game-agent-turn-create-request.json)。

- `turn_id`：前端为本次新操作生成，重试同一操作保留。
- `input`：可为 `{"type":"ASSIGNED_TASK","task_id":"..."}`，或包含 text/locale 的 `MESSAGE`；这两种带技能绑定时都表示正式执行。
- `skill_bindings`：当前实现要求恰好一个已激活技能四元组，不能只发 skill_id，不能沿用上一轮代码的版本。
- `client_state.client_turn_sequence` 必须是当前 Session.last_turn_sequence + 1。
- `expected_world_revision` 和 `client_state.last_event_sequence` 对应已同步的当前世界状态。

Run 的关键字段：

| 字段 | 含义／使用方式 |
| --- | --- |
| `status`、`terminal` | 技能运行阶段和是否终态 |
| `sandbox.status`、`sandbox.failure` | 沙箱执行事实和具体失败 |
| `sandbox.action_intents` | 执行提出的游戏动作；不是已生效世界状态 |
| `world_application.status` | `COMMITTED` 才表示世界变更已提交 |
| `world_application.receipt` | 世界版本、事件序号和状态 hash 的提交结果 |
| `agent_feedback` | 可能暂为 null；生成后展示 `message` |
| `evidence_refs` | 证据索引，按 evidence_id 查询完整内容 |

显示成功时同时确认 `Run.status=SUCCEEDED` 与 `world_application.status=COMMITTED`。界面更新世界以 Snapshot／已提交事件为准，不拿模型文字或 action_intents 自行判定过关。

评分和结果证据由执行／世界规则产生，按实际 Evidence.payload 及世界字段消费；当前没有独立 `/score` 或由前端上传“最终分数”的接口。

世界 events 与 presentation-events 均要求 `after_sequence>=0`；limit 默认 100、范围 1–500。两种流的序号分别保存，不互相代替，也不与聊天列表游标混用。断线后先按游标补齐，必要时以 Snapshot 恢复；不要因重放事件再发起一次游戏执行。

## 5. Agent 调度、错误计数与反馈

| 接口 | 目的 |
| --- | --- |
| [GET /product-experience/v1/sessions/{session_id}/agent-interactions](HTTP接口参考.md#listproductagentinteractions) | 读取自动反馈、文字聊天及成长总结 |
| [GET /product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}](HTTP接口参考.md#getproductagentinteraction) | 获取单条最新内容与补丁决定 |

列表参数：`after_sequence` 默认 0；`limit` 默认 50、范围 1–100。

读取后消费 `interactions[]`，保存 `next_after_sequence`。`has_more=true` 时继续读下一页。只在页面成功处理后推进游标，按 interaction_id 去重。旧交互的补丁决定更新不会天然成为新的列表 sequence；决定提交后使用单条 GET 刷新该交互。

| Interaction 字段 | 前端用途 |
| --- | --- |
| `interaction_id`、`sequence`、`interaction_revision` | 身份、列表顺序和单条修订 |
| `role` | 内部角色标识，按产品人物名称展示 |
| `response_type` | `message`／`question`／`hint`／`skill_patch`／`growth_summary` |
| `feedback.message` | 主要展示文字；不要依赖内部存储的顶层 message |
| `question`、`hint_level` | 引导问题和提示等级，可能为 null |
| `feedback.run_id`、`turn_id`、`command_id` | 关联当前提交或提问，避免混入旧结果 |
| `skill_patch`、`patch_decision` | 可选代码建议及学生决定 |
| `feedback.source`、`degraded` | 回复来源信息，不是过关判断依据 |

新Bug练习流程不按失败阈值触发：主关正式Run成功后调用prepare，一局只生成一道同难度变式题；本局错误代码和证据用于出题。每次进入先start新的entry，旧局错误不进入新局练习上下文。普通提问、轮询和语音帧不增加失败次数。

**没有公开的失败计数写接口。** 新Challenge.failure_count是用于出题的本局证据条数，EntryStatus.attempts是挑战完成判题次数，二者不能互换。不要假定公共Interaction含failure_count。历史主关计数仍存在，不由start物理清空。

主动文字提示与语音始终使用叮当（teaching_agent）。旧主关管线仍可能产生阈值触发的bug_agent提问和task_completed的Book交互，用于兼容旧端与回放；新界面消费游标但忽略这些旧版Bug/Book展示，使用prepare/summary结果。Bug出题与Book文字走现有模型Relay；豆包负责主动实时语音和书书TTS。`world_agent`枚举存在不意味着有独立可调用接口。

## 6. 文字提问与代码修改建议

### 文字问叮当

使用同一个 `POST /v1/agent-sessions/{session_id}/turns`，关键是 **MESSAGE + 空 skill_bindings**：

```json
{
  "turn_id": "turn_hint_demo_0001",
  "expected_world_revision": 12,
  "input": {"type": "MESSAGE", "text": "我刚才提交成功了吗？", "locale": "zh-CN"},
  "skill_bindings": [],
  "client_state": {"last_event_sequence": 10, "client_turn_sequence": 5}
}
```

示例数字都需替换为最新 Session／世界值。202 后轮询 Command 和 Interaction 列表，按本次 turn_id／command_id 找回复，不要求创建一个新 Run。

后端读取当前任务、已保存的草稿、编译错误、最近正式 Run、学习记录和近期对话。HTTP 文字提问请求没有 editor_code 字段；若问题需要引用刚修改但未保存的代码，先保存 Draft。不要把任意额外字段塞进 input。

成功 Run 已提交但 Book 总结尚未完成时可以提问。当前实测仍可能出现第一次请求 HTTP 400、恢复后成功；根因尚未定位，联调时保留请求 ID 和 body，不能把所有 400 都按序号冲突盲目重试。

### 请求代码建议

1. 先 GET capabilities，确认 `skill_patch_enabled=true`。
2. 选中同一会话中符合条件的失败 Interaction，并读取其最新版本和当前 Draft。
3. 提交新 Turn，将 input 设为：

```json
{"type":"UI_ACTION","action_id":"request_ai_patch","selection_id":"interaction_failed_demo_0001"}
```

这次请求仍须提供当前已激活技能的**一个完整 skill_binding**及最新 world/turn 序号；不要复制文字提问的空数组。Worker 将 `request_ai_patch` 识别为专门的建议流程，不是重新执行技能。

4. 轮询后在新 Interaction 中读取 `response_type="skill_patch"` 与 `skill_patch`。
5. 展示 rationale、operations 中的原文件定位和新代码，让学生接受或拒绝。

当前建议仅支持当前入口文件的一个 `UPSERT_FILE` 操作；会核对选中失败证据与当前草稿是否对应。代码已改动、选中了不符合条件的旧失败、开关关闭等情况可能拒绝。`required_hint_level=4` 是后端处理约束；Turn 请求没有让前端手工填写 hint_level 的字段。

### 接受／拒绝建议

调用 [recordProductPatchDecision](HTTP接口参考.md#recordproductpatchdecision)：

```text
POST /product-experience/v1/sessions/{session_id}/agent-interactions/{interaction_id}/patches/{patch_id}/decision
```

请求体完整字段见 [JSON 示例](examples/product-experience-patch-decision-request.json)。从返回的**建议 Interaction 与 skill_patch**复制 session_id、turn_id、interaction_id、patch_id、patch_sha256、draft_id、skill_id、base_draft_revision、base_draft_sha256、result_draft_sha256；`expected_interaction_revision` 使用最新单条 Interaction 的修订号。前端只生成新的 decision_id、decided_at，并设置 `decision=ACCEPT` 或 `REJECT` 及 reason_code（允许 null）。

200 后查看 receipt.draft_updated：接受且写入成功时刷新 Draft，再刷新单条 Interaction。拒绝不会修改草稿。接受建议**不会自动编译、激活或运行**；学生再点击提交，按模块 3–4 走完整链路。

## 7. 问叮当实时语音

入口为同一后端的：

```text
WS /product-experience/v1/sessions/{session_id}/dingdang-voice
```

按现有按钮实现“点击开启持续对话，再点击关闭”。首帧发送游戏 token；收到 voice.ready 后持续上传 PCM，播放服务器返回的音频并展示字幕。无需前端单独接 STT、TTS，也无需直连豆包。

完整消息、音频规格、上下文同步和错误码见 [实时语音接口](实时语音接口.md)。这条语音连接不会创建 Turn／Run，不保存录音或聊天记录到 Interaction；关闭后重新打开是新的语音会话。文字聊天历史接口不会自动出现这些语音回合。

当前代码、运行结果和教学等级由后端供给语音模型；编辑器未保存内容通过 context 帧同步。音频流本身不负责保存 Draft、执行代码、修改世界或确认补丁。

## 8. 学习记录与成长总结

正式学习档案仍由后端learner worker自动处理，没有学生前端“写学习档案”接口。新Demo提供挑战通过后生成总结的practice summary接口；练习状态目前不会写入持久化学习档案。

| 页面需求 | 使用接口／字段 |
| --- | --- |
| 展示本次成功／失败及错误 | Run、Evidence |
| 新流程最终总结 | 主关Run成功→prepare→answer正确→summary；完整文字音频同时就绪再展示 |
| 旧版已归档总结 | Interaction列表的growth_summary，用feedback.run_id对应成功；旧speech接口补音频 |
| 恢复任务进度 | Workspace.current_task 与最新正式 Run |
| 展示教学阶段／提示等级 | Interaction.hint_level 及回复；语音由共享策略引导 |
| 教师查看掌握情况与班级统计 | 补充文档中的教师身份只读接口 |

新流程主关成功后显示Bug挑战准备中，而不是立即展示旧Book。挑战通过后summary返回完整结果，再同时展示全文和播放音频。当前旧前端已有的Book展示需要由前端同事按新状态机调整，不能同时开启两种完成展示。

旧总结按interaction_id/run_id去重，新挑战按entry_id/challenge_id恢复，summary可重放。恢复总结不重新执行主关。由代码建议辅助完成的学习标记由后端计算，前端不手工修改“独立完成”状态。

当前学生端没有独立公开的完整 LearnerProfile／知识点掌握表查询接口。需要教师查询时使用教师身份和脱敏 learner_ref，不能把教师查询当作学生 token 可调用的通用 profile 接口。

## 9. 前端联调验收顺序

| 操作 | 应观察到的结果 |
| --- | --- |
| 首次进入、退出后重新进入 | 同一当前会话恢复，草稿和世界状态正确 |
| 连续两次保存代码 | Draft revision 前进；旧版本保存产生明确冲突 |
| 提交正确代码 | Build CERTIFIED → Activation → Run SUCCEEDED/COMMITTED；prepare生成一题Bug挑战 |
| 主关连续答错并主动提示 | 错误作为本局出题证据；主动提示仍是叮当，不展示旧自动Bug提问 |
| Bug挑战答错、重试、答对 | 同题同局；重复ID只计一次；正确后summary返回全文及完整音频 |
| 新的一局 | 新entry；本局错误上下文从start时重新开始，不能用旧Run触发 |
| 正式运行成功后再提问 | 反馈关联最新成功 Run，不把“总结未完成”说成运行失败 |
| 连续文字聊天 | 本轮问题与近期历史进入上下文；不会重复执行技能 |
| 申请建议、分别接受与拒绝 | 接受只改草稿；拒绝不改草稿；都不自动运行 |
| 开启语音、修改代码、运行、插话、关闭 | 代码和正式结果能刷新，音频可打断，关闭后停止录音播放 |
| 提交后断线再恢复 | 根据已保存 command_id 对账，不重复提交；列表不重复显示 |

最近新增练习流程的真实实测与故障修复见[复查报告](../../../outputs/practice-bug-recheck-2026-09-10.md)，主关读取性能的历史报告见[读取优化记录](../../../outputs/published-read-optimization.md)。这些实测时间不是接口 SLA。

## 10. 文档维护

字段参考及样例可在仓库根目录重新生成：

```powershell
& walnut-world-backend/.venv/Scripts/python.exe walnut-world-backend/scripts/generate_frontend_api_reference.py
```

该命令只读取本地代码与合同，不启动 Docker、不连接数据库、不调用模型。新增业务接口后需同步本主文档中的功能流程；旧版 `Python后端接口文档.md` 的历史交付状态不能替代当前路由清单。
