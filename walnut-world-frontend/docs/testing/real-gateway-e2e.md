# 真实 Walnut Gateway Godot E2E（opt-in）

此门禁加载生产 `scenes/app/app_root.tscn`，只使用生产 `HTTPRequest` transport、Game/Product Gateway、ClientStore 与 SessionController。它连接唯一 `walnut-world-backend` Gateway；不会启动 Agent 的历史 `yaya_agent_backend` 服务，不注入 FakeGateway、fixture、预制 Build/Certification 或业务结果 SQL。

普通 offline Godot suite 当前为 60/60 PASS，另精确排除两条 real-Provider opt-in 用例并将其记录为 `EXCLUDED_NOT_RUN`，不计入 skip 或 PASS；当前 0 skip/fail，stdout SHA-256 为 `269E5D6BA4FDCEFBBDCF82E33FDA204C820AD942EAECA2312DDED37753D8C2E4`。只有设置 `YAYA_REAL_GATEWAY_E2E=1` 后，缺配置、缺 capability/route、非终态 worker、degraded/fallback Provider、Evidence/Events/Snapshot/Interaction 错链都会非零失败。

## 生产拓扑与允许的初始权威

服务端必须是：

```text
disposable PostgreSQL
  -> Backend Alembic head 019_int2_skill_patch_authority (parent 018_world_presentation_events)
  -> production HS256 INT1 authority seeder
  -> walnut-world-backend Gateway (the only listener)
  -> combined workflow-worker
  -> digest-pinned Docker + recoverable relay backed by a real Provider
```

Seeder 只允许创建 Published Content/Task、初始 World、Learner/Profile、BuildPolicy、LaunchAuthority、revision-zero Registry 和空 Artifact root。它不得创建 Session、Draft、Build、Artifact、Certification、Activation、Run、Evidence、Interaction 或 worker Job。Session Control 的成功终态会在同一 Backend 事务创建 server-owned Session、revision-1 starter Draft 与 Workspace；测试不再预建这些资源。

同一短期 production JWT 下，`GET /v1/student-bootstrap` 必须返回五个 INT1 HTTP core capability（`skill_builds`、`skill_activations`、`agent_sessions`、`http_world_recovery`、`evidence_query`）为 true；同时返回 exact seven-field Session create request、Build policy、full Activation scope/registry revision、可空 exact active tuple，以及 HTTP events/snapshot authority。WSS 与 Client Event Batch 是未挂载的排除项；v0.4 `StudentBootstrapV2` 不包含 `world_event_stream`、`client_event_batch` 或 `stream_url` 响应字段，测试不得为它们建立断言。Content、Workspace、Draft、Build、Activation、Turn、Command、Run、Evidence、World Events/Snapshot 与 Interaction 都必须从同一 actor/content/session/world scope 可读。

这不是空账号旅程，但所有业务结果必须由公共 HTTP + durable worker 产生。测试期间不得有并发 World writer。

## Provider 门禁

Live acceptance 只接受 recoverable relay 后真实 Provider 的 `source=provider`、`degraded=false` 结果。Relay endpoint/key 与 Provider/model 标识只从服务进程环境或受限 secret 文件注入；不要写进脚本、仓库、trace、日志或报告。Worker 启动必须 capability fail-fast；稳定 dispatch 采用 GET-first、权威 `ABSENT` 后同 ID PUT，`PENDING` 遵守 `Retry-After`，并联合验证 fence、数据库 receipt、completion/raw Provider bytes hash。

当前 INT2 真实 Provider M2 门禁已 PASS。运行 `run868a` 用时 301.012 秒，DeepSeek `deepseek-v4-flash` 的全部可见决策均为 `source=provider`、`degraded=false`；18 unique dispatch / 18 generation，单 dispatch 最大 1。受控 Provider relay response-loss 恢复同一 dispatch，generation 仍为 1；这项证据不等同于公开 Gateway pending write response-loss。

该运行从 fresh authority 开始，经正式 Godot UI 闭合 4 次客观失败 → Request Patch → Dialog 显式 `ACCEPT` → 手动 Build/Activate/Run，Patch 最终状态为 `PUBLIC_UI_CHAIN_CLOSED`。Phase 1 精确为 12 POST/1 PUT、6 Turn、5 Run、11 terminal Command（7 `APPLIED` + 4 `REJECTED`）、1 条 World commit 与 8 条 presentation event；PostgreSQL 断库/原容器恢复及三服务新进程后，Phase 2 为 17 GET/0 mutation。DB SHA-256 为 `b8bb2b568ac6978d938a98d041f9c5b74ef108167f53790c4bbeecbb6c051e30`，脱敏 stdout SHA-256 为 `2A7D2C057EF66D54F4DBFA828166DAC0A688704471618E6FA2940CE4F95B2425`；清理后精确恢复运行前原有 3 个 Docker 容器。

2026-08-13 的 194.12 秒 historical INT1 live 闭合 4 个 Turn/Run/Interaction/Learner 与同 dispatch 恢复；可复现入口、脱敏 stdout hash 和结构化断言保留在版本化 INT1 报告中。它不是当前 INT2 证据，当前 INT2 PASS 也不是 production private DinD。

2026-08-12 的 direct-POST 与 169.836 秒 fixture relay 结果仍保留为历史/确定性证据，不作为当前 real-Provider PASS 的来源。

Windows 本地 runner 应使用短 runtime root（例如 `%TEMP%\wi1-<8-hex>`），避免深 `%TEMP%` 路径超过 Artifact receipt hard-link 的路径预算。生产 Compose 的 `/var/lib/walnut` 不受该 Windows 主机限制。

## 启动顺序

1. 在`walnut-world-backend`对disposable PostgreSQL执行`python -m alembic upgrade head`，确认current head为`019_int2_skill_patch_authority`。
2. 设置 production JWT、contract release、runtime root、pinned image、recoverable relay、Provider/model/prompt、TeachingSpec 与 WorldRules 配置；运行 `python -m walnut_backend.int1_e2e_authority`。保留它输出的短期 `authorization`，不要输出 JWT secret 或 relay key。
3. 用完全相同的数据库/runtime/authority 配置启动 combined worker：

```powershell
python -m walnut_backend.worker_main
```

4. 启动唯一 Gateway。Frontend plaintext transport 只允许明确的 loopback port `8790`，因此本地直连使用：

```powershell
python -m uvicorn walnut_backend.main:app --host 127.0.0.1 --port 8790
```

部署环境应使用 HTTPS；不要为了测试放宽 transport。若使用 Compose 默认 `8000` 端口，应通过 HTTPS ingress，或使用不改变容器监听器的本地端口映射/override 将 host `127.0.0.1:8790` 映射到唯一 backend container。任何情况下都只有一个产品 Gateway。

## 两阶段调用 Godot 门禁

Frontend 只注入 endpoint、seeder 返回的短期 student JWT，以及一个不在仓库内的 phase-1 指纹路径：

```powershell
$env:YAYA_API_BASE_URL = 'http://127.0.0.1:8790'
$env:YAYA_AUTH_TOKEN = '<short-lived-student-jwt>'
$env:GODOT_EXE = '<Godot 4.5.2 console executable>'
$phase1Fingerprint = Join-Path $env:TEMP 'walnut-real-phase1-authority.json'
```

严格按以下顺序运行；两个 Godot 进程之间必须真实观测任务自有 PostgreSQL 断库/原容器恢复，以及 Gateway/workflow/learner 三服务新进程重启；不能把两条 Godot 命令连续执行后声称跨进程恢复：

1. 重置 exact `user://real_gateway_chain_<base-url-hash>.json` persistence family（主文件、`.bak`、`.tmp`），运行 phase 1，并把经过 runner 校验的完整 authority fingerprint 持久化到显式路径：

   ```powershell
   .\scripts\run-real-gateway-e2e.ps1 `
     -ResetPersistence `
     -EnableWorldPresentation `
     -EnableSkillPatch `
     -Phase1FingerprintPath $phase1Fingerprint `
     -TotalDeadlineSeconds 900 `
     -ResourceDeadlineSeconds 300 `
     -InteractionDeadlineSeconds 120
   ```

2. 在 Frontend 进程之外停止本次门禁创建的 disposable PostgreSQL；确认已发布端口关闭、数据库型 Gateway GET 失败且 Gateway/workflow/learner 进程仍存活。重启精确的原 PostgreSQL 容器，确认同一 container ID、恢复后 Snapshot exact 不变，并产生 `INT1_LOCAL_DIAGNOSTIC_DATABASE_OUTAGE_FINGERPRINT`；不得操作任何非本次门禁所有的容器。
3. 停止 phase-1 Gateway/workflow/learner，确认中间无 Gateway listener，再以新 PID 和新 worker identity 启动三服务。保留同一 PostgreSQL、runtime root、authority、base URL、JWT scope、phase-1 persistence 与指纹文件。
4. 启动独立 recovery-only Godot 进程，加载同一个 phase-1 指纹，逐字段 exact 比对 persistence SHA、Session、Workspace、Draft、World、presentation、Skill Patch、Interaction feedback 与 active tuple；完成后清理并断言主文件、`.bak`、`.tmp` 均无残留：

   ```powershell
   .\scripts\run-real-gateway-e2e.ps1 `
     -RecoveryOnly `
     -CleanupPersistence `
     -EnableWorldPresentation `
     -EnableSkillPatch `
     -Phase1FingerprintPath $phase1Fingerprint `
     -TotalDeadlineSeconds 300 `
     -ResourceDeadlineSeconds 180 `
     -InteractionDeadlineSeconds 90
   ```

脚本只临时设置 opt-in/deadline/指纹路径环境变量，结束后恢复原值，不打印或修改 token。Phase 1 只接受一行结构化 `REAL_GATEWAY_CHAIN_E2E_PASS`，RecoveryOnly 只接受一行 `REAL_GATEWAY_CHAIN_RECOVERY_PASS`。生产 HTTP transport 的 bounded audit 只记录 operation、method、path、完成状态与响应 status，不记录 token、headers 或 body。M2 Phase 1 必须从真实 audit 观测到 exact 12 POST/1 PUT：1 Session create、1 Draft CAS、2 Build、2 Activation、6 Turn 和 1 PatchDecision。当前 deterministic actual10 与真实 Provider `run868a` 的 RecoveryOnly 审计均为 exact 17 GET/0 mutation，且 `total_started == total_completed`；不能用 `no_mutation=true` 或硬编码 side-effect count 自证。

Phase-1 指纹包含 persistence bytes SHA、authority binding、完整 Session 与 active tuple、Workspace/Draft/World/Interaction digest、World presentation high watermark、Skill Patch公开权威与完整 Interaction feedback。RecoveryOnly 在 AppRoot 前先绑定本次 phase-1 persistence bytes，在恢复后再 exact 比对整组 authority fingerprint；旧运行或另一 base URL 的指纹会 fail closed。

生产代码支持持久化 `agent_turn`/`agent_hint` envelope 的 stable-ID、原 Idempotency-Key、原 request body 与原 cursor 恢复。不过当前 live Gateway 没有验收安全的 response-loss fault injection，本门禁没有构造“服务端已提交、客户端未收到响应”的 live pending envelope。因此两阶段指纹必须把 `live_pending_response_loss.status` 明确记为 **`NOT_PROVEN`**；offline 跨 ClientStore focused tests 不能把这一项提升为 real-Provider PASS。

## 验收闭包

用例严格执行：

1. AppRoot：Student Bootstrap → Content → server-created Session → revision-1 Workspace/starter Draft → Snapshot/Interaction；要求 fresh authority 的 active tuple 为 null。
2. 对 starter entrypoint 追加确定性 failure marker，正式执行第一次 Product Draft PUT/CAS，将 Draft/Workspace 从 revision `1 → 2`；通过正式 `BuildButton` 完成第一次 terminal Build/PUBLIC/HIDDEN/Certification，再通过独立 `ActivationButton` 完成第一次 exact Activation。
3. 在 failure tuple 上通过正式 `SubmitButton` 连续执行 4 次 Turn；角色必须为 `teaching_agent/teaching_agent/bug_agent/bug_agent`，4 个 Command 与 4 个 Run 均为 `REJECTED`，共同 canonical reason 为 `TASK_INCOMPLETE`，World authority不得前进。
4. 只有第4次客观失败在正式UI可见后，学生才能点击 `RequestAiPatchButton`。该Request Patch创建第5个、无Run的`teaching_agent` proposal Turn，并打开包含before/after、operation hash与Evidence引用的正式Dialog；请求预览本身不得更改Draft、Workspace、World或active Skill。
5. 学生必须在Dialog中显式`ACCEPT`；PatchDecision回执与canonical Draft同时逐字段对账，接受后Draft从revision `2 → 3`。`ACCEPT`只更新Draft/建议权威，不得自动Build、Activate或Run。
6. 学生随后依次手动点击正式Build、Activate与Run控件；第二组Build/Certification/Activation必须与accepted Draft精确绑定，registry revision为2，第6个`book_agent` Turn的Command为`APPLIED`、Run为`SUCCEEDED`。
7. 后端全链必须精确为6 Turn、5 Run和11 terminal Command：1 Session + 2 Build + 2 Activation + 6 Turn Command，其中7个`APPLIED`、4个`REJECTED`；每个Command都有唯一回执，5个Run及其Evidence、6个Interaction闭合且不重复副作用。
8. 唯一成功Run产生精确1条`world.committed`域事件和8条有序World presentation事件；正式`WorldViewport`必须完整经历一次PLAYING播放并达到high watermark 8，不得把8条presentation误计为8条World commit。
9. 分别核对ClientStore权威与正式`TaskWorkspace`、`DialoguePanel`、`WorldViewport`的Draft/Interaction/Snapshot/Patch/presentation投影；只有transport audit、公开链hash与后端PostgreSQL全行指纹均闭合，才输出`REAL_GATEWAY_CHAIN_E2E_PASS`。
10. 外部编排必须再闭合 PostgreSQL 断库/恢复指纹、三服务新进程身份、RecoveryOnly exact 17 GET/0 mutation、phase-1 指纹不变与持久化文件 cleanup，才能把对应 deterministic 或真实 Provider M2/restart 标记为 PASS。

响应丢失/重试必须保持 stable ID、Idempotency-Key 与 byte-equivalent body。Provider relay 使用同一 dispatch GET/PUT/GET；Build/Sandbox inspect/wait/logs/reconcile 同一全标签容器。进程重启、lease takeover 或 receipt reconciliation 不得重复 Provider、Docker compile、PUBLIC/HIDDEN tests、Artifact、Certification、Activation、Sandbox、World CAS/Event、Interaction 或 Learner revision。

静态 fixture-free 门禁可独立运行，不需要服务端：

```powershell
& $env:GODOT_EXE --headless --path . --script res://tests/client/real_gateway_chain_e2e_static_test.gd
```

静态门禁、offline 60/60、focused recovery 或 fixture 均不能替代 INT2 real Provider。当前 M1、deterministic actual10/outage/restart 与真实 Provider M2 `run868a` 均已 PASS；两条 real opt-in 在普通 discovery 中仍为 `EXCLUDED_NOT_RUN`。Historical INT1 live 不是当前 INT2 证据；production private DinD 和公开 Gateway pending write response-loss 仍为 `NOT_PROVEN`。PatchDecision 默认关闭并按 Backend flag 条件挂载；WSS/Event Batch/Feishu 与自动/多文件 Patch 排除。
