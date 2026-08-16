# INT2 文档目标—当前实现—缺口—验收测试矩阵

> 初始审计日期：2026-08-14（Asia/Shanghai）；current evidence 收口日期：2026-08-15。
> 状态说明：`CURRENT PASS` 只用于本轮实际运行并可由仓库脚本复现的能力；`RED` 是已复核缺口；`NOT_PROVEN` 是尚未完成的门禁。deterministic 证据与 real Provider 证据严格分开。

| 目标 | 当前生产实现（审计事实） | 缺口 / 初始状态 | 必须闭合的验收测试 |
|---|---|---|---|
| 单一 Product Gateway / PostgreSQL authority | Backend 是唯一公开 Gateway、Alembic/PostgreSQL owner；Agent 为 library；Godot 只连 Gateway | 保持不回归 | 三仓 topology/port-surface/contract verifier；数据库无 `yaya_*` 产品表 |
| v0.4 字节兼容 | v0.4 保持 138 files、26,127 bytes、SHA-256 `b62a615...72b9`；v0.4/v0.5 previous-release lock 和泛化 verifier 已加入 | `CURRENT PASS`；当前 v0.6 candidate 为 147 entries、27,848 bytes、SHA-256 `11dde4...ab05`；Git tag 因未授权提交/打 tag 而 `NOT_PROVEN` | 三仓 contract verifier；最终报告明确 candidate/tag 状态 |
| Backend 权威逐动作投影 | `WorldEngine` 产生 immutable reducer steps；迁移 018 建立独立 append-only presentation stream；与 aggregate/Snapshot/Run/Evidence/Workspace 同事务 | `CURRENT PASS`（focused + fresh PostgreSQL + 正式 deterministic E2E） | 保留 reducer hash-chain、原子回滚、ACK-loss exactly-once 回归 |
| 展示事件来自已提交结果 | 只由 Backend reducer 的已验证 HARVEST before/after state 派生；不支持或混合动作写 durable gap marker | `CURRENT PASS`；原始 Sandbox/model payload 不具展示权威 | raw payload 篡改、不支持动作和 durable gap 反例持续全绿 |
| 展示身份和最终 Snapshot 闭合 | 每条事件闭合 request scope、commit、revision、全局 sequence、action index/count、稳定 ID、hash chain 和最终 Snapshot 三元组 | `CURRENT PASS` | exact-key/hash/identity/history/final Snapshot corruption matrix |
| HTTP presentation read | 新增默认关闭的只读 `/v1/worlds/{world_id}/presentation-events`；v0.4 aggregate endpoint 未变 | `CURRENT PASS`；冷读/分页/跨 commit/重复 GET/损坏均严格验证 | GET-only recovery audit；flag=false 404、POST 405 |
| 正式 Godot 逐动作 | AppRoot 正式装配严格 Gateway、Player 和 3D HARVEST renderer | `CURRENT PASS`：正式 deterministic Gateway/Godot 观察到 8 个 HARVEST、`TURN_RUNNING -> PLAYING -> COMPLETED` | full offline 与正式跨进程 harness 持续回归 |
| Player 安全性 | 不排序；整批预检；严格 known type/version/payload/hash/sequence；renderer 失败回权威 Snapshot | `CURRENT PASS` | corruption/late-failure/duplicate-only/跨页边界 focused tests |
| 2x / skip / replay | 正式 UI 已接 1x/2x、skip、当前结果 replay；replay 只改 renderer，不回写 ClientStore authority | `CURRENT PASS`（offline）；正式 deterministic 主路径证明 1x 播放，2x/skip/replay 由 focused 证明 | INT2 formal E2E 再覆盖控制路径最终指纹一致 |
| 重复/退出/重启 | Player 持久 cursor/high-watermark；重复事件不演出；冷启动完整分页校验；恢复绑定 presentation fingerprint | `CURRENT PASS`：Gateway/Workflow/Learner/Godot 重启后指纹一致，actual10 的 recovery-only 审计为 exact 17 GET / 0 mutation | INT2 formal E2E 继续验证 Patch 后成功路径 |
| Snapshot fallback 原子 | Player/renderer 先验证再表现；失败显式恢复 Snapshot 并收口验证后 HWM | `CURRENT PASS`（focused corruption + formal recovery） | 未知/缺口/篡改/退出矩阵持续全绿 |
| 本地预测从属 | 正式 main scene 不接入 demo/源码正则；本地移动只作用于预置 renderer，不写 ClientStore，下一次权威 Snapshot 会校正 avatar | `CURRENT PASS`：`world_viewport_presentation_test.gd` 同时锁定 ClientStore 不变、Snapshot 校正与正式 composition 静态禁用 demo/正则 seam | demo不得接入 AppRoot；权威动作/Snapshot抵达后校正；正式状态下本地移动不覆盖 authority |
| Patch capability 默认 false | v0.6只读capability GET始终挂载；presentation与Patch routes按flag条件挂载；Patch依赖World flag；Frontend双flag只能收紧 | `CURRENT PASS`：默认关闭与UI gate保持；正式M2仅在受控双flag下通过，不改变默认值 | flag=false route 404/capability false/UI入口不可用；任一层false零POST |
| 学生显式 Patch request | Frontend正式按钮、可见失败Interaction选择与pending request seam已实现；普通hint仍只允许0..3 | `CURRENT PASS`（deterministic formal）：正式按钮产生request/proposal；公开Gateway pending response-loss仍`NOT PROVEN` | 仅正式按钮产生稳定request；无点击零proposal；普通hint+4拒绝；response loss同identity恢复 |
| Agent eligibility AND 语义 | Agent已实现显式request/failure双scope、teaching RECTIFICATION L4、Draft/Evidence闭合与pre-Provider fail closed | `CURRENT PASS`：Agent full 599/599 non-live、0 skip，formal proposal闭合第四次失败 | false/非teaching/hint<4/无失败Evidence/Draft漂移均零Proposal；fallback/degraded零Patch |
| 单入口一次 UPSERT | v0.4保留宽superset；INT2 Agent policy只形成一次当前entrypoint全文件UPSERT | `CURRENT PASS`：focused/contract回归、Backend current-tree full 468/468 与 formal public链闭合当前entrypoint一次UPSERT | multi-op/delete/rename/path/entrypoint/content hash篡改全拒绝 |
| 精确预览和确认 | 正式Dialog展示before/after、operation/path/hash/Evidence，Accept/Reject为独立按钮；关闭不等于Reject | `CURRENT PASS`：formal状态`PUBLIC_UI_CHAIN_CLOSED`，预览前后Draft不变，确认后才产生revision3 | 正式UI逐字段闭合且未确认零Draft mutation |
| Patch Decision 幂等/CAS | Backend 019实现与Frontend pending decision recovery已存在；route默认关闭 | `CURRENT PASS`（fresh PG + settled formal restart）；公开Gateway commit-ACK loss注入仍`NOT PROVEN` | identical replay/concurrent identical；payload conflict；stale CAS；commit-ACK loss/restart |
| REJECT 零副作用 | policy与Backend real-PG focused回归禁止Draft/Build/Activation/Run/World/Learner副作用 | `CURRENT PASS`：独立REJECT fingerprint、Backend current-tree full 468/468 与 real-Provider主链均未重复生成REJECT proposal | 决策/receipt外业务指纹不变；同key重放不增行 |
| ACCEPT只产生新 Draft | 019建立immutable Draft revision/assistance；ACCEPT只追加next revision并同步Workspace | `CURRENT PASS`：formal链exact产生revision3，无自动Build/Activation/Run | exact one immutable next revision + Workspace same tx；故障全回滚；无自动Build等 |
| Frontend decision restart | ClientStore/AppRoot恢复pending PatchDecision且保留stable key/body | focused pending恢复与formal settled-authority跨进程恢复均PASS；公开Gateway ACK-loss仍`NOT PROVEN` | ACCEPT/REJECT lost response启动先reconcile；已决proposal不再弹窗 |
| ACCEPT后手动三步 | ACCEPT后读取canonical Draft并清空旧activation readiness；Build/Activate/Run仍是正式独立按钮 | `CURRENT PASS`：formal actions精确为`REQUEST_PATCH, ACCEPT_PATCH, BUILD, ACTIVATE, SUBMIT` | dirty edit零POST；ACCEPT后须新Build->Activate->Run分别点击 |
| Patch provenance | Backend 019建立Draft/Build/Certification/Activation/Run稳定FK/hash图 | `CURRENT PASS`：formal patched Run、focused篡改反例与Backend current-tree full 468/468已闭合 | exact Draft ref build；missing/wrong link拒绝；禁止latest-patch推断 |
| Learner assistance | Backend/Agent已有assistance authority与阻止独立晋级实现 | `CURRENT PASS`：formal patched success投影`used_skill_patch=true`，learner revision5，重启指纹一致 | used_skill_patch=true；independent mastery不增长；worker restart/fence stale |
| Provider/Sandbox/Gateway 丢响应 | durable relay、Sandbox receipt、workflow fencing与完整副作用指纹已装配 | `CURRENT PASS`（Provider relay/Sandbox）：deterministic 16/16，real-Provider run `868a` 为18/18、单 dispatch generation最大1，ACK loss同dispatch恢复；公开Gateway pending write fault仍`NOT_PROVEN` | provider/sandbox/gateway ACK loss不重复；generation_count<=1；Patch/World/Learner exactly-once |
| Backend full门禁 | current head019/父018；旧415/415及其JUnit保留为历史快照 | `CURRENT PASS`：current-tree full 468/468、0 failure/error/skip；JUnit SHA-256 `852068818ADB98BEB12B830CEF27BBAE6928515C6CE3A1DF63FE6B43F3150DF6` | fresh PostgreSQL zero->new head；full pytest 0 skip；全静态与合同门禁 |
| Frontend full offline | 当前runner执行60条offline，另有2条real opt-in精确排除；旧59/59保留为历史快照 | `CURRENT PASS`：60/60、0 failure；stdout SHA-256 `269E5D6BA4FDCEFBBDCF82E33FDA204C820AD942EAECA2312DDED37753D8C2E4` | 后续变更持续full offline；不可用offline替代real Provider |
| deterministic三仓 | 正式学生链通过公开UI请求/确认Patch，再手动Build/Activate/Run并演出8个HARVEST | `CURRENT PASS`：Turn6/Run5/Interaction6；Command11=7 APPLIED+4 REJECTED；dispatch/generation16/16 | Patch request→preview→ACCEPT/REJECT→manual Build/Activate/Run 全链 |
| 正式跨进程恢复 | M2 harness真实stop/start PostgreSQL并重启Gateway/Workflow/Learner/Godot；deterministic DB SHA `a37d5c...14e13a`，real-Provider DB SHA `b8bb2b...51e30` | `CURRENT PASS`：两类M2的phase2均为17 GET/0 mutation，DB/relay/Sandbox/Artifact指纹不变；live结束精确恢复原有3个容器 | Patch/World/Build/Sandbox/Interaction/Learner 指纹不变 |
| 一次受控真实 Provider | 2026-08-15 run `868a`；M2 deterministic仍作为独立非计费证据 | `CURRENT PASS`：301.012秒，`source=provider`、`degraded=false`、18 unique dispatch / 18 generation、max1，`PUBLIC_UI_CHAIN_CLOSED`，World commit1/presentation8，phase2 17 GET/0 mutation | 继续保持单次受控运行；不得外推production private DinD或公开Gateway pending write fault |
| 文档与证据 | 核心报告、矩阵与三仓状态镜像已区分current/historical/deterministic/real Provider | `CURRENT PASS`：Backend 468、Frontend 60与run `868a` current事实已回填；v0.6 tag、private DinD及公开Gateway pending write仍保留`NOT_PROVEN` | 持续明确current/historical/deterministic/real Provider、host Docker/DinD、已证/未证/排除 |

## 初始独立审计结论

- 初始矩阵来自三名只读独立审计；其后实现与文档收口已修改工作树，本节不再描述当前状态。
- 里程碑一、M2 deterministic/formal/recovery与受控 real-Provider M2现均为current PASS；Patch capability继续默认关闭是发布策略，不得把受控双flag验收误写成默认上线。
- 上表只有在相应测试和跨进程证据真实产生后才能改成 PASS；deterministic fixture 不得写为 real Provider。
