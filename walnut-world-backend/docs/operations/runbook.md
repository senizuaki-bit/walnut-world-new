# Python 后端运行与验收手册

## 范围与边界

`walnut-world-backend` 是 Godot 唯一公开 HTTP Gateway，也是唯一生产 PostgreSQL 写入、事务和 Alembic 迁移权威。当前工作树线性 migration head 是 `019_int2_skill_patch_authority`，父修订为 `018_world_presentation_events`。

Sibling `agent` 只提供 additive v0.6 candidate Wire contracts、Ports、provider-neutral Runtime、Build/CAS 和 Sandbox 库；v0.4/v0.5历史字节继续锁定。生产不得启动第二个Agent产品HTTP、运行Agent私有migration、读取`yaya_*`表或代理到第二服务。

Compose 的产品拓扑是：

```text
postgres (private)
  -> migrate: alembic upgrade head (one-shot)
  -> backend 127.0.0.1:8790 -> :8000 (the only published listener)
  -> llm-relay :8081 (private; Provider key owner; no published port)
  -> docker-engine (private DinD daemon; no published port)
       -> sandbox-image (one-shot exact-digest pull)
       -> workflow-worker (Control + Build + Turn + terminal hand-off; no port)
  -> learner-worker (durable learner/product projection; no port)
```

`docker-engine` 与 `workflow-worker` 把同一个 `walnut-runtime` named volume 挂载到 `/var/lib/walnut`，并通过私有 named socket 通信。这样 Worker 交给 Docker daemon 的 bind source 在 daemon namespace 中真实存在；不要把 Windows host path 同时当作 Linux container target，也不要把 Docker Desktop 的 host socket 直接暴露给 Worker。`workflow-worker` 在 Backend-owned tables/UoW 中闭合 Control、Build/Certification、Activation、exact-version Turn 与 Run/World/Event/Evidence，再把 terminal hand-off 耐久交给独立 `learner-worker`；后者闭合 Learner、Product AgentInteraction 与 Workspace。Provider 失败时保留已经提交的客观 Run/World/Evidence，但不发布 `provider_fallback` Interaction、不推进 Learner。

INT2 capability GET始终挂载；`WALNUT_ENABLE_WORLD_PRESENTATION`控制presentation GET，`WALNUT_ENABLE_SKILL_PATCH`控制PatchDecision且只有World flag已开时才合法。两项默认false；正式 deterministic M2 与受控 real-Provider M2 只在 harness 子进程中显式开启，均已 PASS，但不得据此改变生产默认值。INT3 已增加且只增加三个飞书教师只读合同接口（learner query、class insights、redacted evidence）及其单一 MCP 适配层；其余 6 个飞书合同接口、Webhook 和飞书业务写路径继续排除。WSS、Client Event Batch、自动/多文件Patch也继续排除。

### INT3 飞书教师 MCP

唯一入口是 `POST /integrations/feishu/v1/mcp`。它是无会话的 Streamable HTTP JSON-RPC 端点，只声明 MCP `2025-06-18`，并只暴露以下三个工具：

- `query_learner_progress`（查询学生学习进度）；
- `query_class_common_issues`（查询班级共性问题）；
- `get_evidence_summary_and_links`（查看证据摘要及档案/Dashboard链接）。

三个工具复用同一 PostgreSQL 权威查询、Bearer JWT、tenant/actor/role/scope、错误合同和访问审计；除访问审计外不写业务表。Aily 必须使用短期、只读的教师 JWT，actor type 为 teacher，并仅含 `learner:read`、`class-insights:read`、`evidence:read`。JWT 只配置在 Aily 凭据/Authorization header 中，不得写入源码、URL、文档、Compose 或日志。只有把该路径部署到真实公网 HTTPS origin 并在 Aily 中完成连接，才能声称三个工具已接通；本地路由存在或测试通过不能替代云端连接。

教师调用不应填写或猜测权威哈希：`query_learner_progress` 只必填 Base/妙搭可见的匿名 `learner_ref`；`query_class_common_issues` 可以零参数调用，`class_ref` 只由 JWT tenant 派生，缺省窗口为 Asia/Shanghai 今日及前 6 个自然日到请求时刻。两者缺省 `content_ref` 都从租户内 SHA 校验后的唯一 Learner Profile 内容解析；无候选返回 NOT_FOUND，多候选返回 INVALID_REQUEST 并要求显式消歧。`get_evidence_summary_and_links` 的 `evidence_id` 只能取自前一个学生查询的 `recent_evidence`，不得由模型构造。显式 class/content 仍执行原 authority 校验。

为兼容标准 MCP 客户端，只有上述精确路径允许省略 Walnut 私有的 `X-Request-Id`、`X-Trace-Id`、`X-Correlation-Id` 和 `X-Schema-Version`，服务端会生成前三者并将 schema 固定为 `1.0.0`；客户端若显式发送非法值仍拒绝。其他 HTTP/WS 路径的原有 transport 合同不变。

## 合同发布 pin

Backend 当前 `contract-release.json` 指向尚未发布 tag 的 additive v0.6 candidate：

- package `0.6.0`；
- release ref `refs/tags/agent-contracts-v0.6.0`；
- manifest 27,848 bytes、147 entries；
- manifest SHA-256 `11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05`。

部署和测试前逐字节验证 sibling Agent：

```powershell
$env:WALNUT_CONTRACT_PATH = (Resolve-Path '..\agent').Path
py -3.12 scripts/verify_contract_release.py --agent-repo $env:WALNUT_CONTRACT_PATH
```

校验不依赖 clean Git/HEAD；它核对当前描述符、manifest closed shape、排序、实际 bytes/hash、package/release 闭合，逐项核对 v0.4/v0.5 previous-release locks 的冻结 inventory/digest，并从已验证的 v0.4 inventory 重建和核对 v0.3 冻结 digest。任一当前或历史字节漂移都必须停止部署；重新生成当前 manifest 不能洗白旧 release drift。

远端 Agent 仓已发布的 annotated v0.4 tag 只作 historical release evidence。当前 v0.6 tag 尚不存在，因此 candidate 的 release identity 为 `NOT_PROVEN`；不能把 descriptor 对齐或 manifest 自校验写成已发布 release PASS。

## 本地分仓验收

需要 Python 3.12、Docker Desktop、可丢弃 PostgreSQL 16 和本机已有的 digest-pinned image。先记录磁盘、Docker daemon/内存和数据库状态；基础设施失败不能记作产品 PASS。

```powershell
docker run --name walnut-postgres-test `
  -e POSTGRES_PASSWORD=postgres `
  -e POSTGRES_DB=walnut_test `
  -p 55432:5432 `
  -d postgres:16.9-alpine@sha256:7c688148e5e156d0e86df7ba8ae5a05a2386aaec1e2ad8e6d11bdf10504b1fb7

.\scripts\verify_all.ps1 `
  -DatabaseUrl 'postgresql://postgres:postgres@127.0.0.1:55432/walnut_test'
```

旧 INT1 fresh gate 299/299 与更早 252/252 仅作 historical evidence。当前 INT2 Backend 已在 fresh PostgreSQL、Alembic head 019 上完成 full gate：468/468 PASS，0 failure/error/skip；JUnit XML 为 `artifacts/backend-full-int2-live-final-20260815T195320Z-671651fc.xml`，SHA-256 `852068818ADB98BEB12B830CEF27BBAE6928515C6CE3A1DF63FE6B43F3150DF6`，Ruff、Pyright、compileall 与 contracts 同步通过。该 full 证明当前 Backend non-live 门禁；受控 real-Provider M2 由下面独立 live 证据证明，production private DinD 仍为 `NOT_PROVEN`。

迁移必须从空库升级到当前head `019_int2_skill_patch_authority`，并验证`018_world_presentation_events -> 019_int2_skill_patch_authority`线性边界。不要手工修改schema、stamp跳过migration或预置业务结果。

## INT1 authority seed

真实链从允许的平台前置权威开始，不是空账号旅程。只使用 `python -m walnut_backend.int1_e2e_authority`，并遵循 [authority seeder 文档](int1-e2e-authority.md)。Seeder 使用 production HS256 `JwtAuthenticator`，只创建 Published Content、初始 World、Learner/Profile、BuildPolicy、LaunchAuthority、revision-zero Registry 和空 Artifact root；它不得创建 Session、Draft、Build、Artifact、Certification、Activation、Run、Evidence、Interaction 或 worker Job。

Session Control 成功终态会在同一事务创建 server-owned Session、revision-1 starter Draft 与 Workspace。后续 Draft CAS、Turn 接受和 terminal projection 刷新 Workspace；客户端无须也不得人工提供 Session ID。

## Compose 启动

所有 secret 只从当前进程环境或受限 secret 文件注入。不要把数据库密码、JWT secret、Provider key 或短期学生 token 写进 Compose、文档、fixture、命令日志或报告。

至少设置：

```powershell
$env:POSTGRES_PASSWORD = '<secret>'
$env:WALNUT_AUTH_HMAC_SECRET = '<32+ character secret>'
$env:WALNUT_FEISHU_PSEUDONYM_SECRET = '<32+ character stable pseudonym secret>'
$env:WALNUT_FEISHU_MCP_DASHBOARD_URL = 'https://larkcommunity.feishu.cn/base/Q6V9biulZaezaHskZqacYm2JnHe?table=blkK3ldZA0pePRGb'
$env:WALNUT_FEISHU_MCP_TEACHER_WORKSPACE_URL = 'https://dcniaqwtmoca.feishuapp.com/app/app_17c6bc5hz7d'
$env:WALNUT_AUTH_ISSUER = 'walnut-api'
$env:WALNUT_AUTH_AUDIENCE = 'walnut-client'
$env:WALNUT_TENANT_ID = '<tenant-id>'
$env:WALNUT_BUILD_IMAGE = 'walnut/backend@sha256:<verified immutable image digest>'
$env:WALNUT_DIND_IMAGE = 'docker:29-dind@sha256:<verified immutable image digest>'
$env:WALNUT_POSTGRES_IMAGE = 'postgres:16.9-alpine@sha256:<verified immutable image digest>'
$env:WALNUT_SANDBOX_IMAGE = 'gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c'
$env:WALNUT_LLM_RELAY_API_KEY = '<private relay secret injected only into this process>'
$env:WALNUT_LLM_UPSTREAM_API_KEY = '<Provider key injected only into this process>'
$env:WALNUT_LLM_UPSTREAM_ENDPOINT = 'https://api.deepseek.com/chat/completions'
$env:WALNUT_LLM_MODEL = '<model-id>'
$env:WALNUT_LLM_PROVIDER = '<provider-id>'
$env:WALNUT_PROMPT_VERSION = '<prompt-version>'
$env:WALNUT_TEACHING_SPEC_VERSION = '<teaching-spec-version>'
$env:WALNUT_WORLD_RULES_VERSION = '<world-rules-version>'
$env:WALNUT_WORLD_CONTENT_VERSION = '<content-version>'

py -3.12 .\scripts\run_compose.py up --detach
```

两个 `WALNUT_FEISHU_MCP_*_URL` 是必填的非敏感展示链接：前者允许 Dashboard query，后者必须是不含 query、userinfo 或凭据的 HTTPS 妙搭应用根 URL。它们不是 MCP 公网地址，也不能代替 Aily 凭据。外部部署仍需把 Gateway 的 `/integrations/feishu/v1/mcp` 暴露到受控公网 HTTPS origin，并在 Aily 侧单独配置只读教师 Authorization。

Compose 自带的私有 `llm-relay` 实现 `YAYA_RECOVERABLE_LLM_V1`；`workflow-worker` 的 relay endpoint 固定为同一 network namespace 内的 `http://127.0.0.1:8081`，不得从主机覆盖成第二个服务。启动时会做 capability GET，随后按稳定 `dispatch_id` 原子创建/重放 PUT，并以只读 GET 对账；结果至少保留 604800 秒，且 `max_generation_count=1`。普通 `/v1/chat/completions` POST 没有客户端可寻址的耐久结果，响应或 ACK 丢失后无法区分“未执行”和“已成功”，因此配置校验会直接拒绝，生产 worker 不会回退到普通 `LlmPort`。

Relay 返回 `PENDING` 时必须提供并遵守 `Retry-After`，job 的 `available_at` 由数据库时钟推进；lease 接管产生新 fence，但复用同一 dispatch。每次恢复联合核对 PostgreSQL dispatch/result receipt 的 hash、fencing token、relay completion hash、原始 Provider response bytes hash 和 `generation_count`；receipt 与 relay 共同漂移或任一单边篡改都 fail closed。Result receipt COMMIT ACK 丢失后先在新事务重读精确 receipt，不因 ACK 不确定再次生成。

`WALNUT_DIND_IMAGE` 和 `WALNUT_SANDBOX_IMAGE` 都必须使用实际 registry digest，不能把示例 digest 当成已验证镜像。Compose 内的 `WALNUT_RUNTIME_ROOT` 固定为 `/var/lib/walnut`，持久数据由 `walnut-runtime` volume 管理；不要再从 Windows 环境注入同名路径。`WALNUT_DEVELOPMENT_AUTH` 必须保持 `false`。Provider live acceptance 只接受真实 recoverable relay 返回的 `source=provider`、`degraded=false`；本地 fixture relay 只能用于 recovery/wiring diagnostics，不能满足 live 门禁。

## Godot 真实 Gateway 门禁

Authority seed、Gateway 和 worker 使用同一数据库、runtime root、JWT issuer/audience/secret、Provider/model/prompt、TeachingSpec、WorldRules 与 sandbox image。将 seeder 输出的短期 production JWT 仅注入 Frontend 进程：

```powershell
$env:YAYA_API_BASE_URL = 'http://127.0.0.1:8790'
$env:YAYA_AUTH_TOKEN = '<short-lived-student-jwt>'
$env:GODOT_EXE = '<Godot 4.5.2 console executable>'
..\walnut-world-frontend\scripts\run-real-gateway-e2e.ps1
```

Frontend plaintext transport 固定只允许 loopback `8790`。Compose 已将 host `127.0.0.1:8790` 映射到唯一 backend container 的 8000；不要启动第二个服务、额外代理或放宽 transport。

该门禁必须从 Student Bootstrap 经公共 HTTP 产生 Session/Draft/Workspace、Build/Certification、Activation、Turn、Run/World/Event/Snapshot/Evidence、Learner/Interaction/Workspace，并由正式 `app_root.tscn` 校验和展示。不得 SQL seed 业务结果。

### 当前 INT2 门禁状态

2026-08-15 的正式 deterministic M2 已以
`DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER` 完成 **270.638 秒 PASS**；outer stdout
SHA-256 为 `90442f1f1171a6014f4025241bb71d3c7afc1d5b3e64499eccb30460dd3640dc`。
它闭合 6 Turn、5 Run、6 Interaction、13 Evidence、16 个唯一 fixture dispatch/generation，
以及 11 个 terminal Command（7 `APPLIED` + 4 `REJECTED`）和 11 个 command receipt。数据库
deterministic full-row authority SHA-256 为
`a37d5c503d136396d0e4fe0f0f7f13594e6dc632c9095d2ae20b6a101b14e13a`。

World revision 1 只有 1 个 committed World domain event，`last_event_sequence=1`；不要把它和
独立的 8 个 authoritative presentation event（high watermark 8）混为同一计数。学生确认后的
Patch 状态为 `PUBLIC_UI_CHAIN_CLOSED`，public-chain SHA-256 为
`102dcec526ca0ffd088cf5f465b3bcaab0af1e97fe0b60980f4833084fe63fff`。同一临时 PostgreSQL
容器的公开端口真实关闭 3,785 ms，Gateway 数据库 GET 在断库时 fail closed 为 HTTP 500，随后
原容器恢复；phase2 新进程只发出 17 GET / 0 mutation。DB/fixture-relay/Sandbox/Artifact 指纹在
断库和三服务重启前后均不变，清理后原有 3 个 Docker 容器的 full ID、运行态和 canonical bytes
精确恢复。

这是 fixture-relay、digest-pinned host-Docker 的 deterministic 证据。独立的受控
real-Provider M2 run `868a` 于 2026-08-15 在 **301.012 秒**取得 PASS：18 unique Provider
dispatch / 18 generation、单 dispatch 最大 generation 1，注入 response-loss 后复用同一
dispatch 且 generation 仍为 1；学生可见 Patch 链为 `PUBLIC_UI_CHAIN_CLOSED`。该 live 同样闭合
1 个 World commit、8 个 presentation event、11 个 terminal Command（7 `APPLIED` + 4
`REJECTED`）和 phase2 17 GET / 0 mutation。outer stdout SHA-256 为
`2A7D2C057EF66D54F4DBFA828166DAC0A688704471618E6FA2940CE4F95B2425`，数据库 SHA-256 为
`b8bb2b568ac6978d938a98d041f9c5b74ef108167f53790c4bbeecbb6c051e30`；清理后精确恢复原有
3 个 Docker 容器。

production private DinD 与公开 Gateway pending write response-loss 仍为 `NOT_PROVEN`；live
harness 的 private relay/proxy response-loss PASS 不能替代后者。2026-08-13 的 194.12 秒
DeepSeek V4 Flash 记录只是 historical INT1 real-Provider / host-Docker evidence，不替代当前
INT2 run `868a`。

### Billable real-Provider wrapper（INT2 M2 已验证）

只有下列 repo-owned wrapper 可以启动显式付费门禁。先清除 direct-key 变量，再从一个绝对路径的受限 key file 注入；key file 的 Windows DACL、大小与严格 UTF-8 要求见 [本地诊断说明](int1-local-diagnostic.md)。PostgreSQL 与 Sandbox 镜像参数必须是以下已知 exact digest，且本机必须已存在这些镜像；wrapper 不会拉取镜像。

```powershell
[Environment]::SetEnvironmentVariable('WALNUT_LLM_UPSTREAM_API_KEY', $null, 'Process')
$env:WALNUT_INT1_REAL_PROVIDER_E2E = 'true'
$env:WALNUT_LLM_UPSTREAM_API_KEY_FILE = (Resolve-Path 'C:\secure\deepseek.key').Path
$env:GODOT_EXE = 'C:\tools\Godot_v4.5.2-stable_win64_console.exe'

.\scripts\run-int1-real-provider-e2e.ps1 `
  -PostgresImage 'postgres:16.9-alpine@sha256:7c688148e5e156d0e86df7ba8ae5a05a2386aaec1e2ad8e6d11bdf10504b1fb7' `
  -SandboxImage 'gcc@sha256:b99b86a28812b1e6453a231a947dc43d76fe192788a12f344a9b568bf9f5d24c' `
  -Provider 'deepseek' `
  -Model 'deepseek-v4-flash' `
  -UpstreamEndpoint 'https://api.deepseek.com/chat/completions' `
  -EnableWorldPresentation `
  -EnableSkillPatch
```

运行前将两个示例绝对路径替换为实际受控路径，并确认 Provider/model/endpoint 是本次获准的 live 配置。必须只设置 `WALNUT_LLM_UPSTREAM_API_KEY_FILE` 或 `WALNUT_LLM_UPSTREAM_API_KEY` 其中之一。Wrapper 仅在 billable 子进程中设置 `WALNUT_LLM_RELAY_MAX_TOTAL_GENERATIONS`：M1 为 24，带 `-EnableSkillPatch` 的六 Turn M2 为 32；结束后恢复调用进程原值，首个超限新 dispatch 必须在 Provider POST 前 fail loud，生产 Compose 和其他场景保持 uncapped 默认。该 wrapper 使用 digest-pinned host Docker；它不是 private DinD live gate，也不注入真实 Docker control-plane response loss。

当前 INT2 live 的脱敏 stdout hash 与结构化断言记录在 sibling Agent 的证据账本中；临时目录不是唯一证据位置。该 host-Docker PASS 不扩大为 production private DinD，fault proxy 的 private relay PUT/GET recovery 也不扩大为公开 Gateway pending write response-loss，后两项仍为 `NOT_PROVEN`。

### 2026-08-12 本地 relay 诊断与历史 wiring

历史 27.6s 三仓记录使用普通确定性 Provider POST，只证明当时的业务 wiring；它没有 capability/PUT/GET recovery contract，也早于真实 Draft PUT/CAS 与正式 UI display 指纹，不能继续作为当前生产 Provider 路径的通过证据。当前 repo-owned recovery harness 改由 fixture relay 驱动；协议/fresh-PostgreSQL focused gates 已验证 PUT 已物化后响应与首次 GET 丢失、新 worker/新 fence GET 同一终态、真实 `JobStepReceipt`、结果 commit-ACK 重读、`Retry-After` 和 `generation_count=1`。

169.836 秒 repo-owned harness 记录的内层 preflight 曾为可用物理内存 `2837123072` bytes、可用磁盘 `53827231744` bytes、0 个运行中容器、所需镜像已存在且固定 loopback `8790` 可用。该历史运行完成四个 Turn（3 失败 + 1 成功；`teaching_agent/teaching_agent/bug_agent/book_agent`）、4 个 Run/Interaction、4 个 learner projection、三个正式 UI panel、Provider PUT ACK/GET recovery、同一 disposable PostgreSQL stop/start、三服务数据库重连与新 PID recovery-only phase2；当时 12 个 dispatch 均为 `generation_count=1`，phase2 为 8 GET / 0 mutation，DB/relay/Sandbox/Artifact 指纹保持不变，分类为 **`DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER` PASS**。它仍是 fixture 历史证据；上面的 194.12 秒记录也只是 historical INT1 real-Provider evidence。两者均使用 digest-pinned host Docker，不是当前 INT2 live 或 production Compose private DinD 证据。完整命令与证据边界见 [本地诊断说明](int1-local-diagnostic.md) 和 sibling Agent 的主报告。

## 运行期核查与故障处置

- `202` 只表示 Command 已耐久接收；使用原 `Location` 对账，`UNKNOWN_COMMIT_STATE` 不换 key。
- 相同 key + byte-equivalent body 重放原 receipt；相同 key + 不同 body fail closed。
- Content、Workspace、Draft、Build、Activation、Run、Evidence、Events、Snapshot 与 Interaction 必须按 actor/content/session/world/exact tuple/revision/sequence/hash 交叉闭合。
- lease 过期只能由新 fencing token 接管；旧 worker 不能提交。Provider 恢复先读 PostgreSQL result receipt，再对稳定 `dispatch_id` 做 relay GET；只有线性一致 GET 明确返回 ABSENT 才允许同 ID PUT，不能调用普通 Provider POST。Build/Sandbox 使用稳定、全标签容器身份：start/create 控制面响应丢失后 inspect/wait/logs/reconcile 同一容器；临时 Docker 不可用保持 retryable，不能落成学生失败或第二次执行。
- 迁移失败：停止 Gateway/worker并记录 revision；不要手修 schema 后继续服务。
- 合同 pin 失败：停止部署；不要用 Markdown、旧 tag 或重新生成 hash 掩盖漂移。
- Provider 不可用：客观 Run/World/Evidence 可保留；不得发布 fallback Interaction 或推进 Learner。
- Artifact、Certification、registry、receipt、snapshot、phase、diagnostic 或 binding 损坏：fail closed，不自动修成可信结果。

当前 INT2 跨仓命令、计数、identity/revision/sequence/hash 和副作用指纹统一记录在 sibling Agent 的 `docs/INT2_CROSS_REPO_VALIDATION_REPORT.md`；历史 INT1 记录仍归档在 `docs/INT1_CROSS_REPOSITORY_VALIDATION_REPORT.md`。
