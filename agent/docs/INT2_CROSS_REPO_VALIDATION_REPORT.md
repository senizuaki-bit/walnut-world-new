# INT2 跨仓验证报告

更新日期：2026-08-15（Asia/Shanghai）

本报告只记录可复现命令与脱敏结论。它不包含 Provider 密钥、Provider 请求或响应正文、聊天凭据、本机隐私文件，也不把临时目录作为唯一证据。各门禁只有实际完成后才从 `NOT_PROVEN` 更新为 `PASS`。

## 证据分类

| 分类 | 含义 | 当前状态 |
| --- | --- | --- |
| Current PASS | 在当前三仓工作树上执行并通过 | Agent discovery 601 = 599 non-live PASS + 2 exact `EXCLUDED_NOT_RUN`；Backend current-tree full 468/468；Frontend offline 60/60 + 2 real opt-in excluded；里程碑一；deterministic M2 actual10；受控 real-Provider M2 run `868a` |
| Historical evidence | 旧提交、旧报告或旧合同的证据，仅作背景 | 不计入 INT2 当前完成标准 |
| Deterministic evidence | 不计费、固定 Provider/沙箱输入，可重复运行 | 完整 INT2 M2 正式公开 UI 链、PostgreSQL 断库恢复与第二 Godot 进程 GET-only 恢复均为 `PASS` |
| Real Provider evidence | 受控真实 DeepSeek Provider，`source=provider` 且 `degraded=false` | run `868a` 在 301.012 秒取得 `PASS`；18 unique dispatch / 18 generation、单 dispatch 最大 generation 1；学生公开 Patch 链为 `PUBLIC_UI_CHAIN_CLOSED` |
| Host Docker | 本机 Docker 运行的测试 PostgreSQL/沙箱 | 已用于里程碑一、正式 deterministic M2 和受控 real-Provider M2 验证 |
| Production private DinD | 生产私有 DinD 边界 | 本阶段未部署，NOT_PROVEN；不得由 host Docker 结果替代 |

## 仓库与合同基线

| 仓库 | 分支/基线 | 当前合同事实 |
| --- | --- | --- |
| Agent | `main` / baseline HEAD `520eed616c8c8f6a7a7fcdd5164028a5d86cd273` + dirty working tree | v0.4 byte lock保持；当前 descriptor 为v0.6 candidate |
| Backend | `main` / baseline HEAD `e9b2bdb7dc7316ada85c166b22e6f9b34a2e3a86` + dirty working tree | 消费 additive v0.6 candidate；current migration head 019，父018 |
| Frontend | `main` / baseline HEAD `5c581fbb` + dirty working tree | 消费同一 additive candidate；pinned v0.4 transport 保持兼容 |

当前 additive v0.6 candidate：147 entries、27,848 manifest bytes、manifest SHA-256 `11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05`。`refs/tags/agent-contracts-v0.6.0` 尚不存在，因此发布身份明确为 `NOT_PROVEN`；本任务未获授权创建 tag、commit 或 push。

## 里程碑一：权威 World 动作演出

### 当前实现结论

- Backend 只从已持久提交的 `WorldTransition` reducer steps 派生 HARVEST 展示投影；模型输出和原始沙箱 intent 不是展示权威。
- 展示流与 World Snapshot、聚合事件和 outbox 位于同一 PostgreSQL 事务，使用独立全局 sequence、稳定事件身份、哈希链和最终 Snapshot 绑定。
- 不支持、缺口、乱序、未知类型、身份或 payload/hash 损坏均 fail closed；客户端恢复权威 Snapshot，不推进伪成功。
- Godot 正式 AppRoot 形成 `TURN_RUNNING -> PLAYING -> COMPLETED`，支持 1x/2x、跳过、当前结果重播、重复去重和重启恢复。
- v0.4 `/events` 保持不变；本阶段仅使用 HTTP GET，没有 WSS 或 Event Batch。

### 正式 deterministic Gateway + Godot 跨进程验证

可复现入口（Backend 仓）：

```powershell
.\scripts\run-int1-local-diagnostic.ps1
```

当前树一次正式运行结果：`PASS`。

| 断言 | 结果 |
| --- | --- |
| 正式页面逐动作演出 | 精确播放 8 条已提交 HARVEST 展示事件，presentation high-watermark = 8 |
| UI 状态 | 正式 Workspace、Dialogue、World Viewport 均加载；运行期间观测到 PLAYING |
| World 权威终态 | world revision = 1、aggregate event sequence = 1；播放、恢复后的权威指纹一致 |
| 重启恢复 | Gateway、Workflow Worker、Learner Worker、Godot 恢复后指纹不变 |
| recovery-only | 8 次请求全部为 GET，0 次 mutation |
| PostgreSQL 暂时不可用 | Snapshot GET 先返回 500，数据库恢复后返回 200，最终权威指纹不变 |
| 学生旅程 | 三次失败后一次成功；命令状态 `REJECTED, REJECTED, REJECTED, APPLIED`，Run 状态 `REJECTED, REJECTED, REJECTED, SUCCEEDED` |
| 证据分类 | deterministic；不是 Real Provider 证据 |

已知且明确区分：正式 M2 已证明 settled Patch request/decision authority 在断库和跨进程重启后保持一致，但没有构造“服务端已提交、客户端未收到响应”的公开 Gateway pending write fault。因此 `live_pending_response_loss.status` 仍为 `NOT_PROVEN`；focused 跨 Store 恢复证据和本次断库/GET-only 恢复都不能把它提升为已证。

## 里程碑二：学生显式确认 Skill Patch

当前状态：`DETERMINISTIC PASS / REAL PROVIDER PASS`。

当前 full 门禁已实际复跑：Agent discovery 601，其中两条 billable live opt-in 为 `EXCLUDED_NOT_RUN`，599/599 non-live PASS、0 skip，脱敏 stdout SHA-256 `346666cb194561079fc280059b070da3e9a13e6c34666ae06cf869deca2740de`；Backend current-tree full 468/468、0 failure/error/skip，JUnit XML SHA-256 `852068818ADB98BEB12B830CEF27BBAE6928515C6CE3A1DF63FE6B43F3150DF6`；Frontend offline 60/60、0 failure，另有 2 条真实 E2E opt-in 精确排除，stdout SHA-256 `269E5D6BA4FDCEFBBDCF82E33FDA204C820AD942EAECA2312DDED37753D8C2E4`。历史 595-test RED、Backend 415/415 与 Frontend 59/59 均保留为历史证据，不再代表当前树。

已冻结的验收语义：

- 入口仅在 capability=true 时可见；学生必须显式提交 `request_ai_patch`，并引用一个当前、合同有效、可验证失败的 Interaction。
- 普通 v0.4 `hint` 只允许 level 0..3；Backend 从失败链权威重算 `failure_count >= 4`，最终 Proposal 才是 `teaching_agent + skill_patch + hint_level=4`。
- Patch 请求使用独立、持久的 Agent correction workflow，不创建 Sandbox Run、World、Build、Activation 或 Learner mastery 副作用。
- Proposal 只允许当前规范入口文件上的一次 `UPSERT_FILE`；Agent 无 Draft/Build/Activation/World 写能力。
- PatchDecision 的幂等 identity 绑定原始 HTTP request body SHA-256；相同 key 仅允许字节等价 replay。
- REJECT 除决策/回执外零业务副作用；ACCEPT 只原子创建下一不可变 Draft revision 并同步 Workspace。
- Build、Certification、Activation、Run 仍由学生分别触发；Learner 必须从稳定 provenance 得出 `used_skill_patch=true`，不得算独立掌握。

### 正式 deterministic Gateway + Godot M2 证据

可复现入口（Backend 仓）：

```powershell
.\scripts\run-int1-local-diagnostic.ps1 `
  -EnableWorldPresentation `
  -EnableSkillPatch
```

本次运行 exit 0，用时 270.638 秒；脱敏 stdout SHA-256 为 `90442f1f1171a6014f4025241bb71d3c7afc1d5b3e64499eccb30460dd3640dc`，最终分类为 `DETERMINISTIC_LOCAL_RELAY_ONLY_NOT_REAL_PROVIDER`。

| 断言 | 当前结果 |
| --- | --- |
| 学生公开 UI 链 | `REQUEST_PATCH → ACCEPT_PATCH → BUILD → ACTIVATE → SUBMIT`，状态 `PUBLIC_UI_CHAIN_CLOSED`；public-chain SHA-256 `102dcec526ca0ffd088cf5f465b3bcaab0af1e97fe0b60980f4833084fe63fff` |
| HTTP mutation | 精确 12 POST + 1 PUT；Patch ACCEPT 不伪装成第二次 Draft PUT |
| Command | 11 terminal = 1 Session + 2 Build + 2 Activation + 6 Turn；精确 7 `APPLIED` + 4 `REJECTED`，11 份 durable command receipt |
| 业务资源 | 6 Turn、5 Run、6 Interaction、5 Learner projection、2 Build、2 Activation、13 Evidence |
| deterministic Provider | 16 dispatch / 16 result / 16 generation，每个 dispatch `generation_count=1`；不是 Real Provider |
| Patch authority | Proposal 预览不改 Draft；ACCEPT 只产生 revision 3；后续 Build、Activate、Run 均由学生分别触发；patched Run 为 assisted、非独立掌握 |
| World 与演出 | World commit = 1、aggregate last sequence = 1；独立 presentation stream = 1、presentation events = 8，playback started/finished = 1/1 且观测到 `PLAYING` |
| PostgreSQL full-row authority | SHA-256 `a37d5c503d136396d0e4fe0f0f7f13594e6dc632c9095d2ae20b6a101b14e13a` |
| 真实断库 | 同一 PostgreSQL 容器端口不可用 3,785 ms；Gateway GET fail closed 为 500；恢复后 Gateway/workflow/learner 均建立新连接，四类指纹不变 |
| 第二进程 recovery-only | 17 GET / 0 mutation；数据库、relay、Sandbox、Artifact 指纹逐字节不变，持久文件精确清理 |

这次 PASS 证明正式 deterministic M2 与 settled authority 的断库/跨进程恢复；它本身不证明真实 Provider、production private DinD 或公开 Gateway pending write response-loss。

### 受控真实 Provider Gateway + Godot M2 证据

同一正式 harness 在受控双 flag 下执行一次真实 Provider 运行，run `868a` exit 0，用时 301.012 秒；脱敏 outer stdout SHA-256 为 `2A7D2C057EF66D54F4DBFA828166DAC0A688704471618E6FA2940CE4F95B2425`。

| 断言 | 当前结果 |
| --- | --- |
| Provider authority | `source=provider`、`degraded=false`；18 unique dispatch / 18 generation，单 dispatch 最大 `generation_count=1` |
| Provider relay response-loss | 注入的 ACK loss 恢复同一 dispatch，恢复后 `generation_count=1`；没有重复生成 |
| 学生公开 UI 链 | Patch 状态 `PUBLIC_UI_CHAIN_CLOSED`；学生显式确认后再分别 Build、Activate、Run |
| World 与演出 | World commit = 1、presentation events = 8 |
| 第二进程 recovery-only | phase2 精确 17 GET / 0 mutation；数据库、relay、Sandbox、Artifact 与 response-loss proxy 指纹不变 |
| PostgreSQL full-row authority | SHA-256 `b8bb2b568ac6978d938a98d041f9c5b74ef108167f53790c4bbeecbb6c051e30` |
| 清理 | 运行结束精确恢复到运行前原有 3 个 Docker 容器，没有遗留本次容器 |

这里通过的是 Provider relay response-loss proxy；它不构造公开 Gateway pending write 的 commit-ACK loss，因此后者仍为 `NOT_PROVEN`。同理，本机 host-Docker PASS 不外推为 production private DinD PASS。

## 当前完成门禁

1. [x] Agent 全量 non-live：599/599 PASS，2 条 live opt-in `EXCLUDED_NOT_RUN`，0 skip。
2. [x] Backend fresh PostgreSQL/Alembic/full pytest/Ruff/Pyright/compileall/contracts：468/468，0 failure/error/skip。
3. [x] Frontend full offline：60/60，0 failure；2 条 real opt-in 精确排除。
4. [x] deterministic 三仓 INT2 M2 E2E。
5. [x] 正式 Gateway + Godot 断库及跨进程 GET-only 恢复 E2E。
6. [x] 一次受控真实 Provider INT2 E2E：run `868a` 301.012 秒 PASS，18/18 generation、单 dispatch 最大 1。
7. [x] Backend/Frontend 与 Agent 核心文档的 current 状态镜像已同步。

本次真实 Provider PASS 同时证明 `source=provider`、`degraded=false`、每个 dispatch 的 `generation_count <= 1`，并以完整指纹证明 Provider、Patch、Sandbox、World、Interaction、Learner 无重复副作用；deterministic 与 real Provider 证据仍保持独立分类。

仍需保留的独立 `NOT_PROVEN` 边界：未发布的 `refs/tags/agent-contracts-v0.6.0`、production private DinD，以及公开 Gateway pending write response-loss。WSS、Event Batch 与 Feishu 是明确排除项，不应用 `NOT_PROVEN` 暗示它们属于本轮验收。

## 明确排除

WSS、Event Batch、Feishu、自动接受/应用 Patch、自动 Build/Certification/Activation/Run、多文件/删除/重命名/大规模重构、通用动画平台、第二 Agent 产品服务、第二产品数据库、Patch 自训练或自动晋级均不在本阶段范围。
