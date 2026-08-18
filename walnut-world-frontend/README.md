# 核桃代码世界前端

本仓库承载核桃代码世界的正式 Godot 学生端。`scenes/app/app_root.tscn` 是 INT1/INT2 composition root：它只接收 Gateway base URL 与短期 Bearer token，不接受人工 Session ID；通过 Student Bootstrap 创建/恢复 Session，再恢复 Content、Workspace、Draft、exact active tuple、Snapshot、presentation 与 Interaction。

## 运行

使用 Godot 4.5.2 打开 `project.godot`，运行 `scenes/app/app_root.tscn`。学生端只访问唯一 `walnut-world-backend` Gateway；不直接调用模型、Docker、数据库、本地编译器或 sibling Agent HTTP。

## 接口边界

Wire合同唯一权威为sibling `../agent/contracts/manifest.json`及其引用文件。当前Frontend descriptor指向additive v0.6 candidate：147 entries、27,848-byte manifest、SHA-256 `11dde4ef0fd71de5f78afa8aaeef527ef72775a953b6399929245eb1c4d7ab05`；v0.4/v0.5历史字节继续锁定，v0.6 tag尚不存在，release identity为`NOT_PROVEN`。前端不得使用旧`/api/*`、猜authority或本地编译C++。

AppRoot 的产品链是 `Student Bootstrap → Session + starter Draft/Workspace → Draft CAS → Build/Certification → Activation → exact-version Turn → Run/World receipt/Evidence → HTTP Events/Snapshot → Learner/Product Interaction → recovery/display`。Command polling 使用真实 deadline、退避和 `Retry-After`；ClientStore 持久保存 exact tuple 与响应丢失 envelope。启动时若存在 pending Turn，AppRoot 必须在 READY 前以原 request/key 和 Turn 前 cursor 对账；闭包前不会按恢复后的 Workspace 高水位生成新 Turn identity。

当前三仓自动化为 Agent 601（599 PASS + 2 exact excluded）、Backend 468/468 PASS、Frontend offline 60/60 PASS；Frontend 两条 real opt-in 精确 `EXCLUDED_NOT_RUN`，不计入 60 条 PASS，当前 0 skip/fail。正式 deterministic actual10 与受控真实 Provider M2 均已 PASS：4 次客观失败后，学生通过 Request Patch 打开预览并在 Dialog 显式 `ACCEPT`，随后手动 Build、Activate 和 Run。真实 Provider 运行 `run868a` 用时 301.012 秒，DeepSeek `deepseek-v4-flash` 为 `source=provider`、`degraded=false`，18 unique dispatch / 18 generation、单 dispatch 最大 1；Provider relay response-loss 恢复同一 dispatch 且 generation 仍为 1。Patch 达到 `PUBLIC_UI_CHAIN_CLOSED`；Phase 1 为 12 POST/1 PUT、6 Turn/5 Run/11 terminal Command（7 `APPLIED` + 4 `REJECTED`）、1 条 World commit 与 8 条 presentation，Phase 2 为 17 GET/0 mutation，并恢复同一权威指纹。公开 Gateway pending write response-loss（不同于上述 Provider relay response-loss）仍为 `NOT_PROVEN`。

INT2 Patch入口只有在本地World/Patch flags与Backend capability全为true时才可用；Backend mutation route默认关闭并条件挂载。任一层false时入口不可用且零Patch POST。WSS、Client Event Batch、Feishu、自动应用/Build/Activate/Run与多文件Patch明确排除。
