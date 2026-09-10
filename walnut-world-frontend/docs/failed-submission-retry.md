# 提交失败后的修改入口

2026-09-10，修复 fronted-art 中失败复盘页「我自己修改」不可点击的问题。

## 原因与决定

手动比较阶段 `_begin_manual_compare()` 禁用了 PrimaryButton。后续 `_set_phase(FAILED)`
只恢复显示和按钮文字，没有解除禁用。失败时编辑器入口隐藏，用户因此无法返回修改。

在进入 FAILED 时恢复 PrimaryButton 的点击状态，沿用已有 `_enter_code_phase()` 打开编辑器。
按钮恢复本身不重建草稿、不修改世界、不提交新的请求，也不解除运行中的按钮锁。
「这次验证没有完成」是通用提交失败提示，不能仅凭这句话认定代码编译错误。

## 验证

`tests/level_demo/crop_agent_bridge_test.gd` 先进入真实手动比较状态，再模拟正式提交超时。
修复前稳定失败于按钮仍被禁用；修复后通过，并验证点击后打开编辑器、保留代码及权威 Snapshot。
只修改前端，不改 main 后端或 Agent。
