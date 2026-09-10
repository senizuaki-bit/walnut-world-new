# 服务失败提示与后端待处理项

## 本机最新状态（2026-09-10，用户授权本地修复后）

本机已完成模型配置迁移及 Agent 校验修复，正确代码的完整提交已通过。
以下“问题与结论”“本次前端修改”记录此前前端优化阶段；其他环境尚需同步本节变更。

- 使用现有启动参数 `-Model deepseek-flash`，统一 Gateway、Worker、Relay 的模型配置。
  本机入口为 `%LOCALAPPDATA%/WalnutWorld/persistent-play/start-play.ps1`，不修改共享启动脚本。
- 停止服务、备份数据库后，仅事务性更新当前 Agent Profile 的 `model_version` 和配置哈希。
  迁移时其他 48 张表的内容指纹不变，历史 Command、响应和学习记录保留。
  迁移前后通过前端只读恢复校验，会话、草稿、Skill、世界及交互游标一致。
- 迁移后首次运行已成功提交世界，但成长总结中的“不代表已经永久掌握”被 Agent 的
  关键词校验误拒绝。局部修复只豁免直接支配该词的明确否定表述，逐个匹配，
  保留对真正永久断言、混合语句和双重否定的拦截。
- Agent 修复位于本地联调分支 `codex/frontend-main-local-20260909` 的提交 `4adfa4c`，
  仅涉及 `agent/python/yaya_agent_runtime/validators.py` 及回归测试。
  `walnut-world-backend` 源码仍与 `origin/main` 基线一致；Agent 补丁未推送或合并到 main/fronted-art。
- 147 项 Agent 运行时测试及修改文件 Ruff 检查通过。真实前端 Run 完整通过，Command 为
  `APPLIED`，工作流首次执行即 `SUCCEEDED/COMPLETE`，成长总结为 `book_agent/provider`、
  `degraded=false`，最近三次模型请求和响应均为 `deepseek-flash`。
  验证 Run `run_8c9f85051e0a07295cf66441` 将世界版本从 6 更新到 7，草稿源码和版本不变。
- 备份位于本机 `%LOCALAPPDATA%/WalnutWorld/persistent-play/backups/model-migration-20260910-143147/`。
  恢复该备份会回到迁移前进度，不应为修复旧失败记录而覆盖后续新进度。

本地启动入口不保存语音密钥；当前运行服务沿用已授权的临时豆包密钥，销毁或完整重启后
需要通过环境变量或现有密钥文件机制重新提供。旧 DEAD_LETTER 记录保留，新提交已正常完成。

## 问题与结论

2026-09-10，正确代码已通过 Build 和 Activation，随后 Agent Turn 在执行 Skill 前失败。
已保存响应中的请求模型为 `deepseek-v4-flash`，返回模型为 `deepseek-flash`；使用生产校验函数
离线检查该响应，得到 `WorkflowInvariantError: Provider reply authority drifted`。
当天只读访问官方 `/models` 得到 `deepseek-flash`、`deepseek-v4-pro`。这证明当前公开标识变化，
不能仅凭名称证明底层模型版本完全等价。

## 本次前端修改

- 保留已验证 Command 的结构化错误，包括 Build、Activation 和失败 Turn 的恢复链路。
- 桥接层传递错误分类，以及当前源码是否确实已通过认证；每次操作清除过期错误。
- 区分编译错误、运行错误、目标未达成、服务失败、尚无法确认结果。
- 只有已认证源码才显示「代码检查已通过」。轮询超时不声称世界未变化或代码错误。
- 已知服务内部错误提示联系老师检查服务，无需因此反复修改答案。
- 标题悬停可查看格式受限的错误编号。原始消息、任意 details 和富文本不直接展示给学生。
- 保留修改入口、代码与世界投影；进入下一阶段时清理上次错误编号。

## 验证

八项 Godot 回归通过：Build、Activation、Submit/Run、Build feedback 恢复、pending Turn 重启恢复、
Turn 失败状态恢复、Crop bridge、Crop demo。覆盖后端错误保留、已认证源码遇到内部错误、超时
不确定性、编译错误、目标失败、错误编号过滤及修改入口。1280×720 实际渲染检查通过。
本轮未调用模型生成、未修改后端/Agent 源码或数据库，未重启运行服务。

## 其他环境仍需负责人同步

1. 运行配置：`scripts/start-persistent-play.ps1` 已支持 `-Model`，可用当前模型名称配置新运行。
   必须同步 Gateway、Worker、Relay 的模型配置，不能只替换一个进程的环境变量。
2. 持久化配置：尚未迁移的 `agent_profiles.profile_json.model_version` 使用旧名称，且配置有哈希。
   `persistent_play_authority.py` 会校验整个配置及哈希；`turn_worker.py` 会对照 Agent Profile、
   Command 与运行配置。需设计保留学习进度、草稿及历史凭据的数据迁移，不能只改启动参数、
   清库或批量替换历史 Command/响应。
3. 新配置验证：通过模型请求与响应身份校验，验证新提交产生真实 Run、Receipt、世界结果和反馈。
   已终结为 DEAD_LETTER 的旧任务不会因配置修复自动成功，应由用户显式发起新的提交。
4. 可选诊断改善：`adapters/postgres/durable_llm.py` 和 `workers/workflow_worker.py` 可保留安全的
   模型不一致原因及预期/实际标识。当前仅凭通用 INTERNAL_ERROR，前端无法断言具体就是模型配置。
5. Agent 校验：评估并同步本地提交 `4adfa4c` 的否定表述修复，不能只改模型配置就认为全链路已恢复。

优先尝试配置与受控数据迁移，保留 main 后端源码。若需源码或契约调整，由后端负责人评估；
不删除模型身份校验、不让前端伪造成功、不将上游原始响应改写为旧模型名。
